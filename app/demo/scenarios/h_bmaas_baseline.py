"""(h) BMaaS order baseline — the pre-v2 journey every v2 feature builds on.

All endpoints exist on main: tenant → 1-rack order → provisioning flow audit →
3-plane isolation check → acceptance report → approve → metering → reclaim.
"""

from ..engine import check, http
from ._common import link, order_step, tenant_step


def _flow(res, ctx):
    stages = {s["state"]: s for s in res.body.get("stages", [])}
    chans = stages.get("provisioning", {}).get("by_channel", {})
    calls = sum(chans.values())
    desc = ", ".join(f"{k} {v}" for k, v in sorted(chans.items()))
    return check(calls > 0 and "isolating" in stages,
                 f"단계 {len(stages)}개 · provisioning 하위 호출 {calls}건 ({desc})",
                 f"{len(stages)} stages · {calls} downstream calls in provisioning ({desc})")


def _iso(res, ctx):
    return check(res.body.get("ok") is True, "VRF · IB P_Key · NVLink 격리 검증 통과",
                 "VRF · IB P_Key · NVLink isolation verified")


def _report(res, ctx):
    checks = (res.body.get("report") or {}).get("checks", [])
    passed = sum(c.get("status") == "pass" for c in checks)
    return check(checks and passed == len(checks),
                 f"인수 점검 {passed}/{len(checks)} 통과 (NCCL·fio·burn-in·node-ready)",
                 f"acceptance checks {passed}/{len(checks)} pass (NCCL, fio, burn-in, node-ready)")


def _approved(res, ctx):
    st = res.body.get("status")
    return check(st in ("approved", "accepted"), f"인수 상태 {st}", f"acceptance {st}")


def _metering(res, ctx):
    lines = [x for x in res.body.get("lines", []) if x.get("source") == ctx["order_id"]]
    return check(lines, f"주문 {ctx['order_id']} 계측 라인 {len(lines)}개 (instance-h · GPU-h)",
                 f"{len(lines)} metering lines for {ctx['order_id']} (instance-h, GPU-h)")


def _reclaimed(res, ctx):
    return check(res.status == 201 and res.body.get("state") in ("delivered", "closed", "completed"),
                 f"반납 주문 {res.body.get('id')} → {res.body.get('state')} (소거 후 풀 복귀)",
                 f"reclaim order {res.body.get('id')} → {res.body.get('state')} (wiped, back to pool)")


SCENARIO = {
    "id": "h-bmaas-baseline",
    "order": 8,
    "feature": "baseline",
    "title_ko": "BMaaS 주문 기준선",
    "title_en": "BMaaS order baseline",
    "summary_ko": "v2 이전부터 있던 흐름: 테넌트 → 1랙 주문 → 격리 → 인수 → 계측 → 반납.",
    "summary_en": "The pre-v2 flow: tenant → 1-rack order → isolation → acceptance → metering → reclaim.",
    "steps": [
        tenant_step("01-tenant", "demo-h-baseline"),
        order_step("02-order", 1),
        {"id": "03-flow", "title_ko": "프로비저닝 흐름 감사", "title_en": "Provisioning flow audit",
         "narration_ko": "주문 단계별로 어떤 하위 API(Redfish·NVUE·VAST)가 몇 번 불렸는지 본다.",
         "narration_en": "Per stage, which downstream APIs (Redfish, NVUE, VAST) were called.",
         "call": http("GET", lambda c: f"/api/v1/orders/{c['order_id']}/flow"),
         "expect": _flow, "links": [link("ops", "provisioning", "운영 · 프로비저닝", "Ops · provisioning")]},
        {"id": "04-isolation", "title_ko": "3-plane 격리 검증", "title_en": "3-plane isolation check",
         "call": http("GET", lambda c: f"/api/v1/isolation/{c['tenant_id']}"),
         "expect": _iso, "links": [link("ops", "dsx-metering", "운영 · 미터링·격리", "Ops · metering & isolation")]},
        {"id": "05-acceptance-report", "title_ko": "인수 보고서", "title_en": "Acceptance report",
         "call": http("GET", lambda c: f"/api/v1/orders/{c['order_id']}/acceptance-report"),
         "expect": _report},
        {"id": "06-approve", "title_ko": "고객 인수 승인", "title_en": "Customer approves acceptance",
         "narration_ko": "고객 관리자 토큰으로 승인한다(테넌트 범위 검사 통과).",
         "narration_en": "Approved with the customer admin token (tenant scope enforced).",
         "call": http("POST", lambda c: f"/api/v1/orders/{c['order_id']}/acceptance",
                      {"decision": "approve"}, as_=lambda c: f"tenant:{c['tenant_id']}"),
         "expect": _approved, "links": [link("customer", "clusters", "고객 · 클러스터", "Customer · clusters")]},
        {"id": "07-metering", "title_ko": "사용량 계측", "title_en": "Usage metering",
         "call": http("GET", "/api/v1/metering/usage", params=lambda c: {"tenant_id": c["tenant_id"]}),
         "expect": _metering, "links": [link("ops", "dsx-metering", "운영 · 미터링", "Ops · metering")]},
        {"id": "08-reclaim", "title_ko": "반납(소거 후 풀 복귀)", "title_en": "Reclaim (wipe → pool)",
         "call": http("POST", "/api/v1/orders",
                      lambda c: {"tenant_id": c["tenant_id"], "kind": "terminate",
                                 "allocation_id": c["order_id_alloc"]}),
         "expect": _reclaimed},
    ],
}
