"""F21 grid curtailment policy engine — reserved customers protected.

A DSX Flex Load Target Set (F20, `grid.loadtarget.set.v1`, AI-Infra `/flex/v1`)
or the pre-v2 shorthand `POST /api/v1/power/flex-events` opens a curtailment
run. The engine walks the policy tiers in `order` and picks capacity to shed:

  token-batch  F02 quota override on the batch tier (TPM factor → 429)
  spot         DPS rack cap cut (Redfish Controls/PowerLimit SetPoint)
  on-demand    DPS rack cap cut, smaller max_reduction_pct
  reserved     never touched (`protect`)

and records an SLA exception (F09), a status-page notice (F13) and, at the end,
restores quotas and caps and writes a demand-response settlement record (F27
reads it). Flow follows DSX Flex integrator guide steps 3–6 (UPSTREAM-REFS §3.4):
subscribers shed load against the Power State Status they watch; the Flex Agent
breach/enforcement path stays in AI-Infra. The shed decision itself has no
upstream implementation (NeoCloud 고유; DSX Flex shed_hints are advisory).

Dependencies that live in other v2 modules are reached through thin seams with
local stubs when the module is absent:
  F02  app.token_quota.apply_override(tenant_id, tier, factor, until, reason)
       / clear_override(tenant_id, tier)          stub: CURT.quota_overrides
  F09  app.sla.create_exception(body) / close_exception(id)  stub: CURT.sla_exceptions
  F13  app.status_notices.create_notice(body) / close_notice(id)  stub: CURT.notices
  F06  app.contracts.contracts_of_tenant(tid) → commitment "reserved"

  GET|PUT /api/v1/power/curtailment-policy
  GET     /api/v1/power/curtailments          GET /api/v1/power/curtailments/{id}
  POST    /api/v1/power/curtailments          (operator start; NeoCloud ext)
  POST    /api/v1/power/curtailments/{id}/end (restore + settle; NeoCloud ext)
"""

from __future__ import annotations

import importlib
import itertools
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import dsx, events
from .store import STORE

router = APIRouter(prefix="/api/v1/power", tags=["grid-curtailment"])

COMMITMENTS = ("reserved", "on_demand", "spot")
STANDBY_KW = 1.5                       # rack floor (BMC/DPU rails, AI-Infra dps.py)
DR_INCENTIVE_KRW_PER_KWH = 1300        # 가정: 국내 DR 감축 실적 정산 단가 수준
TOKEN_BATCH_SHARE = 0.4                # 가정: token racks' draw that serves the batch tier
DEFAULT_POLICY = {
    "tiers": [
        {"name": "token-batch", "match": {"product": "token-api", "tier": "batch"},
         "order": 1, "max_reduction_pct": 70},
        {"name": "spot", "match": {"commitment": "spot"}, "order": 2,
         "max_reduction_pct": 50},
        {"name": "on-demand", "match": {"commitment": "on_demand"}, "order": 3,
         "max_reduction_pct": 20},
    ],
    "protect": ["reserved"],
    "commitments": {},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


class _Curt:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        self.policy: dict = {k: (list(v) if isinstance(v, list) else
                                 dict(v) if isinstance(v, dict) else v)
                             for k, v in DEFAULT_POLICY.items()}
        self.runs: dict[str, dict] = {}
        self.by_source: dict[str, str] = {}       # load target / flex id → run id
        self.quota_overrides: dict[tuple, dict] = {}
        self.sla_exceptions: dict[str, dict] = {}
        self.notices: dict[str, dict] = {}
        self.settlements: list[dict] = []
        self._seq = itertools.count(1)

    def nid(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq):04d}"


CURT = _Curt()


def reset() -> None:
    with CURT.lock:
        CURT.reset()


def _module(name: str):
    try:
        return importlib.import_module(f"app.{name}")
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# seams to F02 / F09 / F13 / F06 (stubs when the module is not merged yet)
# ---------------------------------------------------------------------------
def quota_override(tenant_id: str, tier: str, factor: float, until: str,
                   reason: str) -> dict:
    m = _module("token_quota")
    rec = {"tenant_id": tenant_id, "tier": tier, "factor": factor, "until": until,
           "reason": reason, "via": "stub"}
    if m and hasattr(m, "apply_override"):
        res = m.apply_override(tenant_id, tier, factor, until, reason)
        rec = {**rec, **(res if isinstance(res, dict) else {}), "via": "F02"}
    CURT.quota_overrides[(tenant_id, tier)] = rec    # run-scoped record (list endpoint)
    return rec


def quota_clear(tenant_id: str, tier: str) -> None:
    m = _module("token_quota")
    if m and hasattr(m, "clear_override"):
        m.clear_override(tenant_id, tier)
    CURT.quota_overrides.pop((tenant_id, tier), None)


def quota_factor(tenant_id: str, tier: str) -> float:
    """Active override factor (1.0 = none): F02 token_quota when merged, else the stub."""
    m = _module("token_quota")
    if m and hasattr(m, "QUOTA"):
        o = m.QUOTA.override(tenant_id, tier)
        return float(o["factor"]) if o else 1.0
    rec = CURT.quota_overrides.get((tenant_id, tier))
    return float(rec["factor"]) if rec else 1.0


def _sla_exception(body: dict) -> dict:
    m = _module("sla")
    if m and hasattr(m, "create_exception"):
        res = m.create_exception(body)
        return res if isinstance(res, dict) else {"id": str(res), **body}
    rec = {"id": CURT.nid("slaex"), **body, "via": "stub"}
    CURT.sla_exceptions[rec["id"]] = rec
    return rec


def _sla_close(ex_id: Optional[str], end: str) -> None:
    m = _module("sla")
    if ex_id and m and hasattr(m, "close_exception"):
        m.close_exception(ex_id, end)
    if ex_id in CURT.sla_exceptions:
        CURT.sla_exceptions[ex_id]["end"] = end


def _notice(body: dict) -> dict:
    m = _module("status_notices")
    if m and hasattr(m, "create_notice"):
        res = m.create_notice(body)
        return res if isinstance(res, dict) else {"id": str(res), **body}
    rec = {"id": CURT.nid("ntc"), **body, "state": "active", "via": "stub"}
    CURT.notices[rec["id"]] = rec
    return rec


def _notice_close(nid: Optional[str], end: str) -> None:
    m = _module("status_notices")
    if nid and m and hasattr(m, "close_notice"):
        m.close_notice(nid, end)
    if nid in CURT.notices:
        CURT.notices[nid].update(state="resolved", end=end)


def _sla_get(ex_id: Optional[str]) -> Optional[dict]:
    m = _module("sla")
    if ex_id and m and hasattr(m, "SLA") and ex_id in m.SLA.exceptions:
        return m.SLA.exceptions[ex_id]
    return CURT.sla_exceptions.get(ex_id)


def _notice_get(nid: Optional[str]) -> Optional[dict]:
    m = _module("status_notices")
    if nid and m and hasattr(m, "get_notice"):
        try:
            return m.get_notice(nid)
        except Exception:                          # noqa: BLE001 — unknown id
            pass
    return CURT.notices.get(nid)


def commitment_of(tenant_id: Optional[str]) -> str:
    """reserved (F06 contract or policy map) | on_demand | spot (unsold racks)."""
    if not tenant_id:
        return "spot"
    explicit = CURT.policy.get("commitments", {}).get(tenant_id)
    if explicit in COMMITMENTS:
        return explicit
    m = _module("contracts")
    if m and hasattr(m, "contracts_of_tenant"):
        try:
            for c in m.contracts_of_tenant(tenant_id):
                if c.get("commitment") == "reserved" and \
                        c.get("status", "active") in ("active", "pending"):
                    return "reserved"
        except Exception:                      # noqa: BLE001
            pass
    return "on_demand"


# ---------------------------------------------------------------------------
# power view (AI-Infra DPS telemetry, local fallback)
# ---------------------------------------------------------------------------
def _rack_power(domain: Optional[str]) -> tuple[dict, str]:
    """rack_id → {domain, measured_kw, cap_kw}; second value = source."""
    body = dsx.ai_get("/dps/v1/telemetry", params={"domain": domain} if domain else None)
    out: dict[str, dict] = {}
    if isinstance(body, dict) and body.get("domains"):
        for d in body["domains"]:
            for g in d.get("groups", []):
                for r in g.get("racks", []):
                    out[r["rack_id"]] = {"domain": d["id"], "measured_kw": r["measured_kw"],
                                         "cap_kw": r["cap_kw"],
                                         "manual_cap_kw": r.get("manual_cap_kw")}
        return out, "ai-infra"
    from . import power
    for d in power._local_domains():
        if domain and d["id"] != domain:
            continue
        for rid in d["racks"]:
            r = STORE.racks.get(rid)
            if r is None:
                continue
            util = power.WORKLOAD_DRAW["training" if r.tenant_id else "idle"]
            out[rid] = {"domain": d["id"], "measured_kw": round(r.power_cap_kw * util, 1),
                        "cap_kw": float(r.power_cap_kw), "manual_cap_kw": None}
    return out, "local-fallback"


def _domain_site(domain: Optional[str]) -> Optional[str]:
    if not domain:
        return None
    from . import power
    for d in power._local_domains():
        if d["id"] == domain:
            return d.get("site") or d.get("compute_block")
    return None


def _match(tier: dict, ctx: dict) -> bool:
    return all(ctx.get(k) == v for k, v in (tier.get("match") or {}).items())


def _token_tenants() -> list[str]:
    m = _module("token_api")
    gw = getattr(m, "GW", None) if m else None
    if gw is not None and getattr(gw, "keys", None):
        return sorted({k["tenant_id"] for k in gw.keys.values() if not k.get("revoked")})
    return sorted(STORE.tenants)


def _token_racks() -> list[str]:
    body = dsx.ai_get("/dynamo/v1/deployments")
    items = body.get("items", body.get("data", [])) if isinstance(body, dict) else (body or [])
    return sorted({r for d in items if isinstance(d, dict) for r in d.get("racks", [])})


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
def _set_rack_cap(rack_id: str, kw: Optional[float]) -> Optional[int]:
    st, _ = dsx.ai_send("PATCH", f"/redfish/v1/Chassis/{rack_id}/Controls/PowerLimit",
                        json={"SetPoint": None if kw is None else round(kw * 1000.0)})
    return st


def plan_and_apply(run: dict) -> dict:
    """Pick and apply actions tier by tier until requested_kw is covered."""
    racks, src = _rack_power(run.get("domain"))
    run["power_source"] = src
    need = float(run["requested_kw"])
    protect = set(CURT.policy.get("protect") or [])
    actions, protected = [], {"tenants": set(), "racks": 0}
    token_racks = set(_token_racks())
    until = run["until"]
    for tier in sorted(CURT.policy["tiers"], key=lambda t: t.get("order", 99)):
        pct = max(0.0, min(100.0, float(tier.get("max_reduction_pct", 0)))) / 100.0
        m = tier.get("match") or {}
        if m.get("product") == "token-api":
            if m.get("tier", "batch") == "interactive" or "interactive" in protect:
                continue
            factor = round(1.0 - pct, 3)
            pool_kw = sum(racks[r]["measured_kw"] for r in token_racks if r in racks)
            est = round(pool_kw * TOKEN_BATCH_SHARE * pct, 1)
            for tid in _token_tenants():
                res = quota_override(tid, m.get("tier", "batch"), factor, until,
                                     f"grid curtailment {run['id']}")
                actions.append({"kind": "quota_override", "tier_name": tier["name"],
                                "target": f"{tid}/{m.get('tier', 'batch')}", "amount": factor,
                                "unit": "factor", "via": res.get("via")})
            if est and actions:
                actions[-1]["est_kw"] = est
                need -= est
            continue
        for rid in sorted(racks, key=lambda r: -racks[r]["measured_kw"]):
            if need <= 0:
                break
            rack = STORE.racks.get(rid)
            tid = rack.tenant_id if rack else None
            ctx = {"commitment": commitment_of(tid), "product": "bmaas"}
            if ctx["commitment"] in protect:
                continue
            if not _match(tier, ctx) or any(a.get("target") == rid for a in actions):
                continue
            meas = racks[rid]["measured_kw"]
            cut = min(need, max(0.0, meas * pct), max(0.0, meas - STANDBY_KW))
            if cut <= 0.05:
                continue
            kind = "preempt" if pct >= 1.0 else "dps_cap"
            new_cap = max(STANDBY_KW, meas - cut)
            st = _set_rack_cap(rid, new_cap)
            actions.append({"kind": kind, "tier_name": tier["name"], "target": rid,
                            "tenant_id": tid, "commitment": ctx["commitment"],
                            "amount": round(cut, 1), "unit": "kW",
                            "cap_kw": round(new_cap, 1),
                            "prev_manual_cap_kw": racks[rid].get("manual_cap_kw"),
                            "applied": "ai-infra" if st and st < 400 else "nocp-local"})
            need -= cut
    for rid, info in racks.items():
        rack = STORE.racks.get(rid)
        if rack and rack.tenant_id and commitment_of(rack.tenant_id) in protect:
            protected["tenants"].add(rack.tenant_id)
            protected["racks"] += 1
    run["actions"] = actions
    run["achieved_kw"] = round(sum(a.get("amount", 0) for a in actions if a["unit"] == "kW")
                               + sum(a.get("est_kw", 0) for a in actions), 1)
    run["protected"] = {"tenants": sorted(protected["tenants"]),
                        "tenant_count": len(protected["tenants"]),
                        "racks": protected["racks"]}
    run["shortfall_kw"] = round(max(0.0, run["requested_kw"] - run["achieved_kw"]), 1)
    return run


def start(requested_kw: float, duration_s: int, *, domain: Optional[str] = None,
          site: Optional[str] = None, load_target_id: Optional[str] = None,
          source: str = "operator", correlation_id: Optional[str] = None) -> dict:
    with CURT.lock:
        key = load_target_id or correlation_id
        if key and key in CURT.by_source:
            return CURT.runs[CURT.by_source[key]]
        if requested_kw is None or requested_kw <= 0:
            raise HTTPException(422, "requested_kw must be > 0")
        t0 = _now()
        end = t0 + timedelta(seconds=max(1, int(duration_s)))
        cid = CURT.nid("curt")
        run = {"id": cid, "load_target_id": load_target_id, "correlation_id": correlation_id,
               "source": source, "domain": domain, "site": site or _domain_site(domain),
               "requested_kw": round(float(requested_kw), 1), "achieved_kw": 0.0,
               "actions": [], "state": "active", "started_at": _iso(t0),
               "until": _iso(end), "ended_at": None,
               "policy": {"tiers": [t["name"] for t in sorted(
                   CURT.policy["tiers"], key=lambda t: t.get("order", 99))],
                   "protect": list(CURT.policy.get("protect") or [])}}
        CURT.runs[cid] = run
        if key:
            CURT.by_source[key] = cid
        if source == "operator" and not load_target_id and (domain or run["site"]):
            # register the target with the DSX Flex Agent before shedding so its
            # baseline is the pre-curtailment load (Power State Status tracks it)
            st, body = dsx.ai_send("POST", "/flex/v1/load-target-sets",
                                   json={"site": domain or run["site"],
                                         "reduce_kw": run["requested_kw"],
                                         "duration_s": int(duration_s)})
            if st and st < 400 and isinstance(body, dict):
                run["load_target_id"] = body.get("id")
                run["correlation_id"] = body.get("correlation_id")
                CURT.by_source[body.get("id")] = cid
        plan_and_apply(run)
        affected = sorted({a["tenant_id"] for a in run["actions"] if a.get("tenant_id")})
        ex = _sla_exception({"kind": "grid_curtailment", "start": run["started_at"],
                             "end": run["until"],
                             "scope": {"site": run["site"],
                                       "racks": [a["target"] for a in run["actions"]
                                                 if a["unit"] == "kW"],
                                       "tenants": affected},
                             "contract_clause": "SLA §예외: 전력망 수요반응(DSX Flex) 감축 구간",
                             "curtailment_id": cid})
        run["sla_exception_id"] = ex.get("id")
        nt = _notice({"kind": "grid_curtailment", "components": ["bmaas", "token-api"],
                      "start": run["started_at"], "end": run["until"],
                      "message_ko": f"전력망 감축 요청으로 {run['requested_kw']:g} kW 감축 중입니다. "
                                    "예약 고객 용량은 보호되며 토큰 Batch·스팟 용량이 일시 축소됩니다.",
                      "message_en": f"Grid curtailment of {run['requested_kw']:g} kW in progress. "
                                    "Reserved capacity is protected; token batch and spot "
                                    "capacity are temporarily reduced.",
                      "curtailment_id": cid})
        run["notice_id"] = nt.get("id")
    events.publish("grid", {"event": "CurtailmentStarted", "curtailment_id": cid,
                            "requested_kw": run["requested_kw"],
                            "achieved_kw": run["achieved_kw"],
                            "protected_tenants": run["protected"]["tenant_count"]},
                   source="nocp.curtailment")
    return run


def end(cid: str, reason: str = "target ended") -> dict:
    with CURT.lock:
        run = _get(cid)
        if run["state"] != "active":
            return run
        t1 = _now()
        restored = []
        for a in run["actions"]:
            if a["kind"] == "quota_override":
                tid, tier = a["target"].split("/", 1)
                quota_clear(tid, tier)
                restored.append(a["target"])
            elif a["kind"] in ("dps_cap", "preempt"):
                _set_rack_cap(a["target"], a.get("prev_manual_cap_kw"))
                restored.append(a["target"])
        _sla_close(run.get("sla_exception_id"), _iso(t1))
        _notice_close(run.get("notice_id"), _iso(t1))
        t0 = datetime.fromisoformat(run["started_at"])
        hours = max(0.0, (t1 - t0).total_seconds()) / 3600.0
        kwh = round(run["achieved_kw"] * hours, 3)
        settle = {"id": CURT.nid("drs"), "curtailment_id": cid,
                  "load_target_id": run.get("load_target_id"), "site": run["site"],
                  "requested_kw": run["requested_kw"], "achieved_kw": run["achieved_kw"],
                  "start": run["started_at"], "end": _iso(t1),
                  "hours": round(hours, 4), "kwh": kwh,
                  "incentive_krw_per_kwh": DR_INCENTIVE_KRW_PER_KWH,
                  "incentive_krw": int(kwh * DR_INCENTIVE_KRW_PER_KWH),
                  "basis": "감축 실적 kW × 시간 × 인센티브 단가 (가정 단가)"}
        CURT.settlements.append(settle)
        run.update(state="restored", ended_at=_iso(t1), end_reason=reason,
                   restored=restored, settlement=settle)
        lt = run.get("load_target_id")
        if lt and run["source"] != "dsx-flex" and not str(lt).startswith("flex"):
            dsx.ai_send("DELETE", f"/flex/v1/load-target-sets/{lt}")
    events.publish("grid", {"event": "CurtailmentRestored", "curtailment_id": cid,
                            "kwh": kwh}, source="nocp.curtailment")
    return run


def _get(cid: str) -> dict:
    run = CURT.runs.get(cid)
    if not run:
        raise HTTPException(404, f"curtailment {cid} not found")
    return run


def _expire() -> None:
    now = _now()
    for cid, run in list(CURT.runs.items()):
        if run["state"] == "active" and datetime.fromisoformat(run["until"]) <= now:
            end(cid, "target interval elapsed")


# ---------------------------------------------------------------------------
# DSX Flex subscription (AI-Infra Exchange `grid` → EventService push → bus)
# ---------------------------------------------------------------------------
def _cloudevent(ev: dict) -> Optional[dict]:
    data = ev.get("data") or {}
    raw = data.get("raw") if isinstance(data, dict) else None
    for cand in (data, raw or {}):
        if isinstance(cand, dict) and isinstance(cand.get("cloudevent"), dict):
            return cand["cloudevent"]
        oem = ((cand or {}).get("Oem") or {}).get("Nvidia") or {}
        if isinstance(oem.get("Data"), dict) and isinstance(oem["Data"].get("cloudevent"), dict):
            return oem["Data"]["cloudevent"]
    return None


def sync_flex() -> None:
    """Subscriber catch-up: open runs for active AI-Infra Load Target Sets and
    restore runs whose target was cleared/completed (lazy, on every read)."""
    body = dsx.ai_get("/flex/v1/load-target-sets")
    if not isinstance(body, dict):
        return
    for lt in body.get("items", []):
        cid = CURT.by_source.get(lt.get("id"))
        if lt.get("state") in ("scheduled", "active") and not cid:
            feeds = list(lt.get("feeds") or {})
            start(lt["reduce_kw"], lt["duration_s"],
                  domain=feeds[0] if len(feeds) == 1 else None, site=lt.get("site"),
                  load_target_id=lt["id"], source="dsx-flex",
                  correlation_id=lt.get("correlation_id"))
        elif lt.get("state") in ("completed", "cleared") and cid and \
                CURT.runs[cid]["state"] == "active":
            end(cid, f"load target {lt['state']}")


def _on_event(ev: dict) -> None:
    if ev.get("source") == "nocp.curtailment":
        return
    ce = _cloudevent(ev)
    if not ce:
        return
    if ce.get("type") == "grid.loadtarget.set.v1":
        for t in (ce.get("data") or {}).get("targets", []) if isinstance(ce.get("data"), dict) else []:
            lc = t.get("load_constraint")
            corr = ce.get("correlationid") or ce.get("id")
            if lc is None:
                for cid, run in list(CURT.runs.items()):
                    if run.get("correlation_id") == corr and run["state"] == "active":
                        end(cid, "load_constraint null (revert)")
                continue
            lt = None
            for x in (dsx.ai_get("/flex/v1/load-target-sets") or {}).get("items", []):
                if x.get("correlation_id") == corr:
                    lt = x
            if not lt:
                continue
            feeds = list(lt.get("feeds") or {})
            start(lt["reduce_kw"], lt["duration_s"],
                  domain=feeds[0] if len(feeds) == 1 else None, site=lt.get("site"),
                  load_target_id=lt["id"], source="dsx-flex", correlation_id=corr)


events.add_listener(_on_event)


def on_flex_event(rec: dict) -> Optional[dict]:
    """Hook for the shorthand POST /api/v1/power/flex-events."""
    try:
        return start(rec.get("reduce_kw"), rec.get("duration_s", 900),
                     domain=rec.get("domain"), load_target_id=rec.get("id"),
                     source="flex-event")
    except HTTPException:
        return None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class TierModel(BaseModel):
    name: str
    match: dict = Field(default_factory=dict)
    order: int = 50
    max_reduction_pct: float = Field(..., ge=0, le=100)


class PolicyBody(BaseModel):
    tiers: list[TierModel]
    protect: list[str] = Field(default_factory=lambda: ["reserved"])
    commitments: Optional[dict[str, str]] = None     # NeoCloud ext: tenant → commitment


class StartBody(BaseModel):
    requested_kw: float = Field(..., gt=0, le=1_000_000)
    duration_s: int = Field(900, gt=0, le=24 * 3600)
    domain: Optional[str] = None
    site: Optional[str] = None
    load_target_id: Optional[str] = None


@router.get("/curtailment-policy")
def get_policy() -> dict:
    with CURT.lock:
        return CURT.policy


@router.put("/curtailment-policy")
def put_policy(body: PolicyBody) -> dict:
    for c in (body.commitments or {}).values():
        if c not in COMMITMENTS:
            raise HTTPException(422, f"commitment must be one of {COMMITMENTS}")
    for p in body.protect:
        if p not in COMMITMENTS:
            raise HTTPException(422, f"protect entries must be in {COMMITMENTS}")
    if any((t.match or {}).get("commitment") in body.protect for t in body.tiers):
        raise HTTPException(422, "a tier cannot match a protected commitment")
    with CURT.lock:
        CURT.policy = {"tiers": [t.model_dump() for t in body.tiers],
                       "protect": list(body.protect),
                       "commitments": dict(body.commitments if body.commitments is not None
                                           else CURT.policy.get("commitments", {}))}
        return CURT.policy


@router.get("/curtailments")
def list_curtailments(state: Optional[str] = None) -> dict:
    with CURT.lock:
        sync_flex()
        _expire()
        items = [r for r in CURT.runs.values() if not state or r["state"] == state]
        return {"items": sorted(items, key=lambda r: r["started_at"], reverse=True)}


@router.post("/curtailments", status_code=201)
def create_curtailment(body: StartBody) -> dict:
    return start(body.requested_kw, body.duration_s, domain=body.domain, site=body.site,
                 load_target_id=body.load_target_id, source="operator")


@router.get("/curtailments/{cid}")
def get_curtailment(cid: str) -> dict:
    with CURT.lock:
        sync_flex()
        _expire()
        run = _get(cid)
        out = dict(run)
        out["sla_exception"] = _sla_get(run.get("sla_exception_id"))
        out["notice"] = _notice_get(run.get("notice_id"))
        lt = run.get("load_target_id")
        if lt and not str(lt).startswith("flex"):
            st = dsx.ai_get("/flex/v1/power-state-status",
                            params={"site": run.get("domain") or run.get("site")})
            if isinstance(st, dict):
                out["power_state"] = {k: st.get(k) for k in
                                      ("measured_kw", "target_kw", "compliance", "headroom_kw")}
        return out


@router.post("/curtailments/{cid}/end")
def end_curtailment(cid: str) -> dict:
    return end(cid, "operator end")


@router.get("/curtailments-settlements")
def list_settlements() -> dict:
    """Demand-response settlement records (input for F27 /finops/dr-settlements)."""
    with CURT.lock:
        _expire()
        return {"items": list(CURT.settlements)}


@router.get("/curtailments-quota-overrides")
def list_quota_overrides() -> dict:
    """Token-quota overrides opened by curtailment runs (applied via F02 token_quota)."""
    with CURT.lock:
        return {"items": list(CURT.quota_overrides.values())}
