"""F05/F06 demo scenario (app/demo/scenarios/f05_billing.py) runs end to end
and is deterministic across reruns (demo:reset)."""

import importlib

from app import billing_webhooks as wh


def _sender(client, lines):
    def send(method, path, body, params):
        r = client.request(method, path, json=body, params=params)
        return r.status_code, dict(r.headers), r.json()
    return send


def test_scenario_format():
    mod = importlib.import_module("app.demo.scenarios.f05_billing")
    sc = mod.SCENARIO
    assert {"id", "title_ko", "title_en", "feature", "steps"} <= set(sc)
    for st in sc["steps"]:
        assert {"id", "title_ko", "title_en", "call"} <= set(st)


def test_scenario_runs_twice_same_total(client):
    mod = importlib.import_module("app.demo.scenarios.f05_billing")
    for _ in range(2):
        lines: list[str] = []
        assert mod.run_local(_sender(client, lines), out=lines.append), "\n".join(lines)
        assert any("curl -s -X POST '$NOCP/lago/api/v1/events/batch'" in ln
                   for ln in lines)
    inv = [i for i in client.get("/api/v1/billing/invoices").json()
           if i["tenant_id"] == "tnt-f05-demo-corp"]
    assert len(inv) == 1 and inv[0]["total"] == mod.TOTAL and inv[0]["status"] == "paid"
    types = [r["type"] for r in wh.deliveries(tenant_id="tnt-f05-demo-corp")]
    assert types.count("invoice.created") == 1        # first run's log was reset
