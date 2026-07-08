"""E2E 시연 시나리오 — 초기화부터 전체 라이프사이클까지 한 여정으로 고정.

scripts/demo_scenario.py(라이브 시연 러너)와 동일한 11막 구성을 CI 스케일
(2-SU 시드)로 검증한다. 막 구성:
  0 초기화 → 1 인프라 검증 → 2 테넌트 온보딩 → 3 개통(F1) → 4 플로우 감사
  → 5 에뮬레이션 운영 → 6 장애/saga → 7 정합성(reconcile) → 8 확장
  → 9 회수(F4) → 10 최종 초기화 (시작 상태 복원)
"""

from app.nico_fake import FAKE_NICO, NicoHostState


def _post(client, path, body=None):
    r = client.post(path, json=body) if body is not None else client.post(path)
    assert r.status_code < 400, f"{path}: {r.status_code} {r.text[:200]}"
    return r.json()


def test_full_demo_scenario(client):
    # ── ACT 0. 초기화 (2-SU 스케일로 재시드) ────────────────────────────
    rs = _post(client, "/api/v1/admin/reseed?blueprints=gb200-nvl72,vr-nvl72")
    assert rs["nodes"] == 540
    assert client.get("/api/v1/nodes/summary").json()["by_state"] == {
        "pool_ready": 540}
    assert client.get("/fake-vast/views").json() == []
    assert client.get("/fake-nico/segments").json() == []
    for page in ("/", "/flow", "/nico", "/arch"):
        assert client.get(page).status_code == 200

    # ── ACT 1. 인프라 검증 ──────────────────────────────────────────────
    site = client.get("/fake-nico/site").json()
    assert site["counts"]["hosts_by_state"]["pool_ready"] == 540
    fabric = client.get("/api/v1/fabric/ib").json()
    assert len(fabric["sites"]) == 1                 # 테스트 시드 = 단일 사이트
    assert len(fabric["sites"][0]["networks"]) == 2      # Fabric-A/B 듀얼
    assert all(len(n["spines"]) == 4 for n in fabric["sites"][0]["networks"])
    assert len(fabric["sites"][0]["sus"]) == 2
    assert client.get("/api/v1/emu/status").json()["trays_active"] == 0

    # ── ACT 2. 테넌트 온보딩 (VNI/VRF 자동 바인딩) ──────────────────────
    acme = _post(client, "/api/v1/tenants",
                 {"name": "acme-ai", "isolation_tier": "bare_metal_dedicated"})
    ni = client.get(f"/api/v1/tenants/{acme['id']}").json()["network_isolation"]
    assert ni["vrf"] == "VRF-acme-ai" and ni["compute_l3vni"] >= 10000

    # ── ACT 3. 개통 F1 (3랙 = 54노드) ───────────────────────────────────
    o1 = _post(client, "/api/v1/orders", {
        "tenant_id": acme["id"], "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 3})
    assert o1["state"] == "delivered" and len(o1["node_ids"]) == 54
    assert len(client.get("/fake-nico/instances").json()) == 54
    assert len(client.get("/fake-nico/dhcp/leases").json()) == 54
    views = client.get("/fake-vast/views").json()
    assert len(views) == 1 and views[0]["capacity_tb"] == 1500
    assert client.get(
        f"/api/v1/tenants/{acme['id']}/isolation").json()["ok"] is True

    # ── ACT 4. 플로우 감사 (단계별 하부 API) ────────────────────────────
    flow = client.get(f"/api/v1/orders/{o1['id']}/flow").json()
    by = {s["state"]: s for s in flow["stages"]}
    assert by["provisioning"]["by_channel"]["Redfish"] == 108   # 2×54
    # HBN = GPU 트레이 54 + 기본 CPU 노드 5 (DPU VPC 연결)
    assert by["isolating"]["by_channel"]["NVUE/HBN"] == 54 + 5
    assert by["storage_binding"]["by_channel"]["VAST-API"] == 3
    cpus = client.get(f"/api/v1/cpu-nodes?tenant_id={acme['id']}").json()
    assert len(cpus) == 5 and all(c["host_ip"] for c in cpus)

    # ── ACT 5. 에뮬레이션 운영 (training → inference) ───────────────────
    _post(client, "/api/v1/emu/tick?n=10")
    cl = client.get("/api/v1/emu/clusters").json()[0]
    assert cl["gpus"] == 216 and cl["avg_util_pct"] > 18
    _post(client, f"/api/v1/emu/clusters/{acme['id']}/workload",
          {"profile": "inference"})
    _post(client, "/api/v1/emu/tick?n=10")
    cl = client.get("/api/v1/emu/clusters").json()[0]
    assert cl["profile"] == "inference"

    # ── ACT 6. 장애 주입 → saga 보상 → 재시도 성공 ─────────────────────
    beta = _post(client, "/api/v1/tenants",
                 {"name": "beta-lab", "isolation_tier": "bare_metal_dedicated"})
    FAKE_NICO.inject_failure("nh-su-1-rack-00-tray-00", "provision")
    fail = _post(client, "/api/v1/orders", {
        "tenant_id": beta["id"], "kind": "new",
        "blueprint_key": "gb200-nvl72", "racks": 1})
    assert fail["state"] == "failed" and "provision failed" in fail["error"]
    summary = client.get("/api/v1/nodes/summary").json()["by_state"]
    assert summary.get("quarantined") == 1
    retry = _post(client, "/api/v1/orders", {
        "tenant_id": beta["id"], "kind": "new",
        "blueprint_key": "gb200-nvl72", "racks": 1})
    assert retry["state"] == "delivered"
    beta_alloc = retry["allocation_ids"][0]

    # ── ACT 6.5 티켓: 고객 접수 → 운영 처리 (open→in_progress→resolved) ──
    tck = _post(client, "/api/v1/tickets", {
        "tenant_id": beta["id"], "subject": "프로비저닝 실패 문의",
        "severity": "high", "ref": fail["id"]})
    assert tck["status"] == "open" and tck["id"].startswith("tck-")
    r = client.patch(f"/api/v1/tickets/{tck['id']}", json={
        "status": "in_progress", "comment": "break-fix 이관", "author": "operator"})
    assert r.status_code == 200
    done = client.patch(f"/api/v1/tickets/{tck['id']}", json={
        "status": "resolved", "comment": "재시도 delivered", "author": "operator"}).json()
    assert done["status"] == "resolved" and len(done["comments"]) == 2
    mine = client.get("/api/v1/tickets",
                      params={"tenant_id": beta["id"], "status": "resolved"}).json()
    assert [t["id"] for t in mine] == [tck["id"]]

    # ── ACT 7. 정합성 — GHOST/ORPHAN/MISMATCH 검출 → 복구 ──────────────
    FAKE_NICO.add_ghost("nh-ghost-demo")
    orphan_host = "nh-su-2-rack-10-tray-00"          # pool_ready 랙
    FAKE_NICO.hosts.pop(orphan_host)
    victim = client.get("/api/v1/nodes",
                        params={"tenant_id": acme["id"]}).json()[0]
    FAKE_NICO.hosts[victim["nico_host_id"]].state = NicoHostState.pool_ready
    rec = _post(client, "/api/v1/reconcile/run")
    assert rec["ghosts_registered"] == 1
    assert rec["orphans_cordoned"] == 1
    assert rec["mismatches"] == 1 and rec["ok"] is False
    # 운영자 복구(상태 원복) 후 mismatch 해소 — orphan은 물리 소실이라 상주
    FAKE_NICO.hosts[victim["nico_host_id"]].state = NicoHostState.allocated
    rec2 = _post(client, "/api/v1/reconcile/run")
    assert rec2["mismatches"] == 0

    # ── ACT 8. 확장 (P_Key 재사용) ──────────────────────────────────────
    pkey1 = client.get("/api/v1/fabric/ib",
                       params={"tenant_id": acme["id"]}).json()["selected"]["pkey"]
    exp = _post(client, "/api/v1/orders", {
        "tenant_id": acme["id"], "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 1})
    assert exp["state"] == "delivered"
    sel = client.get("/api/v1/fabric/ib",
                     params={"tenant_id": acme["id"]}).json()["selected"]
    assert sel["racks"] == 4 and sel["pkey"] == pkey1

    # ── ACT 9. 회수 F4 (sanitize·스토리지/VPC 해체) ─────────────────────
    term = _post(client, "/api/v1/orders", {
        "tenant_id": beta["id"], "kind": "terminate",
        "allocation_id": beta_alloc})
    assert term["state"] == "closed" and term["error"] is None
    node = client.get(f"/api/v1/nodes/{term['node_ids'][0]}").json()
    report = client.get(
        f"/fake-nico/hosts/{node['nico_host_id']}/sanitize-report").json()
    assert report["passed"] and len(report["steps"]) == 7
    # beta 자원 해체, acme(2건) 자원 유지
    assert len(client.get("/fake-vast/views").json()) == 2
    segs = client.get("/fake-nico/segments").json()
    assert all(sg["tenant_ref"] == acme["id"] for sg in segs)

    # ── ACT 9.5 과금 프리뷰: beta 마감·acme 활성 (비즈 포털) ────────────
    usage = client.get("/api/v1/billing/usage").json()
    beta_line = next(l for l in usage["lines"] if l["tenant_id"] == beta["id"])
    assert beta_line["active"] is False and beta_line["end"] is not None
    assert beta_line["projected_monthly_usd"] == 0.0
    acme_lines = [l for l in usage["lines"] if l["tenant_id"] == acme["id"]]
    assert len(acme_lines) == 2 and all(l["active"] for l in acme_lines)
    assert usage["totals"]["projected_monthly_usd"] > 0
    only_beta = client.get("/api/v1/billing/usage",
                           params={"tenant_id": beta["id"]}).json()
    assert len(only_beta["lines"]) == 1

    # ── ACT 10. 최종 초기화 — 시작 상태 복원 ────────────────────────────
    _post(client, "/api/v1/admin/reseed?blueprints=gb200-nvl72,vr-nvl72")
    assert client.get("/api/v1/nodes/summary").json()["by_state"] == {
        "pool_ready": 540}
    assert client.get("/api/v1/orders").json() == []
    assert client.get("/api/v1/tickets").json() == []
    assert client.get("/fake-vast/views").json() == []
    assert client.get("/fake-nico/segments").json() == []
    assert client.get("/api/v1/trace?limit=10").json() == []
