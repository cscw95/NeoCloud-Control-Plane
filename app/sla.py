"""F09 — SLA engine: per-tenant monthly availability, exception windows,
service credits (→ F05 billing) and automatic violation tickets.

Downtime sources
  * F10 remediation runs: node-minute records (record_downtime) from the
    NVSentinel event time to recovery/replacement (app.remediation).
  * status-page incidents (app.scenario_api, existing): service-wide minutes
    for the tenant.
Availability basis is per instance: the tenant's monthly availability is its
worst node (service-wide incident minutes + that node's merged downtime),
minus minutes inside SLA exceptions (grid curtailment, maintenance) whose
scope covers the node. Target 99.9 %, credit table in scenario_api.

Credits are written to STORE.sla_credits in the shape billing (F05,
app.billing._apply_credits) consumes: {credit_id, tenant_id, month,
pct_of_monthly, incident_id, status, applied_invoice[, amount_krw]}.
Recalculating a month updates the unapplied credit instead of adding one; if
the credit is already on an invoice, only the difference becomes a new credit.

NVSentinel and CAPI MachineHealthCheck define no SLA/credit API, so the paths
are NeoCloud's (x-neocloud):
  GET  /api/v1/sla/downtime?tenant_id=&period=
  POST /api/v1/sla/exceptions   GET /api/v1/sla/exceptions   PATCH /{id} {end}
  GET  /api/v1/sla/kpi?tenant_id=&period=     (auto replace ≤60 min KPI)
Python seams (F21 grid): create_exception(body) / close_exception(id, end).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .store import STORE

router = APIRouter(prefix="/api/v1/sla", tags=["sla"])

EXCEPTION_KINDS = ("grid_curtailment", "maintenance")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        a = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(422, "period must be 'YYYY-MM'")
    b = a.replace(year=a.year + 1, month=1) if a.month == 12 else a.replace(month=a.month + 1)
    return a, b


class Scope(BaseModel):
    site: Optional[str] = None
    racks: Optional[list[str]] = None
    tenants: Optional[list[str]] = None


class ExceptionCreate(BaseModel):
    kind: str
    start: str
    end: Optional[str] = None
    scope: Scope = Field(default_factory=Scope)
    contract_clause: str = ""


class ExceptionPatch(BaseModel):
    end: str


class _SlaState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        self.downtime: dict[str, dict] = {}
        self.exceptions: dict[str, dict] = {}
        self.tickets: dict[str, str] = {}          # tenant|month → ticket id
        self.seq = 0

    def nid(self, p: str) -> str:
        self.seq += 1
        return f"{p}-{self.seq:04d}"


SLA = _SlaState()


def reset() -> None:
    with SLA.lock:
        SLA.reset()


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------
def record_downtime(*, tenant_id: str, node_id: str, start: str, end: str,
                    cause: str, ref: str, instance_id: Optional[str] = None,
                    rack_id: Optional[str] = None) -> dict:
    with SLA.lock:
        rec = {"id": SLA.nid("dt"), "tenant_id": tenant_id, "node_id": node_id,
               "instance_id": instance_id, "rack_id": rack_id, "start": start,
               "end": end, "cause": cause, "ref": ref, "recorded_at": _now()}
        SLA.downtime[rec["id"]] = rec
        return rec


def create_exception(body: dict) -> dict:
    b = ExceptionCreate(**{k: v for k, v in body.items() if k in ExceptionCreate.model_fields})
    if b.kind not in EXCEPTION_KINDS:
        raise HTTPException(422, f"kind must be one of {EXCEPTION_KINDS}")
    _dt(b.start)
    if b.end:
        _dt(b.end)
    with SLA.lock:
        rec = {"id": SLA.nid("slaex"), **b.model_dump(), "created_at": _now(),
               "refs": {k: v for k, v in body.items() if k not in ExceptionCreate.model_fields}}
        SLA.exceptions[rec["id"]] = rec
        return rec


def close_exception(ex_id: str, end: str) -> dict:
    with SLA.lock:
        rec = SLA.exceptions.get(ex_id)
        if rec is None:
            raise HTTPException(404, f"exception '{ex_id}' not found")
        rec["end"] = end
        return rec


# ---------------------------------------------------------------------------
# computation
# ---------------------------------------------------------------------------
def _clip(a: datetime, b: datetime, lo: datetime, hi: datetime):
    a, b = max(a, lo), min(b, hi)
    return (a, b) if b > a else None


def _merge(iv: list) -> list:
    out = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _covers(ex: dict, tenant_id: str, rack_id: Optional[str]) -> bool:
    sc = ex.get("scope") or {}
    tenants, racks = sc.get("tenants"), sc.get("racks")
    if tenants and tenant_id not in tenants:
        return False
    if racks and (rack_id is None or rack_id not in racks):
        return False
    return True                                  # site-wide when no tenant/rack filter


def _ex_windows(tenant_id: str, rack_id: Optional[str], lo: datetime, hi: datetime) -> list:
    out = []
    for ex in SLA.exceptions.values():
        if not _covers(ex, tenant_id, rack_id):
            continue
        end = _dt(ex["end"]) if ex.get("end") else hi
        w = _clip(_dt(ex["start"]), end, lo, hi)
        if w:
            out.append((w[0], w[1], ex["id"]))
    return out


def _minus(iv: tuple, windows: list) -> tuple[float, list]:
    """Minutes of `iv` outside the exception windows, and the ids that hit."""
    a, b = iv
    total = (b - a).total_seconds() / 60
    hit, cut = [], []
    for wa, wb, wid in windows:
        c = _clip(a, b, wa, wb)
        if c:
            hit.append(wid)
            cut.append(c)
    excluded = sum((y - x).total_seconds() / 60 for x, y in _merge(cut))
    return total - excluded, hit


def downtime_lines(tenant_id: str, month: str, incidents: Optional[list] = None) -> list:
    lo, hi = month_bounds(month)
    lines = []
    with SLA.lock:
        for r in sorted(SLA.downtime.values(), key=lambda r: r["start"]):
            if r["tenant_id"] != tenant_id:
                continue
            iv = _clip(_dt(r["start"]), _dt(r["end"]), lo, hi)
            if not iv:
                continue
            counted, hit = _minus(iv, _ex_windows(tenant_id, r["rack_id"], lo, hi))
            mins = (iv[1] - iv[0]).total_seconds() / 60
            lines.append({"node_id": r["node_id"], "instance_id": r["instance_id"],
                          "rack_id": r["rack_id"], "start": r["start"], "end": r["end"],
                          "minutes": round(mins, 2), "counted_minutes": round(counted, 2),
                          "excluded_minutes": round(mins - counted, 2), "cause": r["cause"],
                          "ref": r["ref"], "exception_id": hit[0] if hit else None})
        for i in incidents or []:
            start = _dt(i["started_at"])
            if i["state"] == "resolved":
                from datetime import timedelta
                end = start + timedelta(minutes=i.get("mttr_min") or 0)
            else:
                end = datetime.now(timezone.utc)
            iv = _clip(start, end, lo, hi)
            if not iv:
                continue
            counted, hit = _minus(iv, _ex_windows(tenant_id, None, lo, hi))
            mins = (iv[1] - iv[0]).total_seconds() / 60
            lines.append({"node_id": None, "instance_id": None, "rack_id": None,
                          "start": iv[0].isoformat(timespec="seconds"),
                          "end": iv[1].isoformat(timespec="seconds"),
                          "minutes": round(mins, 2), "counted_minutes": round(counted, 2),
                          "excluded_minutes": round(mins - counted, 2),
                          "cause": f"incident:{i['id']}", "ref": i["id"],
                          "exception_id": hit[0] if hit else None})
    return lines


def availability(tenant_id: str, month: str, incidents: Optional[list] = None) -> dict:
    """Worst-instance availability for the month (+ fleet view)."""
    lo, hi = month_bounds(month)
    window = (hi - lo).total_seconds() / 60
    lines = downtime_lines(tenant_id, month, incidents)
    service = sum(x["counted_minutes"] for x in lines if x["node_id"] is None)
    per_node: dict[str, float] = {}
    for x in lines:
        if x["node_id"] is not None:
            per_node[x["node_id"]] = per_node.get(x["node_id"], 0.0) + x["counted_minutes"]
    worst_node = max(per_node, key=per_node.get, default=None)
    worst = service + (per_node[worst_node] if worst_node else 0.0)
    return {"lines": lines, "window_min": window, "downtime_min": round(worst, 2),
            "worst_node": worst_node,
            "exceptions_minutes": round(sum(x["excluded_minutes"] for x in lines), 2),
            "availability_pct": round(max(0.0, 100 - worst / window * 100), 4),
            "basis": "per-instance (worst node) + service-wide incidents"}


def kpi(tenant_id: Optional[str], month: str) -> dict:
    from .remediation import REM
    lo, hi = month_bounds(month)
    runs = [r for r in REM.runs.values() if r["state"] == "completed"
            and (not tenant_id or r["target"]["tenant_id"] == tenant_id)
            and lo <= _dt(r["detected_at"]) < hi]
    met = [r for r in runs if r["kpi_met"]]
    return {"kpi": "auto replace within", "target_min": REM.policy.kpi_replace_minutes,
            "runs": len(runs), "met": len(met), "missed": len(runs) - len(met),
            "mttr_avg_min": round(sum(r["mttr_min"] for r in runs) / len(runs), 1) if runs else None,
            "mttr_max_min": max((r["mttr_min"] for r in runs), default=None),
            "missed_runs": [r["id"] for r in runs if not r["kpi_met"]]}


def upsert_credit(tenant_id: str, month: str, pct: int, amount_usd: float,
                  incident_id: Optional[str], refs: list) -> dict:
    """One credit per tenant-month; recalculation never duplicates."""
    ckey = f"{tenant_id}|{month}"
    mine = [c for c in STORE.sla_credits.values() if c.get("key") == ckey
            or c.get("key", "").startswith(ckey + "|delta")]
    base = next((c for c in mine if c.get("key") == ckey), None)
    if base is None:
        base = {"credit_id": f"crd-{STORE.next_seq('sla_credit')}", "key": ckey,
                "tenant_id": tenant_id, "month": month, "incident_id": incident_id,
                "amount_usd": amount_usd, "pct_of_monthly": pct, "status": "calculated",
                "applied_invoice": None, "source": "sla-engine", "refs": refs}
        STORE.sla_credits[base["credit_id"]] = base
        return base
    have = sum(int(c.get("pct_of_monthly") or 0) for c in mine if c.get("status") != "void")
    if pct <= have:
        return base
    if base.get("applied_invoice") is None and base.get("status") == "calculated" \
            and len(mine) == 1:
        base.update(pct_of_monthly=pct, amount_usd=amount_usd, refs=refs,
                    incident_id=incident_id or base.get("incident_id"))
        base.pop("amount_krw", None)
        return base
    delta = {"credit_id": f"crd-{STORE.next_seq('sla_credit')}",
             "key": f"{ckey}|delta{len(mine)}", "tenant_id": tenant_id, "month": month,
             "incident_id": incident_id, "pct_of_monthly": pct - have,
             "amount_usd": round(amount_usd * (pct - have) / pct, 2),
             "status": "calculated", "applied_invoice": None, "source": "sla-engine",
             "supplements": base["credit_id"], "refs": refs}
    STORE.sla_credits[delta["credit_id"]] = delta
    return delta


def open_violation_ticket(tenant_id: str, month: str, avail: float, refs: list) -> Optional[str]:
    key = f"{tenant_id}|{month}"
    if key in SLA.tickets and SLA.tickets[key] in STORE.tickets:
        return SLA.tickets[key]
    from .business import create_ticket
    from .models import TicketCreate
    t = create_ticket(TicketCreate(
        tenant_id=tenant_id, type="sla_violation", severity="high",
        subject=f"SLA 위반 {month}: 가용성 {avail}% < 99.9% / SLA violation",
        body="자동 생성(F09 SLA 엔진). Auto-created by the SLA engine.",
        ref=",".join(refs)[:200] or None, refs=refs))
    SLA.tickets[key] = t.id
    return t.id


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _incidents(tenant_id: str, month: str) -> list:
    return [i for i in STORE.incidents.values()
            if i.get("tenant_id") == tenant_id and i["started_at"][:7] == month]


@router.get("/downtime")
def get_downtime(tenant_id: str, period: Optional[str] = None) -> list:
    period = period or _now()[:7]
    if tenant_id not in STORE.tenants:
        raise HTTPException(404, f"tenant '{tenant_id}' not found")
    return downtime_lines(tenant_id, period, _incidents(tenant_id, period))


@router.post("/exceptions", status_code=201)
def post_exception(body: ExceptionCreate) -> dict:
    return create_exception(body.model_dump())


@router.get("/exceptions")
def list_exceptions() -> list:
    return sorted(SLA.exceptions.values(), key=lambda e: e["id"], reverse=True)


@router.patch("/exceptions/{ex_id}")
def patch_exception(ex_id: str, body: ExceptionPatch) -> dict:
    _dt(body.end)
    return close_exception(ex_id, body.end)


@router.get("/kpi")
def get_kpi(tenant_id: Optional[str] = None, period: Optional[str] = None) -> dict:
    return kpi(tenant_id, period or _now()[:7])
