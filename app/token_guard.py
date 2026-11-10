"""F03 input/output safety guard (emulated) for the token gateway.

Stands in for the guard models the DSX deck names: Kanana Safeguard (input,
Korean) and Qwen3Guard-Stream (output, token-by-token). Both are emulated by
rules (keyword + PII regex); no model runs. Category names follow the
Qwen3Guard safety taxonomy (QwenLM/Qwen3Guard README, Apache-2.0: Violent,
Non-violent Illegal Acts, Sexual Content or Sexual Acts, PII, Suicide &
Self-Harm, Unethical Acts, Politically Sensitive Topics, Copyright Violation,
Jailbreak); Kanana Safeguard is modelled from public documentation only
(공개 문서 기준). The streaming check follows Qwen3Guard-Stream's flow: the
guard scores each new token against the text so far and the caller stops the
stream at the first unsafe token (finish_reason "content_filter").

Policy per tenant: mode off|monitor|enforce, categories, optional
blocked_terms (tenant custom deny list, like Azure/Envoy AI Gateway custom
block lists). Public-sector (공공) tenants are pinned to enforce with every
category; changing that is refused (409).

  GET  /api/v1/token/guard-policy/{tenant_id}
  PUT  /api/v1/token/guard-policy/{tenant_id} {mode, categories:[...], blocked_terms?}
  GET  /api/v1/token/guard-events?tenant_id=&stage=&limit=
  POST /api/v1/token/guard/check {tenant_id, text, stage}   (dry run, no event)

Blocked requests answer 400 {error:{type:"content_filter", stage, code,
message, param}} (OpenAI error envelope + stage); see token_api.py.
"""

from __future__ import annotations

import itertools
import re
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .store import STORE

router = APIRouter(prefix="/api/v1/token", tags=["token-guard"])

MODES = ("off", "monitor", "enforce")
STAGES = ("input", "output")
DEFAULT_MODE = "monitor"
EVENTS_MAX = 2000
STREAM_WINDOW = 256          # chars of trailing text rescored per streamed token

# category → patterns (case-insensitive). Kept small and deterministic: this is
# a demo stand-in, not a classifier.
_RULES: dict[str, list[str]] = {
    "violent": [r"make (a|an) (bomb|explosive)", r"폭탄\s*(제조|만드는)", r"총기\s*개조",
                r"kill (him|her|them|someone)"],
    "illegal_acts": [r"마약\s*(제조|구매|판매)", r"buy (cocaine|heroin|meth)",
                     r"launder(ing)? money", r"자금\s*세탁"],
    "sexual": [r"\bexplicit sex", r"성\s*착취"],
    "pii": [r"\b\d{6}-[1-4]\d{6}\b",                        # 주민등록번호
            r"\b01[016789]-?\d{3,4}-?\d{4}\b",              # KR mobile
            r"\b(?:\d{4}[- ]){3}\d{4}\b",                   # card number
            r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"],               # e-mail
    "self_harm": [r"how to (commit )?suicide", r"자살\s*방법", r"자해\s*방법"],
    "unethical": [r"write (a )?phishing", r"피싱\s*(문자|메일)\s*작성"],
    "political": [r"election (rigging|fraud) (plan|guide)", r"선거\s*조작\s*방법"],
    "copyright": [r"full (text|lyrics) of", r"전문을?\s*그대로\s*(출력|복사)"],
    "jailbreak": [r"ignore (all )?(previous|prior) instructions", r"\bDAN mode\b",
                  r"이전\s*지시(를|는)?\s*무시", r"탈옥"],
}
CATEGORIES = tuple(_RULES)
_CUSTOM = ("custom", "blocked_terms")
_COMPILED = {c: [re.compile(p, re.I) for p in ps] for c, ps in _RULES.items()}
_PUBLIC_PREFIXES = ("gov-", "public-", "tnt-gov-", "tnt-public-")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Guard:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.RLock()):
            self.policies: dict[str, dict] = {}
            self.public: set[str] = set()
            self.events: deque = deque(maxlen=EVENTS_MAX)
            self._seq = itertools.count(1)


GUARD = _Guard()


def reset() -> None:
    GUARD.reset()


def mark_public(tenant_id: str) -> None:
    """F07 hook: a public-sector tenant is pinned to enforce."""
    with GUARD.lock:
        GUARD.public.add(tenant_id)


def is_public(tenant_id: Optional[str]) -> bool:
    """Public-sector tenant? Explicit mark (F07) or tenant attribute
    sector/tenant_type == public|gov; until F07 adds the field, a tenant id or
    name starting gov-/public- (stub rule)."""
    if not tenant_id:
        return False
    if tenant_id in GUARD.public:
        return True
    t = STORE.tenants.get(tenant_id)
    for attr in ("sector", "tenant_type", "segment"):
        v = getattr(t, attr, None) if t is not None else None
        if isinstance(v, str) and v.lower() in ("public", "gov", "government", "공공"):
            return True
    names = [tenant_id.lower()] + ([t.name.lower()] if t is not None else [])
    return any(n.startswith(_PUBLIC_PREFIXES) for n in names)


def policy(tenant_id: Optional[str]) -> dict:
    with GUARD.lock:
        p = GUARD.policies.get(tenant_id or "")
        if is_public(tenant_id):
            return {"tenant_id": tenant_id, "mode": "enforce",
                    "categories": list(CATEGORIES),
                    "blocked_terms": list((p or {}).get("blocked_terms", [])),
                    "public_sector": True, "locked": True,
                    "updated_at": (p or {}).get("updated_at")}
        if p is None:
            return {"tenant_id": tenant_id, "mode": DEFAULT_MODE,
                    "categories": list(CATEGORIES), "blocked_terms": [],
                    "public_sector": False, "locked": False, "updated_at": None}
        return {**p, "public_sector": False, "locked": False}


def scan(text: str, categories, blocked_terms=()) -> list[str]:
    """Categories hit by text (rule order); 'custom' for a blocked term."""
    hits = [c for c in categories if c in _COMPILED
            and any(rx.search(text) for rx in _COMPILED[c])]
    low = text.lower()
    if any(t and t.lower() in low for t in blocked_terms):
        hits.append("custom")
    return hits


def check(tenant_id: Optional[str], text: str, stage: str, *,
          request_id: Optional[str] = None, record: bool = True) -> dict:
    """Verdict for one text at one stage. action: allow | flag | block.
    Qwen3Guard-like label: Safe | Unsafe."""
    p = policy(tenant_id)
    if p["mode"] == "off":
        return {"stage": stage, "mode": "off", "action": "allow",
                "label": "Safe", "categories": []}
    hits = scan(text, p["categories"], p.get("blocked_terms") or ())
    action = "allow" if not hits else ("block" if p["mode"] == "enforce" else "flag")
    verdict = {"stage": stage, "mode": p["mode"], "action": action,
               "label": "Unsafe" if hits else "Safe", "categories": hits}
    if hits and record:
        with GUARD.lock:
            GUARD.events.append({"seq": next(GUARD._seq), "at": _now(),
                                 "tenant_id": tenant_id, "request_id": request_id,
                                 **verdict, "sample": text[-120:]})
    return verdict


class StreamGuard:
    """Qwen3Guard-Stream style: feed tokens; returns the verdict on the first
    unsafe token (stream must stop there when action == block)."""

    def __init__(self, tenant_id: Optional[str], request_id: Optional[str] = None):
        self.tenant_id, self.request_id = tenant_id, request_id
        self.p = policy(tenant_id)
        self.text = ""
        self.flagged: Optional[dict] = None

    def feed(self, token: str) -> Optional[dict]:
        self.text += token
        if self.p["mode"] == "off" or self.flagged is not None:
            return None
        tail = self.text[-STREAM_WINDOW:]
        if not scan(tail, self.p["categories"], self.p.get("blocked_terms") or ()):
            return None
        self.flagged = check(self.tenant_id, tail, "output", request_id=self.request_id)
        return self.flagged


# ---------------------------------------------------------------------------
class PolicyBody(BaseModel):
    mode: str = Field(..., pattern="^(off|monitor|enforce)$")
    categories: Optional[list[str]] = None
    blocked_terms: list[str] = Field(default_factory=list, max_length=200)


class CheckBody(BaseModel):
    tenant_id: Optional[str] = None
    text: str = Field(..., max_length=200_000)
    stage: str = Field("input", pattern="^(input|output)$")


def _scope(request: Request, tenant_id: Optional[str]) -> None:
    ident = getattr(request.state, "identity", None)
    if ident is not None and ident.role == "tenant" and tenant_id != ident.tenant_id:
        raise HTTPException(403, "tenant scope violation")


@router.get("/guard-policy/{tenant_id}")
def get_policy(tenant_id: str, request: Request):
    _scope(request, tenant_id)
    return policy(tenant_id)


@router.put("/guard-policy/{tenant_id}")
def put_policy(tenant_id: str, body: PolicyBody, request: Request):
    _scope(request, tenant_id)
    if tenant_id not in STORE.tenants:
        raise HTTPException(404, f"tenant {tenant_id} not found")
    cats = list(CATEGORIES) if body.categories is None else body.categories
    # "custom"/"blocked_terms" name the tenant deny list (always applied when set)
    cats = [c for c in cats if c not in _CUSTOM]
    bad = [c for c in cats if c not in CATEGORIES]
    if bad:
        raise HTTPException(422, f"unknown categories {bad}; allowed {list(CATEGORIES)}")
    terms = [t.strip() for t in body.blocked_terms if t.strip()]
    if is_public(tenant_id) and (body.mode != "enforce" or set(cats) != set(CATEGORIES)):
        raise HTTPException(409, "public-sector tenant guard policy is fixed to "
                                 "enforce with all categories")
    with GUARD.lock:
        GUARD.policies[tenant_id] = {"tenant_id": tenant_id, "mode": body.mode,
                                     "categories": cats, "blocked_terms": terms,
                                     "updated_at": _now()}
    return policy(tenant_id)


@router.get("/guard-events")
def guard_events(request: Request, tenant_id: Optional[str] = None,
                 stage: Optional[str] = None, limit: int = 100):
    ident = getattr(request.state, "identity", None)
    if ident is not None and ident.role == "tenant":
        tenant_id = ident.tenant_id
    with GUARD.lock:
        rows = [e for e in GUARD.events
                if (not tenant_id or e["tenant_id"] == tenant_id)
                and (not stage or e["stage"] == stage)]
    rows = rows[-max(1, min(limit, EVENTS_MAX)):]
    return {"count": len(rows), "categories": list(CATEGORIES), "events": rows}


@router.post("/guard/check")
def guard_check(body: CheckBody, request: Request):
    if body.tenant_id:
        _scope(request, body.tenant_id)
    return {"tenant_id": body.tenant_id, "policy": policy(body.tenant_id),
            **check(body.tenant_id, body.text, body.stage, record=False)}
