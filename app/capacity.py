"""F35 capacity scenarios — DSX Sim stand-in: N racks under a site power
budget, answered by a bundle of AI-Infra DPS simulate calls.

No open-source upstream (UPSTREAM-REFS.md: F35 "DPS 문서(공개 문서 기준).
원본 없음"). Each scenario fans out to `POST /dps/v1/simulate` on AI-Infra
(DPS static vs dynamic provisioning, docs.nvidia.com/datacenter/dps, 공개 문서
기준) once per power profile, with operating_limit_kw = site MW / PUE, and
judges whether the requested rack count fits. When AI-Infra is unreachable
the same static check runs on the NOCP blueprint numbers (vr-nvl72 MaxP 227
/ MaxQ 187 kW) and the scenario says source "local-blueprint".

  POST /api/v1/capacity/scenarios {racks, policy, profile, site_mw?, pue?,
                                   workload_mix?, name?}         (operator)
  GET  /api/v1/capacity/scenarios            GET /api/v1/capacity/scenarios/{id}
"""

from __future__ import annotations

import itertools
import math
import threading
from datetime import datetime, timezone
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from . import dsx
from .contracts import _caller_tenant
from .spec import get_blueprint

router = APIRouter(prefix="/api/v1", tags=["capacity"])

PROFILES = ("max-p", "training", "balanced", "inference", "max-q")
POLICIES = ("static", "dynamic", "compare")
DEFAULT_RACKS, DEFAULT_SITE_MW, DEFAULT_PUE = 104, 30.0, 1.25
GPUS_PER_RACK = 72
MAX_SCENARIOS = 200


class ScenarioBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    racks: int = DEFAULT_RACKS
    policy: str = "compare"
    profile: Union[str, list[str]] = "max-p"
    site_mw: float = DEFAULT_SITE_MW
    pue: float = DEFAULT_PUE
    workload_mix: Optional[dict[str, float]] = None
    name: Optional[str] = None


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.items: dict[str, dict] = {}
        self.ids = itertools.count(1)


S = _State()


def reset() -> None:
    with S.lock:
        S.items.clear()
        S.ids = itertools.count(1)


def _require_operator(request: Request) -> None:
    if _caller_tenant(request) is not None:
        raise HTTPException(403, "operator only")


def _local(profile: str, limit_kw: float) -> dict:
    """Static-only fallback on the blueprint rack caps."""
    bp = get_blueprint("vr-nvl72")
    rack_kw = bp.maxq_rack_kw if profile == "max-q" else bp.maxp_rack_kw
    racks = int(limit_kw // rack_kw)
    case = {"rack_budget_kw": float(rack_kw), "racks_supported": racks,
            "gpus_supported": racks * GPUS_PER_RACK,
            "expected_draw_kw": float(racks * rack_kw),
            "headroom_kw": round(limit_kw - racks * rack_kw, 1),
            "tokens_per_s": None, "tokens_per_mw": None}
    return {"static": case, "dynamic": None, "delta": None}


def _judge(case: Optional[dict], racks: int, limit_kw: float) -> Optional[dict]:
    if not case:
        return None
    sup = case["racks_supported"]
    per = case["rack_budget_kw"]
    need = round(racks * per, 1)
    tps = case.get("tokens_per_s")
    return {**case, "fits": sup >= racks, "racks_requested": racks,
            "shortfall_racks": max(0, racks - sup),
            "spare_racks": max(0, sup - racks),
            "requested_budget_kw": need,
            "requested_headroom_kw": round(limit_kw - need, 1),
            "requested_tokens_per_s": round(tps / sup * racks) if tps and sup else None}


def _run_profile(profile: str, body: ScenarioBody, limit_kw: float) -> dict:
    req = {"operating_limit_kw": round(limit_kw, 1), "profile": profile,
           "gpus_per_rack": GPUS_PER_RACK}
    if body.workload_mix:
        req["workload_mix"] = body.workload_mix
    st, res = dsx.ai_send("POST", "/dps/v1/simulate", json=req)
    if st == 200 and isinstance(res, dict) and "static" in res:
        src = "dps-simulate"
    elif st is not None and st >= 400 and st != 404:
        raise HTTPException(st if st in (409, 422) else 502,
                            f"DPS simulate rejected: {res}")
    else:
        res, src = _local(profile, limit_kw), "local-blueprint"
    out = {"profile": profile, "source": src, "request": req,
           "inputs": res.get("inputs")}
    for pol in ("static", "dynamic"):
        if body.policy in (pol, "compare"):
            out[pol] = _judge(res.get(pol), body.racks, limit_kw)
    if body.policy == "compare":
        out["delta"] = res.get("delta")
    return out


def _recommend(results: list[dict], policy: str) -> Optional[dict]:
    cands = []
    for r in results:
        for pol in (("static", "dynamic") if policy == "compare" else (policy,)):
            c = r.get(pol)
            if c and c["fits"]:
                cands.append((c.get("requested_tokens_per_s") or 0,
                              c["requested_headroom_kw"], r["profile"], pol))
    if not cands:
        return None
    tps, head, prof, pol = max(cands)
    return {"profile": prof, "policy": pol, "requested_tokens_per_s": tps or None,
            "requested_headroom_kw": head}


def _sentence(sc: dict) -> dict:
    rec, b = sc["recommendation"], sc["inputs"]
    if rec:
        ko = (f"{b['racks']}랙은 {b['site_mw']} MW(PUE {b['pue']}, IT {sc['it_limit_kw']:,.0f} kW)"
              f" 안에 {rec['profile']}·{rec['policy']} 정책으로 들어갑니다"
              f" (여유 {rec['requested_headroom_kw']:,.0f} kW).")
        en = (f"{b['racks']} racks fit {b['site_mw']} MW (PUE {b['pue']}, IT "
              f"{sc['it_limit_kw']:,.0f} kW) with {rec['profile']} / {rec['policy']}"
              f" (headroom {rec['requested_headroom_kw']:,.0f} kW).")
    else:
        worst = min((r.get(p)["shortfall_racks"] for r in sc["results"]
                     for p in ("static", "dynamic") if r.get(p)), default=b["racks"])
        ko = (f"{b['racks']}랙은 {b['site_mw']} MW 안에 들어가지 않습니다"
              f" (최소 {worst}랙 부족).")
        en = f"{b['racks']} racks do not fit {b['site_mw']} MW (short by ≥{worst} racks)."
    return {"ko": ko, "en": en}


@router.post("/capacity/scenarios", status_code=201)
def create_scenario(body: ScenarioBody, request: Request) -> dict:
    _require_operator(request)
    profiles = [body.profile] if isinstance(body.profile, str) else list(body.profile)
    if profiles == ["all"]:
        profiles = list(PROFILES)
    if not profiles or len(profiles) > len(PROFILES) or \
            any(p not in PROFILES for p in profiles):
        raise HTTPException(422, f"profile must be one of {list(PROFILES)} (or a list, or 'all')")
    if body.policy not in POLICIES:
        raise HTTPException(422, f"policy must be one of {list(POLICIES)}")
    if not 1 <= body.racks <= 2000:
        raise HTTPException(422, "racks must be 1..2000")
    if not (math.isfinite(body.site_mw) and 0 < body.site_mw <= 1000):
        raise HTTPException(422, "site_mw must be in (0, 1000]")
    if not (math.isfinite(body.pue) and 1.0 <= body.pue <= 3.0):
        raise HTTPException(422, "pue must be in [1.0, 3.0]")
    limit_kw = body.site_mw * 1000.0 / body.pue
    results = [_run_profile(p, body, limit_kw) for p in dict.fromkeys(profiles)]
    with S.lock:
        if len(S.items) >= MAX_SCENARIOS:
            S.items.pop(next(iter(S.items)))
        sid = f"cap-{next(S.ids):04d}"
        sc = {"id": sid, "name": body.name or f"{body.racks} racks @ {body.site_mw:g} MW",
              "created_at": datetime.now(timezone.utc).isoformat(),
              "inputs": {"racks": body.racks, "policy": body.policy,
                         "profiles": list(dict.fromkeys(profiles)),
                         "site_mw": body.site_mw, "pue": body.pue,
                         "workload_mix": body.workload_mix,
                         "gpus_per_rack": GPUS_PER_RACK},
              "it_limit_kw": round(limit_kw, 1),
              "rack_blueprint": {"key": "vr-nvl72",
                                 "maxp_rack_kw": get_blueprint("vr-nvl72").maxp_rack_kw,
                                 "maxq_rack_kw": get_blueprint("vr-nvl72").maxq_rack_kw},
              "results": results,
              "source": "dps-simulate" if all(r["source"] == "dps-simulate"
                                              for r in results) else "local-blueprint"}
        sc["recommendation"] = _recommend(results, body.policy)
        sc["fits"] = sc["recommendation"] is not None
        sc["summary"] = _sentence(sc)
        S.items[sid] = sc
        return sc


@router.get("/capacity/scenarios")
def list_scenarios(request: Request) -> dict:
    _require_operator(request)
    with S.lock:
        rows = [{k: s[k] for k in ("id", "name", "created_at", "inputs", "fits",
                                   "recommendation", "source", "summary")}
                for s in S.items.values()]
    return {"count": len(rows), "scenarios": rows}


@router.get("/capacity/scenarios/{scenario_id}")
def get_scenario(scenario_id: str, request: Request) -> dict:
    _require_operator(request)
    with S.lock:
        sc = S.items.get(scenario_id)
    if sc is None:
        raise HTTPException(404, f"scenario {scenario_id} not found")
    return sc
