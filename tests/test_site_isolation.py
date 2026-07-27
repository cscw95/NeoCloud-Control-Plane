"""Site isolation — Gasan and Ansan are independent clusters (no IB/NVLink cross).

M4 placement forbids spill across site boundaries, and the fabric/NICo
read-models must be separated per site."""


def _reseed_phase1(client):
    r = client.post("/api/v1/admin/reseed")          # default = Phase 1, 2 sites
    assert r.json()["scalable_units"] == 11


def test_cluster_never_spans_sites(client):
    _reseed_phase1(client)
    tid = client.post("/api/v1/tenants", json={
        "name": "span-ai", "isolation_tier": "bare_metal_dedicated"}).json()["id"]

    # 40 racks: exceeds Gasan total (36 racks) → must be built from SU spill within Ansan only
    o = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 40}).json()
    assert o["state"] == "delivered", o.get("error")

    fabric = client.get("/api/v1/fabric/ib").json()
    su_site = {su["su_id"]: s["factory_id"]
               for s in fabric["sites"] for su in s["sus"]}
    nodes = client.get(f"/api/v1/nodes?tenant_id={tid}").json()
    used_sites = {su_site[n["rack_id"].rsplit("-rack-", 1)[0]] for n in nodes}
    assert len(used_sites) == 1                      # built within a single site

    sel = client.get(f"/api/v1/fabric/ib?tenant_id={tid}").json()["selected"]
    assert sel["site"] == "IGIS 안산"

    # 120 racks: no single site can hold it alone (Gasan 36, Ansan remaining 64) → rejected
    o2 = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 120}).json()
    assert o2["state"] == "rejected"
    assert "single site" in o2["error"]


def test_fabric_and_nico_are_site_scoped(client):
    _reseed_phase1(client)

    f = client.get("/api/v1/fabric/ib").json()
    assert len(f["sites"]) == 2
    names = {s["name"] for s in f["sites"]}
    assert names == {"STT 가산", "IGIS 안산"}
    by = {s["name"]: s for s in f["sites"]}
    assert sum(len(s["racks"]) for s in by["STT 가산"]["sus"]) == 36
    assert sum(len(s["racks"]) for s in by["IGIS 안산"]["sus"]) == 104
    # Independent spine set per site (id namespace separation)
    spine_ids = {sp["id"] for s in f["sites"]
                 for n in s["networks"] for sp in n["spines"]}
    assert len(spine_ids) == 16                      # 4 spine × 2 nets (A/B) × 2 sites

    site = client.get("/fake-nico/site").json()
    assert {x["name"] for x in site["sites"]} == names
    nico = {x["name"]: x for x in site["sites"]}
    assert nico["STT 가산"]["hosts"] == 36 * 18
    assert nico["IGIS 안산"]["hosts"] == 104 * 18
    host = client.get("/fake-nico/hosts/nh-su-1-rack-00-tray-00").json()
    assert host["site"] == "STT 가산"


def test_inventory_sites_effective_state_per_site(client):
    """Per-site effective resource state — sellable/allocated/unhealthy split + N-site generalization."""
    _reseed_phase1(client)
    d = client.get("/api/v1/inventory/sites").json()
    assert len(d["sites"]) == 2                      # dynamic, one per factory
    by = {s["name"]: s for s in d["sites"]}
    g, a = by["STT 가산"], by["IGIS 안산"]
    assert g["racks_total"] == 36 and a["racks_total"] == 104
    assert g["racks_sellable"] == 36 and g["racks_unhealthy"] == 0
    assert d["totals"]["gpus_total"] == 10080

    # Provisioning + equipment fault → only the effective count of that site decreases
    tid = client.post("/api/v1/tenants", json={
        "name": "inv-ai", "isolation_tier": "bare_metal_dedicated"}).json()["id"]
    o = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 40}).json()
    assert o["state"] == "delivered"                 # placed in Ansan only
    su = client.get("/api/v1/scalable-units/su-1").json()   # Gasan SU
    client.patch("/api/v1/equipment/state", json={
        "kind": "rack", "id": su["rack_ids"][0], "state": "faulted"})

    d2 = client.get("/api/v1/inventory/sites").json()
    by2 = {s["name"]: s for s in d2["sites"]}
    assert by2["IGIS 안산"]["racks_allocated"] == 40
    assert by2["IGIS 안산"]["racks_sellable"] == 104 - 40
    assert by2["STT 가산"]["racks_allocated"] == 0   # verify per-site separation
    assert by2["STT 가산"]["racks_unhealthy"] == 1
    assert by2["STT 가산"]["racks_sellable"] == 35
    assert by2["IGIS 안산"]["tenants"] == [tid]
    assert by2["STT 가산"]["max_single_cluster_racks"] == 35


def test_idle_racks_locked_by_dedicated_su_isolation(client):
    """Real-world repro: even with 24 idle racks, a new 24-rack deal is rejected due to dedicated SU isolation.

    The leftover racks of a partially-occupied SU are 'physically idle' but
    cannot be used for a new deal — the UI (contractable) and the rejection
    reason must show this distinction."""
    _reseed_phase1(client)

    def mk(name):
        return client.post("/api/v1/tenants", json={
            "name": name,
            "isolation_tier": "bare_metal_dedicated"}).json()["id"]

    def order(tid, racks):
        return client.post("/api/v1/orders", json={
            "tenant_id": tid, "kind": "new",
            "blueprint_key": "vr-nvl72", "racks": racks}).json()

    assert order(mk("skb"), 16)["state"] == "delivered"       # Gasan su-1 (1 SU)
    assert order(mk("hyper-a"), 40)["state"] == "delivered"   # Ansan spill
    assert order(mk("hyper-b"), 40)["state"] == "delivered"   # Ansan spill

    inv = {s["name"]: s
           for s in client.get("/api/v1/inventory/sites").json()["sites"]}
    ansan, gasan = inv["IGIS 안산"], inv["STT 가산"]
    assert ansan["racks_sellable"] == 24                 # physically idle
    assert ansan["racks_contractable"] == 8              # available for a new deal
    assert ansan["racks_locked_by_isolation"] == 16      # partially-occupied SU locked
    assert gasan["racks_contractable"] == 20 and gasan["racks_locked_by_isolation"] == 0

    # New tenant, 24 racks → rejected + reason includes per-site availability from the tenant's viewpoint
    t3 = mk("new-corp")
    o = order(t3, 24)
    assert o["state"] == "rejected"
    assert "single site" in o["error"]
    assert "STT 가산 20랙" in o["error"] and "IGIS 안산 8랙" in o["error"]

    # Adjusting as advised succeeds: Gasan 20 racks (max single deal) / Ansan 8 racks
    assert order(t3, 20)["state"] == "delivered"
    assert order(mk("edge-ai"), 8)["state"] == "delivered"
