"""Business/Customer 포털 북바운드 (M5·M7-lite) — 티켓 · 사용량/과금 프리뷰.

- 티켓: 고객 포털에서 생성, 운영/비즈 포털에서 상태 전이(open→in_progress→
  resolved)와 코멘트를 관리한다.
- 과금 프리뷰: 인도(delivered)된 신규 주문을 라인아이템으로, rack-hour 단가
  (데모 단가)를 곱해 산출한다. 종료 시각은 해당 주문의 allocation 전부를
  회수한 terminate 주문의 closed 시각(부분 회수는 데모 단순화로 미반영),
  활성 라인은 현재 시각 기준 + 월 환산(projected monthly)을 함께 준다.
  실구현은 M7 미디에이션(시간별 사용 레코드 → 요율 엔진)으로 대체된다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from .models import (
    OrderKind,
    OrderState,
    Ticket,
    TicketComment,
    TicketCreate,
    TicketUpdate,
)
from .store import STORE

router = APIRouter(prefix="/api/v1", tags=["business"])

# 데모 단가 (USD / rack-hour) — 상용 요율은 계약별 요율 엔진에서 산출
RACK_HOUR_RATE_USD = {
    "vr-nvl72": 980.0,
    "gb300-nvl72": 720.0,
    "gb200-nvl72": 560.0,
}
HOURS_PER_MONTH = 720

TICKET_STATUSES = ("open", "in_progress", "resolved")
TICKET_SEVERITIES = ("low", "medium", "high", "critical")
# 티켓 유형·라우팅 (CP-005/006/009) — tech/change → 운영(ops),
# billing_dispute(청구 이의)·계약 → 사업(biz)
TICKET_TYPES = ("tech", "change", "billing_dispute")
TICKET_ROUTING = {"tech": "ops", "change": "ops", "billing_dispute": "biz"}
CHANGE_SCOPES = ("in_contract", "contract_amendment")
BILLING_DISPUTE_POLICY = ("납기 유지·차기 청구 조정 원칙"
                          "(명백한 금액 오류만 재발행)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 티켓
# ---------------------------------------------------------------------------
@router.post("/tickets", status_code=201, response_model=Ticket)
def create_ticket(body: TicketCreate) -> Ticket:
    s = STORE
    with s.lock:
        if body.tenant_id not in s.tenants:
            raise HTTPException(404, f"tenant '{body.tenant_id}' not found")
        if body.severity not in TICKET_SEVERITIES:
            raise HTTPException(422, f"severity must be one of {TICKET_SEVERITIES}")
        if body.type not in TICKET_TYPES:
            raise HTTPException(422, f"type must be one of {TICKET_TYPES}")
        if body.change_scope is not None:
            if body.type != "change":
                raise HTTPException(422, "change_scope는 type='change' 티켓 전용")
            if body.change_scope not in CHANGE_SCOPES:
                raise HTTPException(422,
                                    f"change_scope must be one of {CHANGE_SCOPES}")
        tid = f"tck-{s.next_seq('ticket')}"
        now = _now()
        ticket = Ticket(
            id=tid, tenant_id=body.tenant_id, subject=body.subject,
            body=body.body, severity=body.severity, ref=body.ref,
            type=body.type, routed_to=TICKET_ROUTING[body.type],
            change_scope=(body.change_scope
                          or ("in_contract" if body.type == "change" else None)),
            policy=(BILLING_DISPUTE_POLICY
                    if body.type == "billing_dispute" else None),
            created_at=now, updated_at=now)
        s.tickets[tid] = ticket
        return ticket


@router.get("/tickets")
def list_tickets(tenant_id: Optional[str] = None,
                 status: Optional[str] = None,
                 type: Optional[str] = None,
                 routed_to: Optional[str] = None) -> list:
    out = list(STORE.tickets.values())
    if tenant_id:
        out = [t for t in out if t.tenant_id == tenant_id]
    if status:
        out = [t for t in out if t.status == status]
    if type:
        out = [t for t in out if t.type == type]
    if routed_to:
        out = [t for t in out if t.routed_to == routed_to]
    return sorted(out, key=lambda t: t.id, reverse=True)


@router.patch("/tickets/{ticket_id}", response_model=Ticket)
def update_ticket(ticket_id: str, body: TicketUpdate) -> Ticket:
    ticket = STORE.tickets.get(ticket_id)
    if not ticket:
        raise HTTPException(404, f"ticket '{ticket_id}' not found")
    if body.status:
        if body.status not in TICKET_STATUSES:
            raise HTTPException(422, f"status must be one of {TICKET_STATUSES}")
        ticket.status = body.status
    if body.comment:
        ticket.comments.append(TicketComment(
            at=_now(), author=body.author, text=body.comment))
    ticket.updated_at = _now()
    return ticket


# ---------------------------------------------------------------------------
# 사용량 · 과금 프리뷰
# ---------------------------------------------------------------------------
@router.get("/billing/rates")
def billing_rates() -> dict:
    return {"currency": "USD", "unit": "rack-hour",
            "rates": RACK_HOUR_RATE_USD, "note": "데모 단가 — 계약별 요율은 M7"}


@router.get("/billing/usage")
def billing_usage(tenant_id: Optional[str] = None) -> dict:
    s = STORE
    now = datetime.now(timezone.utc)
    # allocation → 회수 시각 (terminate closed)
    end_by_alloc: dict[str, str] = {}
    for o in s.orders.values():
        if (o.kind == OrderKind.terminate and o.state == OrderState.closed
                and o.allocation_id and o.history):
            end_by_alloc[o.allocation_id] = o.history[-1].at

    lines = []
    for o in sorted(s.orders.values(), key=lambda x: x.id):
        if o.kind != OrderKind.new or o.state != OrderState.delivered:
            continue                       # failed/rejected 주문은 과금 없음
        if tenant_id and o.tenant_id != tenant_id:
            continue
        start = next((e.at for e in o.history if e.state == "delivered"), None)
        if not start:
            continue
        ends = [end_by_alloc[a] for a in o.allocation_ids if a in end_by_alloc]
        ended = bool(ends) and len(ends) == len(o.allocation_ids)
        end_at = max(ends) if ended else None
        end_dt = (datetime.fromisoformat(end_at) if end_at else now)
        hours = max(0.0, (end_dt - datetime.fromisoformat(start))
                    .total_seconds() / 3600)
        rate = RACK_HOUR_RATE_USD.get(o.blueprint_key or "", 600.0)
        rack_hours = o.racks * hours
        lines.append({
            "order_id": o.id, "tenant_id": o.tenant_id,
            "blueprint_key": o.blueprint_key, "racks": o.racks,
            "start": start, "end": end_at, "active": not ended,
            "hours": round(hours, 4), "rack_hours": round(rack_hours, 4),
            "rate_usd": rate, "amount_usd": round(rack_hours * rate, 2),
            "projected_monthly_usd": (round(o.racks * rate * HOURS_PER_MONTH, 2)
                                      if not ended else 0.0),
        })
    return {
        "tenant_id": tenant_id, "generated_at": _now(), "lines": lines,
        "totals": {
            "rack_hours": round(sum(l["rack_hours"] for l in lines), 4),
            "amount_usd": round(sum(l["amount_usd"] for l in lines), 2),
            "projected_monthly_usd": round(
                sum(l["projected_monthly_usd"] for l in lines), 2),
        },
    }
