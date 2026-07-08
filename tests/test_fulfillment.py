"""승인 게이트 fulfillment — 비즈 요청 → 운영자 단계별 승인 → 인도."""

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
    # 아직 아무 자원도 건드리지 않음
    assert client.get("/api/v1/nodes/summary").json()["by_state"] == {
        "pool_ready": 540}

    o = _approve(client, o["id"])                     # 정책·배치
    assert o["state"] == "validated" and o["pending_stage"] == "reserved"
    assert len(o["node_ids"]) == 18

    o = _approve(client, o["id"])                     # 예약
    assert o["state"] == "reserved" and o["pending_stage"] == "provisioning"
    host = FAKE_NICO.get_host("nh-su-2-rack-00-tray-00")
    assert host.state == NicoHostState.reserved       # NICo 싱크

    o = _approve(client, o["id"])                     # 프로비저닝+할당
    assert o["state"] == "provisioning" and o["pending_stage"] == "isolating"
    assert FAKE_NICO.get_host("nh-su-2-rack-00-tray-00").state == \
        NicoHostState.allocated

    o = _approve(client, o["id"])                     # 격리
    assert o["state"] == "isolating" and o["pending_stage"] == "storage_binding"
    assert len(client.get("/fake-nico/segments").json()) == 1

    o = _approve(client, o["id"])                     # 스토리지
    assert o["state"] == "storage_binding" and o["pending_stage"] == "acceptance"
    assert len(client.get("/fake-vast/views").json()) == 1

    o = _approve(client, o["id"])                     # 인수 검증
    assert o["state"] == "acceptance" and o["pending_stage"] == "delivered"

    o = _approve(client, o["id"])                     # 인도
    assert o["state"] == "delivered" and o["pending_stage"] is None
    nodes = client.get(f"/api/v1/nodes?tenant_id={tid}").json()
    assert {n["state"] for n in nodes} == {"in_service"}

    # 완주 후 재승인은 409
    assert client.post(f"/api/v1/orders/{o['id']}/approve").status_code == 409

    # /arch 플로우 화면과 싱크 — 승인 이벤트가 단계별로 기록됨
    flow = client.get(f"/api/v1/orders/{o['id']}/flow").json()
    assert [s["state"] for s in flow["stages"]] == [
        "received", "validated", "reserved", "provisioning",
        "isolating", "storage_binding", "acceptance", "delivered"]
    ops_events = [a for s in flow["stages"] for a in s["apis"]
                  if a["src"] == "Operator"]
    assert len(ops_events) == 7                       # 게이트 승인 7회


def test_reject_before_provisioning_is_clean(client):
    tid = _mk_tenant(client)
    o = _request(client, tid)
    _approve(client, o["id"])                         # validated까지만
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
    # saga 원복: 전량 풀 복귀, VAST/VPC 잔재 없음
    assert client.get("/api/v1/nodes/summary").json()["by_state"] == {
        "pool_ready": 540}
    assert client.get("/fake-vast/views").json() == []
    assert client.get("/fake-nico/segments").json() == []
