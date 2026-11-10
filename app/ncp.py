"""NCP (NeoCloud OS) essentials on the DSX baseline.

  - /api/v1/metering/usage?tenant=    GPU-hour, instance-hour, K8s node-hour
  - /api/v1/isolation/{tenant}        isolation matrix: VPC/VRF, IB P_Key, NVLink,
                                      Spectrum-X, NSG (+ cross-tenant uniqueness)
  - /api/v1/rma                       RMA workflow: health_event → cordon_drain →
                                      isolate → replace → burn_in → pool_return
  - /api/v1/firmware/baseline|drift   site firmware baseline and per-machine drift
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import dsx, events
from .models import HwState
from .store import STORE

router = APIRouter(prefix="/api/v1", tags=["ncp"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _hours_since(ts: str) -> float:
    try:
        start = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - start).total_seconds() / 3600)


# ---------------------------------------------------------------------------
# Metering
# ---------------------------------------------------------------------------
@router.get("/metering/usage")
def metering_usage(tenant: Optional[str] = None,
                   tenant_id: Optional[str] = None) -> dict:
    """Usage lines per tenant and product. BMaaS from delivered orders (rack-hours
    → instance-hours = trays, GPU-hours = GPUs); Managed K8s from live NVKE
    nodes and legacy NKD workers (node-hours)."""
    from . import business, nvke, spec
    t = tenant_id or tenant                      # guard forces tenant_id
    lines = []
    for b in business.billing_usage(t)["lines"]:
        bp = spec.get_blueprint(b["blueprint_key"]) if b.get("blueprint_key") else None
        trays = bp.gpu_per_rack // 4 if bp else 18
        gpus = bp.gpu_per_rack if bp else 72
        for metric, mult, unit in (("instance_hour", trays, "instance-h"),
                                   ("gpu_hour", gpus, "GPU-h")):
            lines.append({"tenant_id": b["tenant_id"], "product": "bmaas",
                          "source": b["order_id"], "metric": metric,
                          "quantity": round(b["rack_hours"] * mult, 4), "unit": unit,
                          "active": b["active"]})
    for n in nvke.node_hours(t):
        h = _hours_since(n["since"])
        tray = STORE.trays.get(n["host"].removeprefix("nh-"))
        gpus = len(tray.gpu_ids) if tray else 0
        lines.append({"tenant_id": n["tenant_id"], "product": "managed-k8s",
                      "source": n["cluster_id"], "metric": "k8s_node_hour",
                      "quantity": round(h, 4), "unit": "node-h", "active": True})
        if gpus:
            lines.append({"tenant_id": n["tenant_id"], "product": "managed-k8s",
                          "source": n["cluster_id"], "metric": "gpu_hour",
                          "quantity": round(h * gpus, 4), "unit": "GPU-h", "active": True})
    for c in STORE.k8s_clusters.values():
        if c.state == "deleted" or (t and c.tenant_id != t):
            continue
        h = _hours_since(c.created_at)
        lines.append({"tenant_id": c.tenant_id, "product": "managed-k8s",
                      "source": c.id, "metric": "k8s_node_hour",
                      "quantity": round(h * (len(c.worker_node_ids) + len(c.cp_node_ids)), 4),
                      "unit": "node-h", "active": c.state == "running"})

    def tot(metric):
        return round(sum(x["quantity"] for x in lines if x["metric"] == metric), 4)
    return {"tenant": t, "period": _period(), "gpu_hours": tot("gpu_hour"),
            "instance_hours": tot("instance_hour"), "k8s_node_hours": tot("k8s_node_hour"),
            "lines": lines, "generated_at": _now()}


# ---------------------------------------------------------------------------
# Isolation matrix
# ---------------------------------------------------------------------------
def _dupes(values: dict) -> set:
    seen, dup = {}, set()
    for tid, v in values.items():
        if v is None:
            continue
        if v in seen and seen[v] != tid:
            dup.update({tid, seen[v]})
        seen[v] = tid
    return dup


@router.get("/isolation/{tenant}")
def isolation(tenant: str) -> dict:
    t = STORE.tenants.get(tenant)
    if t is None:
        raise HTTPException(404, f"tenant '{tenant}' not found")
    ni = STORE.netiso.get(tenant)
    parts = [p for p in STORE.partitions.values() if p.tenant_id == tenant]
    racks = [r for r in STORE.racks.values() if r.tenant_id == tenant]
    vrf_dup = _dupes({k: v.vrf for k, v in STORE.netiso.items()})
    pkey_dup = _dupes({k: v.ib_pkey for k, v in STORE.netiso.items()})
    vni_dup = _dupes({k: v.compute_l3vni for k, v in STORE.netiso.items()})

    def status(present: bool, dup: bool = False) -> str:
        return "violation" if dup else ("isolated" if present else "not-provisioned")

    layers = [
        {"layer": "VPC / VRF", "value": f"{ni.vrf} (vpc_{ni.compute_l3vni})" if ni else "",
         "status": status(bool(ni), tenant in vrf_dup or tenant in vni_dup),
         "detail": "DPU HBN L3 EVPN (FNN) — NICo VPC"},
        {"layer": "InfiniBand P_Key", "value": hex(ni.ib_pkey) if ni and ni.ib_pkey else "",
         "status": status(bool(ni and ni.ib_pkey), tenant in pkey_dup),
         "detail": "UFM partition — NICo InfiniBandPartition"},
        {"layer": "NVLink Partition",
         "value": ",".join(f"{p.rack_id}#{p.partition_id}" for p in parts)
                  or ",".join(r.id for r in racks),
         "status": status(bool(parts or racks)),
         "detail": "NVLinkLogicalPartition per NVL72 domain"
                   + (" (whole rack)" if racks and not parts else "")},
        {"layer": "Spectrum-X", "value": f"sx-{tenant}" if ni else "",
         "status": status(bool(ni)),
         "detail": f"SpectrumXPartition — converged VNI {ni.converged_vni}" if ni
                   else "SpectrumXPartition"},
        {"layer": "NetworkSecurityGroup", "value": f"nsg-{tenant}-default" if ni else "",
         "status": status(bool(ni)), "detail": "deny-all ingress + tenant bastion:22"},
    ]
    # Cross-check against NICo REST objects when the emulator is up.
    nico = {}
    for key in ("vpc", "ib_partition", "nvlink_partition", "spectrumx_partition", "nsg"):
        body = dsx.nico_get(dsx.nico_rest_path(key), params={"tenantId": tenant}, timeout=1.5)
        if body is not None:
            from .adapters import _items
            nico[key] = len(_items(body))
    ok = all(x["status"] != "violation" for x in layers)
    return {"tenant": tenant, "isolation_tier": t.isolation_tier.value,
            "verified_at": _now(), "ok": ok, "layers": layers,
            "nico_objects": nico or None,
            "source": "nico" if nico else "nocp"}


# ---------------------------------------------------------------------------
# RMA workflow
# ---------------------------------------------------------------------------
RMA_STAGES = ["health_event", "cordon_drain", "isolate", "replace", "burn_in",
              "pool_return"]


class RmaCreate(BaseModel):
    machine: str                      # NICo host id (nh-…) or tray id
    component: str = "GPU"
    reason: str = "health event"
    rack: Optional[str] = None
    tenant: Optional[str] = None
    source: str = "operator"          # operator | nvsentinel | oob-event


class _RmaState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cases: dict[str, dict] = {}
        self.seq = 0

    def reset(self) -> None:
        with self.lock:
            self.cases.clear()
            self.seq = 0


RMA = _RmaState()


def _tray_of(machine: str):
    return STORE.trays.get(machine) or STORE.trays.get(machine.removeprefix("nh-"))


def _host_id(machine: str) -> str:
    return machine if machine.startswith("nh-") else f"nh-{machine}"


def _nvke_node(host_id: str):
    from .nvke import NVKE
    for p in NVKE.nodepools.values():
        for n in p.nodes:
            if n.nico_host_id == host_id and n.state != "removed":
                return p.cluster_id, n
    return None


def _adapter():
    from .lifecycle import get_adapter
    return get_adapter()


def _run_stage(case: dict, stage: str) -> dict:
    """Side effects of entering `stage`; returns the step record."""
    host_id = case["nico_host_id"]
    tray = _tray_of(case["machine"])
    step = {"stage": stage, "at": _now(), "status": "succeeded"}
    adapter = _adapter()
    if stage == "cordon_drain":
        try:
            adapter.cordon(host_id, f"rma {case['id']}: {case['reason']}")
        except Exception as e:                     # noqa: BLE001 — record, keep going
            step["warning"] = f"nico cordon: {getattr(e, 'detail', e)}"
        hit = _nvke_node(host_id)
        if hit:                                    # tenant migration (NVKE)
            from .nvke import replace_node
            rec = replace_node(hit[0], hit[1].name, f"rma {case['id']}", source="rma")
            step["migration"] = {"cluster": hit[0], "new_host": rec["new_host"]}
        if tray is not None:
            tray.state = HwState.draining
    elif stage == "isolate":
        if tray is not None:
            tray.state = HwState.maintenance
        step["detail"] = "tray isolated from tenant fabric (NICo tray quarantine)"
    elif stage == "replace":
        case["replacement_serial"] = f"SN-{case['id'].upper()}-R"
        step["detail"] = f"{case['component']} replaced → {case['replacement_serial']}"
    elif stage == "burn_in":
        body = dsx.nico_get(dsx.nico_rest_path("machine_validation", id=host_id), timeout=1.5)
        step["validation"] = body if body is not None else {
            "suite": "health-validation toolkit/v0.3.2 (dcgm diag L3, nccl allreduce)",
            "result": "pass", "source": "local-fallback"}
    elif stage == "pool_return":
        try:
            host = adapter.get_host(host_id)
            host.cordoned = False                  # in-process FakeNico object
        except Exception:                          # noqa: BLE001
            pass
        dsx.nico_send("PATCH", dsx.nico_rest_path("machine_one", id=host_id),
                      json={"maintenanceMessage": None})
        try:                                       # released host → sanitize → pool_ready
            if adapter.get_host(host_id).state.value == "released":
                from .adapters import wait_job
                job = wait_job(adapter, adapter.sanitize(host_id))
                step["sanitize"] = job.state
        except Exception as e:                     # noqa: BLE001 — record, keep going
            step["warning"] = f"nico sanitize: {getattr(e, 'detail', e)}"
        if tray is not None:
            tray.state = HwState.allocated if tray.tenant_id else HwState.ready
        step["detail"] = "returned to pool"
    return step


def _publish(case: dict) -> None:
    events.publish("rma", {"rma": case["id"], "machine": case["machine"],
                           "stage": case["stage"]}, source="nocp.rma")


@router.get("/rma")
def list_rma(state: Optional[str] = Query(None, description="open | closed")) -> dict:
    cases = sorted(RMA.cases.values(), key=lambda c: c["id"], reverse=True)
    if state == "open":
        cases = [c for c in cases if c["stage"] != "pool_return"]
    elif state == "closed":
        cases = [c for c in cases if c["stage"] == "pool_return"]
    return {"stages": RMA_STAGES, "cases": cases}


def open_case(body: RmaCreate) -> dict:
    tray = _tray_of(body.machine)
    if tray is None and not body.machine.startswith("nh-"):
        raise HTTPException(404, f"machine '{body.machine}' not found")
    with RMA.lock:
        RMA.seq += 1
        cid = f"rma-{RMA.seq:04d}"
        case = {"id": cid, "machine": body.machine, "nico_host_id": _host_id(body.machine),
                "rack": body.rack or (tray.rack_id if tray else None),
                "tenant": body.tenant or (tray.tenant_id if tray else None),
                "component": body.component, "reason": body.reason,
                "source": body.source, "stage": RMA_STAGES[0], "stage_index": 0,
                "opened_at": _now(), "closed_at": None,
                "steps": [{"stage": RMA_STAGES[0], "at": _now(), "status": "succeeded",
                           "detail": body.reason}]}
        RMA.cases[cid] = case
    _publish(case)
    return case


@router.post("/rma", status_code=201)
def create_rma(body: RmaCreate) -> dict:
    return open_case(body)


@router.get("/rma/{rma_id}")
def get_rma(rma_id: str) -> dict:
    case = RMA.cases.get(rma_id)
    if case is None:
        raise HTTPException(404, f"rma '{rma_id}' not found")
    return case


@router.post("/rma/{rma_id}/advance")
def advance_rma(rma_id: str, to: Optional[str] = None) -> dict:
    """Advance one stage (or run through to `to`)."""
    with RMA.lock:
        case = get_rma(rma_id)
        if to is not None and to not in RMA_STAGES:
            raise HTTPException(422, f"to must be one of {RMA_STAGES}")
        if case["stage"] == RMA_STAGES[-1]:
            raise HTTPException(409, f"rma '{rma_id}' already closed")
        target = RMA_STAGES.index(to) if to else case["stage_index"] + 1
        if target <= case["stage_index"]:
            raise HTTPException(409, f"rma '{rma_id}' is already at {case['stage']}")
        while case["stage_index"] < target:
            nxt = RMA_STAGES[case["stage_index"] + 1]
            case["steps"].append(_run_stage(case, nxt))
            case["stage_index"] += 1
            case["stage"] = nxt
            _publish(case)
        if case["stage"] == RMA_STAGES[-1]:
            case["closed_at"] = _now()
        return case


# ---------------------------------------------------------------------------
# Firmware baseline / drift
# ---------------------------------------------------------------------------
FIRMWARE_BASELINE = [
    {"component": "BMC (OpenBMC)", "baseline": "2026.04.2"},
    {"component": "HMC", "baseline": "1.8.0"},
    {"component": "GPU VBIOS (Rubin)", "baseline": "97.10.2C.00.11"},
    {"component": "ConnectX-8 FW", "baseline": "40.44.1036"},
    {"component": "BlueField-4 (DOCA)", "baseline": "3.1.0"},
    {"component": "NVSwitch Tray FW", "baseline": "2.5.1"},
    {"component": "Power Shelf (Dell PSU)", "baseline": "1.12.4"},
]


@router.get("/firmware/baseline")
def firmware_baseline() -> dict:
    body = dsx.nico_get(dsx.nico_rest_path("firmware_baseline"), timeout=1.5)
    if body is not None:
        from .adapters import _items
        items = _items(body) or (body.get("components") if isinstance(body, dict) else None)
        if not items and isinstance(body, dict) and isinstance(body.get("baseline"), dict):
            # NICo ext: {"baseline": {trayType: {component: version}}}
            items = [{"component": f"{tt} {comp}", "tray_type": tt, "baseline": ver}
                     for tt, comps in body["baseline"].items() if isinstance(comps, dict)
                     for comp, ver in comps.items()]
        if items:
            return {"source": "nico", "components": items}
    return {"source": "nocp", "site_policy": "drift → auto-update on idle hosts (OOB)",
            "components": FIRMWARE_BASELINE}


@router.get("/firmware/drift")
def firmware_drift() -> dict:
    body = dsx.nico_get(dsx.nico_rest_path("firmware_drift"), timeout=1.5)
    if body is not None:
        from .adapters import _items
        rows = _items(body) or (body.get("drift") if isinstance(body, dict) else None) or []
        if not rows and isinstance(body, dict) and isinstance(body.get("trays"), list):
            # NICo ext: {"total", "trays": [{trayId, type, rackId, diffs: [{component,
            # expected, actual}]}]} → one row per drifted component
            rows = [{"machine": t.get("trayId"), "rack": t.get("rackId"),
                     "tray_type": t.get("type"),
                     "component": f"{t.get('type')} {d.get('component')}",
                     "current": d.get("actual"), "baseline": d.get("expected"),
                     "severity": "warning",
                     "remediation": "PATCH /tray/{id}/firmware"}
                    for t in body["trays"] if isinstance(t, dict)
                    for d in t.get("diffs") or [] if isinstance(d, dict)]
        return {"source": "nico", "drift": rows}
    adapter = _adapter()
    try:
        hosts = adapter.list_hosts()
    except Exception:                              # noqa: BLE001
        hosts = []
    drift = []
    for h in hosts:
        if h.firmware_ok:
            continue
        tray = _tray_of(h.tray_id or h.host_id)
        drift.append({"machine": h.host_id, "rack": tray.rack_id if tray else None,
                      "component": "BMC (OpenBMC)", "current": "unknown",
                      "baseline": FIRMWARE_BASELINE[0]["baseline"],
                      "severity": "warning",
                      "remediation": "idle" if h.state.value == "pool_ready" else "deferred"})
    return {"source": "nocp", "drift": drift, "checked_hosts": len(hosts)}
