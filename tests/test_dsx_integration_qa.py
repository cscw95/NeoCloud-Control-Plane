"""Regressions from the live 4-repo chain QA (INTEGRATION-QA.md): upstream
shapes as the AI-Infra / NICo emulators really return them."""
import json

import httpx
import pytest

from app import dsx


@pytest.fixture()
def upstream():
    routes, calls = {}, []

    def handler(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body, dict(req.headers)))
        fn = routes.get((req.method, req.url.path))
        if fn is None:
            return httpx.Response(404, json={"detail": "nope"})
        st, b = fn(body)
        return httpx.Response(st, json=b)
    dsx.set_transport(httpx.MockTransport(handler))
    yield routes, calls
    dsx.set_transport(None)


def test_power_domains_with_rack_count_and_groups(client, upstream):
    """AI-Infra reports a domain's `racks` as a count; rack ids live on groups."""
    routes, _ = upstream
    dom = {"id": "pd-gasan", "operating_limit_kw": 3000.0, "mode": "static", "racks": 2,
           "resource_groups": ["rg-su-1"]}
    routes[("GET", "/dps/v1/power-domains")] = lambda b: (200, {"items": [dom]})
    routes[("GET", "/dps/v1/power-domains/pd-gasan")] = lambda b: (200, dom)
    routes[("GET", "/dps/v1/resource-groups")] = lambda b: (200, {"items": [
        {"id": "rg-su-1", "domain": "pd-gasan", "racks": ["su-1-rack-00", "su-1-rack-01"]}]})
    r = client.get("/api/v1/power/domains")
    assert r.status_code == 200, r.text
    d = r.json()["domains"][0]
    assert d["rack_count"] == 2 and d["racks"] == 2 and d["racks_cap_kw"] > 0
    assert client.get("/api/v1/power/domains/pd-gasan").json()["domain"]["rack_count"] == 2


def test_simulate_passes_profile_and_smoothing(client, upstream):
    routes, calls = upstream
    routes[("POST", "/dps/v1/simulate")] = lambda b: (200, {"static": {}, "dynamic": {}})
    client.post("/api/v1/power/simulate", json={"domain": "pd-x", "smoothing": True,
                                                "profile": "max-q"})
    sent = [c[2] for c in calls if c[1] == "/dps/v1/simulate"][0]
    assert sent["smoothing"] is True and sent["profile"] == "max-q"


def test_oob_rows_keyed_by_rack_id(client, upstream):
    """AI-Infra rows carry `rack_id`; rack detail and the leak overlay must find them."""
    routes, _ = upstream
    routes[("GET", "/emulator/v1/oob/summary")] = lambda b: (200, {"racks": [
        {"rack_id": "su-1-rack-00", "bmc_reachable": 18, "bmc_total": 18, "psu_ok": 35,
         "psu_total": 36, "leak": "OK", "max_inlet_c": 27.0, "sel_critical": 0,
         "power_kw": 80.0, "cap_kw": 94.0}]})
    s = client.get("/api/v1/oob/summary").json()
    assert s["racks"][0]["rack"] == "su-1-rack-00"
    d = client.get("/api/v1/oob/racks/su-1-rack-00").json()
    assert d["psu_ok"] == 35 and d["psu_total"] == 36 and d["cap_kw"] == 94.0
    client.post("/api/v1/oob/events", json={"Events": [{
        "MessageId": "NeoCloudEmulator.1.0.LeakDetected", "Severity": "Critical",
        "OriginOfCondition": {"@odata.id":
                              "/redfish/v1/Chassis/su-1-rack-00-tray-02/ThermalSubsystem/LeakDetection"}}]})
    ev = client.get("/api/v1/oob/events?topic=leak").json()["events"][0]
    assert ev["rack"] == "su-1-rack-00" and ev["machine"] == "su-1-rack-00-tray-02"
    assert client.get("/api/v1/oob/summary").json()["racks"][0]["leak"] == "Critical"


def test_subscribe_is_idempotent_and_prunes_duplicates(client, upstream):
    routes, calls = upstream
    dest = "http://127.0.0.1:8000/api/v1/oob/events"
    routes[("GET", "/redfish/v1/EventService/Subscriptions")] = lambda b: (200, {"Members": [
        {"@odata.id": "/redfish/v1/EventService/Subscriptions/sub-1"},
        {"@odata.id": "/redfish/v1/EventService/Subscriptions/sub-2"}]})
    for sid in ("sub-1", "sub-2"):
        routes[("GET", f"/redfish/v1/EventService/Subscriptions/{sid}")] = \
            lambda b, sid=sid: (200, {"Id": sid, "Destination": dest})
    routes[("DELETE", "/redfish/v1/EventService/Subscriptions/sub-2")] = lambda b: (200, {})
    sub = client.post("/api/v1/oob/subscribe").json()
    assert sub["subscribed"] and sub["id"] == "sub-1" and sub.get("reused")
    assert ("DELETE", "/redfish/v1/EventService/Subscriptions/sub-2") in [c[:2] for c in calls]
    assert not any(c[0] == "POST" for c in calls)


def test_action_refused_by_bmc_is_not_reported_accepted(client, upstream):
    """NICo answers 202 with delegated=false when the Redfish call failed."""
    routes, _ = upstream
    routes[("POST", "/v2/org/neocloud/nico/machine/nh-su-1-rack-00-tray-00/gpu/reset")] = \
        lambda b: (202, {"message": "accepted", "delegated": False,
                         "detail": "AI Infra error 409: not powered on"})
    r = client.post("/api/v1/oob/machines/nh-su-1-rack-00-tray-00/actions/gpu-reset")
    assert r.status_code == 409 and "not powered on" in r.json()["detail"]


def test_firmware_shapes_from_nico(client, upstream):
    routes, _ = upstream
    routes[("GET", "/v2/org/neocloud/nico/firmware/baseline")] = lambda b: (200, {
        "baseline": {"Compute": {"BMC": "25.10-1", "GPU": "97.10.42"},
                     "PowerShelf": {"PSU": "3.4.0"}}})
    routes[("GET", "/v2/org/neocloud/nico/firmware/drift")] = lambda b: (200, {
        "total": 1, "trays": [{"trayId": "su-1-rack-02-nvsw-08", "type": "NVSwitch",
                               "rackId": "su-1-rack-02", "diffs": [
                                   {"component": "NVOS", "expected": "25.02", "actual": "25.01"}]}]})
    b = client.get("/api/v1/firmware/baseline").json()
    assert b["source"] == "nico" and {"component": "Compute BMC", "tray_type": "Compute",
                                      "baseline": "25.10-1"} in b["components"]
    d = client.get("/api/v1/firmware/drift").json()["drift"]
    assert d == [{"machine": "su-1-rack-02-nvsw-08", "rack": "su-1-rack-02",
                  "tray_type": "NVSwitch", "component": "NVSwitch NVOS", "current": "25.01",
                  "baseline": "25.02", "severity": "warning",
                  "remediation": "PATCH /tray/{id}/firmware"}]


def test_emulator_api_key_sent(client, upstream, monkeypatch):
    routes, calls = upstream
    monkeypatch.setenv("NOCP_EMU_API_KEY", "k-123")
    routes[("POST", "/dps/v1/flex-events")] = lambda b: (201, {"id": "flex-1"})
    client.post("/api/v1/power/flex-events", json={"domain": "pd-x", "reduce_kw": 10})
    hdr = [c[3] for c in calls if c[1] == "/dps/v1/flex-events"][0]
    assert hdr.get("x-api-key") == "k-123"
    monkeypatch.delenv("NOCP_EMU_API_KEY")
    assert dsx.emu_headers() == {}
