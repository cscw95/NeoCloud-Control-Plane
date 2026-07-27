"""Managed K8s Live-mode extended-API regression tests (R3, R4, R6).

- R3: overview / installs / nodes / acceptance / nodepools / addons /
      services / kubeconfig / upgrades / CVE / health-events / storage /
      metrics surfaces
- R4: install saga 8-stage record (K8sInstall, K8sCluster.stage_history) —
      in pacing mode (NOCP_K8S_STAGE_DELAY), reaching Active is verified by polling
- R6: GPU fault injection (POST /emu/faults) -> worker Quarantined transition +
      health-event + hot-spare replacement proposal

The AI Infra (:9100)-dependent path (_ai_infra_faults) is bypassed with an
empty list when NOCP_NICO_URL is unset (same pattern as the existing
tray_emu._emulator_reprov_faults), so the entire path is verified with the
in-process FakeNico alone.
"""
import time
from collections import Counter

from app.nico_fake import FAKE_NICO


def _mk_tenant(client, name="live-corp"):
    r = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _k8s_order(client, tenant_id, version="v1.32.4", **kw):
    return client.post("/api/v1/orders", json={
        "tenant_id": tenant_id, "kind": "new", "blueprint_key": "vr-nvl72",
        "racks": 1, "managed_k8s": True, "k8s_version": version, **kw})


def _bmaas_order(client, tenant_id):
    return client.post("/api/v1/orders", json={
        "tenant_id": tenant_id, "kind": "new", "blueprint_key": "vr-nvl72",
        "racks": 1}).json()


def _wait(fn, timeout=12.0, interval=0.05):
    """Polling helper — wait until fn() returns truthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError("timeout waiting for condition")


# ---------------------------------------------------------------------------
# R4 — Install record (common to Day-1/Day-2) + overview
# ---------------------------------------------------------------------------
def test_install_record_day1_and_overview(client):
    tid = _mk_tenant(client)
    o = _k8s_order(client, tid).json()
    assert o["state"] == "delivered", o.get("error")

    installs = client.get("/api/v1/k8s/installs").json()
    assert len(installs) == 1
    inst = installs[0]
    assert inst["order_id"] == o["id"]
    assert inst["cluster_id"] == o["k8s_cluster_id"]
    assert inst["state"] == "succeeded"
    names = [s["name"] for s in inst["stages"]]
    assert names == ["cp-reserve", "os-provision", "net-attach",
                     "nkd-bootstrap", "addons", "acceptance",
                     "telemetry", "active"]
    assert all(s["status"] == "done" and s["ts"] for s in inst["stages"])

    # Cluster derived state — stepper / progress
    c = client.get(f"/api/v1/k8s/clusters/{o['k8s_cluster_id']}").json()
    assert c["stage"] == "active" and c["progress_pct"] == 100
    assert [h["status"] for h in c["stage_history"]].count("done") == 8

    ov = client.get("/api/v1/k8s/overview").json()
    assert ov["clusters_total"] == 1
    assert ov["by_state"]["active"] == 1 and ov["by_state"]["installing"] == 0
    assert ov["installs_active"] == 0 and ov["upgrades_active"] == 0
    assert ov["versions"] == {"supported": ["v1.32.4", "v1.33.2"],
                              "nkd": "25.06"}
    assert {"tenant_id": tid, "clusters": 1} in ov["tenants"]


def test_install_record_day2_and_failure(client):
    tid = _mk_tenant(client)
    o = _bmaas_order(client, tid)
    assert o["state"] == "delivered"
    r = client.post("/api/v1/k8s/installs", json={
        "tenant_id": tid, "allocation_id": o["allocation_ids"][0],
        "k8s_version": "v1.33.2"})
    assert r.status_code == 201
    installs = client.get(f"/api/v1/k8s/installs?tenant_id={tid}").json()
    assert len(installs) == 1 and installs[0]["state"] == "succeeded"
    assert installs[0]["k8s_version"] == "v1.33.2"

    # Failure record — inject CP provision failure (Day-1; the new tenant is
    # a gb200 SU — with dedicated isolation, the vr SU held by t1 can't be placed)
    tid2 = _mk_tenant(client, "fail-corp")
    pool = sorted(cn["id"] for cn in client.get("/api/v1/cpu-nodes").json()
                  if cn["state"] == "pool_ready")
    FAKE_NICO.inject_failure(f"nh-{pool[5]}", "provision")  # after default 5 = CP#1
    o2 = client.post("/api/v1/orders", json={
        "tenant_id": tid2, "kind": "new", "blueprint_key": "gb200-nvl72",
        "racks": 1, "managed_k8s": True, "k8s_version": "v1.32.4"}).json()
    assert o2["state"] == "failed"
    failed = [i for i in client.get("/api/v1/k8s/installs").json()
              if i["tenant_id"] == tid2]
    assert len(failed) == 1 and failed[0]["state"] == "failed"
    st = {s["name"]: s["status"] for s in failed[0]["stages"]}
    assert st["os-provision"] == "failed"
    # Failed cluster goes to the failed bucket in overview
    ov = client.get("/api/v1/k8s/overview").json()
    assert ov["by_state"]["failed"] == 1


# ---------------------------------------------------------------------------
# R4 — Pacing mode: observe stage progression / reaching Active via polling
# ---------------------------------------------------------------------------
def test_paced_day2_install_polls_to_active(client, monkeypatch):
    monkeypatch.setenv("NOCP_K8S_STAGE_DELAY", "0.05")
    tid = _mk_tenant(client, "paced-d2")
    o = _bmaas_order(client, tid)
    r = client.post("/api/v1/k8s/installs", json={
        "tenant_id": tid, "allocation_id": o["allocation_ids"][0],
        "k8s_version": "v1.32.4"})
    assert r.status_code == 201
    assert r.json()["state"] == "k8s_installing"     # async — returns immediately

    inst = _wait(lambda: next(
        (i for i in client.get("/api/v1/k8s/installs").json()
         if i["tenant_id"] == tid and i["state"] == "succeeded"), None))
    assert inst["cluster_id"]
    c = client.get(f"/api/v1/k8s/clusters/{inst['cluster_id']}").json()
    assert c["state"] == "running" and c["progress_pct"] == 100
    o2 = _wait(lambda: (lambda x: x if x["state"] == "delivered" else None)(
        client.get(f"/api/v1/orders/{o['id']}").json()))
    assert o2["k8s_cluster_id"] == inst["cluster_id"]
    assert o2["access_package"]["managed_k8s"]["cluster_id"] == c["id"]


def test_paced_day1_order_polls_to_delivered(client, monkeypatch):
    monkeypatch.setenv("NOCP_K8S_STAGE_DELAY", "0.05")
    tid = _mk_tenant(client, "paced-d1")
    r = _k8s_order(client, tid)
    assert r.status_code == 201
    o = r.json()
    assert o["state"] == "k8s_installing"            # bg saga in progress
    done = _wait(lambda: (lambda x: x if x["state"] == "delivered" else None)(
        client.get(f"/api/v1/orders/{o['id']}").json()))
    assert done["k8s_cluster_id"]
    c = client.get(f"/api/v1/k8s/clusters/{done['k8s_cluster_id']}").json()
    assert c["state"] == "running"
    checks = {x["check"] for x in c["conditions"]}
    assert {"nodes-ready", "nccl-smoke", "dcgm-diag"} <= checks


# ---------------------------------------------------------------------------
# R3 — Cluster detail surfaces
# ---------------------------------------------------------------------------
def test_nodes_acceptance_nodepools_services(client):
    tid = _mk_tenant(client)
    o = _k8s_order(client, tid).json()
    cid = o["k8s_cluster_id"]
    c = client.get(f"/api/v1/k8s/clusters/{cid}").json()

    nodes = client.get(f"/api/v1/k8s/clusters/{cid}/nodes").json()
    roles = Counter(n["role"] for n in nodes)
    assert roles == {"cp": 3, "gpu-worker": len(c["worker_node_ids"])}
    assert all(n["state"] == "Ready" for n in nodes)
    cp = [n for n in nodes if n["role"] == "cp"]
    assert all(n["gpu_count"] == 0 and n["ip"].startswith("10.250.1.")
               for n in cp)
    gw = [n for n in nodes if n["role"] == "gpu-worker"]
    assert all(n["gpu_count"] == 4 and n["version"] == "v1.32.4"
               for n in gw)

    acc = client.get(f"/api/v1/k8s/clusters/{cid}/acceptance").json()
    assert acc["status"] == "pass" and acc["report_ts"]
    assert {ck["name"] for ck in acc["checks"]} == {
        "node-ready", "nccl-allreduce", "dcgm-diag", "storage-mount"}
    assert all(ck["status"] == "pass" for ck in acc["checks"])

    pools = client.get(f"/api/v1/k8s/clusters/{cid}/nodepools").json()
    by_role = {p["role"]: p for p in pools}
    assert by_role["cp"]["count"] == 3
    assert by_role["cp"]["image"] == "ubuntu-24.04-k8s-cp"
    assert by_role["gpu-worker"]["count"] == len(c["worker_node_ids"])
    assert by_role["gpu-worker"]["image"].startswith("dgx-os")

    svc = client.get(f"/api/v1/k8s/clusters/{cid}/services").json()
    assert svc["api_vip"] == c["api_vip"]
    types = {e["type"] for e in svc["entries"]}
    assert types == {"LB", "DNS", "Ingress"}
    lb = next(e for e in svc["entries"] if e["type"] == "LB")
    assert lb["vip_or_host"] == c["api_vip"] and lb["ports"] == [6443]
    assert all(e["state"] == "active" for e in svc["entries"])


def test_storage_and_metrics(client):
    tid = _mk_tenant(client)
    o = _k8s_order(client, tid).json()
    cid = o["k8s_cluster_id"]

    st = client.get(f"/api/v1/k8s/clusters/{cid}/storage").json()
    assert len(st) == 1
    pvc = st[0]
    assert pvc["gds"] is True and pvc["mode"] == "RWX"
    assert pvc["capacity_tb"] == 500          # 1 rack × 500TB (auto-computed)
    assert 0 < pvc["used_tb"] < pvc["capacity_tb"]
    assert pvc["storage_class"] == "vast-nfs-rdma-gds"

    client.post("/api/v1/emu/tick?n=3")       # generate time series
    m = client.get(f"/api/v1/k8s/clusters/{cid}/metrics").json()
    assert m["gpus_total"] == 72 and m["dcgm_mode"] == "in-band"
    assert {"gpu_util_pct", "gpu_temp_max_c", "ecc_correctable",
            "ib_bw_tbs", "ticks"} <= set(m)
    assert len(m["ticks"]) >= 1
    assert {"ts", "util", "temp"} <= set(m["ticks"][0])


def test_addons_get_and_optional_post(client):
    tid = _mk_tenant(client)
    o = _k8s_order(client, tid).json()
    cid = o["k8s_cluster_id"]

    addons = client.get(f"/api/v1/k8s/clusters/{cid}/addons").json()
    assert {a["name"] for a in addons} >= {"cilium", "gpu-operator",
                                           "network-operator", "kube-vip"}
    assert all(a["status"] == "installed" and a["channel"] == "nkd-bundle"
               for a in addons)

    r = client.post(f"/api/v1/k8s/clusters/{cid}/addons",
                    json={"name": "kueue"})
    assert r.status_code == 201
    added = next(a for a in r.json() if a["name"] == "kueue")
    assert added["status"] == "installed" and added["channel"] == "optional"
    # Duplicate / unsupported addon
    assert client.post(f"/api/v1/k8s/clusters/{cid}/addons",
                       json={"name": "kueue"}).status_code == 409
    assert client.post(f"/api/v1/k8s/clusters/{cid}/addons",
                       json={"name": "no-such"}).status_code == 404
    # spec catalog exposure
    spec = client.get("/api/v1/k8s/spec").json()
    assert any(a["name"] == "kueue" for a in spec["optional_addons"])
    assert [s["name"] for s in spec["install_stages"]][0] == "cp-reserve"


# ---------------------------------------------------------------------------
# R3 — kubeconfig issue/revoke + RBAC templates
# ---------------------------------------------------------------------------
def test_kubeconfig_issue_revoke_and_rbac_templates(client):
    tid = _mk_tenant(client)
    o = _k8s_order(client, tid).json()
    cid = o["k8s_cluster_id"]

    r = client.post(f"/api/v1/k8s/clusters/{cid}/kubeconfigs",
                    json={"role": "admin", "ttl_h": 24})
    assert r.status_code == 201
    kc = r.json()
    assert kc["role"] == "admin" and kc["ttl_h"] == 24
    assert len(kc["serial"]) == 16 and not kc["revoked"]
    assert kc["expires_at"] > kc["issued_at"]
    c = client.get(f"/api/v1/k8s/clusters/{cid}").json()
    assert c["api_vip"] in kc["kubeconfig_yaml"]
    assert "oidc-login" in kc["kubeconfig_yaml"]

    lst = client.get(f"/api/v1/k8s/clusters/{cid}/kubeconfigs").json()
    assert [k["kubeconfig_id"] for k in lst] == [kc["kubeconfig_id"]]

    r = client.delete(f"/api/v1/k8s/clusters/{cid}/kubeconfigs/"
                      f"{kc['kubeconfig_id']}")
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert client.delete(f"/api/v1/k8s/clusters/{cid}/kubeconfigs/"
                         f"{kc['kubeconfig_id']}").status_code == 409
    # Unsupported role
    assert client.post(f"/api/v1/k8s/clusters/{cid}/kubeconfigs",
                       json={"role": "root"}).status_code == 422

    tpl = client.get("/api/v1/k8s/rbac-templates").json()
    assert {t["role"] for t in tpl} >= {"admin", "edit", "view"}


# ---------------------------------------------------------------------------
# R3/R6 — Upgrade saga (v1.32.4 → v1.33.2)
# ---------------------------------------------------------------------------
def test_upgrade_saga_and_validation(client):
    tid = _mk_tenant(client)
    o = _k8s_order(client, tid, version="v1.32.4").json()
    cid = o["k8s_cluster_id"]

    # Validation — unsupported / same version
    assert client.post(f"/api/v1/k8s/clusters/{cid}/upgrades",
                       json={"target_version": "v1.31.0"}).status_code == 422
    assert client.post(f"/api/v1/k8s/clusters/{cid}/upgrades",
                       json={"target_version": "v1.32.4"}).status_code == 409

    r = client.post(f"/api/v1/k8s/clusters/{cid}/upgrades",
                    json={"target_version": "v1.33.2"})
    assert r.status_code == 201
    up = r.json()
    assert up["from_version"] == "v1.32.4"
    assert up["state"] in ("running", "succeeded")

    done = _wait(lambda: next(
        (u for u in client.get(
            f"/api/v1/k8s/clusters/{cid}/upgrades").json()
         if u["upgrade_id"] == up["upgrade_id"]
         and u["state"] == "succeeded"), None))
    total = done["node_progress"]["total"]
    assert done["node_progress"]["done"] == total == 3 + 18   # CP + workers
    steps = Counter(e["step"] for e in done["events"])
    assert steps == {"cordon": total, "drain": total,
                     "upgrade": total, "uncordon": total}
    first_node_steps = [e["step"] for e in done["events"]
                        if e["node"] == done["events"][0]["node"]]
    assert first_node_steps == ["cordon", "drain", "upgrade", "uncordon"]

    c = client.get(f"/api/v1/k8s/clusters/{cid}").json()
    assert c["version"] == "v1.33.2"
    nodes = client.get(f"/api/v1/k8s/clusters/{cid}/nodes").json()
    assert all(n["version"] == "v1.33.2" for n in nodes)
    # Re-request after completion — downgrade not allowed
    assert client.post(f"/api/v1/k8s/clusters/{cid}/upgrades",
                       json={"target_version": "v1.32.4"}).status_code == 409


def test_cves_static_curation(client):
    cves = client.get("/api/v1/k8s/cves").json()
    assert 3 <= len(cves) <= 5
    ctd = next(c for c in cves if c["cve_id"] == "CVE-2026-32871")
    assert ctd["component"] == "containerd"
    assert "v1.33.2" in ctd["patched_in"]
    assert all({"cve_id", "component", "severity", "affected_versions",
                "patched_in"} <= set(c) for c in cves)


# ---------------------------------------------------------------------------
# R6 — Fault injection → worker Quarantined + health-event + hot-spare proposal
# ---------------------------------------------------------------------------
def test_fault_injection_quarantines_worker(client):
    tid = _mk_tenant(client)
    o = _k8s_order(client, tid).json()
    cid = o["k8s_cluster_id"]
    nodes = client.get(f"/api/v1/k8s/clusters/{cid}/nodes").json()
    victim = next(n for n in nodes if n["role"] == "gpu-worker")
    assert victim["state"] == "Ready"

    # Inject GPU fault via the existing XID episode path (DCGM→NVSentinel)
    r = client.post("/api/v1/emu/faults", json={
        "tray_id": victim["name"], "gpu": 1, "xid": 79, "ttl_ticks": 200})
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] == tid

    ev = client.get(f"/api/v1/k8s/clusters/{cid}/health-events").json()
    hit = next(e for e in ev if e["node"] == victim["name"]
               and e["kind"] == "gpu-xid" and e["action"] == "quarantined")
    assert hit["severity"] == "critical"
    assert hit["hot_spare"] and hit["hot_spare"]["node"]      # replacement proposal
    assert hit["hot_spare"]["node"] != victim["name"]

    nodes = client.get(f"/api/v1/k8s/clusters/{cid}/nodes").json()
    q = next(n for n in nodes if n["name"] == victim["name"])
    assert q["state"] == "Quarantined"
    c = client.get(f"/api/v1/k8s/clusters/{cid}").json()
    assert victim["name"] in c["quarantined_nodes"]
    ov = client.get("/api/v1/k8s/overview").json()
    assert ov["health_events_open"] >= 1

    # After recovery (force-expire TTL by advancing ticks), quarantine lifts
    client.post("/api/v1/emu/tick?n=200")
    nodes = client.get(f"/api/v1/k8s/clusters/{cid}/nodes").json()
    assert next(n for n in nodes
                if n["name"] == victim["name"])["state"] == "Ready"


# ---------------------------------------------------------------------------
# Constraint — new collections are included in the reset cascade
# ---------------------------------------------------------------------------
def test_reset_clears_new_collections(client):
    tid = _mk_tenant(client)
    o = _k8s_order(client, tid).json()
    cid = o["k8s_cluster_id"]
    client.post(f"/api/v1/k8s/clusters/{cid}/kubeconfigs",
                json={"role": "edit"})
    client.post(f"/api/v1/k8s/clusters/{cid}/upgrades",
                json={"target_version": "v1.33.2"})
    _wait(lambda: client.get(
        f"/api/v1/k8s/clusters/{cid}/upgrades").json()[0]["state"]
        == "succeeded")

    client.post("/api/v1/admin/reseed?blueprints=gb200-nvl72,vr-nvl72")
    assert client.get("/api/v1/k8s/installs").json() == []
    ov = client.get("/api/v1/k8s/overview").json()
    assert ov["clusters_total"] == 0 and ov["upgrades_active"] == 0
    assert client.get("/api/v1/k8s/clusters").json() == []
