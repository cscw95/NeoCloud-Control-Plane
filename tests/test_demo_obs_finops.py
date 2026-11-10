"""F13/F26/F27 demo scenarios: format (WAVE-v2 시연 요건) and, when the v2-demo
runner is present, a full in-process run."""
import importlib

import pytest

NAMES = ("f13_status_notices", "f26_metrics", "f27_finops")


@pytest.mark.parametrize("name", NAMES)
def test_scenario_format(name):
    sc = importlib.import_module(f"app.demo.scenarios.{name}").SCENARIO
    assert {"id", "title_ko", "title_en", "feature", "steps"} <= set(sc)
    ids = [s["id"] for s in sc["steps"]]
    assert len(ids) == len(set(ids)) >= 4
    for s in sc["steps"]:
        assert s["title_ko"] and s["title_en"] and "call" in s


@pytest.mark.parametrize("name", NAMES)
def test_scenario_runs_when_runner_present(client, name):
    engine = pytest.importorskip("app.demo.engine")
    sc = importlib.import_module(f"app.demo.scenarios.{name}").SCENARIO
    run = engine.Run("t", sc, "step")
    while run.step() is not None:
        pass
    bad = [(s["id"], s["assertion"]) for s in run.steps if s["state"] == "failed"]
    assert not bad, bad
