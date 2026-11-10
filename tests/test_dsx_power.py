"""MaxLPS power — local fallback model, AI-Infra DPS proxy, rack policy propagation."""
import json

import httpx
import pytest

from app import dsx


@pytest.fixture()
def ai_mock():
    calls, routes = [], {}

    def handler(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        fn = routes.get((req.method, req.url.path))
        if fn is None:
            return httpx.Response(404, json={"detail": "nope"})
        st, b = fn(body)
        return httpx.Response(st, json=b)
    dsx.set_transport(httpx.MockTransport(handler))
    yield routes, calls
    dsx.set_transport(None)


def test_domains_local_fallback_with_budget(client):
    r = client.get("/api/v1/power/domains").json()
    assert r["source"] == "local-fallback"
    d = r["domains"][0]
    assert d["id"].startswith("pd-") and d["rack_count"] > 0
    assert d["site_budget_kw"] == d["operating_limit_kw"] and d["budget_ok"]
    did = d["id"]
    # PATCH within budget, then above budget → 422
    r = client.patch(f"/api/v1/power/domains/{did}",
                     json={"operating_limit_kw": d["site_budget_kw"] * 0.8, "mode": "dynamic"})
    assert r.status_code == 200 and r.json()["domain"]["mode"] == "dynamic"
    assert client.patch(f"/api/v1/power/domains/{did}",
                        json={"operating_limit_kw": d["site_budget_kw"] * 2}).status_code == 422
    assert client.patch(f"/api/v1/power/domains/{did}", json={"mode": "x"}).status_code == 422
    assert client.get(f"/api/v1/power/domains/{did}").json()["domain"]["mode"] == "dynamic"
    assert client.get("/api/v1/power/domains/pd-nope").status_code == 404


def test_profiles_simulate_flex_telemetry_local(client):
    p = client.get("/api/v1/power/profiles").json()
    assert {x["id"] for x in p["profiles"]} == {"max-p", "max-q", "training", "inference", "balanced"}
    did = client.get("/api/v1/power/domains").json()["domains"][0]["id"]
    s = client.post("/api/v1/power/simulate", json={
        "domain": did, "workload_mix": {"training": 0.5, "inference": 0.5}}).json()
    assert s["dynamic"]["racks_supported"] > s["static"]["racks_supported"]
    assert s["gain_pct"] > 0 and s["dynamic"]["tokens_per_mw"] > s["static"]["tokens_per_mw"]
    f = client.post("/api/v1/power/flex-events", json={
        "domain": did, "reduce_kw": 500, "duration_s": 600})
    assert f.status_code == 201 and f.json()["effective_limit_kw"] < f.json()["operating_limit_kw"]
    assert client.get("/api/v1/events/recent?topic=grid").json()[-1]["data"]["event"] == "flex"
    t = client.get(f"/api/v1/power/telemetry?domain={did}").json()
    assert t["domains"][0]["racks"] and t["domains"][0]["headroom_kw"] > 0


def test_create_domain_local(client):
    r = client.post("/api/v1/power/domains", json={
        "name": "lab", "operating_limit_kw": 300, "racks": ["su-1-rack-00", "su-1-rack-01"]})
    assert r.status_code == 201 and r.json()["domain"]["rack_count"] == 2
    ids = [d["id"] for d in client.get("/api/v1/power/domains").json()["domains"]]
    assert r.json()["domain"]["id"] in ids


def test_proxy_to_dps(client, ai_mock):
    routes, calls = ai_mock
    routes[("GET", "/dps/v1/power-domains")] = lambda b: (200, [
        {"id": "pd-block-1", "operating_limit_kw": 4000, "mode": "dynamic", "racks": []}])
    routes[("POST", "/dps/v1/simulate")] = lambda b: (200, {
        "static": {"racks_supported": 10}, "dynamic": {"racks_supported": 14}})
    r = client.get("/api/v1/power/domains").json()
    assert r["source"] == "ai-infra" and r["domains"][0]["compute_block"] == "block-1"
    s = client.post("/api/v1/power/simulate", json={"domain": "pd-block-1"}).json()
    assert s["source"] == "ai-infra" and s["dynamic"]["racks_supported"] == 14


def test_rack_policy_propagates_to_ai_infra(client, ai_mock):
    routes, calls = ai_mock
    routes[("POST", "/dps/v1/racks/su-2-rack-00/profile")] = lambda b: (200, b)
    routes[("PATCH", "/redfish/v1/Chassis/su-2-rack-00/Controls/PowerLimit")] = lambda b: (200, b)
    r = client.post("/api/v1/racks/su-2-rack-00/power-policy",
                    json={"policy": "dps-dynamic", "profile": "inference"}).json()
    assert r["cap_source"] == "ai-infra" and r["power_cap_kw"] == round(227 * 0.75)
    setpoint = [c for c in calls if c[1].endswith("PowerLimit")][0][2]
    assert setpoint == {"SetPoint": r["power_cap_kw"] * 1000}


def test_rack_policy_validation_and_compat(client):
    r = client.post("/api/v1/racks/su-2-rack-00/power-policy", json={"policy": "maxq"}).json()
    assert r["power_cap_kw"] == 187 and r["cap_source"] == "nocp-local"
    assert client.post("/api/v1/racks/su-2-rack-00/power-policy",
                       json={"policy": "turbo"}).status_code == 422
    assert client.post("/api/v1/racks/su-2-rack-00/power-policy",
                       json={"policy": "maxp", "profile": "x"}).status_code == 422


def test_events_sse_once(client):
    from app import events
    events.publish("leak", {"rack": "r1"})
    r = client.get("/api/v1/events?once=true&topic=leak")
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: leak" in r.text
