"""(c) Phase-1 DoD chain c / US-07 — XID fault → automatic cordon → drain → GPU
reset → replace → RMA (NVSentinel fault-quarantine → node-drainer →
fault-remediation order) → SLA downtime → credit → invoice deduction.

Contracts: F10 (remediation policy/runs), F09 (downtime, credits), F05 (invoice).
The XID arrives as a Redfish EventService event (`{"Events":[...]}`), the same
envelope AI-Infra pushes to POST /api/v1/oob/events; before F10 the pre-v2 manual
RMA (POST /api/v1/rma) stands in for the automatic run.
"""

from datetime import datetime, timezone

from ..engine import Result, check, http, won
from ._common import first_node_host, link, order_step, tenant_step


def _policy_ok(res, ctx):
    return check(res.body.get("mode") == "auto",
                 "자동 복구 정책 auto (cordon·drain·reset·replace·rma, 랙당 동시 1건)",
                 "remediation policy auto (cordon, drain, reset, replace, rma; 1 per rack)")


def _inject(ctx):
    """XID 79 as a Redfish Event — fed through the EventService receiver path."""
    from ... import oob
    host = first_node_host(ctx)
    ctx["machine"] = host
    ev = {"Events": [{
        "EventType": "Alert", "EventId": f"xid-{ctx['run_id']}",
        "MessageId": "NvidiaGPU.1.0.XidError",
        "Message": "GPU Xid 79: GPU has fallen off the bus (SEL)",
        "Severity": "Critical",
        "EventTimestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "OriginOfCondition": {"@odata.id": f"/redfish/v1/Systems/{host}"}}]}
    recs = oob.ingest(ev)
    return Result(202, {"accepted": len(recs), "ids": [r["id"] for r in recs],
                        "machine": host, "event": ev})


def _injected(res, ctx):
    return check(res.body.get("accepted") == 1,
                 f"XID 79 Critical 이벤트 수신 · 대상 {res.body.get('machine')}",
                 f"XID 79 Critical event received · target {res.body.get('machine')}")


def _remediated(res, ctx):
    runs = res.body if isinstance(res.body, list) else res.body.get("runs", [])
    mine = [r for r in runs if (r.get("target") or {}).get("machine_id") in
            (ctx.get("machine"), ctx.get("machine", "").removeprefix("nh-"))] or runs
    if not mine:
        return check(False, "복구 실행 없음", "no remediation run")
    r = mine[0]
    ctx["remediation_id"] = r.get("id")
    steps = " → ".join(s.get("name", "?") for s in r.get("steps", []))
    ok = r.get("outcome") in ("recovered", "replaced") and (r.get("mttr_min") or 0) <= 60
    return check(ok, f"{steps} · 결과 {r.get('outcome')} · MTTR {r.get('mttr_min')}분 (≤60)",
                 f"{steps} · outcome {r.get('outcome')} · MTTR {r.get('mttr_min')} min (≤60)")


def _rma_ok(res, ctx):
    cases = res.body.get("cases", []) if isinstance(res.body, dict) else []
    mine = [c for c in cases if ctx.get("machine", "~") in (c.get("machine"), "nh-" + str(c.get("machine")))
            or str(c.get("machine", "")).endswith(ctx.get("machine", "~").removeprefix("nh-"))]
    return check(mine, f"RMA {mine[0].get('id') if mine else '-'} · 단계 {mine[0].get('stage') if mine else '-'}",
                 f"RMA {mine[0].get('id') if mine else '-'} · stage {mine[0].get('stage') if mine else '-'}")


def _downtime_ok(res, ctx):
    rows = res.body if isinstance(res.body, list) else res.body.get("items", [])
    mins = sum(r.get("minutes", 0) for r in rows)
    return check(rows, f"다운타임 {len(rows)}건 · 합계 {mins}분 (노드·분 단위 귀속)",
                 f"{len(rows)} downtime records · {mins} min (per node-minute)")


def _sla_ok(res, ctx):
    b = res.body
    credits = b.get("credits") or []
    return check(credits, f"월 가용성 {b.get('availability_pct')}% (목표 {b.get('target_pct')}%) · 크레딧 {len(credits)}건",
                 f"monthly availability {b.get('availability_pct')}% (target {b.get('target_pct')}%) · {len(credits)} credit(s)")


def _run_ok(res, ctx):
    inv = [i for i in res.body.get("invoices", []) if i.get("tenant_id") == ctx["tenant_id"]]
    if inv:
        ctx["invoice_id"] = inv[0]["id"]
    return check(inv, f"청구 초안 {inv[0]['id'] if inv else '-'}", f"draft {inv[0]['id'] if inv else '-'}")


def _credit_on_invoice(res, ctx):
    b = res.body
    credits = b.get("sla_credits") or []
    amt = sum(c.get("amount", 0) for c in credits)
    return check(credits and amt > 0,
                 f"SLA 크레딧 {len(credits)}건 {won(amt)} 차감 → 합계 {won(b.get('total'))}",
                 f"{len(credits)} SLA credit(s) {won(amt)} deducted → total {won(b.get('total'))}")


SCENARIO = {
    "id": "c-xid-remediation",
    "order": 3,
    "feature": "F10/F09/F05",
    "story": "US-07",
    "title_ko": "(c) XID 장애 주입 → 자동 cordon/drain/교체 → SLA 크레딧 → 청구서 차감",
    "title_en": "(c) XID fault → auto cordon/drain/replace → SLA credit → invoice deduction",
    "summary_ko": "GPU가 버스에서 떨어지면 NVSentinel 흐름대로 자동 교체되고, 가용성 미달분이 다음 청구서에서 빠진다 (DoD 체인 c).",
    "summary_en": "A GPU off the bus is replaced by the NVSentinel flow and the SLA shortfall is credited (DoD chain c).",
    "steps": [
        tenant_step("01-tenant", "demo-c-xid"),
        order_step("02-order", 1),
        {"id": "03-policy", "feature": "F10", "requires": ["PUT /api/v1/remediation/policy"],
         "title_ko": "자동 복구 정책 켜기", "title_en": "Turn on auto remediation",
         "narration_ko": "기본은 '모니터링만'. 덱 NOTE대로 운영자가 auto로 켠다(opt-in).",
         "narration_en": "Default is monitor-only; the operator opts in to auto, as the deck notes.",
         "call": http("PUT", "/api/v1/remediation/policy",
                      {"mode": "auto", "actions": {"cordon": True, "drain": True, "gpu_reset": True,
                                                   "replace": True, "rma": True},
                       "max_parallel_per_rack": 1, "kpi_replace_minutes": 60}),
         "expect": _policy_ok,
         "links": [link("ops", "dsx-rma", "운영 · RMA·복구", "Ops · RMA & remediation")]},
        {"id": "04-inject-xid", "title_ko": "XID 79 장애 주입", "title_en": "Inject XID 79",
         "narration_ko": "AI-Infra가 보내는 것과 같은 Redfish EventService 봉투로 XID 79(GPU fallen off the bus)를 넣는다.",
         "narration_en": "XID 79 (GPU fallen off the bus) arrives in the same Redfish EventService envelope AI-Infra pushes.",
         "call": _inject, "expect": _injected,
         "links": [link("ops", "dsx-oob", "운영 · OOB 이벤트", "Ops · OOB events")]},
        {"id": "05-remediation", "feature": "F10", "requires": ["GET /api/v1/remediation/runs"],
         "title_ko": "자동 복구 실행 확인", "title_en": "Automatic remediation run",
         "narration_ko": "cordon → drain → GPU reset → 재검증 실패 → 예비 노드 교체 → RMA 순으로 진행, MTTR을 잰다.",
         "narration_en": "cordon → drain → GPU reset → re-validate fails → spare swap → RMA, with MTTR measured.",
         "expect_ko": "outcome recovered|replaced · MTTR ≤ 60분", "expect_en": "outcome recovered|replaced · MTTR ≤ 60 min",
         "call": http("GET", "/api/v1/remediation/runs", params=lambda c: {"tenant_id": c["tenant_id"]}),
         "fallback": http("POST", "/api/v1/rma",
                          lambda c: {"machine": first_node_host(c), "component": "GPU",
                                     "reason": "XID 79 (manual, pre-F10)", "source": "oob-event",
                                     "tenant": c["tenant_id"]}),
         "save": lambda r, c: c.setdefault("machine", first_node_host(c)),
         "expect": _remediated},
        {"id": "06-rma", "title_ko": "RMA 케이스 연결", "title_en": "Linked RMA case",
         "call": http("GET", "/api/v1/rma"), "expect": _rma_ok,
         "links": [link("ops", "dsx-rma", "운영 · RMA", "Ops · RMA")]},
        {"id": "07-downtime", "feature": "F09", "requires": ["GET /api/v1/sla/downtime"],
         "title_ko": "SLA 다운타임 귀속", "title_en": "SLA downtime attribution",
         "call": http("GET", "/api/v1/sla/downtime",
                      params=lambda c: {"tenant_id": c["tenant_id"], "period": c["period"]}),
         "expect": _downtime_ok},
        {"id": "08-sla-report", "feature": "F09", "requires": ["GET /api/v1/sla/downtime"],
         "title_ko": "월 SLA 보고서 · 크레딧", "title_en": "Monthly SLA report · credit",
         "call": http("GET", lambda c: f"/api/v1/tenants/{c['tenant_id']}/sla-report"),
         "expect": _sla_ok,
         "links": [link("customer", "status", "고객 · 상태·SLA", "Customer · status & SLA")]},
        {"id": "09-invoice-run", "feature": "F05", "requires": ["POST /api/v1/billing/invoices:run"],
         "title_ko": "월 청구 실행", "title_en": "Run monthly billing",
         "call": http("POST", "/api/v1/billing/invoices:run",
                      lambda c: {"period": c["period"], "tenant_id": c["tenant_id"]}),
         "expect": _run_ok},
        {"id": "10-credit-deducted", "feature": "F05",
         "requires": ["GET /api/v1/billing/invoices/{id}", "GET /api/v1/sla/downtime"],
         "title_ko": "청구서에서 크레딧 차감", "title_en": "Credit deducted on the invoice",
         "call": http("GET", lambda c: f"/api/v1/billing/invoices/{c['invoice_id']}"),
         "expect": _credit_on_invoice,
         "links": [link("customer", "billing", "고객 · 청구", "Customer · billing")]},
    ],
}
