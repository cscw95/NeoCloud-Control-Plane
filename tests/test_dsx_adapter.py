"""DSX baseline — NICo REST adapter (NOCP_NICO_API=rest|bridge), versions, catalog."""
import json

import httpx
import pytest

from app import adapters, dsx
from app.adapters import (NicoHttpAdapter, NicoRestAdapter, build_http_adapter,
                          nico_api_mode)


def _mock(routes):
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        for (m, p), resp in routes.items():
            if req.method == m and req.url.path == p:
                status, body = resp(req) if callable(resp) else resp
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"detail": "not found"})
    return httpx.MockTransport(handler), calls


BRIDGE_HOST = {"host_id": "nh-1", "tray_id": "t-1", "sku": "vr-nvl72",
               "state": "provisioned", "host_ip": "10.1.1.2"}


def _adapter(routes):
    t, calls = _mock(routes)
    c = httpx.Client(base_url="http://nico", transport=t)
    return NicoRestAdapter("http://nico/nico-bridge", org="neocloud", client=c), calls


def test_mode_defaults_to_rest(monkeypatch):
    monkeypatch.delenv("NOCP_NICO_API", raising=False)
    assert nico_api_mode() == "rest"
    assert isinstance(build_http_adapter("http://x:9000/nico-bridge"), NicoRestAdapter)
    monkeypatch.setenv("NOCP_NICO_API", "bridge")
    a = build_http_adapter("http://x:9000")
    assert type(a) is NicoHttpAdapter
    assert str(a._c.base_url).rstrip("/") == "http://x:9000/nico-bridge"


def test_rest_list_hosts_maps_machines():
    machines = [{"id": "nh-1", "status": "Ready", "siteId": "s1",
                 "instanceTypeId": "vr-nvl72", "labels": {"trayId": "t-1"}},
                {"id": "nh-2", "status": "InUse", "tenantId": "tnt-1",
                 "instanceId": "i-9", "maintenanceMessage": "leak"}]
    a, calls = _adapter({("GET", "/v2/org/neocloud/nico/machine"): (200, machines)})
    hosts = a.list_hosts()
    assert [h.state.value for h in hosts] == ["pool_ready", "allocated"]
    assert hosts[0].tray_id == "t-1" and hosts[1].cordoned
    assert calls == [("GET", "/v2/org/neocloud/nico/machine")]


def test_rest_falls_back_to_bridge_when_resource_missing():
    a, calls = _adapter({("GET", "/nico-bridge/hosts"): (200, [BRIDGE_HOST])})
    hosts = a.list_hosts()
    assert hosts[0].host_id == "nh-1"
    assert calls[-1] == ("GET", "/nico-bridge/hosts")


def test_rest_allocate_and_release_use_instance_resource():
    seen = {}

    def create(req):
        seen["body"] = json.loads(req.content)
        return 201, {"id": "i-1", "machineId": "nh-1"}
    a, calls = _adapter({
        ("POST", "/v2/org/neocloud/nico/instance"): create,
        ("GET", "/nico-bridge/hosts/nh-1"): (200, {**BRIDGE_HOST, "state": "allocated",
                                                   "instance_id": "i-1"}),
        ("DELETE", "/v2/org/neocloud/nico/instance/i-1"): (202, {"machineId": "nh-1"}),
    })
    host = a.allocate("nh-1", "tnt-a")
    assert seen["body"]["machineId"] == "nh-1" and seen["body"]["tenantId"] == "tnt-a"
    assert host.state.value == "allocated"
    job = a.release("i-1")
    assert job.state == "succeeded" and a.get_job(job.job_id) is job
    # host-state ops without a REST object stay on the bridge
    assert ("POST", "/v2/org/neocloud/nico/instance") in calls


def test_rest_errors_other_than_missing_surface():
    a, _ = _adapter({("GET", "/v2/org/neocloud/nico/machine/nh-1"):
                     (409, {"detail": "busy"})})
    with pytest.raises(Exception) as e:
        a.get_host("nh-1")
    assert getattr(e.value, "status_code", None) == 409


def test_platform_versions_and_catalog(client):
    v = client.get("/api/v1/platform/versions").json()
    for k, val in dsx.VERSIONS.items():
        assert v[k] == val
    assert set(v["layers"]) == {"provisioning", "runtime", "workload", "fleet_health"}
    cat = client.get("/api/v1/catalog").json()
    assert [p["id"] for p in cat["products"]] == ["bmaas", "managed-k8s"]
    assert cat["products"][1]["engine"] == "nvke"


def test_rest_paths_table_uses_nico_prefix():
    assert all(p.startswith("/v2/org/{org}/nico/") for p in adapters.NICO_REST_PATHS.values())
