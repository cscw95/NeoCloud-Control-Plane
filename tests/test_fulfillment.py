"""Approval-gated fulfillment — biz request → operator per-stage approval → delivery."""

from app.nico_fake import FAKE_NICO, NicoHostState


def _mk_tenant(client, name="appr-ai"):
    r = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"})
    return r.json()["id"]


def _request(client, tid, racks=1):
    r = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new", "blueprint_key": "vr-nvl72",
        "racks": racks, "approval_mode": True})
    assert r.status_code == 201
    return r.json()


def _approve(client, oid):
    r = client.post(f"/api/v1/orders/{oid}/approve")
    assert r.status_code == 200, r.text
    return r.json()


def test_approval_gated_fulfillment_full_journey(client):
    tid = _mk_tenant(client)
    o = _request(client, tid)
    assert o["state"] == "received" and o["pending_stage"] == "validated"
    assert o["approval_mode"] is True
    # No resources touched yet
    assert client.get("/api/v1/nodes/summary").json()["by_state"] == {
        "pool_ready": 540}

    o = _approve(client, o["id"])                     # policy / placement
    assert o["state"] == "validated" and o["pending_stage"] == "reserved"
    assert len(o["node_ids"]) == 18

    o = _approve(client, o["id"])                     # reserve
    assert o["state"] == "reserved" and o["pending_stage"] == "provisioning"
    host = FAKE_NICO.get_host("nh-su-2-rack-00-tray-00")
    assert host.state == NicoHostState.reserved       # NICo sync

    o = _approve(client, o["id"])                     # provisioning + allocation
    assert o["state"] == "provisioning" and o["pending_stage"] == "isolating"
    assert FAKE_NICO.get_host("nh-su-2-rack-00-tray-00").state == \
        NicoHostState.allocated

    o = _approve(client, o["id"])                     # isolation
    assert o["state"] == "isolating" and o["pending_stage"] == "storage_binding"
    assert len(client.get("/fake-nico/segments").json()) == 1

    o = _approve(client, o["id"])                     # storage
    assert o["state"] == "storage_binding" and o["pending_stage"] == "acceptance"
    assert len(client.get("/fake-vast/views").json()) == 1

    o = _approve(client, o["id"])                     # acceptance verification
    assert o["state"] == "acceptance" and o["pending_stage"] == "delivered"

    o = _approve(client, o["id"])                     # delivery
    assert o["state"] == "delivered" and o["pending_stage"] is None
    nodes = client.get(f"/api/v1/nodes?tenant_id={tid}").json()
    assert {n["state"] for n in nodes} == {"in_service"}

    # Re-approval after completion is 409
    assert client.post(f"/api/v1/orders/{o['id']}/approve").status_code == 409

    # In sync with the /arch flow view — approval events recorded per stage
    flow = client.get(f"/api/v1/orders/{o['id']}/flow").json()
    assert [s["state"] for s in flow["stages"]] == [
        "received", "validated", "reserved", "provisioning",
        "isolating", "storage_binding", "acceptance", "delivered"]
    ops_events = [a for s in flow["stages"] for a in s["apis"]
                  if a["src"] == "Operator"]
    assert len(ops_events) == 7                       # 7 gate approvals


def test_reject_before_provisioning_is_clean(client):
    tid = _mk_tenant(client)
    o = _request(client, tid)
    _approve(client, o["id"])                         # only up to validated
    r = client.post(f"/api/v1/orders/{o['id']}/reject?reason=용량 정책")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "rejected" and "용량 정책" in body["error"]
    assert client.get("/api/v1/nodes/summary").json()["by_state"] == {
        "pool_ready": 540}


def test_reject_mid_flight_compensates(client):
    tid = _mk_tenant(client)
    o = _request(client, tid)
    for _ in range(3):                                # validated→reserved→provisioning
        o = _approve(client, o["id"])
    assert o["state"] == "provisioning"
    body = client.post(f"/api/v1/orders/{o['id']}/reject?reason=고객 요청 취소").json()
    assert body["state"] == "failed"
    # saga rollback: all returned to the pool, no VAST/VPC residue
    assert client.get("/api/v1/nodes/summary").json()["by_state"] == {
        "pool_ready": 540}
    assert client.get("/fake-vast/views").json() == []
    assert client.get("/fake-nico/segments").json() == []
