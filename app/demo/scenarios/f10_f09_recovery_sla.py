"""F10 auto-recovery + F09 SLA demo — XID → cordon → drain → GPU reset →
(recovered | replaced + RMA) → SLA downtime, exception, credit, ticket.

Self-contained (no import of app/demo/engine.py); step format follows engine.py.
Deterministic: step 00 resets F10/F09 state and reuses the demo tenant; the
simulated clock gives fixed MTTRs (13 min recovered; 22 min replaced + 13 min queued
behind the first run on the same rack).
"""


def http(method, path, json=None, params=None):
    spec = {"method": method, "path": path, "as": "operator", "service": "nocp"}
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def link(route, ko, en):
    return {"console": "ops", "route": route, "label_ko": ko, "label_en": en}


def _body(res):
    return res.body if isinstance(res.body, dict) else {}


def _save_setup(res, ctx):
    b = _body(res)
    ctx["tenant_id"], ctx["trays"] = b["tenant_id"], b["trays"]


def _setup(res, ctx):
    b = _body(res)
    return (res.status == 200 and b.get("policy", {}).get("mode") == "auto",
            f"데모 고객 {b.get('tenant_id')} · 트레이 {len(b.get('trays') or [])}개 · 정책 auto",
            f"demo tenant {b.get('tenant_id')} · {len(b.get('trays') or [])} trays · policy auto")


def _steps(run):
    return " → ".join(s["name"] for s in run.get("steps") or [])


def _recovered(res, ctx):
    run = _body(res).get("run") or {}
    return (run.get("outcome") == "recovered" and run.get("mttr_min") == 13.0,
            f"XID 48 → {_steps(run)} · 결과 {run.get('outcome')} · MTTR {run.get('mttr_min')}분",
            f"XID 48 → {_steps(run)} · outcome {run.get('outcome')} · MTTR {run.get('mttr_min')} min")


def _save_replaced(res, ctx):
    run = _body(res).get("run") or {}
    ctx["run_id"], ctx["rma_id"] = run.get("id"), run.get("rma_id")
    ctx["detected_at"], ctx["mttr2"] = run.get("detected_at"), run.get("mttr_min")
    ctx["restored_at"] = next((s["at"] for s in run.get("steps") or []
                               if s["name"] == "replace"), None)


def _replaced(res, ctx):
    run = _body(res).get("run") or {}
    rep = run.get("replacement") or {}
    q = next((s for s in run.get("steps") or [] if s["name"] == "queued"), None)
    wait_ko = f"랙 동시 1건 제한으로 {q['waited_for']} 뒤 대기 · " if q else ""
    wait_en = f"queued behind {q['waited_for']} (1 per rack) · " if q else ""
    return (run.get("outcome") == "replaced" and run.get("kpi_met") is True,
            f"영구 XID 79 → {wait_ko}리셋 실패 → 예비 교체 {rep.get('old_host')} → "
            f"{rep.get('new_host')} · MTTR {run.get('mttr_min')}분 (KPI ≤60분 충족) · "
            f"RMA {run.get('rma_id')}",
            f"persistent XID 79 → {wait_en}reset failed → spare {rep.get('old_host')} → "
            f"{rep.get('new_host')} · MTTR {run.get('mttr_min')} min (KPI ≤60 met) · "
            f"RMA {run.get('rma_id')}")


def _rma(res, ctx):
    b = _body(res)
    return (b.get("stage") == "pool_return",
            f"RMA {b.get('id')}: {' → '.join(s['stage'] for s in b.get('steps') or [])}",
            f"RMA {b.get('id')}: {' → '.join(s['stage'] for s in b.get('steps') or [])}")


def _report_ok(res, ctx):
    b = _body(res)
    return (b.get("violated") is False and b.get("downtime_min") == ctx["mttr2"],
            f"가용성 {b.get('availability_pct')}% (최악 노드 {b.get('downtime_min')}분) · 위반 없음",
            f"availability {b.get('availability_pct')}% (worst node {b.get('downtime_min')} min) · no violation")


def _exception(res, ctx):
    b = _body(res)
    return (res.status == 201, f"계획 점검 예외 {b.get('id')} 등록 ({b.get('contract_clause')})",
            f"maintenance exception {b.get('id')} registered")


def _excluded(res, ctx):
    b = _body(res)
    return (b.get("exceptions_minutes", 0) > 0 and b.get("downtime_min") < ctx["mttr2"],
            f"예외 구간 {b.get('exceptions_minutes')}분 제외 → 최악 노드 {b.get('downtime_min')}분",
            f"{b.get('exceptions_minutes')} exception minutes excluded → worst node "
            f"{b.get('downtime_min')} min")


def _save_credit(res, ctx):
    b = _body(res)
    ctx["credit_id"] = (b.get("credits") or [{}])[0].get("credit_id")


def _violated(res, ctx):
    b = _body(res)
    c = (b.get("credits") or [{}])[0]
    return (b.get("violated") is True and c.get("pct_of_monthly") == 10
            and b.get("violation_ticket_id"),
            f"가용성 {b.get('availability_pct')}% < 99.9% → 크레딧 {c.get('pct_of_monthly')}% "
            f"({c.get('credit_id')}, 청구서 반영 대기) · 티켓 {b.get('violation_ticket_id')}",
            f"availability {b.get('availability_pct')}% < 99.9% → credit {c.get('pct_of_monthly')}% "
            f"({c.get('credit_id')}, pending invoice) · ticket {b.get('violation_ticket_id')}")


def _no_dup(res, ctx):
    b = _body(res)
    ids = [c.get("credit_id") for c in b.get("credits") or []]
    return (ids == [ctx["credit_id"]],
            f"재계산 → 크레딧 {len(ids)}건 (중복 없음)", f"recalculated → {len(ids)} credit (no duplicate)")


def _kpi(res, ctx):
    b = _body(res)
    return (b.get("runs") == 2 and b.get("missed") == 0,
            f"자동 교체 KPI: {b.get('met')}/{b.get('runs')}건 ≤{b.get('target_min')}분 · "
            f"최대 MTTR {b.get('mttr_max_min')}분",
            f"auto-replace KPI: {b.get('met')}/{b.get('runs')} within {b.get('target_min')} min · "
            f"max MTTR {b.get('mttr_max_min')} min")


SCENARIO = {
    "id": "f10-f09-recovery-sla",
    "order": 10,
    "feature": "F10",
    "title_ko": "F10·F09 자동 복구 연쇄와 SLA 크레딧 (XID → 리셋/교체 → 가용성 → 크레딧)",
    "title_en": "F10·F09 auto-recovery chain and SLA credit (XID → reset/replace → availability → credit)",
    "summary_ko": "NVSentinel 순서(격리 → 드레인 → GPUReset/RebootNode)로 복구, 실패 시 예비 교체와 RMA, "
                  "다운타임은 SLA 엔진으로, 위반 시 크레딧·티켓.",
    "summary_en": "NVSentinel order (quarantine → drain → GPUReset/RebootNode); on failure spare "
                  "replacement and RMA; downtime feeds the SLA engine, violations yield credit + ticket.",
    "steps": [
        {"id": "00-setup", "title_ko": "데모 초기화 (정책 auto)", "title_en": "Reset demo (policy auto)",
         "call": http("POST", "/api/v1/remediation/demo/setup"), "expect": _setup,
         "save": _save_setup},
        {"id": "01-transient", "title_ko": "일시 XID 48 주입 → GPU 리셋으로 복구",
         "title_en": "Inject transient XID 48 → recovered by GPU reset",
         "narration_ko": "AI-Infra SEL 주입 + NICo gpu-health-monitor 보고 → HealthEvent → 자동 연쇄.",
         "narration_en": "AI-Infra SEL inject + NICo gpu-health-monitor report → HealthEvent → chain.",
         "call": lambda ctx: http("POST", "/api/v1/remediation/inject",
                                  {"machine_id": ctx["trays"][0], "xid": 48, "gpu": 3}),
         "expect": _recovered, "links": [link("rma", "운영 · RMA", "Ops · RMA")]},
        {"id": "02-persistent", "title_ko": "영구 XID 79 주입 → 리셋 실패 → 예비 교체",
         "title_en": "Inject persistent XID 79 → reset fails → spare replacement",
         "call": lambda ctx: http("POST", "/api/v1/remediation/inject",
                                  {"machine_id": ctx["trays"][1], "xid": 79, "persistent": True}),
         "expect": _replaced, "save": _save_replaced},
        {"id": "03-rma", "title_ko": "고장 노드 RMA (번인 → 풀 복귀)", "title_en": "RMA of the faulty node",
         "call": lambda ctx: http("GET", f"/api/v1/rma/{ctx['rma_id']}"), "expect": _rma},
        {"id": "04-report", "title_ko": "월 SLA 리포트", "title_en": "Monthly SLA report",
         "call": lambda ctx: http("GET", f"/api/v1/tenants/{ctx['tenant_id']}/sla-report"),
         "expect": _report_ok},
        {"id": "05-exception", "title_ko": "교체 구간을 계획 점검 예외로 등록",
         "title_en": "Register the replacement window as a maintenance exception",
         "call": lambda ctx: http("POST", "/api/v1/sla/exceptions", {
             "kind": "maintenance", "start": ctx["detected_at"], "end": ctx["restored_at"],
             "scope": {"tenants": [ctx["tenant_id"]]},
             "contract_clause": "SLA §예외: 사전 공지된 계획 점검"}),
         "expect": _exception},
        {"id": "06-excluded", "title_ko": "예외 구간 제외 확인", "title_en": "Exception minutes excluded",
         "call": lambda ctx: http("GET", f"/api/v1/tenants/{ctx['tenant_id']}/sla-report"),
         "expect": _excluded},
        {"id": "07-violation", "title_ko": "52.6분 장애 추가 → 위반 → 크레딧·티켓",
         "title_en": "Add a 52.6-min incident → violation → credit + ticket",
         "call": lambda ctx: http("GET", f"/api/v1/tenants/{ctx['tenant_id']}/sla-report",
                                  params={"synthesize_violation": "true"}),
         "expect": _violated, "save": _save_credit},
        {"id": "08-recalc", "title_ko": "재계산 → 크레딧 중복 없음", "title_en": "Recalculate → no duplicate",
         "call": lambda ctx: http("GET", f"/api/v1/tenants/{ctx['tenant_id']}/sla-report"),
         "expect": _no_dup},
        {"id": "09-kpi", "title_ko": "자동 교체 1시간 KPI", "title_en": "Auto-replace 1 h KPI",
         "call": lambda ctx: http("GET", "/api/v1/sla/kpi", params={"tenant_id": ctx["tenant_id"]}),
         "expect": _kpi},
    ],
}
