"""F27 FinOps cost (rack / tenant / model) and demand-response settlements."""
import json
import sys
import types
from pathlib import Path

import pytest

from app import auth, finops, power
from app.store import STORE

from _schema import check

C = Path(__file__).parent / "contracts"
COST = json.loads((C / "finops-cost.schema.json").read_text())
DRS = json.loads((C / "dr-settlement.schema.json").read_text())
PERIOD = "2026-10"                      # complete month: 31 days = 744 h


@pytest.fixture()
def alloc(client):
    tid = "tnt-finops"
    racks = sorted(STORE.racks)[:4]
    for r in racks:
        STORE.racks[r].tenant_id = tid
    yield tid, racks
    for r in racks:
        STORE.racks[r].tenant_id = None


def test_cost_by_rack_formula(client, alloc):
    tid, racks = alloc
    body = client.get("/api/v1/finops/cost", params={"period": PERIOD, "by": "rack"}).json()
    assert check(body, COST) == []
    assert body["window"]["hours"] == 744.0 and body["window"]["complete"] is True
    assert body["power_source"] == "model"
    it = {i["key"]: i for i in body["items"]}
    rk = STORE.racks[racks[0]]
    kw = rk.power_cap_kw * power.WORKLOAD_DRAW["training"]
    want_kwh = kw * 744 * finops.PUE
    assert it[rk.id]["energy_kwh"] == pytest.approx(want_kwh, rel=1e-6)
    assert it[rk.id]["energy_cost_krw"] == round(want_kwh * finops.TARIFF_KRW_PER_KWH)
    capex = finops.CAPEX_KRW_PER_RACK[rk.blueprint_key]
    assert it[rk.id]["depreciation_krw"] == round(capex / (5 * 8760) * 744)
    idle = next(i for i in body["items"] if i["tenant_id"] is None)
    assert idle["energy_kwh"] < it[rk.id]["energy_kwh"]         # idle draw < training draw
    t = body["totals"]
    assert t["racks"] == len(STORE.racks)
    assert t["total_cost_krw"] == t["energy_cost_krw"] + t["depreciation_krw"]


def test_cost_by_tenant_keeps_idle_line(client, alloc):
    tid, racks = alloc
    body = client.get("/api/v1/finops/cost", params={"period": PERIOD, "by": "tenant"}).json()
    assert check(body, COST) == []
    keys = [i["key"] for i in body["items"]]
    assert keys == [tid, "unallocated"]
    tenant = body["items"][0]
    assert tenant["racks"] == 4 and tenant["kind"] == "tenant"
    assert body["items"][1]["kind"] == "idle"
    assert sum(i["total_cost_krw"] for i in body["items"]) == pytest.approx(
        body["totals"]["total_cost_krw"], abs=2)


def test_cost_by_model_splits_token_pool(client, alloc, monkeypatch):
    tid, racks = alloc
    empty = client.get("/api/v1/finops/cost", params={"period": PERIOD, "by": "model"}).json()
    assert empty["items"] == [] and "note" in empty["meta"]
    monkeypatch.setenv("NOCP_FINOPS_TOKEN_RACKS", ",".join(racks[:2]))
    gw = types.SimpleNamespace(lock=__import__("threading").Lock(), usage=[
        {"model": "llama-3.3-70b", "at": "2026-10-05T00:00:00+00:00",
         "usage": {"total_tokens": 3_000_000}},
        {"model": "qwen3-32b", "at": "2026-10-06T00:00:00+00:00",
         "usage": {"total_tokens": 1_000_000}},
        {"model": "qwen3-32b", "at": "2026-11-01T00:00:00+00:00",     # other period
         "usage": {"total_tokens": 9_000_000}}])
    monkeypatch.setitem(sys.modules, "app.token_api", types.SimpleNamespace(GW=gw))
    body = client.get("/api/v1/finops/cost", params={"period": PERIOD, "by": "model"}).json()
    assert check(body, COST) == []
    it = {i["key"]: i for i in body["items"]}
    assert it["llama-3.3-70b"]["share"] == 0.75 and it["qwen3-32b"]["tokens"] == 1_000_000
    pool = sum(i["total_cost_krw"] for i in body["items"])
    rows = client.get("/api/v1/finops/cost", params={"period": PERIOD}).json()["items"]
    want = sum(r["total_cost_krw"] for r in rows if r["key"] in racks[:2])
    assert pool == pytest.approx(want, abs=2)
    assert it["qwen3-32b"]["cost_per_1m_tokens_krw"] == pytest.approx(
        it["qwen3-32b"]["total_cost_krw"], rel=1e-6)


def test_period_validation(client):
    assert client.get("/api/v1/finops/cost", params={"period": "2026/10"}).status_code == 422
    assert client.get("/api/v1/finops/cost", params={"period": "2099-01"}).status_code == 422
    cur = client.get("/api/v1/finops/cost").json()
    assert cur["window"]["complete"] is False and cur["window"]["hours"] <= 31 * 24


def test_dr_settlements_from_flex_event_fallback(client, monkeypatch):
    # fallback path for a build without F21 (app.curtailment absent)
    real = finops._module
    monkeypatch.setattr(finops, "_module", lambda n: None if n == "curtailment" else real(n))
    dom = power._local_domains()[0]["id"]
    r = client.post("/api/v1/power/flex-events", json={"domain": dom, "reduce_kw": 600,
                                                      "duration_s": 1800})
    assert r.status_code < 300, r.text
    body = client.get("/api/v1/finops/dr-settlements").json()
    assert check(body, DRS) == []
    assert body["source"] == "flex-events" and body["totals"]["count"] == 1
    s = body["items"][0]
    assert s["kwh"] == 300.0 and s["incentive_krw"] == 300 * finops.DR_INCENTIVE_KRW_PER_KWH
    cost = client.get("/api/v1/finops/cost", params={"by": "tenant"}).json()["totals"]
    assert cost["dr_incentive_krw"] == s["incentive_krw"]
    assert cost["net_cost_krw"] == cost["total_cost_krw"] - s["incentive_krw"]


def test_dr_settlements_from_f21_run_after_flex_event(client):
    # integrated build: the flex event opens an F21 run; ending it settles
    dom = power._local_domains()[0]["id"]
    r = client.post("/api/v1/power/flex-events", json={"domain": dom, "reduce_kw": 600,
                                                      "duration_s": 1800})
    assert r.status_code < 300, r.text
    runs = client.get("/api/v1/power/curtailments").json()["items"]
    assert runs, "flex event should open a curtailment run"
    client.post(f"/api/v1/power/curtailments/{runs[0]['id']}/end")
    body = client.get("/api/v1/finops/dr-settlements").json()
    assert check(body, DRS) == []
    assert body["source"] == "f21-curtailment" and body["totals"]["count"] == 1


def test_dr_settlements_prefer_f21_engine(client, monkeypatch):
    rec = {"id": "drs-0001", "curtailment_id": "curt-0001", "load_target_id": "lts-1",
           "site": "gasan", "requested_kw": 900.0, "achieved_kw": 820.0,
           "start": "2026-10-10T01:00:00+00:00", "end": "2026-10-10T02:00:00+00:00",
           "hours": 1.0, "kwh": 820.0, "incentive_krw_per_kwh": 1300, "incentive_krw": 1066000}
    fake = types.SimpleNamespace(CURT=types.SimpleNamespace(settlements=[rec]),
                                 DR_INCENTIVE_KRW_PER_KWH=1300)
    monkeypatch.setitem(sys.modules, "app.curtailment", fake)
    body = client.get("/api/v1/finops/dr-settlements", params={"period": "2026-10"}).json()
    assert check(body, DRS) == []
    assert body["source"] == "f21-curtailment" and body["totals"]["incentive_krw"] == 1066000
    assert client.get("/api/v1/finops/dr-settlements",
                      params={"period": "2026-09"}).json()["totals"]["count"] == 0


def test_operator_only(client, monkeypatch):
    monkeypatch.setenv("NOCP_DEV_INSECURE_HEADERS", "0")
    tok = auth.mint("tenant", tenant_id="tnt-x")
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/v1/finops/cost", headers=h).status_code == 403
    assert client.get("/api/v1/finops/dr-settlements", headers=h).status_code == 403
