"""F10 auto-recovery chain + F09 SLA engine (contract + NVSentinel shapes)."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from app import ncp, nico_fake, remediation, sla
from app.store import STORE

REF = Path(__file__).resolve().parents[2] / "ref" / "upstream" / "nvsentinel"
if not REF.exists():        # worktree under wt/<name>/: ref lives at the neocloud root
    REF = Path(__file__).resolve().parents[4] / "ref" / "upstream" / "nvsentinel"


@pytest.fixture(autouse=True)
def _fresh():
    remediation.reset()
    sla.reset()
    ncp.RMA.reset()
    yield


def _bmaas(client, name="rem-co"):
    tid = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"}).json()["id"]
    r = client.post("/api/v1/orders", json={"tenant_id": tid, "kind": "new",
                                            "blueprint_key": "vr-nvl72", "racks": 1})
    assert r.status_code == 201, r.text
    trays = sorted(t.id for t in STORE.trays.values() if t.tenant_id == tid)
    assert trays
    return tid, trays


def _nvke(client, name="rem-k8s", count=2):
    tid = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"}).json()["id"]
    c = client.post("/api/v1/nvke/clusters", json={
        "name": "train", "tenant_id": tid, "k8s_version": "1.33",
        "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": count}]}).json()
    return tid, c


def _auto(client, **kw):
    body = {"mode": "auto", **kw}
    r = client.put("/api/v1/remediation/policy", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _names(run):
    return [s["name"] for s in run["steps"]]


# ---------------------------------------------------------------------------
# F10
# ---------------------------------------------------------------------------
def test_policy_default_monitor_and_validation(client):
    p = client.get("/api/v1/remediation/policy").json()
    assert p == {"mode": "monitor", "actions": {"cordon": True, "drain": True,
                 "gpu_reset": True, "replace": True, "rma": True},
                 "max_parallel_per_rack": 1, "kpi_replace_minutes": 60}
    assert client.put("/api/v1/remediation/policy", json={"mode": "x"}).status_code == 422


def test_monitor_mode_takes_no_action(client):
    tid, trays = _bmaas(client)
    r = client.post("/api/v1/remediation/inject",
                    json={"machine_id": trays[0], "xid": 48}).json()
    assert r["run"] is None
    assert r["health_event"]["healthEvent"]["processingStrategy"] == "STORE_ONLY"
    assert r["health_event"]["healthEventStatus"]["nodeQuarantined"] == ""
    assert client.get("/api/v1/remediation/runs").json() == []
    assert not nico_fake.FAKE_NICO.get_host(f"nh-{trays[0]}").cordoned
    assert STORE.trays[trays[0]].tenant_id == tid


def test_gpu_reset_success_path_no_replacement(client):
    tid, trays = _bmaas(client)
    _auto(client)
    r = client.post("/api/v1/remediation/inject",
                    json={"machine_id": trays[0], "xid": 48, "gpu": 3}).json()
    run = r["run"]
    assert run["outcome"] == "recovered" and run["state"] == "completed"
    assert _names(run) == ["detect", "quarantine", "drain", "gpu_reset", "validate", "uncordon"]
    assert run["remediation_cr"]["kind"] == "GPUReset"
    assert run["mttr_min"] == 13.0 and run["kpi_met"] is True
    assert run["replacement"] is None and run["rma_id"] is None
    assert STORE.trays[trays[0]].tenant_id == tid          # no swap
    st = r["health_event"]["healthEventStatus"]
    assert st["nodeQuarantined"] == "UnQuarantined" and st["faultRemediated"] is True
    assert st["userPodsEvictionStatus"]["status"] == "Succeeded"
    # downtime fed to the SLA engine
    dt = client.get(f"/api/v1/sla/downtime?tenant_id={tid}&period={run['detected_at'][:7]}").json()
    assert [(x["node_id"], x["minutes"]) for x in dt] == [(f"nh-{trays[0]}", 13.0)]
    # events on topic health
    kinds = [e["data"]["type"] for e in client.get("/api/v1/events/recent?topic=health").json()
             if e["data"].get("run_id") == run["id"]]
    assert kinds[0] == "RemediationStarted" and kinds[-1] == "RemediationCompleted"
    assert "RemediationStepDone" in kinds


def test_reset_failure_replaces_bmaas_tray_and_opens_rma(client):
    tid, trays = _bmaas(client)
    _auto(client)
    run = client.post("/api/v1/remediation/inject", json={
        "machine_id": trays[1], "xid": 79, "persistent": True}).json()["run"]
    assert run["recommended_action"] == "RESTART_BM"
    assert run["remediation_cr"]["kind"] == "RebootNode"
    assert run["outcome"] == "replaced"
    assert _names(run)[:7] == ["detect", "quarantine", "drain", "gpu_reset", "validate",
                               "replace", "rma_open"]
    assert _names(run)[-3:] == ["repair", "burn_in", "pool_return"]
    assert run["steps"][3]["state"] == "Failed" and run["steps"][4]["state"] == "Failed"
    assert run["mttr_min"] == 22.0 and run["kpi_met"] is True
    new = run["replacement"]["new_host"].removeprefix("nh-")
    assert STORE.trays[new].tenant_id == tid
    assert STORE.trays[trays[1]].tenant_id is None             # faulty one back to pool
    rma = client.get(f"/api/v1/rma/{run['rma_id']}").json()
    assert rma["stage"] == "pool_return" and rma["source"] == "nvsentinel"
    assert client.get(f"/api/v1/remediation/runs/{run['id']}").json()["id"] == run["id"]
    assert [r["id"] for r in client.get(
        f"/api/v1/remediation/runs?tenant_id={tid}&state=completed").json()] == [run["id"]]


def test_nvke_node_replaced_via_f11(client):
    tid, c = _nvke(client)
    _auto(client)
    node = c["nodepools"][0]["nodes"][0]
    run = client.post("/api/v1/remediation/inject", json={
        "machine_id": node["nico_host_id"], "xid": 94, "persistent": True}).json()["run"]
    assert run["target"]["product"] == "nvke" and run["target"]["cluster_id"] == c["id"]
    assert run["outcome"] == "replaced" and run["replacement"]["kind"] == "nvke-node"
    nodes = client.get(f"/api/v1/nvke/clusters/{c['id']}").json()["nodepools"][0]["nodes"]
    hosts = {n["nico_host_id"] for n in nodes}
    assert node["nico_host_id"] not in hosts and run["replacement"]["new_host"] in hosts
    assert run["mttr_min"] <= 60


def test_manual_run_and_actions_off(client):
    tid, trays = _bmaas(client)
    remediation.REM.faults[f"nh-{trays[0]}"] = {"xid": 79, "gpu": 0, "persistent": True}
    client.put("/api/v1/remediation/policy", json={
        "mode": "monitor", "actions": {"replace": False, "rma": False}})
    r = client.post("/api/v1/remediation/runs", json={"machine_id": trays[0],
                                                      "reason": "operator test"})
    assert r.status_code == 201
    run = r.json()
    assert run["trigger"]["kind"] == "manual" and run["outcome"] == "failed"
    assert run["kpi_met"] is False and run["rma_id"] is None
    assert client.post("/api/v1/remediation/runs",
                       json={"machine_id": "nope"}).status_code == 404


def test_same_rack_runs_are_serialized(client):
    tid, trays = _bmaas(client)
    _auto(client)
    a = client.post("/api/v1/remediation/inject",
                    json={"machine_id": trays[0], "xid": 48}).json()["run"]
    b = client.post("/api/v1/remediation/inject",
                    json={"machine_id": trays[1], "xid": 48}).json()["run"]
    q = [s for s in b["steps"] if s["name"] == "queued"]
    assert q and q[0]["waited_for"] == [a["id"]]
    quarantine_b = next(s["at"] for s in b["steps"] if s["name"] == "quarantine")
    assert quarantine_b > a["completed_at"]
    # another rack is not held
    other = next(t.id for t in sorted(STORE.trays.values(), key=lambda t: t.id)
                 if t.rack_id != STORE.trays[trays[0]].rack_id and t.tenant_id is None)
    c = remediation.start_run(other, "x")
    assert "queued" not in _names(c)


def test_health_events_platform_connector(client):
    tid, trays = _bmaas(client)
    _auto(client)
    r = client.post("/api/v1/remediation/health-events", json={"version": 1, "events": [{
        "agent": "syslog-health-monitor", "componentClass": "GPU", "checkName": "SysLogsXIDError",
        "isFatal": True, "errorCode": ["48"], "nodeName": f"nh-{trays[2]}",
        "recommendedAction": "COMPONENT_RESET"}]})
    assert r.status_code == 202 and r.json()["events"][0]["run_id"]
    # non-fatal → stored only (quarantine rule does not match)
    r = client.post("/api/v1/remediation/health-events", json={"events": [{
        "agent": "gpu-health-monitor", "componentClass": "GPU", "isFatal": False,
        "errorCode": ["13"], "nodeName": f"nh-{trays[2]}"}]})
    assert r.json()["events"][0]["run_id"] is None
    assert client.post("/api/v1/remediation/health-events", json={}).status_code == 422
    assert len(client.get("/api/v1/remediation/health-events").json()) == 2


# ---------------------------------------------------------------------------
# upstream contract checks (NVSentinel proto + janitor CRDs)
# ---------------------------------------------------------------------------
def _proto_fields(message):
    src = (REF / "data-models/protobufs/health_event.proto").read_text()
    body = re.search(r"message %s \{(.*?)\n\}" % message, src, re.S).group(1)
    return {m.group(1) for m in re.finditer(r"^\s+[\w.<>, ]+?\s+(\w+)\s*=\s*\d+;", body, re.M)}


def _validate(obj, schema, path="$"):
    t = schema.get("type")
    if t == "object":
        assert isinstance(obj, dict), path
        for k in schema.get("required", []):
            assert k in obj, f"{path}.{k} required"
        props = schema.get("properties", {})
        for k, v in obj.items():
            if k in props:
                _validate(v, props[k], f"{path}.{k}")
    elif t == "array":
        assert isinstance(obj, list), path
        for i, v in enumerate(obj):
            _validate(v, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string":
        assert isinstance(obj, str), path
        if "enum" in schema:
            assert obj in schema["enum"], f"{path}={obj}"
    elif t == "boolean":
        assert isinstance(obj, bool), path
    elif t == "integer":
        assert isinstance(obj, int), path


def test_contract_health_event_matches_proto(client):
    tid, trays = _bmaas(client)
    _auto(client)
    r = client.post("/api/v1/remediation/inject",
                    json={"machine_id": trays[0], "xid": 48}).json()
    ev, st = r["health_event"]["healthEvent"], r["health_event"]["healthEventStatus"]
    assert set(ev) == _proto_fields("HealthEvent")
    assert set(st) == _proto_fields("HealthEventStatus")
    assert set(r["health_event"]) == _proto_fields("HealthEventWithStatus")
    enum = re.search(r"enum RecommendedAction \{(.*?)\}",
                     (REF / "data-models/protobufs/health_event.proto").read_text(), re.S).group(1)
    assert ev["recommendedAction"] in re.findall(r"(\w+)\s*=", enum)
    statuses = {"NotStarted", "InProgress", "Failed", "Succeeded", "AlreadyDrained",
                "UnQuarantined", "Quarantined", "AlreadyQuarantined", "Cancelled"}
    assert st["nodeQuarantined"] in statuses
    assert st["userPodsEvictionStatus"]["status"] in statuses


@pytest.mark.parametrize("xid,kind", [(48, "gpuresets"), (79, "rebootnodes")])
def test_contract_janitor_cr_matches_crd(client, xid, kind):
    tid, trays = _bmaas(client)
    _auto(client)
    run = client.post("/api/v1/remediation/inject",
                      json={"machine_id": trays[0], "xid": xid}).json()["run"]
    crd = yaml.safe_load((REF / "distros/kubernetes/nvsentinel/charts/janitor/crds"
                          / f"janitor.dgxc.nvidia.com_{kind}.yaml").read_text())
    v = crd["spec"]["versions"][0]
    cr = run["remediation_cr"]
    assert cr["apiVersion"] == f"{crd['spec']['group']}/{v['name']}"
    assert cr["kind"] == crd["spec"]["names"]["kind"]
    _validate(cr, v["schema"]["openAPIV3Schema"])


# ---------------------------------------------------------------------------
# F09
# ---------------------------------------------------------------------------
def _month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _report(client, tid, month=None):
    r = client.get(f"/api/v1/tenants/{tid}/sla-report?month={month or _month()}")
    assert r.status_code == 200, r.text
    return r.json()


def test_sla_downtime_credit_ticket_and_no_duplicates(client):
    tid, trays = _bmaas(client, "sla-f09")
    _auto(client)
    run = client.post("/api/v1/remediation/inject", json={
        "machine_id": trays[0], "xid": 79, "persistent": True}).json()["run"]
    rep = _report(client, tid)
    assert rep["downtime_min"] == run["mttr_min"] == 22.0
    assert rep["violated"] is False and rep["credits"] == []      # 22 min < 43.8 min budget
    assert rep["kpi"]["runs"] == 1 and rep["kpi"]["met"] == 1
    # a second, longer outage on the same node (replacement disabled → failed run)
    start = datetime.now(timezone.utc).replace(day=2, hour=1, minute=0, second=0)
    sla.record_downtime(tenant_id=tid, node_id=f"nh-{trays[0]}", start=start.isoformat(),
                        end=(start + timedelta(minutes=40)).isoformat(),
                        cause="xid:79", ref="rem-x", rack_id=STORE.trays[trays[0]].rack_id)
    rep = _report(client, tid)
    assert rep["violated"] is True and rep["availability_pct"] < 99.9
    assert len(rep["credits"]) == 1 and rep["credits"][0]["pct_of_monthly"] == 10
    assert rep["credits"][0]["applied_invoice"] is None
    tk = rep["violation_ticket_id"]
    t = next(x for x in client.get(f"/api/v1/tickets?tenant_id={tid}&type=sla_violation").json())
    assert t["id"] == tk and run["id"] in t["refs"]
    # recalculation: same credit, same ticket
    again = _report(client, tid)
    assert [c["credit_id"] for c in again["credits"]] == [rep["credits"][0]["credit_id"]]
    assert again["violation_ticket_id"] == tk
    assert len(client.get(f"/api/v1/tickets?tenant_id={tid}&type=sla_violation").json()) == 1
    # worse month before invoicing → credit upgraded in place
    sla.record_downtime(tenant_id=tid, node_id=f"nh-{trays[0]}", start=start.isoformat(),
                        end=(start + timedelta(minutes=600)).isoformat(),
                        cause="xid:79", ref="rem-y")
    up = _report(client, tid)
    assert [c["credit_id"] for c in up["credits"]] == [rep["credits"][0]["credit_id"]]
    assert up["credits"][0]["pct_of_monthly"] == 25
    # once applied to an invoice, only the difference is added
    STORE.sla_credits[up["credits"][0]["credit_id"]].update(status="applied",
                                                           applied_invoice="inv-1")
    sla.record_downtime(tenant_id=tid, node_id=f"nh-{trays[0]}", start=start.isoformat(),
                        end=(start + timedelta(minutes=3000)).isoformat(),
                        cause="xid:79", ref="rem-z")
    fin = _report(client, tid)
    assert [c["pct_of_monthly"] for c in fin["credits"]] == [25, 25]
    assert fin["credits"][1]["supplements"] == up["credits"][0]["credit_id"]
    assert _report(client, tid)["credits"] == fin["credits"]


def test_sla_exception_window_excluded(client):
    tid, trays = _bmaas(client, "sla-grid")
    rack = STORE.trays[trays[0]].rack_id
    start = datetime.now(timezone.utc).replace(day=3, hour=2, minute=0, second=0, microsecond=0)
    sla.record_downtime(tenant_id=tid, node_id=f"nh-{trays[0]}", start=start.isoformat(),
                        end=(start + timedelta(minutes=120)).isoformat(),
                        cause="grid", ref="t", rack_id=rack)
    assert _report(client, tid)["violated"] is True
    r = client.post("/api/v1/sla/exceptions", json={
        "kind": "grid_curtailment", "start": start.isoformat(),
        "end": (start + timedelta(minutes=100)).isoformat(),
        "scope": {"racks": [rack]}, "contract_clause": "SLA §예외 DSX Flex"})
    assert r.status_code == 201
    ex = r.json()
    rep = _report(client, tid)
    assert rep["exceptions_minutes"] == 100.0 and rep["downtime_min"] == 20.0
    assert rep["violated"] is False
    dt = client.get(f"/api/v1/sla/downtime?tenant_id={tid}&period={_month()}").json()
    assert dt[0]["exception_id"] == ex["id"] and dt[0]["counted_minutes"] == 20.0
    # scope that does not cover the tenant → no effect
    assert client.post("/api/v1/sla/exceptions", json={
        "kind": "maintenance", "start": start.isoformat(), "scope": {"tenants": ["other"]}}
    ).status_code == 201
    assert _report(client, tid)["downtime_min"] == 20.0
    assert client.post("/api/v1/sla/exceptions", json={
        "kind": "bogus", "start": start.isoformat()}).status_code == 422
    assert len(client.get("/api/v1/sla/exceptions").json()) == 2


def test_sla_python_seams_for_grid(client):
    ex = sla.create_exception({"kind": "grid_curtailment", "start": "2026-11-10T01:00:00+00:00",
                               "end": None, "scope": {"site": "site-1"},
                               "contract_clause": "x", "curtailment_id": "curt-1"})
    assert ex["refs"] == {"curtailment_id": "curt-1"} and ex["end"] is None
    assert sla.close_exception(ex["id"], "2026-11-10T02:00:00+00:00")["end"].startswith("2026")
    r = client.patch(f"/api/v1/sla/exceptions/{ex['id']}",
                     json={"end": "2026-11-10T03:00:00+00:00"})
    assert r.status_code == 200 and r.json()["end"].endswith("03:00:00+00:00")
    assert client.patch("/api/v1/sla/exceptions/nope",
                        json={"end": "2026-11-10T03:00:00+00:00"}).status_code == 404


def test_credit_shape_for_billing(client):
    """F05 app.billing._apply_credits reads these keys."""
    tid, trays = _bmaas(client, "sla-bill")
    start = datetime.now(timezone.utc).replace(day=4, hour=0, minute=0, second=0)
    sla.record_downtime(tenant_id=tid, node_id=f"nh-{trays[0]}", start=start.isoformat(),
                        end=(start + timedelta(minutes=90)).isoformat(), cause="x", ref="r")
    c = _report(client, tid)["credits"][0]
    for k in ("credit_id", "tenant_id", "month", "pct_of_monthly", "incident_id",
              "status", "applied_invoice"):
        assert k in c
    assert c["status"] == "calculated" and c["month"] == _month()
    assert client.get("/api/v1/sla/kpi").json()["target_min"] == 60


def test_demo_scenario_runs_twice_deterministically(client):
    from app.demo.scenarios.f10_f09_recovery_sla import SCENARIO

    class Res:
        def __init__(self, r):
            self.status, self.headers = r.status_code, dict(r.headers)
            self.body = r.json() if "json" in r.headers.get("content-type", "") else r.text

    for _ in range(2):
        ctx, out = {}, []
        for st in SCENARIO["steps"]:
            spec = st["call"](ctx) if callable(st["call"]) else st["call"]
            res = Res(client.request(spec["method"], spec["path"], json=spec.get("json"),
                                     params=spec.get("params")))
            exp = st["expect"]
            ok = (res.status == exp["status"]) if isinstance(exp, dict) else exp(res, ctx)[0]
            assert ok, (st["id"], res.status, res.body if not callable(exp) else exp(res, ctx))
            if st.get("save"):
                st["save"](res, ctx)
            out.append(exp(res, ctx)[2] if callable(exp) else "")
        assert len(out) == len(SCENARIO["steps"])


_PY = {"string": str, "number": (int, float), "integer": int, "boolean": bool,
       "object": dict, "array": list, "null": type(None)}


def _jv(obj, schema, path="$"):
    """Minimal JSON Schema check (type lists, enum, required, properties, items)."""
    types = schema.get("type")
    if types:
        types = types if isinstance(types, list) else [types]
        assert any(isinstance(obj, _PY[t]) and not (t in ("number", "integer")
                   and isinstance(obj, bool)) for t in types), f"{path}: {obj!r} not {types}"
    if "enum" in schema:
        assert obj in schema["enum"], f"{path}={obj!r}"
    if isinstance(obj, dict):
        for k in schema.get("required", []):
            assert k in obj, f"{path}.{k} required"
        for k, sub in schema.get("properties", {}).items():
            if k in obj:
                _jv(obj[k], sub, f"{path}.{k}")
    if isinstance(obj, list) and "items" in schema:
        for i, v in enumerate(obj):
            _jv(v, schema["items"], f"{path}[{i}]")


def test_contract_neocloud_ext_schemas(client):
    import json
    ext = REF.parent / "neocloud-ext"
    run_s = json.loads((ext / "remediation-run.schema.json").read_text())
    sla_s = json.loads((ext / "sla.schema.json").read_text())["$defs"]
    tid, trays = _bmaas(client, "sla-schema")
    _auto(client)
    for body in ({"machine_id": trays[0], "xid": 48},
                 {"machine_id": trays[1], "xid": 79, "persistent": True}):
        _jv(client.post("/api/v1/remediation/inject", json=body).json()["run"], run_s)
    start = datetime.now(timezone.utc).replace(day=5, hour=0, minute=0, second=0)
    sla.record_downtime(tenant_id=tid, node_id=f"nh-{trays[2]}", start=start.isoformat(),
                        end=(start + timedelta(minutes=90)).isoformat(), cause="x", ref="r")
    _jv(client.post("/api/v1/sla/exceptions", json={
        "kind": "maintenance", "start": start.isoformat(),
        "end": (start + timedelta(minutes=5)).isoformat(),
        "contract_clause": "c"}).json(), sla_s["exception"])
    for d in client.get(f"/api/v1/sla/downtime?tenant_id={tid}&period={_month()}").json():
        _jv(d, sla_s["downtime"])
    for c in _report(client, tid)["credits"]:
        _jv(c, sla_s["credit"])
