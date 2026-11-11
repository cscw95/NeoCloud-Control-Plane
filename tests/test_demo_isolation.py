"""Demo isolation + Day 0 → 2 metadata.

* every scenario passes whatever ran before it: all scenarios twice in a row, then
  twice in reverse order, on one NOCP with no reset in between → 0 failed steps;
* a rerun returns what the previous run of the same scenario held (00-cleanup);
* day / order / showcase_order on every scenario, ?day= and ?showcase=1 filters,
  GET /api/v1/demo/playlist (DEMO-ORDER.md 18 steps, screen-only items with a route);
* CORS allows and exposes X-Request-Id.
"""

from app.demo import janitor, playlist
from app.store import STORE

SHOWCASE = ["f-rack-bringup", "a-signup-to-invoice", "h-bmaas-baseline", "g-nvke-clique",
            "b-token-api", "c-xid-remediation", "e-leak-workload-move", "d-grid-curtailment"]


def _ids(client):
    body = client.get("/api/v1/demo/scenarios").json()
    assert body["load_errors"] == {}
    return [s["id"] for s in body["scenarios"]]


def _run(client, sid):
    r = client.post(f"/api/v1/demo/scenarios/{sid}/run", params={"wait": True},
                    json={"mode": "all"})
    assert r.status_code == 201, r.text
    return r.json()


def _failed(run):
    return {s["id"] for s in run["steps"] if s["state"] == "failed"}


def test_all_scenarios_twice_forward_then_twice_reverse_never_fail(client):
    """Isolation: no scenario fails a step it does not also fail alone on a fresh NOCP.
    (The unit build runs with the DSX emulators offline, so a few scenarios that need
    live AI-Infra/NICo answers fail even alone; on a live stack scripts/demo_all.sh
    run twice forward and twice in reverse shows 0 failed.)"""
    ids = _ids(client)
    assert len(ids) >= 33
    alone = {}
    for sid in ids:
        client.post("/api/v1/demo/reset", json={"scope": "nocp"})   # default 140-rack layout
        alone[sid] = _failed(_run(client, sid))
    client.post("/api/v1/demo/reset", json={"scope": "nocp"})
    failures, cleaned = [], set()
    for n, order in enumerate([ids, ids, ids[::-1], ids[::-1]]):
        for sid in order:
            run = _run(client, sid)
            extra = _failed(run) - alone[sid]
            failures += [(n, sid, s["id"], s["assertion"]["detail_en"])
                         for s in run["steps"] if s["id"] in extra]
            if run["steps"][0]["id"] == "00-cleanup":
                cleaned.add(sid)
    assert failures == []
    janitor.wait()
    # the scenarios that need nothing live are fully green alone
    for sid in SHOWCASE + ["f07-identity", "f29-packaging", "f11-nvke-capi"]:
        assert alone[sid] == set(), (sid, alone[sid])
    # reruns gave back what the previous run held (NVKE racks, BMaaS allocations)
    assert {"g-nvke-clique", "a-signup-to-invoice"} <= cleaned
    held = {}
    for r in STORE.racks.values():
        if r.tenant_id and "demo" in r.tenant_id:
            held[r.tenant_id] = held.get(r.tenant_id, 0) + 1
    # steady state: at most one run's worth of racks per scenario, not one per pass
    assert sum(held.values()) <= 40, held


def test_rerun_uses_new_names(client):
    client.post("/api/v1/demo/reset", json={"scope": "nocp"})
    a = _run(client, "a-signup-to-invoice")
    b = _run(client, "a-signup-to-invoice")
    assert a["verdict"] == b["verdict"] == "pass"
    assert a["context"]["uid"] != b["context"]["uid"]
    assert a["context"]["tenant_id"] != b["context"]["tenant_id"]
    assert b["steps"][0]["id"] == "00-cleanup" and b["steps"][0]["state"] == "passed"


def test_every_scenario_has_day_and_order(client):
    rows = client.get("/api/v1/demo/scenarios").json()["scenarios"]
    for s in rows:
        assert s["day"] in (0, 1, 2) and 1 <= s["order"] <= 18, s["id"]
    keys = [(s["day"], s["order"]) for s in rows]
    assert keys == sorted(keys)                       # Day 0 → 2 presentation order
    unmapped = {s["id"] for s in rows} - set(playlist._META)
    assert unmapped == set()
    by = {s["id"]: s for s in rows}
    assert by["f-rack-bringup"]["day"] == 0 and by["a-signup-to-invoice"]["day"] == 1
    assert by["b-token-api"]["day"] == 2


def test_day_filter_and_showcase(client):
    for d in (0, 1, 2):
        rows = client.get("/api/v1/demo/scenarios", params={"day": d}).json()["scenarios"]
        assert rows and {s["day"] for s in rows} == {d}
    assert client.get("/api/v1/demo/scenarios", params={"day": "day1"}).json()["scenarios"]
    assert client.get("/api/v1/demo/scenarios", params={"day": 5}).status_code == 422
    rows = client.get("/api/v1/demo/scenarios", params={"showcase": 1}).json()["scenarios"]
    assert [s["id"] for s in rows] == SHOWCASE
    assert [s["showcase_order"] for s in rows] == list(range(1, 9))
    assert [s["day"] for s in rows] == [0, 1, 1, 1, 2, 2, 2, 2]
    for s in rows:                                     # no "(a)".."(h)" letter prefixes
        assert not s["title_ko"].startswith("(") and not s["title_en"].startswith("(")


def test_playlist_day0_to_day2(client):
    p = client.get("/api/v1/demo/playlist").json()
    assert [d["day"] for d in p["days"]] == [0, 1, 2]
    items = p["items"]
    assert [i["order"] for i in items] == list(range(1, len(items) + 1))
    assert sorted({i["step"] for i in items}) == list(range(1, 19)) and p["steps"] == 18
    days = [i["day"] for i in items]
    assert days == sorted(days)
    for i in items:
        assert i["kind"] in ("scenario", "screen") and i["route"] and i["console"]
        assert i["title_ko"] and i["title_en"] and i["caption_ko"] and i["caption_en"]
    screens = [i for i in items if i["kind"] == "screen"]
    assert any(i["step"] == 5 and i["route"] == "dsx-power" for i in screens)
    known = set(_ids(client))
    assert all(i["scenario_id"] in known for i in items if i["kind"] == "scenario")
    # showcase scenarios appear in the same order as ?showcase=1
    show = [i["scenario_id"] for i in items if i.get("showcase_order")]
    assert show == SHOWCASE and p["showcase"] == SHOWCASE
    d1 = client.get("/api/v1/demo/playlist", params={"day": 1}).json()
    assert d1["items"] and {i["day"] for i in d1["items"]} == {1}


def test_cors_allows_and_exposes_request_id(client):
    from app.main import CORS_ORIGINS
    origin = CORS_ORIGINS[0] if CORS_ORIGINS and CORS_ORIGINS[0] != "*" else "http://127.0.0.1:8090"
    pre = client.options("/api/v1/demo/scenarios", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "x-request-id"})
    assert pre.status_code == 200
    assert "x-request-id" in pre.headers.get("access-control-allow-headers", "").lower()
    r = client.get("/health", headers={"Origin": origin})
    assert "x-request-id" in r.headers.get("access-control-expose-headers", "").lower()
