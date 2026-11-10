"""Demo runner (/api/v1/demo/*) — loader, step/run-all modes, pending features,
SSE stream, operator-only gate, deterministic reset."""

import json

from app import auth, demo
from app.demo.engine import missing_routes

EXPECTED = {"a-signup-to-invoice", "b-token-api", "c-xid-remediation", "d-grid-curtailment",
            "e-leak-workload-move", "f-rack-bringup", "g-nvke-clique", "h-bmaas-baseline"}


def _run(client, sid, mode="all", wait=True):
    r = client.post(f"/api/v1/demo/scenarios/{sid}/run", params={"wait": wait},
                    json={"mode": mode})
    assert r.status_code == 201, r.text
    return r.json()


def test_all_flagship_scenarios_load(client):
    body = client.get("/api/v1/demo/scenarios").json()
    assert body["load_errors"] == {}
    ids = {s["id"] for s in body["scenarios"]}
    assert EXPECTED <= ids
    for s in body["scenarios"]:
        assert s["title_ko"] and s["title_en"] and s["steps"] > 0


def test_scenario_detail_lists_steps_with_narration(client):
    sc = client.get("/api/v1/demo/scenarios/a-signup-to-invoice").json()
    assert sc["steps"] and all(st["narration_ko"] and st["narration_en"] for st in sc["steps"])
    assert client.get("/api/v1/demo/scenarios/nope").status_code == 404


def test_baseline_runs_green(client):
    run = _run(client, "h-bmaas-baseline")
    assert run["state"] == "done"
    bad = [(s["id"], s["assertion"]) for s in run["steps"] if s["state"] != "passed"]
    assert bad == [] and run["verdict"] == "pass"
    st = run["steps"][1]
    assert st["request"]["method"] == "POST" and st["request"]["url"].endswith("/api/v1/orders")
    assert st["request"]["headers"]["Authorization"].endswith("(redacted)")
    assert st["response"]["status"] == 201 and st["duration_ms"] is not None
    assert st["assertion"]["detail_ko"] and st["assertion"]["detail_en"]


def test_every_flagship_scenario_never_fails(client):
    """Unmerged v2 features report `pending`, never `failed` (offline unit build)."""
    for sid in sorted(EXPECTED):
        client.post("/api/v1/demo/reset", json={"blueprints": "gb200-nvl72,vr-nvl72"})
        run = _run(client, sid)
        failed = [(s["id"], s["assertion"]) for s in run["steps"] if s["state"] == "failed"]
        assert failed == [], (sid, failed)
        for s in run["steps"]:
            if s["state"] == "pending":
                assert "pending feature" in s["assertion"]["detail_en"]


def test_step_mode_advances_one_at_a_time(client):
    run = _run(client, "h-bmaas-baseline", mode="step", wait=False)
    assert run["state"] == "ready" and run["cursor"] == 0
    rid = run["id"]
    one = client.post(f"/api/v1/demo/runs/{rid}/step").json()
    assert one["cursor"] == 1 and one["state"] == "paused"
    assert one["steps"][0]["state"] == "passed" and one["steps"][1]["state"] == "waiting"
    rest = client.post(f"/api/v1/demo/runs/{rid}/continue", params={"wait": True}).json()
    assert rest["state"] == "done"
    assert client.post(f"/api/v1/demo/runs/{rid}/step").status_code == 409
    assert client.get(f"/api/v1/demo/runs/{rid}").json()["verdict"] == "pass"
    assert any(r["run_id"] == rid for r in client.get("/api/v1/demo/runs").json())


def test_sse_stream_replays_steps_then_done(client):
    run = _run(client, "h-bmaas-baseline")
    with client.stream("GET", f"/api/v1/demo/runs/{run['id']}/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "run" and kinds[-1] == "done"
    assert kinds.count("step") == 2 * len(run["steps"])     # running + final per step
    assert events[-1]["data"]["verdict"] == "pass"


def test_pending_feature_detection_and_fallback(client):
    assert missing_routes(["GET /api/v1/orders"]) == []
    assert missing_routes(["GET /api/v1/orders/{x}/flow"]) == []
    assert missing_routes(["POST /api/v1/zzz-not-there"]) == ["POST /api/v1/zzz-not-there"]
    sc = {"id": "t-pending", "title_ko": "t", "title_en": "t", "steps": [
        {"id": "1", "title_ko": "a", "title_en": "a", "feature": "F99",
         "requires": ["POST /api/v1/zzz-not-there"],
         "call": {"method": "POST", "path": "/api/v1/zzz-not-there"},
         "fallback": {"method": "POST", "path": "/api/v1/tenants",
                      "json": {"name": "fb-demo", "isolation_tier": "bare_metal_dedicated"}},
         "save": lambda res, ctx: ctx.__setitem__("tenant_id", res.body["id"])},
        {"id": "2", "title_ko": "b", "title_en": "b",
         "call": {"method": "GET", "path": lambda c: f"/api/v1/tenants/{c['tenant_id']}"}},
        {"id": "3", "title_ko": "c", "title_en": "c", "feature": "F98",
         "call": {"method": "GET", "path": lambda c: f"/x/{c['never_set']}"}},
    ]}
    run = demo.RUNNER.create(sc, "all")
    run.run_all()
    v = run.view()
    assert [s["state"] for s in v["steps"]] == ["pending", "passed", "pending"]
    assert "pending feature F99" in v["steps"][0]["assertion"]["detail_en"]
    assert v["steps"][0]["fallback"] is True
    assert v["verdict"] == "pass_with_pending"


def test_operator_only(client):
    tok = auth.mint("tenant", tenant_id="tnt-x", tenant_role="admin")
    r = client.get("/api/v1/demo/scenarios", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
    op = auth.mint("operator")
    r = client.get("/api/v1/demo/scenarios", headers={"Authorization": f"Bearer {op}"})
    assert r.status_code == 200


def test_reset_is_deterministic(client):
    a = client.post("/api/v1/demo/reset", json={"blueprints": "gb200-nvl72,vr-nvl72"}).json()
    run1 = _run(client, "h-bmaas-baseline")
    b = client.post("/api/v1/demo/reset", json={"blueprints": "gb200-nvl72,vr-nvl72"}).json()
    run2 = _run(client, "h-bmaas-baseline")
    assert a["nocp"]["nodes"] == b["nocp"]["nodes"]
    assert client.get("/api/v1/demo/runs").json()[-1]["run_id"] == "demo-run-0001"
    strip = lambda r: [(s["id"], s["state"], s["response"]["summary"] if s["response"] else None)
                       for s in r["steps"]]
    assert strip(run1) == strip(run2)
    assert run1["context"]["order_id"] == run2["context"]["order_id"]
