"""F21/F22 demo scenarios run end to end through a minimal runner (same step
format as app/demo/engine.py: call = http spec or callable(ctx), expect =
callable(res, ctx) → (ok, detail_ko, detail_en))."""
import importlib
from types import SimpleNamespace

import pytest


def _v(x, ctx):
    return x(ctx) if callable(x) else x


def _run(client, mod):
    sc = importlib.import_module(f"app.demo.scenarios.{mod}").SCENARIO
    assert {"id", "title_ko", "title_en", "feature", "steps"} <= set(sc)
    ctx, out = {"run_id": "t"}, []
    for st in sc["steps"]:
        call = st["call"]
        if callable(call):
            res = SimpleNamespace(status=200, body=call(ctx))
        else:
            assert call["service"] == "nocp"
            r = client.request(call["method"], _v(call["path"], ctx),
                               json=_v(call.get("json"), ctx), params=_v(call.get("params"), ctx))
            res = SimpleNamespace(status=r.status_code, body=r.json())
        ok, ko, en = st["expect"](res, ctx)
        assert ok, (st["id"], ko, res.body)
        assert ko and en
        out.append(st["id"])
    return out


@pytest.mark.parametrize("mod", ["f21_curtailment", "f22_leak_response"])
def test_scenario_runs(client, mod):
    assert _run(client, mod)


def test_f20_scenario_shape():
    sc = importlib.import_module("app.demo.scenarios.f20_dsx_flex").SCENARIO
    assert sc["feature"] == "F20" and len(sc["steps"]) == 8
    assert all(callable(s["call"]) or s["call"]["service"] == "ai" for s in sc["steps"])
