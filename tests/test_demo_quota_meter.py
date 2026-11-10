"""F02/F04 demo scenario (app/demo/scenarios/f02_f04_quota_metering.py) runs
end to end and gives the same numbers on a re-run."""

import importlib


def _sender(client):
    def send(method, path, body, params, headers):
        r = client.request(method, path, json=body, params=params, headers=headers or None)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, dict(r.headers), data
    return send


def test_scenario_format():
    sc = importlib.import_module("app.demo.scenarios.f02_f04_quota_metering").SCENARIO
    assert {"id", "title_ko", "title_en", "feature", "steps"} <= set(sc)
    for st in sc["steps"]:
        assert {"id", "title_ko", "title_en", "call", "expect"} <= set(st)
        assert isinstance(st["call"], dict)          # runner: callable = internal call


def test_scenario_runs_twice(client):
    mod = importlib.import_module("app.demo.scenarios.f02_f04_quota_metering")
    for _ in range(2):
        lines: list[str] = []
        assert mod.run_local(_sender(client), out=lines.append), "\n".join(lines)
        assert any("curl -si -X GET '$NOCP/api/v1/metering/reconcile" in ln for ln in lines)
    from app import metering
    rec = metering.reconcile(mod.PERIOD, tenant_id=mod.TID)
    assert (rec["gateway_tokens"], rec["engine_tokens"]) == (1000, 820)
