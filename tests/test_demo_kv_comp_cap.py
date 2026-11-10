"""F33/F34/F35 demo scenarios: format check, and F34/F35 run end to end
in-process (F35 with AI-Infra DPS simulate behind a MockTransport)."""
import json

import httpx

from app import capacity, dsx
from app.demo.scenarios import f33_kvbm, f34_compliance, f35_capacity
from app.demo.scenarios._kcc_local import run_local
from tests.test_compliance_capacity import _fake_dps


def _send(client, ai=None):
    def send(method, path, body, params, service="nocp", hdrs=None):
        if service == "ai":
            r = ai.request(method, path, json=body, params=params)
        else:
            r = client.request(method, path, json=body, params=params, headers=hdrs)
        try:
            data = r.json()
        except (ValueError, json.JSONDecodeError):
            data = r.text
        return r.status_code, dict(r.headers), data
    return send


def test_scenario_format():
    for m, feat in ((f33_kvbm, "F33"), (f34_compliance, "F34"), (f35_capacity, "F35")):
        sc = m.SCENARIO
        assert sc["feature"] == feat and sc["title_ko"] and sc["title_en"]
        ids = [s["id"] for s in sc["steps"]]
        assert len(ids) == len(set(ids))
        for s in sc["steps"]:
            assert s["title_ko"] and s["title_en"] and s["call"]
    ai = [s["call"] for s in f33_kvbm.SCENARIO["steps"]
          if isinstance(s["call"], dict) and s["call"]["service"] == "ai"]
    assert {c["path"] for c in ai} >= {"/dynamo/v1/kv/tiers", "/dynamo/v1/generate",
                                       "/dynamo/v1/kv/tiers/config", "/clear_kv_blocks"}


def test_f34_demo_runs(client, monkeypatch):
    monkeypatch.setenv("NOCP_DEV_INSECURE_HEADERS", "1")
    out = []
    assert run_local(f34_compliance.SCENARIO, _send(client), out=out.append), "\n".join(out)
    assert any("해시 체인 재계산 일치" in x for x in out)


def test_f35_demo_runs(client, monkeypatch):
    capacity.reset()
    seen = []
    fake = _fake_dps(seen)
    monkeypatch.setenv("NOCP_DSX_OFFLINE", "0")
    dsx.set_transport(fake)
    try:
        ai = httpx.Client(transport=fake, base_url="http://ai")
        out = []
        ok = run_local(f35_capacity.SCENARIO, _send(client, ai), out=out.append)
    finally:
        dsx.set_transport(None)
    assert ok, "\n".join(out)
    assert len(seen) == 1 + 3 + 1           # raw call + 3 profiles + 25 MW case
