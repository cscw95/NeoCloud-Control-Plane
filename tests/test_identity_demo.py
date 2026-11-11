"""F07 demo scenario runs end to end (minimal executor mirroring app/demo/engine.py)."""

import importlib.util
from pathlib import Path

from app import auth
from app.demo.engine import _resolve
from app.demo.janitor import new_uid

_PATH = Path(__file__).resolve().parents[1] / "app" / "demo" / "scenarios" / "f07_identity.py"


class _Res:
    def __init__(self, r):
        self.status = r.status_code
        try:
            self.body = r.json()
        except ValueError:
            self.body = r.text


def _run(client, scenario):
    ctx, out = {"uid": new_uid()}, []    # each run signs up its own org (demo isolation)
    for step in scenario["steps"]:
        spec = _resolve(step["call"], ctx)
        headers = dict(spec.get("headers") or {})
        if spec.get("as", "operator") == "operator":
            headers["Authorization"] = "Bearer " + auth.mint("operator", ttl_s=600)
        res = _Res(client.request(spec["method"], spec["path"], json=spec.get("json"), headers=headers))
        ok, ko, en = step["expect"](res, ctx)
        assert ko and en
        out.append((step["id"], ok, en))
        if ok and step.get("save"):
            step["save"](res, ctx)
    return out


def test_f07_demo_scenario_passes_twice(client):
    spec = importlib.util.spec_from_file_location("f07_identity", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for _ in range(2):                    # own org per run + step 00 withdraws the last one
        results = _run(client, mod.SCENARIO)
        assert all(ok for _, ok, _ in results), results
