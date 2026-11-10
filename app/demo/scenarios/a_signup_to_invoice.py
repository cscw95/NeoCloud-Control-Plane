"""(a) Phase-1 DoD chain a / US-01·02·06 — signup → contract → 4 reserved NVL72
racks → acceptance → KRW invoice with VAT and a tax invoice.

Contracts: F07 (signup, OIDC), F06 (reserved contract), F05 (price book, invoices).
Before F07 lands the tenant is made with POST /api/v1/tenants (fallback); before F06
the order carries racks/blueprint itself.
"""

import math

from ..engine import check, http, won
from ._common import link

ORG = "demo-a-bio"


def _signup_ok(res, ctx):
    st = res.body.get("status")
    return check(res.status in (200, 201) and st == "pending_verification",
                 f"가입 접수 → {res.body.get('tenant_id')} · {st}",
                 f"signup received → {res.body.get('tenant_id')} · {st}")


def _save_tenant(res, ctx):
    ctx["tenant_id"] = res.body.get("tenant_id") or res.body["id"]


def _verified(res, ctx):
    st = res.body.get("status")
    return check(st == "active", f"사업자 확인 완료 → {st}", f"business verified → {st}")


def _contract_ok(res, ctx):
    return check(res.status == 201 and res.body.get("status") == "draft",
                 f"계약 {res.body.get('id')} 초안 · 4랙 · 12개월",
                 f"contract {res.body.get('id')} draft · 4 racks · 12 months")


def _active(res, ctx):
    return check(res.body.get("status") == "active", "계약 활성화 (status=active)",
                 "contract active")


def _order_body(ctx):
    if ctx.get("contract_id"):
        return {"tenant_id": ctx["tenant_id"], "kind": "new", "contract_id": ctx["contract_id"]}
    return {"tenant_id": ctx["tenant_id"], "kind": "new", "blueprint_key": "vr-nvl72", "racks": 4}


def _order_ok(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    nodes = b.get("node_ids") or []
    racks = sorted({n.removeprefix("ni-").rsplit("-tray-", 1)[0] for n in nodes})
    sus = {r.rsplit("-rack-", 1)[0] for r in racks}
    ok = b.get("state") == "delivered" and len(racks) == 4 and len(sus) == 1
    via = f"계약 {ctx['contract_id']}" if ctx.get("contract_id") else "계약 없이(F06 대기)"
    return check(ok, f"{via} · 4랙 {len(nodes)}노드 인도 · 같은 SU/IB 구역 {', '.join(sus)}",
                 f"{'contract ' + ctx['contract_id'] if ctx.get('contract_id') else 'no contract (F06 pending)'}"
                 f" · 4 racks {len(nodes)} nodes delivered · one SU/IB zone {', '.join(sus)}")


def _save_order(res, ctx):
    ctx["order_id"] = res.body["id"]


def _approved(res, ctx):
    st = res.body.get("status")
    return check(st in ("approved", "accepted"), f"인수 승인 → {st} (과금 시작)",
                 f"acceptance → {st} (billing starts)")


def _run_ok(res, ctx):
    inv = [i for i in res.body.get("invoices", []) if i.get("tenant_id") == ctx["tenant_id"]]
    return check(inv, f"{ctx['period']} 청구 초안 {len(inv)}건 생성",
                 f"{len(inv)} draft invoice(s) for {ctx['period']}")


def _save_invoice(res, ctx):
    ctx["invoice_id"] = [i for i in res.body["invoices"]
                         if i["tenant_id"] == ctx["tenant_id"]][0]["id"]


def _invoice_ok(res, ctx):
    b = res.body
    vat_ok = b.get("vat") == math.floor((b.get("subtotal") or 0) * 0.1)
    total_ok = b.get("total") == (b.get("subtotal") or 0) + (b.get("vat") or 0)
    ok = b.get("currency") == "KRW" and vat_ok and total_ok and b.get("lines")
    return check(ok, f"공급가 {won(b.get('subtotal'))} + 부가세 {won(b.get('vat'))} = 합계 {won(b.get('total'))} "
                     f"(라인 {len(b.get('lines') or [])}개, 원 미만 절사)",
                 f"subtotal {won(b.get('subtotal'))} + VAT {won(b.get('vat'))} = total {won(b.get('total'))} "
                 f"({len(b.get('lines') or [])} lines, VAT floored)")


def _tax_ok(res, ctx):
    b = res.body
    return check(b.get("approval_no") and b.get("tax_amount") is not None,
                 f"전자세금계산서(모사) 승인번호 {b.get('approval_no')} · 공급받는자 {b.get('buyer', {}).get('business_reg_no')}",
                 f"e-tax invoice (emulated) approval {b.get('approval_no')} · buyer {b.get('buyer', {}).get('business_reg_no')}")


SCENARIO = {
    "id": "a-signup-to-invoice",
    "order": 1,
    "feature": "F07/F06/F05",
    "story": "US-01 · US-02 · US-06",
    "title_ko": "(a) 가입 → 계약 → 4랙 예약 → 인수 → 청구서(KRW+VAT)",
    "title_en": "(a) Signup → contract → 4 reserved racks → acceptance → invoice (KRW+VAT)",
    "summary_ko": "기업 고객이 웹 가입부터 첫 원화 청구서·세금계산서를 받기까지 (Phase 1 DoD 체인 a).",
    "summary_en": "An enterprise goes from web signup to its first KRW invoice and tax invoice (Phase 1 DoD chain a).",
    "steps": [
        {"id": "01-signup", "feature": "F07", "requires": ["POST /api/v1/signup"],
         "title_ko": "셀프 가입", "title_en": "Self signup",
         "narration_ko": "사업자번호·관리자 이메일로 가입하면 테넌트가 '확인 대기'로 생긴다.",
         "narration_en": "Signing up with a business number creates the tenant as pending verification.",
         "expect_ko": "status = pending_verification", "expect_en": "status = pending_verification",
         "call": http("POST", "/api/v1/signup",
                      {"org_name": ORG, "business_reg_no": "214-86-00001",
                       "admin_email": "admin@demo-a-bio.example", "admin_name": "Demo Admin",
                       "public_sector": False}, as_="none"),
         "fallback": http("POST", "/api/v1/tenants",
                          {"name": ORG, "isolation_tier": "bare_metal_dedicated"}),
         "expect": _signup_ok, "save": _save_tenant,
         "links": [link("customer", "dashboard", "고객 콘솔", "Customer console")]},
        {"id": "02-verify", "feature": "F07", "requires": ["POST /api/v1/signup/{tid}/verify"],
         "title_ko": "운영자 사업자 확인", "title_en": "Operator verifies the business",
         "narration_ko": "운영자가 확인하면 realm·관리자가 활성화되고 주문이 허용된다(미확인 주문은 403).",
         "narration_en": "Once verified, the realm and admin go active and orders are allowed (403 before).",
         "call": http("POST", lambda c: f"/api/v1/signup/{c['tenant_id']}/verify"),
         "expect": _verified},
        {"id": "03-contract", "feature": "F06", "requires": ["POST /api/v1/contracts"],
         "title_ko": "4랙 12개월 예약 계약 등록", "title_en": "Register a 4-rack 12-month reserved contract",
         "narration_ko": "영업이 같은 IB 구역 4랙, 12개월, 랙당 단가로 약정 계약을 만든다.",
         "narration_en": "Sales books 4 racks in one IB zone for 12 months at a reserved rack price.",
         "call": http("POST", "/api/v1/contracts",
                      lambda c: {"tenant_id": c["tenant_id"], "product": "bmaas",
                                 "commitment": "reserved", "racks": 4,
                                 "blueprint_key": "vr-nvl72", "term_months": 12,
                                 "start_date": c["period"] + "-01", "unit_price": 95_000_000,
                                 "placement": {"same_ib_zone": True}, "sla_tier": "premium"}),
         "expect": _contract_ok, "save": lambda r, c: c.__setitem__("contract_id", r.body["id"]),
         "links": [link("biz", "contracts", "사업 · 계약", "Business · contracts")]},
        {"id": "04-activate", "feature": "F06", "requires": ["POST /api/v1/contracts/{id}/activate"],
         "title_ko": "계약 활성화", "title_en": "Activate the contract",
         "call": http("POST", lambda c: f"/api/v1/contracts/{c['contract_id']}/activate"),
         "expect": _active},
        {"id": "05-order", "title_ko": "계약으로 4랙 주문", "title_en": "Order 4 racks on the contract",
         "narration_ko": "주문에 contract_id만 넣으면 랙 수·배치 제약(같은 IB 구역)을 계약에서 가져온다.",
         "narration_en": "With contract_id the order takes rack count and placement (one IB zone) from it.",
         "expect_ko": "delivered · 4랙 72노드 · 한 SU", "expect_en": "delivered · 4 racks 72 nodes · one SU",
         "call": http("POST", "/api/v1/orders", _order_body),
         "expect": _order_ok, "save": _save_order,
         "links": [link("ops", "provisioning", "운영 · 프로비저닝", "Ops · provisioning")]},
        {"id": "06-acceptance", "title_ko": "고객 인수 승인", "title_en": "Customer acceptance",
         "narration_ko": "인수 보고서(NCCL·fio·burn-in)를 보고 고객 관리자가 승인한다.",
         "narration_en": "The customer admin approves after reading the NCCL/fio/burn-in report.",
         "call": http("POST", lambda c: f"/api/v1/orders/{c['order_id']}/acceptance",
                      {"decision": "approve"}, as_=lambda c: f"tenant:{c['tenant_id']}"),
         "expect": _approved},
        {"id": "07-invoice-run", "feature": "F05", "requires": ["POST /api/v1/billing/invoices:run"],
         "title_ko": "월 청구 실행", "title_en": "Run monthly billing",
         "narration_ko": "요율표로 랙-시간·약정 true-up·SLA 크레딧을 계산해 테넌트별 초안을 만든다(멱등).",
         "narration_en": "The price book rates rack-hours, true-up and SLA credits into drafts (idempotent).",
         "call": http("POST", "/api/v1/billing/invoices:run",
                      lambda c: {"period": c["period"], "tenant_id": c["tenant_id"]}),
         "expect": _run_ok, "save": _save_invoice,
         "links": [link("customer", "billing", "고객 · 청구", "Customer · billing")]},
        {"id": "08-invoice", "feature": "F05", "requires": ["GET /api/v1/billing/invoices/{id}"],
         "title_ko": "원화 청구서 확인", "title_en": "Read the KRW invoice",
         "expect_ko": "KRW · VAT = floor(공급가×10%) · 합계 = 공급가+VAT",
         "expect_en": "KRW · VAT = floor(subtotal×10%) · total = subtotal+VAT",
         "call": http("GET", lambda c: f"/api/v1/billing/invoices/{c['invoice_id']}"),
         "expect": _invoice_ok},
        {"id": "09-finalize", "feature": "F05",
         "requires": ["POST /api/v1/billing/invoices/{id}/finalize"],
         "title_ko": "청구서 확정", "title_en": "Finalize the invoice",
         "narration_ko": "확정하면 수정이 막히고 SLA 크레딧이 이 청구서에 적용되며 세금계산서가 발행된다.",
         "narration_en": "Finalizing locks the invoice, applies SLA credits to it and issues the tax invoice.",
         "call": http("POST", lambda c: f"/api/v1/billing/invoices/{c['invoice_id']}/finalize"),
         "expect": lambda r, c: check(r.body.get("status") == "finalized",
                                      f"상태 {r.body.get('status')}", f"status {r.body.get('status')}")},
        {"id": "10-tax-invoice", "feature": "F05",
         "requires": ["GET /api/v1/billing/invoices/{id}/tax-invoice"],
         "title_ko": "전자세금계산서(모사)", "title_en": "E-tax invoice (emulated)",
         "call": http("GET", lambda c: f"/api/v1/billing/invoices/{c['invoice_id']}/tax-invoice"),
         "expect": _tax_ok,
         "links": [link("biz", "revenue", "사업 · 매출", "Business · revenue")]},
        {"id": "11-payment-method", "feature": "F05", "requires": ["POST /api/v1/billing/payment-methods"],
         "title_ko": "결제수단 등록 (Stripe 테스트 모드 모사)", "title_en": "Add a payment method (Stripe test-mode style)",
         "call": http("POST", "/api/v1/billing/payment-methods",
                      lambda c: {"tenant_id": c["tenant_id"], "kind": "card", "token": "tok_visa_4242"}),
         "expect": {"status": [200, 201]}},
        {"id": "12-pay", "feature": "F05", "requires": ["POST /api/v1/billing/invoices/{id}/pay"],
         "title_ko": "결제", "title_en": "Pay",
         "call": http("POST", lambda c: f"/api/v1/billing/invoices/{c['invoice_id']}/pay"),
         "expect": lambda r, c: check(r.body.get("status") == "paid",
                                      f"청구서 {r.body.get('status')} · 결제 {(r.body.get('payment') or {}).get('status')}",
                                      f"invoice {r.body.get('status')} · payment {(r.body.get('payment') or {}).get('status')}")},
    ],
}
