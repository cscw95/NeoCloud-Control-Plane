"""F04 — metering events, idempotency, gateway↔engine reconciliation, NICo
hour events, CloudEvents contract, /metering/usage token fields."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import metering
from tests.contracts.mini_schema import errors

CONTRACTS = Path(__file__).parent / "contracts"
PERIOD = datetime.now(timezone.utc).strftime("%Y-%m")
TS = datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@pytest.fixture()
def m(client):
    metering.reset()
    yield client
    metering.reset()


def _ev(key, source="gateway", q=100, tt="input", rid="req-1", tenant="tnt-a"):
    return {"idempotency_key": key, "source": source, "tenant_id": tenant,
            "product": "token-api", "metric": "token", "quantity": q, "unit": "tokens",
            "ts": TS, "attrs": {"model": "llama-3.3-70b", "token_type": tt, "request_id": rid}}


def test_same_key_twice_is_one_event(m):
    r1 = m.post("/api/v1/metering/events", json=_ev("gw:req-1:input"))
    assert r1.status_code == 201 and r1.json()["seq"] == 1
    r2 = m.post("/api/v1/metering/events", json=_ev("gw:req-1:input", q=999))
    assert r2.status_code == 200 and r2.json()["duplicate"] is True
    assert r2.json()["event"]["quantity"] == 100                  # first write wins
    body = m.get("/api/v1/metering/events").json()
    assert body["total"] == 1 and body["summary"]["duplicates_dropped"] == 1


def test_validation(m):
    bad = _ev("k")
    bad.pop("tenant_id")
    assert m.post("/api/v1/metering/events", json=bad).status_code == 422
    assert m.post("/api/v1/metering/events", json={**_ev("k"), "source": "x"}).status_code == 422
    assert m.post("/api/v1/metering/events", json={**_ev("k"), "quantity": -1}).status_code == 422
    e = _ev("k")
    e["attrs"]["token_type"] = "weird"
    assert m.post("/api/v1/metering/events", json=e).status_code == 422


def test_filters_and_per_source_totals(m):
    for i, src in enumerate(("gateway", "engine", "gateway")):
        metering.ingest(_ev(f"{src}:{i}:input", source=src, q=10 * (i + 1), rid=f"r{i}"))
    metering.ingest({**_ev("nico:i1:gpu_hour", source="nico"), "product": "bmaas",
                     "metric": "gpu_hour", "unit": "GPU-h", "quantity": 4.5,
                     "attrs": {"instance_id": "i1"}})
    gw = m.get("/api/v1/metering/events", params={"source": "gateway"}).json()
    assert gw["total"] == 2
    s = gw["summary"]["by_source"]
    assert s["gateway"]["by_metric"]["token"] == 40 and s["engine"]["by_metric"]["token"] == 20
    assert s["nico"]["by_metric"]["gpu_hour"] == 4.5
    later = m.get("/api/v1/metering/events", params={"since": "2999-01-01T00:00:00Z"}).json()
    assert later["total"] == 0
    assert m.get("/api/v1/metering/events", params={"tenant_id": "tnt-zz"}).json()["total"] == 0


def test_batch(m):
    r = m.post("/api/v1/metering/events/batch",
               json={"events": [_ev("a"), _ev("a"), {**_ev("b"), "source": "bogus"}]})
    assert r.json()["accepted"] == 1 and r.json()["duplicates"] == 1 and r.json()["rejected"] == 1


def test_reconcile_ok_and_warning(m):
    for rid in ("r1", "r2"):
        for src, p in (("gateway", "gw"), ("engine", "eng")):
            metering.ingest(_ev(f"{p}:{rid}:input", source=src, q=100, rid=rid))
            metering.ingest(_ev(f"{p}:{rid}:output", source=src, q=50, tt="output", rid=rid))
    ok = m.get("/api/v1/metering/reconcile", params={"period": PERIOD}).json()
    assert ok["gateway_tokens"] == ok["engine_tokens"] == 300
    assert ok["diff_pct"] == 0.0 and ok["status"] == "ok" and ok["mismatches"] == []
    # engine counted 10 more output tokens on r2, and r3 never reached the engine
    metering.ingest(_ev("eng:r2:reasoning", source="engine", q=10, tt="reasoning", rid="r2"))
    metering.ingest(_ev("gw:r3:input", q=40, rid="r3"))
    w = m.get("/api/v1/metering/reconcile", params={"period": PERIOD}).json()
    assert (w["gateway_tokens"], w["engine_tokens"]) == (340, 310)
    assert w["mismatches"] == ["r2", "r3"] and w["status"] == "warning" and w["warning"]
    kinds = {d["request_id"]: d["kind"] for d in w["mismatch_details"]}
    assert kinds == {"r2": "count", "r3": "missing_engine"}
    assert w["diff_pct"] == round(30 * 100 / 340, 4)
    loose = m.get("/api/v1/metering/reconcile",
                  params={"period": PERIOD, "threshold_pct": 50}).json()
    assert loose["status"] == "ok"
    assert m.get("/api/v1/metering/reconcile", params={"period": "2026-13"}).status_code == 422


def test_engine_records_ingest_idempotent(m):
    rec = {"seq": 7, "source": "engine", "request_id": "req-9", "tenant_id": "tnt-a",
           "model": "llama-3.3-70b", "deployment": "dgd-1", "at": TS,
           "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
                     "prompt_tokens_details": {"cached_tokens": 20},
                     "completion_tokens_details": {"reasoning_tokens": 5}}}
    r = m.post("/api/v1/metering/engine/sync", json={"records": [rec]}).json()
    assert r["ingested"] == 4 and r["cursor"] == 7
    r = m.post("/api/v1/metering/engine/sync", json={"records": [rec]}).json()
    assert r["ingested"] == 0 and r["duplicates"] == 4
    by = {e["attrs"]["token_type"]: e["quantity"]
          for e in metering.events(source="engine")}
    assert by == {"input": 100, "cached": 20, "output": 25, "reasoning": 5}


def test_engine_pull_offline_safe(m):
    r = m.post("/api/v1/metering/engine/sync").json()
    assert r["ingested"] == 0 and r.get("offline") is True


def test_nico_state_open_close_emits_hours(m):
    msg = {"machine_id": "fm100ht-1", "timestamp": "2026-11-01T00:00:00Z",
           "managed_host_state": {"state": "assigned", "instance_state": {"state": "ready"}},
           "x-neocloud": {"tenant_id": "tnt-a", "instance_id": "inst-1", "gpus": 4,
                          "rack_id": "rk-1"}}
    assert m.post("/api/v1/metering/nico/state", json=msg).json()["action"] == "opened"
    end = {"machine_id": "fm100ht-1", "timestamp": "2026-11-01T02:30:00Z",
           "managed_host_state": {"state": "waitingforcleanup",
                                  "cleanup_state": {"state": "hostcleanup"}}}
    r = m.post("/api/v1/metering/nico/state", json=end).json()
    assert r["action"] == "closed"
    q = {e["metric"]: e["quantity"] for e in r["events"]}
    assert q == {"instance_hour": 2.5, "gpu_hour": 10.0}
    assert all(e["source"] == "nico" and e["product"] == "bmaas" for e in r["events"])
    assert m.post("/api/v1/metering/nico/state", json=end).json()["action"] == "noop"
    no_tenant = {**msg, "machine_id": "x-unknown"}
    no_tenant.pop("x-neocloud")
    assert m.post("/api/v1/metering/nico/state", json=no_tenant).status_code == 422


def test_cloudevents_contract(m):
    schema = json.loads((CONTRACTS / "cloudevents.json").read_text())
    metering.ingest(_ev("gw:req-ce:input"))
    data = m.get("/api/v1/metering/events", params={"format": "cloudevents"}).json()["data"]
    assert data and data[0]["specversion"] == "1.0" and data[0]["id"] == "gw:req-ce:input"
    for ce in data + metering.SINK.buf:
        assert errors(ce, schema, schema) == [], ce


def test_lago_event_shape(m):
    metering.ingest(_ev("gw:req-l:cached", tt="cached", q=7))
    ev = m.get("/api/v1/metering/events", params={"format": "lago"}).json()["data"][0]["event"]
    assert set(ev) == {"transaction_id", "external_subscription_id", "code", "timestamp",
                       "properties"}
    assert ev["code"] == "cached_tokens" and ev["transaction_id"] == "gw:req-l:cached"
    assert isinstance(ev["timestamp"], int) and ev["properties"]["tokens"] == 7


def test_usage_gains_token_fields(m):
    metering.ingest(_ev("gw:u1:input", q=100, rid="u1"))
    metering.ingest(_ev("gw:u1:output", q=40, tt="output", rid="u1"))
    metering.ingest(_ev("eng:u1:input", source="engine", q=100, rid="u1"))   # not billed
    body = m.get("/api/v1/metering/usage").json()
    assert {"gpu_hours", "instance_hours", "k8s_node_hours", "lines"} <= set(body)
    assert body["tokens_by_type"] == {"input": 100, "cached": 0, "output": 40, "reasoning": 0}
    assert [(l["token_type"], l["quantity"]) for l in body["token_lines"]] == [("input", 100),
                                                                              ("output", 40)]


def test_tenant_scope_and_operator_only(m):
    metering.ingest(_ev("a1", tenant="tnt-a"))
    metering.ingest(_ev("b1", tenant="tnt-b"))
    hdr = {"X-Tenant-Id": "tnt-a"}
    rows = m.get("/api/v1/metering/events", headers=hdr).json()["data"]
    assert [r["tenant_id"] for r in rows] == ["tnt-a"]
    assert m.post("/api/v1/metering/events", headers=hdr, json=_ev("c1")).status_code == 403


def test_billing_reads_store_shape(m):
    from app.store import STORE
    metering.ingest(_ev("gw:bill:input"))
    assert isinstance(STORE.metering_events, list)
    assert [e["quantity"] for e in STORE.metering_events
            if e["idempotency_key"] == "gw:bill:input"] == [100]
    STORE.metering_events = []                       # replaced list → index rebuilt
    assert metering.ingest(_ev("gw:bill:input"))[1] is False
