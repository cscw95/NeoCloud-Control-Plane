"""F21 curtailment policy engine — reserved customers protected, token batch and
spot shrink first, SLA exception + notice, restore and DR settlement."""
import pytest

from app import curtailment


@pytest.fixture(autouse=True)
def _reset():
    curtailment.reset()
    yield
    curtailment.reset()


def _tenant(client, name):
    return client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "vm_multitenant"}).json()["id"]


def _alloc(client, tid, racks):
    r = client.post("/api/v1/allocations", json={
        "tenant_id": tid, "su_id": "su-1", "scope": "rack_set", "rack_ids": racks})
    assert r.status_code == 201, r.text


def _setup(client):
    res, od, sp = (_tenant(client, n) for n in ("rsv-co", "od-co", "spot-co"))
    _alloc(client, res, ["su-1-rack-00", "su-1-rack-01"])
    _alloc(client, od, ["su-1-rack-02", "su-1-rack-03"])
    _alloc(client, sp, ["su-1-rack-04"])
    pol = client.get("/api/v1/power/curtailment-policy").json()
    pol["commitments"] = {res: "reserved", od: "on_demand", sp: "spot"}
    r = client.put("/api/v1/power/curtailment-policy", json=pol)
    assert r.status_code == 200, r.text
    return res, od, sp


def _domain(client):
    return client.get("/api/v1/power/domains").json()["domains"][0]["id"]


def test_policy_default_and_validation(client):
    p = client.get("/api/v1/power/curtailment-policy").json()
    assert p["protect"] == ["reserved"]
    assert [t["name"] for t in sorted(p["tiers"], key=lambda t: t["order"])] == \
        ["token-batch", "spot", "on-demand"]
    bad = {"tiers": [{"name": "x", "match": {"commitment": "reserved"}, "order": 1,
                      "max_reduction_pct": 10}], "protect": ["reserved"]}
    assert client.put("/api/v1/power/curtailment-policy", json=bad).status_code == 422
    bad = {"tiers": [{"name": "x", "match": {}, "order": 1, "max_reduction_pct": 150}]}
    assert client.put("/api/v1/power/curtailment-policy", json=bad).status_code == 422


def test_reserved_protected_batch_and_spot_shrink_first(client):
    res, od, sp = _setup(client)
    dom = _domain(client)
    r = client.post("/api/v1/power/curtailments",
                    json={"requested_kw": 600, "duration_s": 900, "domain": dom})
    assert r.status_code == 201, r.text
    run = r.json()
    acts = run["actions"]
    # reserved racks / tenant untouched
    assert not [a for a in acts if a.get("tenant_id") == res]
    assert not [a for a in acts if a["target"] in ("su-1-rack-00", "su-1-rack-01")]
    assert run["protected"]["tenant_count"] == 1 and run["protected"]["racks"] == 2
    # token batch shrinks via quota override (factor 0.3), interactive untouched
    q = [a for a in acts if a["kind"] == "quota_override"]
    assert q and all(a["target"].endswith("/batch") and a["amount"] == 0.3 for a in q)
    ov = client.get("/api/v1/power/curtailments-quota-overrides").json()["items"]
    assert all(o["tier"] == "batch" for o in ov) and ov
    assert curtailment.quota_factor(od, "batch") == 0.3
    assert curtailment.quota_factor(od, "interactive") == 1.0
    # spot shed before on-demand (tier order)
    caps = [a for a in acts if a["kind"] == "dps_cap"]
    tiers = [a["tier_name"] for a in caps]
    assert tiers and tiers[0] == "spot"
    if "on-demand" in tiers:
        assert tiers.index("on-demand") > max(i for i, t in enumerate(tiers) if t == "spot")
    assert run["achieved_kw"] > 0
    assert run["sla_exception_id"] and run["notice_id"] and run["state"] == "active"
    d = client.get(f"/api/v1/power/curtailments/{run['id']}").json()
    assert d["sla_exception"]["kind"] == "grid_curtailment"
    assert d["notice"]["kind"] == "grid_curtailment" and d["notice"]["message_ko"]
    assert [x["id"] for x in client.get("/api/v1/power/curtailments").json()["items"]] == [run["id"]]


def test_max_reduction_pct_bounds_cuts(client):
    _setup(client)
    run = client.post("/api/v1/power/curtailments",
                      json={"requested_kw": 100000, "duration_s": 60}).json()
    for a in run["actions"]:
        if a["kind"] == "dps_cap":
            pct = 0.5 if a["tier_name"] == "spot" else 0.2
            assert a["amount"] <= (a["amount"] + a["cap_kw"]) * pct + 0.2
    assert run["shortfall_kw"] > 0


def test_end_restores_and_settles(client):
    res, od, sp = _setup(client)
    run = client.post("/api/v1/power/curtailments",
                      json={"requested_kw": 400, "duration_s": 900}).json()
    r = client.post(f"/api/v1/power/curtailments/{run['id']}/end").json()
    assert r["state"] == "restored"
    assert curtailment.quota_factor(od, "batch") == 1.0
    assert client.get("/api/v1/power/curtailments-quota-overrides").json()["items"] == []
    assert set(r["restored"]) >= {a["target"] for a in run["actions"]}
    s = r["settlement"]
    assert s["achieved_kw"] == run["achieved_kw"] and s["kwh"] >= 0
    assert s["incentive_krw"] == int(s["kwh"] * s["incentive_krw_per_kwh"])
    assert client.get("/api/v1/power/curtailments-settlements").json()["items"][0]["id"] == s["id"]
    d = client.get(f"/api/v1/power/curtailments/{run['id']}").json()
    assert d["sla_exception"]["end"] and d["notice"]["state"] == "resolved"
    # idempotent end
    assert client.post(f"/api/v1/power/curtailments/{run['id']}/end").json()["state"] == "restored"


def test_flex_event_shorthand_opens_run(client):
    _setup(client)
    dom = _domain(client)
    r = client.post("/api/v1/power/flex-events",
                    json={"domain": dom, "reduce_kw": 300, "duration_s": 600})
    assert r.status_code == 201, r.text
    cid = r.json()["curtailment_id"]
    run = client.get(f"/api/v1/power/curtailments/{cid}").json()
    assert run["source"] == "flex-event" and run["requested_kw"] == 300


def test_dsx_flex_cloudevent_on_bus_opens_run(client, monkeypatch):
    """A grid.loadtarget.set.v1 pushed through the Exchange opens one run."""
    from app import dsx, events
    lt = {"id": "lts-abc", "site": "pd-x", "feeds": {"pd-x": {}}, "reduce_kw": 250.0,
          "duration_s": 900, "correlation_id": "c0ffee00-0000-4000-8000-000000000001",
          "state": "active"}
    monkeypatch.setattr(dsx, "ai_get", lambda p, params=None, timeout=3.0:
                        {"items": [lt]} if p == "/flex/v1/load-target-sets" else None)
    ce = {"specversion": "1.0.2", "type": "grid.loadtarget.set.v1",
          "correlationid": lt["correlation_id"],
          "data": {"targets": [{"load_constraint": {"value": 1, "unit": "megawatt"}}]}}
    events.publish("health", {"raw": {"Oem": {"Nvidia": {"Data": {"cloudevent": ce}}}}},
                   source="ai-infra.eventservice")
    runs = curtailment.CURT.runs
    assert len(runs) == 1
    run = next(iter(runs.values()))
    assert run["source"] == "dsx-flex" and run["load_target_id"] == "lts-abc"
    # second delivery is deduplicated
    events.publish("grid", {"cloudevent": ce}, source="ai-infra.eventservice")
    assert len(curtailment.CURT.runs) == 1
    # null constraint (revert) restores
    ce2 = {**ce, "data": {"targets": [{"load_constraint": None}]}}
    events.publish("grid", {"cloudevent": ce2}, source="ai-infra.eventservice")
    assert run["state"] == "restored"
