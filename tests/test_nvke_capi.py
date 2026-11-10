"""F11 NVKE depth — CAPI/CAPNICo objects, IMEX ComputeDomain, Topograph labels,
node replacement within the failure domain, in-place upgrade, CRD contract."""
from pathlib import Path

import pytest
import yaml

from app import nico_fake
from app.store import STORE

CLIQUE = "nvidia.com/gpu.clique"
ZONE = "topology.kubernetes.io/zone"


def _tenant(client, name="capi-co"):
    return client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"}).json()["id"]


def _two_rack_cluster(client, k8s="1.33"):
    tid = _tenant(client)
    r = client.post("/api/v1/nvke/clusters", json={
        "name": "train", "tenant_id": tid, "k8s_version": k8s,
        "control_plane": {"etcd_encryption": True},
        "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 36}]})
    assert r.status_code == 201, r.text
    return tid, r.json()


def test_two_rack_pool_gives_two_cliques_and_two_compute_domains(client):
    tid, c = _two_rack_cluster(client)
    cid = c["id"]
    nodes = client.get(f"/api/v1/nvke/clusters/{cid}/nodes").json()
    assert len(nodes) == 36
    gpus = sum(len(STORE.trays[nico_fake.FAKE_NICO.get_host(n["nico_machine_id"]).tray_id]
                   .gpu_ids) for n in nodes)
    assert gpus == 144
    cliques = {n["labels"][CLIQUE] for n in nodes}
    racks = {n["labels"][ZONE] for n in nodes}
    assert len(cliques) == 2 and len(racks) == 2
    for n in nodes:
        dom, cl = n["labels"][CLIQUE].rsplit(".", 1)
        assert len(dom) == 36 and cl.isdigit()
        assert n["labels"]["network.topology.nvidia.com/ib-spine"]
        assert n["attestation"]["tpm"] == "verified"
        assert n["attestation"]["spiffe_id"].startswith("spiffe://")
        assert n["join"]["agent"] == "nvke-agent" and n["join"]["state"] == "Joined"
        assert n["machine_phase"] == "Running"
        assert n["provider_id"] == f"nico://{n['nico_instance_id']}"

    cds = client.get(f"/api/v1/nvke/clusters/{cid}/compute-domains").json()
    assert len(cds) == 2
    assert all(cd["status"] == "Ready" and cd["num_nodes"] == 18 for cd in cds)
    assert {cd["cliques"][0] for cd in cds} == cliques

    capi = client.get(f"/api/v1/nvke/clusters/{cid}/capi").json()
    assert capi["cluster"]["phase"] == "Provisioned"
    assert sorted(capi["cluster"]["failure_domains"]) == sorted(racks)
    mds = capi["machine_deployments"]
    assert len(mds) == 2 and {m["failure_domain"] for m in mds} == racks
    assert all(m["replicas"] == 18 for m in mds)
    assert len(capi["nico_machines"]) == 36
    assert all(nm["phase"] == "Ready" for nm in capi["nico_machines"])
    last = capi["machines"][-1]["name"]
    phases = [h["phase"] for h in capi["history"] if h["name"] == last]
    assert phases == ["Pending", "Provisioning", "Provisioned", "Running"]
    assert c["control_plane"]["etcd_encryption"] is True


def test_etcd_encryption_opt_out(client):
    tid = _tenant(client)
    c = client.post("/api/v1/nvke/clusters", json={
        "name": "x", "tenant_id": tid, "control_plane": {"etcd_encryption": False}}).json()
    assert c["control_plane"]["etcd_encryption"] is False


def test_replacement_keeps_failure_domain(client):
    _, c = _two_rack_cluster(client)
    cid = c["id"]
    # shrink to leave spare trays in both racks, then replace one node
    pid = c["nodepools"][0]["id"]
    client.patch(f"/api/v1/nvke/clusters/{cid}/nodepools/{pid}", json={"count": 20})
    nodes = client.get(f"/api/v1/nvke/clusters/{cid}/nodes").json()
    target = next(n for n in nodes if n["labels"][ZONE] == nodes[-1]["labels"][ZONE])
    rack = target["labels"][ZONE]
    r = client.post(f"/api/v1/nvke/clusters/{cid}/nodes/{target['name']}/replace",
                    json={"reason": "XID 79"})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["failure_domain"] == rack and rec["failure_domain_kept"] is True
    after = {n["name"]: n for n in client.get(f"/api/v1/nvke/clusters/{cid}/nodes").json()}
    assert after[target["name"]]["labels"][ZONE] == rack
    assert after[target["name"]]["nico_machine_id"] == rec["new_host"]
    hist = client.get(f"/api/v1/nvke/clusters/{cid}/capi").json()["history"]
    assert any(h["phase"] == "Deleted" and h["kind"] == "Machine" for h in hist)


def test_in_place_upgrade_rolls_machines(client):
    tid = _tenant(client)
    c = client.post("/api/v1/nvke/clusters", json={
        "name": "u", "tenant_id": tid, "k8s_version": "1.33",
        "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 4}]}).json()
    cid = c["id"]
    hosts = [n["nico_host_id"] for n in c["nodepools"][0]["nodes"]]
    r = client.post(f"/api/v1/nvke/clusters/{cid}/upgrade",
                    json={"k8s_version": "1.34", "max_unavailable": 2})
    assert r.status_code == 200, r.text
    up = r.json()["upgrade"]
    pool_step = up["steps"][1]
    assert pool_step["batches"] == 2 and pool_step["to"] == "v1.34.1"
    assert "KubeadmUpgradeApply" in up["steps"][0]["detail"]
    capi = client.get(f"/api/v1/nvke/clusters/{cid}/capi").json()
    assert all(m["version"] == "v1.34.1" and m["phase"] == "Running" for m in capi["machines"])
    assert all(md["up_to_date_replicas"] == md["replicas"] for md in capi["machine_deployments"])
    assert any(h["phase"] == "Updating" for h in capi["history"])
    nodes = client.get(f"/api/v1/nvke/clusters/{cid}/nodes").json()
    assert sorted(n["nico_machine_id"] for n in nodes) == sorted(hosts)   # in place


def test_custom_compute_domain_and_scale_down(client):
    _, c = _two_rack_cluster(client)
    cid, pid = c["id"], c["nodepools"][0]["id"]
    r = client.post(f"/api/v1/nvke/clusters/{cid}/compute-domains",
                    json={"name": "train-all", "nodepool": "gpu", "allocation_mode": "All"})
    assert r.status_code == 201, r.text
    cd = r.json()
    assert len(cd["cliques"]) == 2 and cd["num_nodes"] == 36
    assert cd["imex_channels"][0]["count"] == 2048
    assert len(cd["computeDomainCliques"]) == 2
    assert client.post(f"/api/v1/nvke/clusters/{cid}/compute-domains",
                       json={"name": "bad", "nodepool": "gpu",
                             "racks": ["rack-x"]}).status_code == 422
    # scale to one rack → the second auto ComputeDomain and MachineDeployment go away
    client.patch(f"/api/v1/nvke/clusters/{cid}/nodepools/{pid}", json={"count": 18})
    auto = [x for x in client.get(f"/api/v1/nvke/clusters/{cid}/compute-domains").json()
            if x["auto"]]
    assert len(auto) == 1
    assert len(client.get(f"/api/v1/nvke/clusters/{cid}/capi").json()
               ["machine_deployments"]) == 1
    assert client.delete(f"/api/v1/nvke/clusters/{cid}/compute-domains/train-all").status_code == 200


def test_topograph_generate_and_result(client):
    _, c = _two_rack_cluster(client)
    body = {"provider": {"name": "nvke", "params": {"cluster_id": c["id"]}},
            "engine": {"name": "k8s", "params": {
                "fabricLabels": ["network.topology.nvidia.com/ib-leaf",
                                 "network.topology.nvidia.com/ib-spine"]}}}
    r = client.post("/api/v1/topograph/v1/generate", json=body)
    assert r.status_code == 202 and len(r.text) == 32
    res = client.get(f"/api/v1/topograph/v1/topology?uid={r.text}").json()
    assert len(res["acceleratorDomains"]) == 2 and res["status"] == "OK"
    lab = next(iter(res["nodeLabels"].values()))
    assert "accelerator.topograph.run/domain" in lab
    assert "network.topology.nvidia.com/ib-spine" in lab
    assert client.post("/api/v1/topograph/v1/lookup", json=body).json() == res
    assert client.get("/api/v1/topograph/v1/topology?uid=nope").status_code == 404


def test_delete_cluster_drops_capi(client):
    _, c = _two_rack_cluster(client)
    cid = c["id"]
    client.delete(f"/api/v1/nvke/clusters/{cid}")
    assert client.get(f"/api/v1/nvke/clusters/{cid}/capi").status_code == 404


def test_tenant_scope_on_capi_routes(client):
    _, c = _two_rack_cluster(client)
    other = _tenant(client, "other-co")
    h = {"X-Tenant-Id": other}
    for p in ("capi", "nodes", "compute-domains"):
        assert client.get(f"/api/v1/nvke/clusters/{c['id']}/{p}", headers=h).status_code == 403


# ---------------------------------------------------------------------------
# Contract: objects validate against the upstream CRD openAPIV3Schema
# ---------------------------------------------------------------------------
def _ref_root():
    for p in Path(__file__).resolve().parents:
        cand = p / "ref" / "upstream"
        if cand.is_dir():
            return cand
    return None


CRDS = {
    ("cluster.x-k8s.io/v1beta2", "Cluster"): "capi/core/config/crd/bases/cluster.x-k8s.io_clusters.yaml",
    ("cluster.x-k8s.io/v1beta2", "Machine"): "capi/core/config/crd/bases/cluster.x-k8s.io_machines.yaml",
    ("cluster.x-k8s.io/v1beta2", "MachineDeployment"):
        "capi/core/config/crd/bases/cluster.x-k8s.io_machinedeployments.yaml",
    ("cluster.x-k8s.io/v1beta2", "MachineSet"):
        "capi/core/config/crd/bases/cluster.x-k8s.io_machinesets.yaml",
    ("infrastructure.cluster.x-k8s.io/v1alpha1", "NicoMachine"):
        "capnico/config/crd/bases/infrastructure.cluster.x-k8s.io_nicomachines.yaml",
    ("infrastructure.cluster.x-k8s.io/v1alpha1", "NicoCluster"):
        "capnico/config/crd/bases/infrastructure.cluster.x-k8s.io_nicoclusters.yaml",
    ("resource.nvidia.com/v1beta1", "ComputeDomain"):
        "dra-driver/deployments/helm/dra-driver-nvidia-gpu/crds/resource.nvidia.com_computedomains.yaml",
    ("resource.nvidia.com/v1beta1", "ComputeDomainClique"):
        "dra-driver/deployments/helm/dra-driver-nvidia-gpu/crds/resource.nvidia.com_computedomaincliques.yaml",
}
TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}


def _validate(v, s, path, errs):
    if s.get("x-kubernetes-int-or-string") and isinstance(v, (int, str)):
        return
    t = s.get("type")
    if t and not isinstance(v, TYPES[t]) or (t == "integer" and isinstance(v, bool)):
        errs.append(f"{path}: {type(v).__name__} != {t}")
        return
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{path}: {v!r} not in {s['enum']}")
    if t == "object" and isinstance(v, dict):
        props = s.get("properties")
        for r in s.get("required", []):
            if r not in v:
                errs.append(f"{path}.{r}: required")
        if props is None:
            return
        for k, x in v.items():
            if path == "" and k == "x-neocloud":
                continue                       # NeoCloud extension, marked
            if k not in props:
                if not (s.get("additionalProperties")
                        or s.get("x-kubernetes-preserve-unknown-fields")):
                    errs.append(f"{path}.{k}: unknown field")
                continue
            if path == "" and k == "metadata":
                continue
            _validate(x, props[k], f"{path}.{k}", errs)
    if t == "array" and isinstance(v, list) and "items" in s:
        for i, x in enumerate(v):
            _validate(x, s["items"], f"{path}[{i}]", errs)


def test_objects_match_upstream_crds(client):
    root = _ref_root()
    if root is None:
        pytest.skip("ref/upstream not found")
    _, c = _two_rack_cluster(client)
    cid = c["id"]
    client.post(f"/api/v1/nvke/clusters/{cid}/compute-domains",
                json={"name": "all", "nodepool": "gpu"})
    objs = client.get(f"/api/v1/nvke/clusters/{cid}/capi?objects=true").json()["objects"]
    for cd in client.get(f"/api/v1/nvke/clusters/{cid}/compute-domains").json():
        objs += [cd["computeDomain"], *cd["computeDomainCliques"]]
    seen = set()
    for o in objs:
        key = (o["apiVersion"], o["kind"])
        doc = yaml.safe_load((root / CRDS[key]).read_text())
        ver = next(x for x in doc["spec"]["versions"] if x["name"] == o["apiVersion"].split("/")[1])
        errs: list = []
        _validate(o, ver["schema"]["openAPIV3Schema"], "", errs)
        assert not errs, (key, o["metadata"]["name"], errs[:5])
        seen.add(key)
    assert seen == set(CRDS)


def test_demo_scenario_passes(client):
    import importlib.util
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "f11_nvke", root / "app" / "demo" / "scenarios" / "f11_nvke.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class Res:
        def __init__(self, r):
            self.status = r.status_code
            try:
                self.body = r.json()
            except ValueError:
                self.body = r.text

    ctx = {}
    for st in mod.SCENARIO["steps"]:
        call = st["call"]
        v = lambda x: x(ctx) if callable(x) else x  # noqa: E731
        res = Res(client.request(call["method"], v(call["path"]), json=v(call.get("json")),
                                 params=v(call.get("params"))))
        ok, ko, en = st["expect"](res, ctx)
        assert ok, (st["id"], res.status, en)
        if st.get("save"):
            st["save"](res, ctx)
