"""F32 marketplace — catalog, one-click deploy (DGD on AI-Infra, stub fallback),
tenant scope, dedicated tier gang-scheduled via F30, DGD CRD contract."""
import json
from pathlib import Path

import httpx
import pytest
import yaml

from app import dsx

from test_kai_scheduler import _ref_root, _run_scenario, _validate


def _tenant(client, name="mkt-co"):
    return client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "k8s_namespace"}).json()["id"]


def test_catalog(client):
    r = client.get("/api/v1/marketplace/models").json()
    assert r["object"] == "list" and r["source"] == "stub"
    ids = {m["id"] for m in r["data"]}
    assert {"llama-3.3-70b-instruct", "deepseek-r1", "kanana-1.5-8b-instruct"} <= ids
    m = client.get("/api/v1/marketplace/models/deepseek-r1").json()
    tiers = {t["id"]: t for t in m["tiers"]}
    assert tiers["serverless"]["gpus"] == 8 and tiers["dedicated"]["gpus"] == 24
    assert tiers["serverless"]["price"]["usd_per_1m_tokens"]["output"] == 4.0
    assert m["reasoning"] is True
    assert client.get("/api/v1/marketplace/models/nope").status_code == 404


def test_deploy_stub_and_lifecycle(client):
    tid = _tenant(client)
    r = client.post("/api/v1/marketplace/deploy",
                    json={"model": "kanana-1.5-8b-instruct", "tier": "serverless", "tenant_id": tid})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["engine"] == "stub" and d["state"] == "Ready" and d["gpus"] == 1
    assert d["dgd"]["kind"] == "DynamoGraphDeployment" and d["namespace"] == tid
    assert [c["type"] for c in d["dgd"]["spec"]["components"]] == ["frontend", "worker"]
    assert d["endpoint"]["chat_completions"] == "/v1/chat/completions"
    assert d["upstream"]["request"]["path"].endswith(f"/namespaces/{tid}/dynamographdeployments")
    assert [x["id"] for x in client.get(f"/api/v1/marketplace/deployments?tenant_id={tid}").json()] == [d["id"]]
    assert client.get(f"/api/v1/marketplace/deployments/{d['id']}").json()["model"] == "kanana-1.5-8b-instruct"
    assert client.get("/api/v1/marketplace/models").json()["data"][3]["marketplace_deployments"] == 1
    assert client.delete(f"/api/v1/marketplace/deployments/{d['id']}").status_code == 200
    assert client.get(f"/api/v1/marketplace/deployments/{d['id']}").status_code == 404


def test_deploy_validation_and_tenant_scope(client):
    tid, other = _tenant(client), _tenant(client, "mkt-other")
    url = "/api/v1/marketplace/deploy"
    assert client.post(url, json={"model": "x", "tenant_id": tid}).status_code == 404
    assert client.post(url, json={"model": "deepseek-r1", "tier": "gold", "tenant_id": tid}).status_code == 422
    assert client.post(url, json={"model": "deepseek-r1"}).status_code == 422          # operator w/o tenant
    assert client.post(url, json={"model": "deepseek-r1", "tenant_id": "tnt-nope"}).status_code == 404
    # tenant: tenant_id comes from the identity; a foreign one is refused
    own = {"X-Tenant-Id": tid}
    r = client.post(url, json={"model": "deepseek-r1"}, headers=own)
    assert r.status_code == 201 and r.json()["tenant_id"] == tid
    assert client.post(url, json={"model": "deepseek-r1", "tenant_id": other}, headers=own).status_code == 403
    did = r.json()["id"]
    assert client.get(f"/api/v1/marketplace/deployments/{did}",
                      headers={"X-Tenant-Id": other}).status_code == 403
    assert client.get("/api/v1/marketplace/deployments", headers={"X-Tenant-Id": other}).json() == []
    assert client.post(url, json={"model": "deepseek-r1", "tier": "serverless", "cluster_id": "x"},
                       headers=own).status_code == 422


def test_deploy_through_ai_infra_dgd(client, monkeypatch):
    """AI-Infra reachable: the DGD manifest is POSTed as-is and its CR is returned."""
    seen = {}

    def handler(req: httpx.Request):
        if req.method == "POST" and req.url.path.endswith("/dynamographdeployments"):
            body = json.loads(req.content)
            seen["body"] = body
            return httpx.Response(201, json={**body, "metadata": {**body["metadata"], "uid": "u-1"},
                                             "status": {"state": "successful", "conditions": []}})
        if req.method == "DELETE":
            seen["deleted"] = req.url.path
            return httpx.Response(200, json={"kind": "Status", "status": "Success"})
        if req.url.path == "/dynamo/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-instruct", "deployments": ["d1"]}]})
        return httpx.Response(404, json={"detail": "nope"})

    monkeypatch.setenv("NOCP_DSX_OFFLINE", "0")
    dsx.set_transport(httpx.MockTransport(handler))
    try:
        assert client.get("/api/v1/marketplace/models").json()["data"][0]["engine"] == "dynamo"
        tid = _tenant(client)
        d = client.post("/api/v1/marketplace/deploy", json={
            "model": "llama-3.3-70b-instruct", "tier": "dedicated", "tenant_id": tid}).json()
        assert d["engine"] == "dynamo" and d["state"] == "Ready"
        types = [c["type"] for c in seen["body"]["spec"]["components"]]
        assert types == ["frontend", "prefill", "decode"]
        assert d["upstream"]["response"]["status"] == 201
        client.delete(f"/api/v1/marketplace/deployments/{d['id']}")
        assert seen["deleted"].endswith("/" + d["name"])
    finally:
        dsx.set_transport(None)


def test_deploy_rejected_by_operator_is_surfaced(client, monkeypatch):
    def handler(req):
        return httpx.Response(409, json={"kind": "Status", "status": "Failure", "code": 409,
                                         "message": "insufficient free GPUs", "reason": "AlreadyExists"})
    monkeypatch.setenv("NOCP_DSX_OFFLINE", "0")
    dsx.set_transport(httpx.MockTransport(handler))
    try:
        r = client.post("/api/v1/marketplace/deploy",
                        json={"model": "deepseek-r1", "tenant_id": _tenant(client)})
        assert r.status_code == 409 and "insufficient" in r.json()["detail"]
    finally:
        dsx.set_transport(None)


def test_dedicated_on_nvke_cluster_is_gang_scheduled(client):
    tid = _tenant(client)
    c = client.post("/api/v1/nvke/clusters", json={
        "name": "srv", "tenant_id": tid,
        "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 18}]}).json()
    client.post(f"/api/v1/nvke/clusters/{c['id']}/queues", json={"name": "serving", "quota_gpus": 48})
    d = client.post("/api/v1/marketplace/deploy", json={
        "model": "deepseek-r1", "tier": "dedicated", "tenant_id": tid,
        "cluster_id": c["id"], "queue": "serving"}).json()
    s = d["scheduler"]
    assert s["state"] == "Running" and s["placement"]["single_clique"]
    w = client.get(f"/api/v1/nvke/clusters/{c['id']}/workloads/{s['workload']}").json()
    assert w["kind"] == "DynamoGraphDeployment" and w["priority_class"] == "inference"
    assert w["podgroup"]["spec"]["preemptibility"] == "non-preemptible"
    client.delete(f"/api/v1/marketplace/deployments/{d['id']}")
    assert client.get(f"/api/v1/nvke/clusters/{c['id']}/workloads/{s['workload']}").json()["state"] == "Completed"


def test_dgd_matches_upstream_crd(client):
    root = _ref_root()
    if root is None:
        pytest.skip("ref/upstream not found")
    doc = yaml.safe_load((root / "dynamo/deploy/operator/config/crd/bases/"
                          "nvidia.com_dynamographdeployments.yaml").read_text())
    schema = next(v for v in doc["spec"]["versions"] if v["name"] == "v1beta1")["schema"]["openAPIV3Schema"]
    tid = _tenant(client)
    for tier in ("serverless", "dedicated"):
        d = client.post("/api/v1/marketplace/deploy",
                        json={"model": "qwen3-235b-a22b", "tier": tier, "tenant_id": tid}).json()
        errs: list = []
        _validate(d["upstream"]["request"]["body"], schema, "", errs)
        assert not errs, (tier, errs[:5])


def test_demo_scenario_passes(client):
    sc = _run_scenario(client, "f32_marketplace")
    assert sc["feature"] == "F32"
