"""F05 Stripe API emulator (test mode) — customers, payment methods, PaymentIntents.

Upstream: stripe/openapi@f1ec696a9d04 latest/openapi.spec3.yaml (MIT; URL only,
7.9 MB). Mounted at `/stripe/v1/...` so a client whose base URL is
`http://<nocp>/stripe` sends the same paths as to api.stripe.com:
  POST /v1/customers · GET /v1/customers/{customer}
  POST /v1/payment_methods · POST /v1/payment_methods/{pm}/attach
  POST /v1/payment_intents · POST /v1/payment_intents/{intent}/confirm
  GET  /v1/payment_intents/{intent} · GET /v1/invoices/{invoice}
  GET  /v1/events · GET /v1/events/{id} · POST|GET /v1/webhook_endpoints
Bodies are form-encoded like Stripe (`metadata[k]=v`, `expand[]=x`); JSON is
accepted too. Errors use the Stripe envelope `{error:{type, code, message,
param, decline_code, payment_intent}}` with 400/402/404.

PaymentIntent flow (Stripe docs): create → requires_payment_method |
requires_confirmation; confirm with a card → succeeded (amount_received =
amount) or, on decline, back to requires_payment_method with
last_payment_error. Events: payment_intent.created, payment_intent.succeeded,
payment_intent.payment_failed, customer.created.

Test mode only (livemode=false). Test payment methods follow Stripe's test
docs: pm_card_visa / tok_visa succeed, pm_card_chargeDeclined /
tok_chargeDeclined decline (card_declined / generic_decline). NeoCloud's own
facade tokens starting with `tok_fail` decline too.

Auth: Stripe uses `Authorization: Bearer sk_test_...`; on NOCP the
Authorization header belongs to the tenancy guard (auth.py), so this surface
takes the NOCP operator identity instead (NeoCloud deviation).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import billing_webhooks as wh
from .store import STORE

router = APIRouter(prefix="/stripe/v1", tags=["stripe-emulator"])

DECLINE_TOKENS = ("pm_card_chargeDeclined", "tok_chargeDeclined", "tok_fail")
ZERO_DECIMAL = ("krw", "jpy")


class StripeError(Exception):
    def __init__(self, status: int, type_: str, message: str, code=None,
                 param=None, **extra):
        self.status = status
        err = {"type": type_, "code": code, "message": message, "param": param,
               **extra}
        self.body = {"error": {k: v for k, v in err.items() if v is not None}}


def _tbl(name: str) -> dict:
    return STORE.stripe.setdefault(name, {})


def _ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _id(prefix: str, kind: str) -> str:
    return f"{prefix}_{STORE.next_seq('stripe_' + kind):014d}"


def _err(e: StripeError) -> JSONResponse:
    return JSONResponse(e.body, status_code=e.status)


def _identity_ok(request: Request) -> bool:
    ident = getattr(request.state, "identity", None)
    return ident is None or ident.role == "operator"


async def _params(request: Request) -> dict:
    """Stripe form encoding (a[b]=c, a[]=x) or JSON → nested dict."""
    ctype = request.headers.get("content-type", "")
    raw = await request.body()
    if not raw:
        return {}
    if "json" in ctype:
        import json
        return json.loads(raw)
    out: dict = {}
    for k, v in parse_qsl(raw.decode(), keep_blank_values=True):
        keys = k.replace("]", "").split("[")
        cur = out
        for i, part in enumerate(keys):
            last = i == len(keys) - 1
            if last:
                if part == "":
                    pass
                elif isinstance(cur, dict):
                    cur[part] = v
                continue
            nxt_list = keys[i + 1] == ""
            if nxt_list:
                cur.setdefault(part, []).append(v)
                break
            cur = cur.setdefault(part, {})
    return out


def _bool(v) -> bool:
    return v is True or str(v).lower() == "true"


# ---------------------------------------------------------------------------
# objects (internal API used by the billing facade too)
# ---------------------------------------------------------------------------
def create_customer(email=None, name=None, metadata=None) -> dict:
    cus = {"id": _id("cus", "customer"), "object": "customer", "address": None,
           "balance": 0, "created": _ts(), "currency": None,
           "default_source": None, "delinquent": False, "description": None,
           "discount": None, "email": email, "invoice_prefix": None,
           "invoice_settings": {"custom_fields": None,
                                "default_payment_method": None, "footer": None,
                                "rendering_options": None},
           "livemode": False, "metadata": metadata or {}, "name": name,
           "phone": None, "preferred_locales": [], "shipping": None,
           "tax_exempt": "none", "test_clock": None}
    _tbl("customers")[cus["id"]] = cus
    wh.emit_stripe("customer.created", cus, stripe_customer=cus["id"])
    return cus


def customer_for(external_id: str, name=None) -> dict:
    """One Stripe customer per NeoCloud tenant (metadata.tenant_id)."""
    for c in _tbl("customers").values():
        if c["metadata"].get("tenant_id") == external_id:
            return c
    return create_customer(name=name, metadata={"tenant_id": external_id})


def create_payment_method(token: str, type_: str = "card",
                          customer: Optional[str] = None) -> dict:
    digits = "".join(ch for ch in token if ch.isdigit())
    last4 = digits[-4:] if digits else ("0002" if _declines(token) else "4242")
    pm = {"id": _id("pm", "payment_method"), "object": "payment_method",
          "allow_redisplay": "unspecified",
          "billing_details": {"address": None, "email": None, "name": None,
                              "phone": None, "tax_id": None},
          "created": _ts(), "customer": customer, "livemode": False,
          "metadata": {"test_token": token}, "type": type_}
    if type_ == "card":
        pm["card"] = {"brand": "visa", "country": "KR", "exp_month": 12,
                      "exp_year": 2030, "funding": "credit", "last4": last4}
    _tbl("payment_methods")[pm["id"]] = pm
    return pm


def _declines(token: str) -> bool:
    return any(token.startswith(t) for t in DECLINE_TOKENS)


def _resolve_pm(pm_ref: Optional[str]) -> Optional[dict]:
    if not pm_ref:
        return None
    pm = _tbl("payment_methods").get(pm_ref)
    if pm is None and (pm_ref.startswith("pm_card_") or pm_ref.startswith("tok_")):
        pm = create_payment_method(pm_ref)          # Stripe test shortcuts
    return pm


def create_payment_intent(amount: int, currency: str, customer=None,
                          payment_method=None, confirm=False, metadata=None,
                          description=None, idempotency_key=None) -> dict:
    if amount is None or int(amount) < 1:
        raise StripeError(400, "invalid_request_error",
                          "This value must be greater than or equal to 1.",
                          code="parameter_invalid_integer", param="amount")
    if not currency:
        raise StripeError(400, "invalid_request_error",
                          "Missing required param: currency.",
                          code="parameter_missing", param="currency")
    pm = _resolve_pm(payment_method)
    if payment_method and pm is None:
        raise StripeError(400, "invalid_request_error",
                          f"No such PaymentMethod: '{payment_method}'",
                          code="resource_missing", param="payment_method")
    pid = _id("pi", "payment_intent")
    pi = {"id": pid, "object": "payment_intent", "amount": int(amount),
          "amount_capturable": 0, "amount_details": {"tip": {}},
          "amount_received": 0, "application": None,
          "application_fee_amount": None, "automatic_payment_methods": None,
          "canceled_at": None, "cancellation_reason": None,
          "capture_method": "automatic",
          "client_secret": f"{pid}_secret_"
                           f"{hashlib.sha256(pid.encode()).hexdigest()[:24]}",
          "confirmation_method": "automatic", "created": _ts(),
          "currency": currency.lower(), "customer": customer,
          "description": description, "last_payment_error": None,
          "latest_charge": None, "livemode": False, "metadata": metadata or {},
          "next_action": None, "on_behalf_of": None,
          "payment_method": pm["id"] if pm else None,
          "payment_method_options": {}, "payment_method_types": ["card"],
          "processing": None, "receipt_email": None, "review": None,
          "setup_future_usage": None, "shipping": None,
          "statement_descriptor": None, "statement_descriptor_suffix": None,
          "status": "requires_confirmation" if pm else "requires_payment_method",
          "transfer_data": None, "transfer_group": None}
    _tbl("payment_intents")[pid] = pi
    wh.emit_stripe("payment_intent.created", dict(pi), idempotency_key,
                   payment_intent=pid, **_meta_refs(pi))
    if _bool(confirm):
        confirm_payment_intent(pid, idempotency_key=idempotency_key)
    return pi


def _meta_refs(pi: dict) -> dict:
    md = pi.get("metadata") or {}
    return {k: md[k] for k in ("invoice_id", "tenant_id") if k in md}


def confirm_payment_intent(pid: str, payment_method=None,
                           idempotency_key=None) -> dict:
    pi = _tbl("payment_intents").get(pid)
    if pi is None:
        raise StripeError(404, "invalid_request_error",
                          f"No such payment_intent: '{pid}'",
                          code="resource_missing", param="intent")
    if pi["status"] == "succeeded":
        raise StripeError(400, "invalid_request_error",
                          "This PaymentIntent has already succeeded.",
                          code="payment_intent_unexpected_state",
                          payment_intent=pi)
    pm = _resolve_pm(payment_method) if payment_method else \
        _tbl("payment_methods").get(pi["payment_method"] or "")
    if pm is None:
        raise StripeError(400, "invalid_request_error",
                          "You cannot confirm this PaymentIntent because it's "
                          "missing a payment method.",
                          code="payment_intent_unexpected_state",
                          payment_intent=pi)
    pi["payment_method"] = pm["id"]
    token = pm["metadata"].get("test_token", "")
    ch = _id("ch", "charge")
    if _declines(token):
        err = {"type": "card_error", "code": "card_declined",
               "decline_code": "generic_decline",
               "message": "Your card was declined.", "charge": ch,
               "payment_method": pm, "doc_url":
               "https://stripe.com/docs/error-codes/card-declined"}
        pi.update(status="requires_payment_method", last_payment_error=err,
                  latest_charge=ch)
        wh.emit_stripe("payment_intent.payment_failed", dict(pi), idempotency_key,
                       payment_intent=pid, **_meta_refs(pi))
        raise StripeError(402, "card_error", "Your card was declined.",
                          code="card_declined", decline_code="generic_decline",
                          charge=ch, payment_intent=pi)
    pi.update(status="succeeded", amount_received=pi["amount"],
              last_payment_error=None, latest_charge=ch)
    wh.emit_stripe("payment_intent.succeeded", dict(pi), idempotency_key,
                   payment_intent=pid, **_meta_refs(pi))
    return pi


def invoice_object(inv: dict) -> dict:
    """Stripe Invoice view of a NeoCloud invoice (KRW is zero-decimal)."""
    status = {"draft": "draft", "finalized": "open", "paid": "paid",
              "void": "void"}.get(inv["status"], "draft")
    cus = customer_for(inv["tenant_id"])
    pay = inv.get("payment") or {}
    paid = inv["total"] if inv["status"] == "paid" else 0
    from .billing import period_bounds
    p0, p1 = period_bounds(inv["period"])
    lines = [{"id": f"il_{inv['id']}_{i}", "object": "line_item",
              "amount": l["amount"], "currency": "krw",
              "description": f"{l['product']} {l['metric']}"
                             f"{' ' + l['token_type'] if l.get('token_type') else ''}",
              "discountable": True, "discounts": [], "invoice": f"in_{inv['id']}",
              "livemode": False, "metadata": {}, "parent": None,
              "period": {"start": int(p0.timestamp()), "end": int(p1.timestamp())},
              "pretax_credit_amounts": [], "pricing": None,
              "quantity": int(round(l["quantity"])), "subtotal": l["amount"],
              "taxes": []}
             for i, l in enumerate(inv.get("lines") or [])]
    created = int(datetime.fromisoformat(inv["created_at"]).timestamp())
    return {
        "id": f"in_{inv['id']}", "object": "invoice", "account_country": "KR",
        "account_name": "NeoCloud (emulated)", "amount_due": inv["total"],
        "amount_overpaid": 0, "amount_paid": paid, "amount_paid_off_stripe": 0,
        "amount_remaining": inv["total"] - paid, "amount_shipping": 0,
        "attempt_count": 1 if pay else 0, "attempted": bool(pay),
        "auto_advance": False, "automatic_tax": {"enabled": False,
                                                  "liability": None,
                                                  "status": None},
        "billing_reason": "subscription_cycle",
        "collection_method": "charge_automatically", "created": created,
        "currency": "krw", "customer": cus["id"], "default_tax_rates": [],
        "description": f"NeoCloud {inv['period']}", "discounts": [],
        "issuer": {"type": "self"},
        "lines": {"object": "list", "data": lines, "has_more": False,
                  "url": f"/v1/invoices/in_{inv['id']}/lines"},
        "livemode": False, "metadata": {"neocloud_invoice_id": inv["id"],
                                        "tenant_id": inv["tenant_id"]},
        "number": inv["id"] if status != "draft" else None,
        "payment_settings": {"default_mandate": None,
                             "payment_method_options": None,
                             "payment_method_types": None},
        "period_end": int(p1.timestamp()), "period_start": int(p0.timestamp()),
        "post_payment_credit_notes_amount": 0,
        "pre_payment_credit_notes_amount": inv.get("credit_total") or 0,
        "starting_balance": 0, "status": status,
        "status_transitions": {
            "finalized_at": _unix(inv.get("finalized_at")),
            "marked_uncollectible_at": None,
            "paid_at": _unix(inv.get("paid_at")), "voided_at": None},
        "subtotal": inv["usage_amount"], "subtotal_excluding_tax": inv["subtotal"],
        "total": inv["total"], "total_excluding_tax": inv["subtotal"],
        "total_discount_amounts": [], "total_pretax_credit_amounts": [],
        "total_taxes": [{"amount": inv["vat"], "tax_behavior": "exclusive",
                         "tax_rate_details": {"tax_rate": "txr_kr_vat_10"},
                         "taxability_reason": "standard_rated",
                         "taxable_amount": inv["subtotal"],
                         "type": "tax_rate_details"}],
        "webhooks_delivered_at": created,
    }


def _unix(iso: Optional[str]) -> Optional[int]:
    return int(datetime.fromisoformat(iso).timestamp()) if iso else None


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
def _guarded(fn):
    """Operator check, body parsing (outside the lock), Stripe error envelope.
    The wrapped handler is sync: fn(request, params, **path_params)."""
    import inspect
    sig = inspect.signature(fn)
    visible = [p for n, p in sig.parameters.items() if n != "p"]

    async def wrapper(request: Request, **kw):
        if not _identity_ok(request):
            return _err(StripeError(401, "invalid_request_error",
                                    "Invalid API Key provided (operator only)."))
        try:
            p = await _params(request) if request.method == "POST" else {}
            with STORE.lock:
                return fn(request, p, **kw)
        except StripeError as e:
            return _err(e)
    wrapper.__name__ = fn.__name__
    wrapper.__signature__ = sig.replace(parameters=visible)
    return wrapper


@router.post("/customers")
@_guarded
def http_create_customer(request: Request, p: dict):
    return create_customer(p.get("email"), p.get("name"), p.get("metadata"))


def _get(tbl: str, key: str, label: str, param: str) -> dict:
    obj = _tbl(tbl).get(key)
    if obj is None:
        raise StripeError(404, "invalid_request_error",
                          f"No such {label}: '{key}'", code="resource_missing",
                          param=param)
    return obj


@router.get("/customers/{customer}")
@_guarded
def http_get_customer(request: Request, p: dict, customer: str):
    return _get("customers", customer, "customer", "id")


@router.post("/payment_methods")
@_guarded
def http_create_pm(request: Request, p: dict):
    token = (p.get("card") or {}).get("token") if isinstance(p.get("card"), dict) \
        else None
    return create_payment_method(token or "tok_visa", p.get("type") or "card")


@router.post("/payment_methods/{payment_method}/attach")
@_guarded
def http_attach_pm(request: Request, p: dict, payment_method: str):
    pm = _resolve_pm(payment_method)
    if pm is None:
        raise StripeError(404, "invalid_request_error",
                          f"No such PaymentMethod: '{payment_method}'",
                          code="resource_missing", param="payment_method")
    _get("customers", p.get("customer") or "", "customer", "customer")
    pm["customer"] = p["customer"]
    return pm


@router.post("/payment_intents")
@_guarded
def http_create_pi(request: Request, p: dict):
    return create_payment_intent(
        int(p["amount"]) if p.get("amount") not in (None, "") else None,
        p.get("currency"), p.get("customer"), p.get("payment_method"),
        p.get("confirm"), p.get("metadata"), p.get("description"),
        request.headers.get("idempotency-key"))


@router.post("/payment_intents/{intent}/confirm")
@_guarded
def http_confirm_pi(request: Request, p: dict, intent: str):
    return confirm_payment_intent(intent, p.get("payment_method"),
                                  request.headers.get("idempotency-key"))


@router.get("/payment_intents/{intent}")
@_guarded
def http_get_pi(request: Request, p: dict, intent: str):
    return _get("payment_intents", intent, "payment_intent", "intent")


@router.get("/invoices/{invoice}")
@_guarded
def http_get_invoice(request: Request, p: dict, invoice: str):
    inv = STORE.invoices.get(invoice.removeprefix("in_"))
    if inv is None:
        raise StripeError(404, "invalid_request_error",
                          f"No such invoice: '{invoice}'", code="resource_missing",
                          param="invoice")
    return invoice_object(inv)


def _list(data: list, url: str) -> dict:
    return {"object": "list", "data": data, "has_more": False, "url": url}


@router.get("/events")
@_guarded
def http_list_events(request: Request, p: dict):
    typ = request.query_params.get("type")
    evs = [e for e in reversed(list(_tbl("events").values()))
           if not typ or e["type"] == typ]
    return _list(evs[:int(request.query_params.get("limit") or 10)], "/v1/events")


@router.get("/events/{event_id}")
@_guarded
def http_get_event(request: Request, p: dict, event_id: str):
    return _get("events", event_id, "event", "id")


@router.post("/webhook_endpoints")
@_guarded
def http_create_we(request: Request, p: dict):
    if not p.get("url"):
        raise StripeError(400, "invalid_request_error", "Missing required param: url.",
                          code="parameter_missing", param="url")
    eps = wh.endpoints("stripe")
    weid = _id("we", "webhook_endpoint")
    ep = {"id": weid, "object": "webhook_endpoint",
          "api_version": p.get("api_version") or wh.STRIPE_API_VERSION,
          "application": None, "created": _ts(),
          "description": p.get("description"),
          "enabled_events": p.get("enabled_events") or ["*"], "livemode": False,
          "metadata": p.get("metadata") or {},
          "secret": f"whsec_{hashlib.sha256(weid.encode()).hexdigest()[:32]}",
          "status": "enabled", "url": p["url"]}
    eps[weid] = ep
    return ep


@router.get("/webhook_endpoints")
@_guarded
def http_list_we(request: Request, p: dict):
    return _list([{k: v for k, v in ep.items() if k != "secret"}
                  for ep in wh.endpoints("stripe").values()],
                 "/v1/webhook_endpoints")
