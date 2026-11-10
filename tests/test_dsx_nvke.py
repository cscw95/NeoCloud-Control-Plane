"""NVKE managed K8s — releases, cluster/nodepool CRUD on NICo instances,
kubeconfig, upgrade, AICR recipe, NVSentinel node replacement, tenancy."""
from app import nico_fake


def _tenant(client, name="nvke-co"):
    return client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"}).json()["id"]


def _host_state(host_id):
    return nico_fake.FAKE_NICO.get_host(host_id).state.value


def test_releases(client):
    r = client.get("/api/v1/nvke/releases").json()
    assert r["nvke_version"] == "0.23.1"
    assert {x["k8s_minor"] for x in r["releases"]} == {"1.33", "1.34"}


def test_cluster_lifecycle_allocates_nico_instances(client):
    tid = _tenant(client)
    r = client.post("/api/v1/nvke/clusters", json={
        "name": "train", "tenant_id": tid, "k8s_version": "1.33",
        "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 2}]})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["state"] == "running" and c["k8s_version"] == "v1.33.5"
    assert c["control_plane"]["dedicated"] and c["node_count"] == 2
    nodes = c["nodepools"][0]["nodes"]
    for n in nodes:
        h = nico_fake.FAKE_NICO.get_host(n["nico_host_id"])
        assert h.state.value == "allocated" and h.tenant_ref == tid
        assert h.sku == "vr-nvl72" and n["instance_id"] == h.instance_id
    cid, pid = c["id"], c["nodepools"][0]["id"]

    # list (tenant filter) + get
    assert [x["id"] for x in client.get(f"/api/v1/nvke/clusters?tenant_id={tid}").json()] == [cid]
    # scale up then down — removed node goes back to pool via sanitize
    r = client.patch(f"/api/v1/nvke/clusters/{cid}/nodepools/{pid}", json={"count": 3})
    assert r.status_code == 200 and len(r.json()["nodes"]) == 3
    gone = r.json()["nodes"][2]["nico_host_id"]
    r = client.patch(f"/api/v1/nvke/clusters/{cid}/nodepools/{pid}", json={"count": 2})
    assert len(r.json()["nodes"]) == 2 and _host_state(gone) == "pool_ready"

    # second pool (CPU)
    r = client.post(f"/api/v1/nvke/clusters/{cid}/nodepools",
                    json={"name": "cpu", "instance_type": "cpu-epyc", "count": 1})
    assert r.status_code == 201
    assert len(client.get(f"/api/v1/nvke/clusters/{cid}/nodepools").json()) == 2

    # kubeconfig json + yaml
    k = client.get(f"/api/v1/nvke/clusters/{cid}/kubeconfig").json()
    assert "server: https://" in k["kubeconfig"]
    y = client.get(f"/api/v1/nvke/clusters/{cid}/kubeconfig?format=yaml")
    assert y.headers["content-type"].startswith("application/yaml")

    # upgrade 1.33 → 1.34, recipe follows
    r = client.post(f"/api/v1/nvke/clusters/{cid}/upgrade", json={"k8s_version": "1.34"})
    assert r.status_code == 200
    assert r.json()["cluster"]["k8s_version"] == "v1.34.1"
    assert all(p["k8s_version"] == "v1.34.1" for p in r.json()["cluster"]["nodepools"])
    assert client.post(f"/api/v1/nvke/clusters/{cid}/upgrade",
                       json={"k8s_version": "1.33"}).status_code == 422
    rec = client.get(f"/api/v1/nvke/clusters/{cid}/aicr-recipe").json()
    assert rec["k8s"] == "1.34" and "gpu-operator" in rec["components"]

    # NVSentinel → replace node
    old = nodes[0]
    r = client.post(f"/api/v1/nvke/clusters/{cid}/nodes/{old['name']}/replace",
                    json={"reason": "XID 79"})
    assert r.status_code == 200
    assert r.json()["old_host"] == old["nico_host_id"] != r.json()["new_host"]
    assert nico_fake.FAKE_NICO.get_host(old["nico_host_id"]).cordoned

    # delete — all hosts return
    hosts = [n["nico_host_id"] for p in client.get(f"/api/v1/nvke/clusters/{cid}")
             .json()["nodepools"] for n in p["nodes"]]
    assert client.delete(f"/api/v1/nvke/clusters/{cid}").json()["state"] == "deleted"
    assert all(_host_state(h) == "pool_ready" for h in hosts)
    assert client.get(f"/api/v1/nvke/clusters/{cid}").status_code == 404


def test_capacity_and_validation(client):
    tid = _tenant(client)
    r = client.post("/api/v1/nvke/clusters", json={
        "name": "x", "tenant_id": tid,
        "nodepools": [{"name": "g", "instance_type": "vr-nvl72", "count": 72}]})
    assert r.status_code in (201, 409)
    assert client.post("/api/v1/nvke/clusters", json={
        "name": "x", "tenant_id": tid, "k8s_version": "1.20"}).status_code == 422
    assert client.post("/api/v1/nvke/clusters", json={
        "name": "x", "tenant_id": "tnt-nope"}).status_code == 404


def test_capacity_exhaustion_rolls_back(client):
    tid = _tenant(client)
    r = client.post("/api/v1/nvke/clusters", json={
        "name": "big", "tenant_id": tid,
        "nodepools": [{"name": "c", "instance_type": "cpu-epyc", "count": 61}]})
    assert r.status_code == 409
    assert client.get(f"/api/v1/nvke/clusters?tenant_id={tid}").json() == []


def test_tenant_scope(client):
    a, b = _tenant(client, "a-co"), _tenant(client, "b-co")
    cid = client.post("/api/v1/nvke/clusters", json={"name": "a", "tenant_id": a}).json()["id"]
    hb = {"X-Tenant-Id": b}
    assert client.get(f"/api/v1/nvke/clusters/{cid}", headers=hb).status_code == 403
    assert client.get("/api/v1/nvke/clusters", headers=hb).json() == []
    ha = {"X-Tenant-Id": a}
    assert client.get(f"/api/v1/nvke/clusters/{cid}/kubeconfig", headers=ha).status_code == 200


def test_gpu_pool_claims_rack_and_releases_it(client):
    from app.store import STORE
    tid = _tenant(client)
    c = client.post("/api/v1/nvke/clusters", json={
        "name": "g", "tenant_id": tid,
        "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 2}]}).json()
    trays = [n["tray_id"] for n in c["nodepools"][0]["nodes"]]
    racks = {STORE.trays[t].rack_id for t in trays}
    assert len(racks) == 1                       # packed onto one dedicated rack
    rack = STORE.racks[racks.pop()]
    assert rack.tenant_id == tid
    client.delete(f"/api/v1/nvke/clusters/{c['id']}")
    assert rack.tenant_id is None


def test_legacy_nkd_cluster_visible_in_nvke_model(client):
    from app.models import K8sCluster
    from app.store import STORE
    STORE.k8s_clusters["k8s-legacy"] = K8sCluster(
        id="k8s-legacy", tenant_id="tnt-old", order_id="ord-1", version="v1.33.2",
        nkd_version="25.06", state="running")
    rows = {c["id"]: c for c in client.get("/api/v1/nvke/clusters").json()}
    assert rows["k8s-legacy"]["source"] == "nkd"
    assert client.get("/api/v1/nvke/clusters/k8s-legacy/kubeconfig").status_code == 200
    assert client.post("/api/v1/nvke/clusters/k8s-legacy/upgrade",
                       json={"k8s_version": "1.34"}).status_code == 409
