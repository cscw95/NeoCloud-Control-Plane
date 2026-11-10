"""F10 — auto-recovery chain (NVSentinel-style).

Flow follows NVIDIA/NVSentinel v1.31.0 (7ce200c634fd, Apache-2.0):
  health monitor   HealthEvent (data-models/protobufs/health_event.proto)
                   via PlatformConnector.HealthEventOccurredV1(HealthEvents)
  fault-quarantine fault-quarantine/pkg/reconciler/reconciler.go — CEL rule
                   "event.agent == 'gpu-health-monitor' && event.componentClass
                   == 'GPU' && event.isFatal == true" → cordon.shouldCordon
  node-drainer     node-drainer/pkg/reconciler/reconciler.go — evict user pods
  fault-remediation fault-remediation/pkg/reconciler/reconciler.go — maps
                   recommendedAction to a janitor CR (janitor.dgxc.nvidia.com/
                   v1alpha1 GPUReset | RebootNode), executed by janitor/pkg/
                   controller/{gpureset,rebootnode}_controller.go
Status strings and HealthEventStatus fields are the proto's. NeoCloud adds
what NVSentinel leaves to the platform: re-validation, replacement from the
spare pool (NVKE: app.nvke.replace_node, F11; BMaaS: spare tray swap), RMA of
the faulty node (app.ncp RMA burn-in → pool return) and the SLA downtime
record (app.sla, F09). The janitor executes the reset through NICo
(POST machine/{id}/gpu/reset → AI-Infra Redfish Systems/{id}/Actions/GpuReset).

Timing is a simulated clock (minutes per step, SIM_MINUTES) anchored at the
event time, so MTTR is deterministic. Default mode is "monitor" (deck NOTE:
quarantine/remediation is opt-in): events are stored (processingStrategy
STORE_ONLY) and nothing is touched.

  GET|PUT /api/v1/remediation/policy
  GET     /api/v1/remediation/runs?tenant_id=&state=   GET /runs/{id}
  POST    /api/v1/remediation/runs {machine_id, reason}
  POST    /api/v1/remediation/health-events  (HealthEvents JSON)   GET (list)
  POST    /api/v1/remediation/inject {machine_id, xid, gpu, persistent}
          fault injection: AI-Infra /emulator/v1/oob/inject sel_critical (SEL
          evidence) + NICo machine/{id}/gpu/fault; AI-Infra has no XID
          injection; integration/v2: NICo gpu/fault drives the AI-Infra
          gpu-faults twin (DCGM XID, Redfish health, GpuReset Task), the local
          fault table is the offline fallback.
  POST    /api/v1/remediation/reset
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import dsx, events
from .models import HwState
from .store import STORE

router = APIRouter(prefix="/api/v1/remediation", tags=["remediation"])

# Simulated minutes per step (MTTR clock). Tenant-facing downtime ends at
# "uncordon" (recovered) or "replace" (replaced); RMA steps run after that.
SIM_MINUTES = {"detect": 0, "quarantine": 1, "drain": 4, "gpu_reset": 3,
               "validate": 4, "uncordon": 1, "replace": 10, "rma_open": 1,
               "isolate": 2, "repair": 30, "burn_in": 45, "pool_return": 2}

# XID → RecommendedAction (proto enum names). [미검증: NVSentinel XID catalog
# (syslog-health-monitor/pkg/xid) not copied; values follow NVIDIA XID docs.]
XID_ACTION = {13: "NONE", 31: "NONE", 43: "NONE", 45: "NONE",
              48: "COMPONENT_RESET", 63: "COMPONENT_RESET", 64: "RUN_FIELDDIAG",
              74: "COMPONENT_RESET", 79: "RESTART_BM", 92: "COMPONENT_RESET",
              94: "COMPONENT_RESET", 95: "RESTART_BM", 119: "COMPONENT_RESET",
              120: "COMPONENT_RESET"}
# fault-remediation values.yaml maintenance.actions → janitor CR kind
ACTION_CR = {"COMPONENT_RESET": "GPUReset", "RESTART_BM": "RebootNode",
             "RESTART_VM": "RebootNode"}
JANITOR_API = "janitor.dgxc.nvidia.com/v1alpha1"


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Actions(BaseModel):
    cordon: bool = True
    drain: bool = True
    gpu_reset: bool = True
    replace: bool = True
    rma: bool = True


class Policy(BaseModel):
    mode: str = "monitor"                       # monitor | auto
    actions: Actions = Field(default_factory=Actions)
    max_parallel_per_rack: int = 1
    kpi_replace_minutes: int = 60


class RunCreate(BaseModel):
    machine_id: str
    reason: str = "operator"


class InjectBody(BaseModel):
    machine_id: str
    xid: int = 79
    gpu: int = 0
    persistent: bool = False


class _RemState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        self.policy = Policy()
        self.runs: dict[str, dict] = {}
        self.health_events: list[dict] = []
        self.faults: dict[str, dict] = {}          # host_id → {xid, gpu, persistent}
        self.rack_busy_until: dict[str, datetime] = {}
        self.seen_events: set = set()              # bus event keys already turned into HealthEvents
        self.seq = 0


REM = _RemState()


def reset() -> None:
    with REM.lock:
        REM.reset()


# ---------------------------------------------------------------------------
# target resolution
# ---------------------------------------------------------------------------
def _host_id(machine: str) -> str:
    return machine if machine.startswith("nh-") else f"nh-{machine}"


def _target(machine_id: str) -> dict:
    from .ncp import _nvke_node
    host = _host_id(machine_id)
    tray = STORE.trays.get(host.removeprefix("nh-"))
    hit = _nvke_node(host)
    if tray is None and hit is None:
        raise HTTPException(404, f"machine '{machine_id}' not found")
    t = {"machine_id": host, "tray_id": tray.id if tray else None,
         "rack_id": tray.rack_id if tray else None,
         "tenant_id": tray.tenant_id if tray else None,
         "cluster_id": None, "node": None, "instance_id": None, "product": "bmaas"}
    if hit:
        cid, node = hit
        from .nvke import NVKE
        t.update(cluster_id=cid, node=node.name, instance_id=node.instance_id,
                 product="nvke", tenant_id=NVKE.clusters[cid].tenant_id)
    return t


# ---------------------------------------------------------------------------
# HealthEvent (NVSentinel proto shape)
# ---------------------------------------------------------------------------
def health_event(host: str, xid: int, gpu: int = 0, message: str = "") -> dict:
    return {"version": 1, "agent": "gpu-health-monitor", "componentClass": "GPU",
            "checkName": "GpuXidError", "isFatal": XID_ACTION.get(xid, "COMPONENT_RESET") != "NONE",
            "isHealthy": False,
            "message": message or f"XID {xid} on GPU{gpu}",
            "recommendedAction": XID_ACTION.get(xid, "COMPONENT_RESET"),
            "errorCode": [str(xid)],
            "entitiesImpacted": [{"entityType": "GPU", "entityValue": str(gpu)}],
            "metadata": {"xid": str(xid)},
            "generatedTimestamp": _iso(_utcnow()), "nodeName": host,
            "quarantineOverrides": {"force": False, "skip": False},
            "drainOverrides": {"force": False, "skip": False},
            "processingStrategy": "EXECUTE_REMEDIATION",
            "id": str(uuid.uuid4()), "customRecommendedAction": ""}


def _quarantine_rule(ev: dict) -> bool:
    """fault-quarantine values.yaml "GPU fatal error ruleset" (CEL) +
    the syslog ruleset (GPU|NIC)."""
    return (ev.get("isFatal") is True
            and ((ev.get("agent") == "gpu-health-monitor" and ev.get("componentClass") == "GPU")
                 or (ev.get("agent") == "syslog-health-monitor"
                     and ev.get("componentClass") in ("GPU", "NIC"))))


def _status() -> dict:
    return {"nodeQuarantined": "", "quarantineFinishTimestamp": None,
            "userPodsEvictionStatus": {"status": "NotStarted", "message": ""},
            "drainFinishTimestamp": None, "faultRemediated": None,
            "lastRemediationTimestamp": None, "spanIds": {},
            "drainDecision": None, "remediationDecision": None}


def ingest(ev: dict, trigger_kind: str = "xid") -> dict:
    """PlatformConnector entry: store the event, run the chain in auto mode."""
    with REM.lock:
        rec = {"createdAt": _iso(_utcnow()), "healthEvent": ev, "healthEventStatus": _status()}
        REM.health_events.append(rec)
        run = None
        if REM.policy.mode != "auto" or not _quarantine_rule(ev):
            ev["processingStrategy"] = "STORE_ONLY"
            return {"health_event": rec, "run": None}
        run = _start(ev["nodeName"], ev.get("message") or "health event",
                     {"event_id": ev["id"], "kind": trigger_kind}, ev, rec)
        return {"health_event": rec, "run": run}


# ---------------------------------------------------------------------------
# chain
# ---------------------------------------------------------------------------
def _publish(kind: str, run: dict, **extra) -> None:
    events.publish("health", {"type": kind, "run_id": run["id"],
                              "machine_id": run["target"]["machine_id"],
                              "tenant_id": run["target"]["tenant_id"], **extra},
                   source="nocp.remediation")


class _Clock:
    def __init__(self, t0: datetime) -> None:
        self.t = t0

    def step(self, name: str) -> str:
        self.t += timedelta(minutes=SIM_MINUTES.get(name, 1))
        return _iso(self.t)


def _step(run: dict, clock: _Clock, name: str, state: str = "Succeeded", **detail) -> dict:
    s = {"name": name, "state": state, "at": clock.step(name), **detail}
    run["steps"].append(s)
    _publish("RemediationStepDone", run, step=name, state=state)
    return s


def _gpu_reset(run: dict, host: str, cr_kind: str) -> tuple[str, str]:
    """Execute the janitor CR through NICo. → (result, via)."""
    path = dsx.nico_rest_path("machine_gpu_reset", id=host)
    if cr_kind == "RebootNode":
        st, body = dsx.nico_send("PATCH", dsx.nico_rest_path("machine_power", id=host),
                                 json={"action": "ForceRestart",
                                       "acknowledgeAttachedInstance": True})
    else:
        st, body = dsx.nico_send("POST", path, json={"acknowledgeAttachedInstance": True})
    fault = REM.faults.get(host)
    if st is not None and st < 400 and isinstance(body, dict):
        result = body.get("resetResult") or ("Failed" if fault and fault["persistent"]
                                             else "Succeeded")
        via = "nico"
    else:
        result = "Failed" if fault and fault["persistent"] else "Succeeded"
        via = "local"
    if result == "Succeeded":
        REM.faults.pop(host, None)
    return result, via


def _healthy(host: str) -> tuple[bool, str]:
    """Re-validation: NICo machine health (PreventAllocations alerts)."""
    body = dsx.nico_get(dsx.nico_rest_path("machine_one", id=host), timeout=1.5)
    if isinstance(body, dict) and isinstance(body.get("health"), dict):
        bad = [a for a in body["health"].get("alerts") or []
               if "PreventAllocations" in (a.get("classifications") or [])]
        return not bad, "nico"
    return host not in REM.faults, "local"


def _cr(kind: str, host: str, gpu: str, phase: str, t: str) -> dict:
    spec = {"nodeName": host}
    if kind == "GPUReset":
        spec["selector"] = {"uuids": [f"GPU-{uuid.uuid5(uuid.NAMESPACE_URL, host + '/' + gpu)}"]}
    else:
        spec["force"] = False
    ok = phase == "Succeeded"
    status = {"startTime": t, "completionTime": t,
              "conditions": [{"type": "Complete", "status": "True" if ok else "False",
                              "reason": phase, "message": f"{kind} {phase}",
                              "lastTransitionTime": t, "observedGeneration": 1}]}
    if kind == "GPUReset":
        status["phase"] = phase
    return {"apiVersion": JANITOR_API, "kind": kind,
            "metadata": {"name": f"{kind.lower()}-{host}"}, "spec": spec, "status": status}


def _replace(run: dict, t: dict) -> dict:
    if t["product"] == "nvke":
        from .nvke import replace_node
        rec = replace_node(t["cluster_id"], t["node"], f"remediation {run['id']}",
                           source="nvsentinel")
        return {"kind": "nvke-node", "old_host": rec["old_host"], "new_host": rec["new_host"],
                "replacement_id": rec["id"]}
    tray = STORE.trays.get(t["tray_id"] or "")
    if tray is None or not t["tenant_id"]:
        return {"kind": "none", "detail": "unassigned machine — nothing to replace"}
    bp = STORE.racks[tray.rack_id].blueprint_key
    spare = next((x for x in sorted(STORE.trays.values(), key=lambda x: x.id)
                  if x.state == HwState.ready and x.tenant_id is None
                  and STORE.racks[x.rack_id].blueprint_key == bp
                  and _host_id(x.id) not in REM.faults), None)
    if spare is None:
        raise HTTPException(409, "no spare tray in pool")
    spare.tenant_id, spare.state = t["tenant_id"], HwState.allocated
    tray.tenant_id, tray.state = None, HwState.maintenance
    return {"kind": "bmaas-tray", "old_host": t["machine_id"], "new_host": _host_id(spare.id),
            "detail": "spare tray from site pool (same blueprint)"}


def _rma(run: dict, clock: _Clock, t: dict) -> None:
    from .ncp import RMA_STAGES, RmaCreate, _run_stage, open_case, _publish as rma_pub
    leak = run["trigger"]["kind"] == "leak"     # F22: facility repair, case stays open at isolate
    case = open_case(RmaCreate(machine=t["machine_id"], component="Tray" if leak else "GPU",
                               reason=run["reason"], rack=t["rack_id"],
                               tenant=t["tenant_id"], source="nvsentinel"))
    run["rma_id"] = case["id"]
    _step(run, clock, "rma_open", rma_id=case["id"])
    for stage, name in (("cordon_drain", None), ("isolate", "isolate"),
                        ("replace", "repair"), ("burn_in", "burn_in"),
                        ("pool_return", "pool_return")):
        if stage == "pool_return":
            REM.faults.pop(t["machine_id"], None)   # repaired part
            dsx.nico_send("DELETE", dsx.nico_rest_path("machine_gpu_reset", id=t["machine_id"])
                          .replace("/gpu/reset", "/gpu/fault"))
        rec = _run_stage(case, stage)
        case["steps"].append(rec)
        case["stage_index"] = RMA_STAGES.index(stage)
        case["stage"] = stage
        rma_pub(case)
        if name:
            _step(run, clock, name, rma_stage=stage, **({"validation": rec["validation"]}
                                                       if "validation" in rec else {}))
        if leak and stage == "isolate":
            return
    case["closed_at"] = clock.t.isoformat(timespec="seconds")


def _start(machine_id: str, reason: str, trigger: dict, ev: Optional[dict] = None,
           ev_rec: Optional[dict] = None) -> dict:
    with REM.lock:
        t = _target(machine_id)
        pol = REM.policy
        REM.seq += 1
        rid = f"rem-{REM.seq:04d}"
        if ev is None:
            f = REM.faults.get(t["machine_id"], {})
            ev = health_event(t["machine_id"], f.get("xid", 79), f.get("gpu", 0), reason)
            ev_rec = {"createdAt": _iso(_utcnow()), "healthEvent": ev,
                      "healthEventStatus": _status()}
            REM.health_events.append(ev_rec)
        detected = datetime.fromisoformat(ev["generatedTimestamp"])
        run = {"id": rid, "trigger": {"event_id": trigger.get("event_id") or ev["id"],
                                      "kind": trigger.get("kind", "xid")},
               "target": {k: t[k] for k in ("machine_id", "tenant_id", "cluster_id",
                                            "rack_id", "instance_id", "product")},
               "reason": reason, "recommended_action": ev["recommendedAction"],
               "state": "running", "steps": [], "mttr_min": None, "outcome": None,
               "remediation_cr": None, "replacement": None, "rma_id": None,
               "kpi_replace_minutes": pol.kpi_replace_minutes, "kpi_met": None,
               "detected_at": _iso(detected), "completed_at": None,
               "health_event_id": ev["id"]}
        REM.runs[rid] = run
        clock = _Clock(detected)
        _publish("RemediationStarted", run, action=ev["recommendedAction"])
        _step(run, clock, "detect", event_id=ev["id"], xid=(ev.get("errorCode") or [None])[0])
        busy = REM.rack_busy_until.get(t["rack_id"] or "")
        if busy and busy > clock.t and pol.max_parallel_per_rack <= 1:
            waited = [r["id"] for r in REM.runs.values()
                      if r["id"] != rid and r["target"]["rack_id"] == t["rack_id"]
                      and r["completed_at"] and datetime.fromisoformat(r["completed_at"]) > clock.t]
            clock.t = busy
            run["steps"].append({"name": "queued", "state": "Succeeded", "at": _iso(busy),
                                 "waited_for": waited,
                                 "detail": f"max_parallel_per_rack={pol.max_parallel_per_rack}"})
        st = ev_rec["healthEventStatus"] if ev_rec else _status()
        host, gpu = t["machine_id"], (ev.get("entitiesImpacted") or [{}])[0].get("entityValue", "0")
        a = pol.actions
        if a.cordon:
            try:
                from .lifecycle import get_adapter
                get_adapter().cordon(host, f"nvsentinel {rid}: {reason}")
            except Exception:                      # noqa: BLE001 — record, keep going
                pass
            s = _step(run, clock, "quarantine", nodeQuarantined="Quarantined")
            st.update(nodeQuarantined="Quarantined", quarantineFinishTimestamp=s["at"])
        if a.drain:
            s = _step(run, clock, "drain", userPodsEvictionStatus="Succeeded")
            st.update(userPodsEvictionStatus={"status": "Succeeded", "message": ""},
                      drainFinishTimestamp=s["at"],
                      drainDecision={"ruleSet": "default", "method": "Immediate",
                                     "stopped": False, "scope": {"entityType": "Node",
                                                                 "entityValue": host},
                                     "reusedFrom": ""})
        recovered = False
        cr_kind = ACTION_CR.get(ev["recommendedAction"])
        if a.gpu_reset and cr_kind:
            result, via = _gpu_reset(run, host, cr_kind)
            s = _step(run, clock, "gpu_reset", result, cr_kind=cr_kind, via=via)
            run["remediation_cr"] = _cr(cr_kind, host, gpu, result, s["at"])
            st.update(remediationDecision={"ruleSet": "default", "action": ev["recommendedAction"],
                                           "stopped": False},
                      lastRemediationTimestamp=s["at"])
            ok, via = _healthy(host)
            _step(run, clock, "validate", "Succeeded" if ok else "Failed", via=via)
            recovered = ok
        if recovered:
            _uncordon(host)
            _step(run, clock, "uncordon", nodeQuarantined="UnQuarantined")
            st.update(nodeQuarantined="UnQuarantined", faultRemediated=True)
            run["outcome"] = "recovered"
        elif a.replace:
            try:
                run["replacement"] = _replace(run, t)
                _step(run, clock, "replace", **{k: v for k, v in run["replacement"].items()
                                                if k in ("old_host", "new_host", "kind")})
                run["outcome"] = "replaced"
                st.update(faultRemediated=True)
            except HTTPException as e:
                _step(run, clock, "replace", "Failed", detail=str(e.detail))
                run["outcome"] = "failed"
                st.update(faultRemediated=False)
        else:
            run["outcome"] = "failed"
            st.update(faultRemediated=False)
        restored = clock.t
        run["mttr_min"] = round((restored - detected).total_seconds() / 60, 1)
        run["kpi_met"] = run["outcome"] != "failed" and run["mttr_min"] <= pol.kpi_replace_minutes
        if not recovered and a.rma:
            _rma(run, clock, t)
        run["state"] = "completed"
        run["completed_at"] = _iso(clock.t)
        if t["rack_id"]:
            REM.rack_busy_until[t["rack_id"]] = max(clock.t, REM.rack_busy_until.get(
                t["rack_id"], clock.t))
        if t["tenant_id"]:
            from . import sla
            sla.record_downtime(tenant_id=t["tenant_id"], node_id=host,
                                instance_id=t["instance_id"], rack_id=t["rack_id"],
                                start=_iso(detected),
                                end=_iso(restored if run["outcome"] != "failed" else clock.t),
                                cause=(f"xid:{(ev.get('errorCode') or ['?'])[0]}"
                                       if run["trigger"]["kind"] == "xid"
                                       else f"{run['trigger']['kind']}:{(ev.get('errorCode') or ['?'])[0]}"),
                                ref=rid)
        _publish("RemediationCompleted", run, outcome=run["outcome"], mttr_min=run["mttr_min"])
        return run


def _uncordon(host: str) -> None:
    try:
        from .lifecycle import get_adapter
        get_adapter().get_host(host).cordoned = False   # in-process FakeNico object
    except Exception:                                  # noqa: BLE001
        pass
    dsx.nico_send("PATCH", dsx.nico_rest_path("machine_one", id=host),
                  json={"maintenanceMessage": None})


def start_run(machine_id: str, reason: str = "operator",
              trigger: Optional[dict] = None, health_event: Optional[dict] = None) -> dict:
    """Explicit start (operator, F22 leak response) — runs whatever the mode.
    A caller-built NVSentinel HealthEvent (F22 LeakDetection, recommendedAction
    REPLACE_VM) is used as-is, so no GPU reset is tried for a non-GPU fault."""
    ev_rec = None
    if health_event is not None:
        ev_rec = {"createdAt": _iso(_utcnow()), "healthEvent": health_event,
                  "healthEventStatus": _status()}
        with REM.lock:
            REM.health_events.append(ev_rec)
    return _start(machine_id, reason, trigger or {"kind": "manual"},
                  health_event, ev_rec)


def inject(body: InjectBody) -> dict:
    t = _target(body.machine_id)
    host = t["machine_id"]
    with REM.lock:
        REM.faults[host] = {"xid": body.xid, "gpu": body.gpu, "persistent": body.persistent,
                            "via": "inject"}           # bus echo of this fault is ignored
    st_ai, _ = dsx.ai_send("POST", "/emulator/v1/oob/inject",
                           json={"kind": "sel_critical", "target": t["tray_id"] or host})
    st_n, _ = dsx.nico_send("POST", dsx.nico_rest_path("machine_gpu_reset", id=host)
                            .replace("/gpu/reset", "/gpu/fault"),
                            json={"xid": body.xid, "gpu": body.gpu,
                                  "persistent": body.persistent})
    ev = health_event(host, body.xid, body.gpu)
    out = ingest(ev)
    return {"injected": {"machine_id": host, "xid": body.xid, "gpu": body.gpu,
                         "persistent": body.persistent,
                         "ai_infra_sel": "ok" if st_ai and st_ai < 400 else "stub",
                         "nico_fault": "ok" if st_n and st_n < 400 else "stub",
                         "xid_source": ("nico gpu/fault → ai-infra gpu-faults twin"
                                        if st_n and st_n < 400 else "nocp-local fault table")},
            **out}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@router.get("/policy")
def get_policy() -> dict:
    return REM.policy.model_dump()


@router.put("/policy")
def put_policy(body: Policy) -> dict:
    if body.mode not in ("monitor", "auto"):
        raise HTTPException(422, "mode must be monitor | auto")
    if body.max_parallel_per_rack < 1:
        raise HTTPException(422, "max_parallel_per_rack must be ≥ 1")
    REM.policy = body
    return body.model_dump()


@router.get("/runs")
def list_runs(tenant_id: Optional[str] = None,
              state: Optional[str] = Query(None, description="running | completed")) -> list:
    out = [r for r in REM.runs.values()
           if (not tenant_id or r["target"]["tenant_id"] == tenant_id)
           and (not state or r["state"] == state)]
    return sorted(out, key=lambda r: r["id"], reverse=True)


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    r = REM.runs.get(run_id)
    if r is None:
        raise HTTPException(404, f"run '{run_id}' not found")
    return r


@router.post("/runs", status_code=201)
def create_run(body: RunCreate) -> dict:
    return start_run(body.machine_id, body.reason, {"kind": "manual"})


@router.get("/health-events")
def list_health_events(limit: int = Query(100, le=1000)) -> list:
    return REM.health_events[-limit:][::-1]


@router.post("/health-events", status_code=202)
def post_health_events(body: dict) -> dict:
    """PlatformConnector.HealthEventOccurredV1 — JSON of `HealthEvents`."""
    evs = body.get("events")
    if not isinstance(evs, list) or not evs:
        raise HTTPException(422, "HealthEvents.events must be a non-empty list")
    out = []
    for e in evs:
        if not isinstance(e, dict) or not e.get("nodeName"):
            raise HTTPException(422, "HealthEvent.nodeName required")
        ev = {**health_event(e["nodeName"], int((e.get("errorCode") or [79])[0])), **e}
        ev.setdefault("id", str(uuid.uuid4()))
        res = ingest(ev)
        out.append({"id": ev["id"], "run_id": (res["run"] or {}).get("id"),
                    "processingStrategy": ev["processingStrategy"]})
    return {"accepted": len(out), "events": out}


@router.post("/inject", status_code=201)
def inject_route(body: InjectBody) -> dict:
    return inject(body)


@router.post("/reset")
def reset_route() -> dict:
    reset()
    return {"reset": True}


DEMO_TENANT = "demo-recovery-co"


@router.post("/demo/setup")
def demo_setup() -> dict:
    """Deterministic demo state: clear F10/F09 records, reuse (or create) the
    demo BMaaS tenant with one delivered vr-nvl72 rack, policy → auto."""
    from . import sla
    from .lifecycle import create_order
    from .models import IsolationTier, OrderCreate, OrderKind, TenantCreate
    from .ncp import RMA
    from .tenancy import create_tenant
    reset()
    sla.reset()
    t = next((x for x in STORE.tenants.values() if x.name == DEMO_TENANT), None)
    if t is None:
        t = create_tenant(TenantCreate(name=DEMO_TENANT,
                                       isolation_tier=IsolationTier.bare_metal_dedicated))
    for cid in [k for k, c in STORE.sla_credits.items() if c.get("tenant_id") == t.id]:
        STORE.sla_credits.pop(cid)
    for k in [k for k, x in STORE.tickets.items()
              if x.tenant_id == t.id and x.type == "sla_violation"]:
        STORE.tickets.pop(k)
    for k in [k for k, i in STORE.incidents.items()
              if i.get("tenant_id") == t.id and str(i.get("key", "")).startswith("synthetic|")]:
        STORE.incidents.pop(k)
    with RMA.lock:
        for k in [k for k, c in RMA.cases.items() if c.get("tenant") == t.id]:
            RMA.cases.pop(k)
    trays = sorted(x.id for x in STORE.trays.values() if x.tenant_id == t.id)
    if not trays:
        create_order(OrderCreate(tenant_id=t.id, kind=OrderKind.new,
                                 blueprint_key="vr-nvl72", racks=1))
        trays = sorted(x.id for x in STORE.trays.values() if x.tenant_id == t.id)
    REM.policy = Policy(mode="auto")
    return {"tenant_id": t.id, "trays": trays[:3], "policy": REM.policy.model_dump()}


# ---------------------------------------------------------------------------
# bus subscription (integration/v2): GPU XID alerts that reach the NOCP bus —
# Redfish EventService NvidiaGPU XidError via app.oob.ingest, or the AI-Infra
# Exchange health/GpuXidError fault source (F10) — become NVSentinel
# HealthEvents, as gpu-health-monitor would raise them from the same DCGM/SEL
# signal. Policy still decides (monitor = STORE_ONLY).
# ---------------------------------------------------------------------------
_XID_RE = re.compile(r"\bxid\s*[:#]?\s*(\d+)", re.I)


def _on_event(ev: dict) -> None:
    if ev.get("topic") != "health" or str(ev.get("source", "")).startswith("nocp."):
        return
    d = ev.get("data") or {}
    raw = d.get("raw") if isinstance(d.get("raw"), dict) else {}
    kind = str(d.get("type") or d.get("event") or d.get("message_id") or "")
    text = f"{d.get('message') or ''} {kind}"
    if "xid" not in text.lower() and d.get("xid") is None:
        return
    if str(d.get("severity") or "Critical") not in ("Critical", "critical", "Fatal", "fatal"):
        return
    m = _XID_RE.search(text)
    xid = int(d.get("xid") or (m.group(1) if m else 79))
    host = d.get("machine") or d.get("machine_id")
    if not host:
        return
    host = host if str(host).startswith("nh-") else f"nh-{host}"
    key = raw.get("EventId") or d.get("fault_id") or d.get("id") or ev.get("id")
    oem = ((raw.get("Oem") or {}).get("Nvidia") or {}) if isinstance(raw.get("Oem"), dict) else {}
    recovers = d.get("reset_recovers", oem.get("ResetRecovers"))
    with REM.lock:
        if (key, host) in REM.seen_events:
            return
        REM.seen_events.add((key, host))
        # the same fault already came in as a HealthEvent (operator inject →
        # NICo → AI-Infra echoes it on the bus): one run per fault, not two
        if REM.faults.get(host, {}).get("via") == "inject":
            return
        # the bus alert says nothing about recoverability unless the source
        # tells (AI-Infra fault source reset_recovers); otherwise the fault is
        # taken as persistent and the chain proves it by reset → re-validate
        REM.faults.setdefault(host, {"xid": xid, "gpu": int(d.get("gpu") or 0),
                                     "persistent": not bool(recovers)})
    try:
        ingest(health_event(host, xid, int(d.get("gpu") or 0),
                            d.get("message") or f"XID {xid} ({kind or 'bus'})"))
    except Exception:                              # noqa: BLE001 — unknown host etc.
        pass


events.add_listener(_on_event)
