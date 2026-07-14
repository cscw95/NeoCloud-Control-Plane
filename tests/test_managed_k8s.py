"""Managed K8s 옵션 — fulfillment 파이프라인·CPU CP 노드·Converged attach·
DCGM in-band 전환·회수까지의 회귀 테스트.

설계 근거:
- CP CPU 노드 3대는 AI Infra Emulator(FakeNico) 인벤토리의 실제 호스트로,
  GPU 트레이와 동일한 DPU 기반 Day1 경로(reserve→provision→allocate)로
  IP·OS 설치 후 테넌트에 할당된다.
- CP 노드는 테넌트 VPC 세그먼트에 Converged Network(VNI)로 attach 된다.
- Managed K8s 제공 시 DCGM 수집은 in-band(dcgm-exporter)로 전환된다.
"""
from collections import Counter

from app.nico_fake import FAKE_NICO


def _mk_tenant(client, name="k8s-corp"):
    r = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _k8s_order(client, tenant_id, racks=1, version="v1.32.4", **kw):
    return client.post("/api/v1/orders", json={
        "tenant_id": tenant_id, "kind": "new", "blueprint_key": "vr-nvl72",
        "racks": racks, "managed_k8s": True, "k8s_version": version, **kw})


def test_managed_k8s_full_lifecycle(client):
    tid = _mk_tenant(client)
    r = _k8s_order(client, tid)
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["state"] == "delivered", o.get("error")
    assert o["managed_k8s"] is True and o["k8s_cluster_id"]

    # ── 상태머신: acceptance → k8s_installing → delivered ────────────────
    states = [h["state"] for h in o["history"]]
    assert "k8s_installing" in states
    assert states.index("acceptance") < states.index("k8s_installing") \
        < states.index("delivered")

    # ── K8s 클러스터 read-model ──────────────────────────────────────────
    c = client.get(f"/api/v1/k8s/clusters/{o['k8s_cluster_id']}").json()
    assert c["state"] == "running" and c["version"] == "v1.32.4"
    assert len(c["cp_node_ids"]) == 3            # CP 3대 (HA·etcd 정족수)
    assert c["dcgm_mode"] == "in-band"
    assert c["gpus_total"] == 72                 # VR NVL72 1랙
    addons = {a["name"] for a in c["addons"]}
    assert {"cilium", "gpu-operator", "network-operator",
            "dcgm-exporter", "kube-vip"} <= addons
    checks = {x["check"]: x["result"] for x in c["conditions"]}
    assert set(checks) >= {"nodes-ready", "gpu-allocatable", "nccl-smoke",
                           "dcgm-diag", "api-vip-ha"}
    assert all(v == "PASS" for v in checks.values())

    # ── CPU 노드: 기본 5(general) + K8s CP 3(k8s_cp) ─────────────────────
    cpus = client.get(f"/api/v1/cpu-nodes?tenant_id={tid}").json()
    roles = Counter(cn["role"] for cn in cpus)
    assert roles == {"general": 5, "k8s_cp": 3}

    # ── CP 노드는 NICo(DPU isolation) 경유로 실제 할당되어야 한다 ────────
    cp = [cn for cn in cpus if cn["role"] == "k8s_cp"]
    for cn in cp:
        h = client.get(f"/fake-nico/hosts/{cn['nico_host_id']}").json()
        assert h["state"] == "allocated" and h["tenant_ref"] == tid
        assert h["host_ip"].startswith("10.250.1.")   # DPU-DHCP CPU 대역
        assert h["instance_id"]                        # AllocateInstance 완료
        assert cn["order_id"] == o["id"]

    # ── Converged Network: CP 노드가 테넌트 세그먼트에 attach ────────────
    seg = next(s for s in client.get("/fake-nico/segments").json()
               if s["tenant_ref"] == tid)
    for cn in cp:
        assert cn["nico_host_id"] in seg["host_ids"]

    # ── DCGM in-band 전환 (에뮬레이션 지표) ──────────────────────────────
    f = client.get(f"/api/v1/emu/faults?tenant_id={tid}").json()
    assert f["dcgm_source"].startswith("in-band")

    # ── 접속 패키지: kubeconfig(OIDC) 섹션 ───────────────────────────────
    mk = o["access_package"]["managed_k8s"]
    assert mk["cluster_id"] == c["id"]
    assert mk["api_server"] == f"https://{c['api_vip']}:6443"
    assert "OIDC" in mk["kubeconfig"]

    # ── /flow: k8s_installing 버킷에 K8s 채널 이벤트 ─────────────────────
    flow = client.get(f"/api/v1/orders/{o['id']}/flow").json()
    stage = next(st for st in flow["stages"]
                 if st["state"] == "k8s_installing")
    assert stage["by_channel"].get("K8s", 0) >= 10   # 부트스트랩+애드온+검증
    assert stage["by_channel"].get("rshim", 0) >= 3  # CP 3대 BFB (DPU 경로)
    ops = " ".join(a["op"] for a in stage["apis"])
    assert "kubeadm init" in ops and "DCGM in-band" in ops

    # ── 회수: 클러스터 해체 + CP 노드 NICo 반납 ──────────────────────────
    r = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate",
        "allocation_id": o["allocation_ids"][0]})
    assert r.json()["state"] == "closed", r.json().get("error")
    c2 = client.get(f"/api/v1/k8s/clusters/{c['id']}").json()
    assert c2["state"] == "deleted"
    for cn in cp:
        h = client.get(f"/fake-nico/hosts/{cn['nico_host_id']}").json()
        assert h["state"] == "pool_ready" and h["tenant_ref"] is None
    cpus_after = client.get(f"/api/v1/cpu-nodes?tenant_id={tid}").json()
    assert cpus_after == []                       # 마지막 회수 — 전량 반납
    assert f_dcgm_oob(client, tid)


def f_dcgm_oob(client, tid) -> bool:
    f = client.get(f"/api/v1/emu/faults?tenant_id={tid}").json()
    return f["dcgm_source"].startswith("oob")


def test_managed_k8s_invalid_version_rejected(client):
    tid = _mk_tenant(client)
    r = _k8s_order(client, tid, version="v1.19.0")
    assert r.json()["state"] == "rejected"
    assert "지원 버전" in r.json()["error"]


def test_managed_k8s_approval_gates_include_k8s_stage(client):
    """승인 모드 — K8s 옵션 주문은 게이트가 8개(k8s_installing 포함)."""
    tid = _mk_tenant(client)
    o = _k8s_order(client, tid, approval_mode=True).json()
    visited = []
    while o["pending_stage"]:
        visited.append(o["pending_stage"])
        o = client.post(f"/api/v1/orders/{o['id']}/approve").json()
        assert o["state"] not in ("failed", "rejected"), o.get("error")
    assert visited == ["validated", "reserved", "provisioning", "isolating",
                       "storage_binding", "acceptance", "k8s_installing",
                       "delivered"]
    assert o["state"] == "delivered" and o["k8s_cluster_id"]


def test_plain_order_pipeline_unchanged(client):
    """비옵션 주문은 기존 7단계 그대로 — k8s_installing 미출현."""
    tid = _mk_tenant(client)
    o = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new", "blueprint_key": "vr-nvl72",
        "racks": 1}).json()
    assert o["state"] == "delivered"
    assert o["managed_k8s"] is False and o["k8s_cluster_id"] is None
    assert "k8s_installing" not in [h["state"] for h in o["history"]]
    assert "managed_k8s" not in (o["access_package"] or {})
    cpus = client.get(f"/api/v1/cpu-nodes?tenant_id={tid}").json()
    assert Counter(cn["role"] for cn in cpus) == {"general": 5}
    f = client.get(f"/api/v1/emu/faults?tenant_id={tid}").json()
    assert f["dcgm_source"].startswith("oob")


def test_k8s_cp_provision_failure_compensates(client):
    """CP 노드 프로비저닝 실패 — saga 보상으로 GPU 노드·CPU 노드 원복."""
    tid = _mk_tenant(client)
    # 첫 pool_ready CPU 노드가 CP 후보 1번 — provision 실패 주입
    first_free = sorted(
        h.host_id for h in FAKE_NICO.hosts.values()
        if h.sku == "cpu-epyc")[5]     # 앞 5대는 기본 제공분이 선점
    FAKE_NICO.inject_failure(first_free, "provision")
    o = _k8s_order(client, tid).json()
    assert o["state"] == "failed"
    assert "k8s cp node setup failed" in o["error"]
    # CPU 노드 전량 반납 확인 (k8s_cp 잔존 없음)
    cpus = client.get("/api/v1/cpu-nodes").json()
    assert all(cn["role"] != "k8s_cp" for cn in cpus)
    # GPU 노드도 보상으로 풀 복귀
    nodes = client.get(f"/api/v1/nodes?tenant_id={tid}").json()
    assert nodes == []


def test_managed_k8s_day2_addon_on_existing_bmaas(client):
    """이미 BMaaS로 개통된 테넌트에 K8s 추가 설치 (Day-2 애드온)."""
    tid = _mk_tenant(client)
    o = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new", "blueprint_key": "vr-nvl72",
        "racks": 1}).json()
    assert o["state"] == "delivered" and o["managed_k8s"] is False

    r = client.post("/api/v1/k8s/installs", json={
        "tenant_id": tid, "allocation_id": o["allocation_ids"][0],
        "k8s_version": "v1.33.2"})
    assert r.status_code == 201, r.text
    o2 = r.json()
    assert o2["state"] == "delivered" and o2["k8s_cluster_id"]
    states = [h["state"] for h in o2["history"]]
    assert states.count("delivered") == 2        # 개통 + 애드온 완료
    assert "k8s_installing" in states

    c = client.get(f"/api/v1/k8s/clusters/{o2['k8s_cluster_id']}").json()
    assert c["state"] == "running" and c["version"] == "v1.33.2"
    assert o2["access_package"]["managed_k8s"]["cluster_id"] == c["id"]
    cpus = client.get(f"/api/v1/cpu-nodes?tenant_id={tid}").json()
    assert Counter(cn["role"] for cn in cpus) == {"general": 5, "k8s_cp": 3}
    # 중복 설치 거절
    r = client.post("/api/v1/k8s/installs", json={
        "tenant_id": tid, "allocation_id": o["allocation_ids"][0]})
    assert r.status_code == 409
    # 회수 시 클러스터·CP 동반 해체
    r = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate",
        "allocation_id": o["allocation_ids"][0]})
    assert r.json()["state"] == "closed"
    assert client.get(f"/api/v1/k8s/clusters/{c['id']}").json()["state"] \
        == "deleted"


def test_k8s_spec_endpoint(client):
    spec = client.get("/api/v1/k8s/spec").json()
    assert spec["cp_nodes_per_cluster"] == 3
    assert "v1.32.4" in spec["supported_versions"]
    assert any(a["name"] == "dcgm-exporter" for a in spec["managed_addons"])
