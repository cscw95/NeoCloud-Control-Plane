"""F27 FinOps demo — last complete month's cost by rack / tenant / model (energy
kWh × tariff + straight-line depreciation, assumed prices), then a flex event is
injected and its demand-response settlement (kW × h × incentive) is read back.
Deterministic: the cost period is the previous full month (fixed hours)."""
from datetime import datetime, timezone


def _last_month():
    now = datetime.now(timezone.utc)
    y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def http(method, path, json=None, params=None):
    spec = {"method": method, "path": path, "as": "operator", "service": "nocp"}
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def _b(res):
    return res.body if isinstance(res.body, dict) else {}


def _won(v):
    return f"{int(v or 0):,}원"


def _cost(by):
    def expect(res, ctx):
        b = _b(res)
        t = b.get("totals") or {}
        top = (b.get("items") or [{}])[0]
        ok = res.status == 200 and b.get("by") == by and t.get("total_cost_krw", 0) > 0
        note = (b.get("meta") or {}).get("note")
        return (ok if by != "model" else res.status == 200,
                f"{b.get('period')} {by}별 {len(b.get('items') or [])}건 · 합계 {_won(t.get('total_cost_krw'))} "
                f"(전력 {_won(t.get('energy_cost_krw'))}, 감가 {_won(t.get('depreciation_krw'))}) · 1위 {top.get('key')}"
                + (f" · {note}" if note else ""),
                f"{b.get('period')} by {by}: {len(b.get('items') or [])} rows · total KRW {int(t.get('total_cost_krw') or 0):,} "
                f"(energy {int(t.get('energy_cost_krw') or 0):,}, depreciation {int(t.get('depreciation_krw') or 0):,}) · top {top.get('key')}")
    return expect


def _domain(ctx):
    return {"domain": ctx.get("domain", "pd-aif-stt-gasan-f1"), "reduce_kw": 400, "duration_s": 1800,
            "source": "demo-grid-operator"}


def _settled(res, ctx):
    b = _b(res)
    items = b.get("items") or []
    last = items[-1] if items else {}
    ok = bool(items) and float(last.get("kwh") or 0) > 0
    return (ok, f"정산 {len(items)}건 · 최근 {last.get('achieved_kw')} kW × {last.get('hours')} h = "
                f"{last.get('kwh')} kWh → {_won(last.get('incentive_krw'))} (출처 {b.get('source')})",
            f"{len(items)} settlements · latest {last.get('achieved_kw')} kW × {last.get('hours')} h = "
            f"{last.get('kwh')} kWh → KRW {int(last.get('incentive_krw') or 0):,} (source {b.get('source')})")


SCENARIO = {
    "id": "f27-finops",
    "order": 27,
    "feature": "F27",
    "title_ko": "F27 FinOps 원가 · 수요반응 정산",
    "title_en": "F27 FinOps cost · demand-response settlement",
    "steps": [
        {"id": "01-rack", "title_ko": "랙별 원가 (지난달)", "title_en": "Cost by rack (last month)",
         "call": http("GET", "/api/v1/finops/cost", params=lambda ctx: {"period": _last_month(), "by": "rack"}),
         "expect": _cost("rack"), "feature": "F27"},
        {"id": "02-tenant", "title_ko": "테넌트별 원가 (유휴 분리)", "title_en": "Cost by tenant (idle kept apart)",
         "call": http("GET", "/api/v1/finops/cost", params=lambda ctx: {"period": _last_month(), "by": "tenant"}),
         "expect": _cost("tenant"), "feature": "F27"},
        {"id": "03-model", "title_ko": "모델별 원가 (토큰 풀 배분)", "title_en": "Cost by model (token pool split)",
         "call": http("GET", "/api/v1/finops/cost", params=lambda ctx: {"period": _last_month(), "by": "model"}),
         "expect": _cost("model"), "feature": "F27"},
        {"id": "04-domains", "title_ko": "전력 도메인 조회", "title_en": "Read power domains",
         "call": http("GET", "/api/v1/power/domains"),
         "save": lambda res, ctx: ctx.update(domain=((res.body or {}).get("items") or (res.body or {}).get("domains")
                                                     or [{"id": "pd-aif-stt-gasan-f1"}])[0]["id"])},
        {"id": "05-inject", "title_ko": "그리드 감축 주입 (400 kW · 30분)", "title_en": "Inject grid curtailment (400 kW · 30 min)",
         "call": http("POST", "/api/v1/power/flex-events", _domain), "feature": "F20"},
        {"id": "05b-runs", "title_ko": "감축 run 조회 (F21)", "title_en": "Read the curtailment run (F21)",
         "call": http("GET", "/api/v1/power/curtailments"), "feature": "F21",
         "save": lambda res, ctx: ctx.update(curt_id=((res.body or {}).get("items") or [{}])[0].get("id"))},
        {"id": "05c-end", "title_ko": "감축 종료 → 정산 기록", "title_en": "End the curtailment → settlement",
         "call": http("POST", lambda ctx: f"/api/v1/power/curtailments/{ctx.get('curt_id')}/end"),
         "feature": "F21"},
        {"id": "06-settle", "title_ko": "수요반응 정산 확인", "title_en": "Demand-response settlement",
         "call": http("GET", "/api/v1/finops/dr-settlements"), "expect": _settled, "feature": "F27"},
    ],
}
