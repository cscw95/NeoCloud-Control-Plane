"""F04 — metering pipeline: usage events, idempotency keys, gateway↔engine
reconciliation.

  POST /api/v1/metering/events              (operator) one event → 201 | 200 {duplicate:true}
  POST /api/v1/metering/events/batch        (operator) {events:[...]} → per-event results
  GET  /api/v1/metering/events?tenant_id=&source=&since=&product=&metric=&format=cloudevents
  GET  /api/v1/metering/reconcile?period=YYYY-MM&threshold_pct=
  POST /api/v1/metering/engine/sync         pull AI-Infra GET /dynamo/v1/usage → source "engine"
  POST /api/v1/metering/nico/state          NICo ManagedHostStateChangeMessage → hour events
  GET  /api/v1/metering/usage               (ncp.py) gains token_lines + tokens_by_type

One append-only log (`STORE.metering_events`, a list of records with a key
index — the shape app/billing.py and the F01 gateway read). A second event with a
known idempotency key is dropped (first write wins, Lago transaction_id
semantics). `EventSink` is the Kafka / OpenMeter adapter boundary: every
accepted record is handed to SINK.emit() as a CloudEvents 1.0 envelope.

Event = {idempotency_key, source: nico|gateway|engine|nvke, tenant_id,
product: bmaas|managed-k8s|token-api, metric, quantity, unit, ts,
attrs:{model?, token_type?, instance_id?, rack_id?, request_id?, ...}}.

Upstream alignment (WAVE-v2 "원본 충실도"):
  - token events mirror envoyproxy/ai-gateway @11e8b24b0d2d llmRequestCosts
    (InputToken / CachedInputToken / OutputToken) — token_type input | cached |
    output | reasoning, input excludes cached, output excludes reasoning
    (F01 app/token_api.py _emit_usage, key gw:<x-request-id>:<token_type>).
  - CloudEvents 1.0 (ref/upstream/cloudevents formats/cloudevents.json):
    specversion "1.0", id = idempotency key, source "/neocloud/metering/<source>",
    type "com.neocloud.metering.usage.v1", subject tenant_id, time ts.
  - Lago events (getlago/lago-openapi @7d237882f6ef, URL only, AGPL):
    to_lago_event() gives {transaction_id, external_subscription_id, code,
    timestamp, properties}; transaction_id = idempotency key.
  - NICo state (ref/upstream/dsx-exchange/schemas/asyncapi/nico/nico.yaml,
    channel NICO/v1/machine/{machineId}/state, ManagedHostStateChangeMessage
    {machine_id, timestamp, managed_host_state}): assigned + instance_state
    ready opens an allocation; leaving `assigned` (waitingforcleanup, ready,
    failed, forcedeletion …) closes it and emits instance_hour + gpu_hour.
    The message has no tenant field, so the tenant comes from the NeoCloud
    extension `x-neocloud:{tenant_id, instance_id, gpus, rack_id}` or the
    tray/node record in STORE.
  - engine usage = AI-Infra feat/v2-dynamo GET /dynamo/v1/usage records
    {seq, request_id, tenant_id, model, usage: OpenAI CompletionUsage}.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .store import STORE

SOURCES = ("nico", "gateway", "engine", "nvke")
PRODUCTS = ("bmaas", "managed-k8s", "token-api")
TOKEN_TYPES = ("input", "cached", "output", "reasoning")
REQUIRED = ("idempotency_key", "source", "tenant_id", "product", "metric", "quantity",
            "unit", "ts")
DEFAULT_THRESHOLD_PCT = 1.0
CE_TYPE = "com.neocloud.metering.usage.v1"

router = APIRouter(prefix="/api/v1/metering", tags=["metering"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def _parse(ts: Any) -> datetime:
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), timezone.utc)
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class MeteringError(ValueError):
    pass


# ---------------------------------------------------------------------------
# log + sink
# ---------------------------------------------------------------------------
class EventSink:
    """Kafka / OpenMeter adapter boundary. The in-memory sink keeps the last
    CloudEvents envelopes; a real adapter would produce them to a topic."""

    def __init__(self, size: int = 1000):
        self.buf: list[dict] = []
        self.size = size

    def emit(self, envelope: dict) -> None:
        self.buf.append(envelope)
        del self.buf[:-self.size]

    def clear(self) -> None:
        self.buf.clear()


class _Meter:
    def __init__(self):
        self.lock = threading.RLock()
        self.seq = 0
        self.duplicates = 0
        self.open_allocs: dict[str, dict] = {}     # machine_id -> open NICo allocation
        self.engine_cursor = 0                     # AI-Infra /dynamo/v1/usage `next`
        self._log: Optional[list] = None
        self.index: dict[str, dict] = {}           # idempotency_key -> record

    def log(self) -> list:
        """STORE.metering_events (append-only list) — rebuilds the key index
        when the list was replaced (reseed, tests) or a pre-F04 stub left a
        dict or nothing."""
        t = getattr(STORE, "metering_events", None)
        if not isinstance(t, list):
            t = list(t.values()) if isinstance(t, dict) else []
            setattr(STORE, "metering_events", t)
        if t is not self._log or len(self.index) != len(t):
            self._log = t
            self.index = {}
            for i, e in enumerate(t):
                self.index.setdefault(e.get("idempotency_key") or f"legacy:{i}", e)
        return t

    def reset(self) -> None:
        with self.lock:
            self.log().clear()
            self.index.clear()
            self.seq = 0
            self.duplicates = 0
            self.open_allocs.clear()
            self.engine_cursor = 0
            SINK.clear()


METER = _Meter()
SINK = EventSink()


def reset() -> None:
    METER.reset()


def validate(e: dict) -> dict:
    if not isinstance(e, dict):
        raise MeteringError("event must be an object")
    miss = [k for k in REQUIRED if e.get(k) in (None, "")]
    if miss:
        raise MeteringError(f"missing field(s): {', '.join(miss)}")
    if e["source"] not in SOURCES:
        raise MeteringError(f"source must be one of {SOURCES}")
    if e["product"] not in PRODUCTS:
        raise MeteringError(f"product must be one of {PRODUCTS}")
    try:
        q = float(e["quantity"])
    except (TypeError, ValueError):
        raise MeteringError("quantity must be a number")
    if q < 0:
        raise MeteringError("quantity must be >= 0")
    try:
        ts = _parse(e["ts"])
    except (TypeError, ValueError):
        raise MeteringError("ts must be an ISO 8601 date-time")
    attrs = e.get("attrs") or {}
    if not isinstance(attrs, dict):
        raise MeteringError("attrs must be an object")
    tt = attrs.get("token_type")
    if e["metric"] == "token" and tt not in TOKEN_TYPES:
        raise MeteringError(f"attrs.token_type must be one of {TOKEN_TYPES} for metric token")
    return {"idempotency_key": str(e["idempotency_key"]), "source": e["source"],
            "tenant_id": str(e["tenant_id"]), "product": e["product"],
            "metric": str(e["metric"]), "quantity": int(q) if q == int(q) else q,
            "unit": str(e["unit"]), "ts": ts.isoformat(timespec="seconds"),
            "attrs": dict(attrs)}


def ingest(event: dict) -> tuple[dict, bool]:
    """Append one event. Returns (record, duplicate). Raises MeteringError."""
    rec = validate(event)
    with METER.lock:
        log = METER.log()
        prev = METER.index.get(rec["idempotency_key"])
        if prev is not None:
            METER.duplicates += 1
            return prev, True
        METER.seq += 1
        rec = {**rec, "seq": METER.seq, "received_at": _iso()}
        log.append(rec)
        METER.index[rec["idempotency_key"]] = rec
    SINK.emit(to_cloudevent(rec))
    return rec, False


def events(tenant_id: Optional[str] = None, source: Optional[str] = None,
           since: Optional[str] = None, until: Optional[str] = None,
           product: Optional[str] = None, metric: Optional[str] = None) -> list[dict]:
    t0 = _parse(since) if since else None
    t1 = _parse(until) if until else None
    with METER.lock:
        rows = list(METER.log())
    out = []
    for r in rows:
        if tenant_id and r.get("tenant_id") != tenant_id:
            continue
        if source and r.get("source") != source:
            continue
        if product and r.get("product") != product:
            continue
        if metric and r.get("metric") != metric:
            continue
        if t0 or t1:
            ts = _parse(r["ts"])
            if (t0 and ts < t0) or (t1 and ts >= t1):
                continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# upstream envelopes
# ---------------------------------------------------------------------------
def to_cloudevent(rec: dict) -> dict:
    """CloudEvents 1.0 JSON format envelope for one metering record."""
    return {"specversion": "1.0", "id": rec["idempotency_key"],
            "source": f"/neocloud/metering/{rec['source']}", "type": CE_TYPE,
            "subject": rec["tenant_id"], "time": rec["ts"],
            "datacontenttype": "application/json",
            "data": {k: rec[k] for k in ("tenant_id", "product", "metric", "quantity",
                                          "unit", "attrs")}}


def lago_code(rec: dict) -> str:
    """Billable metric code for a record (input_tokens, gpu_hours, …)."""
    if rec["metric"] == "token":
        return f"{rec['attrs'].get('token_type', 'input')}_tokens"
    m = rec["metric"]
    return m[:-5] + "_hours" if m.endswith("_hour") else m


def to_lago_event(rec: dict, external_subscription_id: Optional[str] = None) -> dict:
    """Lago POST /api/v1/events `event` object (transaction_id = idempotency key)."""
    props = {"quantity": rec["quantity"], "unit": rec["unit"], "product": rec["product"]}
    if rec["metric"] == "token":
        props["tokens"] = rec["quantity"]
    for k in ("model", "token_type", "tier", "instance_id", "rack_id"):
        if rec["attrs"].get(k) is not None:
            props[k] = rec["attrs"][k]
    return {"transaction_id": rec["idempotency_key"],
            "external_subscription_id": external_subscription_id
                                        or f"sub-{rec['tenant_id']}-{rec['product']}",
            "code": lago_code(rec),
            "timestamp": int(_parse(rec["ts"]).timestamp()),
            "properties": props}


# ---------------------------------------------------------------------------
# reconciliation (gateway vs engine)
# ---------------------------------------------------------------------------
def period_bounds(period: str) -> tuple[datetime, datetime]:
    try:
        y, m = (int(x) for x in period.split("-"))
        p0 = datetime(y, m, 1, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise MeteringError("period must be YYYY-MM")
    p1 = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=timezone.utc)
    return p0, p1


def _per_request(rows: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for r in rows:
        parts = r["idempotency_key"].split(":")
        rid = r["attrs"].get("request_id") or (parts[1] if len(parts) >= 3 else r["idempotency_key"])
        d = out.setdefault(rid, {"tokens": 0, "tenant_id": r["tenant_id"],
                                 "model": r["attrs"].get("model")})
        d["tokens"] += r["quantity"]
    return out


def reconcile(period: str, threshold_pct: float = DEFAULT_THRESHOLD_PCT,
              tenant_id: Optional[str] = None) -> dict:
    p0, p1 = period_bounds(period)
    rows = [r for r in events(tenant_id=tenant_id, metric="token",
                              since=p0.isoformat(), until=p1.isoformat())]
    gw = _per_request([r for r in rows if r["source"] == "gateway"])
    en = _per_request([r for r in rows if r["source"] == "engine"])
    g_tot = sum(v["tokens"] for v in gw.values())
    e_tot = sum(v["tokens"] for v in en.values())
    mismatches, details = [], []
    for rid in sorted(set(gw) | set(en)):
        g, e = gw.get(rid, {}).get("tokens", 0), en.get(rid, {}).get("tokens", 0)
        if g != e:
            mismatches.append(rid)
            info = gw.get(rid) or en.get(rid)
            details.append({"request_id": rid, "tenant_id": info["tenant_id"],
                            "model": info["model"], "gateway_tokens": g, "engine_tokens": e,
                            "kind": ("missing_engine" if rid not in en else
                                     "missing_gateway" if rid not in gw else "count")})
    diff_pct = round(abs(g_tot - e_tot) * 100.0 / max(g_tot, e_tot), 4) if max(g_tot, e_tot) else 0.0
    warning = diff_pct > threshold_pct
    by_tenant: dict[str, dict] = {}
    for side, d in (("gateway_tokens", gw), ("engine_tokens", en)):
        for v in d.values():
            bt = by_tenant.setdefault(v["tenant_id"], {"gateway_tokens": 0, "engine_tokens": 0})
            bt[side] += v["tokens"]
    return {"period": period, "tenant_id": tenant_id, "gateway_tokens": g_tot,
            "engine_tokens": e_tot, "diff_pct": diff_pct, "threshold_pct": threshold_pct,
            "status": "warning" if warning else "ok", "warning": warning,
            "requests": {"gateway": len(gw), "engine": len(en)},
            "mismatches": mismatches, "mismatch_details": details[:200],
            "by_tenant": by_tenant, "generated_at": _iso()}


# ---------------------------------------------------------------------------
# engine source (AI-Infra Dynamo emulator usage records)
# ---------------------------------------------------------------------------
def engine_record_events(rec: dict) -> list[dict]:
    """AI-Infra /dynamo/v1/usage record → engine-source token events
    (same split as the gateway so the two sides compare per token type)."""
    u = rec.get("usage") or {}
    prompt = int(u.get("prompt_tokens", 0))
    completion = int(u.get("completion_tokens", 0))
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    reasoning = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0))
    parts = {"input": prompt - cached, "cached": cached,
             "output": completion - reasoning, "reasoning": reasoning}
    rid = rec.get("request_id")
    ts = rec.get("at") or _iso()
    return [{"idempotency_key": f"eng:{rid}:{tt}", "source": "engine",
             "tenant_id": rec.get("tenant_id") or "unknown", "product": "token-api",
             "metric": "token", "quantity": q, "unit": "tokens", "ts": ts,
             "attrs": {"model": rec.get("model"), "token_type": tt, "request_id": rid,
                       "deployment": rec.get("deployment")}}
            for tt, q in parts.items() if q > 0]


def ingest_engine_records(records: list[dict]) -> dict:
    n_new = n_dup = 0
    for rec in records:
        for e in engine_record_events(rec):
            _, dup = ingest(e)
            n_dup += dup
            n_new += not dup
        METER.engine_cursor = max(METER.engine_cursor, int(rec.get("seq") or 0))
    return {"records": len(records), "ingested": n_new, "duplicates": n_dup,
            "cursor": METER.engine_cursor}


def _ai_infra_base() -> str:
    return os.environ.get("NOCP_AI_INFRA_URL") or os.environ.get("AI_INFRA_URL") \
        or "http://127.0.0.1:9100"


def pull_engine(base: Optional[str] = None) -> dict:
    """GET {AI-Infra}/dynamo/v1/usage?since=<cursor> and ingest. Offline-safe."""
    if os.environ.get("NOCP_DSX_OFFLINE") == "1" and base is None:
        return {"records": 0, "ingested": 0, "duplicates": 0, "cursor": METER.engine_cursor,
                "offline": True}
    import httpx
    url = (base or _ai_infra_base()).rstrip("/") + "/dynamo/v1/usage"
    try:
        r = httpx.get(url, params={"since": METER.engine_cursor, "limit": 10000}, timeout=3.0)
        r.raise_for_status()
        body = r.json()
    except Exception as ex:                     # engine unreachable → report, no raise
        return {"records": 0, "ingested": 0, "duplicates": 0, "cursor": METER.engine_cursor,
                "error": f"{type(ex).__name__}: {ex}", "url": url}
    out = ingest_engine_records(body.get("records") or [])
    out["url"] = url
    return out


# ---------------------------------------------------------------------------
# NICo source (DSX Exchange ManagedHostStateChangeMessage)
# ---------------------------------------------------------------------------
def _host_state(msg: dict) -> tuple[str, Optional[str]]:
    mhs = msg.get("managed_host_state") or {}
    inst = mhs.get("instance_state") or {}
    return str(mhs.get("state", "")).lower(), (str(inst.get("state")).lower()
                                               if inst.get("state") else None)


def _resolve_host(machine_id: str, ext: dict) -> dict:
    out = {"tenant_id": ext.get("tenant_id"), "instance_id": ext.get("instance_id"),
           "gpus": ext.get("gpus"), "rack_id": ext.get("rack_id")}
    tray = STORE.trays.get(machine_id) if hasattr(STORE, "trays") else None
    if tray is not None:
        out["gpus"] = out["gpus"] if out["gpus"] is not None else len(getattr(tray, "gpu_ids", []) or [])
        out["rack_id"] = out["rack_id"] or getattr(tray, "rack_id", None)
        out["tenant_id"] = out["tenant_id"] or getattr(tray, "tenant_id", None)
    if out["gpus"] is None:
        out["gpus"] = 4                         # GB200/VR NVL72 compute tray
    out["instance_id"] = out["instance_id"] or f"inst-{machine_id}"
    return out


def nico_state(msg: dict) -> dict:
    """Consume one ManagedHostStateChangeMessage. Opens an allocation on
    assigned/ready, closes it (and emits instance_hour + gpu_hour) when the
    host leaves `assigned`."""
    for k in ("machine_id", "managed_host_state", "timestamp"):
        if not msg.get(k):
            raise MeteringError(f"missing field: {k}")
    mid = str(msg["machine_id"])
    ts = _parse(msg["timestamp"])
    state, inst = _host_state(msg)
    ext = msg.get("x-neocloud") or {}
    with METER.lock:
        open_ = METER.open_allocs.get(mid)
        if state == "assigned" and inst == "ready":
            if open_ is None:
                host = _resolve_host(mid, ext)
                if not host["tenant_id"]:
                    raise MeteringError("tenant unknown: pass x-neocloud.tenant_id")
                METER.open_allocs[mid] = {**host, "machine_id": mid,
                                          "start": ts.isoformat(timespec="seconds")}
                return {"action": "opened", "allocation": METER.open_allocs[mid], "events": []}
            return {"action": "noop", "allocation": open_, "events": []}
        if state == "assigned" or open_ is None:
            return {"action": "noop", "allocation": open_, "events": []}
        METER.open_allocs.pop(mid, None)
    hours = max(0.0, (ts - _parse(open_["start"])).total_seconds() / 3600)
    product = ext.get("product") or "bmaas"
    base = f"nico:{open_['instance_id']}:{open_['start']}"
    attrs = {"instance_id": open_["instance_id"], "rack_id": open_["rack_id"],
             "machine_id": mid, "start": open_["start"],
             "end": ts.isoformat(timespec="seconds"), "end_state": state}
    out = []
    for metric, q, unit in (("instance_hour", hours, "instance-h"),
                            ("gpu_hour", hours * int(open_["gpus"]), "GPU-h")):
        rec, dup = ingest({"idempotency_key": f"{base}:{metric}", "source": "nico",
                           "tenant_id": open_["tenant_id"], "product": product,
                           "metric": metric, "quantity": round(q, 6), "unit": unit,
                           "ts": ts.isoformat(timespec="seconds"), "attrs": attrs})
        out.append({**rec, "duplicate": dup})
    return {"action": "closed", "allocation": open_, "events": out}


# ---------------------------------------------------------------------------
# summaries used by GET /api/v1/metering/usage (ncp.py)
# ---------------------------------------------------------------------------
def usage_extension(tenant_id: Optional[str] = None, period: Optional[str] = None) -> dict:
    """token_lines (tenant × model × token_type, gateway source = billed side)
    and tokens_by_type for the current month."""
    period = period or _now().strftime("%Y-%m")
    p0, p1 = period_bounds(period)
    rows = events(tenant_id=tenant_id, source="gateway", metric="token",
                  since=p0.isoformat(), until=p1.isoformat())
    agg: dict[tuple, dict] = {}
    by_type = {t: 0 for t in TOKEN_TYPES}
    for r in rows:
        tt = r["attrs"].get("token_type")
        k = (r["tenant_id"], r["attrs"].get("model"), tt, r["attrs"].get("tier"))
        a = agg.setdefault(k, {"tenant_id": k[0], "product": "token-api", "model": k[1],
                               "token_type": tt, "tier": k[3], "metric": "token",
                               "quantity": 0, "unit": "tokens", "events": 0})
        a["quantity"] += r["quantity"]
        a["events"] += 1
        by_type[tt] = by_type.get(tt, 0) + r["quantity"]
    return {"token_lines": sorted(agg.values(), key=lambda x: (x["tenant_id"], str(x["model"]),
                                                              TOKEN_TYPES.index(x["token_type"]))),
            "tokens_by_type": by_type, "tokens_total": sum(by_type.values())}


def summary(tenant_id: Optional[str] = None) -> dict:
    rows = events(tenant_id=tenant_id)
    by_source: dict[str, dict] = {}
    for r in rows:
        s = by_source.setdefault(r["source"], {"events": 0, "by_metric": {}})
        s["events"] += 1
        s["by_metric"][r["metric"]] = round(s["by_metric"].get(r["metric"], 0) + r["quantity"], 6)
    return {"events": len(rows), "duplicates_dropped": METER.duplicates,
            "by_source": by_source, "open_allocations": len(METER.open_allocs),
            "engine_cursor": METER.engine_cursor}


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------
def _operator_only(request: Request) -> None:
    who = getattr(request.state, "identity", None)
    if who is not None and getattr(who, "role", None) == "tenant":
        raise HTTPException(403, "operator identity required")


@router.post("/events", status_code=201)
def post_event(request: Request, body: dict = Body(...)):
    _operator_only(request)
    try:
        rec, dup = ingest(body)
    except MeteringError as e:
        raise HTTPException(422, str(e))
    if dup:
        return JSONResponse({"duplicate": True, "event": rec}, status_code=200)
    return rec


@router.post("/events/batch")
def post_events_batch(request: Request, body: dict = Body(...)) -> dict:
    _operator_only(request)
    evs = body.get("events")
    if not isinstance(evs, list) or not evs:
        raise HTTPException(422, "events must be a non-empty array")
    results = []
    for e in evs:
        try:
            rec, dup = ingest(e)
            results.append({"idempotency_key": rec["idempotency_key"], "duplicate": dup,
                            "status": 200 if dup else 201})
        except MeteringError as ex:
            results.append({"idempotency_key": (e or {}).get("idempotency_key")
                            if isinstance(e, dict) else None, "status": 422, "error": str(ex)})
    return {"results": results, "accepted": sum(r["status"] == 201 for r in results),
            "duplicates": sum(r["status"] == 200 for r in results),
            "rejected": sum(r["status"] == 422 for r in results)}


@router.get("/events")
def get_events(tenant_id: Optional[str] = None, source: Optional[str] = None,
               since: Optional[str] = None, product: Optional[str] = None,
               metric: Optional[str] = None, limit: int = Query(500, ge=1, le=10000),
               format: Optional[str] = Query(None, pattern="^(cloudevents|lago)$")) -> dict:
    try:
        rows = events(tenant_id, source, since, None, product, metric)
    except ValueError:
        raise HTTPException(422, "since must be an ISO 8601 date-time")
    total = len(rows)
    rows = rows[-limit:]
    if format == "cloudevents":
        data = [to_cloudevent(r) for r in rows]
    elif format == "lago":
        data = [{"event": to_lago_event(r)} for r in rows]
    else:
        data = rows
    return {"events": data, "count": len(data), "total": total, "summary": summary(tenant_id)}


@router.get("/reconcile")
def get_reconcile(period: Optional[str] = None, tenant_id: Optional[str] = None,
                  threshold_pct: float = Query(DEFAULT_THRESHOLD_PCT, ge=0),
                  sync: bool = False) -> dict:
    synced = pull_engine() if sync else None
    try:
        out = reconcile(period or _now().strftime("%Y-%m"), threshold_pct, tenant_id)
    except MeteringError as e:
        raise HTTPException(422, str(e))
    if synced is not None:
        out["engine_sync"] = synced
    return out


@router.post("/engine/sync")
def post_engine_sync(request: Request, body: Optional[dict] = Body(None)) -> dict:
    """Pull engine usage from AI-Infra, or ingest `{records:[...]}` pushed in the body."""
    _operator_only(request)
    if body and isinstance(body.get("records"), list):
        return ingest_engine_records(body["records"])
    return pull_engine((body or {}).get("base_url"))


@router.post("/nico/state")
def post_nico_state(request: Request, body: dict = Body(...)) -> dict:
    _operator_only(request)
    try:
        return nico_state(body)
    except (MeteringError, ValueError) as e:
        raise HTTPException(422, str(e))
