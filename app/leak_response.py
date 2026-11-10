"""F22 coolant leak → cordon → workload move.

Input: a Redfish LeakDetection event (DMTF Environmental registry
`Environmental.1.5.LeakDetectedCritical|LeakDetectedWarning`, origin
`/redfish/v1/Chassis/{tray}/ThermalSubsystem/LeakDetection`) arriving through the
AI-Infra EventService push → `/api/v1/oob/events` → NOCP bus topic `leak`.
The event is converted to an NVSentinel HealthEvent
(`NVIDIA/NVSentinel data-models/protobufs/health_event.proto`, Apache-2.0:
agent, componentClass, checkName, isFatal, isHealthy, recommendedAction,
entitiesImpacted[{entityType, entityValue}], nodeName, generatedTimestamp) and
then follows the NVSentinel order fault-quarantine (cordon) → node-drainer →
fault-remediation (replace). The leak monitor and the DSX Exchange
`mission-control/leak-response` message are not public (empty upstream file),
so the response record is a NeoCloud extension schema
(`ref/upstream/neocloud-ext/leak-response.schema.json`, x-neocloud).

  Critical  cordon the tray's NICo host; NVKE node → F10 replace run
            (app.remediation.start_run when merged, else nvke.replace_node);
            BMaaS node → customer ticket + status notice.
  Warning   status notice only.

  GET /api/v1/oob/leak-responses[?state=]   GET /api/v1/oob/leak-responses/{id}
"""

from __future__ import annotations

import importlib
import itertools
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from . import events
from .store import STORE

router = APIRouter(prefix="/api/v1/oob", tags=["leak-response"])

KPI_MOVE_S = 300                     # plan: replace run within 5 min (emulated)
AGENT = "neocloud-leak-monitor"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Leak:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        self.responses: dict[str, dict] = {}
        self.seen: set = set()
        self._seq = itertools.count(1)


LEAK = _Leak()


def reset() -> None:
    with LEAK.lock:
        LEAK.reset()


def _module(name: str):
    try:
        return importlib.import_module(f"app.{name}")
    except ImportError:
        return None


def _severity(ev: dict) -> str:
    d = ev.get("data") or {}
    mid = str(d.get("message_id") or "")
    if mid.endswith("Critical"):
        return "Critical"
    if mid.endswith("Warning"):
        return "Warning"
    if mid.endswith(("Normal", "Restored")):
        return "OK"
    sev = str(d.get("severity") or "OK").capitalize()
    return sev if sev in ("Critical", "Warning") else "OK"


def _trays(d: dict) -> list[str]:
    m = d.get("machine")
    if m:
        t = m.removeprefix("nh-")
        return [t] if t in STORE.trays else []
    rack = STORE.racks.get(d.get("rack") or "")
    return list(rack.tray_ids) if rack else []


def health_event(tray_id: str, severity: str, message: str) -> dict:
    """NVSentinel HealthEvent (proto field names, JSON mapping)."""
    critical = severity == "Critical"
    return {"version": 1, "agent": AGENT, "componentClass": "Tray",
            "checkName": "LeakDetection", "isFatal": critical, "isHealthy": False,
            "message": message or f"coolant leak {severity} on {tray_id}",
            "recommendedAction": "REPLACE_VM" if critical else "CONTACT_SUPPORT",
            "errorCode": ["Environmental.1.5.LeakDetected" + severity],
            "entitiesImpacted": [{"entityType": "Tray", "entityValue": tray_id}],
            "metadata": {"rack": STORE.trays[tray_id].rack_id},
            "generatedTimestamp": _now(), "nodeName": f"nh-{tray_id}",
            "processingStrategy": "EXECUTE_REMEDIATION" if critical else "STORE_ONLY",
            "id": f"he-{tray_id}-{int(time.time())}"}


def _nvke_node(host: str):
    from . import nvke
    for c in nvke.NVKE.clusters.values():
        if c.state == "deleted":
            continue
        for pid in c.nodepool_ids:
            for n in nvke.NVKE.nodepools[pid].nodes:
                if n.nico_host_id == host or n.tray_id == host.removeprefix("nh-"):
                    return c, n
    return None, None


def _host_of(tray_id: str) -> str:
    for ni in STORE.node_instances.values():
        if ni.tray_id == tray_id:
            return ni.nico_host_id
    return f"nh-{tray_id}"


def _step(resp: dict, name: str, state: str = "done", **kw) -> None:
    resp["steps"].append({"name": name, "state": state, "at": _now(), **kw})


def _notice(resp: dict, severity: str) -> None:
    body = {"kind": "incident", "components": ["bmaas", "managed-k8s"],
            "start": resp["detected_at"], "end": None,
            "message_ko": f"랙 {resp['rack']} 트레이 {resp['tray_id']} 냉각수 누수({severity}) 대응 중",
            "message_en": f"Coolant leak ({severity}) on rack {resp['rack']} tray "
                          f"{resp['tray_id']} — response in progress"}
    m = _module("status_notices")
    if m and hasattr(m, "create_notice"):
        res = m.create_notice(body)
        resp["notice_id"] = (res or {}).get("id") if isinstance(res, dict) else str(res)
        resp["notice"] = {"via": "F13"}
    else:
        resp["notice"] = {**body, "via": "stub"}


def _ticket(resp: dict, tenant_id: str) -> None:
    from .business import create_ticket
    from .models import TicketCreate
    t = create_ticket(TicketCreate(
        tenant_id=tenant_id, severity="high", type="tech", ref=resp["id"],
        subject=f"[누수] 트레이 {resp['tray_id']} 격리 — 워크로드 이전 필요",
        body=(f"Coolant leak Critical on {resp['tray_id']} (rack {resp['rack']}). "
              "The BMaaS node is cordoned; please move the workload. "
              "냉각수 누수로 노드를 격리했습니다. 워크로드 이전이 필요합니다.")))
    resp["ticket_id"] = t.id


def respond(tray_id: str, severity: str, event_id: str, message: str = "") -> dict:
    t0 = time.time()
    with LEAK.lock:
        rid = f"leak-{next(LEAK._seq):04d}"
        rack = STORE.trays[tray_id].rack_id
        host = _host_of(tray_id)
        resp = {"id": rid, "event_id": event_id, "tray_id": tray_id, "rack": rack,
                "machine_id": host, "severity": severity, "detected_at": _now(),
                "health_event": health_event(tray_id, severity, message),
                "steps": [], "kind": None, "tenant_id": None, "cluster_id": None,
                "remediation_run_id": None, "replacement": None, "ticket_id": None,
                "state": "open", "kpi_s": KPI_MOVE_S}
        LEAK.responses[rid] = resp
        _step(resp, "health-event", source="oob.leak")
        if severity != "Critical":
            _notice(resp, severity)
            _step(resp, "notice")
            resp.update(kind="notice-only", state="done")
            return _finish(resp, t0)
        from .lifecycle import get_adapter
        try:
            get_adapter().cordon(host, f"coolant leak Critical ({event_id})")
            _step(resp, "cordon", machine=host)
        except Exception as e:                    # noqa: BLE001 — host may be gone
            _step(resp, "cordon", "failed", error=str(e)[:200])
        cluster, node = _nvke_node(host)
        ni = next((n for n in STORE.node_instances.values() if n.tray_id == tray_id), None)
        if cluster is not None:
            resp.update(kind="nvke", tenant_id=cluster.tenant_id, cluster_id=cluster.id)
            rem = _module("remediation")
            if rem and hasattr(rem, "start_run"):
                run = rem.start_run(machine_id=host, reason="coolant leak Critical",
                                    trigger={"event_id": event_id, "kind": "leak"})
                resp["remediation_run_id"] = (run or {}).get("id")
                _step(resp, "remediation-run", run_id=resp["remediation_run_id"], via="F10")
            else:
                from .nvke import replace_node
                _step(resp, "drain", node=node.name)
                rec = replace_node(cluster.id, node.name, f"coolant leak Critical ({event_id})",
                                   source="leak-response")
                resp["replacement"] = rec
                resp["remediation_run_id"] = rec["id"]
                _step(resp, "replace", old_host=rec["old_host"], new_host=rec["new_host"],
                      via="nvke.replace_node (F10 stub)")
            resp["state"] = "moved"
        elif ni is not None and ni.tenant_id:
            resp.update(kind="bmaas", tenant_id=ni.tenant_id)
            _ticket(resp, ni.tenant_id)
            _step(resp, "ticket", ticket_id=resp["ticket_id"])
            _notice(resp, severity)
            _step(resp, "notice")
            resp["state"] = "awaiting-customer"
        else:
            resp.update(kind="idle", state="cordoned")
        return _finish(resp, t0)


def _finish(resp: dict, t0: float) -> dict:
    resp["elapsed_s"] = round(time.time() - t0, 3)
    resp["within_kpi"] = resp["elapsed_s"] <= KPI_MOVE_S
    events.publish("health", {"event": "LeakResponse", "id": resp["id"],
                              "tray_id": resp["tray_id"], "kind": resp["kind"],
                              "state": resp["state"]}, source="nocp.leak-response")
    return resp


def _on_event(ev: dict) -> None:
    if ev.get("topic") != "leak" or ev.get("source") == "nocp.leak-response":
        return
    d = ev.get("data") or {}
    sev = _severity(ev)
    if sev == "OK":
        return
    raw = d.get("raw") if isinstance(d.get("raw"), dict) else {}
    key = raw.get("EventId") or d.get("id") or ev.get("id")
    for tray in _trays(d)[:18]:
        with LEAK.lock:
            open_same = any(r["tray_id"] == tray and r["severity"] == sev and
                            r["state"] not in ("done",) for r in LEAK.responses.values())
            if (key, tray) in LEAK.seen or open_same:
                continue
            LEAK.seen.add((key, tray))
        respond(tray, sev, str(d.get("message_id") or key), d.get("message") or "")


events.add_listener(_on_event)


@router.get("/leak-responses")
def list_responses(state: Optional[str] = None, tenant_id: Optional[str] = None) -> dict:
    with LEAK.lock:
        return {"items": [r for r in LEAK.responses.values()
                          if (not state or r["state"] == state)
                          and (not tenant_id or r["tenant_id"] == tenant_id)]}


@router.get("/leak-responses/{rid}")
def get_response(rid: str) -> dict:
    with LEAK.lock:
        r = LEAK.responses.get(rid)
        if not r:
            raise HTTPException(404, f"leak response {rid} not found")
        return r
