"""OOB monitoring (DSX Fleet Health) + AI-Infra proxy for the consoles.

  - /api/v1/oob/summary            per-rack BMC reachability, PSU, leak, inlet, SEL, power
  - /api/v1/oob/racks/{id}         rack detail: trays, power shelves, Redfish Power/Thermal,
                                   NICo rack health report
  - /api/v1/oob/events             POST = Redfish EventService webhook (AI-Infra pushes
                                   here), GET = buffered events
  - /api/v1/oob/subscribe          register NOCP with the AI-Infra EventService
  - /api/v1/oob/sel/{machine}      Redfish SEL entries
  - /api/v1/oob/machines/{m}/actions/{ac-power-cycle|gpu-reset}
                                   NICo machine action, Redfish fallback
  - /api/v1/infra/{path}           allow-listed proxy to AI-Infra (obs/UFM/NetQ/VAST/…)
                                   so the consoles never call :9100 directly

Upstream first; without AI-Infra the OOB views come from the topology plus
whatever events were received (`source: "local-fallback"`).
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
import httpx
from fastapi.responses import JSONResponse, Response

from . import dsx, events
from .store import STORE

router = APIRouter(prefix="/api/v1", tags=["oob"])

PSU_SHELVES_PER_RACK = 4
PSU_PER_SHELF = 6
INLET_BASE_C = 25.0
LEAK_ORDER = {"OK": 0, "Warning": 1, "Critical": 2}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _OobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: deque = deque(maxlen=500)
        self.leak: dict[str, str] = {}          # rack -> OK|Warning|Critical
        self.sel: dict[str, list] = {}          # machine -> SEL-like entries
        self.subscription: Optional[dict] = None
        self.seq = 0

    def reset(self) -> None:
        with self.lock:
            self.events.clear()
            self.leak.clear()
            self.sel.clear()
            self.subscription = None
            self.seq = 0


OOB = _OobState()


# ---------------------------------------------------------------------------
# Local model
# ---------------------------------------------------------------------------
def _site_of_rack(rack) -> str:
    f = STORE.factory_of_su(rack.su_id)
    return f.id if f else ""


def _rack_local(rack) -> dict:
    trays = STORE.trays_of_rack(rack.id)
    bad = [t for t in trays if t.state.value not in ("ready", "allocated")]
    bmc_total = len(trays) + len(rack.nvlink_switch_tray_ids)
    util = 0.86 if rack.tenant_id else 0.30
    sel_crit = sum(1 for t in trays for e in OOB.sel.get(t.id, [])
                   if e.get("Severity") == "Critical")
    return {"rack": rack.id, "site": _site_of_rack(rack), "model": rack.model,
            "tenant_id": rack.tenant_id,
            "bmc_reachable": bmc_total - len(bad), "bmc_total": bmc_total,
            "psu_ok": PSU_SHELVES_PER_RACK * PSU_PER_SHELF,
            "psu_total": PSU_SHELVES_PER_RACK * PSU_PER_SHELF,
            "leak": OOB.leak.get(rack.id, "OK"),
            "max_inlet_c": round(INLET_BASE_C + 8 * util, 1),
            "sel_critical": sel_crit,
            "power_kw": round(rack.power_cap_kw * util, 1),
            "cap_kw": rack.power_cap_kw}


def _local_summary(site: Optional[str]) -> list[dict]:
    rows = [_rack_local(r) for r in STORE.racks.values()]
    return [r for r in rows if not site or r["site"] == site]


def _overlay(row: dict) -> dict:
    """Events received by the webhook win over a stale upstream snapshot."""
    leak = OOB.leak.get(row.get("rack"))
    if leak and LEAK_ORDER.get(leak, 0) > LEAK_ORDER.get(row.get("leak", "OK"), 0):
        row = {**row, "leak": leak}
    return row


# ---------------------------------------------------------------------------
# Summary / rack detail / SEL
# ---------------------------------------------------------------------------
@router.get("/oob/summary")
def summary(site: Optional[str] = None) -> dict:
    body = dsx.ai_get("/emulator/v1/oob/summary", params={"site": site} if site else None)
    if body is not None:
        rows = body if isinstance(body, list) else (body.get("racks") or [])
        # AI-Infra keys rows by `rack_id`; NOCP views and the consoles use `rack`.
        rows = [{**r, "rack": r.get("rack") or r.get("rack_id")} for r in rows
                if isinstance(r, dict)]
        src = "ai-infra"
    else:
        rows, src = _local_summary(site), "local-fallback"
    rows = [_overlay(r) for r in rows]
    totals = {
        "racks": len(rows),
        "bmc_unreachable": sum(r.get("bmc_total", 0) - r.get("bmc_reachable", 0) for r in rows),
        "psu_failed": sum(r.get("psu_total", 0) - r.get("psu_ok", 0) for r in rows),
        "leak_warning": sum(1 for r in rows if r.get("leak") == "Warning"),
        "leak_critical": sum(1 for r in rows if r.get("leak") == "Critical"),
        "sel_critical": sum(r.get("sel_critical", 0) for r in rows),
        "power_kw": round(sum(r.get("power_kw", 0) for r in rows), 1),
        "cap_kw": round(sum(r.get("cap_kw", 0) for r in rows), 1),
    }
    return {"source": src, "site": site, "racks": rows, "totals": totals, "at": _now()}


@router.get("/oob/racks/{rack_id}")
def rack_detail(rack_id: str) -> dict:
    rack = STORE.racks.get(rack_id)
    power = dsx.ai_get(f"/redfish/v1/Chassis/{rack_id}/PowerSubsystem")
    thermal = dsx.ai_get(f"/redfish/v1/Chassis/{rack_id}/ThermalSubsystem")
    health = dsx.nico_get(dsx.nico_rest_path("rack_health", id=rack_id))
    upstream = next((r for r in (summary().get("racks") or []) if r.get("rack") == rack_id), None)
    if rack is None and upstream is None and power is None:
        raise HTTPException(404, f"rack '{rack_id}' not found")
    row = upstream or (_rack_local(rack) if rack else {"rack": rack_id})
    trays = []
    if rack is not None:
        n = max(len(rack.tray_ids), 1)
        for t in STORE.trays_of_rack(rack_id):
            sel = OOB.sel.get(t.id, [])
            trays.append({
                "machine": t.id, "nico_host_id": f"nh-{t.id}", "tray": t.index,
                "kind": "compute", "state": t.state.value,
                "bmc_reachable": t.state.value in ("ready", "allocated"),
                "inlet_c": row.get("max_inlet_c"),
                "power_w": round((row.get("power_kw") or 0) * 1000 / n),
                "gpus": len(t.gpu_ids),
                "sel_critical": sum(1 for e in sel if e.get("Severity") == "Critical"),
                "leak": row.get("leak", "OK")})
    shelves = [{"shelf": i + 1, "psu_ok": PSU_PER_SHELF, "psu_total": PSU_PER_SHELF,
                "vendor": "Dell"} for i in range(PSU_SHELVES_PER_RACK)]
    return {"source": "ai-infra" if (power or thermal) else "local-fallback",
            **row, "trays": trays, "power_shelves": shelves,
            "redfish": {"power_subsystem": power, "thermal_subsystem": thermal},
            "nico_health_report": health,
            "events": [e for e in OOB.events if e.get("rack") == rack_id][-20:]}


@router.get("/oob/sel/{machine}")
def sel(machine: str) -> dict:
    body = dsx.ai_get(f"/redfish/v1/Systems/{machine}/LogServices/SEL/Entries")
    if isinstance(body, dict):
        return {"source": "ai-infra", "machine": machine,
                "entries": body.get("Members") or body.get("entries") or []}
    return {"source": "local-fallback", "machine": machine,
            "entries": list(OOB.sel.get(machine, []))}


# ---------------------------------------------------------------------------
# EventService webhook + subscription
# ---------------------------------------------------------------------------
_TOPIC_BY_TYPE = {"leak": "leak", "thermal": "thermal", "psu": "power",
                  "power": "power", "sel": "health", "health": "health",
                  "grid": "grid"}


def _classify(ev: dict) -> str:
    hint = " ".join(str(ev.get(k, "")) for k in
                    ("topic", "EventType", "MessageId", "Message")).lower()
    for key, topic in _TOPIC_BY_TYPE.items():
        if key in hint:
            return topic
    return "health"


def _rack_of(origin: str) -> Optional[str]:
    origin = origin or ""
    for part in origin.strip("/").split("/"):
        if part in STORE.racks:
            return part
        t = STORE.trays.get(part) or STORE.trays.get(part.removeprefix("nh-"))
        if t is not None:
            return t.rack_id
    return None


def _machine_of(origin: str) -> Optional[str]:
    """Tray (machine) id inside a Redfish origin such as
    /redfish/v1/Chassis/{tray}/ThermalSubsystem/LeakDetection."""
    for part in (origin or "").strip("/").split("/"):
        if part in STORE.trays or part.removeprefix("nh-") in STORE.trays:
            return part
    return None


def ingest(raw: dict) -> list[dict]:
    """Normalize Redfish Event (`{"Events":[...]}`) or a bare event dict."""
    batch = raw.get("Events") if isinstance(raw.get("Events"), list) else [raw]
    out = []
    for e in batch:
        if not isinstance(e, dict):
            continue
        origin = e.get("OriginOfCondition")
        if isinstance(origin, dict):
            origin = origin.get("@odata.id", "")
        origin = origin or e.get("rack") or e.get("machine") or ""
        topic = _classify(e)
        sev = e.get("Severity") or e.get("severity") or "OK"
        rack = e.get("rack") or _rack_of(origin)
        machine = e.get("machine") or _machine_of(origin)
        with OOB.lock:
            OOB.seq += 1
            rec = {"id": f"oob-{OOB.seq}", "topic": topic, "severity": sev,
                   "message": e.get("Message") or e.get("message") or "",
                   "message_id": e.get("MessageId"), "origin": origin,
                   "rack": rack, "machine": machine, "received_at": _now(),
                   "raw": e}
            OOB.events.append(rec)
            if topic == "leak" and rack:
                OOB.leak[rack] = sev if sev in LEAK_ORDER else "Warning"
            if topic == "health" and machine:
                OOB.sel.setdefault(machine, []).append({
                    "Id": str(len(OOB.sel[machine]) + 1), "Severity": sev,
                    "Message": rec["message"], "Created": rec["received_at"]})
        events.publish(topic, rec, source="ai-infra.eventservice")
        out.append(rec)
    return out


def _csv(env: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(env, default).split(",") if x.strip()]


def webhook_source_allowed(request: Request) -> bool:
    """EventService receiver gate: shared secret when NOCP_WEBHOOK_SECRET is set,
    else the client address must be in NOCP_WEBHOOK_SOURCES (IPs/CIDRs)."""
    secret = os.environ.get("NOCP_WEBHOOK_SECRET")
    if secret:
        got = request.headers.get("x-webhook-token", "")
        return hmac.compare_digest(got.encode(), secret.encode())
    host = request.client.host if request.client else ""
    for item in _csv("NOCP_WEBHOOK_SOURCES", "127.0.0.1,::1,testclient"):
        if host == item:
            return True
        try:
            if ipaddress.ip_address(host) in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


def destination_allowed(dest: str) -> bool:
    """EventService subscription destinations must match NOCP_WEBHOOK_DESTINATIONS."""
    try:
        u = httpx.URL(dest)
    except Exception:                              # noqa: BLE001
        return False
    if u.scheme not in ("http", "https") or u.userinfo:
        return False
    norm = f"{u.scheme}://{u.host}{':' + str(u.port) if u.port else ''}{u.path}"
    return any(norm == a or norm.startswith(a.rstrip("/") + "/") for a in
               _csv("NOCP_WEBHOOK_DESTINATIONS",
                    "http://127.0.0.1:8000/api/v1/oob/events,"
                    "http://localhost:8000/api/v1/oob/events"))


@router.post("/oob/events", status_code=202)
async def receive_events(request: Request) -> dict:
    """Redfish EventService destination (AI-Infra POSTs here)."""
    if not webhook_source_allowed(request):
        raise HTTPException(403, "event source not allowed")
    try:
        raw = await request.json()
    except Exception:                              # noqa: BLE001
        raise HTTPException(400, "event body must be JSON")
    if not isinstance(raw, dict):
        raise HTTPException(400, "event body must be an object")
    recs = ingest(raw)
    return {"accepted": len(recs), "ids": [r["id"] for r in recs]}


@router.get("/oob/events")
def list_events(limit: int = Query(50, ge=1, le=500), topic: Optional[str] = None,
                rack: Optional[str] = None) -> dict:
    rows = [e for e in OOB.events if (not topic or e["topic"] == topic)
            and (not rack or e["rack"] == rack)]
    rows = list(reversed(rows))[:limit]
    return {"events": rows, "subscription": OOB.subscription}


def _find_subscription(dest: str) -> Optional[str]:
    """Id of an AI-Infra EventService subscription already pointing at dest.
    Extra subscriptions to the same destination (left by earlier NOCP runs)
    are deleted so each event is delivered once."""
    coll = dsx.ai_get("/redfish/v1/EventService/Subscriptions", timeout=2.0)
    found = []
    for m in (coll or {}).get("Members", []) if isinstance(coll, dict) else []:
        path = str(m.get("@odata.id", "")).removeprefix(dsx.ai_infra_url())
        sub = dsx.ai_get(path, timeout=2.0)
        if isinstance(sub, dict) and sub.get("Destination") == dest:
            found.append((sub.get("Id"), path))
    for _, path in found[1:]:
        dsx.ai_send("DELETE", path, timeout=2.0)
    return found[0][0] if found else None


def subscribe(destination: Optional[str] = None) -> dict:
    dest = destination or (os.environ.get("NOCP_PUBLIC_URL", "http://127.0.0.1:8000")
                           .rstrip("/") + "/api/v1/oob/events")
    if not destination_allowed(dest):
        rec = {"destination": dest, "status": None, "subscribed": False,
               "error": "destination not in NOCP_WEBHOOK_DESTINATIONS", "at": _now()}
        OOB.subscription = rec
        return rec
    existing = _find_subscription(dest)
    if existing:                     # idempotent: restarts must not stack duplicates
        rec = {"destination": dest, "status": 200, "subscribed": True,
               "id": existing, "reused": True, "at": _now()}
        OOB.subscription = rec
        return rec
    st, body = dsx.ai_send("POST", "/redfish/v1/EventService/Subscriptions",
                           json={"Destination": dest, "Protocol": "Redfish",
                                 "EventTypes": ["Alert", "StatusChange"],
                                 "Context": "nocp"}, timeout=2.0)
    rec = {"destination": dest, "status": st, "subscribed": st is not None and st < 400,
           "id": (body or {}).get("Id") if isinstance(body, dict) else None, "at": _now()}
    OOB.subscription = rec
    return rec


@router.post("/oob/subscribe")
def subscribe_route(body: Optional[dict] = None) -> dict:
    rec = subscribe((body or {}).get("destination"))
    if rec.get("error"):
        return JSONResponse(rec, status_code=422)
    if not rec["subscribed"]:
        return JSONResponse(rec, status_code=502)
    return rec


# ---------------------------------------------------------------------------
# Machine OOB actions (NICo v2.3: AC power cycle, AdminGpuReset)
# ---------------------------------------------------------------------------
_ACTIONS = {
    "ac-power-cycle": {
        "nico": ("PATCH", "machine_power", {"action": "ACPowercycle",
                                            "acknowledgeAttachedInstance": True}),
        "redfish": ("POST", "/redfish/v1/Chassis/{m}/Actions/Chassis.Reset",
                    {"ResetType": "PowerCycle"}),
    },
    "gpu-reset": {
        "nico": ("POST", "machine_gpu_reset", {}),
        "redfish": ("POST", "/redfish/v1/Systems/{m}/Actions/GpuReset", {}),
    },
}


@router.post("/oob/machines/{machine}/actions/{action}", status_code=202)
def machine_action(machine: str, action: str) -> dict:
    spec = _ACTIONS.get(action)
    if spec is None:
        raise HTTPException(422, f"action must be one of {sorted(_ACTIONS)}")
    m, key, body = spec["nico"]
    st, resp = dsx.nico_send(m, dsx.nico_rest_path(key, id=machine), json=body)
    via = "nico"
    if st is None or st in (404, 405, 501):
        m, path, body = spec["redfish"]
        st, resp = dsx.ai_send(m, path.format(m=machine.removeprefix("nh-")), json=body)
        via = "ai-infra-redfish"
    if st is None:
        via = "local-fallback"
    elif st >= 400:
        raise HTTPException(st, resp.get("detail") if isinstance(resp, dict) else str(resp))
    # NICo answers 202 even when its AI-Infra Redfish call failed
    # ({"delegated": false, "detail": ...}) — surface that instead of "accepted".
    if via == "nico" and isinstance(resp, dict) and resp.get("delegated") is False:
        raise HTTPException(409, f"NICo accepted {action} but the BMC refused it: "
                                 f"{resp.get('detail', '')}")
    rec = {"machine": machine, "action": action, "accepted": True, "via": via,
           "status": st, "at": _now()}
    events.publish("health", {"event": "oob-action", **rec}, source="nocp.oob")
    return rec


# ---------------------------------------------------------------------------
# /api/v1/infra/* — AI-Infra proxy (consoles never call :9100 directly)
# ---------------------------------------------------------------------------
INFRA_PREFIXES = ("emulator/v1/", "ufm/v1/", "netq/v1/", "vast/v1/", "converged/v1/",
                  "redfish/v1/", "dps/v1/", "exchange/v1/topics")


def _safe_infra_path(request: Request, path: str) -> str:
    """Reject traversal / absolute URLs / encoded tricks, then allow-list (SSRF)."""
    raw = request.scope.get("raw_path", b"").decode("latin-1")
    low = (path + " " + raw).lower()
    if (".." in path.split("/") or "\\" in path or "://" in path or "//" in path
            or path.startswith("/") or "@" in path
            or any(t in low for t in ("%2e", "%2f", "%5c", "%00", "%40"))
            or not path or any(ord(c) < 0x21 for c in path)):
        raise HTTPException(400, "invalid infra path")
    if not path.startswith(INFRA_PREFIXES) or path.startswith("emulator/v1/reset"):
        raise HTTPException(403, f"infra path '{path}' is not proxied")
    base = httpx.URL(dsx.ai_infra_url())
    url = httpx.URL(f"{dsx.ai_infra_url()}/{path}")
    if (url.scheme, url.host, url.port) != (base.scheme, base.host, base.port):
        raise HTTPException(400, "invalid infra path")
    return str(url)


@router.api_route("/infra/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
async def infra_proxy(path: str, request: Request):
    target = _safe_infra_path(request, path)
    body = None
    if request.method != "GET":
        raw = await request.body()
        if raw:
            try:
                body = await request.json()
            except Exception:                      # noqa: BLE001
                raise HTTPException(400, "proxied body must be JSON")
    st, resp = dsx.call(request.method, target, json=body,
                        params=dict(request.query_params), timeout=8.0)
    if st is None:
        return JSONResponse({"detail": "ai-infra unreachable", "path": path},
                            status_code=502)
    if isinstance(resp, (dict, list)):
        return JSONResponse(resp, status_code=st)
    return Response(content=str(resp), status_code=st, media_type="text/plain")


# ---------------------------------------------------------------------------
# /api/v1/machines — NICo REST Machine view for the operator console
# ---------------------------------------------------------------------------
def _local_machine(h) -> dict:
    tray = STORE.trays.get(h.tray_id)
    return {"id": h.host_id, "trayId": h.tray_id, "rackId": tray.rack_id if tray else None,
            "siteId": h.site, "instanceTypeId": h.sku,
            "status": {"pool_ready": "Ready", "discovered": "Initializing",
                       "sanitizing": "Reset", "quarantined": "Error",
                       "rma": "Maintenance"}.get(h.state.value, "InUse"),
            "lifecycleState": h.state.value, "tenantId": h.tenant_ref,
            "instanceId": h.instance_id,
            "maintenanceMessage": "cordoned" if h.cordoned else None,
            "firmwareOk": h.firmware_ok, "attested": h.attested}


def _adapter():
    from .lifecycle import get_adapter
    return get_adapter()


NICO_PAGE = 100                                    # NICo REST spec page size cap


@router.get("/machines")
def list_machines(rack: Optional[str] = None, status: Optional[str] = None,
                  tenant_id: Optional[str] = None,
                  limit: int = Query(200, ge=1, le=1000),
                  offset: int = Query(0, ge=0)) -> dict:
    """Machines page [offset, offset+limit). NICo is read page by page (pageSize 100);
    `next_offset` is set while more rows remain, so clients page instead of asking
    for more than `limit` max (1000)."""
    params = {k: v for k, v in (("rackId", rack), ("status", status),
                                ("tenantId", tenant_id)) if v}
    path = dsx.nico_rest_path("machine")
    first = offset // NICO_PAGE + 1
    body = dsx.nico_get(path, params={**params, "pageSize": NICO_PAGE, "pageNumber": first})
    if body is not None:
        from .adapters import _items
        rows, page, chunk = [], first, _items(body)
        while True:
            rows += chunk
            if len(chunk) < NICO_PAGE or len(rows) >= (offset - (first - 1) * NICO_PAGE) + limit:
                break
            page += 1
            chunk = _items(dsx.nico_get(path, params={**params, "pageSize": NICO_PAGE,
                                                      "pageNumber": page}) or [])
            if not chunk:
                break
        skip = offset - (first - 1) * NICO_PAGE
        out = rows[skip:skip + limit]
        more = len(rows) > skip + limit or len(chunk) == NICO_PAGE
        return {"source": "nico", "machines": out, "offset": offset, "limit": limit,
                "next_offset": offset + len(out) if more and out else None}
    rows = [_local_machine(h) for h in _adapter().list_hosts()]
    rows = [r for r in rows if (not rack or r["rackId"] == rack)
            and (not status or r["status"].lower() == status.lower())
            and (not tenant_id or r["tenantId"] == tenant_id)]
    out = rows[offset:offset + limit]
    return {"source": "local-fallback", "machines": out, "offset": offset, "limit": limit,
            "total": len(rows),
            "next_offset": offset + limit if offset + limit < len(rows) else None}


def _machine_or_404(machine_id: str) -> dict:
    body = dsx.nico_get(dsx.nico_rest_path("machine_one", id=machine_id))
    if isinstance(body, dict):
        return {"source": "nico", **body}
    try:
        return {"source": "local-fallback", **_local_machine(_adapter().get_host(machine_id))}
    except HTTPException:
        raise HTTPException(404, f"machine '{machine_id}' not found")


@router.get("/machines/{machine_id}")
def get_machine(machine_id: str) -> dict:
    return _machine_or_404(machine_id)


@router.get("/machines/{machine_id}/hardware")
def machine_hardware(machine_id: str) -> dict:
    m = _machine_or_404(machine_id)
    tray = STORE.trays.get(m.get("trayId") or machine_id.removeprefix("nh-"))
    gpus = [STORE.gpus[g] for g in (tray.gpu_ids if tray else []) if g in STORE.gpus]
    cpus = [STORE.cpus[c] for c in (tray.cpu_ids if tray else []) if c in STORE.cpus]
    dpu = STORE.dpus.get(tray.dpu_id) if tray and tray.dpu_id else None
    return {"machine": machine_id, "source": m["source"],
            "capabilities": m.get("machineCapabilities"),
            "health": dsx.nico_get(dsx.nico_rest_path("machine_health", id=machine_id),
                                   timeout=1.5),
            "gpus": [g.model_dump() for g in gpus],
            "cpus": [c.model_dump() for c in cpus],
            "dpu": dpu.model_dump() if dpu else None,
            "connectx_supernics": tray.connectx_supernics if tray else None}


@router.get("/machines/{machine_id}/sanitize-report")
def machine_sanitize_report(machine_id: str) -> dict:
    try:
        rep = _adapter().get_sanitize_report(machine_id)
    except HTTPException as e:
        raise HTTPException(e.status_code if e.status_code < 500 else 404,
                            f"no sanitize report for '{machine_id}'")
    return rep.model_dump()
