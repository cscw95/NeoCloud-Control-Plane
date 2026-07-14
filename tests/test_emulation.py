"""NICo read-API surface + compute-tray emulation tests.

Emulation assertions use thresholds, not exact values — the engine is a
random walk and the background ticker may add extra ticks.
"""

TRAYS_PER_RACK = 18


def _mk_tenant(client, name="emu-ai"):
    r = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _deliver(client, tid, racks=1):
    r = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": racks})
    order = r.json()
    assert order["state"] == "delivered", order.get("error")
    return order


# ---------------------------------------------------------------------------
# NICo read API surface
# ---------------------------------------------------------------------------
def test_nico_read_api_surface(client):
    site = client.get("/fake-nico/site").json()
    assert site["ha_nodes"] == 3
    assert {s["name"] for s in site["services"]} >= {
        "API Service", "DHCP", "PXE", "Hardware Health", "Site Agent"}
    assert site["counts"]["hosts_by_state"]["pool_ready"] == 600   # GPU 540 + CPU 풀 60

    types = {t["key"]: t for t in client.get("/fake-nico/instance-types").json()}
    assert types["vr-nvl72"]["hosts_total"] == 288   # 16랙 × 18트레이
    assert types["vr-nvl72"]["hbm_type"] == "HBM4"

    host_id = "nh-su-1-rack-00-tray-00"
    hw = client.get(f"/fake-nico/hosts/{host_id}/hardware").json()
    assert len(hw["gpus"]) == 4 and hw["dpu"]["sku"] == "BF3"
    assert hw["firmware"]["bios"].startswith("GB")

    he = client.get(f"/fake-nico/hosts/{host_id}/health").json()
    assert he["state"] == "standby" and he["leak_detected"] is False

    at = client.get(f"/fake-nico/hosts/{host_id}/attestation").json()
    assert at["attested"] is True and len(at["tpm"]) == 4

    assert client.get("/fake-nico/dhcp/leases").json() == []
    assert client.get("/nico").status_code == 200


def test_nico_instances_and_leases_after_delivery(client):
    tid = _mk_tenant(client)
    _deliver(client, tid, racks=1)
    instances = client.get("/fake-nico/instances").json()
    assert len(instances) == TRAYS_PER_RACK
    assert all(i["tenant_ref"] == tid and i["host_ip"] for i in instances)
    leases = client.get("/fake-nico/dhcp/leases").json()
    assert len(leases) == TRAYS_PER_RACK
    assert all(l["mac"].startswith("0c:42:a1:") for l in leases)
    jobs = client.get("/fake-nico/jobs?limit=5").json()
    assert len(jobs) == 5 and jobs[0]["state"] in ("succeeded", "failed")


# ---------------------------------------------------------------------------
# tray emulation
# ---------------------------------------------------------------------------
def test_emulation_pool_is_standby(client):
    client.post("/api/v1/emu/tick?n=2")
    status = client.get("/api/v1/emu/status").json()
    assert status["trays_total"] == 540 and status["trays_active"] == 0
    assert client.get("/api/v1/emu/clusters").json() == []
    tray = client.get("/api/v1/emu/trays/su-1-rack-00-tray-00").json()
    assert tray["workload"] == "standby"
    assert all(g["util_pct"] == 0 for g in tray["gpus"])


def test_emulation_cluster_lifecycle(client):
    tid = _mk_tenant(client)
    order = _deliver(client, tid, racks=1)

    client.post("/api/v1/emu/tick?n=10")            # training 수렴
    clusters = client.get("/api/v1/emu/clusters").json()
    assert len(clusters) == 1
    c = clusters[0]
    assert c["tenant_id"] == tid
    assert c["trays"] == TRAYS_PER_RACK and c["gpus"] == 72
    assert c["avg_util_pct"] > 18                    # 학습 부하 램프업
    assert c["power_kw"] > 20                        # idle 대비 뚜렷한 부하
    assert c["power_cap_kw"] == 187                  # vr-nvl72 MaxQ (TDP 227)
    assert c["max_gpu_temp_c"] > 45

    trays = client.get(f"/api/v1/emu/trays?tenant_id={tid}").json()
    assert len(trays) == TRAYS_PER_RACK
    t0 = trays[0]
    assert t0["job_name"] and len(t0["gpus"]) == 4
    assert {"util_pct", "temp_c", "power_w", "hbm_used_gb"} <= set(t0["gpus"][0])

    # 워크로드 전환 → idle 수렴
    r = client.post(f"/api/v1/emu/clusters/{tid}/workload",
                    json={"profile": "idle"})
    assert r.status_code == 200
    client.post("/api/v1/emu/tick?n=10")
    c = client.get("/api/v1/emu/clusters").json()[0]
    assert c["profile"] == "idle" and c["avg_util_pct"] < 12

    # 회수 → 클러스터 소멸, 트레이 standby 복귀
    term = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate",
        "allocation_id": order["allocation_ids"][0]}).json()
    assert term["state"] == "closed"
    client.post("/api/v1/emu/tick?n=1")
    assert client.get("/api/v1/emu/clusters").json() == []
    tray = client.get(f"/api/v1/emu/trays/{t0['tray_id']}").json()
    assert tray["workload"] == "standby" and tray["tenant_id"] is None


def test_default_seed_phase1_two_sites_all_vera_rubin():
    """기본 시드 = Phase 1 실배치: STT 가산(2층) + IGIS 안산(2층), 전량 VR.

    가산 6MW/1,728 + 3MW/864 · 안산 12.9MW/3,888 + 10.4MW/3,600 = 10,080 GPU."""
    from app.seed import seed_default
    from app.store import Store
    st = Store()
    seed_default(st)

    assert {f.name for f in st.factories.values()} == {"STT 가산", "IGIS 안산"}
    assert len(st.blocks) == 4                        # 사이트당 2개 층
    floors = {b.name: b for b in st.blocks.values()}
    assert floors["1층 · 6MW"].ready == "2027-03"
    assert floors["2층 · 10.4MW"].ready == "2028-01"

    # 층별 GPU 수 = 계획 수치
    def floor_gpus(block):
        sus = [st.sus[s] for d in block.du_ids
               for s in st.dus[d].su_ids]
        return sum(len(su.rack_ids) for su in sus) * 72
    assert floor_gpus(floors["1층 · 6MW"]) == 1728
    assert floor_gpus(floors["2층 · 3MW"]) == 864
    assert floor_gpus(floors["1층 · 12.9MW"]) == 3888
    assert floor_gpus(floors["2층 · 10.4MW"]) == 3600

    assert len(st.racks) == 140 and len(st.gpus) == 10080
    assert all(r.blueprint_key == "vr-nvl72" for r in st.racks.values())
    capped_mw = sum(r.power_cap_kw for r in st.racks.values()) / 1000
    assert capped_mw == 26.18                         # 140랙 × 187kW MaxQ
    assert len(st.cpu_nodes) == 60


def test_ib_fabric_topology_and_tenant_view(client):
    tid = _mk_tenant(client)
    order = _deliver(client, tid, racks=1)

    f = client.get("/api/v1/fabric/ib").json()
    assert len(f["sites"]) == 1                      # 테스트 시드 = 단일 사이트
    site = f["sites"][0]
    assert len(site["networks"]) == 2               # 듀얼 IB (Fabric-A/B)
    assert all(len(n["spines"]) == 4 for n in site["networks"])
    assert {su["su_id"] for su in site["sus"]} == {"su-1", "su-2"}
    vr = [su for su in site["sus"] if su["blueprint_key"] == "vr-nvl72"][0]
    assert vr["rails"] == 8 and len(vr["racks"]) == 16   # NCP RD: VR SU=16랙
    assert vr["links_800g"] == 2304 and vr["leaves_per_network"] == 16
    gb = [su for su in site["sus"] if su["blueprint_key"] == "gb200-nvl72"][0]
    assert gb["rails"] == 4                          # rail 수 = SuperNIC/tray

    t = [x for x in f["tenants"] if x["tenant_id"] == tid][0]
    assert t["gpus"] == 72 and t["racks"] == 1
    assert t["pkey"] and t["pkey"].startswith("0x8")
    assert t["site"] == site["name"]                 # 테넌트 → 소속 사이트 표기

    sel = client.get(f"/api/v1/fabric/ib?tenant_id={tid}").json()["selected"]
    assert sel and sel["sus"] == ["su-2"]

    # 확장 주문 시 P_Key 재사용 (테넌트당 1개 유지)
    client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 2})
    f2 = client.get(f"/api/v1/fabric/ib?tenant_id={tid}").json()["selected"]
    assert f2["racks"] == 3 and f2["pkey"] == t["pkey"]

    # 첫 주문만 회수 — 잔여 2랙은 fabric에 유지

    client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate",
        "allocation_id": order["allocation_ids"][0]})
    f3 = client.get(f"/api/v1/fabric/ib?tenant_id={tid}").json()["selected"]
    assert f3["racks"] == 2


def test_su_scale_order_spans_sus(client):
    """SU 1개 주문 — 16랙(VR SU 전체, NCP RD) 단일 SU 배치."""
    tid = _mk_tenant(client)
    order = _deliver(client, tid, racks=16)          # vr SU 통째
    assert len(order["node_ids"]) == 16 * TRAYS_PER_RACK
    assert len(order["allocation_ids"]) == 1         # 단일 SU rack_set
    f = client.get(f"/api/v1/fabric/ib?tenant_id={tid}").json()["selected"]
    assert f["racks"] == 16 and f["gpus"] == 1152   # 1 SU = 1,152 GPU


def test_order_flow_buckets_downstream_apis(client):
    """/orders/{id}/flow — 단계별 하부 API 전체 리스트 (NICo 내부 포함)."""
    tid = _mk_tenant(client)
    order = _deliver(client, tid, racks=1)

    flow = client.get(f"/api/v1/orders/{order['id']}/flow").json()
    assert [s["state"] for s in flow["stages"]] == [
        "received", "validated", "reserved", "provisioning",
        "isolating", "storage_binding", "acceptance", "delivered"]
    by = {s["state"]: s for s in flow["stages"]}

    assert by["reserved"]["by_channel"].get("gRPC") == 18          # ReserveHost
    prov = by["provisioning"]["by_channel"]
    assert prov.get("rshim") == 18            # DPU provisioning — BFB(호스트당 1)
    assert prov["Redfish"] == 36                                   # 2건 × 18호스트
    assert prov["DHCP"] == 18 and prov["PXE"] == 18
    assert prov["cloud-init"] == 18 and prov["gRPC"] == 18         # Allocate
    iso = by["isolating"]["by_channel"]
    assert iso.get("NVUE/HBN") == 18 + 5           # GPU 트레이 + 기본 CPU 노드
    assert iso.get("UFM") == 1 and iso.get("NMX") == 1
    assert by["storage_binding"]["by_channel"].get("VAST-API") == 3
    assert by["acceptance"]["by_channel"].get("internal", 0) >= 3  # verify 3종
    # 이벤트 내용까지 노출되는지
    sample = by["provisioning"]["apis"][0]
    assert {"seq", "src", "dst", "channel", "op"} <= set(sample)

    # terminate 주문의 flow — 역순 해체 + sanitize 단계 포함
    term = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate",
        "allocation_id": order["allocation_ids"][0]}).json()
    tf = client.get(f"/api/v1/orders/{term['id']}/flow").json()
    assert [s["state"] for s in tf["stages"]] == [
        "received", "validated", "reclaiming", "closed"]
    rec = {s["state"]: s for s in tf["stages"]}["reclaiming"]["by_channel"]
    assert rec.get("NMX") == 1 and rec.get("UFM") == 1
    assert rec.get("VAST-API", 0) >= 1
    assert rec.get("internal", 0) >= 18 * 7                        # sanitize 7단계
    assert client.get("/arch").status_code == 200


def test_emulation_history_timeseries(client):
    """/emu/history — 전역·테넌트별 시계열 (포털 라인 그래프 데이터)."""
    tid = _mk_tenant(client)
    _deliver(client, tid, racks=1)
    client.post("/api/v1/emu/tick?n=15")

    g = client.get("/api/v1/emu/history").json()
    assert len(g) >= 15
    last = g[-1]
    assert {"avg_util_pct", "power_kw", "active_gpus", "alloc_pct",
            "tokens_ks", "max_gpu_temp_c"} <= set(last)
    assert last["active_gpus"] == 72 and last["total_gpus"] == 2160
    assert last["avg_util_pct"] > 10                  # training 램프업 반영

    t = client.get(f"/api/v1/emu/history?tenant_id={tid}").json()
    assert len(t) >= 15 and t[-1]["gpus"] == 72
    assert t[-1]["power_cap_kw"] == 187

    assert len(client.get("/api/v1/emu/history?limit=5").json()) == 5
    # 회수 후 테넌트 시계열 정리
    order = client.get("/api/v1/orders").json()[0]
    client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate",
        "allocation_id": order["allocation_ids"][0]})
    client.post("/api/v1/emu/tick?n=1")
    assert client.get(f"/api/v1/emu/history?tenant_id={tid}").json() == []


def test_nico_bulk_health_for_observability(client):
    """운영 포털 Observability — NICo REST 벌크 센서 직접 연동."""
    tid = _mk_tenant(client)
    _deliver(client, tid, racks=1)
    client.post("/api/v1/emu/tick?n=5")

    mine = client.get(f"/fake-nico/health?tenant_ref={tid}").json()
    assert len(mine) == TRAYS_PER_RACK
    h = mine[0]
    assert {"host_id", "tray_id", "instance_id", "host_ip", "nico_state",
            "power_w", "gpu_temp_c", "coolant_supply_c"} <= set(h)
    assert h["tenant_ref"] == tid and h["nico_state"] == "allocated"
    assert h["power_w"] > 2000 and len(h["gpu_temp_c"]) == 4

    everything = client.get("/fake-nico/health").json()
    assert len(everything) == 600     # GPU 트레이 540 + CPU 노드 60
    standby = [x for x in everything if x["tenant_ref"] is None]
    assert standby and standby[0]["state"] == "standby"


def test_emulation_health_reflects_runtime(client):
    tid = _mk_tenant(client)
    _deliver(client, tid, racks=1)
    client.post("/api/v1/emu/tick?n=8")
    node = client.get(f"/api/v1/nodes?tenant_id={tid}").json()[0]
    he = client.get(f"/fake-nico/hosts/{node['nico_host_id']}/health").json()
    assert he["state"] in ("ok", "warning")
    assert he["power_w"] > 2000                      # 활성 트레이 부하 반영
    assert max(he["gpu_temp_c"]) > 45


def test_gpu_fault_metrics_availability_and_mttr(client):
    """GPU 장애 조치 지표 — 가용성·TTA·TTR·MTTR (/emu/faults)."""
    from app.tray_emu import EMULATOR
    tid = _mk_tenant(client, "mttr-ai")
    _deliver(client, tid, racks=1)
    client.post("/api/v1/emu/tick?n=2")

    # 결정적 검증: 장애 에피소드를 직접 주입 (랜덤 XID와 동일 경로)
    with EMULATOR._lock:
        rt = next(r for r in EMULATOR.trays.values() if r.tenant_id == tid)
        g = rt.gpus[0]
        g.fault_ttl, g.state = 5, "fault"
        g.xid_events.append(79)
        from datetime import datetime, timezone
        EMULATOR.fault_log.append({
            "tray_id": rt.tray_id, "host_id": rt.host_id, "gpu": g.index,
            "xid": 79, "tenant_id": tid,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "started_step": EMULATOR.step,
            "action": "NVSentinel — cordon/drain 지정 후 복구 절차",
            "tta_s": 2, "resolved_at": None, "ttr_s": None, "state": "open"})

    f1 = client.get(f"/api/v1/emu/faults?tenant_id={tid}").json()
    assert f1["faults_open"] == 1 and f1["gpus_total"] == 72
    assert f1["availability_pct"] < 100.0            # 진행 장애 반영
    assert f1["mttr_s"] is None                      # 아직 복구 전

    client.post("/api/v1/emu/tick?n=6")              # fault_ttl 5 소진 → 복구
    f2 = client.get(f"/api/v1/emu/faults?tenant_id={tid}").json()
    assert f2["faults_open"] == 0 and f2["faults_resolved"] == 1
    assert f2["availability_pct"] == 100.0
    assert f2["mttr_s"] and f2["mttr_s"] >= 8        # ≥4틱 × 2s
    ep = f2["recent"][0]
    assert ep["state"] == "resolved" and ep["ttr_s"] == f2["mttr_s"]
    assert ep["tta_s"] == 2 and ep["xid"] == 79


def test_random_xid_injection_gated_by_env(client, monkeypatch):
    """확률적 XID 주입은 NOCP_RANDOM_FAULTS=1 일 때만 동작 (기본 비활성)."""
    from app import tray_emu
    tid = _mk_tenant(client, "nofault-ai")
    _deliver(client, tid, racks=1)

    # random()이 항상 0을 반환해도 (주입 확률 100% 상황) env 미설정이면 미주입
    monkeypatch.setattr(tray_emu.random, "random", lambda: 0.0)
    monkeypatch.delenv("NOCP_RANDOM_FAULTS", raising=False)
    n0 = len(tray_emu.EMULATOR.fault_log)
    client.post("/api/v1/emu/tick?n=3")
    assert len(tray_emu.EMULATOR.fault_log) == n0

    # opt-in 하면 기존 랜덤 주입 경로가 그대로 동작
    monkeypatch.setenv("NOCP_RANDOM_FAULTS", "1")
    client.post("/api/v1/emu/tick?n=1")
    assert len(tray_emu.EMULATOR.fault_log) > n0


def test_seed_sample_faults_and_tickets_for_demo():
    """데모 리셋 후 알림/티켓 메뉴가 비지 않도록 샘플이 시드된다."""
    from app.seed import seed_default, seed_demo_samples
    from app.store import STORE
    from app.tray_emu import EMULATOR

    seed_default(STORE, blueprints=["vr-nvl72"])
    EMULATOR.reset()
    try:
        seed_demo_samples(STORE)
        samples = [f for f in EMULATOR.fault_log
                   if "(sample)" in f.get("detail", "")]
        assert len(samples) == 2
        assert sum(1 for f in samples if f["resolved_at"]) == 1      # resolved 1
        assert sum(1 for f in samples if f["resolved_at"] is None) == 1  # 대응 중
        assert {f["xid"] for f in samples} <= {63, 79, 48}
        assert len(STORE.tickets) == 2
        assert all("(sample)" in t.body for t in STORE.tickets.values())
        assert {t.status for t in STORE.tickets.values()} == {"open", "resolved"}
    finally:
        EMULATOR.reset()
