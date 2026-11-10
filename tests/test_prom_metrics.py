"""F26 NOCP /metrics — SLA · quota · curtailment · recovery; promtool-style lint; OTLP."""
import os
import shutil
import subprocess
import sys
import threading
import types

import pytest

from app import prom, prom_metrics, status_notices
from app.store import STORE


@pytest.fixture(autouse=True)
def _reset():
    status_notices.reset()
    yield
    status_notices.reset()


def _scrape(client, **h):
    r = client.get("/metrics", headers=h)
    assert r.status_code == 200
    return r


def test_text_exposition_lints_clean(client):
    r = _scrape(client)
    assert r.headers["content-type"] == prom.CONTENT_TYPE_TEXT
    assert prom.lint(r.text) == []
    s = prom.parse(r.text)
    for fam in ("nocp_build_info", "nocp_http_requests_total", "nocp_racks",
                "nocp_sla_target_ratio", "nocp_status_component_state", "nocp_status_notices",
                "nocp_token_quota_overrides_active", "nocp_curtailments",
                "nocp_curtailment_requested_watts", "nocp_dr_settled_energy_kwh_total",
                "nocp_remediation_kpi_replace_seconds"):
        assert fam in s, fam
    assert "nocp_metrics_collector_errors_total" not in s


def test_openmetrics_variant(client):
    r = _scrape(client, accept="application/openmetrics-text; version=1.0.0")
    assert r.headers["content-type"] == prom.CONTENT_TYPE_OM
    assert r.text.endswith("# EOF\n")
    assert "# TYPE nocp_http_requests counter" in r.text
    assert prom.lint(r.text, openmetrics=True) == []


def test_sla_and_status_series(client):
    tid = next(iter(STORE.tenants), None) or "tnt-m"
    STORE.tenants.setdefault(tid, None)
    status_notices.create_notice({"kind": "maintenance", "components": ["Storage"],
                                  "message_ko": "점검", "message_en": "maint"})
    s = prom.parse(_scrape(client).text)
    av = {lab["tenant"]: v for lab, v in s["nocp_sla_availability_ratio"]}
    assert 0 <= av[tid] <= 1
    comp = {lab["component"]: lab["state"] for lab, _ in s["nocp_status_component_state"]}
    assert comp["Storage"] == "maintenance"
    nt = {(lab["kind"], lab["state"]): v for lab, v in s["nocp_status_notices"]}
    assert nt[("maintenance", "active")] == 1
    if STORE.tenants.get(tid) is None:
        STORE.tenants.pop(tid, None)


def test_reads_v2_modules_when_present(client, monkeypatch):
    """F21 curtailment, F02 quota overrides, F10 runs, F01 usage — lazy, by attribute."""
    curt = types.SimpleNamespace(CURT=types.SimpleNamespace(
        runs={"curt-1": {"state": "active", "requested_kw": 900, "achieved_kw": 700,
                         "protected": ["tnt-a"]}},
        quota_overrides={("t", "batch"): {"tier": "batch", "factor": 0.3}},
        sla_exceptions={"slx-1": {"kind": "grid_curtailment", "end": None}},
        settlements=[{"kwh": 50.0, "incentive_krw": 65000}]), DR_INCENTIVE_KRW_PER_KWH=1300)
    rem = types.SimpleNamespace(REM=types.SimpleNamespace(runs={
        "r1": {"outcome": "replaced", "mttr_min": 42.0},
        "r2": {"outcome": "recovered", "mttr_min": 6.0}}))
    gw = types.SimpleNamespace(lock=threading.Lock(), usage=[
        {"model": "m1", "service_tier": "batch",
         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 4}}])
    monkeypatch.setitem(sys.modules, "app.curtailment", curt)
    monkeypatch.setitem(sys.modules, "app.remediation", rem)
    monkeypatch.setitem(sys.modules, "app.token_api", types.SimpleNamespace(GW=gw))
    text = _scrape(client).text
    assert prom.lint(text) == []
    s = prom.parse(text)
    assert s["nocp_curtailment_requested_watts"][0][1] == 900_000
    assert s["nocp_curtailment_achieved_watts"][0][1] == 700_000
    assert s["nocp_curtailment_protected_reserved"][0][1] == 1
    q = {lab["tier"]: v for lab, v in s["nocp_token_quota_override_factor"]}
    assert q["batch"] == 0.3 and q["interactive"] == 1
    assert s["nocp_dr_incentive_krw_total"][0][1] == 65000
    out = {lab["outcome"]: v for lab, v in s["nocp_remediation_runs_total"]}
    assert out == {"replaced": 1, "recovered": 1}
    assert s["nocp_remediation_mttr_seconds_max"][0][1] == 42 * 60
    tok = {lab["type"]: v for lab, v in s["nocp_token_tokens_total"]}
    assert tok == {"prompt": 10, "completion": 5, "cached": 4}
    ex = {(lab["kind"], lab["open"]): v for lab, v in s["nocp_sla_exceptions"]}
    assert ex[("grid_curtailment", "true")] == 1


def test_collector_failure_is_isolated(client, monkeypatch):
    def boom(reg):
        raise RuntimeError("x")
    monkeypatch.setattr(prom_metrics, "COLLECTORS", prom_metrics.COLLECTORS + (boom,))
    text = _scrape(client).text
    assert prom.lint(text) == []
    assert 'nocp_metrics_collector_errors_total{collector="boom",error="RuntimeError"} 1' in text


def test_otlp_payload_shape(client):
    p = prom_metrics.otlp_payload(now_ns=1)
    rm = p["resourceMetrics"][0]
    assert {"key": "service.name", "value": {"stringValue": "nocp"}} in rm["resource"]["attributes"]
    ms = {m["name"]: m for m in rm["scopeMetrics"][0]["metrics"]}
    c = ms["nocp_http_requests_total"]["sum"]
    assert c["isMonotonic"] is True and c["aggregationTemporality"] == 2
    assert "gauge" in ms["nocp_build_info"]
    assert client.get("/api/v1/observability/otlp").json()["enabled"] is False


def test_promtool_when_available(client):
    tool = os.environ.get("PROMTOOL") or shutil.which("promtool")
    if not tool:
        pytest.skip("promtool not installed (set PROMTOOL=/path/to/promtool)")
    out = subprocess.run([tool, "check", "metrics"], input=_scrape(client).text,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
