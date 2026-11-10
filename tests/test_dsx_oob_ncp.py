"""OOB monitoring, infra proxy, metering, isolation matrix, RMA, firmware."""
import json

import httpx
import pytest

from app import dsx, nico_fake


@pytest.fixture()
def upstream():
    routes, calls = {}, []

    def handler(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.host, req.url.path, body))
        fn = routes.get((req.method, req.url.path))
        if fn is None:
            return httpx.Response(404, json={"detail": "nope"})
        st, b = fn(body)
        return httpx.Response(st, json=b)
    dsx.set_transport(httpx.MockTransport(handler))
    yield routes, calls
    dsx.set_transport(None)


def _tenant(client, name="oob-co"):
    return client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"}).json()["id"]


def test_oob_summary_local_and_event_webhook(client):
    s = client.get("/api/v1/oob/summary").json()
    assert s["source"] == "local-fallback" and s["totals"]["racks"] == 30
    row = s["racks"][0]
    for k in ("bmc_reachable", "bmc_total", "psu_ok", "psu_total", "leak",
              "max_inlet_c", "sel_critical", "power_kw", "cap_kw"):
        assert k in row
    # Redfish EventService push → leak overlay + SEL + bus
    r = client.post("/api/v1/oob/events", json={"Events": [
        {"EventType": "Alert", "MessageId": "Leak.1.0.LeakDetected", "Severity": "Critical",
         "Message": "coolant leak tray 3",
         "OriginOfCondition": {"@odata.id": "/redfish/v1/Chassis/su-1-rack-00"}},
        {"EventType": "Alert", "MessageId": "SEL.1.0.GpuXid", "Severity": "Critical",
         "Message": "XID 79", "OriginOfCondition": "/redfish/v1/Systems/su-1-rack-00-tray-03"}]})
    assert r.status_code == 202 and r.json()["accepted"] == 2
    s = client.get("/api/v1/oob/summary").json()
    rack = next(x for x in s["racks"] if x["rack"] == "su-1-rack-00")
    assert rack["leak"] == "Critical" and rack["sel_critical"] == 1
    ev = client.get("/api/v1/oob/events?topic=leak").json()["events"]
    assert ev[0]["rack"] == "su-1-rack-00"
    assert client.get("/api/v1/oob/sel/su-1-rack-00-tray-03").json()["entries"][0]["Message"] == "XID 79"
    assert {e["topic"] for e in client.get("/api/v1/events/recent").json()} >= {"leak", "health"}
    d = client.get("/api/v1/oob/racks/su-1-rack-00").json()
    assert len(d["trays"]) == 18 and len(d["power_shelves"]) == 4
    assert client.get("/api/v1/oob/racks/nope").status_code == 404


def test_oob_upstream_proxy_and_actions(client, upstream):
    routes, calls = upstream
    routes[("GET", "/emulator/v1/oob/summary")] = lambda b: (200, {"racks": [
        {"rack": "R1", "bmc_reachable": 17, "bmc_total": 18, "psu_ok": 23, "psu_total": 24,
         "leak": "OK", "max_inlet_c": 31.0, "sel_critical": 0, "power_kw": 150, "cap_kw": 187}]})
    routes[("POST", "/redfish/v1/Systems/su-1-rack-00-tray-00/Actions/GpuReset")] = lambda b: (202, {})
    routes[("PATCH", "/v2/org/neocloud/nico/machine/nh-su-1-rack-00-tray-00/power")] = \
        lambda b: (202, {"status": "accepted"})
    routes[("POST", "/redfish/v1/EventService/Subscriptions")] = lambda b: (201, {"Id": "sub-1"})
    s = client.get("/api/v1/oob/summary").json()
    assert s["source"] == "ai-infra" and s["totals"]["bmc_unreachable"] == 1
    r = client.post("/api/v1/oob/machines/nh-su-1-rack-00-tray-00/actions/ac-power-cycle").json()
    assert r["via"] == "nico"
    assert [c for c in calls if c[2].endswith("/power")][0][3]["action"] == "ACPowercycle"
    r = client.post("/api/v1/oob/machines/nh-su-1-rack-00-tray-00/actions/gpu-reset").json()
    assert r["via"] == "ai-infra-redfish"
    assert client.post("/api/v1/oob/machines/x/actions/explode").status_code == 422
    sub = client.post("/api/v1/oob/subscribe").json()
    assert sub["subscribed"] and sub["destination"].endswith("/api/v1/oob/events")


def test_infra_proxy(client, upstream):
    routes, calls = upstream
    routes[("GET", "/ufm/v1/fabric/health")] = lambda b: (200, {"links_active": 10})
    routes[("POST", "/emulator/v1/obs/racks/R1/control")] = lambda b: (200, {"ok": b})
    assert client.get("/api/v1/infra/ufm/v1/fabric/health").json() == {"links_active": 10}
    r = client.post("/api/v1/infra/emulator/v1/obs/racks/R1/control", json={"op": "off"})
    assert r.json() == {"ok": {"op": "off"}}
    assert client.get("/api/v1/infra/etc/passwd").status_code == 403
    assert client.post("/api/v1/infra/emulator/v1/reset").status_code == 403
    assert calls[0][1] == "127.0.0.1"


def test_infra_proxy_unreachable(client):
    assert client.get("/api/v1/infra/ufm/v1/fabric/health").status_code == 502


def test_metering_counts_nvke_nodes(client):
    tid = _tenant(client)
    client.post("/api/v1/nvke/clusters", json={
        "name": "m", "tenant_id": tid,
        "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 2}]})
    u = client.get(f"/api/v1/metering/usage?tenant={tid}").json()
    assert u["tenant"] == tid and u["period"]
    metrics = {(l["product"], l["metric"]) for l in u["lines"]}
    assert ("managed-k8s", "k8s_node_hour") in metrics and ("managed-k8s", "gpu_hour") in metrics
    assert sum(1 for l in u["lines"] if l["metric"] == "k8s_node_hour") == 2
    # tenant plane: forced to own tenant
    other = client.get("/api/v1/metering/usage?tenant=tnt-zzz",
                       headers={"X-Tenant-Id": tid}).json()
    assert other["tenant"] == tid


def test_isolation_matrix(client):
    tid = _tenant(client)
    r = client.post("/api/v1/allocations", json={
        "tenant_id": tid, "scope": "rack_set", "su_id": "su-2", "rack_ids": ["su-2-rack-00"]})
    assert r.status_code == 201, r.text
    m = client.get(f"/api/v1/isolation/{tid}").json()
    assert m["layers"][0]["status"] == "isolated"
    assert m["layers"][2]["status"] == "isolated" and "su-2-rack-00" in m["layers"][2]["value"]
    assert [l["layer"] for l in m["layers"]] == [
        "VPC / VRF", "InfiniBand P_Key", "NVLink Partition", "Spectrum-X", "NetworkSecurityGroup"]
    assert m["ok"]
    assert client.get("/api/v1/isolation/tnt-nope").status_code == 404
    assert client.get(f"/api/v1/isolation/{tid}",
                      headers={"X-Tenant-Id": "tnt-other"}).status_code == 403


def test_rma_workflow_with_nvke_migration(client):
    tid = _tenant(client)
    c = client.post("/api/v1/nvke/clusters", json={
        "name": "r", "tenant_id": tid,
        "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 1}]}).json()
    host = c["nodepools"][0]["nodes"][0]["nico_host_id"]
    r = client.post("/api/v1/rma", json={"machine": host, "component": "GPU",
                                         "reason": "NVSentinel XID 79", "source": "nvsentinel"})
    assert r.status_code == 201
    case = r.json()
    assert case["stage"] == "health_event" and case["tenant"] == tid
    case = client.post(f"/api/v1/rma/{case['id']}/advance").json()
    assert case["stage"] == "cordon_drain"
    assert case["steps"][-1]["migration"]["new_host"] != host
    assert nico_fake.FAKE_NICO.get_host(host).cordoned
    case = client.post(f"/api/v1/rma/{case['id']}/advance?to=pool_return").json()
    assert case["stage"] == "pool_return" and case["closed_at"]
    assert [s["stage"] for s in case["steps"]] == [
        "health_event", "cordon_drain", "isolate", "replace", "burn_in", "pool_return"]
    assert not nico_fake.FAKE_NICO.get_host(host).cordoned
    assert client.post(f"/api/v1/rma/{case['id']}/advance").status_code == 409
    assert client.get("/api/v1/rma?state=closed").json()["cases"][0]["id"] == case["id"]
    assert any(e["topic"] == "rma" for e in client.get("/api/v1/events/recent").json())
    assert client.post("/api/v1/rma", json={"machine": "bogus"}).status_code == 404


def test_firmware_baseline_and_drift(client):
    b = client.get("/api/v1/firmware/baseline").json()
    assert any(c["component"].startswith("BMC") for c in b["components"])
    nico_fake.FAKE_NICO.get_host("nh-su-1-rack-00-tray-01").firmware_ok = False
    d = client.get("/api/v1/firmware/drift").json()
    assert [x["machine"] for x in d["drift"]] == ["nh-su-1-rack-00-tray-01"]
    assert d["drift"][0]["rack"] == "su-1-rack-00"


def test_operator_only_surfaces_closed_to_tenants(client):
    h = {"X-Tenant-Id": "tnt-a"}
    for p in ("/api/v1/oob/summary", "/api/v1/power/domains", "/api/v1/rma",
              "/api/v1/firmware/drift", "/api/v1/infra/ufm/v1/fabric/health"):
        assert client.get(p, headers=h).status_code == 403, p


def test_sse_operator_query(client, monkeypatch):
    monkeypatch.setenv("NOCP_DEV_OPERATOR_DEFAULT", "0")
    assert client.get("/api/v1/events?once=true").status_code == 403
    assert client.get("/api/v1/events?once=true&operator=true").status_code == 200


def test_wpps_policy_from_console(client):
    r = client.post("/api/v1/racks/su-2-rack-00/power-policy", json={"policy": "max-q"}).json()
    assert r["policy"] == "maxq" and r["profile"] == "max-q"
    r = client.post("/api/v1/racks/su-2-rack-00/power-policy", json={"policy": "training"}).json()
    assert r["policy"] == "dps-dynamic" and r["power_cap_kw"] == round(227 * 0.92)
