def _tenant(client, name, tier):
    return client.post("/api/v1/tenants",
                       json={"name": name, "isolation_tier": tier}).json()


def test_create_tenant_binds_vni(client):
    t = _tenant(client, "acme-ai", "bare_metal_dedicated")
    assert t["id"] == "tnt-acme-ai"
    vmap = client.get("/api/v1/network/vni-map").json()
    row = next(x for x in vmap if x["tenant_id"] == t["id"])
    assert 10_000 <= row["compute_l3vni"] <= 19_999
    assert row["vrf"] == "VRF-acme-ai"


def test_allocate_whole_su_binds_racks(client):
    _tenant(client, "acme-ai", "bare_metal_dedicated")
    a = client.post("/api/v1/allocations", json={
        "tenant_id": "tnt-acme-ai", "su_id": "su-1",
        "scope": "scalable_unit"}).json()
    assert len(a["rack_ids"]) == 14
    assert a["dpu_mode"] == "dpu"
    rack = client.get("/api/v1/racks/su-1-rack-00").json()
    assert rack["tenant_id"] == "tnt-acme-ai"
    assert rack["state"] == "allocated"
    s = client.get("/api/v1/inventory/summary").json()
    assert s["gpus_by_state"]["allocated"] == 1008


def test_bare_metal_cannot_share_su(client):
    _tenant(client, "acme-ai", "bare_metal_dedicated")
    _tenant(client, "globex", "bare_metal_dedicated")
    # acme takes a subset of su-1
    client.post("/api/v1/allocations", json={
        "tenant_id": "tnt-acme-ai", "su_id": "su-1", "scope": "rack_set",
        "rack_ids": ["su-1-rack-00", "su-1-rack-01"]})
    # globex (dedicated) tries to share the same SU -> 409
    r = client.post("/api/v1/allocations", json={
        "tenant_id": "tnt-globex", "su_id": "su-1", "scope": "rack_set",
        "rack_ids": ["su-1-rack-02"]})
    assert r.status_code == 409


def test_rack_conflict(client):
    _tenant(client, "acme-ai", "vm_multitenant")
    _tenant(client, "globex", "vm_multitenant")
    client.post("/api/v1/allocations", json={
        "tenant_id": "tnt-acme-ai", "su_id": "su-1", "scope": "rack_set",
        "rack_ids": ["su-1-rack-00"]})
    r = client.post("/api/v1/allocations", json={
        "tenant_id": "tnt-globex", "su_id": "su-1", "scope": "rack_set",
        "rack_ids": ["su-1-rack-00"]})
    assert r.status_code == 409


def test_nvlink_partition(client):
    _tenant(client, "acme-ai", "bare_metal_dedicated")
    client.post("/api/v1/allocations", json={
        "tenant_id": "tnt-acme-ai", "su_id": "su-1", "scope": "scalable_unit"})
    p = client.post("/api/v1/nvlink-partitions", json={
        "rack_id": "su-1-rack-00", "tenant_id": "tnt-acme-ai",
        "tray_ids": ["su-1-rack-00-tray-00", "su-1-rack-00-tray-01"]}).json()
    assert p["partition_id"] == 1
    # overlapping trays rejected
    r = client.post("/api/v1/nvlink-partitions", json={
        "rack_id": "su-1-rack-00", "tenant_id": "tnt-acme-ai",
        "tray_ids": ["su-1-rack-00-tray-01"]})
    assert r.status_code == 409


def test_partition_requires_owned_rack(client):
    _tenant(client, "acme-ai", "bare_metal_dedicated")
    # no allocation -> rack not owned
    r = client.post("/api/v1/nvlink-partitions", json={
        "rack_id": "su-1-rack-00", "tenant_id": "tnt-acme-ai",
        "tray_ids": ["su-1-rack-00-tray-00"]})
    assert r.status_code == 409


def test_isolation_report_ok(client):
    _tenant(client, "acme-ai", "bare_metal_dedicated")
    client.post("/api/v1/allocations", json={
        "tenant_id": "tnt-acme-ai", "su_id": "su-1", "scope": "scalable_unit"})
    rep = client.get("/api/v1/tenants/tnt-acme-ai/isolation").json()
    assert rep["ok"] is True
    layers = {f["layer"] for f in rep["findings"]}
    assert {"identity", "physical", "network"} <= layers


def test_dealloc_releases(client):
    _tenant(client, "acme-ai", "vm_multitenant")
    a = client.post("/api/v1/allocations", json={
        "tenant_id": "tnt-acme-ai", "su_id": "su-1", "scope": "rack_set",
        "rack_ids": ["su-1-rack-00"]}).json()
    client.delete(f"/api/v1/allocations/{a['id']}")
    rack = client.get("/api/v1/racks/su-1-rack-00").json()
    assert rack["tenant_id"] is None
    assert rack["state"] == "ready"
