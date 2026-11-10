"""F26 NOCP `GET /metrics` — SLA · quota · curtailment · recovery as Prometheus text.

Exposition per `app/prom.py` (Prometheus text 0.0.4, or OpenMetrics 1.0 when the
scraper sends `Accept: application/openmetrics-text`). Metric names follow the
Prometheus naming guide (base units, `_total` counters, `nocp_` prefix); the
token gateway series reuse the Dynamo frontend label set (`model`) from
`dynamo/lib/runtime/src/metrics/prometheus_names.rs` (ai-dynamo/dynamo@3a524d0d70f9).

Other v2 modules are read lazily when present and skipped when absent:
  F01 app.token_api.GW          (usage → requests/tokens per model·tier)
  F02 app.token_quota           (overrides; else F21 stub CURT.quota_overrides)
  F09 app.sla                   (exceptions)
  F10 app.remediation           (runs, MTTR; else RMA cases + node replace ops)
  F21 app.curtailment.CURT      (runs, settlements; else /power/flex-events)
  F13 app.status_notices        (this wave)

OTLP: with `NOCP_OTLP_ENDPOINT` set, a daemon thread POSTs the same samples as
OTLP/HTTP JSON (`opentelemetry-proto` `ExportMetricsServiceRequest`) to
`<endpoint>/v1/metrics` every `NOCP_OTLP_INTERVAL_S` (default 15 s). Off by default.
"""

from __future__ import annotations

import importlib
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import Response

from . import __version__, metrics, ncp, power, status_notices
from .prom import CONTENT_TYPE_OM, CONTENT_TYPE_TEXT, Registry, wants_openmetrics
from .store import STORE

router = APIRouter(tags=["observability"])

SLA_TARGET_RATIO = 0.999


def _module(name: str):
    try:
        return importlib.import_module(f"app.{name}")
    except ImportError:
        return None


def _holder(mod, attr: str):
    """First object in `mod` exposing dict/list `attr` (e.g. REM.runs, CURT.settlements)."""
    if mod is None:
        return None
    if isinstance(getattr(mod, attr, None), (dict, list)):
        return getattr(mod, attr)
    for v in vars(mod).values():
        x = getattr(v, attr, None) if not isinstance(v, type) else None
        if isinstance(x, (dict, list)):
            return x
    return None


def _values(x) -> list:
    return list(x.values()) if isinstance(x, dict) else list(x or [])


def _sv(v) -> str:
    return getattr(v, "value", v) if v is not None else "unknown"


# ── collectors ────────────────────────────────────────────────────────────
def _platform(reg: Registry) -> None:
    reg.gauge("nocp_build_info", "NOCP build information.").add(1, version=__version__)
    mods, _, _ = metrics.METRICS.snapshot()
    c = reg.counter("nocp_http_requests_total",
                    "HTTP requests handled, by Control-Plane module (cp-*/d-*).")
    for m in mods.values():
        c.add(m["calls"], module=m["id"])
    reg.gauge("nocp_tenants", "Tenants registered.").add(len(STORE.tenants))
    g = reg.gauge("nocp_racks", "Racks by blueprint, hardware state and allocation.")
    counts: dict[tuple, int] = {}
    for r in STORE.racks.values():
        k = (r.blueprint_key, _sv(r.state), "true" if r.tenant_id else "false")
        counts[k] = counts.get(k, 0) + 1
    for (bp, st, al), n in sorted(counts.items()):
        g.add(n, blueprint=bp, state=st, allocated=al)
    o = reg.gauge("nocp_orders", "Service orders by state.")
    oc: dict[str, int] = {}
    for od in STORE.orders.values():
        oc[_sv(od.state)] = oc.get(_sv(od.state), 0) + 1
    for st, n in sorted(oc.items()):
        o.add(n, state=st)


def _sla(reg: Registry) -> None:
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    window_min = max(1.0, (now - start).total_seconds() / 60)
    inc = list(STORE.incidents.values())
    open_ = reg.gauge("nocp_incidents_open", "Open status-page incidents by severity and component.")
    oc: dict[tuple, int] = {}
    tot: dict[str, int] = {}
    for i in inc:
        tot[i.get("severity", "P2")] = tot.get(i.get("severity", "P2"), 0) + 1
        if i.get("state") != "resolved":
            k = (i.get("severity", "P2"), i.get("component", "Compute"))
            oc[k] = oc.get(k, 0) + 1
    for (sev, comp), n in sorted(oc.items()):
        open_.add(n, severity=sev, component=comp)
    c = reg.counter("nocp_incidents_total", "Incidents recorded since start, by severity.")
    for sev, n in sorted(tot.items()):
        c.add(n, severity=sev)
    reg.gauge("nocp_sla_target_ratio", "Monthly availability SLA target.").add(SLA_TARGET_RATIO)
    av = reg.gauge("nocp_sla_availability_ratio",
                   "Month-to-date availability per tenant (incident downtime, same formula as "
                   "/tenants/{id}/sla-report).")
    dt = reg.gauge("nocp_sla_downtime_seconds", "Month-to-date downtime per tenant.")
    for tid in sorted(STORE.tenants):
        mins = 0.0
        for i in inc:
            if i.get("tenant_id") != tid or str(i.get("started_at", ""))[:7] != month:
                continue
            if i.get("state") == "resolved":
                mins += float(i.get("mttr_min") or 0.0)
            else:
                try:
                    mins += max(0.0, (now - datetime.fromisoformat(i["started_at"]))
                                .total_seconds() / 60)
                except (KeyError, ValueError, TypeError):
                    pass
        av.add(round(max(0.0, 1 - mins / window_min), 6), tenant=tid, month=month)
        dt.add(round(mins * 60, 1), tenant=tid, month=month)
    cr = reg.counter("nocp_sla_credits_total", "Service credits calculated, by status.")
    cu = reg.gauge("nocp_sla_credit_usd", "Service credit amount (USD) by status.")
    by: dict[str, list] = {}
    for x in STORE.sla_credits.values():
        by.setdefault(x.get("status", "calculated"), []).append(x)
    for st, xs in sorted(by.items()):
        cr.add(len(xs), status=st)
        cu.add(round(sum(float(x.get("amount_usd") or 0) for x in xs), 2), status=st)
    ex = _values(_holder(_module("sla"), "exceptions"))
    curt = _module("curtailment")
    if not ex and curt is not None:
        ex = _values(_holder(curt, "sla_exceptions"))
    e = reg.gauge("nocp_sla_exceptions", "SLA exception windows (F09) by kind and open/closed.")
    ec: dict[tuple, int] = {}
    for x in ex:
        k = (x.get("kind", "unknown"), "false" if x.get("end") else "true")
        ec[k] = ec.get(k, 0) + 1
    for (kind, op), n in sorted(ec.items()):
        e.add(n, kind=kind, open=op)


def _status(reg: Registry) -> None:
    impacts = status_notices.component_impacts()
    oc: dict[str, list] = {}
    for i in STORE.incidents.values():
        if i.get("state") != "resolved":
            oc.setdefault(i.get("component", "Compute"), []).append(i.get("severity"))
    g = reg.gauge("nocp_status_component_state",
                  "Status-page component state (1 = current state).")
    for comp in status_notices.COMPONENTS:
        sev = oc.get(comp, [])
        st = ("outage" if "P0" in sev else "degraded" if sev
              else impacts.get(comp, "operational"))
        g.add(1, component=comp, state=st)
    n = reg.gauge("nocp_status_notices", "Status-page notices (F13) by kind and state.")
    nc: dict[tuple, int] = {}
    for x in status_notices.list_notices(limit=100000):
        nc[(x["kind"], x["state"])] = nc.get((x["kind"], x["state"]), 0) + 1
    for kind in status_notices.KINDS:
        for st in status_notices.STATES:
            n.add(nc.get((kind, st), 0), kind=kind, state=st)


def _tokens(reg: Registry) -> None:
    gw = getattr(_module("token_api"), "GW", None)
    req = reg.counter("nocp_token_requests_total",
                      "Token API requests completed, by model and service tier (F01).")
    tok = reg.counter("nocp_token_tokens_total",
                      "Tokens served, by model and token type (prompt|completion|cached|reasoning).")
    if gw is not None:
        with gw.lock:
            recs = list(gw.usage)
        rq: dict[tuple, int] = {}
        tk: dict[tuple, int] = {}
        for u in recs:
            m, t = u.get("model") or "unknown", u.get("service_tier") or "interactive"
            rq[(m, t)] = rq.get((m, t), 0) + 1
            us = u.get("usage") or {}
            for typ, key in (("prompt", "prompt_tokens"), ("completion", "completion_tokens"),
                             ("cached", "cached_tokens"), ("reasoning", "reasoning_tokens")):
                if us.get(key):
                    tk[(m, typ)] = tk.get((m, typ), 0) + int(us[key])
        for (m, t), v in sorted(rq.items()):
            req.add(v, model=m, tier=t)
        for (m, typ), v in sorted(tk.items()):
            tok.add(v, model=m, type=typ)
    ov = _values(_holder(_module("token_quota"), "overrides"))
    curt = _module("curtailment")
    if not ov and curt is not None:
        ov = _values(_holder(curt, "quota_overrides"))
    q = reg.gauge("nocp_token_quota_overrides_active",
                  "Active token quota overrides (F02), by tier (e.g. batch cut by F21).")
    qc: dict[str, int] = {"interactive": 0, "batch": 0}
    for x in ov:
        if isinstance(x, dict) and not x.get("cleared"):
            qc[x.get("tier", "batch")] = qc.get(x.get("tier", "batch"), 0) + 1
    for t, v in sorted(qc.items()):
        q.add(v, tier=t)
    f = reg.gauge("nocp_token_quota_override_factor",
                  "Lowest active quota factor per tier (1 = no override).")
    for t in sorted(qc):
        fs = [float(x.get("factor", 1)) for x in ov
              if isinstance(x, dict) and x.get("tier", "batch") == t and not x.get("cleared")]
        f.add(min(fs) if fs else 1.0, tier=t)


def _curtailment(reg: Registry) -> None:
    curt = _module("curtailment")
    runs = _values(_holder(curt, "runs")) if curt else []
    st = reg.gauge("nocp_curtailments", "Grid curtailment runs (F21) by state.")
    sc: dict[str, int] = {}
    req_w = ach_w = 0.0
    for r in runs:
        sc[r.get("state", "unknown")] = sc.get(r.get("state", "unknown"), 0) + 1
        if r.get("state") == "active":
            req_w += float(r.get("requested_kw") or 0) * 1000
            ach_w += float(r.get("achieved_kw") or 0) * 1000
    for s in ("active", "restored"):
        sc.setdefault(s, 0)
    for s, n in sorted(sc.items()):
        st.add(n, state=s)
    reg.gauge("nocp_curtailment_requested_watts",
              "Load reduction requested by active Load Target Sets.").add(req_w)
    reg.gauge("nocp_curtailment_achieved_watts",
              "Load reduction achieved by active curtailment actions.").add(ach_w)
    pr = 0
    for r in runs:
        pr += int(r.get("protected_count") or len(r.get("protected") or []))
    reg.gauge("nocp_curtailment_protected_reserved",
              "Reserved customers/racks protected in curtailment runs.").add(pr)
    from . import finops
    items, src = finops.settlements()
    d = reg.counter("nocp_dr_settled_energy_kwh_total",
                    "Demand-response energy curtailed and settled (kWh).")
    d.add(round(sum(float(s.get("kwh") or 0) for s in items), 3), source=src)
    reg.counter("nocp_dr_incentive_krw_total", "Demand-response incentive settled (KRW).").add(
        sum(int(s.get("incentive_krw") or 0) for s in items), source=src)
    reg.counter("nocp_flex_events_total", "Flex events received on /api/v1/power/flex-events.").add(
        len(power.list_flex_events()))


def _recovery(reg: Registry) -> None:
    rem = _module("remediation")
    runs = _values(_holder(rem, "runs")) if rem else []
    src = "f10-remediation" if rem is not None else "rma+node-ops"
    oc: dict[str, int] = {}
    mttr: list[float] = []
    if runs:
        for r in runs:
            out = r.get("outcome") or r.get("state") or "running"
            oc[out] = oc.get(out, 0) + 1
            if r.get("mttr_min") is not None:
                mttr.append(float(r["mttr_min"]))
    else:
        for op in STORE.node_ops.values():
            out = op.get("state") or op.get("status") or "unknown"
            oc[f"node_{op.get('kind') or op.get('action') or 'op'}_{out}"] = oc.get(
                f"node_{op.get('kind') or op.get('action') or 'op'}_{out}", 0) + 1
    c = reg.counter("nocp_remediation_runs_total", "Auto-recovery runs (F10) by outcome.")
    for k, v in sorted(oc.items()):
        c.add(v, outcome=k, source=src)
    if mttr:
        reg.gauge("nocp_remediation_mttr_seconds_avg",
                  "Average auto-recovery MTTR (seconds) over completed runs.").add(
            round(sum(mttr) / len(mttr) * 60, 1))
        reg.gauge("nocp_remediation_mttr_seconds_max",
                  "Worst auto-recovery MTTR (seconds) over completed runs.").add(
            round(max(mttr) * 60, 1))
    reg.gauge("nocp_remediation_kpi_replace_seconds",
              "Replacement KPI target (60 minutes).").add(3600)
    with ncp.RMA.lock:
        cases = list(ncp.RMA.cases.values())
    rc: dict[str, int] = {}
    for x in cases:
        rc[x.get("state", "open")] = rc.get(x.get("state", "open"), 0) + 1
    r = reg.gauge("nocp_rma_cases", "RMA cases by state.")
    for k, v in sorted(rc.items()):
        r.add(v, state=k)


COLLECTORS = (_platform, _sla, _status, _tokens, _curtailment, _recovery)


def collect() -> Registry:
    reg = Registry()
    for fn in COLLECTORS:
        try:
            fn(reg)
        except Exception as exc:                  # noqa: BLE001 — one bad source never breaks a scrape
            reg.counter("nocp_metrics_collector_errors_total",
                        "Collector failures during this scrape.").add(1, collector=fn.__name__,
                                                                      error=type(exc).__name__)
    return reg


@router.get("/metrics", include_in_schema=True)
def metrics_endpoint(request: Request) -> Response:
    """Prometheus scrape target (text 0.0.4; OpenMetrics 1.0 via Accept)."""
    om = wants_openmetrics(request.headers.get("accept"))
    body = collect().render(openmetrics=om)
    return Response(body, media_type=CONTENT_TYPE_OM if om else CONTENT_TYPE_TEXT)


# ── OTLP/HTTP JSON push (opentelemetry-proto metrics.v1) ──────────────────
def otlp_payload(reg: Optional[Registry] = None, now_ns: Optional[int] = None) -> dict:
    reg = reg or collect()
    ts = str(now_ns or time.time_ns())
    out = []
    for f in reg.families.values():
        pts = [{"attributes": [{"key": k, "value": {"stringValue": str(v)}}
                               for k, v in labels.items()],
                "timeUnixNano": ts, "asDouble": float(v)} for labels, v in f.samples]
        m: dict[str, Any] = {"name": f.name + ("_total" if f.type == "counter" else ""),
                             "description": f.help}
        if f.type == "counter":
            m["sum"] = {"dataPoints": pts, "aggregationTemporality": 2, "isMonotonic": True}
        else:
            m["gauge"] = {"dataPoints": pts}
        out.append(m)
    return {"resourceMetrics": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "nocp"}},
            {"key": "service.version", "value": {"stringValue": __version__}}]},
        "scopeMetrics": [{"scope": {"name": "app.prom_metrics", "version": __version__},
                          "metrics": out}]}]}


_OTLP = {"thread": None, "stop": threading.Event(), "pushes": 0, "last_status": None}


def otlp_push_once(endpoint: Optional[str] = None) -> Optional[int]:
    import httpx
    ep = (endpoint or os.environ.get("NOCP_OTLP_ENDPOINT", "")).rstrip("/")
    if not ep:
        return None
    url = ep if ep.endswith("/v1/metrics") else ep + "/v1/metrics"
    try:
        r = httpx.post(url, json=otlp_payload(), timeout=3.0)
        _OTLP["last_status"] = r.status_code
    except Exception:                             # noqa: BLE001 — best effort
        _OTLP["last_status"] = "unreachable"
    _OTLP["pushes"] += 1
    return _OTLP["last_status"] if isinstance(_OTLP["last_status"], int) else None


def start_otlp() -> bool:
    """Start the push loop when NOCP_OTLP_ENDPOINT is set (idempotent)."""
    if not os.environ.get("NOCP_OTLP_ENDPOINT") or _OTLP["thread"] is not None:
        return False
    interval = float(os.environ.get("NOCP_OTLP_INTERVAL_S", "15"))

    def loop():
        while not _OTLP["stop"].wait(interval):
            otlp_push_once()

    t = threading.Thread(target=loop, name="nocp-otlp", daemon=True)
    _OTLP["thread"] = t
    t.start()
    return True


@router.get("/api/v1/observability/otlp")
def otlp_status() -> dict:
    """OTLP push state (NeoCloud ext): endpoint, pushes, last HTTP status."""
    return {"endpoint": os.environ.get("NOCP_OTLP_ENDPOINT") or None,
            "enabled": _OTLP["thread"] is not None, "pushes": _OTLP["pushes"],
            "last_status": _OTLP["last_status"],
            "interval_s": float(os.environ.get("NOCP_OTLP_INTERVAL_S", "15"))}
