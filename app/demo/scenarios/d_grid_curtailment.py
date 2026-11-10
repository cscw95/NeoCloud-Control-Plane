"""(d) Phase-2 DoD ① — grid curtailment (DSX Flex Load Target Set) with reserved
customers protected, the token Batch tier shrunk (429), restore and settlement.

Contracts: F20 (AI-Infra `/flex/v1/*`, Exchange topic `grid`), F21 (curtailment
policy/runs), F02 (quota override), F09 (SLA exception), F13 (status notice),
F27 (DR settlement). The pre-v2 shorthand POST /api/v1/power/flex-events exists on
main and carries the Load Target Set until F20/F21 land. DSX Flex 1.7.0 has no
public wire format: message names follow the NVIDIA DSX Flex brief (공개 문서 기준).
"""

from ..engine import check, http
from ._common import link, order_step, tenant_step


def _domains_ok(res, ctx):
    ds = res.body.get("domains", []) if isinstance(res.body, dict) else []
    if ds:
        ctx["domain"] = ds[0]["id"]
        ctx["limit_kw"] = ds[0].get("operating_limit_kw")
    return check(ds, f"전력 도메인 {len(ds)}개 · {ds[0]['id'] if ds else '-'} 한도 {ds[0].get('operating_limit_kw') if ds else '-'} kW",
                 f"{len(ds)} power domains · {ds[0]['id'] if ds else '-'} limit {ds[0].get('operating_limit_kw') if ds else '-'} kW")


def _policy_ok(res, ctx):
    return check(res.status in (200, 201) and "reserved" in (res.body.get("protect") or []),
                 "감축 정책: 예약 보호 > 온디맨드 > 스팟 > 토큰 Batch",
                 "curtailment policy: reserved protected > on-demand > spot > token batch")


def _flex_body(ctx):
    return {"domain": ctx["domain"], "reduce_kw": 2000.0, "duration_s": 900,
            "source": "dsx-flex"}


def _flex_ok(res, ctx):
    b = res.body
    ctx["flex_id"] = b.get("id")
    st = b.get("state") or b.get("status")
    return check(res.status == 201 and st in ("active", "scheduled", "pending"),
                 f"Load Target Set {b.get('id')} · −{b.get('reduce_kw')} kW · {b.get('duration_s')}s · {st} (출처 {b.get('source')})",
                 f"Load Target Set {b.get('id')} · −{b.get('reduce_kw')} kW · {b.get('duration_s')}s · {st} (source {b.get('source')})")


def _curtail_ok(res, ctx):
    rows = res.body if isinstance(res.body, list) else res.body.get("items", [])
    if not rows:
        return check(False, "감축 실행 없음", "no curtailment run")
    c = rows[0]
    ctx["curtailment_id"] = c.get("id")
    acts = c.get("actions", [])
    # DoD ①: no rack cap / preemption on the reserved customer's racks (its token
    # batch tier is not part of the reservation and may shrink like anyone's)
    hit_reserved = [a for a in acts if a.get("kind") != "quota_override"
                    and (a.get("tenant_id") == ctx.get("reserved_tenant")
                         or a.get("commitment") == "reserved")]
    kinds = sorted({a.get("kind") for a in acts})
    return check(not hit_reserved and c.get("achieved_kw", 0) > 0,
                 f"요청 {c.get('requested_kw')} kW · 달성 {c.get('achieved_kw')} kW · 조치 {', '.join(kinds)} · 예약 고객 조치 0건",
                 f"requested {c.get('requested_kw')} kW · achieved {c.get('achieved_kw')} kW · actions {', '.join(kinds)} · 0 on reserved")


def _batch_shrunk(res, ctx):
    rows = res.body if isinstance(res.body, list) else (
        res.body.get("data") or res.body.get("quotas") or [res.body])
    batch = [q for q in rows if q.get("tier") == "batch" and q.get("override")]
    f = (batch[0].get("override") or {}).get("factor") if batch else None
    return check(f is not None and f < 1,
                 f"토큰 Batch 등급 override factor {f} (TPM 축소 → 429), Interactive 유지",
                 f"token batch tier override factor {f} (TPM cut → 429), interactive untouched")


def _exception_ok(res, ctx):
    rows = res.body if isinstance(res.body, list) else res.body.get("items", [])
    grid = [e for e in rows if e.get("kind") == "grid_curtailment"]
    return check(grid, f"SLA 예외 구간 {len(grid)}건 (그리드 감축은 가용성 계산 제외)",
                 f"{len(grid)} SLA exception window(s) (grid curtailment excluded from availability)")


def _notice_ok(res, ctx):
    ns = [n for n in (res.body.get("notices") or []) if n.get("kind") == "grid_curtailment"]
    return check(ns, f"상태 페이지 공지 {len(ns)}건 (grid_curtailment)",
                 f"{len(ns)} status notice(s) (grid_curtailment)")


def _restored(res, ctx):
    b = res.body
    return check(b.get("state") in ("restored", "completed", "ended"),
                 f"감축 종료 → {b.get('state')} · 쿼터·DPS 상한 원복",
                 f"curtailment ended → {b.get('state')} · quotas and DPS caps restored")


def _settled(res, ctx):
    rows = res.body if isinstance(res.body, list) else res.body.get("items", [])
    kwh = sum(r.get("kwh", 0) or r.get("reduced_kwh", 0) for r in rows)
    return check(rows, f"수요반응 정산 {len(rows)}건 · 감축 {kwh} kWh",
                 f"{len(rows)} demand-response settlement(s) · {kwh} kWh curtailed")


SCENARIO = {
    "id": "d-grid-curtailment",
    "order": 4,
    "feature": "F20/F21/F02/F09/F13/F27",
    "title_ko": "(d) 그리드 감축(DSX Flex) → 예약 보호 · Batch 축소 · 원복 · 정산",
    "title_en": "(d) Grid curtailment (DSX Flex) → reserved protected, batch shrunk, restore, settlement",
    "summary_ko": "전력망 감축 요청이 오면 예약 고객 랙은 그대로 두고 토큰 Batch·스팟부터 줄인 뒤 원복·정산한다 (Phase 2 DoD ①).",
    "summary_en": "On a grid request, reserved racks stay untouched; token batch and spot shrink first, then restore and settle.",
    "steps": [
        tenant_step("01-reserved-tenant", "demo-d-reserved", key="reserved_tenant"),
        order_step("02-reserved-order", 1, key="reserved_tenant", out="reserved_order"),
        {"id": "03-domains", "title_ko": "DPS 전력 도메인", "title_en": "DPS power domains",
         "call": http("GET", "/api/v1/power/domains"), "expect": _domains_ok,
         "links": [link("ops", "dsx-power", "운영 · MaxLPS 전력", "Ops · MaxLPS power")]},
        {"id": "04-policy", "feature": "F21", "requires": ["PUT /api/v1/power/curtailment-policy"],
         "title_ko": "감축 정책 (예약 보호)", "title_en": "Curtailment policy (protect reserved)",
         "call": http("PUT", "/api/v1/power/curtailment-policy", lambda c: {
                       "commitments": {c["reserved_tenant"]: "reserved"},
                       "tiers": [{"name": "token-batch", "match": {"product": "token-api", "tier": "batch"},
                                  "order": 1, "max_reduction_pct": 70},
                                 {"name": "spot", "match": {"commitment": "spot"}, "order": 2,
                                  "max_reduction_pct": 50},
                                 {"name": "on-demand", "match": {"commitment": "on_demand"}, "order": 3,
                                  "max_reduction_pct": 20}],
                       "protect": ["reserved"]}),
         "expect": _policy_ok},
        {"id": "05-load-target", "title_ko": "Load Target Set 수신 (−2 MW)",
         "title_en": "Load Target Set received (−2 MW)",
         "narration_ko": "전력망 운영자가 15분간 2 MW 감축을 요청한다. F20 전에는 축약형 flex-event로 들어온다.",
         "narration_en": "The grid operator asks for −2 MW for 15 min; before F20 it arrives as the flex-event shorthand.",
         "call": http("POST", "/api/v1/power/flex-events", _flex_body), "expect": _flex_ok,
         "links": [link("ops", "dsx-power", "운영 · MaxLPS 전력", "Ops · MaxLPS power")]},
        {"id": "06-curtailment", "feature": "F21", "requires": ["GET /api/v1/power/curtailments"],
         "title_ko": "감축 실행: 누구를 줄였나", "title_en": "Curtailment run: who was reduced",
         "expect_ko": "예약 고객 랙 상한 변화 0 · 달성 kW > 0", "expect_en": "0 changes on reserved racks · achieved kW > 0",
         "call": http("GET", "/api/v1/power/curtailments"), "expect": _curtail_ok},
        {"id": "07-batch-quota", "feature": "F02", "requires": ["GET /api/v1/token/quotas"],
         "title_ko": "토큰 Batch 쿼터 축소", "title_en": "Token batch quota shrunk",
         "call": http("GET", "/api/v1/token/quotas"), "expect": _batch_shrunk},
        {"id": "08-sla-exception", "feature": "F09", "requires": ["GET /api/v1/sla/exceptions"],
         "title_ko": "SLA 예외 구간 기록", "title_en": "SLA exception recorded",
         "call": http("GET", "/api/v1/sla/exceptions"), "expect": _exception_ok},
        {"id": "09-status-notice", "feature": "F13", "requires": ["POST /api/v1/status/notices"],
         "title_ko": "상태 페이지 공지", "title_en": "Status page notice",
         "call": http("GET", "/api/v1/status", as_="none"), "expect": _notice_ok,
         "links": [link("customer", "status", "고객 · 상태", "Customer · status")]},
        {"id": "09b-end", "feature": "F21", "requires": ["POST /api/v1/power/curtailments/{id}/end"],
         "title_ko": "감축 종료 신호", "title_en": "Curtailment end signal",
         "narration_ko": "Load Target 기간이 끝나면(시연에서는 즉시) run을 닫는다.",
         "narration_en": "When the load-target window ends (immediately in the demo) the run is closed.",
         "call": http("POST", lambda c: f"/api/v1/power/curtailments/{c['curtailment_id']}/end")},
        {"id": "10-restore", "feature": "F21", "requires": ["GET /api/v1/power/curtailments/{id}"],
         "title_ko": "감축 종료 · 원복", "title_en": "Curtailment ends · restore",
         "call": http("GET", lambda c: f"/api/v1/power/curtailments/{c['curtailment_id']}"),
         "expect": _restored},
        {"id": "11-settlement", "feature": "F27", "requires": ["GET /api/v1/finops/dr-settlements"],
         "title_ko": "수요반응 정산", "title_en": "Demand-response settlement",
         "call": http("GET", "/api/v1/finops/dr-settlements"), "expect": _settled,
         "links": [link("biz", "revenue", "사업 · 매출", "Business · revenue")]},
    ],
}
