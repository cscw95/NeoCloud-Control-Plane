"""F06 reserved (committed) contracts — IMPROVEMENT-PLAN-v2 §F06.

A contract reserves capacity for a tenant over a term (e.g. 4 NVL72 racks for
12 months) at a committed unit price. Orders may reference a contract via
`contract_id`: rack count, blueprint and the placement constraint (same IB
zone) then come from the contract, and billing starts at
`max(start_date, delivered_at)`. After the term ends the contract is `expired`
and the billing engine (billing.py) prices further usage at the on-demand rate.

IB zone: each site (AIFactory) has its own independent IB fabric (fabric.py),
so "same IB zone" == every rack inside one site — the same rule M4 placement
(lifecycle.select_racks) already enforces. The pre-check here turns a
placement miss into a 422 instead of a rejected order.

Unit price semantics: KRW per rack-hour for bmaas / managed-k8s; for token-api
the contract only carries the monthly commitment (`min_monthly_amount`) and
token usage is priced from the price book (commitment "reserved" items first).
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .store import STORE
from .trace import emit

router = APIRouter(prefix="/api/v1", tags=["contracts"])

PRODUCTS = ("bmaas", "managed-k8s", "token-api")
RACK_PRODUCTS = ("bmaas", "managed-k8s")
STATUSES = ("draft", "active", "expired", "terminated")
DEFAULT_BLUEPRINT = "vr-nvl72"


class ContractCreate(BaseModel):
    tenant_id: str
    product: str
    commitment: str = "reserved"
    racks: Optional[int] = None
    blueprint_key: Optional[str] = None
    term_months: int = Field(..., ge=1, le=120)
    start_date: str                               # YYYY-MM-DD
    unit_price: float = Field(..., ge=0)
    min_monthly_amount: Optional[float] = Field(None, ge=0)
    placement: Optional[dict] = None              # {same_ib_zone: true}
    sla_tier: str = "standard"


class ContractPatch(BaseModel):
    racks: Optional[int] = None
    blueprint_key: Optional[str] = None
    term_months: Optional[int] = Field(None, ge=1, le=120)
    start_date: Optional[str] = None
    unit_price: Optional[float] = Field(None, ge=0)
    min_monthly_amount: Optional[float] = Field(None, ge=0)
    placement: Optional[dict] = None
    sla_tier: Optional[str] = None
    status: Optional[str] = None                  # only "terminated"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(422, f"start_date must be YYYY-MM-DD (got '{s}')")


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def term_window(c: dict) -> tuple[datetime, datetime]:
    """[start, end) of the contract term in UTC; a termination cuts it short."""
    start = _dt(date.fromisoformat(c["start_date"]))
    end = _dt(date.fromisoformat(c["end_date"]))
    if c.get("terminated_at"):
        end = min(end, datetime.fromisoformat(c["terminated_at"]))
    return start, end


def refresh_status(c: dict, now: Optional[datetime] = None) -> dict:
    """Lazy expiry: an active contract past its end date becomes expired."""
    now = now or _now()
    if c["status"] == "active" and now >= term_window(c)[1]:
        c["status"] = "expired"
        c["expired_at"] = term_window(c)[1].isoformat()
    return c


def is_reserved_at(c: dict, at: datetime) -> bool:
    """True when `at` falls inside an activated contract's term."""
    if c["status"] == "draft" or not c.get("activated_at"):
        return False
    start, end = term_window(c)
    return start <= at < end


def contracts_of_tenant(tenant_id: str) -> list[dict]:
    return [refresh_status(c) for c in STORE.contracts.values()
            if c["tenant_id"] == tenant_id]


def _caller_tenant(request: Request) -> Optional[str]:
    ident = getattr(request.state, "identity", None)
    if ident is None or getattr(ident, "is_operator", False):
        return None
    return getattr(ident, "tenant_id", None)


def _require_operator(request: Request) -> None:
    if _caller_tenant(request) is not None:
        raise HTTPException(403, "operator only")


def _get(contract_id: str, request: Optional[Request] = None) -> dict:
    c = STORE.contracts.get(contract_id)
    if c is None:
        raise HTTPException(404, f"contract '{contract_id}' not found")
    if request is not None:
        tid = _caller_tenant(request)
        if tid is not None and tid != c["tenant_id"]:
            raise HTTPException(403, "tenant scope violation")
    return refresh_status(c)


def _validate(c: dict) -> None:
    if c["product"] not in PRODUCTS:
        raise HTTPException(422, f"product must be one of {PRODUCTS}")
    if c["commitment"] != "reserved":
        raise HTTPException(422, "commitment must be 'reserved'")
    if c["product"] in RACK_PRODUCTS:
        if not c.get("racks") or c["racks"] < 1:
            raise HTTPException(422, f"racks >= 1 required for {c['product']}")
        from . import spec
        if c["blueprint_key"] not in spec.BLUEPRINTS:
            raise HTTPException(422, f"unknown blueprint_key '{c['blueprint_key']}'")
    elif c.get("min_monthly_amount") is None:
        raise HTTPException(422, "token-api contract needs min_monthly_amount")


def _public(c: dict) -> dict:
    out = dict(c)
    out["orders"] = [o for o in (order_billing_start(oid)
                                 for oid in c.get("order_ids", [])) if o]
    return out


def order_billing_start(order_id: str) -> Optional[dict]:
    """{order_id, delivered_at, billing_start} — billing start for a contract
    order is max(contract start_date, delivered_at)."""
    o = STORE.orders.get(order_id)
    if o is None:
        return None
    delivered = next((e.at for e in o.history if e.state == "delivered"), None)
    out = {"order_id": o.id, "state": o.state.value, "delivered_at": delivered,
           "billing_start": None}
    c = STORE.contracts.get(getattr(o, "contract_id", None) or "")
    if delivered and c:
        start = _dt(date.fromisoformat(c["start_date"]))
        out["billing_start"] = max(start, datetime.fromisoformat(delivered)).isoformat()
    elif delivered:
        out["billing_start"] = delivered
    return out


# ---------------------------------------------------------------------------
# Order hook (lifecycle.create_order)
# ---------------------------------------------------------------------------
def apply_contract_to_order(body) -> None:
    """Fill an OrderCreate from its contract; 422 when it cannot be honoured.

    Called before the order pipeline runs, so a missing same-IB-zone capacity
    is a 422 (contract test) rather than a rejected order."""
    from .lifecycle import select_racks
    from .models import OrderKind, OrderState

    with STORE.lock:
        c = STORE.contracts.get(body.contract_id)
        if c is None:
            raise HTTPException(422, f"contract '{body.contract_id}' not found")
        refresh_status(c)
        if c["tenant_id"] != body.tenant_id:
            raise HTTPException(422, "contract belongs to another tenant")
        if c["status"] != "active":
            raise HTTPException(422, f"contract is {c['status']} (must be active)")
        if c["product"] not in RACK_PRODUCTS:
            raise HTTPException(422, "token-api contracts carry no rack order")
        if body.kind != OrderKind.new:
            return
        live = [STORE.orders[oid] for oid in c["order_ids"] if oid in STORE.orders]
        if any(o.state not in (OrderState.rejected, OrderState.failed,
                               OrderState.closed) for o in live):
            raise HTTPException(409, "contract capacity already ordered "
                                     f"({', '.join(o.id for o in live)})")
        body.racks = c["racks"]
        body.blueprint_key = c["blueprint_key"]
        if c["product"] == "managed-k8s":
            body.managed_k8s = True
        tenant = STORE.tenants.get(body.tenant_id)
        if (c.get("placement") or {}).get("same_ib_zone", True):
            if select_racks(body.blueprint_key, body.racks, tenant=tenant) is None:
                raise HTTPException(
                    422, f"insufficient capacity: {body.racks}x "
                         f"{body.blueprint_key} rack(s) not available in one "
                         "IB zone (site) for contract "
                         f"{c['id']}")


def link_order(contract_id: str, order_id: str) -> None:
    with STORE.lock:
        c = STORE.contracts.get(contract_id)
        if c is not None and order_id not in c["order_ids"]:
            c["order_ids"].append(order_id)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@router.post("/contracts", status_code=201)
def create_contract(body: ContractCreate, request: Request) -> dict:
    _require_operator(request)
    with STORE.lock:
        if body.tenant_id not in STORE.tenants:
            raise HTTPException(404, f"tenant '{body.tenant_id}' not found")
        start = _parse_date(body.start_date)
        c = body.model_dump()
        if c["product"] in RACK_PRODUCTS:
            c["blueprint_key"] = c["blueprint_key"] or DEFAULT_BLUEPRINT
        c["placement"] = c["placement"] or {"same_ib_zone": True}
        _validate(c)
        cid = f"ctr-{STORE.next_seq('contract')}"
        now = _now().isoformat()
        c.update(id=cid, status="draft", currency="KRW",
                 end_date=add_months(start, body.term_months).isoformat(),
                 created_at=now, updated_at=now, activated_at=None,
                 terminated_at=None, order_ids=[])
        STORE.contracts[cid] = c
        emit("BizPortal", "NeoCloudOS.M7", "REST", f"POST /contracts → {cid}",
             f"약정 계약 초안 — {body.tenant_id} {body.product} "
             f"{body.racks or ''}랙 {body.term_months}개월",
             payload={"contract_id": cid, "tenant_id": body.tenant_id})
        return _public(c)


@router.get("/contracts")
def list_contracts(request: Request, tenant_id: Optional[str] = None,
                   status: Optional[str] = None) -> list:
    tid = _caller_tenant(request)
    if tid is not None:
        tenant_id = tid
    with STORE.lock:
        out = [refresh_status(c) for c in STORE.contracts.values()]
        if tenant_id:
            out = [c for c in out if c["tenant_id"] == tenant_id]
        if status:
            out = [c for c in out if c["status"] == status]
        return [_public(c) for c in sorted(out, key=lambda c: c["id"])]


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: str, request: Request) -> dict:
    with STORE.lock:
        return _public(_get(contract_id, request))


@router.patch("/contracts/{contract_id}")
def patch_contract(contract_id: str, body: ContractPatch,
                   request: Request) -> dict:
    _require_operator(request)
    with STORE.lock:
        c = _get(contract_id)
        changes = body.model_dump(exclude_none=True)
        status = changes.pop("status", None)
        if c["status"] in ("expired", "terminated"):
            raise HTTPException(409, f"contract is {c['status']}")
        if status is not None:
            if status != "terminated":
                raise HTTPException(422, "status may only be set to 'terminated' "
                                         "(use /activate to activate)")
            c["status"] = "terminated"
            c["terminated_at"] = _now().isoformat()
            from . import billing_lago            # Lago subscription.terminated
            billing_lago.on_contract_terminated(c)
        if changes:
            if c["status"] != "draft" and set(changes) - {"sla_tier"}:
                raise HTTPException(409, "only a draft contract can change terms")
            merged = {**c, **changes}
            if "start_date" in changes or "term_months" in changes:
                merged["end_date"] = add_months(
                    _parse_date(merged["start_date"]),
                    merged["term_months"]).isoformat()
            _validate(merged)
            c.update(merged)
        c["updated_at"] = _now().isoformat()
        return _public(c)


@router.post("/contracts/{contract_id}/activate")
def activate_contract(contract_id: str, request: Request) -> dict:
    _require_operator(request)
    with STORE.lock:
        c = _get(contract_id)
        if c["status"] != "draft":
            raise HTTPException(409, f"contract is {c['status']} (must be draft)")
        now = _now()
        c["activated_at"] = now.isoformat()
        c["status"] = "active"
        c["updated_at"] = now.isoformat()
        refresh_status(c, now)                     # back-dated term already over
        from . import billing_lago                 # Lago plan + subscription.started
        billing_lago.on_contract_activated(c)
        emit("BizPortal", "NeoCloudOS.M7", "internal",
             f"contract:activate → {contract_id}",
             f"약정 계약 활성 — {c['tenant_id']} {c['product']} "
             f"{c.get('racks') or ''}랙 {c['start_date']}~{c['end_date']}",
             payload={"contract_id": contract_id, "status": c["status"]})
        return _public(c)
