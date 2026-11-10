"""F05/F06 contract tests — Lago and Stripe shapes (billing_lago/_stripe/_webhooks).

Two levels:
  1. Always: the required fields and enums of each upstream object, taken from
     the pinned specs (Lago getlago/lago-openapi@7d237882f6ef openapi.yaml,
     Stripe stripe/openapi@f1ec696a9d04 latest/openapi.spec3.yaml). Only the
     field names are listed here — Lago is AGPL, so no spec text is copied.
  2. When the spec files are present (env NEOCLOUD_LAGO_OPENAPI /
     NEOCLOUD_STRIPE_OPENAPI = path to the downloaded yaml): full schema
     validation of every response and webhook body against the component
     schema ($ref / allOf / anyOf / type / nullable / enum / required).
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app import billing_webhooks as wh
from app.store import STORE

# ---------------------------------------------------------------------------
# required fields (from the pinned specs)
# ---------------------------------------------------------------------------
LAGO_REQUIRED = {
    "EventObject": ["transaction_id", "lago_customer_id", "code", "timestamp",
                    "lago_subscription_id", "external_subscription_id"],
    "BillableMetricObject": ["lago_id", "name", "code", "description", "recurring",
                             "rounding_function", "rounding_precision", "created_at",
                             "expression", "field_name", "aggregation_type",
                             "weighted_interval", "filters"],
    "PlanObject": ["lago_id", "name", "created_at", "code", "interval",
                   "amount_cents", "amount_currency"],
    "ChargeObject": ["lago_id", "lago_billable_metric_id", "billable_metric_code",
                     "created_at", "charge_model", "invoiceable", "regroup_paid_fees",
                     "pay_in_advance", "prorated", "min_amount_cents", "properties",
                     "filters"],
    "MinimumCommitmentObject": ["lago_id", "amount_cents", "created_at"],
    "CustomerObject": [
        "lago_id", "sequential_id", "slug", "external_id", "billing_entity_code",
        "address_line1", "address_line2", "applicable_timezone", "city", "country",
        "currency", "email", "legal_name", "legal_number", "logo_url", "name",
        "firstname", "lastname", "account_type", "customer_type", "phone", "state",
        "tax_identification_number", "timezone", "url", "zipcode",
        "net_payment_term", "external_salesforce_id", "created_at", "updated_at",
        "finalize_zero_amount_invoice", "skip_invoice_custom_sections",
        "billing_configuration", "shipping_address", "metadata"],
    "SubscriptionObject": [
        "activated_at", "activation_rules", "billing_time", "canceled_at",
        "cancellation_reason", "consolidate_invoice", "created_at",
        "current_billing_period_ending_at", "current_billing_period_started_at",
        "downgrade_plan_date", "ending_at", "external_customer_id", "external_id",
        "lago_customer_id", "lago_id", "name", "next_plan_code",
        "on_termination_credit_note", "on_termination_invoice", "payment_method",
        "plan_amount_cents", "plan_amount_currency", "plan_code",
        "previous_plan_code", "progressive_billing_disabled",
        "purchase_order_number", "started_at", "status", "subscription_at",
        "terminated_at", "trial_ended_at"],
    "InvoiceObject": [
        "lago_id", "billing_entity_code", "sequential_id", "number",
        "purchase_order_number", "issuing_date", "payment_dispute_lost_at",
        "payment_due_date", "payment_overdue", "net_payment_term", "invoice_type",
        "status", "payment_status", "currency", "fees_amount_cents",
        "coupons_amount_cents", "credit_notes_amount_cents",
        "sub_total_excluding_taxes_amount_cents", "taxes_amount_cents",
        "sub_total_including_taxes_amount_cents", "prepaid_credit_amount_cents",
        "prepaid_granted_credit_amount_cents",
        "prepaid_purchased_credit_amount_cents",
        "progressive_billing_credit_amount_cents", "total_amount_cents",
        "total_paid_amount_cents", "total_offsetted_credit_note_amount_cents",
        "total_due_amount_cents", "version_number", "self_billed", "file_url",
        "xml_url", "web_url", "created_at", "updated_at", "voided_at"],
    "FeeObject": ["item", "amount_cents", "amount_currency", "taxes_amount_cents",
                  "taxes_rate", "total_amount_cents", "total_amount_currency",
                  "pay_in_advance", "invoiceable", "units", "total_aggregated_units",
                  "precise_unit_amount", "sub_total_excluding_taxes_amount_cents",
                  "sub_total_excluding_taxes_precise_amount_cents", "payment_status"],
    "FeeItem": ["type", "code", "name", "description", "invoice_display_name",
                "filters", "filter_invoice_display_name", "lago_item_id",
                "item_type", "grouped_by"],
    "InvoiceAppliedTaxObject": ["lago_id", "lago_tax_id", "tax_name", "tax_code",
                                "tax_rate", "tax_description", "amount_cents",
                                "amount_currency", "created_at"],
    "WebhookEndpointObject": ["lago_id", "lago_organization_id", "webhook_url",
                              "signature_algo", "name", "event_types", "created_at"],
    "PaginationMeta": ["current_page", "total_pages", "total_count"],
    "ApiErrorUnprocessableEntity": ["status", "error", "code", "error_details"],
    "ApiErrorNotFound": ["status", "error", "code"],
    "Webhook": ["webhook_type", "object_type", "organization_id"],
    "EventErrorsObject": ["status", "error", "message", "event"],
    "InvoicePaymentFailureObject": ["lago_invoice_id", "lago_customer_id",
                                    "external_customer_id", "provider_customer_id",
                                    "payment_provider", "payment_provider_code",
                                    "provider_error", "error_details"],
}
LAGO_ENUMS = {
    "InvoiceObject.status": ["draft", "finalized", "voided", "failed", "pending",
                             "deleted"],
    "InvoiceObject.payment_status": ["pending", "succeeded", "failed"],
    "InvoiceObject.invoice_type": ["subscription", "add_on", "credit", "one_off",
                                   "advance_charges", "progressive_billing"],
    "SubscriptionObject.status": ["active", "canceled", "incomplete", "pending",
                                  "terminated"],
    "FeeItem.type": ["charge", "fixed_charge", "add_on", "subscription", "credit",
                     "commitment"],
    "FeeItem.item_type": ["AddOn", "BillableMetric", "Subscription",
                          "WalletTransaction"],
    "BillableMetricObject.aggregation_type": ["count_agg", "sum_agg", "max_agg",
                                              "unique_count_agg", "weighted_sum_agg",
                                              "latest_agg"],
    "PlanObject.interval": ["weekly", "monthly", "quarterly", "semiannual", "yearly"],
}
# webhook_type → (object_type, component schema of the object)
LAGO_WEBHOOKS = {
    "customer.created": ("customer", "CustomerObject"),
    "subscription.started": ("subscription", "SubscriptionWithCustomerObject"),
    "subscription.terminated": ("subscription", "SubscriptionWithCustomerObject"),
    "invoice.drafted": ("invoice", "InvoiceObjectExtended"),
    "invoice.created": ("invoice", "InvoiceObjectExtended"),
    "invoice.voided": ("invoice", "InvoiceObjectExtended"),
    "invoice.payment_status_updated": ("invoice", "InvoiceObjectExtended"),
    "invoice.payment_failure": ("payment_provider_invoice_payment_error",
                                "InvoicePaymentFailureObject"),
    "event.error": ("event_error", "EventErrorsObject"),
}
STRIPE_REQUIRED = {
    "payment_intent": ["created", "id", "livemode", "object", "status"],
    "invoice": ["amount_due", "amount_overpaid", "amount_paid",
                "amount_paid_off_stripe", "amount_remaining", "amount_shipping",
                "attempt_count", "attempted", "auto_advance", "automatic_tax",
                "collection_method", "created", "currency", "customer",
                "default_tax_rates", "discounts", "id", "issuer", "lines",
                "livemode", "object", "payment_settings", "period_end",
                "period_start", "post_payment_credit_notes_amount",
                "pre_payment_credit_notes_amount", "starting_balance",
                "status_transitions", "subtotal", "total"],
    "line_item": ["amount", "currency", "discountable", "discounts", "id",
                  "livemode", "metadata", "object", "period", "subtotal"],
    "event": ["created", "data", "id", "livemode", "object", "pending_webhooks",
              "type"],
    "customer": ["created", "id", "livemode", "object"],
    "payment_method": ["billing_details", "created", "id", "livemode", "object",
                       "type"],
    "error": ["error"],
    "api_errors": ["type"],
}
STRIPE_ENUMS = {
    "payment_intent.status": ["canceled", "processing", "requires_action",
                              "requires_capture", "requires_confirmation",
                              "requires_payment_method", "succeeded"],
    "invoice.status": ["draft", "open", "paid", "uncollectible", "void"],
    "api_errors.type": ["api_error", "card_error", "idempotency_error",
                        "invalid_request_error"],
}


def _has(obj: dict, name: str, table: dict) -> None:
    missing = [k for k in table[name] if k not in obj]
    assert not missing, f"{name} missing {missing}"


def _enum(obj: dict, key: str, table: dict) -> None:
    name, field = key.split(".")
    assert obj[field] in table[key], f"{key}={obj[field]!r}"


# ---------------------------------------------------------------------------
# optional full-spec validation (spec files are not committed)
# ---------------------------------------------------------------------------
def _load(env: str):
    path = os.environ.get(env)
    if not path or not os.path.exists(path):
        return None
    import yaml
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(path) as f:
        return yaml.load(f, Loader=loader)["components"]["schemas"]


LAGO_SPEC = _load("NEOCLOUD_LAGO_OPENAPI")
STRIPE_SPEC = _load("NEOCLOUD_STRIPE_OPENAPI")

_PYTYPE = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
           "object": dict, "array": list, "null": type(None)}


def validate(inst, schema: dict, comps: dict, path: str = "$") -> list[str]:
    """Small JSON-Schema/OpenAPI subset validator (enough for these specs)."""
    if "$ref" in schema:
        return validate(inst, comps[schema["$ref"].split("/")[-1]], comps, path)
    errs: list[str] = []
    for sub in schema.get("allOf", []):
        errs += validate(inst, sub, comps, path)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            if not any(not validate(inst, s, comps, path) for s in schema[key]):
                if not (inst is None and schema.get("nullable")):
                    errs.append(f"{path}: matches no {key}")
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if schema.get("nullable"):
            types = types + ["null"]
        ok = any(isinstance(inst, _PYTYPE[x]) and not (x in ("integer", "number")
                 and isinstance(inst, bool)) for x in types)
        if not ok:
            return errs + [f"{path}: {type(inst).__name__} is not {types}"]
    nullable = schema.get("nullable") or (isinstance(t, list) and "null" in t)
    if "enum" in schema and inst not in schema["enum"] and not (
            inst is None and (nullable or None in schema["enum"])):
        errs.append(f"{path}: {inst!r} not in enum")
    if isinstance(inst, dict):
        for k in schema.get("required", []):
            if k not in inst:
                errs.append(f"{path}: missing {k}")
        for k, sub in (schema.get("properties") or {}).items():
            if k in inst:
                errs += validate(inst[k], sub, comps, f"{path}.{k}")
    if isinstance(inst, list) and isinstance(schema.get("items"), dict):
        for i, x in enumerate(inst):
            errs += validate(x, schema["items"], comps, f"{path}[{i}]")
    return errs


def _full(spec, name: str, obj) -> None:
    if spec is None:
        return
    errs = validate(obj, {"$ref": f"#/components/schemas/{name}"}, spec)
    assert not errs, f"{name}: " + "; ".join(errs[:10])


# ---------------------------------------------------------------------------
# scenario
# ---------------------------------------------------------------------------
def _last_month() -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    p0 = (first - timedelta(days=1)).replace(day=1)
    return p0.strftime("%Y-%m"), p0


@pytest.fixture()
def flow(client):
    period, p0 = _last_month()
    tid = client.post("/api/v1/tenants", json={
        "name": "lago-corp", "isolation_tier": "bare_metal_dedicated"}).json()["id"]
    cus = client.post("/lago/api/v1/customers", json={"customer": {
        "external_id": tid, "name": "Lago Corp", "currency": "KRW",
        "legal_number": "214-86-00001",
        "billing_configuration": {"payment_provider": "stripe"}}})
    assert cus.status_code == 200, cus.text
    c = client.post("/api/v1/contracts", json={
        "tenant_id": tid, "product": "token-api", "commitment": "reserved",
        "term_months": 12, "start_date": p0.date().isoformat(), "unit_price": 0,
        "min_monthly_amount": 1_000_000}).json()
    assert client.post(f"/api/v1/contracts/{c['id']}/activate").status_code == 200
    ts = int((p0 + timedelta(days=3)).timestamp())
    batch = {"events": [
        {"transaction_id": "req-001:input", "external_subscription_id": c["id"],
         "code": "input_tokens", "timestamp": ts,
         "properties": {"tokens": 500_000_000, "model": "llama-3.3-70b"}},
        {"transaction_id": "req-001:output", "external_subscription_id": c["id"],
         "code": "output_tokens", "timestamp": ts,
         "properties": {"tokens": 250_000_000, "model": "llama-3.3-70b"}}]}
    ev = client.post("/lago/api/v1/events/batch", json=batch)
    assert ev.status_code == 200, ev.text
    run = client.post("/api/v1/billing/invoices:run",
                      json={"period": period, "tenant_id": tid}).json()
    inv_id = run["created"][0]
    return {"tid": tid, "contract": c, "period": period, "inv_id": inv_id,
            "batch": batch, "events": ev.json(), "customer": cus.json()}


def test_lago_objects_match_spec(client, flow):
    cus = flow["customer"]["customer"]
    _has(cus, "CustomerObject", LAGO_REQUIRED)
    _full(LAGO_SPEC, "CustomerObject", cus)
    assert cus["external_id"] == flow["tid"] and cus["currency"] == "KRW"
    bms = client.get("/lago/api/v1/billable_metrics").json()
    _has(bms["meta"], "PaginationMeta", LAGO_REQUIRED)
    _full(LAGO_SPEC, "BillableMetricsPaginated", bms)
    codes = {m["code"] for m in bms["billable_metrics"]}
    assert {"input_tokens", "output_tokens", "rack_hour"} <= codes
    for m in bms["billable_metrics"]:
        _has(m, "BillableMetricObject", LAGO_REQUIRED)
        _enum(m, "BillableMetricObject.aggregation_type", LAGO_ENUMS)
    plan = client.get(f"/lago/api/v1/plans/neocloud_contract_{flow['contract']['id']}"
                      ).json()["plan"]
    _has(plan, "PlanObject", LAGO_REQUIRED)
    _enum(plan, "PlanObject.interval", LAGO_ENUMS)
    _has(plan["minimum_commitment"], "MinimumCommitmentObject", LAGO_REQUIRED)
    assert plan["minimum_commitment"]["amount_cents"] == 1_000_000
    for ch in plan["charges"]:
        _has(ch, "ChargeObject", LAGO_REQUIRED)
    _full(LAGO_SPEC, "PlanObject", plan)
    sub = client.get(f"/lago/api/v1/subscriptions/{flow['contract']['id']}"
                     ).json()["subscription"]
    _has(sub, "SubscriptionObject", LAGO_REQUIRED)
    _enum(sub, "SubscriptionObject.status", LAGO_ENUMS)
    assert sub["status"] == "active" and sub["external_customer_id"] == flow["tid"]
    _full(LAGO_SPEC, "SubscriptionObject", sub)
    for e in flow["events"]["events"]:
        _has(e, "EventObject", LAGO_REQUIRED)
        _full(LAGO_SPEC, "EventObject", e)
    _full(LAGO_SPEC, "EventsCreated", flow["events"])


def test_lago_invoice_finalize_and_amounts(client, flow):
    lago_inv = client.get(f"/lago/api/v1/invoices/{flow['inv_id']}").json()["invoice"]
    _has(lago_inv, "InvoiceObject", LAGO_REQUIRED)
    for k in ("status", "payment_status", "invoice_type"):
        _enum(lago_inv, f"InvoiceObject.{k}", LAGO_ENUMS)
    assert lago_inv["status"] == "draft"
    # tokens 500M × ₩800/1M + 250M × ₩3,200/1M = ₩1,200,000 ≥ ₩1,000,000 commitment
    by_code = {f["item"]["code"]: f for f in lago_inv["fees"]}
    assert by_code["input_tokens"]["amount_cents"] == 400_000
    assert by_code["output_tokens"]["amount_cents"] == 800_000
    assert "minimum_commitment" not in by_code            # usage above commitment
    assert lago_inv["fees_amount_cents"] == 1_200_000
    assert lago_inv["taxes_amount_cents"] == 120_000
    assert lago_inv["total_amount_cents"] == 1_320_000
    for f in lago_inv["fees"]:
        _has(f, "FeeObject", LAGO_REQUIRED)
        _has(f["item"], "FeeItem", LAGO_REQUIRED)
        _enum(f["item"], "FeeItem.type", LAGO_ENUMS)
        _enum(f["item"], "FeeItem.item_type", LAGO_ENUMS)
    _has(lago_inv["applied_taxes"][0], "InvoiceAppliedTaxObject", LAGO_REQUIRED)
    _full(LAGO_SPEC, "InvoiceObjectExtended", lago_inv)
    r = client.put(f"/lago/api/v1/invoices/{lago_inv['lago_id']}/finalize")
    assert r.status_code == 200, r.text
    fin = r.json()["invoice"]
    assert fin["status"] == "finalized" and fin["payment_status"] == "pending"
    facade = client.get(f"/api/v1/billing/invoices/{flow['inv_id']}").json()
    assert facade["status"] == "finalized" and facade["tax_invoice"]["approval_no"]
    listed = client.get("/lago/api/v1/invoices",
                        params={"external_customer_id": flow["tid"]}).json()
    _full(LAGO_SPEC, "InvoicesPaginated", listed)
    assert [i["lago_id"] for i in listed["invoices"]] == [fin["lago_id"]]


def test_commitment_true_up_is_a_commitment_fee(client):
    period, p0 = _last_month()
    tid = client.post("/api/v1/tenants", json={
        "name": "small-corp", "isolation_tier": "bare_metal_dedicated"}).json()["id"]
    c = client.post("/api/v1/contracts", json={
        "tenant_id": tid, "product": "token-api", "term_months": 12,
        "start_date": p0.date().isoformat(), "unit_price": 0,
        "min_monthly_amount": 1_000_000}).json()
    client.post(f"/api/v1/contracts/{c['id']}/activate")
    client.post("/lago/api/v1/events", json={"event": {
        "transaction_id": "small-1", "external_subscription_id": c["id"],
        "code": "input_tokens", "timestamp": int((p0 + timedelta(days=1)).timestamp()),
        "properties": {"tokens": 1_000_000}}})
    inv_id = client.post("/api/v1/billing/invoices:run",
                         json={"period": period, "tenant_id": tid}).json()["created"][0]
    lago_inv = client.get(f"/lago/api/v1/invoices/{inv_id}").json()["invoice"]
    commit = [f for f in lago_inv["fees"] if f["item"]["type"] == "commitment"]
    assert len(commit) == 1 and commit[0]["amount_cents"] == 1_000_000 - 800
    assert lago_inv["fees_amount_cents"] == 1_000_000


def test_lago_event_validation_and_idempotency(client, flow):
    # required fields → Lago 422 validation_errors
    r = client.post("/lago/api/v1/events", json={"event": {"code": "input_tokens"}})
    assert r.status_code == 422
    body = r.json()
    _has(body, "ApiErrorUnprocessableEntity", LAGO_REQUIRED)
    assert body["code"] == "validation_errors"
    assert body["error_details"]["transaction_id"] == ["value_is_mandatory"]
    _full(LAGO_SPEC, "ApiErrorUnprocessableEntity", body)
    # a resent batch is accepted and not billed twice
    again = client.post("/lago/api/v1/events/batch", json=flow["batch"])
    assert again.status_code == 200
    run = client.post("/api/v1/billing/invoices:run",
                      json={"period": flow["period"], "tenant_id": flow["tid"]}).json()
    assert run["updated"] == [flow["inv_id"]]
    assert client.get(f"/api/v1/billing/invoices/{flow['inv_id']}"
                      ).json()["usage_amount"] == 1_200_000
    # unknown code → 200, then event.error webhook (async validation in Lago)
    r = client.post("/lago/api/v1/events", json={"event": {
        "transaction_id": "x-1", "external_subscription_id": flow["contract"]["id"],
        "code": "no_such_metric", "properties": {}}})
    assert r.status_code == 200
    err = wh.deliveries("lago")[-1]
    assert err["type"] == "event.error"
    _has(err["body"]["event_error"], "EventErrorsObject", LAGO_REQUIRED)
    nf = client.get("/lago/api/v1/customers/nobody")
    assert nf.status_code == 404
    _has(nf.json(), "ApiErrorNotFound", LAGO_REQUIRED)


def test_lago_webhooks_signed_and_shaped(client, flow):
    inv = flow["inv_id"]
    client.post(f"/api/v1/billing/invoices/{inv}/finalize")
    client.post("/api/v1/billing/payment-methods", json={
        "tenant_id": flow["tid"], "kind": "card", "token": "tok_visa_4242"})
    assert client.post(f"/api/v1/billing/invoices/{inv}/pay").status_code == 200
    types = [r["type"] for r in wh.deliveries("lago", tenant_id=flow["tid"])]
    for want in ("customer.created", "subscription.started", "invoice.drafted",
                 "invoice.created", "invoice.payment_status_updated"):
        assert want in types, (want, types)
    for rec in wh.deliveries("lago"):
        body, raw = rec["body"], rec["raw"]
        _has(body, "Webhook", LAGO_REQUIRED)
        assert body["webhook_type"] == rec["type"]
        obj_type, schema = LAGO_WEBHOOKS[rec["type"]]
        assert body["object_type"] == obj_type and obj_type in body
        assert rec["headers"]["X-Lago-Signature-Algorithm"] == "hmac"
        assert wh.verify_lago(raw, rec["headers"]["X-Lago-Signature"])
        assert not wh.verify_lago(raw + " ", rec["headers"]["X-Lago-Signature"])
        assert rec["deliveries"][0]["verified"] is True
        _full(LAGO_SPEC, schema, body[obj_type])
    paid = wh.deliveries("lago", type=None, invoice_id=inv)[-1]["body"]["invoice"]
    assert paid["payment_status"] == "succeeded" and paid["total_due_amount_cents"] == 0
    # facade log endpoint shows the raw headers
    log = client.get("/api/v1/billing/webhooks", params={"invoice_id": inv}).json()
    assert {"invoice.created", "payment_intent.succeeded"} <= {r["type"] for r in log}
    assert all("headers" in r and "body" in r for r in log)


def test_stripe_payment_intent_and_events(client, flow):
    inv = flow["inv_id"]
    client.post(f"/api/v1/billing/invoices/{inv}/finalize")
    client.post("/api/v1/billing/payment-methods", json={
        "tenant_id": flow["tid"], "kind": "card", "token": "tok_fail_0002"})
    r = client.post(f"/api/v1/billing/invoices/{inv}/pay")
    assert r.status_code == 402
    failed = r.json()["detail"]["payment"]
    assert failed["status"] == "requires_payment_method"
    assert failed["last_payment_error"]["code"] == "card_declined"
    lago = [x["type"] for x in wh.deliveries("lago", invoice_id=inv)]
    assert "invoice.payment_failure" in lago
    pf = [x for x in wh.deliveries("lago", invoice_id=inv)
          if x["type"] == "invoice.payment_failure"][0]["body"]
    _has(pf["payment_provider_invoice_payment_error"], "InvoicePaymentFailureObject",
         LAGO_REQUIRED)
    good = client.post("/api/v1/billing/payment-methods", json={
        "tenant_id": flow["tid"], "kind": "card", "token": "tok_visa_4242"}).json()
    r = client.post(f"/api/v1/billing/invoices/{inv}/pay",
                    json={"payment_method_id": good["id"]})
    assert r.status_code == 200, r.text
    pay = r.json()["payment"]
    pi = client.get(f"/stripe/v1/payment_intents/{pay['id']}").json()
    _has(pi, "payment_intent", STRIPE_REQUIRED)
    _enum(pi, "payment_intent.status", STRIPE_ENUMS)
    assert pi["status"] == "succeeded" and pi["amount_received"] == 1_320_000
    assert pi["currency"] == "krw" and pi["livemode"] is False
    _full(STRIPE_SPEC, "payment_intent", pi)
    sinv = client.get(f"/stripe/v1/invoices/in_{inv}").json()
    _has(sinv, "invoice", STRIPE_REQUIRED)
    _enum(sinv, "invoice.status", STRIPE_ENUMS)
    assert sinv["status"] == "paid" and sinv["amount_paid"] == sinv["total"]
    for li in sinv["lines"]["data"]:
        _has(li, "line_item", STRIPE_REQUIRED)
    _full(STRIPE_SPEC, "invoice", sinv)
    evs = client.get("/stripe/v1/events", params={"limit": 50}).json()
    types = [e["type"] for e in evs["data"]]
    assert {"payment_intent.succeeded", "payment_intent.payment_failed",
            "customer.created"} <= set(types)
    for e in evs["data"]:
        _has(e, "event", STRIPE_REQUIRED)
        _full(STRIPE_SPEC, "event", e)
    for rec in wh.deliveries("stripe"):
        assert wh.verify_stripe(rec["raw"], rec["headers"]["Stripe-Signature"])
    cus = client.get(f"/stripe/v1/customers/{pi['customer']}").json()
    _has(cus, "customer", STRIPE_REQUIRED)
    _full(STRIPE_SPEC, "customer", cus)


def test_stripe_direct_api_form_encoded(client):
    cus = client.post("/stripe/v1/customers", data={
        "name": "Direct", "metadata[tenant_id]": "tnt-x"}).json()
    assert cus["object"] == "customer" and cus["metadata"] == {"tenant_id": "tnt-x"}
    r = client.post("/stripe/v1/payment_intents", data={
        "amount": "5000", "currency": "krw", "customer": cus["id"],
        "payment_method": "pm_card_visa", "confirm": "true"})
    assert r.status_code == 200 and r.json()["status"] == "succeeded"
    r = client.post("/stripe/v1/payment_intents", data={
        "amount": "5000", "currency": "krw", "payment_method": "pm_card_chargeDeclined",
        "confirm": "true"})
    assert r.status_code == 402
    err = r.json()
    _has(err, "error", STRIPE_REQUIRED)
    _has(err["error"], "api_errors", STRIPE_REQUIRED)
    assert err["error"]["type"] == "card_error"
    assert err["error"]["decline_code"] == "generic_decline"
    _full(STRIPE_SPEC, "error", err)
    r = client.post("/stripe/v1/payment_intents", data={"currency": "krw",
                                                          "amount": "0"})
    assert r.status_code == 400
    assert client.get("/stripe/v1/payment_intents/pi_nope").status_code == 404


def test_contract_termination_terminates_subscription(client, flow):
    cid = flow["contract"]["id"]
    r = client.patch(f"/api/v1/contracts/{cid}", json={"status": "terminated"})
    assert r.status_code == 200, r.text
    sub = client.get(f"/lago/api/v1/subscriptions/{cid}").json()["subscription"]
    assert sub["status"] == "terminated" and sub["terminated_at"]
    assert wh.deliveries("lago")[-1]["type"] == "subscription.terminated"
    # Lago-side void of a finalized unpaid invoice
    client.post(f"/api/v1/billing/invoices/{flow['inv_id']}/finalize")
    v = client.post(f"/lago/api/v1/invoices/{flow['inv_id']}/void")
    assert v.status_code == 200 and v.json()["invoice"]["status"] == "voided"
    assert client.post(f"/lago/api/v1/invoices/{flow['inv_id']}/void").status_code == 405


def test_tenant_cannot_use_emulators(client, flow):
    h = {"X-Tenant-Id": flow["tid"]}
    assert client.get("/lago/api/v1/customers", headers=h).status_code == 401
    assert client.get("/stripe/v1/events", headers=h).status_code == 401
    own = client.get("/api/v1/billing/webhooks", headers=h).json()
    assert own and all(r["refs"].get("tenant_id") == flow["tid"] for r in own)


def test_reset_clears_emulator_state(client, flow):
    assert STORE.lago and STORE.billing_webhooks
    STORE.reset()
    assert not STORE.lago and not STORE.stripe and not STORE.billing_webhooks
    json.dumps(wh.endpoints("lago"))            # default receiver re-created
