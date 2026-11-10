"""F34 compliance evidence (ISMS-P controls, audit export with hash chain),
F35 capacity scenarios (104 racks · 30 MW via AI-Infra DPS simulate), and
F33 cached-token invoice line. Responses are checked against the NeoCloud
extension schemas in tests/schemas/ (copies of ref/upstream/neocloud-ext/)."""
import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app import capacity, dsx
from app.compliance import verify_chain
from app.shared_services import SHARED
from app.store import STORE

SCHEMAS = Path(__file__).parent / "schemas"
TENANT_H = {"X-Tenant-Id": "tnt-acme"}
# DPS SimulateBody fields (AI-Infra app/dps.py, DPS 공개 문서 기준)
SIMULATE_FIELDS = {"domain", "operating_limit_kw", "workload_mix", "profile",
                   "smoothing", "gpus_per_rack"}


def _schema(name):
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text())


def validate(inst, s, path="$"):
    """Subset of JSON Schema 2020-12: type, enum, required, properties,
    items, pattern, minimum."""
    errs = []
    t = s.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        py = {"object": dict, "array": list, "string": str, "boolean": bool,
              "integer": int, "number": (int, float), "null": type(None)}
        ok = any(isinstance(inst, py[x]) and not (x in ("integer", "number")
                                                  and isinstance(inst, bool))
                 for x in ts)
        if not ok:
            return [f"{path}: {type(inst).__name__} not {t}"]
    if "enum" in s and inst not in s["enum"]:
        errs.append(f"{path}: {inst!r} not in {s['enum']}")
    if isinstance(inst, str) and "pattern" in s and not re.search(s["pattern"], inst):
        errs.append(f"{path}: {inst!r} !~ {s['pattern']}")
    if isinstance(inst, (int, float)) and "minimum" in s and inst < s["minimum"]:
        errs.append(f"{path}: {inst} < {s['minimum']}")
    if isinstance(inst, dict):
        errs += [f"{path}.{k}: required" for k in s.get("required", []) if k not in inst]
        for k, sub in s.get("properties", {}).items():
            if k in inst:
                errs += validate(inst[k], sub, f"{path}.{k}")
    if isinstance(inst, list) and "items" in s:
        for i, x in enumerate(inst):
            errs += validate(x, s["items"], f"{path}[{i}]")
    return errs


def _period():
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ── F34 ───────────────────────────────────────────────────────────────
def test_compliance_controls_schema_and_live_status(client):
    SHARED.reset()
    b = client.get("/api/v1/compliance/controls").json()
    assert validate(b, _schema("compliance-controls")) == []
    ids = {c["id"]: c for c in b["controls"]}
    assert {"2.5.1", "2.9.4", "2.10.2"} <= set(ids)
    assert ids["2.9.4"]["status"] == "partial"           # empty audit trail
    SHARED.create_realm("tnt-acme", "Acme")
    SHARED.audit("ops", "iam.token.issue", "c1")
    b = client.get("/api/v1/compliance/controls").json()
    ids = {c["id"]: c for c in b["controls"]}
    assert ids["2.5.1"]["status"] == "implemented" and ids["2.9.4"]["status"] == "implemented"
    assert b["summary"]["total"] == len(b["controls"])
    only = client.get("/api/v1/compliance/controls", params={"domain": "2.9"}).json()
    assert only["controls"] and all(c["id"].startswith("2.9") for c in only["controls"])
    assert client.get("/api/v1/compliance/controls",
                      params={"status": "bogus"}).status_code == 422
    assert client.get("/api/v1/compliance/controls", headers=TENANT_H).status_code == 403


def test_audit_export_hash_chain_formats(client):
    SHARED.reset()
    for i in range(5):
        SHARED.audit("ops", f"vault.secret.write", f"kv/{i}", tenant_ref="tnt-acme")
    p = _period()
    r = client.get("/api/v1/audit/export", params={"period": p})
    assert r.status_code == 200
    b = r.json()
    assert validate(b, _schema("audit-export")) == []
    assert b["count"] == 5 and verify_chain(b["entries"])
    assert r.headers["x-audit-digest"] == b["digest"] == b["entries"][-1]["hash"]
    assert b["entries"][0]["prev_hash"] == "0" * 64
    tampered = [dict(e) for e in b["entries"]]
    tampered[2]["target"] = "kv/evil"
    assert not verify_chain(tampered)
    # the export is itself audited; the next export sees it
    assert SHARED.audit_log[-1].action == "audit.export"
    r2 = client.get("/api/v1/audit/export", params={"period": p, "format": "csv"})
    rows = list(csv.DictReader(io.StringIO(r2.text)))
    assert r2.headers["content-type"].startswith("text/csv") and len(rows) == 6
    assert rows[0]["hash"] == b["entries"][0]["hash"]
    r3 = client.get("/api/v1/audit/export", params={"period": p, "format": "jsonl",
                                                    "action": "vault."})
    assert len(r3.text.strip().splitlines()) == 5
    today = datetime.now(timezone.utc).date()
    rng = f"{(today - timedelta(days=1)).isoformat()}..{today.isoformat()}"
    assert client.get("/api/v1/audit/export", params={"period": rng}).json()["count"] >= 5
    old = client.get("/api/v1/audit/export", params={"period": "2001-01"}).json()
    assert old["count"] == 0 and old["digest"] == "0" * 64


def test_audit_export_validation_and_rbac(client):
    for bad in ("2026", "2026-13", "2026-11-10..2026-11-01", "x..y"):
        assert client.get("/api/v1/audit/export", params={"period": bad}).status_code == 422
    assert client.get("/api/v1/audit/export",
                      params={"period": _period(), "format": "xml"}).status_code == 422
    assert client.get("/api/v1/audit/export", params={"period": _period()},
                      headers=TENANT_H).status_code == 403


# ── F35 ───────────────────────────────────────────────────────────────
def _fake_dps(seen):
    """MockTransport answering /dps/v1/simulate like AI-Infra app/dps.py for
    a 72-GPU rack (max-p rack peak 227 kW, max-q 187 kW)."""
    peak = {"max-p": 227.0, "training": 222.0, "balanced": 210.0,
            "inference": 200.0, "max-q": 187.0}

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body)
        lim, p = body["operating_limit_kw"], body["profile"]

        def case(b, f):
            n = int(lim // b)
            return {"rack_budget_kw": b, "racks_supported": n, "gpus_supported": n * 72,
                    "expected_draw_kw": round(n * 160.0, 1),
                    "headroom_kw": round(lim - n * 160.0, 1),
                    "tokens_per_s": round(n * 72 * 600 * f),
                    "tokens_per_mw": round(n * 72 * 600 * f / (lim / 1000))}
        st, dy = case(peak[p], 1.0), case(min(peak[p], 175.0), 0.99)
        return httpx.Response(200, json={"inputs": {**body}, "static": st, "dynamic": dy,
                                         "delta": {"racks": dy["racks_supported"]
                                                   - st["racks_supported"]}})
    return httpx.MockTransport(handler)


def test_capacity_104_racks_30mw_via_dps(client, monkeypatch):
    capacity.reset()
    seen = []
    monkeypatch.setenv("NOCP_DSX_OFFLINE", "0")
    dsx.set_transport(_fake_dps(seen))
    try:
        r = client.post("/api/v1/capacity/scenarios",
                        json={"racks": 104, "policy": "compare", "profile": ["max-p", "max-q"]})
    finally:
        dsx.set_transport(None)
    assert r.status_code == 201, r.text
    sc = r.json()
    assert validate(sc, _schema("capacity-scenario")) == []
    assert sc["it_limit_kw"] == 24000.0 and sc["source"] == "dps-simulate"
    assert len(seen) == 2 and all(set(b) <= SIMULATE_FIELDS for b in seen)
    assert seen[0] == {"operating_limit_kw": 24000.0, "profile": "max-p", "gpus_per_rack": 72}
    mp = next(x for x in sc["results"] if x["profile"] == "max-p")
    # static max-p: 24000 // 227 = 105 racks ≥ 104 → fits with 1 spare
    assert mp["static"]["racks_supported"] == 105 and mp["static"]["fits"]
    assert mp["static"]["requested_headroom_kw"] == round(24000 - 104 * 227.0, 1)
    assert mp["dynamic"]["racks_supported"] == 137
    assert sc["fits"] and sc["recommendation"]["profile"] in ("max-p", "max-q")
    assert "104랙" in sc["summary"]["ko"] and "104 racks" in sc["summary"]["en"]
    lst = client.get("/api/v1/capacity/scenarios").json()
    assert lst["count"] == 1 and lst["scenarios"][0]["id"] == sc["id"]
    assert client.get(f"/api/v1/capacity/scenarios/{sc['id']}").json()["id"] == sc["id"]


def test_capacity_does_not_fit_and_local_fallback(client):
    capacity.reset()
    # offline (unit-suite default) → static check on blueprint MaxP 227 kW
    r = client.post("/api/v1/capacity/scenarios",
                    json={"racks": 104, "policy": "static", "site_mw": 25, "pue": 1.3})
    sc = r.json()
    assert r.status_code == 201 and sc["source"] == "local-blueprint"
    st = sc["results"][0]["static"]
    assert st["racks_supported"] == int(25000 / 1.3 // 227) and not st["fits"]
    assert st["shortfall_racks"] == 104 - st["racks_supported"]
    assert sc["fits"] is False and sc["recommendation"] is None
    assert "부족" in sc["summary"]["ko"]
    mq = client.post("/api/v1/capacity/scenarios",
                     json={"racks": 104, "policy": "static", "profile": "max-q"}).json()
    assert mq["results"][0]["static"]["rack_budget_kw"] == 187.0 and mq["fits"]


def test_capacity_validation_and_rbac(client):
    bad = [{"racks": 0}, {"policy": "magic"}, {"profile": "turbo"}, {"site_mw": -1},
           {"pue": 0.5}, {"unknown": 1}, {"profile": []}]
    for b in bad:
        assert client.post("/api/v1/capacity/scenarios", json=b).status_code == 422, b
    assert client.post("/api/v1/capacity/scenarios", json={},
                       headers=TENANT_H).status_code == 403
    assert client.get("/api/v1/capacity/scenarios/cap-9999").status_code == 404
    allp = client.post("/api/v1/capacity/scenarios", json={"profile": "all"}).json()
    assert [r["profile"] for r in allp["results"]] == list(capacity.PROFILES)


# ── F33 cached-token invoice line ─────────────────────────────────────
def test_cached_tokens_bill_as_separate_line(client):
    now = datetime.now(timezone.utc)
    p0 = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
    ts = (p0 + timedelta(days=2)).isoformat()
    tid = client.post("/api/v1/tenants", json={
        "name": "f33-kv", "isolation_tier": "bare_metal_dedicated"}).json()["id"]
    usage = {"prompt_tokens": 2000, "prompt_tokens_details": {"cached_tokens": 1500}}
    n = 100_000                                   # requests
    parts = {"input": (usage["prompt_tokens"] - 1500) * n, "cached": 1500 * n}
    STORE.metering_events = [
        {"idempotency_key": f"gw:f33:{tt}", "source": "gateway", "tenant_id": tid,
         "product": "token-api", "metric": "token", "quantity": q, "ts": ts,
         "attrs": {"model": "llama-3.3-70b-instruct", "token_type": tt,
                   "tier": "interactive"}} for tt, q in parts.items()]
    try:
        r = client.post("/api/v1/billing/invoices:run",
                        json={"period": p0.strftime("%Y-%m"), "tenant_id": tid})
        assert r.status_code == 200, r.text
        inv = client.get("/api/v1/billing/invoices", params={"tenant_id": tid}).json()[0]
        inv = client.get(f"/api/v1/billing/invoices/{inv['id']}").json()
    finally:
        del STORE.metering_events
    lines = {ln.get("token_type"): ln for ln in inv["lines"] if ln["metric"] == "token"}
    assert set(lines) == {"input", "cached"}
    assert lines["cached"]["quantity"] == 150_000_000 and lines["cached"]["unit_price"] == 200
    assert lines["cached"]["amount"] == 30_000 and lines["input"]["amount"] == 40_000
