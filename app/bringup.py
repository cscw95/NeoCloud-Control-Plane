"""F23 rack bring-up: DCIM (NetBox stand-in) + 15-step bring-up orchestrator.

  POST /api/v1/dcim/racks                  {serial, site, row, position, blueprint_key, cable_plan:[{a, b}]}
  GET  /api/v1/dcim/racks                  ?state=  (planned→installed→bringup→validating→sellable|failed)
  GET  /api/v1/dcim/racks/{id}
  PATCH /api/v1/dcim/racks/{id}            {cable_plan} — fix the plan before a retry
  GET  /api/v1/dcim/sellable               sellable inventory (racks / trays / GPUs)
  POST /api/v1/bringup/racks/{id}:start    run the 15 steps (deck B1), stop at the first failure
  GET  /api/v1/bringup/racks/{id}          → {state, steps[15]}

Steps call AI-Infra (Config Manager, RMS) and NICo REST (POST /rack, machine
attestation, validation) through `dsx`. An unreachable upstream does not fail a
step: it runs against NOCP's own record and is tagged `source: local-fallback`.
A cable-plan mismatch reported by LLDP fails step 04 and the rack goes `failed`.

  GET  /api/v1/dcim/cables                 NetBox-style cable list (?rack=)
  POST /api/v1/bringup/inject              demo fault injection (operator): miswire |
                                           attestation_fail | clear
  POST /api/v1/bringup/demo/reset          remove the demo rack, clear its faults

Upstream message shapes and flow:
  - NetBox 4.x REST API (netbox-community/netbox, Apache-2.0) dcim/racks and
    dcim/cables serializers: rack {id, url, display, name, facility_id, site{slug},
    status{value,label}, serial}; cable {id, display, a_terminations[], b_terminations[],
    status{value,label}, label}; lists {count, next, previous, results}. NetBox is
    not in ref/upstream yet — field names follow the public NetBox REST docs.
  - NICo infra-controller rest-api/openapi/spec.yaml: step 05 is
    POST /expected-rack (ExpectedRackCreateRequest, labels chassis.* / location.*)
    then POST /rack/{id}/bringup (BringUpRackRequest → {taskIds}); falls back to the
    v2 contract POST /rack on an older NICo emulator.
  - nv-rms fern/proto/rack_manager.proto: SetPowerState{operation} and
    UpdateFirmware{firmware_targets[{target, filename}]} field names.
  - nv-config-manager ztp.openapi.json: ZTP stages validate_serial → boot-script →
    config → firmware → provisioned (run by AI-Infra per switch).
  Every upstream request/response of a run is kept in `steps[].calls` (clipped).
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import dsx, events, spec

router = APIRouter(prefix="/api/v1", tags=["dcim-bringup"])

STATES = ("planned", "installed", "bringup", "validating", "sellable", "failed")
# (no, key, name, owner) — DSX deck B1 (SHEET 06–07)
STEPS = [
    (1, "dcim_register", "Rack serial · location · cable plan registered (DCIM)", "nocp"),
    (2, "dcim_event", "DCIM event published (Exchange bus)", "nocp"),
    (3, "switch_ztp", "Switch ZTP: DHCP → config render → NVUE validate", "ai-infra"),
    (4, "cable_validation", "LLDP cable validation vs cable plan", "ai-infra"),
    (5, "nico_register", "NICo REST rack registration", "nico"),
    (6, "redfish_discovery", "Redfish discovery · SKU check vs blueprint", "ai-infra"),
    (7, "rms_power_on", "RMS rack power on", "ai-infra"),
    (8, "tray_power_inventory", "Tray · power shelf power and inventory", "ai-infra"),
    (9, "firmware_bundle", "Firmware bundle (BMC → BF → GPU)", "ai-infra"),
    (10, "nvlink_partition", "NVLink domain · default partition", "nico"),
    (11, "attestation", "DPU pairing · measured boot · TPM attestation", "nico"),
    (12, "cluster_readiness", "Cluster readiness node check", "nocp"),
    (13, "health_validation", "Health validation (DCGM · NVLink · IB · NCCL)", "nico"),
    (14, "report", "Pass / fail report", "nocp"),
    (15, "sellable", "Sellable → inventory pool", "nocp"),
]
VALIDATING_FROM = 11          # steps 11–15 run with the rack in `validating`


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Dcim:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.racks: dict[str, dict] = {}

    def reset(self) -> None:
        with self.lock:
            self.racks.clear()


DCIM = _Dcim()


class Cable(BaseModel):
    a: str = Field(min_length=1, max_length=128)
    b: str = Field(min_length=1, max_length=128)


class DcimRackIn(BaseModel):
    serial: str = Field(min_length=1, max_length=64)
    site: str = Field(min_length=1, max_length=64)
    row: str = Field(min_length=1, max_length=32)
    position: str = Field(min_length=1, max_length=32)
    blueprint_key: str = spec.DEFAULT_BLUEPRINT
    cable_plan: list[Cable] = Field(default_factory=list, max_length=2048)


class DcimRackPatch(BaseModel):
    cable_plan: list[Cable] = Field(max_length=2048)


def _slug(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", v.lower()).strip("-")


def _set_state(r: dict, state: str, detail: str = "") -> None:
    if r["state"] != state:
        r["state"] = state
        r["history"].append({"state": state, "at": _now(), "detail": detail or state})
        events.publish("dcim", {"kind": "rack_state", "rack": r["id"], "state": state,
                                "detail": detail})
    r["updated_at"] = _now()


def _rack(rack_id: str) -> dict:
    r = DCIM.racks.get(rack_id)
    if r is None:
        raise HTTPException(404, f"dcim rack {rack_id} not found")
    return r


# NetBox Rack.status choices: reserved | available | planned | active | deprecated
_NETBOX_STATUS = {"planned": "planned", "installed": "planned", "bringup": "planned",
                  "validating": "planned", "failed": "planned", "sellable": "active"}


def _view(r: dict) -> dict:
    v = {k: x for k, x in r.items() if k != "steps"}
    st = _NETBOX_STATUS[r["state"]]
    v.update(name=r["id"], display=r["id"], url=f"/api/v1/dcim/racks/{r['id']}",
             facility_id=f"{r['row']}-{r['position']}",
             site={"slug": r["site"], "display": r["site"]},
             status={"value": st, "label": st.title()})
    return v


@router.post("/dcim/racks", status_code=201)
def create_rack(body: DcimRackIn) -> dict:
    try:
        bp = spec.get_blueprint(body.blueprint_key)
    except (KeyError, ValueError, HTTPException):
        raise HTTPException(400, f"unknown blueprint {body.blueprint_key}")
    with DCIM.lock:
        if any(r["serial"] == body.serial for r in DCIM.racks.values()):
            raise HTTPException(409, f"rack serial {body.serial} already registered")
        if any((r["site"], r["row"], r["position"]) == (body.site, body.row, body.position)
               for r in DCIM.racks.values()):
            raise HTTPException(409, f"position {body.site}/{body.row}/{body.position} taken")
        rid = f"bu-{_slug(body.serial)}"
        r = {"id": rid, "serial": body.serial, "site": body.site, "row": body.row,
             "position": body.position, "blueprint_key": bp.key, "model": bp.model,
             "cable_plan": [c.model_dump() for c in body.cable_plan],
             "state": "planned", "history": [], "nico_rack_id": None, "machines": [],
             "gpus": bp.compute_trays * bp.gpu_per_tray, "source": None,
             "created_at": _now(), "updated_at": _now(), "steps": _fresh_steps()}
        r["history"].append({"state": "planned", "at": _now(), "detail": "registered"})
        DCIM.racks[rid] = r
    events.publish("dcim", {"kind": "rack_created", "rack": rid, "serial": body.serial,
                            "site": body.site})
    return _view(r)


@router.get("/dcim/racks")
def list_racks(state: Optional[str] = Query(None)) -> dict:
    if state and state not in STATES:
        raise HTTPException(400, f"state must be one of {list(STATES)}")
    with DCIM.lock:
        items = [_view(r) for r in DCIM.racks.values() if not state or r["state"] == state]
    return {"count": len(items), "next": None, "previous": None, "results": items,
            "racks": items, "states": list(STATES)}


@router.get("/dcim/racks/{rack_id}")
def get_rack(rack_id: str) -> dict:
    return _view(_rack(rack_id))


@router.patch("/dcim/racks/{rack_id}")
def patch_rack(rack_id: str, body: DcimRackPatch) -> dict:
    with DCIM.lock:
        r = _rack(rack_id)
        if r["state"] in ("bringup", "validating", "sellable"):
            raise HTTPException(409, f"rack is {r['state']}")
        r["cable_plan"] = [c.model_dump() for c in body.cable_plan]
        r["updated_at"] = _now()
        return _view(r)


@router.get("/dcim/sellable")
def sellable() -> dict:
    with DCIM.lock:
        racks = [r for r in DCIM.racks.values() if r["state"] == "sellable"]
        return {"racks": [{"id": r["id"], "site": r["site"], "serial": r["serial"],
                           "blueprint_key": r["blueprint_key"], "gpus": r["gpus"],
                           "trays": len(r["machines"]), "nico_rack_id": r["nico_rack_id"],
                           "sellable_at": r["updated_at"]} for r in racks],
                "rack_count": len(racks), "tray_count": sum(len(r["machines"]) for r in racks),
                "gpu_count": sum(r["gpus"] for r in racks)}


@router.get("/dcim/cables")
def cables(rack: Optional[str] = Query(None)) -> dict:
    """NetBox-style cables from the DCIM cable plans; status `connected` once LLDP
    validated the link (step 04 passed), `planned` before, mismatched links flagged."""
    out = []
    with DCIM.lock:
        for r in DCIM.racks.values():
            if rack and r["id"] != rack:
                continue
            validated = any(s["key"] == "cable_validation" and s["status"] == "passed"
                            for s in r["steps"])
            bad = {m["a"]: m for m in r.get("cable_mismatches") or []}
            for i, c in enumerate(r["cable_plan"], 1):
                def term(end: str) -> list:
                    dev, _, port = end.partition(":")
                    return [{"object_type": "dcim.interface",
                             "object": {"device": {"name": dev}, "name": port or end}}]
                st = "connected" if validated else "planned"
                out.append({"id": len(out) + 1, "display": f"#{r['id']}-{i}",
                            "label": f"{r['id']}-{i}", "rack": r["id"],
                            "a_terminations": term(c["a"]), "b_terminations": term(c["b"]),
                            "status": {"value": st, "label": st.title()},
                            "lldp_mismatch": bad.get(c["a"])})
    return {"count": len(out), "next": None, "previous": None, "results": out}


class InjectIn(BaseModel):
    kind: str = Field(pattern="^(miswire|attestation_fail|clear)$")
    rack: Optional[str] = None
    a: Optional[str] = None
    actual_b: Optional[str] = None
    machine: Optional[str] = None


@router.post("/bringup/inject")
def inject(body: InjectIn) -> dict:
    """Demo fault injection through the emulators' hooks (operator only)."""
    _CALLS.clear()
    if body.kind == "miswire":
        r = _rack(body.rack or "")
        a = body.a or (r["cable_plan"][0]["a"] if r["cable_plan"] else None)
        if not a:
            raise HTTPException(400, "rack has no cable plan")
        _ai("POST", "/config-manager/v1/lldp-faults",
            {"rack": r["id"], "a": a, "actual_b": body.actual_b or f"{r['id']}-leaf-sw-0:swp99"})
    elif body.kind == "attestation_fail":
        if not body.machine:
            raise HTTPException(400, "machine is required")
        _logged("PUT", f"{dsx.nico_url()}/emulator/v1/rest/machine/{body.machine}/attestation",
                json={"measuredBoot": "Failed"})
    else:
        if body.rack:
            _ai("DELETE", f"/config-manager/v1/lldp-faults?rack={body.rack}")
        if body.machine:
            _logged("PUT", f"{dsx.nico_url()}/emulator/v1/rest/machine/{body.machine}/attestation",
                    json={})
    calls = list(_CALLS)
    ok = bool(calls) and all(c["status"] is not None and c["status"] < 400 for c in calls)
    return {"kind": body.kind, "applied": ok,
            "source": "upstream" if ok else "local-fallback", "calls": calls}


DEMO_SERIAL = "SN-DEMO-0001"


@router.post("/bringup/demo/reset")
def demo_reset() -> dict:
    """Deterministic demo start: drop the demo rack record and clear its LLDP faults."""
    rid = f"bu-{_slug(DEMO_SERIAL)}"
    with DCIM.lock:
        removed = DCIM.racks.pop(rid, None) is not None
    dsx.call("DELETE", dsx.ai_infra_url() + f"/config-manager/v1/lldp-faults?rack={rid}")
    return {"rack": rid, "removed": removed, "serial": DEMO_SERIAL}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def _fresh_steps() -> list[dict]:
    return [{"no": n, "key": k, "name": name, "owner": o, "status": "pending",
             "detail": None, "source": None, "started_at": None, "finished_at": None,
             "calls": []}
            for n, k, name, o in STEPS]


class StepFailed(Exception):
    pass


_CALLS: list = []          # upstream calls of the step being run (raw request/response)


def _clip(v: Any, n: int = 600) -> Any:
    import json as _j
    try:
        txt = _j.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        txt = str(v)
    return v if len(txt) <= n else txt[:n] + "…"


def _logged(method: str, url: str, json: Any = None, params: Optional[dict] = None):
    st, body = dsx.call(method, url, json=json, params=params, timeout=5.0)
    _CALLS.append({"method": method, "url": url, "params": params, "request": _clip(json),
                   "status": st, "response": _clip(body)})
    return st, body


def _ai(method: str, path: str, json: Any = None):
    return _logged(method, dsx.ai_infra_url() + path, json=json)


def _nico(method: str, path: str, json: Any = None):
    """NICo REST call; `path` is relative to /v2/org/{org}/nico."""
    org = dsx.os.environ.get("NOCP_NICO_ORG", "neocloud")
    url = f"{dsx.nico_url()}/v2/org/{org}/nico{path}"
    if method == "GET":
        return _logged("GET", url, params=json)
    return _logged(method, url, json=json)


def _upstream(st, body, what: str):
    """(status, body) → body | None (unreachable). Raise StepFailed on HTTP errors."""
    if st is None:
        return None
    if st >= 400:
        msg = body.get("detail") if isinstance(body, dict) else body
        raise StepFailed(f"{what}: HTTP {st} {msg}")
    return body


def _machines(r: dict) -> list[str]:
    st, body = _nico("GET", "/machine", json={"rackId": r["nico_rack_id"] or r["id"],
                                             "pageSize": 100})
    ms = _upstream(st, body, "NICo machines")
    if ms is None:
        return r["machines"] or [f"nh-{r['id']}-tray-{i:02d}" for i in range(18)]
    return [m["id"] for m in ms]


def _step(r: dict, key: str) -> tuple[str, Optional[str]]:
    """Run one step → (detail, source). source None = upstream answered."""
    rid = r["id"]
    bp = spec.get_blueprint(r["blueprint_key"])
    if key == "dcim_register":
        if not r["cable_plan"]:
            raise StepFailed("cable plan is empty")
        return f"{r['serial']} @ {r['site']}/{r['row']}/{r['position']}, " \
               f"{len(r['cable_plan'])} cables", "nocp"
    if key == "dcim_event":
        ev = events.publish("dcim", {"kind": "bringup_requested", "rack": rid,
                                     "serial": r["serial"], "cable_plan": len(r["cable_plan"])})
        return f"event #{ev['id']} on topic dcim", "nocp"
    if key == "switch_ztp":
        sw = _upstream(*_ai("GET", f"/config-manager/v1/switches?rack={rid}"), "switches")
        if sw is None:
            return "Config Manager unreachable", "local-fallback"
        done = []
        for s in [s for s in sw.get("switches", []) if s.get("source") == "bringup"]:
            z = _upstream(*_ai("POST", f"/config-manager/v1/switches/{s['id']}/ztp"), "ZTP")
            if z and z.get("state") != "configured":
                raise StepFailed(f"ZTP {s['id']} {z.get('state')}")
            done.append(s["id"])
        if not done:
            raise StepFailed("no switches discovered for the rack")
        return f"{len(done)} switches configured", None
    if key == "cable_validation":
        res = _upstream(*_ai("POST", "/config-manager/v1/lldp-validate",
                             {"rack": rid, "cable_plan": r["cable_plan"]}), "LLDP")
        if res is None:
            return "LLDP unreachable — cable plan not verified", "local-fallback"
        r["cable_mismatches"] = res.get("mismatches") or []
        if r["cable_mismatches"]:
            m = r["cable_mismatches"][0]
            raise StepFailed(f"{len(r['cable_mismatches'])} cable mismatch(es): "
                             f"{m['a']} expected {m['expected_b']} got {m['actual_b']}")
        return f"{res.get('matched')}/{res.get('checked')} links match", None
    if key == "nico_register":
        labels = {"chassis.manufacturer": "Dell", "chassis.serial-number": r["serial"],
                  "chassis.model": bp.model, "location.datacenter": r["site"],
                  "location.room": r["row"], "location.position": r["position"]}
        st, body = _nico("POST", "/expected-rack", {"siteId": r["site"], "rackId": rid,
                                                    "name": rid, "labels": labels})
        if st == 404 or st == 405:                     # NICo without expected-rack
            st, body = _nico("POST", "/rack", {"name": rid, "siteId": r["site"],
                                               "serial": r["serial"],
                                               "location": {"row": r["row"],
                                                            "position": r["position"]}})
        r["nico_rack_id"] = rid
        if st == 409:
            detail = "already registered"
        else:
            res = _upstream(st, body, "NICo expected-rack")
            if res is None:
                return "NICo unreachable", "local-fallback"
            detail = f"expected-rack {res.get('rackId') or res.get('id')}"
        bst, bbody = _nico("POST", f"/rack/{rid}/bringup", {"siteId": r["site"],
                                                            "description": "DCIM bring-up"})
        if bst is not None and bst < 400 and isinstance(bbody, dict):
            r["nico_task_ids"] = bbody.get("taskIds") or []
            detail += f", bring-up task {len(r['nico_task_ids'])}"
        return detail, None
    if key == "redfish_discovery":
        inv = _upstream(*_ai("GET", f"/rms/v1/racks/{rid}/inventory"), "RMS inventory")
        if inv is None:
            return "RMS unreachable", "local-fallback"
        trays = inv.get("compute_trays") or []
        found = {"compute_trays": len(trays),
                 "nvlink_switch_trays": inv.get("nvlink_switch_trays"),
                 "power_shelves": inv.get("power_shelves"),
                 "gpu_per_tray": min((t.get("gpus", 0) for t in trays), default=0)}
        want = {"compute_trays": bp.compute_trays, "nvlink_switch_trays": bp.nvlink_switch_trays,
                "power_shelves": bp.power_shelves, "gpu_per_tray": bp.gpu_per_tray}
        bad = {k: (want[k], found[k]) for k in want if found[k] != want[k]}
        if bad:
            raise StepFailed("SKU mismatch vs blueprint " + ", ".join(
                f"{k} expected {e} got {g}" for k, (e, g) in bad.items()))
        return f"{bp.key}: {found['compute_trays']} trays × {found['gpu_per_tray']} GPU", None
    if key == "rms_power_on":
        res = _upstream(*_ai("POST", f"/rms/v1/racks/{rid}/power",
                                 {"rack_id": rid, "node_id": rid,
                                  "operation": "POWER_OPERATION_ON"}), "RMS power")
        return ("rack powered on", None) if res is not None else ("RMS unreachable",
                                                                  "local-fallback")
    if key == "tray_power_inventory":
        inv = _upstream(*_ai("GET", f"/rms/v1/racks/{rid}/inventory"), "RMS inventory")
        if inv is None:
            return "RMS unreachable", "local-fallback"
        off = [t["id"] for t in inv.get("compute_trays") or [] if t.get("power") != "On"]
        if off:
            raise StepFailed(f"{len(off)} trays not powered on")
        return f"{len(inv['compute_trays'])} trays On, {inv.get('power_shelves')} shelves", None
    if key == "firmware_bundle":
        from .firmware_bundles import get_bundle
        b = get_bundle(None)
        res = _upstream(*_ai("POST", f"/rms/v1/racks/{rid}/firmware",
                             {"rack_id": rid, "version": b["version"], "activate": True,
                              "firmware_targets": [{"target": c, "filename": b["components"][c]}
                                                   for c in b["apply_order"]]}),
                        "RMS firmware")
        if res is None:
            return f"bundle {b['version']} (RMS unreachable)", "local-fallback"
        return f"bundle {b['version']} order {' > '.join(res.get('order') or [])}", None
    if key == "nvlink_partition":
        st, body = _nico("GET", f"/domain/nvlink/nvl-{rid}")
        dom = _upstream(st, body, "NICo NVLink domain")
        if dom is None:
            return "default NVL72 partition (NICo unreachable)", "local-fallback"
        return f"{dom.get('id')} {dom.get('topology')} default partition", None
    if key == "attestation":
        r["machines"] = _machines(r)
        bad, fallback = [], False
        for mid in r["machines"]:
            st, body = _nico("GET", f"/machine/{mid}/attestation")
            a = _upstream(st, body, f"attestation {mid}")
            if a is None:
                fallback = True
                continue
            if not a.get("attested"):
                bad.append(f"{mid} measuredBoot={a.get('measuredBoot')} "
                           f"dpu={a.get('dpu', {}).get('attested')}")
        if bad:
            raise StepFailed(f"{len(bad)} machine(s) failed attestation: {bad[0]}")
        return f"{len(r['machines'])} machines attested", "local-fallback" if fallback else None
    if key == "cluster_readiness":
        if len(r["machines"]) != bp.compute_trays:
            raise StepFailed(f"{len(r['machines'])} machines, blueprint needs {bp.compute_trays}")
        return f"{len(r['machines'])} nodes ready for validation", "nocp"
    if key == "health_validation":
        failed, fallback = [], False
        for mid in r["machines"]:
            st, body = _nico("POST", f"/machine/{mid}/validation/run", {})
            run = _upstream(st, body, f"validation {mid}")
            if run is None:
                fallback = True
                continue
            if (run.get("status") or {}).get("state") != "Success":
                failed.append(mid)
        if failed:
            raise StepFailed(f"{len(failed)} machine(s) failed validation: {failed[0]}")
        return f"{len(r['machines'])} machines passed", "local-fallback" if fallback else None
    if key == "report":
        events.publish("dcim", {"kind": "bringup_report", "rack": rid, "result": "pass"})
        return "all checks passed", "nocp"
    if key == "sellable":
        _set_state(r, "sellable", "in sellable inventory")
        return f"{r['gpus']} GPUs added to the sellable pool", "nocp"
    raise StepFailed(f"unknown step {key}")


@router.post("/bringup/racks/{rack_id}:start")
def start(rack_id: str) -> dict:
    with DCIM.lock:
        r = _rack(rack_id)
        if r["state"] in ("bringup", "validating"):
            raise HTTPException(409, f"bring-up already running ({r['state']})")
        if r["state"] == "sellable":
            raise HTTPException(409, "rack is already sellable")
        r["steps"], r["cable_mismatches"], r["failed_step"] = _fresh_steps(), [], None
        r["source"] = None
        r["runs"] = r.get("runs", 0) + 1
        if r["state"] == "planned":
            _set_state(r, "installed", "racked and cabled")
        _set_state(r, "bringup", f"run {r['runs']} started")
        fallback = False
        for s in r["steps"]:
            if s["no"] == VALIDATING_FROM:
                _set_state(r, "validating", "health validation")
            s["status"], s["started_at"] = "running", _now()
            _CALLS.clear()
            try:
                s["detail"], s["source"] = _step(r, s["key"])
                s["status"] = "passed"
            except StepFailed as exc:
                s["status"], s["detail"] = "failed", str(exc)
                r["failed_step"] = s["no"]
            s["finished_at"] = _now()
            s["calls"] = list(_CALLS)
            if s["source"] == "local-fallback":
                fallback = True
            if s["status"] == "failed":
                for rest in r["steps"][s["no"]:]:
                    rest["status"] = "blocked"
                _set_state(r, "failed", f"step {s['no']:02d} {s['key']}: {s['detail']}")
                events.publish("dcim", {"kind": "bringup_report", "rack": rack_id,
                                        "result": "fail", "step": s["no"],
                                        "detail": s["detail"]})
                break
        r["source"] = "local-fallback" if fallback else "upstream"
        return status(rack_id)


@router.get("/bringup/racks/{rack_id}")
def status(rack_id: str) -> dict:
    r = _rack(rack_id)
    passed = sum(s["status"] == "passed" for s in r["steps"])
    return {"rack": rack_id, "state": r["state"], "failed_step": r.get("failed_step"),
            "progress": {"passed": passed, "total": len(STEPS)},
            "cable_mismatches": r.get("cable_mismatches") or [],
            "source": r.get("source"), "steps": r["steps"]}
