"""F30 KAI scheduler emulation — queues/quotas, gang, clique-first placement,
reclaim/preempt, stale gang eviction, curtailment, upstream CRD contract."""
import importlib.util
from pathlib import Path

import pytest
import yaml

from app import kai_scheduler


def _tenant(client, name="sched-co"):
    return client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"}).json()["id"]


def _cluster(client, nodes=36, name="sched-co"):
    tid = _tenant(client, name)
    r = client.post("/api/v1/nvke/clusters", json={
        "name": "train", "tenant_id": tid,
        "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": nodes}]})
    assert r.status_code == 201, r.text
    return tid, r.json()["id"]


def _q(client, cid, name, quota, prio=100, **kw):
    r = client.post(f"/api/v1/nvke/clusters/{cid}/queues",
                    json={"name": name, "quota_gpus": quota, "priority": prio, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _w(client, cid, **body):
    r = client.post(f"/api/v1/nvke/clusters/{cid}/workloads", json={"kind": "PyTorchJob", **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_default_queues_and_queue_cr(client):
    _, cid = _cluster(client)
    qs = {q["name"]: q for q in client.get(f"/api/v1/nvke/clusters/{cid}/queues").json()}
    assert {"default-parent-queue", "default-queue"} <= set(qs)
    q = _q(client, cid, "research", 72, 100, limit_gpus=144, over_quota_weight=2)
    cr = q["resource"]
    assert cr["apiVersion"] == "scheduling.run.ai/v2" and cr["kind"] == "Queue"
    assert cr["spec"]["resources"]["gpu"] == {"quota": 72, "limit": 144, "overQuotaWeight": 2}
    assert cr["spec"]["parentQueue"] == "default-parent-queue"
    assert client.post(f"/api/v1/nvke/clusters/{cid}/queues",
                       json={"name": "research", "quota_gpus": 1}).status_code == 409
    assert client.post(f"/api/v1/nvke/clusters/{cid}/queues",
                       json={"name": "bad", "quota_gpus": 9, "limit_gpus": 4}).status_code == 422
    assert client.get("/api/v1/nvke/clusters/nope/queues").status_code == 404


def test_gang_fits_one_clique_first(client):
    _, cid = _cluster(client)
    _q(client, cid, "research", 144)
    a = _w(client, cid, name="a", gpus=64, queue="research")
    assert a["state"] == "Running" and a["pods"] == 16
    assert a["placement"]["single_clique"] and a["placement"]["clique_count"] == 1
    assert a["podgroup"]["spec"]["minMember"] == 16
    assert a["podgroup"]["spec"]["topologyConstraint"]["preferredTopologyLevel"] == "nvidia.com/gpu.clique"
    # second job: 8 GPUs left in a's clique, 72 in the other → best fit uses the leftover
    b = _w(client, cid, name="b", gpus=8, queue="research")
    assert b["placement"]["cliques"][0]["rack"] == a["placement"]["cliques"][0]["rack"]
    # 144 GPUs = 2 NVL72 racks → exactly two cliques once capacity allows
    for n in ("a", "b"):
        client.delete(f"/api/v1/nvke/clusters/{cid}/workloads/{n}")
    c = _w(client, cid, name="c", gpus=144, queue="research")
    assert c["state"] == "Running" and c["placement"]["clique_count"] == 2
    assert c["job"]["kind"] == "PyTorchJob"
    assert c["job"]["spec"]["pytorchReplicaSpecs"]["Worker"]["replicas"] == 35


def test_required_topology_and_gang_all_or_nothing(client):
    _, cid = _cluster(client)
    _q(client, cid, "research", 144)
    _w(client, cid, name="fill", gpus=40, queue="research")
    _w(client, cid, name="fill2", gpus=40, queue="research")      # best fit → other clique
    r = _w(client, cid, name="strict", gpus=60, queue="research", topology="required")
    assert r["state"] == "Pending" and r["reason"] == "Unschedulable"
    assert "single nvidia.com/gpu.clique" in r["message"]
    assert r["podgroup"]["status"]["schedulingConditions"][0]["reason"] == "Unschedulable"
    # preferred topology may span cliques: 60 GPUs over 32 + 32 free
    sp = _w(client, cid, name="span", gpus=60, queue="research")
    assert sp["state"] == "Running" and sp["placement"]["clique_count"] == 2
    client.delete(f"/api/v1/nvke/clusters/{cid}/workloads/span")
    # gang: a 100 GPU job cannot run partially on the 64 free GPUs
    g = _w(client, cid, name="gang", gpus=100, queue="research")
    assert g["state"] == "Pending" and g["placement"]["pods"] == []
    sched = client.get(f"/api/v1/nvke/clusters/{cid}/scheduler").json()
    assert sched["capacity"]["used_gpus"] == 80


def test_reclaim_over_quota_queue(client):
    _, cid = _cluster(client)
    _q(client, cid, "research", 72, 100)
    _q(client, cid, "prod", 72, 100)
    big = _w(client, cid, name="big", gpus=144, queue="research")
    assert big["state"] == "Running" and big["over_quota"]
    p = _w(client, cid, name="p", gpus=72, queue="prod", priority_class="build")
    assert p["state"] == "Running" and p["preempted"] == ["big"]
    big = client.get(f"/api/v1/nvke/clusters/{cid}/workloads/big").json()
    assert big["state"] == "Preempted" and big["reason"] == "Reclaimed"
    assert big["evictions"][0]["action"] == "reclaim"
    actions = [a["action"] for a in p["cycle"]["actions"]]
    assert "reclaim" in actions and actions[-1] == "close-session"
    # job done → the reclaimed gang comes back
    d = client.delete(f"/api/v1/nvke/clusters/{cid}/workloads/p").json()
    assert "big" in d["started"]


def test_preempt_within_queue_and_non_preemptible_quota(client):
    _, cid = _cluster(client)
    _q(client, cid, "team", 144)
    _w(client, cid, name="t1", gpus=144, queue="team", priority_class="train")
    hi = _w(client, cid, name="hi", gpus=72, queue="team", priority_class="inference")
    assert hi["state"] == "Running" and hi["preempted"] == ["t1"]
    # non-preemptible over quota is refused (KAI NonPreemptibleOverQuota)
    _q(client, cid, "small", 8)
    np = _w(client, cid, name="np", gpus=16, queue="small", priority_class="build")
    assert np["state"] == "Pending" and np["reason"] == "NonPreemptibleOverQuota"
    # limit
    _q(client, cid, "capped", 0, limit_gpus=8)
    lim = _w(client, cid, name="lim", gpus=16, queue="capped")
    assert lim["reason"] == "OverLimit"


def test_fair_share_priority_then_weight():
    s = kai_scheduler._ClusterSched("x")
    for n, q, p, w in (("a", 10, 100, 1), ("b", 10, 100, 3), ("hi", 0, 200, 1)):
        s.queues[n] = kai_scheduler.Queue(name=n, quota_gpus=q, priority=p, over_quota_weight=w,
                                          parent_queue="default-parent-queue")
    for i, (q, g) in enumerate((("a", 100), ("b", 100), ("hi", 20))):
        s.jobs[f"j{i}"] = kai_scheduler.Workload(
            name=f"j{i}", kind="PyTorchJob", namespace="d", queue=q, gpus=g, gpus_per_pod=4,
            pods=g // 4, priority_class="train", priority=50, preemptible=True)
    fs = kai_scheduler.fair_share(s, 100)
    assert fs["hi"] == 20                          # higher priority takes over-quota first
    assert fs["a"] == pytest.approx(10 + 60 * 0.25) and fs["b"] == pytest.approx(10 + 60 * 0.75)


def test_stale_gang_eviction_on_node_replace(client):
    _, cid = _cluster(client)
    _q(client, cid, "research", 144)
    a = _w(client, cid, name="a", gpus=16, queue="research")
    node = a["placement"]["pods"][0]["node"]
    r = client.post(f"/api/v1/nvke/clusters/{cid}/nodes/{node}/replace",
                    json={"reason": "XID 79"})
    assert r.status_code == 200, r.text
    a = client.get(f"/api/v1/nvke/clusters/{cid}/workloads/a").json()
    assert a["evictions"] and a["evictions"][0]["action"] == "stalegangeviction"
    assert a["state"] == "Running"                 # rescheduled whole on healthy nodes


def test_curtail_evicts_preemptible_first(client):
    _, cid = _cluster(client)
    _q(client, cid, "prod", 72)
    _q(client, cid, "batch", 0)
    inf = _w(client, cid, name="inf", gpus=72, queue="prod", priority_class="inference")
    tr = _w(client, cid, name="tr", gpus=72, queue="batch", priority_class="train")
    assert inf["state"] == tr["state"] == "Running"
    # operator curtails 72 GPUs: the preemptible training gang goes, inference stays
    assert client.post(f"/api/v1/nvke/clusters/{cid}/scheduler/curtail",
                       json={"gpus": 72}).status_code == 200
    s = client.get(f"/api/v1/nvke/clusters/{cid}/scheduler").json()
    assert s["capacity"]["fenced_gpus"] == 72
    states = {w["name"]: w["state"] for w in client.get(f"/api/v1/nvke/clusters/{cid}/workloads").json()}
    assert states == {"inf": "Running", "tr": "Preempted"}
    r = client.delete(f"/api/v1/nvke/clusters/{cid}/scheduler/curtail").json()
    assert r["started"] == ["tr"]


def test_tenant_scope(client):
    tid, cid = _cluster(client)
    other = _tenant(client, "other-co")
    assert client.get(f"/api/v1/nvke/clusters/{cid}/queues",
                      headers={"X-Tenant-Id": other}).status_code == 403
    assert client.get(f"/api/v1/nvke/clusters/{cid}/queues",
                      headers={"X-Tenant-Id": tid}).status_code == 200
    assert client.post(f"/api/v1/nvke/clusters/{cid}/scheduler/curtail", json={"gpus": 4},
                       headers={"X-Tenant-Id": tid}).status_code == 403


def test_validation_errors(client):
    _, cid = _cluster(client, nodes=2)
    base = f"/api/v1/nvke/clusters/{cid}/workloads"
    assert client.post(base, json={"gpus": 6}).status_code == 422       # not a multiple of 4
    assert client.post(base, json={"gpus": 4, "kind": "Pod"}).status_code == 422
    assert client.post(base, json={"gpus": 4, "queue": "nope"}).status_code == 404
    assert client.post(base, json={"gpus": 4, "priority_class": "x"}).status_code == 422
    assert client.post(base, json={"gpus": 4, "queue": "default-parent-queue"}).status_code == 404


# ---------------------------------------------------------------------------
# Contract: Queue / PodGroup / Topology validate against the KAI CRDs
# ---------------------------------------------------------------------------
def _ref_root():
    for p in Path(__file__).resolve().parents:
        cand = p / "ref" / "upstream"
        if cand.is_dir():
            return cand
    return None


CRDS = {
    ("scheduling.run.ai/v2", "Queue"): "scheduling.run.ai_queues.yaml",
    ("scheduling.run.ai/v2alpha2", "PodGroup"): "scheduling.run.ai_podgroups.yaml",
    ("kai.scheduler/v1alpha1", "Topology"): "kai.scheduler_topologies.yaml",
}
TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list,
         "number": (int, float)}


def _validate(v, s, path, errs):
    if s.get("x-kubernetes-int-or-string") and isinstance(v, (int, str)):
        return
    if "anyOf" in s and not s.get("type"):
        return
    t = s.get("type")
    if t and not isinstance(v, TYPES[t]) or (t in ("integer", "number") and isinstance(v, bool)):
        errs.append(f"{path}: {type(v).__name__} != {t}")
        return
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{path}: {v!r} not in {s['enum']}")
    if t == "object" and isinstance(v, dict):
        for r in s.get("required", []):
            if r not in v:
                errs.append(f"{path}.{r}: required")
        props = s.get("properties")
        if props is None:
            return
        for k, x in v.items():
            if k not in props:
                if not (s.get("additionalProperties") or s.get("x-kubernetes-preserve-unknown-fields")):
                    errs.append(f"{path}.{k}: unknown field")
                continue
            if path == "" and k == "metadata":
                continue
            _validate(x, props[k], f"{path}.{k}", errs)
    if t == "array" and isinstance(v, list) and "items" in s:
        for i, x in enumerate(v):
            _validate(x, s["items"], f"{path}[{i}]", errs)


def _check(root, o):
    doc = yaml.safe_load((root / "kai-scheduler/deployments/kai-scheduler/crds" /
                          CRDS[(o["apiVersion"], o["kind"])]).read_text())
    ver = next(x for x in doc["spec"]["versions"] if x["name"] == o["apiVersion"].split("/")[1])
    errs: list = []
    _validate(o, ver["schema"]["openAPIV3Schema"], "", errs)
    assert not errs, (o["kind"], o["metadata"]["name"], errs[:5])


def test_objects_match_kai_crds(client):
    root = _ref_root()
    if root is None:
        pytest.skip("ref/upstream not found")
    _, cid = _cluster(client)
    _q(client, cid, "research", 72)
    _w(client, cid, name="ok", gpus=8, queue="research")
    _w(client, cid, name="strict", gpus=144, queue="research", topology="required")
    objs = client.get(f"/api/v1/nvke/clusters/{cid}/apis/scheduling.run.ai/v2/queues").json()["items"]
    objs += client.get(f"/api/v1/nvke/clusters/{cid}/apis/scheduling.run.ai/v2alpha2/podgroups").json()["items"]
    objs += client.get(f"/api/v1/nvke/clusters/{cid}/apis/kai.scheduler/v1alpha1/topologies").json()["items"]
    seen = set()
    for o in objs:
        _check(root, o)
        seen.add(o["kind"])
    assert seen == {"Queue", "PodGroup", "Topology"}
    levels = objs[-1]["spec"]["levels"]
    assert levels[-1]["nodeLabel"] == "kubernetes.io/hostname"
    assert any(lv["nodeLabel"] == "nvidia.com/gpu.clique" for lv in levels)


def _run_scenario(client, fname):
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(fname, root / "app" / "demo" / "scenarios" / f"{fname}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class Res:
        def __init__(self, r):
            self.status = r.status_code
            try:
                self.body = r.json()
            except ValueError:
                self.body = r.text

    ctx: dict = {}
    for st in mod.SCENARIO["steps"]:
        call = st["call"]
        v = lambda x: x(ctx) if callable(x) else x  # noqa: E731
        res = Res(client.request(call["method"], v(call["path"]), json=v(call.get("json")),
                                 params=v(call.get("params"))))
        ok, ko, en = st["expect"](res, ctx)
        assert ok, (st["id"], res.status, en)
        assert ko and en
        if st.get("save"):
            st["save"](res, ctx)
    return mod.SCENARIO


def test_demo_scenario_passes(client):
    sc = _run_scenario(client, "f30_scheduling")
    assert sc["feature"] == "F30" and sc["title_ko"] and sc["title_en"]
