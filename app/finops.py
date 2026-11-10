"""F27 FinOps — cost by rack / tenant / model and demand-response settlement.

No upstream settlement format exists (UPSTREAM-REFS.md §2: "F27 정산 형식 원본 없음").
The allocation idea follows OpenCost (cost = resource usage × unit price, with
idle/unallocated cost kept as its own line); the wire shape is a NeoCloud extension
schema, `ref/upstream/neocloud-ext/finops-cost.schema.json` and
`dr-settlement.schema.json` (`x-neocloud`), copied to `tests/contracts/`.
Curtailment performance (kW · hours) comes from the F21 engine, which itself reads
DSX Flex 1.7.0 Load Target Sets (OpenADR 3 `reports` is the fallback format).

  GET /api/v1/finops/cost?period=YYYY-MM&by=rack|tenant|model
      energy kWh = average rack kW × hours × PUE;  cost = kWh × tariff
                 + straight-line depreciation of the rack capex (가정 단가)
  GET /api/v1/finops/dr-settlements?period=YYYY-MM
      achieved kW × hours × incentive (F21 `CURT.settlements`; pre-v2
      `POST /api/v1/power/flex-events` records as fallback)

Rack draw: allocated racks at the training draw, unallocated at idle
(power.WORKLOAD_DRAW, same model as the local /power/telemetry fallback),
scaled per power domain by the AI-Infra DPS measured kW when it answers.
Operator surface (403 for tenant tokens).
"""

from __future__ import annotations

import importlib
import os
from calendar import monthrange
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request

from . import dsx, power
from .store import STORE

router = APIRouter(prefix="/api/v1/finops", tags=["finops"])

CURRENCY = "KRW"
TARIFF_KRW_PER_KWH = 185.0          # 가정: 산업용(을) 고압 평균 판매단가 수준
PUE = 1.25                          # 가정: DLC 시설 (CDU·칠러·UPS 손실 포함)
DEPRECIATION_YEARS = 5
CAPEX_KRW_PER_RACK = {              # 가정: 랙 1대 (컴퓨트·NVLink 스위치·파워셸프) 취득가
    "vr-nvl72": 5_600_000_000,
    "gb300-nvl72": 4_900_000_000,
    "gb200-nvl72": 4_200_000_000,
}
DR_INCENTIVE_KRW_PER_KWH = 1300     # 가정: F21 curtailment.py 와 같은 값 (있으면 그 값을 쓴다)


def _module(name: str):
    try:
        return importlib.import_module(f"app.{name}")
    except ImportError:
        return None


def _require_operator(request: Request) -> None:
    ident = getattr(request.state, "identity", None)
    if ident is not None and ident.role != "operator":
        raise HTTPException(403, "operator surface not allowed for tenant scope")


# ── period ────────────────────────────────────────────────────────────────
def period_window(period: Optional[str], now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    period = period or now.strftime("%Y-%m")
    try:
        p = datetime.strptime(period, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(422, "period must be 'YYYY-MM'")
    days = monthrange(p.year, p.month)[1]
    end = p.replace(day=days, hour=23, minute=59, second=59)
    if p > now:
        raise HTTPException(422, f"period {period} is in the future")
    full_h = days * 24.0
    if end >= now:                                       # current month: elapsed hours
        hours = (now - p).total_seconds() / 3600.0
        complete = False
    else:
        hours, complete = full_h, True
    return {"period": period, "start": p.isoformat(timespec="seconds"),
            "end": (now if not complete else end).isoformat(timespec="seconds"),
            "hours": round(hours, 4), "month_hours": full_h, "complete": complete}


# ── rack draw ─────────────────────────────────────────────────────────────
def _rack_blocks() -> dict[str, str]:
    out = {}
    for b in STORE.blocks.values():
        for r in power._block_racks(b.id):
            out.setdefault(r.id, b.id)
    return out


def _domain_scale() -> tuple[dict, str]:
    """pd-<block> → measured/modeled factor from AI-Infra DPS (empty when offline)."""
    body = dsx.ai_get("/dps/v1/telemetry")
    doms = body.get("domains") if isinstance(body, dict) else None
    if not doms:
        return {}, "model"
    modeled: dict[str, float] = {}
    blocks = _rack_blocks()
    for r in STORE.racks.values():
        b = blocks.get(r.id)
        if b:
            modeled[f"pd-{b}"] = modeled.get(f"pd-{b}", 0.0) + _model_kw(r)
    out = {}
    for d in doms:
        m = modeled.get(d.get("id"))
        if m and d.get("measured_kw") is not None:
            out[d["id"]] = float(d["measured_kw"]) / m
    return out, ("ai-infra-dps" if out else "model")


def _model_kw(rack) -> float:
    draw = power.WORKLOAD_DRAW["training"] if rack.tenant_id else power.WORKLOAD_DRAW["idle"]
    return float(rack.power_cap_kw) * draw


def rack_rows(win: dict, source: str = "auto") -> tuple[list[dict], str]:
    scale, src = (_domain_scale() if source != "model" else ({}, "model"))
    hours = win["hours"]
    blocks = _rack_blocks() if scale else {}
    rows = []
    for r in sorted(STORE.racks.values(), key=lambda r: r.id):
        block = blocks.get(r.id)
        kw = _model_kw(r) * scale.get(f"pd-{block}", 1.0)
        it_kwh = kw * hours
        kwh = it_kwh * PUE
        energy = kwh * TARIFF_KRW_PER_KWH
        capex = CAPEX_KRW_PER_RACK.get(r.blueprint_key, CAPEX_KRW_PER_RACK["vr-nvl72"])
        dep = capex / (DEPRECIATION_YEARS * 8760.0) * hours
        rows.append({"rack": r.id, "su": r.su_id, "blueprint_key": r.blueprint_key,
                     "tenant_id": r.tenant_id, "avg_power_kw": round(kw, 2),
                     "it_energy_kwh": round(it_kwh, 3), "energy_kwh": round(kwh, 3),
                     "energy_cost_krw": round(energy), "depreciation_krw": round(dep)})
    return rows, src


def _item(key: str, rows: list[dict], kind: str, **extra) -> dict:
    e = sum(x["energy_cost_krw"] for x in rows)
    d = sum(x["depreciation_krw"] for x in rows)
    return {"key": key, "kind": kind, "racks": len(rows),
            "avg_power_kw": round(sum(x["avg_power_kw"] for x in rows), 2),
            "energy_kwh": round(sum(x["energy_kwh"] for x in rows), 3),
            "energy_cost_krw": e, "depreciation_krw": d, "total_cost_krw": e + d, **extra}


# ── token racks / usage (F01 gateway + Dynamo deployments) ────────────────
def _token_racks() -> tuple[list[str], dict, str]:
    """(racks, {model: [racks]}, source). AI `/dynamo/v1/deployments` first, then
    env NOCP_FINOPS_TOKEN_RACKS (comma list, model unknown)."""
    body = dsx.ai_get("/dynamo/v1/deployments")
    items = body.get("items", body.get("data", [])) if isinstance(body, dict) else (body or [])
    by_model: dict[str, list] = {}
    for d in items if isinstance(items, list) else []:
        if isinstance(d, dict) and d.get("racks"):
            m = d.get("model") or d.get("served_model_name") or d.get("name") or "unknown"
            by_model.setdefault(m, []).extend(d["racks"])
    if by_model:
        racks = sorted({r for v in by_model.values() for r in v})
        return racks, by_model, "ai-infra-dynamo"
    env = [r.strip() for r in os.environ.get("NOCP_FINOPS_TOKEN_RACKS", "").split(",") if r.strip()]
    return env, {}, ("env" if env else "none")


def _token_usage(win: dict) -> dict[str, int]:
    m = _module("token_api")
    gw = getattr(m, "GW", None) if m else None
    out: dict[str, int] = {}
    if gw is None:
        return out
    start, end = win["start"], win["end"]
    with gw.lock:
        recs = list(gw.usage)
    for u in recs:
        at = str(u.get("at") or "")
        if at and not (start <= at.replace("Z", "+00:00") <= end):
            continue
        out[u.get("model") or "unknown"] = out.get(u.get("model") or "unknown", 0) + int(
            (u.get("usage") or {}).get("total_tokens") or 0)
    return out


def _by_model(rows: list[dict], win: dict) -> tuple[list[dict], dict]:
    racks, by_model, src = _token_racks()
    pool = [x for x in rows if x["rack"] in set(racks)]
    usage = _token_usage(win)
    meta = {"token_racks": len(pool), "token_racks_source": src,
            "usage_source": "token_api.GW" if usage else "none"}
    if not pool:
        meta["note"] = ("토큰 서빙 랙을 알 수 없음 (AI /dynamo/v1/deployments 미응답, "
                        "NOCP_FINOPS_TOKEN_RACKS 미설정)")
        return [], meta
    pool_item = _item("token-pool", pool, "pool")
    models = sorted(set(usage) | set(by_model))
    tot = sum(usage.values())
    items = []
    for mname in models:
        share = (usage.get(mname, 0) / tot) if tot else (1.0 / len(models))
        cost = round(pool_item["total_cost_krw"] * share)
        tok = usage.get(mname, 0)
        items.append({"key": mname, "kind": "model", "share": round(share, 6),
                      "tokens": tok, "racks": len(by_model.get(mname, [])) or None,
                      "energy_kwh": round(pool_item["energy_kwh"] * share, 3),
                      "energy_cost_krw": round(pool_item["energy_cost_krw"] * share),
                      "depreciation_krw": round(pool_item["depreciation_krw"] * share),
                      "total_cost_krw": cost,
                      "cost_per_1m_tokens_krw": round(cost / tok * 1e6, 2) if tok else None})
    meta["allocation"] = "tokens" if tot else "even"
    return items, meta


# ── demand-response settlements ───────────────────────────────────────────
def settlements(period: Optional[str] = None) -> tuple[list[dict], str]:
    curt = _module("curtailment")
    store = getattr(getattr(curt, "CURT", None), "settlements", None) if curt else None
    rate = getattr(curt, "DR_INCENTIVE_KRW_PER_KWH", DR_INCENTIVE_KRW_PER_KWH)
    if store is not None:
        items = [{**s, "source": "f21-curtailment"} for s in list(store)]
        src = "f21-curtailment"
    else:
        items, src = [], "flex-events"
        for i, ev in enumerate(power.list_flex_events(), 1):
            kw = float(ev.get("reduce_kw") or 0.0)
            hours = float(ev.get("duration_s") or 0) / 3600.0
            kwh = round(kw * hours, 3)
            start = ev.get("at") or ev.get("start") or ev.get("created_at")
            items.append({"id": f"drs-flex-{i:04d}", "curtailment_id": None,
                          "load_target_id": ev.get("id"), "site": ev.get("site"),
                          "domain": ev.get("domain"), "requested_kw": kw, "achieved_kw": kw,
                          "start": start, "end": None, "hours": round(hours, 4), "kwh": kwh,
                          "incentive_krw_per_kwh": rate, "incentive_krw": int(kwh * rate),
                          "basis": "flex-event 축약형: 달성 kW = 요청 kW 가정",
                          "source": "flex-events"})
    if period:
        items = [s for s in items if str(s.get("start") or "").startswith(period)]
    return items, src


# ── routes ────────────────────────────────────────────────────────────────
def assumptions() -> dict:
    return {"tariff_krw_per_kwh": TARIFF_KRW_PER_KWH, "pue": PUE,
            "depreciation": {"method": "straight-line", "years": DEPRECIATION_YEARS,
                             "capex_krw_per_rack": CAPEX_KRW_PER_RACK},
            "draw": {"allocated": power.WORKLOAD_DRAW["training"],
                     "unallocated": power.WORKLOAD_DRAW["idle"], "of": "rack power_cap_kw"},
            "tenant_basis": "현재 랙 배정 기준 (기간 중 재배정 미반영)",
            "label": "가정 단가 — 실제 요금·취득가로 교체"}


def cost_report(period: Optional[str], by: str = "rack", source: str = "auto") -> dict:
    win = period_window(period)
    rows, src = rack_rows(win, source)
    meta: dict = {}
    if by == "rack":
        items = [{"key": x["rack"], "kind": "rack", "racks": 1, "tenant_id": x["tenant_id"],
                  "su": x["su"], "blueprint_key": x["blueprint_key"],
                  "avg_power_kw": x["avg_power_kw"], "energy_kwh": x["energy_kwh"],
                  "energy_cost_krw": x["energy_cost_krw"],
                  "depreciation_krw": x["depreciation_krw"],
                  "total_cost_krw": x["energy_cost_krw"] + x["depreciation_krw"]}
                 for x in rows]
    elif by == "tenant":
        groups: dict[str, list] = {}
        for x in rows:
            groups.setdefault(x["tenant_id"] or "unallocated", []).append(x)
        items = []
        for k in sorted(groups, key=lambda k: (k == "unallocated", k)):
            t = STORE.tenants.get(k)
            items.append(_item(k, groups[k], "idle" if k == "unallocated" else "tenant",
                               name=getattr(t, "name", None)))
    else:
        items, meta = _by_model(rows, win)
    tot = _item("total", rows, "total")
    drs, _ = settlements(win["period"])
    dr = sum(int(s.get("incentive_krw") or 0) for s in drs)
    if by == "rack":
        items.sort(key=lambda i: -i["total_cost_krw"])
    return {"period": win["period"], "by": by, "currency": CURRENCY,
            "window": win, "power_source": src, "items": items,
            "totals": {"racks": tot["racks"], "energy_kwh": tot["energy_kwh"],
                       "energy_cost_krw": tot["energy_cost_krw"],
                       "depreciation_krw": tot["depreciation_krw"],
                       "total_cost_krw": tot["total_cost_krw"],
                       "dr_incentive_krw": dr, "net_cost_krw": tot["total_cost_krw"] - dr},
            "assumptions": assumptions(), **({"meta": meta} if meta else {})}


@router.get("/cost")
def get_cost(request: Request, period: Optional[str] = None,
             by: Literal["rack", "tenant", "model"] = "rack",
             source: Literal["auto", "model"] = "auto") -> dict:
    _require_operator(request)
    return cost_report(period, by, source)


@router.get("/dr-settlements")
def get_dr_settlements(request: Request, period: Optional[str] = None) -> dict:
    _require_operator(request)
    if period:
        period_window(period)
    items, src = settlements(period)
    return {"period": period, "currency": CURRENCY, "source": src, "items": items,
            "totals": {"count": len(items),
                       "kwh": round(sum(float(s.get("kwh") or 0) for s in items), 3),
                       "achieved_kw_max": max((float(s.get("achieved_kw") or 0) for s in items),
                                              default=0.0),
                       "incentive_krw": sum(int(s.get("incentive_krw") or 0) for s in items)},
            "basis": "감축 실적 kW × 시간 × 인센티브 단가 (가정 단가)"}
