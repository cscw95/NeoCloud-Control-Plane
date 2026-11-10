"""MaxLPS power (DSX MaxLPS / DPS `nvidia.dcpower.v1`) — NOCP northbound surface.

The power cap has one source of truth: the AI-Infra emulator's DPS service
(`/dps/v1/*`) and its Redfish PowerLimit control. NOCP proxies those and adds
the site budget link: every PowerDomain maps to a ComputeBlock (floor) and its
contracted `power_mw`, so an OperatingLimit above the floor contract is refused
here before it reaches DPS.

When AI-Infra is unreachable the same contract is answered from a local model
(`source: "local-fallback"`): domains from the topology, WPPS profiles, a
static-vs-dynamic simulation and flex events kept in memory.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import dsx, spec
from .store import STORE

router = APIRouter(prefix="/api/v1/power", tags=["maxlps-power"])

# WPPS workload power profiles (Mission Control WPPS / DPS). `cap_factor` is the
# fraction of rack TDP the profile allows; `perf_per_watt` is relative to max-p.
WPPS_PROFILES = [
    {"id": "max-p", "description": "Maximum performance — full TDP", "cap_factor": 1.0,
     "perf_per_watt": 1.0},
    # max-q is exactly the reference-design MaxQ / MaxP (187 / 227 kW, spec.py vr-nvl72)
    {"id": "max-q", "description": "Maximum efficiency (throughput / W)",
     "cap_factor": round(187 / 227, 4), "perf_per_watt": 1.18},
    {"id": "training", "description": "Training — sustained high draw, smoothing on",
     "cap_factor": 0.92, "perf_per_watt": 1.08},
    {"id": "inference", "description": "Inference — bursty, memory-bound", "cap_factor": 0.75,
     "perf_per_watt": 1.25},
    {"id": "balanced", "description": "Balanced default", "cap_factor": 0.87,
     "perf_per_watt": 1.12},
]
PROFILE_IDS = {p["id"] for p in WPPS_PROFILES}
RACK_POLICIES = ("maxq", "maxp", "dps-dynamic")

# Average draw / TDP per workload class (dynamic allocation sizes on this, static
# allocation must reserve full TDP per rack).
WORKLOAD_DRAW = {"training": 0.86, "inference": 0.62, "balanced": 0.74, "idle": 0.30}
DYNAMIC_RESERVE = 0.05            # DPS keeps 5 % of the OperatingLimit as headroom


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _PowerState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.domains: dict[str, dict] = {}       # local overrides / created domains
        self.groups: dict[str, dict] = {}
        self.rack_policy: dict[str, dict] = {}   # rack_id -> {policy, profile}
        self.flex_events: list[dict] = []

    def reset(self) -> None:
        with self.lock:
            self.domains.clear()
            self.groups.clear()
            self.rack_policy.clear()
            self.flex_events.clear()


POWER = _PowerState()


# ---------------------------------------------------------------------------
# Topology ↔ power domain mapping
# ---------------------------------------------------------------------------
def _block_racks(block_id: str) -> list:
    block = STORE.blocks.get(block_id)
    if block is None:
        return []
    racks = []
    for did in block.du_ids:
        du = STORE.dus.get(did)
        for su_id in (du.su_ids if du else []):
            racks.extend(STORE.racks_of_su(su_id))
    return racks


def _block_of_rack(rack_id: str) -> Optional[str]:
    for b in STORE.blocks.values():
        if any(r.id == rack_id for r in _block_racks(b.id)):
            return b.id
    return None


def _budget_kw(block_id: str) -> float:
    """Contracted floor budget (ComputeBlock.power_mw); falls back to the
    factory design power split across its blocks, then to summed rack TDP."""
    block = STORE.blocks.get(block_id)
    if block is None:
        return 0.0
    if block.power_mw:
        return block.power_mw * 1000
    f = STORE.factories.get(block.factory_id)
    if f and f.design_power_mw and f.block_ids:
        return f.design_power_mw * 1000 / len(f.block_ids)
    return float(sum(r.tdp_kw for r in _block_racks(block_id)))


def _local_domains() -> list[dict]:
    out = []
    for b in STORE.blocks.values():
        did = f"pd-{b.id}"
        racks = _block_racks(b.id)
        budget = _budget_kw(b.id)
        base = {"id": did, "name": b.name or b.id, "site": b.factory_id,
                "compute_block": b.id, "operating_limit_kw": budget,
                "mode": "static", "smoothing": False,
                "racks": [r.id for r in racks]}
        base.update(POWER.domains.get(did, {}))
        out.append(base)
    for did, d in POWER.domains.items():
        if d.get("created") and not any(x["id"] == did for x in out):
            out.append(dict(d))
    return out


def _group_racks(groups: Optional[list]) -> dict:
    """domain id -> rack ids, from DPS resource groups (AI-Infra reports a
    domain's `racks` as a count, the rack ids live on its groups)."""
    out: dict = {}
    for g in groups or []:
        if isinstance(g, dict) and isinstance(g.get("racks"), list):
            out.setdefault(g.get("domain"), []).extend(g["racks"])
    return out


def _enrich(d: dict, groups: Optional[dict] = None) -> dict:
    """Attach the NOCP site-budget link to a domain (remote or local)."""
    block = d.get("compute_block") or (d["id"][3:] if str(d.get("id", "")).startswith("pd-") else None)
    rack_ids = d.get("racks")
    if not isinstance(rack_ids, list):
        rack_ids = (groups or {}).get(d.get("id")) or []
    racks = [STORE.racks[r] for r in rack_ids if r in STORE.racks]
    if not racks and block:
        racks = _block_racks(block)
    cap = sum(r.power_cap_kw for r in racks)
    budget = _budget_kw(block) if block in STORE.blocks else None
    limit = float(d.get("operating_limit_kw") or 0)
    return {**d, "compute_block": block,
            "site_budget_kw": budget,
            "racks_cap_kw": cap, "racks_tdp_kw": sum(r.tdp_kw for r in racks),
            "rack_count": len(racks),
            "budget_ok": budget is None or limit <= budget + 1e-6,
            "static_oversubscribed": limit > 0 and cap > limit}


def _remote(path: str, params: Optional[dict] = None):
    return dsx.ai_get(path, params=params)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class DomainCreate(BaseModel):
    id: Optional[str] = None
    name: str
    compute_block: Optional[str] = None
    operating_limit_kw: float = Field(..., gt=0)
    mode: str = "static"
    smoothing: bool = False
    racks: list[str] = Field(default_factory=list)


class DomainUpdate(BaseModel):
    operating_limit_kw: Optional[float] = Field(None, gt=0)
    mode: Optional[str] = None
    smoothing: Optional[bool] = None


class SimulateRequest(BaseModel):
    domain: str
    operating_limit_kw: Optional[float] = Field(None, gt=0)
    workload_mix: dict = Field(default_factory=lambda: {"training": 0.6, "inference": 0.4})
    profile: Optional[str] = None         # WPPS profile (AI-Infra default max-p)
    smoothing: Optional[bool] = None      # power smoothing (default: the domain's)
    gpus_per_rack: Optional[int] = Field(None, ge=1, le=576)


class FlexEvent(BaseModel):
    domain: str
    reduce_kw: float = Field(..., gt=0)
    duration_s: int = Field(900, gt=0)
    source: str = "grid-operator"


def _check_mode(mode: Optional[str]) -> None:
    if mode is not None and mode not in ("static", "dynamic"):
        raise HTTPException(422, "mode must be 'static' or 'dynamic'")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/domains")
def list_domains() -> dict:
    body = _remote("/dps/v1/power-domains")
    if isinstance(body, (list, dict)):
        items = body if isinstance(body, list) else dsx_items(body)
        groups = _group_racks(dsx_items(_remote("/dps/v1/resource-groups") or []))
        return {"source": "ai-infra", "domains": [_enrich(d, groups) for d in items]}
    return {"source": "local-fallback", "domains": [_enrich(d) for d in _local_domains()]}


def dsx_items(body) -> list:
    from .adapters import _items
    return _items(body)


@router.post("/domains", status_code=201)
def create_domain(body: DomainCreate) -> dict:
    _check_mode(body.mode)
    if body.compute_block and body.compute_block not in STORE.blocks:
        raise HTTPException(404, f"compute block '{body.compute_block}' not found")
    if body.compute_block and body.operating_limit_kw > _budget_kw(body.compute_block):
        raise HTTPException(422, "operating_limit_kw exceeds the compute block budget "
                                 f"({_budget_kw(body.compute_block)} kW)")
    payload = body.model_dump()
    payload["id"] = body.id or f"pd-custom-{len(POWER.domains) + 1}"
    st, resp = dsx.ai_send("POST", "/dps/v1/power-domains", json=payload)
    if st is not None and st < 400:
        return {"source": "ai-infra", "domain": _enrich(resp if isinstance(resp, dict) else payload)}
    if st is not None:
        raise HTTPException(st, (resp or {}).get("detail", "dps rejected the domain")
                            if isinstance(resp, dict) else str(resp))
    with POWER.lock:
        POWER.domains[payload["id"]] = {**payload, "created": True}
    return {"source": "local-fallback", "domain": _enrich(payload)}


def _find_local(domain_id: str) -> dict:
    for d in _local_domains():
        if d["id"] == domain_id:
            return d
    raise HTTPException(404, f"power domain '{domain_id}' not found")


@router.get("/domains/{domain_id}")
def get_domain(domain_id: str) -> dict:
    body = _remote(f"/dps/v1/power-domains/{domain_id}")
    if isinstance(body, dict):
        groups = _group_racks(dsx_items(_remote("/dps/v1/resource-groups") or []))
        return {"source": "ai-infra", "domain": _enrich(body, groups)}
    return {"source": "local-fallback", "domain": _enrich(_find_local(domain_id))}


@router.patch("/domains/{domain_id}")
def update_domain(domain_id: str, body: DomainUpdate) -> dict:
    _check_mode(body.mode)
    patch = body.model_dump(exclude_none=True)
    block = domain_id[3:] if domain_id.startswith("pd-") else None
    if block not in STORE.blocks and body.operating_limit_kw is not None:
        remote = _remote(f"/dps/v1/power-domains/{domain_id}")   # DPS-side link
        block = remote.get("compute_block") if isinstance(remote, dict) else None
    if (block in STORE.blocks and body.operating_limit_kw is not None
            and body.operating_limit_kw > _budget_kw(block)):
        raise HTTPException(422, "operating_limit_kw exceeds the compute block budget "
                                 f"({_budget_kw(block)} kW)")
    st, resp = dsx.ai_send("PATCH", f"/dps/v1/power-domains/{domain_id}", json=patch)
    if st is not None and st < 400:
        return {"source": "ai-infra", "domain": _enrich(resp if isinstance(resp, dict)
                                                        else {"id": domain_id, **patch})}
    if st is not None and st != 404:
        raise HTTPException(st, resp.get("detail") if isinstance(resp, dict) else str(resp))
    _find_local(domain_id)
    with POWER.lock:
        POWER.domains.setdefault(domain_id, {}).update(patch)
    return {"source": "local-fallback", "domain": _enrich(_find_local(domain_id))}


@router.get("/profiles")
def profiles() -> dict:
    body = _remote("/dps/v1/profiles")
    if body is not None:
        return {"source": "ai-infra", "profiles": body if isinstance(body, list)
                else dsx_items(body) or body.get("profiles", [])}
    return {"source": "local-fallback", "profiles": WPPS_PROFILES}


def _local_simulate(req: SimulateRequest) -> dict:
    d = _find_local(req.domain)
    limit = req.operating_limit_kw or float(d["operating_limit_kw"])
    racks = [STORE.racks[r] for r in d["racks"] if r in STORE.racks]
    if not racks:
        raise HTTPException(422, f"domain '{req.domain}' has no racks")
    tdp = max(r.tdp_kw for r in racks)
    gpr = spec.get_blueprint(racks[0].blueprint_key).gpu_per_rack
    mix = {k: float(v) for k, v in req.workload_mix.items() if k in WORKLOAD_DRAW}
    total = sum(mix.values()) or 1.0
    draw = sum(WORKLOAD_DRAW[k] * v for k, v in mix.items()) / total if mix else 0.8
    static_racks = int(limit // tdp)
    dyn_racks = int(limit * (1 - DYNAMIC_RESERVE) // (tdp * draw))
    tokens_per_rack = 1.0           # normalized throughput unit per rack at full TDP

    def side(n, per_rack_kw):
        used = n * per_rack_kw
        return {"racks_supported": n, "gpus_supported": n * gpr,
                "power_used_kw": round(used, 1),
                "headroom_kw": round(limit - used, 1),
                "tokens_per_mw": round(n * tokens_per_rack / (limit / 1000), 3)}

    st, dy = side(static_racks, tdp), side(dyn_racks, tdp * draw)
    gain = (dyn_racks - static_racks) / static_racks if static_racks else None
    return {"domain": req.domain, "operating_limit_kw": limit, "rack_tdp_kw": tdp,
            "workload_mix": mix, "avg_draw_ratio": round(draw, 3),
            "static": st, "dynamic": dy,
            "gain_pct": round(gain * 100, 1) if gain is not None else None,
            "installed_racks": len(racks)}


@router.post("/simulate")
def simulate(body: SimulateRequest) -> dict:
    payload = body.model_dump(exclude_none=True)
    st, resp = dsx.ai_send("POST", "/dps/v1/simulate", json=payload)
    if st is not None and st < 400 and isinstance(resp, dict):
        return {"source": "ai-infra", **resp}
    return {"source": "local-fallback", **_local_simulate(body)}


@router.post("/flex-events", status_code=201)
def flex_event(body: FlexEvent) -> dict:
    payload = body.model_dump()
    st, resp = dsx.ai_send("POST", "/dps/v1/flex-events", json=payload)
    if st is not None and st < 400:
        rec = {"source": "ai-infra", **(resp if isinstance(resp, dict) else payload)}
    else:
        d = _find_local(body.domain)
        limit = float(d["operating_limit_kw"])
        if body.reduce_kw >= limit:
            raise HTTPException(422, "reduce_kw must be below the operating limit")
        rec = {"source": "local-fallback", **payload,
               "id": f"flex-{len(POWER.flex_events) + 1}",
               "operating_limit_kw": limit,
               "effective_limit_kw": limit - body.reduce_kw,
               "state": "active", "at": _now()}
    with POWER.lock:
        POWER.flex_events.append(rec)
    from . import events
    events.publish("grid", {"event": "flex", **rec}, source="nocp.power")
    return rec


@router.get("/flex-events")
def list_flex_events() -> list:
    return list(POWER.flex_events)


@router.get("/telemetry")
def telemetry(domain: Optional[str] = None) -> dict:
    body = _remote("/dps/v1/telemetry", params={"domain": domain} if domain else None)
    if body is not None:
        return {"source": "ai-infra", **(body if isinstance(body, dict) else {"items": body})}
    doms = [d for d in _local_domains() if not domain or d["id"] == domain]
    if domain and not doms:
        raise HTTPException(404, f"power domain '{domain}' not found")
    out = []
    for d in doms:
        racks = []
        for rid in d["racks"]:
            r = STORE.racks.get(rid)
            if r is None:
                continue
            util = WORKLOAD_DRAW["training"] if r.tenant_id else WORKLOAD_DRAW["idle"]
            pw = round(r.power_cap_kw * util, 1)
            racks.append({"rack": rid, "power_kw": pw, "cap_kw": r.power_cap_kw,
                          "headroom_kw": round(r.power_cap_kw - pw, 1),
                          **POWER.rack_policy.get(rid, {})})
        used = sum(x["power_kw"] for x in racks)
        limit = float(d["operating_limit_kw"])
        out.append({"domain": d["id"], "operating_limit_kw": limit,
                    "power_kw": round(used, 1), "headroom_kw": round(limit - used, 1),
                    "mode": d["mode"], "racks": racks})
    return {"source": "local-fallback", "domains": out, "at": _now()}


# ---------------------------------------------------------------------------
# Rack power policy (topology.set_power_policy) — DPS dynamic + WPPS profiles
# ---------------------------------------------------------------------------
def rack_policy_cap(rack, policy: str, profile: Optional[str]) -> int:
    """Rack cap (kW) for a policy / WPPS profile on the rack's blueprint."""
    bp = spec.get_blueprint(rack.blueprint_key)
    if profile:
        factor = next(p["cap_factor"] for p in WPPS_PROFILES if p["id"] == profile)
        return int(round(bp.rack_tdp_kw * factor))
    if policy == "maxq":
        return bp.maxq_rack_kw
    return bp.maxp_rack_kw                 # maxp, or dps-dynamic (DPS steers below)


def _is_static(rack_id: str) -> bool:
    return POWER.rack_policy.get(rack_id, {}).get("policy") != "dps-dynamic"


def check_block_budget(rack, policy: str, cap_kw: float) -> None:
    """Block budget rule: the caps of statically capped racks (maxq/maxp or a
    WPPS profile without DPS steering) are firm reservations, so their sum over
    the ComputeBlock must fit the block budget (power_mw). dps-dynamic racks
    reserve nothing: DPS steers them inside the domain OperatingLimit, which is
    itself bounded by the same budget on domain create/PATCH. 422 otherwise."""
    block = _block_of_rack(rack.id)
    if block is None:
        return
    budget = _budget_kw(block)
    reserved = sum(r.power_cap_kw for r in _block_racks(block)
                   if r.id != rack.id and _is_static(r.id))
    if policy != "dps-dynamic":
        reserved += cap_kw
    if reserved > budget + 1e-6:
        raise HTTPException(422, f"rack power policy would reserve {reserved:g} kW of static "
                                 f"caps in compute block '{block}', above its budget "
                                 f"({budget:g} kW); use dps-dynamic or lower other racks")


def apply_rack_policy(rack, policy: str, profile: Optional[str]) -> dict:
    """Compute the rack cap for a policy/profile, check the ComputeBlock budget
    and push it to AI-Infra (DPS rack profile + Redfish PowerLimit). Returns
    the propagation report."""
    cap = rack_policy_cap(rack, policy, profile)
    check_block_budget(rack, policy, cap)
    rack.power_cap_kw = cap
    POWER.rack_policy[rack.id] = {"policy": policy, "profile": profile}
    prop = {}
    if profile or policy == "dps-dynamic":
        st, _ = dsx.ai_send("POST", f"/dps/v1/racks/{rack.id}/profile",
                            json={"profile": profile or "balanced",
                                  "mode": "dynamic" if policy == "dps-dynamic" else "static"})
        prop["dps_profile"] = st
    st, _ = dsx.ai_send("PATCH", f"/redfish/v1/Chassis/{rack.id}/Controls/PowerLimit",
                        json={"SetPoint": cap * 1000})
    prop["redfish_power_limit"] = st
    ok = all(v is not None and v < 400 for v in prop.values())
    return {"cap_source": "ai-infra" if ok else "nocp-local", "ai_infra": prop}
