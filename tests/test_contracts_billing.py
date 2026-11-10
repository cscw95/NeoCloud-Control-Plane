"""F05 billing (price book · invoices · payment · tax invoice) and F06 contracts."""

from datetime import datetime, timezone

from app.models import LifecycleEvent
from app.store import STORE

PERIOD = "2026-10"                 # a full past month → deterministic hours


def _mk_tenant(client, name="bill-corp"):
    r = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _contract(client, tid, racks=4, start="2026-10-01", term=12, **kw):
    body = {"tenant_id": tid, "product": "bmaas", "commitment": "reserved",
            "racks": racks, "blueprint_key": "vr-nvl72", "term_months": term,
            "start_date": start, "unit_price": 1_000_000,
            "placement": {"same_ib_zone": True}, "sla_tier": "premium", **kw}
    r = client.post("/api/v1/contracts", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _activate(client, cid):
    r = client.post(f"/api/v1/contracts/{cid}/activate")
    assert r.status_code == 200, r.text
    return r.json()


def _order(client, tid, **kw):
    r = client.post("/api/v1/orders", json={"tenant_id": tid, "kind": "new", **kw})
    return r


def _backdate_delivery(oid, at: str):
    o = STORE.orders[oid]
    for e in o.history:
        if e.state == "delivered":
            e.at = at


def _run(client, period=PERIOD, **kw):
    r = client.post("/api/v1/billing/invoices:run", json={"period": period, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _invoice(client, tid, period=PERIOD):
    inv = client.get(f"/api/v1/billing/invoices?tenant_id={tid}&period={period}").json()
    assert len(inv) == 1
    return inv[0]


# ---------------------------------------------------------------------------
# F06 contracts
# ---------------------------------------------------------------------------
def test_contract_lifecycle_and_order_from_contract(client):
    tid = _mk_tenant(client)
    c = _contract(client, tid)
    assert c["status"] == "draft" and c["end_date"] == "2027-10-01"
    # draft contracts cannot carry orders
    assert _order(client, tid, contract_id=c["id"]).status_code == 422
    c = _activate(client, c["id"])
    assert c["status"] == "active"
    assert client.post(f"/api/v1/contracts/{c['id']}/activate").status_code == 409
    # racks / blueprint come from the contract, not the body
    r = _order(client, tid, contract_id=c["id"], racks=1, blueprint_key="gb200-nvl72")
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["state"] == "delivered" and o["racks"] == 4
    assert o["blueprint_key"] == "vr-nvl72" and o["contract_id"] == c["id"]
    # one site → same IB zone
    sites = {STORE.factory_of_su(STORE.racks[STORE.node_instances[n].rack_id].su_id).id
             for n in o["node_ids"]}
    assert len(sites) == 1
    got = client.get(f"/api/v1/contracts/{c['id']}").json()
    assert got["orders"][0]["order_id"] == o["id"]
    # billing start = max(start_date, delivered_at) → delivery (now) here
    assert got["orders"][0]["billing_start"] == got["orders"][0]["delivered_at"]
    # contract capacity already ordered
    assert _order(client, tid, contract_id=c["id"]).status_code == 409
    assert [x["id"] for x in client.get(
        f"/api/v1/contracts?tenant_id={tid}").json()] == [c["id"]]


def test_contract_order_422_without_same_ib_zone_capacity(client):
    tid = _mk_tenant(client)
    c = _activate(client, _contract(client, tid, racks=17)["id"])  # VR SU = 16
    r = _order(client, tid, contract_id=c["id"])
    assert r.status_code == 422 and "IB zone" in r.json()["detail"]
    assert not STORE.orders                     # nothing placed


def test_contract_validation_and_patch_rules(client):
    tid = _mk_tenant(client)
    bad = client.post("/api/v1/contracts", json={
        "tenant_id": tid, "product": "bmaas", "term_months": 12,
        "start_date": "2026-10-01", "unit_price": 1})
    assert bad.status_code == 422                # racks required
    bad = client.post("/api/v1/contracts", json={
        "tenant_id": tid, "product": "token-api", "term_months": 12,
        "start_date": "2026-13-01", "unit_price": 1, "min_monthly_amount": 5})
    assert bad.status_code == 422                # bad date
    c = _contract(client, tid)
    r = client.patch(f"/api/v1/contracts/{c['id']}", json={"racks": 2, "term_months": 6})
    assert r.status_code == 200 and r.json()["end_date"] == "2027-04-01"
    _activate(client, c["id"])
    assert client.patch(f"/api/v1/contracts/{c['id']}",
                        json={"racks": 3}).status_code == 409
    r = client.patch(f"/api/v1/contracts/{c['id']}", json={"status": "terminated"})
    assert r.json()["status"] == "terminated"
    # tenant plane: read own only, no writes
    other = _mk_tenant(client, "other-corp")
    h = {"X-Tenant-Id": other}
    assert client.get(f"/api/v1/contracts/{c['id']}", headers=h).status_code == 403
    assert client.get("/api/v1/contracts", headers=h).json() == []
    assert client.post("/api/v1/contracts", headers=h, json={
        "tenant_id": other, "product": "token-api", "term_months": 1,
        "start_date": "2026-10-01", "unit_price": 1,
        "min_monthly_amount": 1}).status_code == 403


def test_contract_expires_and_switches_to_on_demand(client):
    tid = _mk_tenant(client)
    # 1-month term in September; order delivered 2026-09-15 → October is on-demand
    c = _activate(client, _contract(client, tid, racks=1, start="2026-09-01", term=3)["id"])
    o = _order(client, tid, contract_id=c["id"]).json()
    _backdate_delivery(o["id"], "2026-09-15T00:00:00+00:00")
    STORE.contracts[c["id"]]["end_date"] = "2026-10-01"   # time travel: 1-month term
    _run(client, "2026-09")
    _run(client)
    sep = _invoice(client, tid, "2026-09")
    oct_ = _invoice(client, tid)
    assert client.get(f"/api/v1/contracts/{c['id']}").json()["status"] == "expired"
    [sl] = sep["lines"]
    assert sl["commitment"] == "reserved" and sl["quantity"] == 16 * 24
    assert sl["amount"] == 16 * 24 * 1_000_000
    [ol] = oct_["lines"]
    assert ol["commitment"] == "on_demand" and ol["unit_price"] == 1_372_000
    assert ol["quantity"] == 31 * 24 and oct_["commitment_true_up"] == 0


# ---------------------------------------------------------------------------
# F05 billing
# ---------------------------------------------------------------------------
def test_invoice_reserved_lines_vat_and_idempotent_rerun(client):
    tid = _mk_tenant(client)
    c = _activate(client, _contract(client, tid, racks=4)["id"])
    o = _order(client, tid, contract_id=c["id"]).json()
    _backdate_delivery(o["id"], "2026-10-11T00:00:00+00:00")
    out = _run(client)
    inv = _invoice(client, tid)
    assert out["created"] == [inv["id"]] and inv["status"] == "draft"
    [line] = inv["lines"]
    assert line["commitment"] == "reserved" and line["quantity"] == 4 * 21 * 24
    assert line["amount"] == 4 * 21 * 24 * 1_000_000
    assert inv["commitment_true_up"] == 0
    assert inv["vat"] == inv["subtotal"] // 10 and inv["total"] == inv["subtotal"] + inv["vat"]
    assert inv["currency"] == "KRW"
    # price-book change + re-run → same invoice id, no duplicate
    pb = client.get("/api/v1/billing/price-book").json()
    assert pb["currency"] == "KRW" and pb["version"] == 1
    out2 = _run(client)
    assert out2["updated"] == [inv["id"]] and out2["created"] == []
    assert len(client.get(f"/api/v1/billing/invoices?tenant_id={tid}").json()) == 1


def test_on_demand_rate_change_rerun_and_finalize_freezes(client):
    tid = _mk_tenant(client)
    o = _order(client, tid, blueprint_key="vr-nvl72", racks=1).json()
    _backdate_delivery(o["id"], "2026-10-31T00:00:00+00:00")       # 24 h in October
    _run(client)
    inv = _invoice(client, tid)
    assert inv["lines"][0]["amount"] == 24 * 1_372_000
    items = client.get("/api/v1/billing/price-book").json()["items"]
    for it in items:
        if it["product"] == "bmaas" and it["metric"] == "rack_hour" \
                and it["commitment"] == "on_demand":
            it["unit_price"] = 1_000_001
    r = client.put("/api/v1/billing/price-book", json={"currency": "KRW", "items": items})
    assert r.status_code == 200 and r.json()["version"] == 2
    _run(client)
    inv = _invoice(client, tid)
    assert inv["lines"][0]["amount"] == 24 * 1_000_001 and inv["price_book_version"] == 2
    # VAT is 원 미만 절사: 24,000,024 × 10% = 2,400,002.4 → 2,400,002
    assert inv["vat"] == 2_400_002
    r = client.post(f"/api/v1/billing/invoices/{inv['id']}/finalize")
    assert r.status_code == 200 and r.json()["status"] == "finalized"
    tax = client.get(f"/api/v1/billing/invoices/{inv['id']}/tax-invoice").json()
    assert tax["supply_amount"] == inv["subtotal"] and tax["tax_amount"] == inv["vat"]
    assert len(tax["approval_no"].replace("-", "")) == 24
    # after finalize: rate change + re-run leave it untouched; finalize again 409
    for it in items:
        it["unit_price"] = 1
    client.put("/api/v1/billing/price-book", json={"currency": "KRW", "items": items})
    out = _run(client)
    assert out["skipped"] == [inv["id"]]
    assert client.get(f"/api/v1/billing/invoices/{inv['id']}").json()["total"] == inv["total"]
    assert client.post(f"/api/v1/billing/invoices/{inv['id']}/finalize").status_code == 409


def test_commitment_true_up_when_usage_below_commit(client):
    tid = _mk_tenant(client)
    c = _activate(client, _contract(client, tid, racks=1,
                                    min_monthly_amount=1_000_000_000)["id"])
    o = _order(client, tid, contract_id=c["id"]).json()
    _backdate_delivery(o["id"], "2026-10-01T00:00:00+00:00")
    _run(client)
    inv = _invoice(client, tid)
    used = 31 * 24 * 1_000_000                    # 744,000,000
    assert inv["lines"][0]["amount"] == used
    assert inv["commitment_true_up"] == 1_000_000_000 - used
    assert inv["usage_amount"] == 1_000_000_000


def test_sla_credit_deducted_once(client):
    tid = _mk_tenant(client)
    o = _order(client, tid, blueprint_key="vr-nvl72", racks=1).json()
    _backdate_delivery(o["id"], "2026-10-31T00:00:00+00:00")
    STORE.sla_credits["crd-9"] = {
        "credit_id": "crd-9", "key": f"{tid}|{PERIOD}", "tenant_id": tid,
        "month": PERIOD, "incident_id": "inc-1", "amount_usd": 1.0,
        "pct_of_monthly": 10, "status": "calculated", "applied_invoice": None}
    _run(client)
    _run(client)                                  # re-run: still one deduction
    inv = _invoice(client, tid)
    usage = 24 * 1_372_000
    assert inv["sla_credits"] == [{"credit_id": "crd-9", "amount": usage // 10,
                                   "month": PERIOD, "pct_of_monthly": 10,
                                   "incident_id": "inc-1"}]
    assert inv["subtotal"] == usage - usage // 10
    assert STORE.sla_credits["crd-9"]["applied_invoice"] == inv["id"]
    client.post(f"/api/v1/billing/invoices/{inv['id']}/finalize")
    assert STORE.sla_credits["crd-9"]["status"] == "applied"
    # next month does not deduct it again
    o2 = _order(client, tid, blueprint_key="vr-nvl72", racks=1).json()
    assert o2["state"] == "delivered"
    nov = _run(client, "2026-11")
    nov_inv = client.get(f"/api/v1/billing/invoices/{nov['created'][0]}").json()
    assert nov_inv["sla_credits"] == []


def test_token_usage_from_metering_events_dedup(client):
    tid = _mk_tenant(client)
    ts = "2026-10-05T00:00:00+00:00"
    ev = lambda key, tt, q, src="gateway": {
        "idempotency_key": key, "source": src, "tenant_id": tid,
        "product": "token-api", "metric": "tokens", "quantity": q,
        "unit": "token", "ts": ts, "attrs": {"model": "m1", "token_type": tt}}
    STORE.metering_events = [ev("r1:in", "input", 1_000_000),
                             ev("r1:in", "input", 1_000_000),        # duplicate
                             ev("r1:out", "output", 500_000),
                             ev("e1:in", "input", 1_000_000, "engine")]
    try:
        _run(client)
        inv = _invoice(client, tid)
    finally:
        del STORE.metering_events
    by_type = {l["token_type"]: l for l in inv["lines"]}
    assert by_type["input"]["quantity"] == 1_000_000 and by_type["input"]["amount"] == 800
    assert by_type["output"]["amount"] == 1_600
    assert inv["subtotal"] == 2_400 and inv["vat"] == 240


def test_payment_and_tenant_scope(client):
    tid = _mk_tenant(client)
    o = _order(client, tid, blueprint_key="vr-nvl72", racks=1).json()
    _backdate_delivery(o["id"], "2026-10-31T00:00:00+00:00")
    _run(client)
    inv = _invoice(client, tid)
    h = {"X-Tenant-Id": tid}
    # tenant cannot run/finalize; cannot pay a draft
    assert client.post("/api/v1/billing/invoices:run", headers=h,
                       json={"period": PERIOD}).status_code == 403
    assert client.post(f"/api/v1/billing/invoices/{inv['id']}/finalize",
                       headers=h).status_code == 403
    assert client.post(f"/api/v1/billing/invoices/{inv['id']}/pay",
                       headers=h).status_code == 409
    client.post(f"/api/v1/billing/invoices/{inv['id']}/finalize")
    assert client.post(f"/api/v1/billing/invoices/{inv['id']}/pay",
                       headers=h).status_code == 422             # no method
    other = _mk_tenant(client, "other-corp")
    assert client.post("/api/v1/billing/payment-methods", headers=h, json={
        "tenant_id": other, "kind": "card", "token": "tok_4242"}).status_code == 403
    assert client.get(f"/api/v1/billing/invoices/{inv['id']}",
                      headers={"X-Tenant-Id": other}).status_code == 403
    assert client.get("/api/v1/billing/invoices",
                      headers={"X-Tenant-Id": other}).json() == []
    bad = client.post("/api/v1/billing/payment-methods", headers=h, json={
        "tenant_id": tid, "kind": "card", "token": "tok_fail_0002"}).json()
    assert bad["last4"] == "0002" and bad["default"] is True and "token" not in bad
    r = client.post(f"/api/v1/billing/invoices/{inv['id']}/pay", headers=h)
    assert r.status_code == 402
    good = client.post("/api/v1/billing/payment-methods", headers=h, json={
        "tenant_id": tid, "kind": "bank_transfer", "token": "tok_bank_1111"}).json()
    r = client.post(f"/api/v1/billing/invoices/{inv['id']}/pay", headers=h,
                    json={"payment_method_id": good["id"]})
    assert r.status_code == 200, r.text
    paid = r.json()
    assert paid["status"] == "paid" and paid["payment"]["status"] == "succeeded"
    assert paid["payment"]["amount"] == paid["total"]
    assert client.post(f"/api/v1/billing/invoices/{inv['id']}/pay",
                       headers=h).status_code == 409


def test_invoice_run_validation(client):
    assert client.post("/api/v1/billing/invoices:run",
                       json={"period": "2026-13"}).status_code == 422
    assert client.post("/api/v1/billing/invoices:run",
                       json={"period": "2099-01"}).status_code == 422
    assert _run(client)["invoices"] == []         # nothing billable
    assert client.put("/api/v1/billing/price-book", json={
        "currency": "USD", "items": []}).status_code == 422
    # deprecated USD preview still served
    assert client.get("/api/v1/billing/rates").json()["currency"] == "USD"
