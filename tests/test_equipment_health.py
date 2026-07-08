"""장비 헬스 취합 API (운영 포털) — 집계·운영 조치·배치 제외 회귀."""


def _mk_tenant(client, name="eq-ai"):
    r = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_equipment_health_aggregation_and_operator_action(client):
    h = client.get("/api/v1/health/equipment").json()
    assert h["sites"] and h["sites"][0]["floors"]
    fl = h["sites"][0]["floors"][0]
    assert {"name", "ready", "racks", "gpus",
            "rack_states", "gpu_states", "unhealthy"} <= set(fl)
    assert h["totals"]["unhealthy_equipment"] == 0
    assert sum(h["totals"]["racks"].values()) == 30          # gb200 14 + vr 16

    su = client.get("/api/v1/scalable-units/su-1").json()
    rack_id = su["rack_ids"][0]

    # 장애 처리 → 집계·리스트 반영
    r = client.patch("/api/v1/equipment/state", json={
        "kind": "rack", "id": rack_id, "state": "faulted"})
    assert r.status_code == 200 and r.json()["state"] == "faulted"
    h = client.get("/api/v1/health/equipment").json()
    assert h["totals"]["unhealthy_equipment"] == 1
    assert any(e["kind"] == "rack" and e["id"] == rack_id
               and e["state"] == "faulted" for e in h["faulted_equipment"])
    assert sum(fl["unhealthy"] for s in h["sites"] for fl in s["floors"]) == 1

    # 정비 전환 후 복구 → 전 장비 정상
    client.patch("/api/v1/equipment/state", json={
        "kind": "rack", "id": rack_id, "state": "maintenance"})
    r = client.patch("/api/v1/equipment/state", json={
        "kind": "rack", "id": rack_id, "state": "ready"})
    assert r.json()["state"] == "ready"
    h = client.get("/api/v1/health/equipment").json()
    assert h["totals"]["unhealthy_equipment"] == 0

    # 검증: 잘못된 kind/state/id
    assert client.patch("/api/v1/equipment/state", json={
        "kind": "rack", "id": rack_id, "state": "exploded"}).status_code == 422
    assert client.patch("/api/v1/equipment/state", json={
        "kind": "pdu", "id": rack_id, "state": "faulted"}).status_code == 422
    assert client.patch("/api/v1/equipment/state", json={
        "kind": "rack", "id": "no-such", "state": "faulted"}).status_code == 404


def test_faulted_rack_excluded_from_placement_and_restore(client):
    su = client.get("/api/v1/scalable-units/su-2").json()   # vr-nvl72 SU
    bad_rack = su["rack_ids"][0]
    client.patch("/api/v1/equipment/state", json={
        "kind": "rack", "id": bad_rack, "state": "faulted"})

    tid = _mk_tenant(client)
    order = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 3}).json()
    assert order["state"] == "delivered", order.get("error")

    nodes = client.get(f"/api/v1/nodes?tenant_id={tid}").json()
    assert nodes and all(n["rack_id"] != bad_rack for n in nodes)

    # 할당 중 랙을 faulted → ready 복구 시 allocated로 되돌아간다
    used_rack = nodes[0]["rack_id"]
    client.patch("/api/v1/equipment/state", json={
        "kind": "rack", "id": used_rack, "state": "faulted"})
    r = client.patch("/api/v1/equipment/state", json={
        "kind": "rack", "id": used_rack, "state": "ready"})
    assert r.json()["state"] == "allocated"
