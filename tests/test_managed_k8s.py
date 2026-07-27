"""Managed K8s option — regression tests covering the fulfillment pipeline,
CPU CP nodes, Converged attach, DCGM in-band switchover, and reclaim.

Design rationale:
- The 3 CP CPU nodes are real hosts in the AI Infra Emulator (FakeNico)
  inventory; they are allocated to the tenant after IP/OS install via the
  same DPU-based Day1 path (reserve->provision->allocate) as the GPU trays.
- CP nodes are attached to the tenant VPC segment via Converged Network (VNI).
- When Managed K8s is provided, DCGM collection switches to in-band
  (dcgm-exporter).
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

    # ── State machine: acceptance → k8s_installing → delivered ───────────
    states = [h["state"] for h in o["history"]]
    assert "k8s_installing" in states
    assert states.index("acceptance") < states.index("k8s_installing") \
        < states.index("delivered")

    # ── K8s cluster read-model ───────────────────────────────────────────
    c = client.get(f"/api/v1/k8s/clusters/{o['k8s_cluster_id']}").json()
    assert c["state"] == "running" and c["version"] == "v1.32.4"
    assert len(c["cp_node_ids"]) == 3            # 3 CP nodes (HA / etcd quorum)
    assert c["dcgm_mode"] == "in-band"
    assert c["gpus_total"] == 72                 # VR NVL72 1 rack
    addons = {a["name"] for a in c["addons"]}
    assert {"cilium", "gpu-operator", "network-operator",
            "dcgm-exporter", "kube-vip"} <= addons
    checks = {x["check"]: x["result"] for x in c["conditions"]}
    assert set(checks) >= {"nodes-ready", "gpu-allocatable", "nccl-smoke",
                           "dcgm-diag", "api-vip-ha"}
    assert all(v == "PASS" for v in checks.values())

    # ── CPU nodes: 5 default (general) + 3 K8s CP (k8s_cp) ───────────────
    cpus = client.get(f"/api/v1/cpu-nodes?tenant_id={tid}").json()
    roles = Counter(cn["role"] for cn in cpus)
    assert roles == {"general": 5, "k8s_cp": 3}

    # ── CP nodes must be actually allocated via NICo (DPU isolation) ─────
    cp = [cn for cn in cpus if cn["role"] == "k8s_cp"]
    for cn in cp:
        h = client.get(f"/fake-nico/hosts/{cn['nico_host_id']}").json()
        assert h["state"] == "allocated" and h["tenant_ref"] == tid
        assert h["host_ip"].startswith("10.250.1.")   # DPU-DHCP CPU range
        assert h["instance_id"]                        # AllocateInstance done
        assert cn["order_id"] == o["id"]

    # ── Converged Network: CP nodes attach to the tenant segment ────────
    seg = next(s for s in client.get("/fake-nico/segments").json()
               if s["tenant_ref"] == tid)
    for cn in cp:
        assert cn["nico_host_id"] in seg["host_ids"]

    # ── DCGM in-band switchover (emulation metric) ───────────────────────
    f = client.get(f"/api/v1/emu/faults?tenant_id={tid}").json()
    assert f["dcgm_source"].startswith("in-band")

    # ── Access package: kubeconfig (OIDC) section ────────────────────────
    mk = o["access_package"]["managed_k8s"]
    assert mk["cluster_id"] == c["id"]
    assert mk["api_server"] == f"https://{c['api_vip']}:6443"
    assert "OIDC" in mk["kubeconfig"]

    # ── /flow: K8s channel events in the k8s_installing bucket ──────────
    flow = client.get(f"/api/v1/orders/{o['id']}/flow").json()
    stage = next(st for st in flow["stages"]
                 if st["state"] == "k8s_installing")
    assert stage["by_channel"].get("K8s", 0) >= 10   # bootstrap+addons+verify
    assert stage["by_channel"].get("rshim", 0) >= 3  # 3 CP BFB (DPU path)
    ops = " ".join(a["op"] for a in stage["apis"])
    assert "kubeadm init" in ops and "DCGM in-band" in ops

    # ── Reclaim: cluster teardown + CP nodes returned to NICo ────────────
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
    assert cpus_after == []                       # final reclaim — all returned
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
    """Approval mode — K8s-option orders have 8 gates (incl. k8s_installing)."""
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
    """Non-option orders keep the original 7 stages — no k8s_installing."""
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
    """CP node provisioning failure — saga compensation reverts GPU/CPU nodes."""
    tid = _mk_tenant(client)
    # First pool_ready CPU node is CP candidate #1 — inject provision failure
    first_free = sorted(
        h.host_id for h in FAKE_NICO.hosts.values()
        if h.sku == "cpu-epyc")[5]     # first 5 are taken by the default set
    FAKE_NICO.inject_failure(first_free, "provision")
    o = _k8s_order(client, tid).json()
    assert o["state"] == "failed"
    assert "k8s cp node setup failed" in o["error"]
    # Verify all CPU nodes returned (no k8s_cp left)
    cpus = client.get("/api/v1/cpu-nodes").json()
    assert all(cn["role"] != "k8s_cp" for cn in cpus)
    # GPU nodes also returned to the pool by compensation
    nodes = client.get(f"/api/v1/nodes?tenant_id={tid}").json()
    assert nodes == []


def test_managed_k8s_day2_addon_on_existing_bmaas(client):
    """Add K8s to a tenant already provisioned via BMaaS (Day-2 addon)."""
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
    assert states.count("delivered") == 2        # provisioning + addon done
    assert "k8s_installing" in states

    c = client.get(f"/api/v1/k8s/clusters/{o2['k8s_cluster_id']}").json()
    assert c["state"] == "running" and c["version"] == "v1.33.2"
    assert o2["access_package"]["managed_k8s"]["cluster_id"] == c["id"]
    cpus = client.get(f"/api/v1/cpu-nodes?tenant_id={tid}").json()
    assert Counter(cn["role"] for cn in cpus) == {"general": 5, "k8s_cp": 3}
    # Reject duplicate install
    r = client.post("/api/v1/k8s/installs", json={
        "tenant_id": tid, "allocation_id": o["allocation_ids"][0]})
    assert r.status_code == 409
    # On reclaim, cluster and CP are torn down together
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
