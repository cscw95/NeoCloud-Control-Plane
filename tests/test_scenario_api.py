"""Service-scenario API contract tests — customer lifecycle (scenario_api).

- Acceptance (CP-004): approve/reject/deemed + coexisting ops approval queue
- Termination workflow (CP-012): backup-confirm gate (409 block) → wipe → Wipe certificate
- Ticket types & routing (CP-005/006/009), Status/RCA (CP-011), SLA credit (BP-006),
  public product inquiry (CP-016), reset cascade
"""

from datetime import datetime, timedelta, timezone

from app.models import NodeLifecycleState as NS
from app.store import STORE

TRAYS_PER_RACK = 18


def _mk_tenant(client, name="scenario-corp"):
    r = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _new_order(client, tenant_id, racks=1, blueprint="vr-nvl72", **kw):
    r = client.post("/api/v1/orders", json={
        "tenant_id": tenant_id, "kind": "new",
        "blueprint_key": blueprint, "racks": racks, **kw})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1) Acceptance — customer approve/reject/deemed
# ---------------------------------------------------------------------------
def test_acceptance_report_and_approve(client):
    tid = _mk_tenant(client)
    order = _new_order(client, tid, racks=1)
    oid = order["id"]
    assert order["state"] == "delivered"

    r = client.get(f"/api/v1/orders/{oid}/acceptance-report")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"           # before customer decision
    assert body["billing_start_date"] is None
    names = {c["name"] for c in body["report"]["checks"]}
    assert names == {"nccl-allreduce", "fio-storage", "burn-in", "node-ready"}
    assert all(c["status"] == "pass" for c in body["report"]["checks"])
    assert body["report"]["nodes_tested"] == TRAYS_PER_RACK
    # deemed deadline = report delivery + 7 days
    rep_ts = datetime.fromisoformat(body["report"]["report_ts"])
    ddl = datetime.fromisoformat(body["deemed_deadline"])
    assert abs((ddl - rep_ts) - timedelta(days=7)) < timedelta(seconds=5)

    r = client.post(f"/api/v1/orders/{oid}/acceptance",
                    json={"decision": "approve"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    assert body["billing_start_date"] is not None    # billing start confirmed
    # trace: AcceptanceApproved = billing start
    trace = client.get(f"/api/v1/trace?order_id={oid}&q=AcceptanceApproved")
    assert any("청구 개시" in e["detail"] for e in trace.json())
    # cannot re-decide
    r = client.post(f"/api/v1/orders/{oid}/acceptance",
                    json={"decision": "reject", "reason": "x"})
    assert r.status_code == 409


def test_acceptance_reject_requires_reason_and_retest_loop(client):
    tid = _mk_tenant(client)
    oid = _new_order(client, tid, racks=1)["id"]

    r = client.post(f"/api/v1/orders/{oid}/acceptance",
                    json={"decision": "reject"})
    assert r.status_code == 422                       # reason required

    r = client.post(f"/api/v1/orders/{oid}/acceptance",
                    json={"decision": "reject", "reason": "NCCL 성능 미달"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["reject_reason"] == "NCCL 성능 미달"
    assert body["billing_start_date"] is None         # billing not started
    # order reverts to the acceptance re-run state
    order = client.get(f"/api/v1/orders/{oid}").json()
    assert order["state"] == "acceptance"

    # after retest passes, customer re-approves → delivered + billing start
    r = client.post(f"/api/v1/orders/{oid}/acceptance",
                    json={"decision": "approve"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    assert r.json()["billing_start_date"] is not None
    assert client.get(f"/api/v1/orders/{oid}").json()["state"] == "delivered"


def test_acceptance_deemed_after_deadline(client):
    tid = _mk_tenant(client)
    oid = _new_order(client, tid, racks=1)["id"]
    client.get(f"/api/v1/orders/{oid}/acceptance-report")   # create record
    # simulate deadline elapsed
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with STORE.lock:
        STORE.acceptances[oid]["deemed_deadline"] = past
    body = client.get(f"/api/v1/orders/{oid}/acceptance-report").json()
    assert body["status"] == "deemed"
    assert body["billing_start_date"] == past          # billing starts at the deadline


def test_acceptance_before_gate_is_pending_and_undecidable(client):
    tid = _mk_tenant(client)
    order = _new_order(client, tid, racks=1, approval_mode=True)
    oid = order["id"]
    assert order["pending_stage"] == "validated"       # awaiting approval — before the gate
    body = client.get(f"/api/v1/orders/{oid}/acceptance-report").json()
    assert body == {"order_id": oid, "status": "pending", "report": None,
                    "deemed_deadline": None, "billing_start_date": None}
    r = client.post(f"/api/v1/orders/{oid}/acceptance",
                    json={"decision": "approve"})
    assert r.status_code == 409


def test_acceptance_customer_approve_completes_ops_queue_order(client):
    """approval_mode order — after reaching the acceptance gate, customer approval
    drives the remaining stages to completion (whichever of ops/customer approves first wins)."""
    tid = _mk_tenant(client)
    oid = _new_order(client, tid, racks=1, approval_mode=True)["id"]
    # ops approves up to the acceptance stage (validated→…→acceptance, 6 gates)
    for _ in range(6):
        r = client.post(f"/api/v1/orders/{oid}/approve")
        assert r.status_code == 200, r.text
    order = r.json()
    assert order["state"] == "acceptance"
    assert order["pending_stage"] == "delivered"
    # customer approval → delivered (without ops' final gate approval)
    r = client.post(f"/api/v1/orders/{oid}/acceptance",
                    json={"decision": "approve"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    order = client.get(f"/api/v1/orders/{oid}").json()
    assert order["state"] == "delivered"
    assert order["pending_stage"] is None


# ---------------------------------------------------------------------------
# 2) Termination workflow — backup gate → wipe → Wipe certificate → closed
# ---------------------------------------------------------------------------
def test_termination_full_workflow(client):
    tid = _mk_tenant(client, name="term-corp")
    oid = _new_order(client, tid, racks=1)["id"]

    r = client.post(f"/api/v1/tenants/{tid}/termination")
    assert r.status_code == 201
    body = r.json()
    term_id = body["termination_id"]
    assert body["state"] == "awaiting_backup"
    assert len(body["checklist"]) == 3
    assert all(c["confirmed"] is False for c in body["checklist"])

    # backup not confirmed → system blocks (409)
    partial = [dict(c, confirmed=(i == 0))
               for i, c in enumerate(body["checklist"])]
    r = client.post(f"/api/v1/tenants/{tid}/termination/backup-confirm",
                    json={"checklist": partial})
    assert r.status_code == 409
    assert "백업 확인 전 종료 진행 불가" in r.json()["detail"]
    assert client.get(f"/api/v1/tenants/{tid}/termination").json()[
        "state"] == "awaiting_backup"

    # all confirmed → wipe saga (pytest: delay 0 — completes synchronously)
    confirmed = [dict(c, confirmed=True) for c in body["checklist"]]
    r = client.post(f"/api/v1/tenants/{tid}/termination/backup-confirm",
                    json={"checklist": confirmed})
    assert r.status_code == 200

    body = client.get(f"/api/v1/tenants/{tid}/termination").json()
    assert body["state"] == "wiped"
    assert body["progress"]["pct"] == 100
    cert = body["wipe_certificate"]
    assert cert["cert_id"].startswith("wipe-")
    assert len(cert["sha256"]) == 64
    assert cert["erase_method"] == "NVMe crypto-erase + GPU/DDR wipe + TPM reset"
    assert len(cert["allocations"]) == 1
    assert len(cert["allocations"][0]["erase_steps"]) == 7   # 7-step wipe

    # resources actually reclaimed — nodes back to pool + terminate order closed
    with STORE.lock:
        assert not [a for a in STORE.allocations.values()
                    if a.tenant_id == tid]
        nodes = [n for n in STORE.node_instances.values()
                 if n.tenant_id == tid]
        assert nodes == []
    term_oid = cert["allocations"][0]["terminate_order_id"]
    assert client.get(f"/api/v1/orders/{term_oid}").json()["state"] == "closed"

    # certificate received → settlement closed (closed)
    r = client.get(f"/api/v1/tenants/{tid}/termination/wipe-certificate")
    assert r.status_code == 200
    assert r.json()["wipe_certificate"]["cert_id"] == cert["cert_id"]
    assert client.get(f"/api/v1/tenants/{tid}/termination").json()[
        "state"] == "closed"
    assert term_id  # workflow id preserved


def test_termination_certificate_blocked_before_wipe(client):
    tid = _mk_tenant(client, name="term-early")
    _new_order(client, tid, racks=1)
    client.post(f"/api/v1/tenants/{tid}/termination")
    r = client.get(f"/api/v1/tenants/{tid}/termination/wipe-certificate")
    assert r.status_code == 409
    # duplicate termination request blocked while in progress
    assert client.post(f"/api/v1/tenants/{tid}/termination").status_code == 409


# ---------------------------------------------------------------------------
# 3) Ticket types & routing
# ---------------------------------------------------------------------------
def test_ticket_types_and_routing(client):
    tid = _mk_tenant(client, name="tix-corp")
    # default tech → ops (backward compatible)
    t = client.post("/api/v1/tickets", json={
        "tenant_id": tid, "subject": "GPU 점검"}).json()
    assert (t["type"], t["routed_to"]) == ("tech", "ops")
    assert t["policy"] is None
    # change → ops + change_scope
    t = client.post("/api/v1/tickets", json={
        "tenant_id": tid, "subject": "랙 증설 요청", "type": "change",
        "change_scope": "contract_amendment"}).json()
    assert (t["type"], t["routed_to"]) == ("change", "ops")
    assert t["change_scope"] == "contract_amendment"
    # billing_dispute → biz + policy guidance
    t = client.post("/api/v1/tickets", json={
        "tenant_id": tid, "subject": "청구 이의", "type": "billing_dispute"}).json()
    assert t["routed_to"] == "biz"
    assert t["policy"] == "납기 유지·차기 청구 조정 원칙(명백한 금액 오류만 재발행)"
    # validation
    assert client.post("/api/v1/tickets", json={
        "tenant_id": tid, "subject": "x", "type": "nope"}).status_code == 422
    assert client.post("/api/v1/tickets", json={
        "tenant_id": tid, "subject": "x", "type": "tech",
        "change_scope": "in_contract"}).status_code == 422
    # routing filter (for the business console)
    biz = client.get("/api/v1/tickets?routed_to=biz").json()
    assert {x["type"] for x in biz} == {"billing_dispute"}


# ---------------------------------------------------------------------------
# 4) Status Page + RCA
# ---------------------------------------------------------------------------
def test_status_page_operational_then_degraded_and_rca(client):
    body = client.get("/api/v1/status").json()
    assert [c["name"] for c in body["components"]] == \
        ["Compute", "GPU Fabric", "Storage", "Network", "Portal"]
    assert all(c["state"] == "operational" for c in body["components"])
    assert body["history_90d"]["uptime_pct"] == 100.0

    # live-fault integration — inject XID 79 into the delivered tenant's tray (P1)
    tid = _mk_tenant(client, name="status-corp")
    oid = _new_order(client, tid, racks=1)["id"]
    with STORE.lock:
        tray_id = STORE.node_instances[
            STORE.orders[oid].node_ids[0]].tray_id
    r = client.post("/api/v1/emu/faults", json={
        "tray_id": tray_id, "gpu": 0, "xid": 79, "ttl_ticks": 2})
    assert r.status_code == 201

    body = client.get("/api/v1/status").json()
    comp = {c["name"]: c["state"] for c in body["components"]}
    assert comp["Compute"] == "degraded"
    inc = next(i for i in body["incidents"] if "XID 79" in i["title"])
    assert inc["severity"] == "P1"
    assert inc["state"] == "investigating"
    assert inc["updates"]

    # recovery (TTL exhausted via tick) → resolved + P1 RCA auto-issued
    client.post("/api/v1/emu/tick?n=4")
    body = client.get("/api/v1/status").json()
    comp = {c["name"]: c["state"] for c in body["components"]}
    assert comp["Compute"] == "operational"
    inc = next(i for i in body["incidents"] if i["id"] == inc["id"])
    assert inc["state"] == "resolved"

    rcas = client.get(f"/api/v1/tenants/{tid}/rca-reports").json()
    assert len(rcas) == 1
    rca = rcas[0]
    assert rca["incident_id"] == inc["id"]
    assert rca["corrective_actions"]
    assert rca["mttr_min"] is not None


# ---------------------------------------------------------------------------
# 5) SLA report + Service Credit
# ---------------------------------------------------------------------------
def test_sla_report_no_violation_then_synthesized_credit(client):
    tid = _mk_tenant(client, name="sla-corp")
    _new_order(client, tid, racks=1)
    month = datetime.now(timezone.utc).strftime("%Y-%m")

    body = client.get(f"/api/v1/tenants/{tid}/sla-report?month={month}").json()
    assert body["target_pct"] == 99.9
    assert body["violated"] is False
    assert body["credits"] == []                       # no violation → no credit

    # demo synthetic violation ×1 → credit computed (10% tier)
    body = client.get(f"/api/v1/tenants/{tid}/sla-report?month={month}"
                      "&synthesize_violation=1").json()
    assert body["violated"] is True
    assert body["availability_pct"] < 99.9
    assert len(body["incidents"]) == 1
    assert len(body["credits"]) == 1
    credit = body["credits"][0]
    assert credit["pct_of_monthly"] == 10
    assert credit["status"] == "calculated"
    assert credit["amount_usd"] == round(
        body["monthly_amount_usd"] * 0.10, 2)
    # credit is idempotent on re-fetch (same credit_id)
    again = client.get(f"/api/v1/tenants/{tid}/sla-report?month={month}"
                       "&synthesize_violation=1").json()
    assert again["credits"][0]["credit_id"] == credit["credit_id"]

    assert client.get(f"/api/v1/tenants/{tid}/sla-report?month=2026-13"
                      ).status_code == 422


# ---------------------------------------------------------------------------
# 6) Public product inquiry (unauthenticated)
# ---------------------------------------------------------------------------
def test_public_inquiries(client):
    r = client.post("/api/v1/public/inquiries", json={
        "company": "AI 스타트업 A", "name": "김철수",
        "email": "cs.kim@example.ai", "gpu_scale": "512+",
        "message": "VR NVL72 2랙 견적 문의"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "received"
    assert body["assigned"] == "영업 담당 배정 예정(자동 메일 발송)"

    r = client.post("/api/v1/public/inquiries", json={
        "company": "B", "name": "x", "email": "invalid", "gpu_scale": 8,
        "message": ""})
    assert r.status_code == 422

    listed = client.get("/api/v1/public/inquiries").json()
    assert len(listed) == 1
    assert listed[0]["inquiry_id"] == body["inquiry_id"]
    assert listed[0]["company"] == "AI 스타트업 A"


# ---------------------------------------------------------------------------
# reset cascade — all new collections reset
# ---------------------------------------------------------------------------
def test_reset_cascade_clears_scenario_collections(client):
    tid = _mk_tenant(client, name="reset-corp")
    oid = _new_order(client, tid, racks=1)["id"]
    client.get(f"/api/v1/orders/{oid}/acceptance-report")
    client.post(f"/api/v1/tenants/{tid}/termination")
    client.post("/api/v1/public/inquiries", json={
        "company": "c", "name": "n", "email": "a@b.c", "gpu_scale": 1,
        "message": "m"})
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    client.get(f"/api/v1/tenants/{tid}/sla-report?month={month}"
               "&synthesize_violation=1")
    with STORE.lock:
        assert STORE.acceptances and STORE.terminations and STORE.inquiries
        assert STORE.incidents and STORE.rcas and STORE.sla_credits

    client.post("/api/v1/admin/reseed?blueprints=gb200-nvl72,vr-nvl72")
    with STORE.lock:
        assert not (STORE.acceptances or STORE.terminations
                    or STORE.inquiries or STORE.incidents or STORE.rcas
                    or STORE.sla_credits)
    assert client.get("/api/v1/public/inquiries").json() == []
    body = client.get("/api/v1/status").json()
    assert all(c["state"] == "operational" for c in body["components"])
