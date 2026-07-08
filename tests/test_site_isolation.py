"""사이트 분리 — 가산·안산은 독립 클러스터 (IB/NVLink 크로스 없음).

M4 배치는 사이트 경계를 넘는 spill을 금지하고, fabric·NICo read-model은
사이트별로 분리되어야 한다."""


def _reseed_phase1(client):
    r = client.post("/api/v1/admin/reseed")          # 기본 = Phase 1 2사이트
    assert r.json()["scalable_units"] == 11


def test_cluster_never_spans_sites(client):
    _reseed_phase1(client)
    tid = client.post("/api/v1/tenants", json={
        "name": "span-ai", "isolation_tier": "bare_metal_dedicated"}).json()["id"]

    # 40랙: 가산 총량(36랙) 초과 → 안산 내 SU spill 로만 구성되어야 한다
    o = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 40}).json()
    assert o["state"] == "delivered", o.get("error")

    fabric = client.get("/api/v1/fabric/ib").json()
    su_site = {su["su_id"]: s["factory_id"]
               for s in fabric["sites"] for su in s["sus"]}
    nodes = client.get(f"/api/v1/nodes?tenant_id={tid}").json()
    used_sites = {su_site[n["rack_id"].rsplit("-rack-", 1)[0]] for n in nodes}
    assert len(used_sites) == 1                      # 단일 사이트 내 구성

    sel = client.get(f"/api/v1/fabric/ib?tenant_id={tid}").json()["selected"]
    assert sel["site"] == "IGIS 안산"

    # 120랙: 어느 사이트도 단독 수용 불가(가산 36 · 안산 잔여 64) → 거절
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
    # 사이트별 독립 스파인 세트 (id 네임스페이스 분리)
    spine_ids = {sp["id"] for s in f["sites"]
                 for n in s["networks"] for sp in n["spines"]}
    assert len(spine_ids) == 16                      # 4 spine × 2망(A/B) × 2사이트

    site = client.get("/fake-nico/site").json()
    assert {x["name"] for x in site["sites"]} == names
    nico = {x["name"]: x for x in site["sites"]}
    assert nico["STT 가산"]["hosts"] == 36 * 18
    assert nico["IGIS 안산"]["hosts"] == 104 * 18
    host = client.get("/fake-nico/hosts/nh-su-1-rack-00-tray-00").json()
    assert host["site"] == "STT 가산"


def test_inventory_sites_effective_state_per_site(client):
    """사이트별 자원 유효 상태 — 판매가능/할당/비정상 분리 + N사이트 일반화."""
    _reseed_phase1(client)
    d = client.get("/api/v1/inventory/sites").json()
    assert len(d["sites"]) == 2                      # factories 수만큼 동적
    by = {s["name"]: s for s in d["sites"]}
    g, a = by["STT 가산"], by["IGIS 안산"]
    assert g["racks_total"] == 36 and a["racks_total"] == 104
    assert g["racks_sellable"] == 36 and g["racks_unhealthy"] == 0
    assert d["totals"]["gpus_total"] == 10080

    # 개통 + 장비 장애 → 해당 사이트의 유효 수량만 감소
    tid = client.post("/api/v1/tenants", json={
        "name": "inv-ai", "isolation_tier": "bare_metal_dedicated"}).json()["id"]
    o = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 40}).json()
    assert o["state"] == "delivered"                 # 안산에만 배치됨
    su = client.get("/api/v1/scalable-units/su-1").json()   # 가산 SU
    client.patch("/api/v1/equipment/state", json={
        "kind": "rack", "id": su["rack_ids"][0], "state": "faulted"})

    d2 = client.get("/api/v1/inventory/sites").json()
    by2 = {s["name"]: s for s in d2["sites"]}
    assert by2["IGIS 안산"]["racks_allocated"] == 40
    assert by2["IGIS 안산"]["racks_sellable"] == 104 - 40
    assert by2["STT 가산"]["racks_allocated"] == 0   # 사이트별 분리 확인
    assert by2["STT 가산"]["racks_unhealthy"] == 1
    assert by2["STT 가산"]["racks_sellable"] == 35
    assert by2["IGIS 안산"]["tenants"] == [tid]
    assert by2["STT 가산"]["max_single_cluster_racks"] == 35


def test_idle_racks_locked_by_dedicated_su_isolation(client):
    """실사용 재현: 유휴 24랙이 있어도 dedicated SU 격리로 신규 24랙 계약 거절.

    부분 점유 SU의 잔여 랙은 '물리 유휴'지만 신규 계약에는 못 쓴다 —
    UI(contractable)와 거절 사유가 이를 구분해 보여줘야 한다."""
    _reseed_phase1(client)

    def mk(name):
        return client.post("/api/v1/tenants", json={
            "name": name,
            "isolation_tier": "bare_metal_dedicated"}).json()["id"]

    def order(tid, racks):
        return client.post("/api/v1/orders", json={
            "tenant_id": tid, "kind": "new",
            "blueprint_key": "vr-nvl72", "racks": racks}).json()

    assert order(mk("skb"), 16)["state"] == "delivered"       # 가산 su-1 (1 SU)
    assert order(mk("hyper-a"), 40)["state"] == "delivered"   # 안산 spill
    assert order(mk("hyper-b"), 40)["state"] == "delivered"   # 안산 spill

    inv = {s["name"]: s
           for s in client.get("/api/v1/inventory/sites").json()["sites"]}
    ansan, gasan = inv["IGIS 안산"], inv["STT 가산"]
    assert ansan["racks_sellable"] == 24                 # 물리 유휴
    assert ansan["racks_contractable"] == 8              # 신규 계약 가능
    assert ansan["racks_locked_by_isolation"] == 16      # 부분 점유 SU 잠김
    assert gasan["racks_contractable"] == 20 and gasan["racks_locked_by_isolation"] == 0

    # 신규 테넌트 24랙 → 거절 + 사유에 테넌트 기준 사이트별 가용 내역
    t3 = mk("new-corp")
    o = order(t3, 24)
    assert o["state"] == "rejected"
    assert "single site" in o["error"]
    assert "STT 가산 20랙" in o["error"] and "IGIS 안산 8랙" in o["error"]

    # 안내대로 조정하면 성공: 가산 20랙(최대 단일 계약) / 안산 8랙
    assert order(t3, 20)["state"] == "delivered"
    assert order(mk("edge-ai"), 8)["state"] == "delivered"
