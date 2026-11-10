"""Bearer-token authentication for the NOCP API.

Identity = role `operator` | `tenant` (+ tenant_id, tenant_role admin|member|viewer).

Token sources, checked in order:
  1. `Authorization: Bearer <token>`
       - static map from env `NOCP_TOKENS`, either JSON
         `{"<token>": {"role": "tenant", "tenant_id": "tnt-a", "tenant_role": "admin"}}`
         or `<token>=operator,<token>=tenant:tnt-a[:viewer]`
       - HMAC-signed `nocp1.<payload>.<sig>` (SHA-256, key `NOCP_TOKEN_SECRET`; a
         per-process random key when unset) minted by `POST /api/v1/auth/tokens`
  2. `?token=` on the SSE stream only — signed tokens with scope `events`,
     issued by `POST /api/v1/events/token` (short-lived).
  3. Legacy headers `X-Tenant-Id` / `X-Tenant-Role` / `X-Operator: true` — honoured
     only when `NOCP_DEV_INSECURE_HEADERS=1` (tests, local demo).

Without an identity, `/api/*` answers 401 unless `NOCP_DEV_OPERATOR_DEFAULT=1`
(dev: implicit operator). Tokens are never logged or echoed back except by the
mint endpoints that create them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

_PROCESS_KEY = secrets.token_bytes(32)
TENANT_ROLES = ("admin", "member", "viewer")
EVENTS_TOKEN_TTL_S = 300


@dataclass(frozen=True)
class Identity:
    role: str                                 # operator | tenant
    tenant_id: Optional[str] = None
    tenant_role: str = "member"
    scope: str = "api"                        # api | events
    via: str = "token"                        # token | header | dev-default

    @property
    def is_operator(self) -> bool:
        return self.role == "operator"


def insecure_headers() -> bool:
    return os.environ.get("NOCP_DEV_INSECURE_HEADERS", "0") == "1"


def dev_operator_default() -> bool:
    return os.environ.get("NOCP_DEV_OPERATOR_DEFAULT", "0") == "1"


def _key() -> bytes:
    k = os.environ.get("NOCP_TOKEN_SECRET")
    return k.encode() if k else _PROCESS_KEY


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint(role: str, tenant_id: Optional[str] = None, tenant_role: str = "member",
         ttl_s: int = 3600, scope: str = "api") -> str:
    if role not in ("operator", "tenant"):
        raise ValueError("role must be operator|tenant")
    if role == "tenant" and not tenant_id:
        raise ValueError("tenant token needs tenant_id")
    payload = {"role": role, "tid": tenant_id, "trole": tenant_role,
               "scope": scope, "exp": int(time.time()) + int(ttl_s),
               "jti": secrets.token_hex(6)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    return f"nocp1.{body}.{sig}"


def _verify_signed(token: str) -> Optional[Identity]:
    try:
        prefix, body, sig = token.split(".")
    except ValueError:
        return None
    if prefix != "nocp1":
        return None
    good = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(good, sig):
        return None
    try:
        p = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(p.get("exp", 0)) < time.time():
        return None
    if p.get("role") not in ("operator", "tenant"):
        return None
    return Identity(role=p["role"], tenant_id=p.get("tid"),
                    tenant_role=p.get("trole") or "member",
                    scope=p.get("scope") or "api")


def _static_map() -> dict:
    raw = os.environ.get("NOCP_TOKENS", "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            return {k: v for k, v in json.loads(raw).items() if isinstance(v, dict)}
        except json.JSONDecodeError:
            return {}
    out = {}
    for item in raw.split(","):
        tok, _, spec = item.strip().partition("=")
        parts = spec.split(":")
        if not tok or not parts[0]:
            continue
        out[tok] = {"role": parts[0], "tenant_id": parts[1] if len(parts) > 1 else None,
                    "tenant_role": parts[2] if len(parts) > 2 else "member"}
    return out


def _verify_static(token: str) -> Optional[Identity]:
    for tok, spec in _static_map().items():
        if hmac.compare_digest(tok.encode(), token.encode()):
            role = spec.get("role")
            if role not in ("operator", "tenant"):
                return None
            if role == "tenant" and not spec.get("tenant_id"):
                return None
            return Identity(role=role, tenant_id=spec.get("tenant_id"),
                            tenant_role=spec.get("tenant_role") or "member")
    return None


def verify(token: str) -> Optional[Identity]:
    return _verify_static(token) or _verify_signed(token)


class AuthError(Exception):
    pass


def resolve(request: Request, sse_paths: frozenset) -> Optional[Identity]:
    """Identity for a request; raises AuthError for a bad presented credential."""
    authz = request.headers.get("authorization", "")
    if authz:
        scheme, _, tok = authz.partition(" ")
        if scheme.lower() != "bearer" or not tok.strip():
            raise AuthError("invalid authorization header")
        ident = verify(tok.strip())
        if ident is None or ident.scope != "api":
            raise AuthError("invalid or expired token")
        return ident
    qtok = request.query_params.get("token")
    if qtok and request.method == "GET" and request.url.path in sse_paths:
        ident = _verify_signed(qtok)
        if ident is None or ident.scope != "events":
            raise AuthError("invalid or expired events token")
        return ident
    if insecure_headers():
        tid = request.headers.get("x-tenant-id")
        if tid:
            role = (request.headers.get("x-tenant-role") or "member").strip().lower()
            return Identity(role="tenant", tenant_id=tid, tenant_role=role, via="header")
        if (request.headers.get("x-operator") or "").strip().lower() in ("true", "1"):
            return Identity(role="operator", via="header")
    return None


# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/v1", tags=["auth"])


class TokenRequest(BaseModel):
    role: str = Field(..., pattern="^(operator|tenant)$")
    tenant_id: Optional[str] = None
    tenant_role: str = Field("member", pattern="^(admin|member|viewer)$")
    ttl_s: int = Field(3600, ge=60, le=86400)


def _caller(request: Request) -> Optional[Identity]:
    return getattr(request.state, "identity", None)


@router.post("/auth/tokens", status_code=201)
def issue_token(body: TokenRequest, request: Request) -> dict:
    """Operator-only: mint a signed API token (returned once, never stored)."""
    who = _caller(request)
    if who is None or not who.is_operator:
        raise HTTPException(403, "operator identity required")
    if body.role == "tenant":
        from .store import STORE
        if not body.tenant_id:
            raise HTTPException(422, "tenant token needs tenant_id")
        if body.tenant_id not in STORE.tenants:
            raise HTTPException(404, f"tenant '{body.tenant_id}' not found")
    tok = mint(body.role, body.tenant_id, body.tenant_role, body.ttl_s)
    return {"token": tok, "token_type": "Bearer", "expires_in": body.ttl_s,
            "role": body.role, "tenant_id": body.tenant_id}


@router.post("/events/token", status_code=201)
def issue_events_token(request: Request) -> dict:
    """Short-lived `?token=` for the SSE stream (EventSource cannot set headers)."""
    who = _caller(request)
    if who is None or not who.is_operator:
        raise HTTPException(403, "operator identity required")
    tok = mint("operator", ttl_s=EVENTS_TOKEN_TTL_S, scope="events")
    return {"token": tok, "expires_in": EVENTS_TOKEN_TTL_S,
            "usage": "GET /api/v1/events?token=<token>"}


@router.get("/auth/whoami")
def whoami(request: Request) -> dict:
    who = _caller(request)
    if who is None:
        return {"authenticated": False}
    return {"authenticated": True, "role": who.role, "tenant_id": who.tenant_id,
            "tenant_role": who.tenant_role, "via": who.via}
