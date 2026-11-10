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
    machines = [{"id": "nh-t-1", "status": "Ready", "lifecycleState": "ready",
                 "siteId": "s1", "instanceTypeId": "vr-nvl72"},
                {"id": "nh-t-2", "status": "Initializing", "lifecycleState": "validating"},
                {"id": "nh-t-3", "status": "InUse", "lifecycleState": "assigned"}]
    a, calls = _adapter({
        ("GET", "/v2/org/neocloud/nico/machine"): (200, machines),
        ("GET", "/nico-bridge/hosts"): (200, [{**BRIDGE_HOST, "host_id": "nh-t-3",
                                               "state": "provisioned"}])})
    hosts = a.list_hosts()
    assert [h.state.value for h in hosts] == ["pool_ready", "discovered", "provisioned"]
    assert hosts[0].tray_id == "t-1"
    assert calls[0] == ("GET", "/v2/org/neocloud/nico/machine")


def test_rest_get_host_ready_stays_on_rest():
    a, calls = _adapter({("GET", "/v2/org/neocloud/nico/machine/nh-1"):
                         (200, {"id": "nh-1", "status": "Ready", "lifecycleState": "ready",
                                "maintenanceMessage": "leak"})})
    h = a.get_host("nh-1")
    assert h.state.value == "pool_ready" and h.cordoned
    assert calls == [("GET", "/v2/org/neocloud/nico/machine/nh-1")]


def test_rest_falls_back_to_bridge_when_resource_missing():
    a, calls = _adapter({("GET", "/nico-bridge/hosts"): (200, [BRIDGE_HOST])})
    hosts = a.list_hosts()
    assert hosts[0].host_id == "nh-1"
    assert calls[-1] == ("GET", "/nico-bridge/hosts")


def test_rest_host_state_steps_use_bridge():
    a, calls = _adapter({
        ("POST", "/nico-bridge/hosts/nh-1/reserve"): (200, {**BRIDGE_HOST, "state": "reserved"}),
        ("POST", "/nico-bridge/instances"): (200, {**BRIDGE_HOST, "state": "allocated"})})
    assert a.reserve("nh-1").state.value == "reserved"
    assert a.allocate("nh-1", "tnt-a").state.value == "allocated"
    assert [c[1] for c in calls] == ["/nico-bridge/hosts/nh-1/reserve", "/nico-bridge/instances"]


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
