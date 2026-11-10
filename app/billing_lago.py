"""F05/F06 Lago API emulator — events, billable metrics, plans, customers,
subscriptions, invoices and webhook endpoints, backed by the NeoCloud billing
engine (billing.py) and contracts (contracts.py).

Upstream: getlago/lago-openapi@7d237882f6ef openapi.yaml (AGPL-3.0, so URL
only — no copy in this repo; model shapes from the public API docs). The Lago
engine itself (getlago/lago-api) was not read. Mounted at `/lago/api/v1/...`
so a client whose base URL is `http://<nocp>/lago` sends Lago's own paths:
  POST|GET /api/v1/billable_metrics · GET /api/v1/billable_metrics/{code}
  POST|GET /api/v1/plans · GET /api/v1/plans/{code}
  POST|GET /api/v1/customers · GET /api/v1/customers/{external_id}
  GET /api/v1/customers/{external_customer_id}/invoices
  POST|GET /api/v1/subscriptions · GET|DELETE /api/v1/subscriptions/{external_id}
  POST /api/v1/events · POST /api/v1/events/batch · GET /api/v1/events/{transaction_id}
  GET /api/v1/invoices · GET /api/v1/invoices/{lago_id}
  PUT /api/v1/invoices/{lago_id}/finalize · POST .../void · POST .../retry_payment
  POST|GET /api/v1/webhook_endpoints
All create calls answer 200 with `{<object>: {...}}` like Lago; lists are
`{<plural>: [...], meta:{current_page, next_page, prev_page, total_pages,
total_count}}`; errors are Lago's `{status, error, code, error_details?}`.

Flow (UPSTREAM-REFS §3.1 7–10): customer → subscription(plan) → events
(code = billable metric code) → invoice (draft → finalized) → payment.
  - NeoCloud tenant = Lago customer (external_id = tenant id). The customer is
    created on first use (facade or Lago call) and `customer.created` is sent.
  - Contract activation (F06) = Lago plan `neocloud_contract_<id>` with
    `minimum_commitment` + subscription external_id = contract id
    (`subscription.started`); termination → `subscription.terminated`.
  - Every customer also has the on-demand subscription `<tenant>:on-demand`
    on plan `neocloud_on_demand` (charges from the KRW price book).
  - Events are idempotent by transaction_id (a resend is accepted and not
    billed twice). Missing required fields → 422 like Lago; an unknown metric
    code or subscription is accepted (200) and reported asynchronously with
    the `event.error` webhook, as Lago validates those after ingest. Accepted
    events feed the engine in the F04 usage-event shape (source=gateway).
  - Invoices are the facade's monthly invoices in Lago shape (KRW is a
    zero-decimal currency, so *_amount_cents = won). SLA credits (F09), a
    pre-tax deduction, are reported as coupons_amount_cents; commitment
    true-ups as fees with item.type "commitment". Webhooks: invoice.drafted
    on run, invoice.created on finalize, invoice.payment_status_updated /
    invoice.payment_failure on payment, invoice.voided on void. Lago has no
    `invoice.finalized` webhook (checked in the spec's `webhooks:`).

Auth: Lago uses `Authorization: Bearer <api key>`; on NOCP that header belongs
to the tenancy guard (auth.py), so this surface takes the NOCP operator
identity (NeoCloud deviation).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import billing_webhooks as wh
from . import contracts as ctr
from .store import STORE
from .trace import emit

router = APIRouter(prefix="/lago/api/v1", tags=["lago-emulator"])

CURRENCY = "KRW"
ON_DEMAND_PLAN = "neocloud_on_demand"
BILLING_ENTITY = "neocloud"
NET_PAYMENT_TERM = 30
AVG_MONTH_HOURS = 730            # indicative minimum_commitment of rack plans
BATCH_MAX = 100

# billable metric code → (product default, engine metric, token_type, field)
METRICS = {
    "rack_hour": ("bmaas", "rack_hour", None, "hours"),
    "gpu_hour": ("bmaas", "gpu_hour", None, "hours"),
    "node_hour": ("bmaas", "node_hour", None, "hours"),
    "input_tokens": ("token-api", "token", "input", "tokens"),
    "cached_tokens": ("token-api", "token", "cached", "tokens"),
    "output_tokens": ("token-api", "token", "output", "tokens"),
    "reasoning_tokens": ("token-api", "token", "reasoning", "tokens"),
}
METRIC_NAMES = {
    "rack_hour": "Rack hours", "gpu_hour": "GPU hours", "node_hour": "Node hours",
    "input_tokens": "Input tokens", "cached_tokens": "Cached input tokens",
    "output_tokens": "Output tokens", "reasoning_tokens": "Reasoning tokens",
}


class LagoError(Exception):
    _TEXT = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
             404: "Not Found", 405: "Method Not Allowed",
             422: "Unprocessable Entity"}

    def __init__(self, status: int, code: Optional[str] = None,
                 error_details: Optional[dict] = None):
        self.status = status
        self.body = {"status": status, "error": self._TEXT.get(status, "Error")}
        if code is not None:
            self.body["code"] = code
        if error_details is not None:
            self.body["error_details"] = error_details


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def _lid(kind: str, key: str) -> str:
    """Deterministic lago_id (uuid5) so demo reruns show the same ids."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"neocloud/lago/{kind}/{key}"))


def _tbl(name: str) -> dict:
    return STORE.lago.setdefault(name, {})


def _mandatory(obj: dict, *fields) -> None:
    missing = {f: ["value_is_mandatory"] for f in fields
               if obj.get(f) in (None, "")}
    if missing:
        raise LagoError(422, "validation_errors", missing)


def _paginate(request: Request, key: str, items: list) -> dict:
    page = max(1, int(request.query_params.get("page") or 1))
    per = max(1, min(100, int(request.query_params.get("per_page") or 20)))
    total = len(items)
    pages = max(1, -(-total // per))
    return {key: items[(page - 1) * per: page * per],
            "meta": {"current_page": page,
                     "next_page": page + 1 if page < pages else None,
                     "prev_page": page - 1 if page > 1 else None,
                     "total_pages": pages, "total_count": total}}


# ---------------------------------------------------------------------------
# billable metrics · plans (seeded from the price book)
# ---------------------------------------------------------------------------
def _metric_obj(code: str, name: str, field: str, description=None,
                aggregation_type="sum_agg", recurring=False) -> dict:
    return {"lago_id": _lid("billable_metric", code), "name": name, "code": code,
            "description": description, "recurring": recurring,
            "rounding_function": None, "rounding_precision": None,
            "created_at": _iso(_now()), "expression": None, "field_name": field,
            "aggregation_type": aggregation_type, "weighted_interval": None,
            "filters": []}


def billable_metrics() -> dict:
    t = _tbl("billable_metrics")
    if not t:
        for code, (_, _, _, field) in METRICS.items():
            t[code] = _metric_obj(code, METRIC_NAMES[code], field,
                                  f"NeoCloud {METRIC_NAMES[code].lower()}")
    return t


def _charge(plan_code: str, metric_code: str, amount, model="standard") -> dict:
    bm = billable_metrics()[metric_code]
    return {"lago_id": _lid("charge", f"{plan_code}/{metric_code}"),
            "lago_billable_metric_id": bm["lago_id"], "code": metric_code,
            "billable_metric_code": metric_code,
            "invoice_display_name": bm["name"], "created_at": bm["created_at"],
            "charge_model": model, "pay_in_advance": False, "invoiceable": True,
            "regroup_paid_fees": None, "prorated": False, "min_amount_cents": 0,
            "properties": {"amount": amount}, "filters": [], "taxes": [],
            "applied_pricing_unit": None, "accepts_target_wallet": False,
            "lago_parent_id": None}


def _amount_str(unit_price: float, per: int = 1) -> str:
    return f"{unit_price / per:.10f}".rstrip("0").rstrip(".") or "0"


def _price_charges(plan_code: str, product: Optional[str] = None,
                   reserved: bool = False) -> list[dict]:
    from . import billing
    out = []
    for code, (prod, metric, token_type, _) in METRICS.items():
        prod = product if (product and metric != "token" and product != "token-api") \
            else prod
        if product == "token-api" and metric != "token":
            continue
        if product in ctr.RACK_PRODUCTS and metric == "token":
            continue
        it = billing.lookup_price(prod, metric, token_type=token_type,
                                  commitment="reserved" if reserved else "on_demand")
        if it is None:
            continue
        per = billing.TOKEN_UNIT if metric == "token" else 1
        out.append(_charge(plan_code, code, _amount_str(it["unit_price"], per)))
    return out


def _plan_obj(code: str, name: str, charges: list, description=None,
              minimum_commitment=None, metadata=None) -> dict:
    return {"lago_id": _lid("plan", code), "name": name,
            "invoice_display_name": name, "created_at": _iso(_now()),
            "code": code, "interval": "monthly", "description": description,
            "amount_cents": 0, "amount_currency": CURRENCY, "trial_period": None,
            "pay_in_advance": False, "bill_charges_monthly": None,
            "bill_fixed_charges_monthly": None, "parent_id": None,
            "pending_deletion": False, "minimum_commitment": minimum_commitment,
            "charges": charges, "fixed_charges": [], "taxes": [_vat_tax()],
            "usage_thresholds": [], "applicable_usage_thresholds": [],
            "entitlements": [], "metadata": metadata}


def _vat_tax() -> dict:
    return {"lago_id": _lid("tax", "kr_vat_10"), "name": "부가가치세 (VAT)",
            "code": "kr_vat_10", "description": "Korea VAT 10% (won floor)",
            "rate": 10.0, "applied_to_organization": True,
            "created_at": "2026-01-01T00:00:00Z"}


def plans() -> dict:
    t = _tbl("plans")
    if ON_DEMAND_PLAN not in t:
        t[ON_DEMAND_PLAN] = _plan_obj(
            ON_DEMAND_PLAN, "NeoCloud on-demand", _price_charges(ON_DEMAND_PLAN),
            "Pay as you go — KRW price book (billing.py)")
    return t


def contract_plan(c: dict) -> dict:
    code = f"neocloud_contract_{c['id']}"
    if c.get("min_monthly_amount") is not None:
        commit = int(c["min_monthly_amount"])
    else:
        commit = int((c.get("racks") or 0) * float(c["unit_price"]) * AVG_MONTH_HOURS)
    charges = _price_charges(code, c["product"], reserved=True)
    if c["product"] in ctr.RACK_PRODUCTS:     # contract unit price per rack-hour
        charges = [ch if ch["billable_metric_code"] != "rack_hour"
                   else {**ch, "properties": {"amount": _amount_str(c["unit_price"])}}
                   for ch in charges]
    plan = _plan_obj(
        code, f"NeoCloud reserved {c['product']} {c['id']}", charges,
        f"{c.get('racks') or ''} {c['product']} · {c['term_months']} months",
        minimum_commitment={
            "lago_id": _lid("commitment", code), "plan_code": code,
            "amount_cents": commit, "invoice_display_name": "Minimum commitment",
            "interval": "monthly", "created_at": _iso(_now()),
            "updated_at": _iso(_now()), "taxes": []},
        metadata={"contract_id": c["id"]})
    plans()[code] = plan
    return plan


# ---------------------------------------------------------------------------
# customers · subscriptions
# ---------------------------------------------------------------------------
def _business_reg_no(tid: str) -> Optional[str]:
    tenant = STORE.tenants.get(tid)
    reg = getattr(tenant, "business_reg_no", None)
    if reg is None:
        for rec in (getattr(STORE, "signups", None) or {}).values():
            rec = rec.model_dump() if hasattr(rec, "model_dump") else rec
            if rec.get("tenant_id") == tid:
                return rec.get("business_reg_no")
    return reg


def _customer_obj(external_id: str, seq: int, fields: dict) -> dict:
    from . import billing_stripe
    tenant = STORE.tenants.get(external_id)
    name = fields.get("name") or (tenant.name if tenant else external_id)
    cus = billing_stripe.customer_for(external_id, name)
    bc = {"invoice_grace_period": None,
          "subscription_invoice_issuing_date_anchor": None,
          "subscription_invoice_issuing_date_adjustment": None,
          "payment_provider": "stripe", "payment_provider_code": "stripe_test",
          "provider_customer_id": cus["id"], "sync": False,
          "sync_with_provider": False, "document_locale": "ko",
          "provider_payment_methods": ["card"],
          **(fields.get("billing_configuration") or {})}
    now = _iso(_now())
    obj = {"lago_id": _lid("customer", external_id), "sequential_id": seq,
           "slug": f"NEO-{seq:04d}", "external_id": external_id,
           "billing_entity_code": BILLING_ENTITY, "address_line1": None,
           "address_line2": None, "applicable_timezone": "Asia/Seoul",
           "city": None, "country": "KR", "currency": CURRENCY, "email": None,
           "legal_name": None, "legal_number": None, "logo_url": None,
           "name": name, "firstname": None, "lastname": None,
           "account_type": "customer", "customer_type": "company", "phone": None,
           "state": None, "tax_identification_number": _business_reg_no(external_id),
           "timezone": "Asia/Seoul", "url": None, "zipcode": None,
           "net_payment_term": NET_PAYMENT_TERM, "external_salesforce_id": None,
           "created_at": now, "updated_at": now,
           "finalize_zero_amount_invoice": "inherit",
           "skip_invoice_custom_sections": False,
           "shipping_address": {"address_line1": None, "address_line2": None,
                                "city": None, "zipcode": None, "state": None,
                                "country": None},
           "metadata": [], "taxes": [_vat_tax()],
           "integration_customers": [], "applicable_invoice_custom_sections": []}
    for k in ("email", "legal_name", "legal_number", "country", "city", "phone",
              "address_line1", "address_line2", "zipcode", "url",
              "tax_identification_number", "firstname", "lastname"):
        if fields.get(k) is not None:
            obj[k] = fields[k]
    if fields.get("currency") not in (None, CURRENCY):
        raise LagoError(422, "validation_errors", {"currency": ["value_is_invalid"]})
    obj["billing_configuration"] = bc
    return obj


def ensure_customer(external_id: str, fields: Optional[dict] = None) -> dict:
    t = _tbl("customers")
    cur = t.get(external_id)
    if cur is not None and not fields:
        return cur
    obj = _customer_obj(external_id, cur["sequential_id"] if cur
                        else STORE.next_seq("lago_customer"), fields or {})
    if cur is not None:
        obj["created_at"] = cur["created_at"]
    t[external_id] = obj
    if cur is None:
        wh.emit_lago("customer.created", "customer", obj, tenant_id=external_id)
        _ensure_on_demand(external_id)
    return obj


def _period_window(at: datetime) -> tuple[str, str]:
    p0 = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    p1 = (p0 + timedelta(days=32)).replace(day=1)
    return _iso(p0), _iso(p1 - timedelta(seconds=1))


def _subscription_obj(external_id: str, customer: dict, plan: dict,
                      name=None, subscription_at: Optional[datetime] = None,
                      ending_at: Optional[datetime] = None) -> dict:
    now = _now()
    start = subscription_at or now
    active = start <= now and (ending_at is None or ending_at > now)
    status = "active" if active else ("terminated" if ending_at and ending_at <= now
                                      else "pending")
    ps, pe = _period_window(now)
    return {"lago_id": _lid("subscription", external_id),
            "external_id": external_id, "lago_customer_id": customer["lago_id"],
            "external_customer_id": customer["external_id"],
            "billing_entity_code": BILLING_ENTITY, "billing_time": "calendar",
            "name": name, "plan_code": plan["code"],
            "plan_amount_cents": plan["amount_cents"],
            "plan_amount_currency": plan["amount_currency"], "status": status,
            "created_at": _iso(now), "canceled_at": None,
            "started_at": _iso(start) if status != "pending" else None,
            "ending_at": _iso(ending_at), "subscription_at": _iso(start),
            "terminated_at": _iso(ending_at) if status == "terminated" else None,
            "previous_plan_code": None, "next_plan_code": None,
            "downgrade_plan_date": None, "trial_ended_at": None,
            "current_billing_period_started_at": ps if status == "active" else None,
            "current_billing_period_ending_at": pe if status == "active" else None,
            "on_termination_credit_note": None, "on_termination_invoice": "generate",
            "applied_invoice_custom_sections": [],
            "payment_method": {"payment_method_type": "provider"},
            "consolidate_invoice": False, "progressive_billing_disabled": False,
            "cancellation_reason": None,
            "activated_at": _iso(start) if status != "pending" else None,
            "activation_rules": [], "purchase_order_number": None}


def _with_customer(sub: dict) -> dict:
    return {**sub, "plan": plans().get(sub["plan_code"]),
            "customer": ensure_customer(sub["external_customer_id"]),
            "entitlements": []}


def _put_subscription(sub: dict) -> dict:
    t = _tbl("subscriptions")
    new = sub["external_id"] not in t
    t[sub["external_id"]] = sub
    if new and sub["status"] == "active":
        wh.emit_lago("subscription.started", "subscription", _with_customer(sub),
                     tenant_id=sub["external_customer_id"],
                     subscription=sub["external_id"])
    return sub


def _ensure_on_demand(tid: str) -> dict:
    ext = f"{tid}:on-demand"
    sub = _tbl("subscriptions").get(ext)
    if sub is None:
        sub = _put_subscription(_subscription_obj(
            ext, _tbl("customers")[tid], plans()[ON_DEMAND_PLAN], "On-demand"))
    return sub


def terminate_subscription(ext: str, at: Optional[datetime] = None) -> dict:
    sub = _tbl("subscriptions")[ext]
    if sub["status"] != "terminated":
        at = at or _now()
        sub.update(status="terminated", terminated_at=_iso(at), ending_at=_iso(at),
                   current_billing_period_started_at=None,
                   current_billing_period_ending_at=None)
        wh.emit_lago("subscription.terminated", "subscription", _with_customer(sub),
                     tenant_id=sub["external_customer_id"], subscription=ext)
    return sub


# F06 hooks (contracts.py) ---------------------------------------------------
def on_contract_activated(c: dict) -> None:
    customer = ensure_customer(c["tenant_id"])
    plan = contract_plan(c)
    cs, ce = ctr.term_window(c)
    sub = _subscription_obj(c["id"], customer, plan, f"Contract {c['id']}",
                            subscription_at=cs, ending_at=ce)
    if sub["status"] == "terminated":          # back-dated term already over
        _tbl("subscriptions")[c["id"]] = sub
        return
    _put_subscription(sub)


def on_contract_terminated(c: dict) -> None:
    if c["id"] in _tbl("subscriptions"):
        terminate_subscription(c["id"])


# ---------------------------------------------------------------------------
# events → F04 usage events
# ---------------------------------------------------------------------------
def _event_ts(v) -> Optional[datetime]:
    if v in (None, ""):
        return _now()
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _event_error(ev: dict, message: str) -> None:
    wh.emit_lago("event.error", "event_error",
                 {"status": 422, "error": "Unprocessable entity",
                  "message": message, "event": ev},
                 transaction_id=ev.get("transaction_id"))


def ingest(ev: dict) -> dict:
    """Store one Lago event (idempotent by transaction_id); returns EventObject."""
    _mandatory(ev, "transaction_id", "external_subscription_id", "code")
    at = _event_ts(ev.get("timestamp"))
    if at is None:
        raise LagoError(422, "validation_errors", {"timestamp": ["invalid_format"]})
    events = _tbl("events")
    tx = str(ev["transaction_id"])
    if tx in events:                            # duplicate: accepted, not rebilled
        return events[tx]["object"]
    sub = _tbl("subscriptions").get(ev["external_subscription_id"])
    customer = _tbl("customers").get(sub["external_customer_id"]) if sub else None
    obj = {"lago_id": _lid("event", tx), "transaction_id": tx,
           "lago_customer_id": customer["lago_id"] if customer else None,
           "code": ev["code"], "timestamp": _iso(at),
           "precise_total_amount_cents": ev.get("precise_total_amount_cents"),
           "properties": ev.get("properties") or {},
           "lago_subscription_id": sub["lago_id"] if sub else None,
           "external_subscription_id": ev["external_subscription_id"],
           "created_at": _iso(_now())}
    usage = None
    if ev["code"] not in billable_metrics():
        _event_error(obj, "billable_metric_not_found")
    elif sub is None:
        _event_error(obj, "subscription_not_found")
    else:
        usage = _usage_event(obj, sub, at)
        if usage is None:
            _event_error(obj, "invalid_properties")
    events[tx] = {"object": obj, "usage": usage}
    return obj


def _usage_event(obj: dict, sub: dict, at: datetime) -> Optional[dict]:
    prod, metric, token_type, field = METRICS[obj["code"]]
    props = obj["properties"]
    try:
        qty = float(props.get(billable_metrics()[obj["code"]]["field_name"] or field))
    except (TypeError, ValueError):
        return None
    c = STORE.contracts.get(sub["external_id"])
    if metric != "token":
        prod = props.get("product") or (c["product"] if c and c["product"]
                                        in ctr.RACK_PRODUCTS else prod)
    attrs = {"model": props.get("model"), "lago_code": obj["code"],
             "external_subscription_id": sub["external_id"]}
    if token_type:
        attrs["token_type"] = token_type
    if props.get("service_tier") or props.get("tier"):
        attrs["tier"] = props.get("service_tier") or props.get("tier")
    return {"idempotency_key": f"lago:{obj['transaction_id']}", "source": "gateway",
            "tenant_id": sub["external_customer_id"], "product": prod,
            "metric": metric, "quantity": qty,
            "unit": "token" if metric == "token" else metric.replace("_", "-"),
            "ts": at.isoformat(), "attrs": attrs}


def usage_events() -> list[dict]:
    """Accepted Lago events in the F04 usage-event shape (billing.py reads them)."""
    return [e["usage"] for e in STORE.lago.get("events", {}).values() if e["usage"]]


# ---------------------------------------------------------------------------
# invoices (facade invoice → Lago InvoiceObjectExtended)
# ---------------------------------------------------------------------------
_STATUS = {"draft": "draft", "finalized": "finalized", "paid": "finalized",
           "void": "voided"}


def _fee(inv: dict, i: int, line: dict, cust: dict, kind: str) -> dict:
    lago_inv = _lid("invoice", inv["id"])
    if kind == "commitment":
        code, name = "minimum_commitment", "Minimum commitment true-up"
        item = {"type": "commitment", "code": code, "name": name,
                "description": None, "invoice_display_name": name,
                "filter_invoice_display_name": None, "filters": None,
                "lago_item_id": _lid("commitment",
                                     f"neocloud_contract_{line['contract_id']}"),
                "item_type": "Subscription", "grouped_by": {}}
        units, unit_amount, sub_ext = "1", str(line["amount"]), line["contract_id"]
    else:
        code = (f"{line['token_type']}_tokens" if line["metric"] == "token"
                else line["metric"])
        bm = billable_metrics().get(code) or _metric_obj(code, code, "quantity")
        name = bm["name"]
        item = {"type": "charge", "code": code, "name": name,
                "description": bm["description"],
                "invoice_display_name": f"{name} · {line['product']}",
                "filter_invoice_display_name": None, "filters": None,
                "lago_item_id": bm["lago_id"], "item_type": "BillableMetric",
                "grouped_by": {k: line[k] for k in ("model", "tier")
                               if line.get(k)}}
        units = f"{line['quantity']:g}"
        unit_amount = _amount_str(line["unit_price"],
                                  1_000_000 if line["metric"] == "token" else 1)
        sub_ext = line.get("contract_id") or f"{inv['tenant_id']}:on-demand"
    sub = _tbl("subscriptions").get(sub_ext)
    tax = int(line["amount"] * 10 // 100)
    amount = int(line["amount"])
    p0, p1 = _period_bounds(inv)
    return {"lago_id": _lid("fee", f"{inv['id']}/{kind}/{i}"),
            "lago_charge_id": (_lid("charge", f"{sub['plan_code']}/{code}")
                               if sub and kind != "commitment" else None),
            "lago_charge_filter_id": None, "lago_fixed_charge_id": None,
            "lago_invoice_id": lago_inv, "lago_true_up_fee_id": None,
            "lago_true_up_parent_fee_id": None, "lago_original_fee_id": None,
            "lago_subscription_id": sub["lago_id"] if sub else None,
            "lago_customer_id": cust["lago_id"],
            "external_customer_id": cust["external_id"],
            "external_subscription_id": sub_ext,
            "amount_cents": amount, "precise_amount": str(amount),
            "precise_total_amount": str(amount + tax), "amount_currency": CURRENCY,
            "taxes_amount_cents": tax, "taxes_precise_amount": str(tax),
            "taxes_rate": 10.0, "units": units, "precise_unit_amount": unit_amount,
            "total_aggregated_units": units, "total_amount_cents": amount + tax,
            "total_amount_currency": CURRENCY, "events_count": None,
            "pay_in_advance": False, "invoiceable": True,
            "from_date": _iso(p0), "to_date": _iso(p1 - timedelta(seconds=1)),
            "payment_status": _payment_status(inv),
            "created_at": inv["created_at"], "succeeded_at": None,
            "failed_at": None, "refunded_at": None, "event_transaction_id": None,
            "description": line.get("product"),
            "precise_coupons_amount_cents": "0",
            "sub_total_excluding_taxes_amount_cents": amount,
            "sub_total_excluding_taxes_precise_amount_cents": str(amount),
            "amount_details": {}, "self_billed": False, "item": item,
            "applied_taxes": [], "presentation_breakdowns": []}


def _period_bounds(inv: dict):
    from .billing import period_bounds
    return period_bounds(inv["period"])


def _payment_status(inv: dict) -> str:
    if inv["status"] == "paid":
        return "succeeded"
    if inv.get("payment_failed"):
        return "failed"
    return "pending"


def invoice_object(inv: dict) -> dict:
    cust = ensure_customer(inv["tenant_id"])
    lago_inv = _lid("invoice", inv["id"])
    issued = datetime.fromisoformat(inv.get("finalized_at") or inv["created_at"])
    paid = inv["total"] if inv["status"] == "paid" else 0
    fees = [_fee(inv, i, l, cust, "charge") for i, l in enumerate(inv.get("lines") or [])]
    fees += [_fee(inv, i, t, cust, "commitment")
             for i, t in enumerate(inv.get("true_up_lines") or []) if t["amount"] > 0]
    subs = sorted({f["external_subscription_id"] for f in fees})
    vat = _vat_tax()
    try:
        seq = int(inv["id"].rsplit("-", 1)[-1])
    except ValueError:
        seq = None
    return {
        "lago_id": lago_inv, "billing_entity_code": BILLING_ENTITY,
        "sequential_id": seq, "number": inv["id"], "purchase_order_number": None,
        "issuing_date": issued.date().isoformat(), "payment_dispute_lost_at": None,
        "payment_due_date": (issued + timedelta(days=NET_PAYMENT_TERM)).date()
        .isoformat(),
        "payment_overdue": False, "net_payment_term": NET_PAYMENT_TERM,
        "invoice_type": "subscription", "status": _STATUS.get(inv["status"], "draft"),
        "payment_status": _payment_status(inv), "currency": CURRENCY,
        "fees_amount_cents": inv["usage_amount"],
        "coupons_amount_cents": inv.get("credit_total") or 0,
        "credit_notes_amount_cents": 0,
        "sub_total_excluding_taxes_amount_cents": inv["subtotal"],
        "taxes_amount_cents": inv["vat"],
        "sub_total_including_taxes_amount_cents": inv["total"],
        "prepaid_credit_amount_cents": 0, "prepaid_granted_credit_amount_cents": 0,
        "prepaid_purchased_credit_amount_cents": 0,
        "progressive_billing_credit_amount_cents": 0,
        "total_amount_cents": inv["total"], "total_paid_amount_cents": paid,
        "total_offsetted_credit_note_amount_cents": 0,
        "total_due_amount_cents": inv["total"] - paid, "version_number": 4,
        "self_billed": False, "file_url": None, "xml_url": None, "web_url": None,
        "created_at": inv["created_at"], "updated_at": inv["updated_at"],
        "voided_at": inv.get("voided_at"), "customer": cust,
        "metadata": [{"lago_id": _lid("invoice_metadata", inv["id"]),
                      "key": "neocloud_period", "value": inv["period"],
                      "created_at": inv["created_at"]}],
        "applied_taxes": [{"lago_id": _lid("applied_tax", inv["id"]),
                           "lago_invoice_id": lago_inv, "lago_tax_id": vat["lago_id"],
                           "tax_name": vat["name"], "tax_code": vat["code"],
                           "tax_rate": vat["rate"],
                           "tax_description": vat["description"],
                           "amount_cents": inv["vat"], "amount_currency": CURRENCY,
                           "fees_amount_cents": inv["subtotal"],
                           "created_at": inv["created_at"]}],
        "applied_invoice_custom_sections": [], "applied_usage_thresholds": [],
        "fees": fees,
        "subscriptions": [_tbl("subscriptions")[s] for s in subs
                          if s in _tbl("subscriptions")],
        "credits": [],
    }


def _invoice_by_lago_id(lago_id: str) -> dict:
    for inv in STORE.invoices.values():
        if _lid("invoice", inv["id"]) == lago_id or inv["id"] == lago_id:
            return inv
    raise LagoError(404, "invoice_not_found")


# facade hooks (billing.py) ----------------------------------------------------
def on_invoice(kind: str, inv: dict, payment: Optional[dict] = None) -> None:
    obj = invoice_object(inv)
    refs = {"tenant_id": inv["tenant_id"], "invoice_id": inv["id"]}
    if kind == "drafted":
        wh.emit_lago("invoice.drafted", "invoice", obj, **refs)
    elif kind == "finalized":
        wh.emit_lago("invoice.created", "invoice", obj, **refs)
    elif kind == "voided":
        wh.emit_lago("invoice.voided", "invoice", obj, **refs)
    elif kind == "paid":
        wh.emit_lago("invoice.payment_status_updated", "invoice", obj, **refs)
    elif kind == "payment_failed":
        cust = obj["customer"]
        err = (payment or {}).get("last_payment_error") or {}
        wh.emit_lago("invoice.payment_failure", "payment_provider_invoice_payment_error",
                     {"lago_invoice_id": obj["lago_id"],
                      "lago_customer_id": cust["lago_id"],
                      "external_customer_id": cust["external_id"],
                      "provider_customer_id":
                          cust["billing_configuration"]["provider_customer_id"],
                      "payment_provider": "stripe",
                      "payment_provider_code": "stripe_test",
                      "provider_error": {"message": err.get("message"),
                                         "error_code": err.get("code")},
                      "error_details": None}, **refs)
        wh.emit_lago("invoice.payment_status_updated", "invoice", obj, **refs)


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
def _guarded(fn):
    """Operator check, JSON body (read outside the lock), Lago error envelope.
    The wrapped handler is sync: fn(request, body, **path_params)."""
    import inspect
    sig = inspect.signature(fn)
    visible = [p for n, p in sig.parameters.items() if n != "body"]

    async def wrapper(request: Request, **kw):
        ident = getattr(request.state, "identity", None)
        if ident is not None and ident.role != "operator":
            return JSONResponse(LagoError(401).body, status_code=401)
        try:
            body = {}
            if request.method in ("POST", "PUT"):
                raw = await request.body()
                if raw:
                    import json
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        raise LagoError(400, "invalid_json")
            with STORE.lock:
                return fn(request, body, **kw)
        except LagoError as e:
            return JSONResponse(e.body, status_code=e.status)
    wrapper.__name__ = fn.__name__
    wrapper.__signature__ = sig.replace(parameters=visible)
    return wrapper


def _root(body: dict, key: str) -> dict:
    obj = body.get(key)
    if not isinstance(obj, dict):
        raise LagoError(400, "bad_request", {key: ["value_is_mandatory"]})
    return obj


@router.post("/billable_metrics")
@_guarded
def http_create_bm(request: Request, body: dict):
    b = _root(body, "billable_metric")
    _mandatory(b, "name", "code", "aggregation_type")
    if b["aggregation_type"] not in ("count_agg", "sum_agg", "max_agg",
                                     "unique_count_agg", "weighted_sum_agg",
                                     "latest_agg"):
        raise LagoError(422, "validation_errors",
                        {"aggregation_type": ["value_is_invalid"]})
    t = billable_metrics()
    if b["code"] in t:
        raise LagoError(422, "validation_errors", {"code": ["value_already_exist"]})
    t[b["code"]] = _metric_obj(b["code"], b["name"], b.get("field_name"),
                               b.get("description"), b["aggregation_type"],
                               bool(b.get("recurring")))
    return {"billable_metric": t[b["code"]]}


@router.get("/billable_metrics")
@_guarded
def http_list_bm(request: Request, body: dict):
    return _paginate(request, "billable_metrics", list(billable_metrics().values()))


@router.get("/billable_metrics/{code}")
@_guarded
def http_get_bm(request: Request, body: dict, code: str):
    bm = billable_metrics().get(code)
    if bm is None:
        raise LagoError(404, "billable_metric_not_found")
    return {"billable_metric": bm}


@router.post("/plans")
@_guarded
def http_create_plan(request: Request, body: dict):
    b = _root(body, "plan")
    _mandatory(b, "name", "code", "interval", "amount_currency")
    if b.get("amount_cents") is None:
        raise LagoError(422, "validation_errors", {"amount_cents": ["value_is_mandatory"]})
    if b["amount_currency"] != CURRENCY:
        raise LagoError(422, "validation_errors",
                        {"amount_currency": ["value_is_invalid"]})
    t = plans()
    if b["code"] in t:
        raise LagoError(422, "validation_errors", {"code": ["value_already_exist"]})
    charges = []
    for ch in b.get("charges") or []:
        bm = next((m for m in billable_metrics().values()
                   if m["lago_id"] == ch.get("billable_metric_id")
                   or m["code"] == ch.get("billable_metric_code")), None)
        if bm is None:
            raise LagoError(404, "billable_metric_not_found")
        charges.append(_charge(b["code"], bm["code"],
                               (ch.get("properties") or {}).get("amount", "0"),
                               ch.get("charge_model") or "standard"))
    plan = _plan_obj(b["code"], b["name"], charges, b.get("description"))
    plan.update(interval=b["interval"], amount_cents=int(b["amount_cents"]),
                pay_in_advance=bool(b.get("pay_in_advance")))
    if b.get("minimum_commitment"):
        mc = b["minimum_commitment"]
        plan["minimum_commitment"] = {
            "lago_id": _lid("commitment", b["code"]), "plan_code": b["code"],
            "amount_cents": int(mc.get("amount_cents") or 0),
            "invoice_display_name": mc.get("invoice_display_name"),
            "interval": b["interval"], "created_at": plan["created_at"],
            "updated_at": plan["created_at"], "taxes": []}
    t[b["code"]] = plan
    return {"plan": plan}


@router.get("/plans")
@_guarded
def http_list_plans(request: Request, body: dict):
    return _paginate(request, "plans", list(plans().values()))


@router.get("/plans/{code}")
@_guarded
def http_get_plan(request: Request, body: dict, code: str):
    p = plans().get(code)
    if p is None:
        raise LagoError(404, "plan_not_found")
    return {"plan": p}


@router.post("/customers")
@_guarded
def http_upsert_customer(request: Request, body: dict):
    b = _root(body, "customer")
    _mandatory(b, "external_id")
    obj = ensure_customer(b["external_id"], b)
    emit("NeoCloudOS.M7", "Lago", "REST", "POST /api/v1/customers",
         f"Lago 고객 동기화 — {b['external_id']} ({obj['lago_id'][:8]})",
         payload={"external_id": b["external_id"]})
    return {"customer": obj}


@router.get("/customers")
@_guarded
def http_list_customers(request: Request, body: dict):
    return _paginate(request, "customers", list(_tbl("customers").values()))


@router.get("/customers/{external_id}")
@_guarded
def http_get_customer(request: Request, body: dict, external_id: str):
    c = _tbl("customers").get(external_id)
    if c is None:
        raise LagoError(404, "customer_not_found")
    return {"customer": c}


@router.get("/customers/{external_customer_id}/invoices")
@_guarded
def http_customer_invoices(request: Request, body: dict, external_customer_id: str):
    if external_customer_id not in _tbl("customers"):
        raise LagoError(404, "customer_not_found")
    invs = sorted((i for i in STORE.invoices.values()
                   if i["tenant_id"] == external_customer_id),
                  key=lambda i: i["id"], reverse=True)
    return _paginate(request, "invoices", [invoice_object(i) for i in invs])


@router.post("/subscriptions")
@_guarded
def http_create_subscription(request: Request, body: dict):
    b = _root(body, "subscription")
    _mandatory(b, "external_customer_id", "plan_code", "external_id")
    plan = plans().get(b["plan_code"])
    if plan is None:
        raise LagoError(404, "plan_not_found")
    cur = _tbl("subscriptions").get(b["external_id"])
    if cur is not None:                     # Lago returns the existing one
        return {"subscription": cur}
    customer = ensure_customer(b["external_customer_id"])
    at = _event_ts(b.get("subscription_at")) if b.get("subscription_at") else None
    end = _event_ts(b.get("ending_at")) if b.get("ending_at") else None
    sub = _put_subscription(_subscription_obj(b["external_id"], customer, plan,
                                              b.get("name"), at, end))
    return {"subscription": sub}


@router.get("/subscriptions")
@_guarded
def http_list_subscriptions(request: Request, body: dict):
    ext = request.query_params.get("external_customer_id")
    st = request.query_params.getlist("status[]") or ["active"]
    subs = [s for s in _tbl("subscriptions").values()
            if (not ext or s["external_customer_id"] == ext) and s["status"] in st]
    return _paginate(request, "subscriptions", subs)


@router.get("/subscriptions/{external_id}")
@_guarded
def http_get_subscription(request: Request, body: dict, external_id: str):
    s = _tbl("subscriptions").get(external_id)
    if s is None:
        raise LagoError(404, "subscription_not_found")
    return {"subscription": s}


@router.delete("/subscriptions/{external_id}")
@_guarded
def http_terminate_subscription(request: Request, body: dict, external_id: str):
    s = _tbl("subscriptions").get(external_id)
    if s is None:
        raise LagoError(404, "subscription_not_found")
    c = STORE.contracts.get(external_id)
    if c is not None and c["status"] == "active":     # keep F06 in step
        c.update(status="terminated", terminated_at=_now().isoformat(),
                 updated_at=_now().isoformat())
    return {"subscription": terminate_subscription(external_id)}


@router.post("/events")
@_guarded
def http_create_event(request: Request, body: dict):
    obj = ingest(_root(body, "event"))
    return {"event": _accepted(obj)}


@router.post("/events/batch")
@_guarded
def http_batch_events(request: Request, body: dict):
    evs = body.get("events")
    if not isinstance(evs, list) or not evs:
        raise LagoError(422, "validation_errors", {"events": ["value_is_mandatory"]})
    if len(evs) > BATCH_MAX:
        raise LagoError(422, "validation_errors", {"events": ["too_many_events"]})
    errors = {}
    for i, ev in enumerate(evs):
        missing = {f: ["value_is_mandatory"] for f in
                   ("transaction_id", "external_subscription_id", "code")
                   if not isinstance(ev, dict) or ev.get(f) in (None, "")}
        if missing:
            errors[str(i)] = missing
    if errors:                                # Lago rejects the whole batch
        raise LagoError(422, "validation_errors", errors)
    out = [_accepted(ingest(ev)) for ev in evs]
    emit("NeoCloudOS.M7", "Lago", "REST", "POST /api/v1/events/batch",
         f"Lago 이벤트 {len(out)}건 수집", payload={"count": len(out)})
    return {"events": out}


def _accepted(obj: dict) -> dict:
    """POST responses: Lago processes events asynchronously, so the
    customer is not resolved yet (lago_customer_id is always null there)."""
    return {**obj, "lago_customer_id": None}


@router.get("/events/{transaction_id}")
@_guarded
def http_get_event(request: Request, body: dict, transaction_id: str):
    e = _tbl("events").get(transaction_id)
    if e is None:
        raise LagoError(404, "event_not_found")
    return {"event": e["object"]}


@router.get("/invoices")
@_guarded
def http_list_invoices(request: Request, body: dict):
    ext = request.query_params.get("external_customer_id")
    st = request.query_params.get("status")
    out = [invoice_object(i) for i in sorted(STORE.invoices.values(),
                                             key=lambda i: i["id"], reverse=True)
           if not ext or i["tenant_id"] == ext]
    if st:
        out = [i for i in out if i["status"] == st]
    return _paginate(request, "invoices", out)


@router.get("/invoices/{lago_id}")
@_guarded
def http_get_invoice(request: Request, body: dict, lago_id: str):
    return {"invoice": invoice_object(_invoice_by_lago_id(lago_id))}


@router.put("/invoices/{lago_id}/finalize")
@_guarded
def http_finalize_invoice(request: Request, body: dict, lago_id: str):
    from . import billing
    inv = _invoice_by_lago_id(lago_id)
    if inv["status"] == "draft":
        billing.finalize(inv)
    return {"invoice": invoice_object(inv)}


@router.post("/invoices/{lago_id}/void")
@_guarded
def http_void_invoice(request: Request, body: dict, lago_id: str):
    from . import billing
    inv = _invoice_by_lago_id(lago_id)
    if inv["status"] not in ("finalized",):
        raise LagoError(405, "not_voidable")
    billing.void(inv)
    return {"invoice": invoice_object(inv)}


@router.post("/invoices/{lago_id}/retry_payment")
@_guarded
def http_retry_payment(request: Request, body: dict, lago_id: str):
    from . import billing
    inv = _invoice_by_lago_id(lago_id)
    if inv["status"] != "finalized":
        raise LagoError(405, "invalid_status")
    pm = next((p for p in STORE.payment_methods.values()
               if p["tenant_id"] == inv["tenant_id"] and p["default"]), None)
    if pm is not None:
        billing.charge(inv, pm)             # outcome goes out as webhooks
    return {"invoice": invoice_object(inv)}


@router.post("/webhook_endpoints")
@_guarded
def http_create_webhook_endpoint(request: Request, body: dict):
    b = _root(body, "webhook_endpoint")
    _mandatory(b, "webhook_url")
    algo = b.get("signature_algo") or "hmac"
    if algo != "hmac":                       # jwt needs Lago's RSA key pair
        raise LagoError(422, "validation_errors", {"signature_algo": ["value_is_invalid"]})
    eps = wh.endpoints("lago")
    if any(e["webhook_url"] == b["webhook_url"] for e in eps.values()):
        raise LagoError(422, "validation_errors", {"webhook_url": ["value_already_exist"]})
    eid = _lid("webhook_endpoint", b["webhook_url"])
    eps[eid] = {"lago_id": eid, "lago_organization_id": wh.LAGO_ORG_ID,
                "webhook_url": b["webhook_url"], "signature_algo": algo,
                "name": b.get("name"), "event_types": b.get("event_types"),
                "created_at": _iso(_now())}
    return {"webhook_endpoint": eps[eid]}


@router.get("/webhook_endpoints")
@_guarded
def http_list_webhook_endpoints(request: Request, body: dict):
    return _paginate(request, "webhook_endpoints", list(wh.endpoints("lago").values()))
