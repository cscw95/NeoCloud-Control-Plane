"""F24 firmware bundles (nvfwupd-style) and rollout plans.

  GET|POST /api/v1/firmware/bundles          {version, components:{bmc, gpu, nvswitch, cx, bf}}
  GET|POST /api/v1/firmware/plans            {rack, bundle, window} — idle hosts only
  GET      /api/v1/firmware/plans/{id}
  POST     /api/v1/firmware/plans/{id}:execute   AI-Infra Redfish SimpleUpdate {bundle}

Upstream: DMTF Redfish UpdateService.v1_17_1 SimpleUpdate (ImageURI, Targets;
the bundle rides in Oem.Nvidia.Bundle — no DMTF bundle parameter) and NVIDIA
nvfwupd package flow (one .fwpkg per bundle). Local copy neocloud/ref/upstream/redfish.

Apply order is BMC → BF → GPU, then CX and NVSwitch. The BMC is the Dell
iDRAC10 and keeps the single fleet number `ncp.IDRAC10_FW` (1.4.2): a bundle
with any other bmc version is refused.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import dsx, events
from .models import HwState
from .ncp import IDRAC10_FW
from .store import STORE

router = APIRouter(prefix="/api/v1", tags=["firmware-bundles"])

COMPONENTS = ("bmc", "gpu", "nvswitch", "cx", "bf")
APPLY_ORDER = ["bmc", "bf", "gpu", "cx", "nvswitch"]
# fleet baseline (ncp.FIRMWARE_BASELINE) as one bundle
DEFAULT_BUNDLE = {"version": "vr-2026.05",
                  "components": {"bmc": IDRAC10_FW, "gpu": "97.10.2C.00.11",
                                 "nvswitch": "2.5.1", "cx": "40.44.1036", "bf": "3.1.0"}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.bundles: dict[str, dict] = {}
        self.plans: dict[str, dict] = {}
        self.seq = 0
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.bundles = {DEFAULT_BUNDLE["version"]: {
                **DEFAULT_BUNDLE, "components": dict(DEFAULT_BUNDLE["components"]),
                "apply_order": APPLY_ORDER, "default": True, "created_at": _now()}}
            self.plans.clear()
            self.seq = 0


FW = _State()


def get_bundle(version: Optional[str]) -> dict:
    b = FW.bundles.get(version or DEFAULT_BUNDLE["version"])
    if b is None:
        raise HTTPException(404, f"firmware bundle {version} not found")
    return b


class BundleIn(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    components: dict[str, str]


class PlanIn(BaseModel):
    rack: str
    bundle: Optional[str] = None
    window: Optional[str] = None


@router.get("/firmware/bundles")
def list_bundles() -> dict:
    with FW.lock:
        return {"bundles": list(FW.bundles.values()), "apply_order": APPLY_ORDER}


@router.post("/firmware/bundles", status_code=201)
def create_bundle(body: BundleIn) -> dict:
    comps = {k.lower(): v for k, v in body.components.items()}
    missing = [c for c in COMPONENTS if c not in comps]
    unknown = sorted(set(comps) - set(COMPONENTS))
    if missing or unknown:
        raise HTTPException(400, f"components must be exactly {list(COMPONENTS)}"
                                 f" (missing {missing}, unknown {unknown})")
    if comps["bmc"] != IDRAC10_FW:
        raise HTTPException(400, f"bmc (Dell iDRAC10) must stay {IDRAC10_FW}")
    with FW.lock:
        if body.version in FW.bundles:
            raise HTTPException(409, f"bundle {body.version} exists")
        b = {"version": body.version, "components": comps, "apply_order": APPLY_ORDER,
             "default": False, "created_at": _now()}
        FW.bundles[body.version] = b
    events.publish("firmware", {"kind": "bundle_created", "version": body.version})
    return b


def _rack_hosts(rack_id: str) -> list[dict]:
    """Hosts of a fleet rack (idle = ready, no tenant) or a DCIM bring-up rack."""
    rack = STORE.racks.get(rack_id)
    if rack is not None:
        out = []
        for tid in rack.tray_ids:
            t = STORE.trays[tid]
            idle = t.state == HwState.ready and not t.tenant_id
            out.append({"host_id": tid, "state": t.state.value, "tenant_id": t.tenant_id,
                        "action": "update" if idle else "skip",
                        "reason": None if idle else "host not idle"})
        return out
    from . import bringup
    r = bringup.DCIM.racks.get(rack_id)
    if r is None:
        raise HTTPException(404, f"rack {rack_id} not found")
    if r["state"] == "sellable":
        return [{"host_id": h, "state": "ready", "tenant_id": None, "action": "update",
                 "reason": None} for h in r.get("machines") or []]
    return [{"host_id": f"{rack_id}-tray-{i:02d}", "state": "bringup", "tenant_id": None,
             "action": "update", "reason": None} for i in range(18)]


@router.get("/firmware/plans")
def list_plans() -> dict:
    with FW.lock:
        return {"plans": list(FW.plans.values())}


@router.post("/firmware/plans", status_code=201)
def create_plan(body: PlanIn) -> dict:
    with FW.lock:
        b = get_bundle(body.bundle)
        hosts = _rack_hosts(body.rack)
        FW.seq += 1
        pid = f"fwp-{FW.seq:04d}"
        p = {"id": pid, "rack": body.rack, "bundle": b["version"],
             "window": body.window or "next-maintenance", "order": APPLY_ORDER,
             "hosts": hosts, "targets": sum(h["action"] == "update" for h in hosts),
             "skipped": sum(h["action"] == "skip" for h in hosts),
             "status": "scheduled", "created_at": _now(), "result": None}
        FW.plans[pid] = p
    events.publish("firmware", {"kind": "plan_scheduled", "plan": pid, "rack": body.rack,
                                "bundle": b["version"], "targets": p["targets"]})
    return p


@router.get("/firmware/plans/{plan_id}")
def get_plan(plan_id: str) -> dict:
    p = FW.plans.get(plan_id)
    if p is None:
        raise HTTPException(404, f"plan {plan_id} not found")
    return p


@router.post("/firmware/plans/{plan_id}:execute")
def execute_plan(plan_id: str) -> dict:
    p = get_plan(plan_id)
    if p["status"] == "completed":
        return p
    b = get_bundle(p["bundle"])
    targets = [f"/redfish/v1/Systems/{h['host_id']}" for h in p["hosts"]
               if h["action"] == "update"]
    st, res = dsx.ai_send("POST", "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
                          json={"ImageURI": f"https://fw.neocloud.local/nvfwupd/"
                                            f"{b['version']}.fwpkg",
                                "Targets": targets,
                                "Oem": {"Nvidia": {"Bundle": {
                                    "version": b["version"],
                                    "components": b["components"]}}}})
    if st is None:
        p["result"] = {"source": "local-fallback", "order": APPLY_ORDER}
    elif st >= 400:
        p["status"], p["result"] = "failed", {"source": "ai-infra", "status": st, "body": res}
        return p
    else:
        order = ((res or {}).get("Oem") or {}).get("Nvidia", {}).get("UpdateOrder")
        p["result"] = {"source": "ai-infra", "task": (res or {}).get("Id"),
                       "order": order or APPLY_ORDER}
    p["status"], p["completed_at"] = "completed", _now()
    events.publish("firmware", {"kind": "plan_completed", "plan": plan_id,
                                "rack": p["rack"], "bundle": p["bundle"]})
    return p
