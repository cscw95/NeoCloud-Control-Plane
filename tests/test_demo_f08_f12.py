"""F08/F12 demo scenarios run with a minimal in-process runner (same step format
as app/demo/engine.py): NOCP calls via TestClient, NICo calls offline (local mode)."""
import importlib

import pytest


class _Res:
    def __init__(self, status, body):
        self.status, self.body = status, body


def _resolve(v, ctx):
    if callable(v):
        return v(ctx)
    if isinstance(v, dict):
        return {k: _resolve(x, ctx) for k, x in v.items()}
    return v


def _run(client, scenario):
    ctx, out = {}, []
    for st in scenario["steps"]:
        call = st["call"]
        try:
            spec = None if callable(call) else _resolve(call, ctx)
        except KeyError:                     # engine: missing context → pending
            out.append((st["id"], None))
            continue
        if callable(call):
            res = _Res(200, call(ctx))
        else:
            if spec.get("service", "nocp") != "nocp":
                res = _Res(0, {"error": "offline"})
            else:
                r = client.request(spec["method"], spec["path"], json=spec.get("json"),
                                   params=spec.get("params"))
                res = _Res(r.status_code, r.json() if r.content else None)
        got = st["expect"](res, ctx)
        ok = got[0] if isinstance(got, tuple) else got
        assert isinstance(got, tuple) and got[1] and got[2], st["id"]
        out.append((st["id"], ok))
    return out


@pytest.mark.parametrize("mod", ["f08_nico_rest", "f12_machine_events"])
def test_scenario_format(mod):
    s = importlib.import_module(f"app.demo.scenarios.{mod}").SCENARIO
    assert {"id", "title_ko", "title_en", "feature", "steps"} <= set(s)
    assert all({"id", "title_ko", "title_en", "call", "expect"} <= set(x) for x in s["steps"])


def test_f12_demo_passes(client):
    from app.demo.scenarios.f12_machine_events import SCENARIO
    res = _run(client, SCENARIO)
    assert all(ok for _, ok in res), res
    assert all(ok for _, ok in _run(client, SCENARIO))        # deterministic re-run


def test_f08_demo_local_mode(client):
    from app.demo.scenarios.f08_nico_rest import SCENARIO
    res = dict(_run(client, SCENARIO))
    assert res["02-order"] is True and res["07-terminate"] is True
    assert res["03-instances"] is None and res["10-bridge-zero"] is None   # NICo offline
    assert res["08-status-history"] is None                               # pending (no ctx)
