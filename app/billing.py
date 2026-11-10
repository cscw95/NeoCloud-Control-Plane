"""F05 billing engine — KRW price book · monthly invoices · payments · tax invoice.

IMPROVEMENT-PLAN-v2 §F05 (Lago/Stripe-style emulation, in-memory):

  usage sources ──> rated lines ──> + commitment true-up (F06) ── SLA credits (F09)
                                     = subtotal ──> VAT 10% ──> total (KRW)

Usage sources
  - rack hours: delivered `new` orders (start = delivered, or
    max(contract start, delivered) for a contract order; end = the closing of
    the terminate order that reclaimed every allocation). A contract order is
    priced at the contract unit price inside the term and at the on-demand
    price-book rate after it (expiry / termination).
  - tokens and metered hours: F04 usage events in `STORE.metering_events`
    (dict or list of {idempotency_key, source, tenant_id, product, metric,
    quantity, unit, ts, attrs}). Duplicate idempotency keys are billed once.
    Tokens are billed from the gateway source only (the engine count is for
    F04 reconciliation). Hour events (nico/nvke) replace the order-derived
    rack hours for that tenant+product when present. Until F04 lands the
    attribute is absent and only order-derived lines are produced (stub).

Money: integer KRW, each line rounded half-up; VAT = floor(subtotal × 10%)
(원 미만 절사, the usual practice on Korean tax invoices). Token prices are
per 1,000,000 tokens. Period boundaries are UTC calendar months.

Tax invoice and payment are emulated adapters: no NTS (국세청) or PG call.

Upstream alignment (WAVE-v2 "원본 충실도"; UPSTREAM-REFS §3.1 7–10): these
/api/v1/billing endpoints stay as the NeoCloud facade. Underneath,
  - billing_lago.py is the Lago-shaped surface (`/lago/api/v1/...`): customers,
    plans, subscriptions, events (ingest → usage events read here), invoices
    in Lago shape, and the Lago webhooks this module triggers (invoice.drafted
    on run, invoice.created on finalize, invoice.payment_status_updated /
    invoice.payment_failure on payment, invoice.voided);
  - billing_stripe.py is the Stripe test-mode surface (`/stripe/v1/...`):
    payment is a PaymentIntent created with confirm=true on the tenant's
    Stripe customer (events payment_intent.succeeded / .payment_failed);
  - billing_webhooks.py signs and logs every webhook
    (GET /api/v1/billing/webhooks shows the raw headers and bodies).
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import contracts as ctr
from .models import OrderKind, OrderState
from .store import STORE
from .trace import emit

router = APIRouter(prefix="/api/v1", tags=["billing-v2"])

CURRENCY = "KRW"
VAT_RATE = Decimal("0.10")
TOKEN_UNIT = 1_000_000
METRICS = ("rack_hour", "gpu_hour", "node_hour", "token")
HOUR_METRICS = ("rack_hour", "gpu_hour", "node_hour")
TOKEN_TYPES = ("input", "cached", "output", "reasoning")
COMMITMENTS = ("reserved", "on_demand")
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# emulated supplier on the tax invoice (placeholder registration number)
SUPPLIER = {"business_reg_no": "000-00-00000", "name": "NeoCloud (emulated supplier)",
            "representative": "-", "address": "-", "biz_type": "정보통신업",
            "biz_item": "클라우드 컴퓨팅"}

# default price book (KRW, demo figures; ~USD rate preview × 1,400)
DEFAULT_ITEMS = [
    {"product": "bmaas", "metric": "rack_hour", "unit_price": 1_372_000,
     "commitment": "on_demand"},
    {"product": "bmaas", "metric": "rack_hour", "unit_price": 1_100_000,
     "commitment": "reserved"},
    {"product": "managed-k8s", "metric": "rack_hour", "unit_price": 1_440_000,
     "commitment": "on_demand"},
    {"product": "managed-k8s", "metric": "rack_hour", "unit_price": 1_160_000,
     "commitment": "reserved"},
    {"product": "bmaas", "metric": "gpu_hour", "unit_price": 19_000},
    {"product": "bmaas", "metric": "node_hour", "unit_price": 76_000},
    {"product": "managed-k8s", "metric": "node_hour", "unit_price": 80_000},
    {"product": "token-api", "metric": "token", "token_type": "input",
     "unit_price": 800},
    {"product": "token-api", "metric": "token", "token_type": "cached",
     "unit_price": 200},
    {"product": "token-api", "metric": "token", "token_type": "output",
     "unit_price": 3_200},
    {"product": "token-api", "metric": "token", "token_type": "reasoning",
     "unit_price": 3_200},
    {"product": "token-api", "metric": "token", "token_type": "input",
     "tier": "batch", "unit_price": 400},
    {"product": "token-api", "metric": "token", "token_type": "output",
     "tier": "batch", "unit_price": 1_600},
]


class PriceItem(BaseModel):
    product: str
    metric: str
    model: Optional[str] = None
    token_type: Optional[str] = None
    tier: Optional[str] = None
    unit_price: float = Field(..., ge=0)
    commitment: Optional[str] = None


class PriceBook(BaseModel):
    currency: str = CURRENCY
    items: list[PriceItem]


class InvoiceRun(BaseModel):
    period: str
    tenant_id: Optional[str] = None


class PaymentMethodCreate(BaseModel):
    tenant_id: str
    kind: str
    token: str


class PayBody(BaseModel):
    payment_method_id: Optional[str] = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _krw(x) -> int:
    return int(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _vat(subtotal: int) -> int:
    return int(math.floor(Decimal(subtotal) * VAT_RATE)) if subtotal > 0 else 0


def _caller_tenant(request: Request) -> Optional[str]:
    return ctr._caller_tenant(request)


def _require_operator(request: Request) -> None:
    if _caller_tenant(request) is not None:
        raise HTTPException(403, "operator only")


def period_bounds(period: str) -> tuple[datetime, datetime]:
    if not PERIOD_RE.match(period or ""):
        raise HTTPException(422, "period must be YYYY-MM")
    y, m = int(period[:4]), int(period[5:])
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=timezone.utc)
    return start, end


def _hours(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 3600)


def _ts(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# price book
# ---------------------------------------------------------------------------
def _normalize_item(it: dict) -> dict:
    it = {k: it.get(k) for k in ("product", "metric", "model", "token_type",
                                 "tier", "unit_price", "commitment")}
    it["unit"] = f"{TOKEN_UNIT:,} tokens" if it["metric"] == "token" else \
        it["metric"].replace("_", "-")
    return it


def price_book() -> dict:
    pb = STORE.price_book
    if not pb:
        pb.update(currency=CURRENCY, version=1, updated_at=_now().isoformat(),
                  items=[_normalize_item(i) for i in DEFAULT_ITEMS])
    return pb


def lookup_price(product: str, metric: str, *, model=None, token_type=None,
                 tier=None, commitment: str = "on_demand") -> Optional[dict]:
    """Most specific matching item; an item field left null is a wildcard.
    A reserved lookup falls back to the on-demand price."""
    want = {"model": model, "token_type": token_type, "tier": tier}
    best, best_score = None, -1
    for it in price_book()["items"]:
        if it["product"] != product or it["metric"] != metric:
            continue
        if (it.get("commitment") or "on_demand") != commitment and \
                it.get("commitment") is not None:
            continue
        score = 0
        ok = True
        for k, v in want.items():
            if it.get(k) is None:
                continue
            if it[k] != v:
                ok = False
                break
            score += 1
        if it.get("commitment") == commitment:
            score += 1
        if ok and score > best_score:
            best, best_score = it, score
    if best is None and commitment == "reserved":
        return lookup_price(product, metric, model=model, token_type=token_type,
                            tier=tier, commitment="on_demand")
    return best


@router.get("/billing/price-book")
def get_price_book() -> dict:
    with STORE.lock:
        return price_book()


@router.put("/billing/price-book")
def put_price_book(body: PriceBook, request: Request) -> dict:
    _require_operator(request)
    if body.currency != CURRENCY:
        raise HTTPException(422, "currency must be KRW")
    items = []
    for it in body.items:
        if it.metric not in METRICS:
            raise HTTPException(422, f"metric must be one of {METRICS}")
        if it.product not in ctr.PRODUCTS:
            raise HTTPException(422, f"product must be one of {ctr.PRODUCTS}")
        if it.token_type is not None and it.token_type not in TOKEN_TYPES:
            raise HTTPException(422, f"token_type must be one of {TOKEN_TYPES}")
        if it.commitment is not None and it.commitment not in COMMITMENTS:
            raise HTTPException(422, f"commitment must be one of {COMMITMENTS}")
        items.append(_normalize_item(it.model_dump()))
    with STORE.lock:
        pb = price_book()
        pb.update(items=items, version=pb["version"] + 1,
                  updated_at=_now().isoformat())
        emit("BizPortal", "NeoCloudOS.M7", "REST", "PUT /billing/price-book",
             f"요율표 v{pb['version']} 반영 — {len(items)}개 항목 (KRW)",
             payload={"version": pb["version"]})
        return pb


# ---------------------------------------------------------------------------
# usage → rated lines
# ---------------------------------------------------------------------------
def _metering_events() -> list[dict]:
    """F04 usage events (stub until F04 lands: attribute absent → [])."""
    raw = getattr(STORE, "metering_events", None) or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    from . import billing_lago
    raw = list(raw) + billing_lago.usage_events()     # Lago POST /events ingest
    out, seen = [], set()
    for e in raw:
        e = e.model_dump() if hasattr(e, "model_dump") else dict(e)
        key = e.get("idempotency_key")
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.append(e)
    return out


def _order_end(o, end_by_alloc: dict) -> Optional[str]:
    ends = [end_by_alloc[a] for a in o.allocation_ids if a in end_by_alloc]
    return max(ends) if ends and len(ends) == len(o.allocation_ids) else None


def _rack_lines(tenant_id: str, p0: datetime, p1: datetime, now: datetime,
                skip_products: set) -> list[dict]:
    end_by_alloc: dict[str, str] = {}
    for o in STORE.orders.values():
        if (o.kind == OrderKind.terminate and o.state == OrderState.closed
                and o.allocation_id and o.history):
            end_by_alloc[o.allocation_id] = o.history[-1].at
    lines = []
    for o in sorted(STORE.orders.values(), key=lambda x: x.id):
        if o.tenant_id != tenant_id or o.kind != OrderKind.new:
            continue
        if o.state not in (OrderState.delivered, OrderState.closed):
            continue
        product = "managed-k8s" if o.managed_k8s else "bmaas"
        if product in skip_products:
            continue
        delivered = next((e.at for e in o.history if e.state == "delivered"), None)
        if not delivered:
            continue
        start = datetime.fromisoformat(delivered)
        c = STORE.contracts.get(o.contract_id or "")
        if c is not None:
            start = max(start, ctr.term_window(c)[0])
        end_at = _order_end(o, end_by_alloc)
        end = datetime.fromisoformat(end_at) if end_at else now
        a, b = max(start, p0), min(end, p1, now)
        if b <= a:
            continue
        segments = [(a, b, None)]
        if c is not None and c.get("activated_at"):
            cs, ce = ctr.term_window(c)
            ra, rb = max(a, cs), min(b, ce)
            if rb > ra:                     # reserved inside the term, on-demand outside
                segments = [s for s in ((a, ra, None), (ra, rb, c), (rb, b, None))
                            if s[1] > s[0]]
        for sa, sb, sc in segments:
            qty = round(o.racks * _hours(sa, sb), 4)
            if sc is not None:
                unit_price, commitment = float(sc["unit_price"]), "reserved"
            else:
                it = lookup_price(product, "rack_hour")
                unit_price, commitment = (it["unit_price"] if it else 0.0), "on_demand"
            lines.append({
                "product": product, "metric": "rack_hour", "quantity": qty,
                "unit_price": unit_price, "amount": _krw(qty * unit_price),
                "unit": "rack-hour", "commitment": commitment,
                "contract_id": sc["id"] if sc else None, "order_id": o.id,
                "blueprint_key": o.blueprint_key, "racks": o.racks,
                "start": sa.isoformat(), "end": sb.isoformat()})
    return lines


def _event_lines(tenant_id: str, p0: datetime, p1: datetime,
                 events: list[dict]) -> tuple[list[dict], set]:
    """Token lines (gateway source) and metered hour lines; returns the
    products whose hours came from events (they replace order-derived ones)."""
    groups: dict[tuple, dict] = {}
    hour_products: set = set()
    tenant_contracts = ctr.contracts_of_tenant(tenant_id)
    for e in events:
        if e.get("tenant_id") != tenant_id:
            continue
        at = _ts(e.get("ts"))
        if at is None or not (p0 <= at < p1):
            continue
        attrs = e.get("attrs") or {}
        product, metric = e.get("product"), e.get("metric")
        if metric in ("token", "tokens") or product == "token-api":
            if (e.get("source") or "gateway") != "gateway":
                continue
            metric = "token"
            product = "token-api"
            key_attrs = (attrs.get("model"), attrs.get("token_type") or "input",
                         attrs.get("tier") or attrs.get("service_tier"))
        elif metric in HOUR_METRICS:
            hour_products.add(product)
            key_attrs = (None, None, None)
        else:
            continue
        reserved = next((c for c in tenant_contracts if c["product"] == product
                         and ctr.is_reserved_at(c, at)), None)
        key = (product, metric) + key_attrs + (reserved["id"] if reserved else None,)
        g = groups.setdefault(key, {"quantity": 0.0, "contract": reserved})
        g["quantity"] += float(e.get("quantity") or 0)
    lines = []
    for (product, metric, model, token_type, tier, cid), g in sorted(
            groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        c = g["contract"]
        if metric in HOUR_METRICS and c is not None and product in ctr.RACK_PRODUCTS \
                and metric == "rack_hour":
            unit_price = float(c["unit_price"])
        else:
            it = lookup_price(product, metric, model=model, token_type=token_type,
                              tier=tier,
                              commitment="reserved" if c else "on_demand")
            unit_price = it["unit_price"] if it else 0.0
        qty = round(g["quantity"], 4)
        per = TOKEN_UNIT if metric == "token" else 1
        line = {"product": product, "metric": metric, "quantity": qty,
                "unit_price": unit_price, "amount": _krw(qty / per * unit_price),
                "unit": f"{TOKEN_UNIT:,} tokens" if metric == "token"
                else metric.replace("_", "-"),
                "commitment": "reserved" if c else "on_demand",
                "contract_id": cid}
        if metric == "token":
            line.update(model=model, token_type=token_type, tier=tier)
        lines.append(line)
    return lines, hour_products


def _true_ups(tenant_id: str, lines: list[dict], p0: datetime, p1: datetime,
              now: datetime) -> list[dict]:
    """Commitment true-up: if the committed amount for the billable part of the
    period exceeds what the contract's usage came to, bill the difference."""
    out = []
    period_h = _hours(p0, p1)
    for c in ctr.contracts_of_tenant(tenant_id):
        if not c.get("activated_at"):
            continue
        cs, ce = ctr.term_window(c)
        if c["product"] in ctr.RACK_PRODUCTS:
            starts = [ctr.order_billing_start(oid) for oid in c["order_ids"]]
            starts = [datetime.fromisoformat(s["billing_start"])
                      for s in starts if s and s["billing_start"]]
            if not starts:
                continue                    # billing starts at delivery
            cs = max(cs, min(starts))
            used = sum(l["amount"] for l in lines if l.get("contract_id") == c["id"])
        else:
            used = sum(l["amount"] for l in lines if l["product"] == "token-api"
                       and l.get("commitment") == "reserved")
        a, b = max(cs, p0), min(ce, p1, now)
        hours = _hours(a, b)
        if hours <= 0:
            continue
        if c.get("min_monthly_amount") is not None:
            commit = _krw(float(c["min_monthly_amount"]) * hours / period_h)
        else:
            commit = _krw(c["racks"] * float(c["unit_price"]) * hours)
        out.append({"contract_id": c["id"], "product": c["product"],
                    "committed": commit, "used": used,
                    "amount": max(0, commit - used),
                    "start": a.isoformat(), "end": b.isoformat()})
    return out


def _apply_credits(inv_id: str, tenant_id: str, period: str,
                   usage_amount: int) -> list[dict]:
    """F09 SLA credits for this tenant up to this period that are not applied
    to another invoice. KRW amount = pct_of_monthly × this invoice's usage
    (unless the credit already carries amount_krw); never exceeds usage."""
    out, left = [], usage_amount
    for c in sorted(STORE.sla_credits.values(), key=lambda c: c["credit_id"]):
        if c.get("tenant_id") != tenant_id:
            continue
        if c.get("applied_invoice") == inv_id:
            c["applied_invoice"] = None     # recomputed below
        if c.get("applied_invoice") is not None or c.get("status") == "void":
            continue
        if (c.get("month") or period) > period:
            continue
        amt = c.get("amount_krw")
        if amt is None:
            amt = _krw(usage_amount * float(c.get("pct_of_monthly") or 0) / 100)
        amt = min(int(amt), left)
        left -= amt
        c["applied_invoice"] = inv_id
        out.append({"credit_id": c["credit_id"], "amount": amt,
                    "month": c.get("month"),
                    "pct_of_monthly": c.get("pct_of_monthly"),
                    "incident_id": c.get("incident_id")})
    return out


def compute_invoice(inv: dict, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    tenant_id, period = inv["tenant_id"], inv["period"]
    p0, p1 = period_bounds(period)
    ev_lines, hour_products = _event_lines(tenant_id, p0, p1, _metering_events())
    lines = _rack_lines(tenant_id, p0, p1, now, hour_products) + ev_lines
    true_ups = _true_ups(tenant_id, lines, p0, p1, now)
    true_up = sum(t["amount"] for t in true_ups)
    usage_amount = sum(l["amount"] for l in lines) + true_up
    credits = _apply_credits(inv["id"], tenant_id, period, usage_amount)
    credit_total = sum(c["amount"] for c in credits)
    subtotal = usage_amount - credit_total
    vat = _vat(subtotal)
    inv.update(lines=lines, commitment_true_up=true_up, true_up_lines=true_ups,
               sla_credits=credits, usage_amount=usage_amount,
               credit_total=credit_total, subtotal=subtotal,
               vat_rate=float(VAT_RATE), vat=vat, total=subtotal + vat,
               price_book_version=price_book()["version"],
               computed_at=now.isoformat(), updated_at=now.isoformat())
    return inv


# ---------------------------------------------------------------------------
# invoices
# ---------------------------------------------------------------------------
def _invoice_key(tenant_id: str, period: str) -> Optional[dict]:
    return next((i for i in STORE.invoices.values()
                 if i["tenant_id"] == tenant_id and i["period"] == period
                 and i["status"] != "void"), None)


def _get_invoice(invoice_id: str, request: Request) -> dict:
    inv = STORE.invoices.get(invoice_id)
    if inv is None:
        raise HTTPException(404, f"invoice '{invoice_id}' not found")
    tid = _caller_tenant(request)
    if tid is not None and tid != inv["tenant_id"]:
        raise HTTPException(403, "tenant scope violation")
    return inv


def _summary(inv: dict) -> dict:
    return {k: inv.get(k) for k in ("id", "tenant_id", "period", "status",
                                    "currency", "subtotal", "vat", "total")}


@router.post("/billing/invoices:run")
def run_invoices(body: InvoiceRun, request: Request) -> dict:
    """Draft invoices per tenant for the period (idempotent: one invoice per
    tenant+period; a draft is recomputed in place, finalized ones are left)."""
    _require_operator(request)
    p0, _ = period_bounds(body.period)
    now = _now()
    if p0 > now:
        raise HTTPException(422, "period is in the future")
    created, updated, skipped, out = [], [], [], []
    with STORE.lock:
        tenants = [body.tenant_id] if body.tenant_id else sorted(STORE.tenants)
        for tid in tenants:
            if tid not in STORE.tenants:
                raise HTTPException(404, f"tenant '{tid}' not found")
            inv = _invoice_key(tid, body.period)
            if inv is not None and inv["status"] != "draft":
                skipped.append(inv["id"])
                out.append(_summary(inv))
                continue
            fresh = inv is None
            if fresh:
                inv = {"id": f"inv-pending-{tid}",
                       "tenant_id": tid, "period": body.period,
                       "currency": CURRENCY, "status": "draft",
                       "created_at": now.isoformat(), "finalized_at": None,
                       "paid_at": None, "payment": None, "tax_invoice": None}
            compute_invoice(inv, now)
            if fresh:
                if not (inv["lines"] or inv["true_up_lines"] or inv["sla_credits"]):
                    continue                # nothing to bill
                pending = inv["id"]
                inv["id"] = (f"inv-{body.period.replace('-', '')}-"
                             f"{STORE.next_seq('invoice'):04d}")
                for c in STORE.sla_credits.values():
                    if c.get("applied_invoice") == pending:
                        c["applied_invoice"] = inv["id"]
            STORE.invoices[inv["id"]] = inv
            (created if fresh else updated).append(inv["id"])
            if fresh:
                from . import billing_lago
                billing_lago.on_invoice("drafted", inv)
            out.append(_summary(inv))
        emit("NeoCloudOS.M7", "BizPortal", "internal",
             f"POST /billing/invoices:run {body.period}",
             f"월 청구 실행 — 신규 {len(created)} · 갱신 {len(updated)} · "
             f"확정 건 제외 {len(skipped)}",
             payload={"period": body.period, "created": created,
                      "updated": updated, "skipped": skipped})
    return {"period": body.period, "invoices": out, "created": created,
            "updated": updated, "skipped": skipped}


@router.get("/billing/invoices")
def list_invoices(request: Request, tenant_id: Optional[str] = None,
                  period: Optional[str] = None,
                  status: Optional[str] = None) -> list:
    tid = _caller_tenant(request)
    if tid is not None:
        tenant_id = tid
    with STORE.lock:
        out = list(STORE.invoices.values())
        if tenant_id:
            out = [i for i in out if i["tenant_id"] == tenant_id]
        if period:
            out = [i for i in out if i["period"] == period]
        if status:
            out = [i for i in out if i["status"] == status]
        return sorted(out, key=lambda i: i["id"], reverse=True)


@router.get("/billing/invoices/{invoice_id}")
def get_invoice(invoice_id: str, request: Request) -> dict:
    with STORE.lock:
        return _get_invoice(invoice_id, request)


def _tax_invoice(inv: dict, now: datetime) -> dict:
    tenant = STORE.tenants.get(inv["tenant_id"])
    reg_no = getattr(tenant, "business_reg_no", None)
    if reg_no is None:                       # F07 signup record, when present
        for rec in (getattr(STORE, "signups", None) or {}).values():
            rec = rec.model_dump() if hasattr(rec, "model_dump") else rec
            if rec.get("tenant_id") == inv["tenant_id"]:
                reg_no = rec.get("business_reg_no")
                break
    seq = STORE.next_seq("tax_invoice")
    digest = hashlib.sha256(inv["id"].encode()).hexdigest()
    approval = f"{now:%Y%m%d}-41{int(digest[:6], 16) % 10**6:06d}-{seq:08d}"
    return {
        "invoice_id": inv["id"], "approval_no": approval,
        "kind": "전자세금계산서", "purpose": "청구",
        "issue_date": now.date().isoformat(), "supplier": SUPPLIER,
        "buyer": {"business_reg_no": reg_no,
                  "name": tenant.name if tenant else inv["tenant_id"],
                  "tenant_id": inv["tenant_id"]},
        "buyer_reg_no_status": "ok" if reg_no else "missing",
        "items": [{"date": now.date().isoformat(),
                   "name": f"NeoCloud 이용료 {inv['period']}",
                   "supply_amount": inv["subtotal"], "tax_amount": inv["vat"]}],
        "supply_amount": inv["subtotal"], "tax_amount": inv["vat"],
        "total": inv["total"], "currency": CURRENCY,
        "nts_transmission": "emulated", "status": "issued",
    }


@router.post("/billing/invoices/{invoice_id}/finalize")
def finalize_invoice(invoice_id: str, request: Request) -> dict:
    _require_operator(request)
    with STORE.lock:
        inv = _get_invoice(invoice_id, request)
        if inv["status"] != "draft":
            raise HTTPException(409, f"invoice is {inv['status']} (must be draft)")
        return finalize(inv)


def finalize(inv: dict) -> dict:
    """draft → finalized (Lago PUT /invoices/{id}/finalize shares this)."""
    now = _now()
    compute_invoice(inv, now)                # freeze the latest numbers
    inv["status"] = "finalized"
    inv["finalized_at"] = now.isoformat()
    inv["tax_invoice"] = _tax_invoice(inv, now)
    for c in inv["sla_credits"]:
        sc = STORE.sla_credits.get(c["credit_id"])
        if sc is not None:
            sc.update(status="applied", applied_invoice=inv["id"],
                      amount_krw=c["amount"])
    emit("NeoCloudOS.M7", "BizPortal", "internal",
         f"invoice:finalize → {inv['id']}",
         f"청구서 확정 — {inv['tenant_id']} {inv['period']} "
         f"₩{inv['total']:,} (VAT ₩{inv['vat']:,}) · 세금계산서 "
         f"{inv['tax_invoice']['approval_no']}",
         payload=_summary(inv))
    from . import billing_lago
    billing_lago.on_invoice("finalized", inv)
    return inv


def void(inv: dict) -> dict:
    """finalized (unpaid) → void (Lago POST /invoices/{id}/void)."""
    now = _now()
    inv.update(status="void", voided_at=now.isoformat(), updated_at=now.isoformat())
    for c in STORE.sla_credits.values():     # credits go back to the pool
        if c.get("applied_invoice") == inv["id"]:
            c.update(applied_invoice=None, status="pending")
    from . import billing_lago
    billing_lago.on_invoice("voided", inv)
    return inv


def charge(inv: dict, pm: dict) -> tuple[bool, dict]:
    """Stripe PaymentIntent (test mode) for the invoice total, confirm=true.
    Returns (succeeded, payment) — payment is the PaymentIntent plus the
    NeoCloud fields invoice_id, tenant_id, kind, failure_code."""
    from . import billing_lago, billing_stripe
    cus = billing_stripe.customer_for(inv["tenant_id"])
    if not pm.get("stripe_payment_method"):
        pm["stripe_payment_method"] = billing_stripe.create_payment_method(
            pm["token"], "card", cus["id"])["id"]
    try:
        pi = billing_stripe.create_payment_intent(
            inv["total"], "krw", cus["id"], pm["stripe_payment_method"], True,
            {"invoice_id": inv["id"], "tenant_id": inv["tenant_id"]},
            f"NeoCloud invoice {inv['id']}", f"nocp-pay-{inv['id']}-"
            f"{STORE.next_seq('payment')}")
        ok = True
    except billing_stripe.StripeError as e:
        pi = e.body["error"].get("payment_intent")
        if pi is None:
            raise HTTPException(e.status, e.body)
        ok = False
    err = pi.get("last_payment_error") or {}
    pay = {**pi, "invoice_id": inv["id"], "tenant_id": inv["tenant_id"],
           "kind": pm["kind"], "failure_code": None if ok else err.get("code")}
    STORE.payments[pi["id"]] = pay
    now = _now()
    if ok:
        inv.update(status="paid", paid_at=now.isoformat(), payment=pay,
                   payment_failed=False, updated_at=now.isoformat())
        billing_lago.on_invoice("paid", inv, pay)
    else:
        inv.update(payment_failed=True, updated_at=now.isoformat())
        billing_lago.on_invoice("payment_failed", inv, pay)
    return ok, pay


@router.post("/billing/invoices/{invoice_id}/pay")
def pay_invoice(invoice_id: str, request: Request,
                body: Optional[PayBody] = None) -> dict:
    """Stripe PaymentIntent (test mode, billing_stripe.py). A token starting
    with 'tok_fail' (or Stripe's tok_chargeDeclined) declines (402)."""
    with STORE.lock:
        inv = _get_invoice(invoice_id, request)
        if inv["status"] != "finalized":
            raise HTTPException(409, f"invoice is {inv['status']} (must be finalized)")
        pms = [p for p in STORE.payment_methods.values()
               if p["tenant_id"] == inv["tenant_id"]]
        pm_id = body.payment_method_id if body else None
        pm = (STORE.payment_methods.get(pm_id) if pm_id
              else next((p for p in pms if p["default"]), None))
        if pm is None or pm["tenant_id"] != inv["tenant_id"]:
            raise HTTPException(422, "no payment method for tenant")
        ok, pay = charge(inv, pm)
        pid = pay["id"]
        if not ok:
            raise HTTPException(402, {"error": "card_declined", "payment": pay})
        emit("PaymentGateway", "NeoCloudOS.M7", "REST",
             f"POST /billing/invoices/{invoice_id}/pay → {pid}",
             f"결제 완료(모사) — ₩{inv['total']:,} {pm['kind']}",
             payload={"invoice_id": inv["id"], "payment_id": pid})
        return inv


@router.get("/billing/invoices/{invoice_id}/tax-invoice")
def get_tax_invoice(invoice_id: str, request: Request) -> dict:
    with STORE.lock:
        inv = _get_invoice(invoice_id, request)
        if inv["tax_invoice"] is None:
            raise HTTPException(409, "tax invoice is issued on finalize")
        return inv["tax_invoice"]


# ---------------------------------------------------------------------------
# payment methods
# ---------------------------------------------------------------------------
@router.post("/billing/payment-methods", status_code=201)
def add_payment_method(body: PaymentMethodCreate, request: Request) -> dict:
    tid = _caller_tenant(request)
    if tid is not None and tid != body.tenant_id:
        raise HTTPException(403, "tenant scope violation")
    if body.kind not in ("card", "bank_transfer"):
        raise HTTPException(422, "kind must be card|bank_transfer")
    if not body.token.strip():
        raise HTTPException(422, "token required")
    with STORE.lock:
        if body.tenant_id not in STORE.tenants:
            raise HTTPException(404, f"tenant '{body.tenant_id}' not found")
        first = not any(p["tenant_id"] == body.tenant_id
                        for p in STORE.payment_methods.values())
        digits = re.sub(r"\D", "", body.token)
        pm = {"id": f"pm-{STORE.next_seq('payment_method')}",
              "tenant_id": body.tenant_id, "kind": body.kind,
              "token": body.token, "last4": digits[-4:] if digits else None,
              "default": first, "created_at": _now().isoformat()}
        from . import billing_stripe
        cus = billing_stripe.customer_for(body.tenant_id,
                                          STORE.tenants[body.tenant_id].name)
        pm["stripe_customer"] = cus["id"]
        pm["stripe_payment_method"] = billing_stripe.create_payment_method(
            body.token, "card" if body.kind == "card" else "customer_balance",
            cus["id"])["id"]
        STORE.payment_methods[pm["id"]] = pm
        return {k: v for k, v in pm.items() if k != "token"}


@router.get("/billing/payment-methods")
def list_payment_methods(request: Request,
                         tenant_id: Optional[str] = None) -> list:
    tid = _caller_tenant(request)
    if tid is not None:
        tenant_id = tid
    with STORE.lock:
        return [{k: v for k, v in p.items() if k != "token"}
                for p in STORE.payment_methods.values()
                if not tenant_id or p["tenant_id"] == tenant_id]


# ---------------------------------------------------------------------------
# webhook log (NeoCloud extension: what Lago/Stripe sent, raw)
# ---------------------------------------------------------------------------
@router.get("/billing/webhooks")
def list_webhooks(request: Request, provider: Optional[str] = None,
                  tenant_id: Optional[str] = None,
                  invoice_id: Optional[str] = None,
                  type: Optional[str] = None) -> list:
    """Signed Lago/Stripe webhooks with raw headers, body and delivery result
    (operator; a tenant sees only its own)."""
    from . import billing_webhooks
    tid = _caller_tenant(request)
    if tid is not None:
        tenant_id = tid
    with STORE.lock:
        out = billing_webhooks.deliveries(provider, tenant_id=tenant_id,
                                          invoice_id=invoice_id)
        if type:
            out = [r for r in out if r["type"] == type]
        if tid is not None:
            out = [r for r in out if r["refs"].get("tenant_id") == tid]
        return [{k: v for k, v in r.items() if k != "raw"} | {"raw_size": len(r["raw"])}
                for r in out]


class DemoReset(BaseModel):
    tenant_id: str


@router.post("/billing/demo:reset")
def demo_reset(body: DemoReset, request: Request) -> dict:
    """Drop one tenant's billing state (contracts, invoices, payments, Lago and
    Stripe objects, webhook log) so a billing demo reruns deterministically
    (NeoCloud extension, operator only). Orders and the tenant are kept."""
    _require_operator(request)
    tid = body.tenant_id
    with STORE.lock:
        removed = {}

        def _drop(name: str, d: dict, pred) -> None:
            keys = [k for k, v in d.items() if pred(v)]
            for k in keys:
                del d[k]
            removed[name] = len(keys)

        cids = {k for k, c in STORE.contracts.items() if c["tenant_id"] == tid}
        _drop("contracts", STORE.contracts, lambda c: c["tenant_id"] == tid)
        _drop("invoices", STORE.invoices, lambda i: i["tenant_id"] == tid)
        _drop("payments", STORE.payments, lambda p: p.get("tenant_id") == tid)
        _drop("payment_methods", STORE.payment_methods,
              lambda p: p["tenant_id"] == tid)
        lago = STORE.lago
        subs = {k for k, s in lago.get("subscriptions", {}).items()
                if s["external_customer_id"] == tid}
        _drop("lago_customers", lago.get("customers", {}),
              lambda c: c["external_id"] == tid)
        _drop("lago_subscriptions", lago.get("subscriptions", {}),
              lambda s: s["external_customer_id"] == tid)
        _drop("lago_events", lago.get("events", {}),
              lambda e: e["object"]["external_subscription_id"] in subs | cids)
        _drop("lago_plans", lago.get("plans", {}),
              lambda p: (p.get("metadata") or {}).get("contract_id") in cids)
        st = STORE.stripe
        cus = {k for k, c in st.get("customers", {}).items()
               if c["metadata"].get("tenant_id") == tid}
        _drop("stripe_customers", st.get("customers", {}),
              lambda c: c["id"] in cus)
        _drop("stripe_payment_intents", st.get("payment_intents", {}),
              lambda p: p.get("customer") in cus)
        _drop("stripe_payment_methods", st.get("payment_methods", {}),
              lambda p: p.get("customer") in cus)
        before = len(STORE.billing_webhooks)
        STORE.billing_webhooks[:] = [
            r for r in STORE.billing_webhooks
            if r["refs"].get("tenant_id") != tid
            and r["refs"].get("stripe_customer") not in cus
            and not (r["provider"] == "stripe"
                     and (r["body"]["data"]["object"].get("customer") in cus))]
        removed["webhooks"] = before - len(STORE.billing_webhooks)
        return {"tenant_id": tid, "removed": removed}
