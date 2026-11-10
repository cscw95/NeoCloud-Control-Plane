"""F28 readiness gate (local engine, NICo offline) and F31 Slurm (Slinky) emulation."""
import json
import pathlib

import pytest
import yaml

from app import readiness, slurm
from tests.schema_check import validate

C = pathlib.Path(__file__).parent / "contracts"
VERDICT = json.loads((C / "node-verdict.schema.json").read_text())


def _crd(kind):
    d = yaml.safe_load((C / f"slinky.slurm.net_{kind}.yaml").read_text())
    v = d["spec"]["versions"][0]
    return d["spec"]["group"] + "/" + v["name"], d["spec"]["names"]["kind"], v["schema"]["openAPIV3Schema"]


@pytest.fixture(autouse=True)
def _clean():
    readiness.reset()
    slurm.reset()
    yield
    readiness.reset()
    slurm.reset()


def _cluster(client, **kw):
    r = client.post("/api/v1/slurm/clusters", json={"name": "train", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _sbatch(client, cid, nodes, extra="", **job):
    script = f"#!/bin/bash\n#SBATCH --job-name=llm\n#SBATCH --nodes={nodes}\n#SBATCH --gres=gpu:4\n{extra}srun python train.py\n"
    r = client.post(f"/api/v1/slurm/clusters/{cid}/jobs", json={"job": {"script": script, **job}})
    assert r.status_code == 200, r.text
    return r.json()


# ── F28 readiness ─────────────────────────────────────────────────────
def test_local_gate_matches_verdict_schema(client):
    r = client.post("/api/v1/readiness/evaluate", json={"rack": "su-2-rack-00"})
    g = r.json()
    assert r.status_code == 200 and g["ready"] and g["nodes_total"] == 18 and g["source"] == ["local"]
    for n in g["nodes"]:
        assert validate(n["verdict"], VERDICT) == []
    assert client.get("/api/v1/readiness/rules").json()["source"] == "local"


def test_injected_fault_fails_gate(client):
    node = "su-2-rack-00-tray-03"
    assert client.post("/api/v1/readiness/inject", json={"node": node, "signals": {"ib_ports_up": 3}}).status_code == 200
    g = client.post("/api/v1/readiness/evaluate", json={"nodes": [node, "su-2-rack-00-tray-04"]}).json()
    assert not g["ready"] and g["nodes_failed"] == [node]
    bad = next(n for n in g["nodes"] if n["node"] == node)
    assert bad["failing_checks"] == ["ib-link-down"] and validate(bad["verdict"], VERDICT) == []
    r = client.post("/api/v1/readiness/inject", json={"node": node, "signals": {"x": 1}})
    assert r.status_code == 400
    r = client.post("/api/v1/readiness/inject", headers={"X-Tenant-Id": "tnt-a"},
                    json={"node": node, "signals": {"ib_ports_up": 3}})
    assert r.status_code == 403
    client.delete(f"/api/v1/readiness/inject/{node}")
    assert client.post("/api/v1/readiness/evaluate", json={"nodes": [node]}).json()["ready"]


def test_acceptance_records_gate(monkeypatch):
    out = readiness.acceptance_check(["su-2-rack-00-tray-00"], "k8s-x")
    assert out["status"] == "pass" and out["name"] == "health-validation-rules"
    readiness.inject("su-2-rack-00-tray-00", {"signals": {"DCGM_FI_DEV_ECC_DBE_VOL_TOTAL": 2}})
    out = readiness.acceptance_check(["su-2-rack-00-tray-00"], "k8s-x")
    assert out["status"] == "fail" and out["failed_nodes"] == ["su-2-rack-00-tray-00"]


# ── F31 Slurm ─────────────────────────────────────────────────────────
def test_cluster_crs_match_slinky_crds(client):
    c = _cluster(client)
    assert c["nodes_total"] == 36 and c["gpus_total"] == 144 and len(c["racks"]) == 2
    kinds = {}
    for cr in c["crs"]:
        kinds[cr["kind"]] = cr
    for crd in ("controllers", "nodesets", "restapis"):
        api, kind, schema = _crd(crd)
        cr = kinds[kind]
        assert cr["apiVersion"] == api
        assert validate({k: v for k, v in cr.items() if k not in ("apiVersion", "kind", "metadata")},
                        {**schema, "properties": {k: schema["properties"][k] for k in ("spec", "status")}},
                        crd=True) == []
    assert "TopologyPlugin=topology/block" in kinds["Controller"]["spec"]["extraConf"]
    assert kinds["NodeSet"]["status"]["slurmIdle"] == 36


def test_topology_conf_block_and_tree(client):
    c = _cluster(client)
    t = client.get(f"/api/v1/slurm/clusters/{c['id']}/topology.conf")
    assert t.status_code == 200 and t.headers["content-type"].startswith("text/plain")
    lines = [x for x in t.text.splitlines() if not x.startswith("#")]
    assert lines[0].startswith("BlockName=block001 Nodes=gpu-[000-017]")
    assert lines[1].startswith("BlockName=block002 Nodes=gpu-[018-035]")
    assert lines[2] == "BlockSizes=18,36"
    tree = client.get(f"/api/v1/slurm/clusters/{c['id']}/topology.conf?plugin=topology/tree").text
    assert "SwitchName=leaf-su-2-rack-00 Nodes=gpu-[000-017]" in tree
    assert "SwitchName=spine-su-2 Switches=leaf-su-2-rack-[00-01]" in tree


def test_hostlist_roundtrip():
    names = ["gpu-000", "gpu-001", "gpu-002", "gpu-005", "gpu-010", "gpu-011"]
    assert slurm.hostlist(names) == "gpu-[000-002,005,010-011]"
    assert slurm.expand_hostlist("gpu-[000-002,005,010-011]") == names


def test_sbatch_slurmrestd_shape_and_same_block(client):
    c = _cluster(client)
    r = _sbatch(client, c["id"], 18)
    assert set(r) >= {"job_id", "step_id", "job_submit_user_msg", "result", "meta", "errors", "warnings"}
    assert r["result"]["error_code"] == 0 and r["meta"]["plugin"]["data_parser"] == "data_parser/v0.0.42"
    j = r["job"]
    assert j["job_state"] == ["RUNNING"] and j["nodes"] == "gpu-[000-017]"
    assert j["x-neocloud"]["placement"]["single_block"] is True
    assert len(j["x-neocloud"]["prolog"]) == 18 and all(p["exit_code"] == 0 for p in j["x-neocloud"]["prolog"])
    assert [h["state"] for h in j["x-neocloud"]["history"]] == ["PENDING", "CONFIGURING", "RUNNING"]
    # second 8-node job fits the other block whole; a 12-node job then waits
    j2 = _sbatch(client, c["id"], 8)["job"]
    assert j2["x-neocloud"]["placement"]["blocks"] == ["su-2-rack-01"]
    j3 = _sbatch(client, c["id"], 12)["job"]
    assert j3["job_state"] == ["PENDING"] and j3["state_reason"] == "Resources"
    # job 1 completes → job 3 starts on block 1
    done = client.post(f"/api/v1/slurm/clusters/{c['id']}/jobs/{j['job_id']}:complete", json={}).json()
    assert done["job_state"] == ["COMPLETED"] and done["exit_code"]["return_code"]["number"] == 0
    j3 = client.get(f"/api/v1/slurm/clusters/{c['id']}/slurm/v0.0.42/job/{j3['job_id']}").json()["jobs"][0]
    assert j3["job_state"] == ["RUNNING"] and j3["x-neocloud"]["placement"]["blocks"] == ["su-2-rack-00"]


def test_prolog_failure_drains_and_requeues(client):
    c = _cluster(client)
    readiness.inject("su-2-rack-00-tray-02", {"signals": {"DCGM_FI_DEV_XID_ERROR": 79}, "gpu": 0})
    j = _sbatch(client, c["id"], 18)["job"]
    assert j["job_state"] == ["RUNNING"] and j["restart_cnt"] == 1
    hist = [h["state"] for h in j["x-neocloud"]["history"]]
    assert hist[:4] == ["PENDING", "CONFIGURING", "REQUEUED", "CONFIGURING"]
    assert "gpu-002" not in slurm.expand_hostlist(j["nodes"])
    assert j["x-neocloud"]["placement"]["blocks"] == ["su-2-rack-01"]  # 17 left in block 1 → whole block 2
    node = client.get(f"/api/v1/slurm/clusters/{c['id']}/slurm/v0.0.42/node/gpu-002").json()["nodes"][0]
    assert node["state"] == ["IDLE", "DRAIN"] and "gpu-xid-critical" in node["reason"]
    # operator clears the fault and resumes the node
    readiness.clear("su-2-rack-00-tray-02")
    r = client.post(f"/api/v1/slurm/clusters/{c['id']}/slurm/v0.0.42/node/gpu-002", json={"state": ["RESUME"]})
    assert r.status_code == 200
    assert client.get(f"/api/v1/slurm/clusters/{c['id']}/slurm/v0.0.42/node/gpu-002").json()["nodes"][0]["state"] == ["IDLE"]


def test_node_failure_requeues_excluding_node(client):
    c = _cluster(client)
    j = _sbatch(client, c["id"], 4)["job"]
    victim = slurm.expand_hostlist(j["nodes"])[1]
    out = client.post(f"/api/v1/slurm/clusters/{c['id']}/nodes/{victim}:fail", json={}).json()
    assert out["state"] == ["DOWN"] and out["remediation"]["status"] in ("stub", "started", "error")
    assert out["job"]["job_state"] == ["RUNNING"] and out["job"]["restart_cnt"] == 1
    assert victim not in slurm.expand_hostlist(out["job"]["nodes"])
    j = client.get(f"/api/v1/slurm/clusters/{c['id']}/slurm/v0.0.42/job/{j['job_id']}").json()["jobs"][0]
    assert "NODE_FAIL" in [h["state"] for h in j["x-neocloud"]["history"]]
    assert client.post(f"/api/v1/slurm/clusters/{c['id']}/nodes/{victim}:fail",
                       headers={"X-Tenant-Id": "tnt-a"}, json={}).status_code == 403


def test_native_submit_cancel_and_errors(client):
    c = _cluster(client, rack_count=1)
    base = f"/api/v1/slurm/clusters/{c['id']}/slurm/v0.0.42"
    assert client.get(base + "/ping").json()["pings"][0]["pinged"] == "UP"
    r = client.post(base + "/job/submit", json={"script": "#!/bin/bash\n#SBATCH -N 2 -t 1:00:00\nhostname\n",
                                                "job": {"name": "legacy-shape"}})
    jid = r.json()["job_id"]
    j = client.get(f"{base}/job/{jid}").json()["jobs"][0]
    assert j["name"] == "legacy-shape" and j["time_limit"]["number"] == 60 and j["node_count"]["number"] == 2
    assert client.delete(f"{base}/job/{jid}").status_code == 200
    j = client.get(f"{base}/job/{jid}").json()["jobs"][0]
    assert j["job_state"] == ["CANCELLED"]
    assert client.get(base + "/nodes").json()["nodes"][0]["state"] == ["IDLE"]
    r = client.post(base + "/job/submit", json={"job": {"script": ""}})
    assert r.status_code == 400 and r.json()["detail"]["errors"][0]["error_number"] == 2072
    r = client.post(base + "/job/submit", json={"job": {"script": "#!/bin/bash\n#SBATCH -N 99\nx\n"}})
    assert r.status_code == 400
    r = client.post(base + "/job/submit", json={"job": {"script": "#!/bin/bash\nx\n", "partition": "nope"}})
    assert r.status_code == 400
    assert client.get(f"{base}/job/999").status_code == 404
    parts = client.get(base + "/partitions").json()["partitions"]
    assert parts[0]["nodes"]["configured"] == "gpu-[000-017]"


def test_segment_and_tenant_scope(client):
    c = client.post("/api/v1/slurm/clusters", headers={"X-Tenant-Id": "tnt-a"},
                    json={"name": "seg"}).json()
    assert c["tenant_id"] == "tnt-a"
    assert client.get(f"/api/v1/slurm/clusters/{c['id']}", headers={"X-Tenant-Id": "tnt-b"}).status_code == 403
    assert client.get("/api/v1/slurm/clusters", headers={"X-Tenant-Id": "tnt-b"}).json() == []
    j = _sbatch(client, c["id"], 24, extra="#SBATCH --segment=12\n")["job"]
    assert j["job_state"] == ["RUNNING"] and len(j["x-neocloud"]["placement"]["blocks"]) == 2
    nodes = slurm.expand_hostlist(j["nodes"])
    per_rack = {}
    for n in nodes:
        per_rack.setdefault(slurm._S["clusters"][c["id"]]["nodes"][n]["rack_id"], []).append(n)
    assert sorted(len(v) for v in per_rack.values()) == [12, 12]


def test_parse_sbatch():
    o = slurm.parse_sbatch("#!/bin/bash\n# comment\n#SBATCH -J x --nodes 3\n#SBATCH --exclusive\n"
                           "#SBATCH --gres=gpu:2 # trailing\nsrun x\n#SBATCH --nodes=9\n")
    assert o == {"job-name": "x", "nodes": "3", "exclusive": True, "gres": "gpu:2"}


# ── demo scenarios (same runner as scripts/demo_f28_f31.py, in-process) ──
@pytest.mark.parametrize("key", ["f28", "f31"])
def test_demo_scenarios_pass(client, key):
    import importlib.util
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "demo_f28_f31.py"
    spec = importlib.util.spec_from_file_location("demo_f28_f31", path)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)

    def send(method, p, body):
        r = client.request(method, p, json=body)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return demo.Res(r.status_code, data)

    sc = demo.load(demo.NAMES[key])
    assert sc["feature"] == key.upper() and sc["title_ko"] and sc["title_en"]
    steps = demo.run_steps(sc, send)
    bad = [(s["id"], ko) for s, _, _, ok, ko, _ in steps if not ok]
    assert not bad and len(steps) == len(sc["steps"]), bad
