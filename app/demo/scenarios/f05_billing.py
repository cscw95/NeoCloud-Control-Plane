"""F05/F06 demo — contract → Lago events → invoice (KRW+VAT) → Stripe payment.

The signup → contract → invoice part of UPSTREAM-REFS §3.1 (steps 7–10) on
the upstream-shaped surfaces: Lago `/lago/api/v1/*` (customer, subscription
from the F06 contract, events/batch, invoice, finalize) and Stripe
`/stripe/v1/*` (PaymentIntent, test mode), plus the signed webhooks NOCP
received. Deterministic: last calendar month, fixed transaction ids, the
tenant's billing state is reset first (POST /api/v1/billing/demo:reset), so
every run ends at the same numbers:

  input 500M tok × ₩800/1M = ₩400,000 · output 250M × ₩3,200/1M = ₩800,000
  cached 100M × ₩200/1M = ₩20,000 → ₩1,220,000 ≥ commitment ₩1,000,000
  VAT 10% (won floor) ₩122,000 → total ₩1,342,000

Format: the demo runner (app/demo/engine.py, feat/v2-demo). Without the
runner this file still works: scripts/demo_billing.py runs the same SCENARIO
over HTTP with run_local().
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

try:                                      # demo runner (v2-demo)
    from ..engine import check, http, won
except ImportError:                       # runner not merged yet: same helpers
    def http(method, path, json=None, params=None, as_="operator",
             service="nocp", headers=None):
        spec = {"method": method, "path": path, "as": as_, "service": service}
        if headers is not None:
            spec["headers"] = headers
        if json is not None:
            spec["json"] = json
        if params is not None:
            spec["params"] = params
        return spec

    def check(cond, ko, en):
        return bool(cond), ko, en

    def won(v):
        try:
            return f"₩{int(v):,}"
        except (TypeError, ValueError):
            return "₩—"

TENANT = "f05-demo-corp"
TOTAL = 1_342_000


def _prepare(ctx):
    now = datetime.now(timezone.utc)
    p0 = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
          - timedelta(days=1)).replace(day=1)
    ctx.update(bill_period=p0.strftime("%Y-%m"), start_date=p0.date().isoformat(),
               event_ts=int((p0 + timedelta(days=3, hours=9)).timestamp()))
    return {"period": ctx["bill_period"], "contract_start": ctx["start_date"],
            "event_timestamp": ctx["event_ts"]}


def _events(ctx):
    sub = ctx["contract_id"]
    ev = [("f05-req-0001:input", "input_tokens", 500_000_000),
          ("f05-req-0001:output", "output_tokens", 250_000_000),
          ("f05-req-0002:cached", "cached_tokens", 100_000_000)]
    return {"events": [{"transaction_id": tx, "external_subscription_id": sub,
                        "code": code, "timestamp": ctx["event_ts"],
                        "properties": {"tokens": n, "model": "llama-3.3-70b"}}
                       for tx, code, n in ev]}


def _b(res):
    return res.body if isinstance(res.body, dict) else {}


def _save(key, *path):
    def save(res, ctx):
        v = res.body
        for p in path:
            v = v[p]
        ctx[key] = v
    return save


def _sv_tenant(res, ctx):
    ctx["tenant_id"] = _b(res).get("id") or f"tnt-{TENANT}"


def _lago_customer(res, ctx):
    c = _b(res).get("customer") or {}
    pp = (c.get("billing_configuration") or {}).get("provider_customer_id")
    return check(res.status == 200 and c.get("external_id") == ctx["tenant_id"],
                 f"Lago 고객 {c.get('lago_id', '')[:8]}… = 테넌트 {c.get('external_id')}"
                 f" · 통화 {c.get('currency')} · Stripe 고객 {pp}",
                 f"Lago customer {c.get('lago_id', '')[:8]}… = tenant "
                 f"{c.get('external_id')} · {c.get('currency')} · Stripe {pp}")


def _subscription(res, ctx):
    s = _b(res).get("subscription") or {}
    return check(s.get("status") == "active" and s.get("plan_code", "").startswith(
        "neocloud_contract_"),
        f"Lago 구독 {s.get('external_id')} {s.get('status')} · 요금제 {s.get('plan_code')}",
        f"Lago subscription {s.get('external_id')} {s.get('status')} · plan "
        f"{s.get('plan_code')}")


def _plan(res, ctx):
    p = _b(res).get("plan") or {}
    mc = (p.get("minimum_commitment") or {}).get("amount_cents")
    return check(mc == 1_000_000,
                 f"요금제 최소 약정 minimum_commitment {won(mc)}/월 · 과금 항목 "
                 f"{len(p.get('charges') or [])}개",
                 f"plan minimum_commitment {won(mc)}/month · "
                 f"{len(p.get('charges') or [])} charges")


def _batch(res, ctx):
    evs = _b(res).get("events") or []
    return check(res.status == 200 and len(evs) == 3,
                 f"이벤트 {len(evs)}건 수집 (transaction_id 기준 멱등)",
                 f"{len(evs)} events accepted (idempotent by transaction_id)")


def _resend(res, ctx):
    return check(res.status == 200, "같은 배치 재전송 → 200, 중복 과금 없음 (다음 단계 금액으로 확인)",
                 "same batch resent → 200, not billed twice (see the amounts next)")


def _run(res, ctx):
    ids = _b(res).get("created") or []
    return check(len(ids) == 1, f"{ctx['bill_period']} 청구 초안 {ids} · 웹훅 invoice.drafted",
                 f"{ctx['bill_period']} draft {ids} · webhook invoice.drafted")


def _lago_invoice(res, ctx):
    i = _b(res).get("invoice") or {}
    fees = {f["item"]["code"]: f["amount_cents"] for f in i.get("fees") or []}
    ok = i.get("total_amount_cents") == TOTAL and i.get("status") == "draft"
    return check(ok,
                 "Lago 청구서 fees " + ", ".join(f"{k} {won(v)}" for k, v in fees.items())
                 + f" · 공급가 {won(i.get('sub_total_excluding_taxes_amount_cents'))}"
                 f" + VAT {won(i.get('taxes_amount_cents'))} = {won(i.get('total_amount_cents'))}",
                 "Lago invoice fees " + ", ".join(f"{k} {won(v)}" for k, v in fees.items())
                 + f" · {won(i.get('sub_total_excluding_taxes_amount_cents'))} + VAT "
                 f"{won(i.get('taxes_amount_cents'))} = {won(i.get('total_amount_cents'))}")


def _finalized(res, ctx):
    i = _b(res).get("invoice") or {}
    return check(i.get("status") == "finalized",
                 f"Lago PUT finalize → {i.get('status')} · 결제 상태 {i.get('payment_status')}"
                 " · 웹훅 invoice.created",
                 f"Lago PUT finalize → {i.get('status')} · payment {i.get('payment_status')}"
                 " · webhook invoice.created")


def _tax(res, ctx):
    b = _b(res)
    return check(b.get("approval_no") and b.get("total") == TOTAL,
                 f"전자세금계산서(모사) {b.get('approval_no')} · 합계 {won(b.get('total'))}",
                 f"e-tax invoice (emulated) {b.get('approval_no')} · {won(b.get('total'))}")


def _paid(res, ctx):
    b = _b(res)
    p = b.get("payment") or {}
    return check(b.get("status") == "paid" and p.get("status") == "succeeded",
                 f"Stripe PaymentIntent {p.get('id')} {p.get('status')} · {won(p.get('amount'))}",
                 f"Stripe PaymentIntent {p.get('id')} {p.get('status')} · "
                 f"{won(p.get('amount'))}")


def _pi(res, ctx):
    b = _b(res)
    return check(b.get("object") == "payment_intent" and b.get("amount_received") == TOTAL
                 and b.get("livemode") is False,
                 f"Stripe 원문: status={b.get('status')} amount_received={b.get('amount_received')}"
                 f" currency={b.get('currency')} livemode={b.get('livemode')}",
                 f"Stripe raw: status={b.get('status')} amount_received="
                 f"{b.get('amount_received')} currency={b.get('currency')} "
                 f"livemode={b.get('livemode')}")


WANT_HOOKS = ["customer.created", "subscription.started", "invoice.drafted",
              "invoice.created", "payment_intent.succeeded",
              "invoice.payment_status_updated"]


def _hooks(res, ctx):
    recs = res.body if isinstance(res.body, list) else []
    types = [r["type"] for r in recs]
    verified = all(d.get("verified") for r in recs for d in r.get("deliveries", []))
    missing = [t for t in WANT_HOOKS if t not in types]
    return check(not missing and verified,
                 f"서명 검증된 웹훅 {len(recs)}건: {', '.join(types)}"
                 + (f" · 누락 {missing}" if missing else ""),
                 f"{len(recs)} signed webhooks verified: {', '.join(types)}"
                 + (f" · missing {missing}" if missing else ""))


SCENARIO = {
    "id": "f05-billing-lago-stripe",
    "order": 20,
    "feature": "F05/F06",
    "title_ko": "F05·F06 약정 → Lago 이벤트 → 청구서(KRW+VAT) → Stripe 결제",
    "title_en": "F05/F06 contract → Lago events → invoice (KRW+VAT) → Stripe payment",
    "summary_ko": "약정 계약이 Lago 요금제·구독이 되고, 토큰 사용 이벤트가 청구서가 되어 "
                  "Stripe 테스트 결제까지 가는 흐름. 원본 형식(Lago·Stripe)의 요청·응답과 "
                  "서명된 웹훅을 그대로 보여 준다.",
    "summary_en": "A reserved contract becomes a Lago plan and subscription, token "
                  "usage events become an invoice, paid by a Stripe test PaymentIntent; "
                  "upstream-shaped requests, responses and signed webhooks are shown.",
    "steps": [
        {"id": "00-prepare", "title_ko": "기간 준비(지난달)", "title_en": "Pick the period (last month)",
         "call": _prepare, "expect": lambda r, c: check(True, f"청구 기간 {c['bill_period']}",
                                                        f"billing period {c['bill_period']}")},
        {"id": "01-tenant", "title_ko": "테넌트", "title_en": "Tenant",
         "call": http("POST", "/api/v1/tenants",
                      {"name": TENANT, "isolation_tier": "bare_metal_dedicated"}),
         "expect": {"status": [201, 409]}, "save": _sv_tenant},
        {"id": "02-reset", "feature": "F05", "title_ko": "이 테넌트 청구 상태 초기화",
         "title_en": "Reset this tenant's billing state",
         "requires": ["POST /api/v1/billing/demo:reset"],
         "call": http("POST", "/api/v1/billing/demo:reset",
                      lambda c: {"tenant_id": c["tenant_id"]})},
        {"id": "03-lago-customer", "feature": "F05", "title_ko": "Lago 고객 등록",
         "title_en": "Lago customer",
         "narration_ko": "NOCP → Lago POST /api/v1/customers. 웹훅 customer.created.",
         "narration_en": "NOCP → Lago POST /api/v1/customers; webhook customer.created.",
         "call": http("POST", "/lago/api/v1/customers", lambda c: {"customer": {
             "external_id": c["tenant_id"], "name": "F05 Demo Corp", "currency": "KRW",
             "legal_number": "214-86-00005",
             "billing_configuration": {"payment_provider": "stripe"}}}),
         "expect": _lago_customer},
        {"id": "04-contract", "feature": "F06", "title_ko": "토큰 API 약정 계약(월 ₩1,000,000)",
         "title_en": "Token API commitment (₩1,000,000/month)",
         "call": http("POST", "/api/v1/contracts", lambda c: {
             "tenant_id": c["tenant_id"], "product": "token-api", "term_months": 12,
             "start_date": c["start_date"], "unit_price": 0,
             "min_monthly_amount": 1_000_000}),
         "expect": {"status": 201}, "save": _save("contract_id", "id")},
        {"id": "05-activate", "feature": "F06", "title_ko": "계약 활성 → Lago 구독",
         "title_en": "Activate → Lago subscription",
         "call": http("POST", lambda c: f"/api/v1/contracts/{c['contract_id']}/activate"),
         "expect": lambda r, c: check(_b(r).get("status") == "active", "계약 active",
                                      "contract active")},
        {"id": "06-subscription", "feature": "F06", "title_ko": "Lago 구독 확인",
         "title_en": "Lago subscription",
         "call": http("GET", lambda c: f"/lago/api/v1/subscriptions/{c['contract_id']}"),
         "expect": _subscription, "save": _save("plan_code", "subscription", "plan_code")},
        {"id": "07-plan", "feature": "F06", "title_ko": "Lago 요금제(minimum_commitment)",
         "title_en": "Lago plan (minimum_commitment)",
         "call": http("GET", lambda c: f"/lago/api/v1/plans/{c['plan_code']}"),
         "expect": _plan},
        {"id": "08-events", "feature": "F05", "title_ko": "사용 이벤트 배치(Lago events/batch)",
         "title_en": "Usage events (Lago events/batch)",
         "narration_ko": "code = billable metric code, properties.tokens 합산(sum_agg).",
         "narration_en": "code = billable metric code; properties.tokens summed (sum_agg).",
         "call": http("POST", "/lago/api/v1/events/batch", _events), "expect": _batch},
        {"id": "09-resend", "feature": "F05", "title_ko": "같은 배치 재전송(멱등)",
         "title_en": "Resend the same batch (idempotent)",
         "call": http("POST", "/lago/api/v1/events/batch", _events), "expect": _resend},
        {"id": "10-run", "feature": "F05", "title_ko": "월 청구 실행(파사드)",
         "title_en": "Monthly run (facade)",
         "call": http("POST", "/api/v1/billing/invoices:run",
                      lambda c: {"period": c["bill_period"], "tenant_id": c["tenant_id"]}),
         "expect": _run, "save": _save("invoice_id", "created", 0)},
        {"id": "11-lago-invoice", "feature": "F05", "title_ko": "Lago 형식 청구서",
         "title_en": "Invoice in Lago shape",
         "call": http("GET", lambda c: f"/lago/api/v1/invoices/{c['invoice_id']}"),
         "expect": _lago_invoice, "save": _save("lago_invoice_id", "invoice", "lago_id")},
        {"id": "12-finalize", "feature": "F05", "title_ko": "Lago finalize",
         "title_en": "Lago finalize",
         "call": http("PUT", lambda c: f"/lago/api/v1/invoices/{c['lago_invoice_id']}/finalize"),
         "expect": _finalized},
        {"id": "13-tax-invoice", "feature": "F05", "title_ko": "세금계산서(모사)",
         "title_en": "Tax invoice (emulated)",
         "call": http("GET", lambda c: f"/api/v1/billing/invoices/{c['invoice_id']}/tax-invoice"),
         "expect": _tax},
        {"id": "14-card", "feature": "F05", "title_ko": "결제 수단(Stripe 테스트 카드)",
         "title_en": "Payment method (Stripe test card)",
         "call": http("POST", "/api/v1/billing/payment-methods", lambda c: {
             "tenant_id": c["tenant_id"], "kind": "card", "token": "tok_visa_4242"}),
         "expect": {"status": 201}},
        {"id": "15-pay", "feature": "F05", "title_ko": "결제(PaymentIntent confirm=true)",
         "title_en": "Pay (PaymentIntent confirm=true)",
         "call": http("POST", lambda c: f"/api/v1/billing/invoices/{c['invoice_id']}/pay"),
         "expect": _paid, "save": _save("payment_intent", "payment", "id")},
        {"id": "16-stripe-pi", "feature": "F05", "title_ko": "Stripe PaymentIntent 원문",
         "title_en": "Stripe PaymentIntent (raw)",
         "call": http("GET", lambda c: f"/stripe/v1/payment_intents/{c['payment_intent']}"),
         "expect": _pi},
        {"id": "17-webhooks", "feature": "F05", "title_ko": "받은 웹훅(서명 검증)",
         "title_en": "Webhooks received (signatures verified)",
         "narration_ko": "Lago: X-Lago-Signature(HMAC-SHA256, base64) · Stripe: Stripe-Signature t=,v1=.",
         "narration_en": "Lago: X-Lago-Signature (HMAC-SHA256, base64) · Stripe: Stripe-Signature t=,v1=.",
         "call": http("GET", "/api/v1/billing/webhooks",
                      params=lambda c: {"tenant_id": c["tenant_id"]}),
         "expect": _hooks},
    ],
}


# ---------------------------------------------------------------------------
# stand-alone runner (scripts/demo_billing.py, tests) — no demo engine needed
# ---------------------------------------------------------------------------
class _Res:
    def __init__(self, status, body, headers=None):
        self.status, self.body, self.headers = status, body, headers or {}


def _resolve(v, ctx):
    if callable(v):
        return v(ctx)
    if isinstance(v, dict):
        return {k: _resolve(x, ctx) for k, x in v.items()}
    return v


def _judge(expect, res, ctx):
    if expect is None:
        return res.status < 400, f"HTTP {res.status}", f"HTTP {res.status}"
    if isinstance(expect, dict):
        want = expect["status"]
        want = want if isinstance(want, list) else [want]
        return res.status in want, f"HTTP {res.status}", f"HTTP {res.status}"
    out = expect(res, ctx)
    return out if isinstance(out, tuple) else (bool(out), "", "")


def run_local(send, out=print, lang="ko") -> bool:
    """Run SCENARIO with send(method, path, json, params) -> (status, headers,
    body). Prints a curl line, a response summary and the verdict per step."""
    ctx: dict = {}
    ok_all = True
    for i, st in enumerate(SCENARIO["steps"], 1):
        title = st["title_ko" if lang == "ko" else "title_en"]
        out(f"\n[{i:02d}/{len(SCENARIO['steps'])}] {title}")
        call = st["call"]
        if callable(call):
            res = _Res(200, call(ctx))
            out(f"  (local) {json.dumps(res.body, ensure_ascii=False)}")
        else:
            spec = _resolve(call, ctx)
            m, path = spec["method"], spec["path"]
            body, params = spec.get("json"), spec.get("params")
            q = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
            out(f"  $ curl -s -X {m} '$NOCP{path}{q}'"
                + (f" -H 'Content-Type: application/json' -d '"
                   f"{json.dumps(body, ensure_ascii=False)}'" if body is not None else ""))
            status, headers, rbody = send(m, path, body, params)
            res = _Res(status, rbody, headers)
            text = json.dumps(rbody, ensure_ascii=False)
            out(f"  ← HTTP {status} {text[:300]}{'…' if len(text) > 300 else ''}")
        try:
            ok, ko, en = _judge(st.get("expect"), res, ctx)
        except (KeyError, TypeError, IndexError) as e:
            ok, ko, en = False, f"검증 오류 {e!r}", f"check error {e!r}"
        out(f"  {'✔' if ok else '✘'} {ko if lang == 'ko' else en}")
        if ok and st.get("save"):
            st["save"](res, ctx)
        ok_all &= bool(ok)
        if not ok:
            break
    return ok_all
