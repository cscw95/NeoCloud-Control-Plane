"""F13 status-page notice types — incident · maintenance · grid_curtailment.

No upstream implementation exists (UPSTREAM-REFS.md §2: "F13 상태 공지 — 원본 없음,
NeoCloud 고유"). The wire shape is a NeoCloud extension schema,
`ref/upstream/neocloud-ext/status-notice.schema.json` (`x-neocloud`), copied to
`tests/contracts/status-notice.schema.json` for the contract test.

  POST /api/v1/status/notices          {kind, components, start, end?, message_ko, message_en}
                                       → 201 notice (operator only)
  GET  /api/v1/status/notices          ?kind=&state=  (public, like /api/v1/status)
  GET  /api/v1/status/notices/{id}
  POST /api/v1/status/notices/{id}/resolve   {end?}  (operator; NeoCloud ext)
  POST /api/v1/status/notices/demo/reset     drop source="demo" notices (demo runner)
  GET  /api/v1/status                  gains `notices` (scenario_api.status_page)

State is derived from the clock: `scheduled` (start in the future) → `active` →
`resolved` (end reached or resolved by hand). Python seams used by F21
curtailment and F22 leak response: `create_notice(body)` / `close_notice(id, end)`.
"""

from __future__ import annotations

import itertools
import threading
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import auth, events

router = APIRouter(prefix="/api/v1/status", tags=["status-notices"])

KINDS = ("incident", "maintenance", "grid_curtailment")
STATES = ("scheduled", "active", "resolved")
# component ids on the public page: the five legacy page components plus the
# three products sold (one account, one invoice)
COMPONENTS = ("Compute", "GPU Fabric", "Storage", "Network", "Portal",
              "bmaas", "managed-k8s", "token-api")
_IMPACT = {"incident": "degraded", "maintenance": "maintenance",
           "grid_curtailment": "degraded"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(v: Optional[str], field: str) -> Optional[datetime]:
    if v in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"{field} must be an ISO 8601 date-time")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class _Notices:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        self.items: dict[str, dict] = {}
        self._seq = itertools.count(1)

    def nid(self) -> str:
        return f"ntc-{next(self._seq):04d}"


NOTICES = _Notices()


def reset() -> None:
    with NOTICES.lock:
        NOTICES.reset()


def _state(n: dict, now: Optional[datetime] = None) -> str:
    if n.get("resolved_at"):
        return "resolved"
    now = now or _now()
    start, end = _parse(n["start"], "start"), _parse(n.get("end"), "end")
    if start and start > now:
        return "scheduled"
    if end and end <= now:
        return "resolved"
    return "active"


def _view(n: dict) -> dict:
    return {**n, "state": _state(n)}


class NoticeIn(BaseModel):
    kind: Literal["incident", "maintenance", "grid_curtailment"]
    components: list[str] = Field(min_length=1, max_length=16)
    start: Optional[str] = None
    end: Optional[str] = None
    message_ko: str = Field(min_length=1, max_length=2000)
    message_en: str = Field(min_length=1, max_length=2000)
    source: Optional[str] = Field(default=None, max_length=64)
    ref: Optional[str] = Field(default=None, max_length=128,
                               description="related object id (curtailment, incident, leak)")


class ResolveIn(BaseModel):
    end: Optional[str] = None


def _require_operator(request: Request) -> None:
    ident = getattr(request.state, "identity", None)
    if ident is not None and ident.role == "operator":
        return
    if ident is None and auth.dev_operator_default():
        return
    if ident is None:
        raise HTTPException(401, "authentication required")
    raise HTTPException(403, "operator only")


def create_notice(body) -> dict:
    """Python seam (F21/F22) and POST handler body. `body` = dict or NoticeIn."""
    b = body if isinstance(body, NoticeIn) else NoticeIn(**{
        k: v for k, v in dict(body).items() if k in NoticeIn.model_fields})
    bad = [c for c in b.components if c not in COMPONENTS]
    if bad:
        raise HTTPException(422, f"unknown component(s) {bad}; allowed {list(COMPONENTS)}")
    start = _parse(b.start, "start") or _now()
    end = _parse(b.end, "end")
    if end and end <= start:
        raise HTTPException(422, "end must be after start")
    now = _iso(_now())
    with NOTICES.lock:
        n = {"id": NOTICES.nid(), "kind": b.kind, "components": list(dict.fromkeys(b.components)),
             "impact": _IMPACT[b.kind], "start": _iso(start), "end": _iso(end) if end else None,
             "message_ko": b.message_ko, "message_en": b.message_en,
             "source": b.source or "operator", "ref": b.ref,
             "created_at": now, "updated_at": now, "resolved_at": None}
        NOTICES.items[n["id"]] = n
        out = _view(n)
    events.publish("grid" if b.kind == "grid_curtailment" else "health",
                   {"event": "StatusNoticeCreated", "notice_id": n["id"], "kind": b.kind,
                    "components": n["components"], "state": out["state"]},
                   source="nocp.status")
    return out


def close_notice(notice_id: Optional[str], end: Optional[str] = None) -> Optional[dict]:
    """Resolve a notice (idempotent). Unknown ids return None (seam callers ignore)."""
    if not notice_id:
        return None
    t = _parse(end, "end") or _now()
    with NOTICES.lock:
        n = NOTICES.items.get(notice_id)
        if n is None:
            return None
        if not n.get("resolved_at"):
            start = _parse(n["start"], "start")
            if start and t < start:
                t = start
            n.update(end=_iso(t), resolved_at=_iso(t), updated_at=_iso(_now()))
        out = _view(n)
    events.publish("grid" if n["kind"] == "grid_curtailment" else "health",
                   {"event": "StatusNoticeResolved", "notice_id": notice_id, "kind": n["kind"]},
                   source="nocp.status")
    return out


def list_notices(kind: Optional[str] = None, state: Optional[str] = None,
                 include_resolved: bool = True, limit: int = 50) -> list[dict]:
    with NOTICES.lock:
        out = [_view(n) for n in NOTICES.items.values()]
    if kind:
        out = [n for n in out if n["kind"] == kind]
    if state:
        out = [n for n in out if n["state"] == state]
    if not include_resolved:
        out = [n for n in out if n["state"] != "resolved"]
    order = {"active": 0, "scheduled": 1, "resolved": 2}
    out.sort(key=lambda n: (order[n["state"]], n["start"]), reverse=False)
    return out[:limit]


def page_notices() -> list[dict]:
    """What GET /api/v1/status shows: open notices first, then the 10 latest resolved."""
    open_ = list_notices(include_resolved=False, limit=50)
    done = sorted(list_notices(state="resolved", limit=1000),
                  key=lambda n: n.get("resolved_at") or n["start"], reverse=True)[:10]
    return open_ + done


@router.post("/notices", status_code=201)
def post_notice(body: NoticeIn, request: Request) -> dict:
    _require_operator(request)
    return create_notice(body)


@router.get("/notices")
def get_notices(kind: Optional[str] = None, state: Optional[str] = None,
                limit: int = 50) -> dict:
    if kind and kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {list(KINDS)}")
    if state and state not in STATES:
        raise HTTPException(422, f"state must be one of {list(STATES)}")
    items = list_notices(kind, state, limit=max(1, min(limit, 200)))
    return {"items": items, "count": len(items), "kinds": list(KINDS),
            "components": list(COMPONENTS)}


@router.get("/notices/{notice_id}")
def get_notice(notice_id: str) -> dict:
    with NOTICES.lock:
        n = NOTICES.items.get(notice_id)
        if n is None:
            raise HTTPException(404, f"notice '{notice_id}' not found")
        return _view(n)


@router.post("/notices/demo/reset")
def demo_reset(request: Request) -> dict:
    """Drop notices created by the demo runner (source "demo"); others stay."""
    _require_operator(request)
    with NOTICES.lock:
        gone = [k for k, n in NOTICES.items.items() if n.get("source") == "demo"]
        for k in gone:
            NOTICES.items.pop(k)
    return {"removed": len(gone)}


@router.post("/notices/{notice_id}/resolve")
def resolve_notice(notice_id: str, request: Request, body: Optional[ResolveIn] = None) -> dict:
    _require_operator(request)
    if notice_id not in NOTICES.items:
        raise HTTPException(404, f"notice '{notice_id}' not found")
    return close_notice(notice_id, body.end if body else None)


def component_impacts() -> dict[str, str]:
    """component → strongest open notice impact (for /metrics and the page)."""
    rank = {"maintenance": 1, "degraded": 2}
    out: dict[str, str] = {}
    for n in list_notices(state="active", limit=1000):
        for c in n["components"]:
            if rank[n["impact"]] > rank.get(out.get(c, ""), 0):
                out[c] = n["impact"]
    return out
