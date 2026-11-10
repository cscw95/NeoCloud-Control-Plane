"""F07 — self signup, organizations, users, roles, in-memory OIDC issuer (JWT).

Upstream model (ref/upstream/UPSTREAM-REFS.md §1, §3.1): Keycloak 26.8.2
(keycloak/keycloak e514445f9dcd)
  - discovery  services/src/main/java/org/keycloak/protocol/oidc/OIDCWellKnownProvider.java
  - token      services/src/main/java/org/keycloak/protocol/oidc/endpoints/TokenEndpoint.java
  - routes     services/src/main/java/org/keycloak/protocol/oidc/OIDCLoginProtocolService.java
Specs: OIDC Discovery 1.0 §3, OIDC Core 1.0, RFC 6749 §4.3/§4.4/§6/§5.2, RFC 7517, RFC 7519.

Paths follow Keycloak (`/realms/{realm}/.well-known/openid-configuration`,
`/realms/{realm}/protocol/openid-connect/{token,certs,auth,userinfo,logout}`); the plan's
`/.well-known/openid-configuration` (platform realm), `/oauth2/jwks`, `/oauth2/token`
are aliases (WAVE-v2 decision 2). The realm of an alias token request is taken from
its client_id (`<tenant>-portal`, `<tenant>-api`, `nocp-ops`).

Flow (NeoCloud, §3.1 step 1–5):
  POST /api/v1/signup → tenant (tenancy.create_tenant: VRF/VNI + IAM realm) + admin
  user, status `pending_verification` → operator `POST /api/v1/signup/{tid}/verify`
  → `active`. Until active, order-type POSTs for the tenant answer 403 (SignupGate).
  The realm and admin exist at once so the admin can sign in and look around.

Keycloak is emulated in memory (constraint 3): `KeycloakEmu` keeps realms, clients
(`ClientRepresentation`-like) and users (`UserRepresentation`-like). The adapter
boundary is that class; a real Keycloak would be driven through its admin REST API
(`POST /admin/realms`, `POST /admin/realms/{realm}/users`, [미검증] per refs).

Signing (env): NOCP_JWT_ALG = HS256 (default, secret NOCP_JWT_SECRET, else
NOCP_TOKEN_SECRET, else a per-process key) | RS256 (pure-Python RSA key, published in
JWKS). HS256 keys are never published (Keycloak certs list only asymmetric keys), so
verifiers share the secret (NICo: EMU_JWT_KEY). Issuer base: NOCP_PUBLIC_URL, else
the request's base URL. Lifetimes: NOCP_JWT_TTL_S (300, Keycloak accessTokenLifespan),
refresh 1800 s (ssoSessionIdleTimeout).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import jwt_lite
from .store import STORE

PLATFORM_REALM = "neocloud"
OPS_CLIENT = "nocp-ops"
TENANT_ROLES = ("admin", "member", "viewer")
ROLE_INFO = {
    "admin": ("조직 관리자: 사용자·SSO·API 키·주문", "Org admin: users, SSO, API keys, orders",
              ["read", "write", "order", "manage_users", "manage_sso"]),
    "member": ("구성원: 조회·주문·API 키", "Member: read, order, API keys", ["read", "write", "order"]),
    "viewer": ("조회 전용", "Read only", ["read"]),
}
REFRESH_TTL_S = 1800
GRANTS = ("password", "client_credentials", "refresh_token")
_BRN_RE = re.compile(r"^(\d{3})-?(\d{2})-?(\d{5})$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PBKDF2_ITERS = 60_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _alg() -> str:
    a = os.environ.get("NOCP_JWT_ALG", "HS256").strip().upper()
    return a if a in ("HS256", "RS256") else "HS256"


def _ttl() -> int:
    try:
        return max(30, int(os.environ.get("NOCP_JWT_TTL_S", "300")))
    except ValueError:
        return 300


def brn_checksum_ok(brn: str) -> bool:
    """Korean business registration number check digit (국세청 검증식)."""
    d = [int(c) for c in re.sub(r"\D", "", brn)]
    if len(d) != 10:
        return False
    w = (1, 3, 7, 1, 3, 7, 1, 3, 5)
    s = sum(a * b for a, b in zip(d[:9], w)) + (d[8] * 5) // 10
    return (10 - s % 10) % 10 == d[9]


def _hash_pw(pw: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(12)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _PBKDF2_ITERS)
    return f"pbkdf2-sha256${salt.hex()}${dk.hex()}"


def _check_pw(pw: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    _, salt, dk = stored.split("$")
    return hmac.compare_digest(_hash_pw(pw, bytes.fromhex(salt)).split("$")[2], dk)


# ── in-memory Keycloak (adapter boundary) ──────────────────────────────────

class KeycloakEmu:
    def __init__(self):
        self.lock = threading.RLock()
        self._secret = secrets.token_bytes(32)
        self._rsa: Optional[jwt_lite.RSAKey] = None
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.realms: dict[str, dict] = {}
            self.users: dict[str, dict[str, dict]] = {}      # realm -> user id -> user
            self.clients: dict[str, dict] = {}               # client_id -> client
            self.signups: dict[str, dict] = {}               # tenant id -> signup record
            self.sso: dict[str, dict] = {}                   # tenant id -> IdP config
            self.refresh_jti: dict[str, dict] = {}           # live refresh tokens
            self._ensure_realm(PLATFORM_REALM, "NeoCloud operators")
            secret = os.environ.get("NOCP_OIDC_OPS_SECRET") or secrets.token_urlsafe(24)
            self._add_client(PLATFORM_REALM, OPS_CLIENT, confidential=True,
                             secret=secret, service_role="operator")
            pw = os.environ.get("NOCP_OIDC_OPERATOR_PASSWORD")
            if pw:
                self.create_user(PLATFORM_REALM, "ops-admin", "ops-admin@neocloud.local",
                                 "NeoCloud Operator", "operator", password=pw)

    # keys
    def secret(self) -> bytes:
        s = os.environ.get("NOCP_JWT_SECRET") or os.environ.get("NOCP_TOKEN_SECRET")
        return s.encode() if s else self._secret

    def rsa(self) -> jwt_lite.RSAKey:
        with self.lock:
            if self._rsa is None:
                self._rsa = jwt_lite.RSAKey.generate(2048)
            return self._rsa

    def jwks(self) -> dict:
        return {"keys": [self.rsa().public_jwk()] if _alg() == "RS256" else []}

    # realms / clients / users
    def _ensure_realm(self, realm: str, display: str) -> dict:
        if realm not in self.realms:
            self.realms[realm] = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, realm)),
                                  "realm": realm, "displayName": display, "enabled": True,
                                  "accessTokenLifespan": _ttl(),
                                  "ssoSessionIdleTimeout": REFRESH_TTL_S}
            self.users[realm] = {}
        return self.realms[realm]

    def _add_client(self, realm: str, client_id: str, *, confidential: bool,
                    secret: Optional[str] = None, service_role: Optional[str] = None) -> dict:
        c = {"id": str(uuid.uuid4()), "clientId": client_id, "realm": realm,
             "publicClient": not confidential,
             "serviceAccountsEnabled": confidential,
             "directAccessGrantsEnabled": True,
             "secret_hash": _hash_pw(secret) if secret else None,
             "service_role": service_role}
        self.clients[client_id] = c
        return c

    def create_tenant_realm(self, tid: str, display: str) -> dict:
        """POST /admin/realms + clients `<tid>-portal` (public) and `<tid>-api`
        (confidential, service account). Returns the api client secret once."""
        with self.lock:
            realm = self._ensure_realm(tid, display)
            self._add_client(tid, f"{tid}-portal", confidential=False)
            api_secret = secrets.token_urlsafe(24)
            self._add_client(tid, f"{tid}-api", confidential=True, secret=api_secret,
                             service_role="member")
            return {"realm": realm, "client_id": f"{tid}-api", "client_secret": api_secret}

    def create_user(self, realm: str, username: str, email: str, name: str, role: str,
                    password: Optional[str] = None, federated: Optional[str] = None) -> tuple[dict, Optional[str]]:
        """POST /admin/realms/{realm}/users — UserRepresentation fields."""
        with self.lock:
            users = self.users[realm]
            if any(u["username"] == username.lower() for u in users.values()):
                raise HTTPException(409, "User exists with same username")
            initial = None
            if password is None and not federated:
                initial = password = secrets.token_urlsafe(12)
            first, _, last = name.partition(" ")
            u = {"id": str(uuid.uuid4()), "username": username.lower(), "email": email.lower(),
                 "firstName": first, "lastName": last, "enabled": True,
                 "emailVerified": bool(federated), "createdTimestamp": _now_ms(),
                 "realmRoles": [role], "attributes": {"tenant_id": [realm]},
                 "federationLink": federated, "_pw": _hash_pw(password) if password else None}
            users[u["id"]] = u
            if realm != PLATFORM_REALM:
                STORE.members[u["id"]] = {"member_id": u["id"], "tenant_id": realm,
                                          "email": u["email"], "role": role, "state": "active",
                                          "invited_at": _now_iso(), "mfa": False,
                                          "source": "sso" if federated else "iam"}
            return u, initial

    def find_user(self, realm: str, username: str) -> Optional[dict]:
        name = username.lower()
        return next((u for u in self.users.get(realm, {}).values()
                     if name in (u["username"], u["email"])), None)

    # tokens
    def issuer(self, base: str, realm: str) -> str:
        return f"{base.rstrip('/')}/realms/{realm}"

    def _sign(self, claims: dict) -> str:
        if _alg() == "RS256":
            return jwt_lite.encode(claims, alg="RS256", rsa=self.rsa())
        return jwt_lite.encode(claims, alg="HS256", secret=self.secret(), kid="hs-dev")

    def issue(self, base: str, realm: str, client_id: str, subject: dict,
              scope: str, with_refresh: bool, sid: Optional[str] = None) -> dict:
        """Token response (RFC 6749 §5.1 + Keycloak fields). `subject`:
        {sub, username, email?, name?, role}."""
        now = int(time.time())
        iss = self.issuer(base, realm)
        role = subject["role"]
        sid = sid or str(uuid.uuid4())
        claims = {"exp": now + _ttl(), "iat": now, "jti": str(uuid.uuid4()), "iss": iss,
                  "aud": ["nocp", "nico"], "sub": subject["sub"], "typ": "Bearer",
                  "azp": client_id, "sid": sid, "scope": scope,
                  "realm_access": {"roles": [role, f"default-roles-{realm}"]},
                  "preferred_username": subject["username"]}
        if subject.get("email"):
            claims["email"] = subject["email"]
            claims["email_verified"] = True
        if subject.get("name"):
            claims["name"] = subject["name"]
        if realm != PLATFORM_REALM:
            claims["tid"] = realm
            claims["trole"] = role
        out = {"access_token": self._sign(claims), "expires_in": _ttl(),
               "refresh_expires_in": 0, "token_type": "Bearer",
               "not-before-policy": 0, "scope": scope}
        if "openid" in scope.split():
            idc = {k: claims[k] for k in ("exp", "iat", "iss", "sub", "sid",
                                          "preferred_username") if k in claims}
            idc.update({"jti": str(uuid.uuid4()), "aud": client_id, "typ": "ID", "azp": client_id})
            for k in ("email", "name", "tid"):
                if k in claims:
                    idc[k] = claims[k]
            out["id_token"] = self._sign(idc)
        if with_refresh:
            rjti = str(uuid.uuid4())
            rc = {"exp": now + REFRESH_TTL_S, "iat": now, "jti": rjti, "iss": iss, "aud": iss,
                  "sub": subject["sub"], "typ": "Refresh", "azp": client_id, "sid": sid,
                  "scope": scope}
            self.refresh_jti[rjti] = {"realm": realm, "client_id": client_id,
                                      "subject": subject, "sid": sid}
            out["refresh_token"] = self._sign(rc)
            out["refresh_expires_in"] = REFRESH_TTL_S
            out["session_state"] = sid
        return out

    def verify(self, token: str, typ: str = "Bearer") -> dict:
        claims = jwt_lite.decode(token, secret=self.secret(),
                                 keys=[self.rsa()] if self._rsa is not None else [],
                                 algorithms=("HS256", "RS256"))
        if claims.get("typ") != typ:
            raise jwt_lite.JWTError("typ", "wrong token type")
        m = re.search(r"/realms/([^/]+)$", claims.get("iss") or "")
        if not m or m.group(1) not in self.realms:
            raise jwt_lite.JWTError("issuer", "unknown realm")
        return claims


KC = KeycloakEmu()


def reset() -> None:
    KC.reset()


def identity_from_jwt(token: str):
    """auth.verify hook: JWT access token → auth.Identity, or None."""
    from .auth import Identity
    if not jwt_lite.looks_like_jwt(token):
        return None
    try:
        c = KC.verify(token)
    except jwt_lite.JWTError:
        return None
    roles = (c.get("realm_access") or {}).get("roles") or []
    if c.get("tid"):
        trole = c.get("trole") if c.get("trole") in TENANT_ROLES else "viewer"
        return Identity(role="tenant", tenant_id=c["tid"], tenant_role=trole, via="jwt")
    if "operator" in roles and c["iss"].endswith(f"/realms/{PLATFORM_REALM}"):
        return Identity(role="operator", via="jwt")
    return None


def tenant_status(tid: Optional[str]) -> Optional[str]:
    """Signup status for a tenant made by signup; None for legacy tenants (active)."""
    if not tid:
        return None
    rec = KC.signups.get(tid)
    if rec is None or tid not in STORE.tenants:
        return None
    return rec["status"]


# ── HTTP ───────────────────────────────────────────────────────────────────
router = APIRouter(tags=["identity"])


def _base(request: Request) -> str:
    return (os.environ.get("NOCP_PUBLIC_URL") or str(request.base_url)).rstrip("/")


def _who(request: Request):
    return getattr(request.state, "identity", None)


def _dev_operator():
    from .auth import Identity, dev_operator_default
    return Identity(role="operator", via="dev-default") if dev_operator_default() else None


def _require_operator(request: Request):
    who = _who(request) or _dev_operator()
    if who is None or not who.is_operator:
        raise HTTPException(403, "operator identity required")
    return who


def _require_tenant_admin(request: Request, tid: str):
    who = _who(request) or _dev_operator()
    if who is not None and who.is_operator:
        return who
    if who is not None and who.tenant_id == tid and who.tenant_role == "admin":
        return who
    raise HTTPException(403, "tenant admin required")


def _require_tenant_reader(request: Request, tid: str):
    who = _who(request) or _dev_operator()
    if who is not None and (who.is_operator or who.tenant_id == tid):
        return who
    raise HTTPException(403, "tenant scope violation")


def _realm_or_404(tid: str) -> None:
    if tid not in KC.realms or tid == PLATFORM_REALM:
        raise HTTPException(404, f"tenant '{tid}' not found")


def _audit(action: str, target: str, tid: Optional[str], actor: str = "neocloud-os") -> None:
    from .shared_services import SHARED
    SHARED.audit(actor, action, target, tenant_ref=tid)


def _public_user(u: dict) -> dict:
    return {k: v for k, v in u.items() if not k.startswith("_")} | {
        "role": u["realmRoles"][0], "name": f"{u['firstName']} {u['lastName']}".strip()}


# signup ---------------------------------------------------------------------
class SignupBody(BaseModel):
    org_name: str = Field(..., min_length=2, max_length=64)
    business_reg_no: str
    admin_email: str
    admin_name: str = Field(..., min_length=1, max_length=64)
    payment_method_token: Optional[str] = None
    public_sector: bool = False


@router.post("/api/v1/signup", status_code=201)
def signup(body: SignupBody, request: Request) -> dict:
    m = _BRN_RE.match(body.business_reg_no.strip())
    if not m:
        raise HTTPException(422, "business_reg_no must be 000-00-00000")
    brn = "-".join(m.groups())
    if not _EMAIL_RE.match(body.admin_email.strip()):
        raise HTTPException(422, "admin_email is not an email address")
    with KC.lock:
        dup = next((r for r in KC.signups.values()
                    if r["business_reg_no"] == brn and r["tenant_id"] in STORE.tenants), None)
        if dup:
            return JSONResponse({"detail": "business_reg_no already registered",
                                 "tenant_id": dup["tenant_id"], "status": dup["status"]},
                                status_code=409)
        from .models import IsolationTier, TenantCreate
        from .tenancy import create_tenant
        tenant = create_tenant(TenantCreate(name=body.org_name,
                                            isolation_tier=IsolationTier.bare_metal_dedicated))
        tid = tenant.id
        if hasattr(tenant, "status"):
            tenant.status = "pending_verification"
        if body.public_sector and hasattr(tenant, "sector"):
            tenant.sector = "public"
        realm = KC.create_tenant_realm(tid, body.org_name)
        user, initial = KC.create_user(tid, body.admin_email, body.admin_email,
                                       body.admin_name, "admin")
        pm = body.payment_method_token
        KC.signups[tid] = {
            "tenant_id": tid, "org_name": body.org_name, "business_reg_no": brn,
            "business_reg_no_checksum_ok": brn_checksum_ok(brn),
            "admin_email": body.admin_email.lower(), "admin_name": body.admin_name,
            "admin_user_id": user["id"], "public_sector": body.public_sector,
            "payment_method": ({"id": pm[:6] + "…", "attached": False} if pm else None),
            "status": "pending_verification", "created_at": _now_iso(),
            "verified_at": None, "verified_by": None,
            "guard_policy": "enforce" if body.public_sector else "default",
        }
    if body.public_sector:
        _mark_public(tid)
    _audit("signup.create", tid, tid, actor=body.admin_email.lower())
    iss = KC.issuer(_base(request), tid)
    return {"tenant_id": tid, "status": "pending_verification", "realm": tid,
            "issuer": iss,
            "admin": {"user_id": user["id"], "username": user["username"],
                      "initial_password": initial},
            "client": {"client_id": realm["client_id"], "client_secret": realm["client_secret"]},
            "next_ko": "운영자가 사업자 확인을 마치면 active가 되고 주문이 허용됩니다.",
            "next_en": "Orders are allowed once an operator verifies the business (status active)."}


def _mark_public(tid: str) -> None:
    """F03 hook (token_guard.mark_public); absent until F03 merges."""
    try:
        from . import token_guard  # type: ignore
        token_guard.mark_public(tid)
    except ImportError:
        pass


@router.get("/api/v1/signup")
def list_signups(request: Request, status: Optional[str] = None) -> list:
    _require_operator(request)
    rows = [r for r in KC.signups.values() if r["tenant_id"] in STORE.tenants]
    return [r for r in rows if status is None or r["status"] == status]


@router.get("/api/v1/signup/{tid}")
def get_signup(tid: str, request: Request) -> dict:
    _require_tenant_reader(request, tid)
    rec = KC.signups.get(tid)
    if rec is None or tid not in STORE.tenants:
        raise HTTPException(404, f"signup '{tid}' not found")
    return rec


class VerifyBody(BaseModel):
    approve: bool = True
    note: Optional[str] = None


@router.post("/api/v1/signup/{tid}/verify")
def verify_signup(tid: str, request: Request, body: Optional[VerifyBody] = None) -> dict:
    who = _require_operator(request)
    body = body or VerifyBody()
    rec = KC.signups.get(tid)
    if rec is None or tid not in STORE.tenants:
        raise HTTPException(404, f"signup '{tid}' not found")
    if rec["status"] == "active" and body.approve:
        return rec
    rec["status"] = "active" if body.approve else "rejected"
    rec["verified_at"] = _now_iso()
    rec["verified_by"] = who.via
    rec["note"] = body.note
    t = STORE.tenants.get(tid)
    if t is not None and hasattr(t, "status"):
        t.status = rec["status"]
    _audit("signup.verify" if body.approve else "signup.reject", tid, tid, actor="operator")
    return rec


@router.delete("/api/v1/signup/{tid}", status_code=204)
def delete_signup(tid: str, request: Request):
    """Operator: withdraw a signup (demo reset). Only while the tenant holds no
    orders or racks; removes the realm, its clients/users and the tenant."""
    _require_operator(request)
    if tid not in KC.signups:
        raise HTTPException(404, f"signup '{tid}' not found")
    if any(o.tenant_id == tid for o in STORE.orders.values()) or \
            any(r.tenant_id == tid for r in STORE.racks.values()):
        raise HTTPException(409, "tenant has orders or racks; release them first")
    from .shared_services import SHARED
    with KC.lock:
        KC.signups.pop(tid, None)
        KC.sso.pop(tid, None)
        KC.realms.pop(tid, None)
        for uid in KC.users.pop(tid, {}):
            STORE.members.pop(uid, None)
        for cid in [c for c, v in KC.clients.items() if v["realm"] == tid]:
            KC.clients.pop(cid)
        for k in [k for k, a in STORE.api_keys.items() if a.get("tenant_id") == tid]:
            STORE.api_keys.pop(k)
        STORE.tenants.pop(tid, None)
        STORE.netiso.pop(tid, None)
        SHARED.realms.pop(tid, None)
    _audit("signup.delete", tid, tid, actor="operator")
    return None


# organization users / roles -------------------------------------------------
class UserBody(BaseModel):
    email: str
    name: str = ""
    role: str = Field("member", pattern="^(admin|member|viewer)$")
    password: Optional[str] = Field(None, min_length=8)


class UserPatch(BaseModel):
    role: Optional[str] = Field(None, pattern="^(admin|member|viewer)$")
    enabled: Optional[bool] = None
    name: Optional[str] = None


@router.get("/api/v1/tenants/{tid}/roles")
def list_roles(tid: str, request: Request) -> list:
    _require_tenant_reader(request, tid)
    return [{"name": r, "description_ko": ROLE_INFO[r][0], "description_en": ROLE_INFO[r][1],
             "permissions": ROLE_INFO[r][2]} for r in TENANT_ROLES]


@router.get("/api/v1/tenants/{tid}/users")
def list_users(tid: str, request: Request) -> list:
    _require_tenant_reader(request, tid)
    _realm_or_404(tid)
    return [_public_user(u) for u in KC.users[tid].values()]


@router.post("/api/v1/tenants/{tid}/users", status_code=201)
def create_user(tid: str, body: UserBody, request: Request) -> dict:
    _require_tenant_admin(request, tid)
    _realm_or_404(tid)
    if not _EMAIL_RE.match(body.email):
        raise HTTPException(422, "email is not an email address")
    u, initial = KC.create_user(tid, body.email, body.email, body.name or body.email,
                                body.role, password=body.password)
    _audit("iam.user.create", u["username"], tid)
    out = _public_user(u)
    if initial:
        out["initial_password"] = initial
    return out


def _user_or_404(tid: str, uid: str) -> dict:
    _realm_or_404(tid)
    u = KC.users[tid].get(uid)
    if u is None:
        raise HTTPException(404, "user not found")
    return u


def _admins_left(tid: str, without: str) -> int:
    return sum(1 for u in KC.users[tid].values()
               if u["id"] != without and u["enabled"] and u["realmRoles"][0] == "admin")


@router.patch("/api/v1/tenants/{tid}/users/{uid}")
def update_user(tid: str, uid: str, body: UserPatch, request: Request) -> dict:
    _require_tenant_admin(request, tid)
    with KC.lock:
        u = _user_or_404(tid, uid)
        demote = (body.role not in (None, "admin") or body.enabled is False)
        if u["realmRoles"][0] == "admin" and demote and _admins_left(tid, uid) == 0:
            raise HTTPException(409, "the last admin cannot be demoted or disabled")
        if body.role:
            u["realmRoles"] = [body.role]
        if body.enabled is not None:
            u["enabled"] = body.enabled
        if body.name is not None:
            u["firstName"], _, u["lastName"] = body.name.partition(" ")
        m = STORE.members.get(uid)
        if m is not None:
            m["role"] = u["realmRoles"][0]
            m["state"] = "active" if u["enabled"] else "disabled"
    _audit("iam.user.update", u["username"], tid)
    return _public_user(u)


@router.delete("/api/v1/tenants/{tid}/users/{uid}", status_code=204)
def delete_user(tid: str, uid: str, request: Request):
    _require_tenant_admin(request, tid)
    with KC.lock:
        u = _user_or_404(tid, uid)
        if u["realmRoles"][0] == "admin" and _admins_left(tid, uid) == 0:
            raise HTTPException(409, "the last admin cannot be removed")
        del KC.users[tid][uid]
        STORE.members.pop(uid, None)
    _audit("iam.user.delete", u["username"], tid)
    return None


@router.post("/api/v1/tenants/{tid}/users/{uid}/reset-password")
def reset_password(tid: str, uid: str, request: Request) -> dict:
    _require_tenant_admin(request, tid)
    u = _user_or_404(tid, uid)
    pw = secrets.token_urlsafe(12)
    u["_pw"] = _hash_pw(pw)
    return {"user_id": uid, "initial_password": pw}


# SSO (customer IdP brokering, emulated) --------------------------------------
class SsoBody(BaseModel):
    issuer_url: str = Field(..., min_length=8, max_length=300)
    client_id: str = Field(..., min_length=1, max_length=128)
    client_secret: Optional[str] = None
    enabled: bool = True
    alias: str = Field("customer-idp", pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    default_role: str = Field("member", pattern="^(admin|member|viewer)$")
    allowed_domains: list[str] = Field(default_factory=list)


def _sso_view(tid: str) -> dict:
    s = KC.sso.get(tid)
    if not s:
        return {"tenant_id": tid, "enabled": False, "issuer_url": "", "client_id": ""}
    iss = s["issuer_url"].rstrip("/")
    return {"tenant_id": tid, "enabled": s["enabled"], "issuer_url": s["issuer_url"],
            "client_id": s["client_id"], "client_secret_set": bool(s.get("secret_hash")),
            "alias": s["alias"], "default_role": s["default_role"],
            "allowed_domains": s["allowed_domains"], "updated_at": s["updated_at"],
            # Keycloak IdentityProviderRepresentation (providerId "oidc")
            "identity_provider": {
                "alias": s["alias"], "providerId": "oidc", "enabled": s["enabled"],
                "firstBrokerLoginFlowAlias": "first broker login",
                "config": {"issuer": iss, "clientId": s["client_id"],
                           "authorizationUrl": f"{iss}/protocol/openid-connect/auth",
                           "tokenUrl": f"{iss}/protocol/openid-connect/token",
                           "jwksUrl": f"{iss}/protocol/openid-connect/certs",
                           "useJwksUrl": "true", "syncMode": "IMPORT"}},
            "login_hint_ko": "로그인 시 kc_idp_hint=" + s["alias"] + " 로 고객 IdP로 이동합니다.",
            "login_hint_en": "Sign-in with kc_idp_hint=" + s["alias"] + " goes to your IdP."}


@router.get("/api/v1/tenants/{tid}/sso")
def get_sso(tid: str, request: Request) -> dict:
    _require_tenant_reader(request, tid)
    _realm_or_404(tid)
    return _sso_view(tid)


@router.put("/api/v1/tenants/{tid}/sso")
def put_sso(tid: str, body: SsoBody, request: Request) -> dict:
    _require_tenant_admin(request, tid)
    _realm_or_404(tid)
    if not body.issuer_url.startswith("https://"):
        raise HTTPException(422, "issuer_url must start with https://")
    prev = KC.sso.get(tid) or {}
    KC.sso[tid] = {"issuer_url": body.issuer_url, "client_id": body.client_id,
                   "secret_hash": (_hash_pw(body.client_secret) if body.client_secret
                                   else prev.get("secret_hash")),
                   "enabled": body.enabled, "alias": body.alias,
                   "default_role": body.default_role,
                   "allowed_domains": [d.lower().lstrip("@") for d in body.allowed_domains],
                   "updated_at": _now_iso()}
    _audit("iam.sso.update", body.alias, tid)
    return _sso_view(tid)


class SsoLoginBody(BaseModel):
    email: str
    name: str = ""


@router.post("/api/v1/tenants/{tid}/sso/simulate-login")
def sso_simulate_login(tid: str, body: SsoLoginBody, request: Request) -> dict:
    """Emulates a brokered login: the customer IdP asserted `email`; Keycloak's
    first-broker-login creates (IMPORT) the user with default_role, then issues
    realm tokens. Operator or tenant admin only (it is a test hook)."""
    _require_tenant_admin(request, tid)
    _realm_or_404(tid)
    s = KC.sso.get(tid)
    if not s or not s["enabled"]:
        raise HTTPException(409, "SSO is not configured for this tenant")
    email = body.email.strip().lower()
    dom = email.rpartition("@")[2]
    if s["allowed_domains"] and dom not in s["allowed_domains"]:
        raise HTTPException(403, f"domain '{dom}' is not allowed by the SSO policy")
    u = KC.find_user(tid, email)
    created = u is None
    if u is None:
        u, _ = KC.create_user(tid, email, email, body.name or email, s["default_role"],
                              federated=s["alias"])
    if not u["enabled"]:
        raise HTTPException(403, "user is disabled")
    tok = KC.issue(_base(request), tid, f"{tid}-portal", _subject(u), "openid profile email", True)
    _audit("iam.sso.login", email, tid, actor=s["alias"])
    return {"user": _public_user(u), "first_login": created, "identity_provider": s["alias"],
            "token": tok}


# OIDC endpoints (Keycloak paths) ----------------------------------------------
def _discovery(base: str, realm: str, alias: bool = False) -> dict:
    iss = KC.issuer(base, realm)
    p = f"{iss}/protocol/openid-connect"
    doc = {
        "issuer": iss,
        "authorization_endpoint": f"{p}/auth",
        "token_endpoint": f"{base}/oauth2/token" if alias else f"{p}/token",
        "introspection_endpoint": f"{p}/token/introspect",
        "userinfo_endpoint": f"{p}/userinfo",
        "end_session_endpoint": f"{p}/logout",
        "jwks_uri": f"{base}/oauth2/jwks" if alias else f"{p}/certs",
        "grant_types_supported": list(GRANTS),
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": [_alg()],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": ["openid", "profile", "email"],
        "claims_supported": ["iss", "sub", "aud", "exp", "iat", "azp", "preferred_username",
                             "email", "name", "tid", "trole", "realm_access"],
    }
    if _alg() == "HS256":
        doc["x-neocloud"] = {"note": "HS256 dev signing: the shared secret is not published "
                                     "in jwks_uri; verifiers use EMU_JWT_KEY."}
    return doc


def _realm_404(realm: str) -> Optional[JSONResponse]:
    if realm not in KC.realms:
        return JSONResponse({"error": "Realm does not exist"}, status_code=404)
    return None


@router.get("/realms/{realm}/.well-known/openid-configuration")
def discovery(realm: str, request: Request):
    return _realm_404(realm) or _discovery(_base(request), realm)


@router.get("/.well-known/openid-configuration")
def discovery_root(request: Request) -> dict:
    """Plan alias: the platform realm's discovery with /oauth2/* endpoints."""
    return _discovery(_base(request), PLATFORM_REALM, alias=True)


@router.get("/realms/{realm}/protocol/openid-connect/certs")
def certs(realm: str):
    return _realm_404(realm) or KC.jwks()


@router.get("/oauth2/jwks")
def jwks_alias() -> dict:
    return KC.jwks()


def _oauth_error(error: str, desc: str, status: int = 400) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Basic realm="token"'} if status == 401 else None
    return JSONResponse({"error": error, "error_description": desc}, status_code=status,
                        headers=headers)


async def _form(request: Request) -> dict:
    raw = (await request.body()).decode("utf-8", "replace")
    if "json" in request.headers.get("content-type", ""):
        try:
            data = json.loads(raw or "{}")
            return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return dict(parse_qsl(raw, keep_blank_values=True))


def _client_auth(request: Request, form: dict) -> tuple[Optional[str], Optional[str]]:
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("basic "):
        import base64
        try:
            cid, _, sec = base64.b64decode(authz[6:]).decode().partition(":")
            from urllib.parse import unquote
            return unquote(cid), unquote(sec)
        except (ValueError, UnicodeDecodeError):
            return None, None
    return form.get("client_id"), form.get("client_secret")


def _subject(u: dict) -> dict:
    return {"sub": u["id"], "username": u["username"], "email": u["email"],
            "name": f"{u['firstName']} {u['lastName']}".strip(), "role": u["realmRoles"][0]}


def _token(request: Request, realm: Optional[str], form: dict):
    """TokenEndpoint.processGrantRequest: client auth → grant → response."""
    grant = form.get("grant_type")
    if not grant:
        return _oauth_error("invalid_request", "Missing form parameter: grant_type")
    cid, csec = _client_auth(request, form)
    if not cid:
        return _oauth_error("invalid_client", "Missing parameter: client_id", 401)
    client = KC.clients.get(cid)
    if client is None or (realm is not None and client["realm"] != realm):
        return _oauth_error("invalid_client", "Invalid client or Invalid client credentials", 401)
    realm = client["realm"]
    if not client["publicClient"] and not _check_pw(csec or "", client["secret_hash"]):
        return _oauth_error("unauthorized_client", "Invalid client or Invalid client credentials", 401)
    if grant not in GRANTS:
        return _oauth_error("unsupported_grant_type", "Unsupported grant_type")
    scope = form.get("scope") or "openid profile email"
    if "openid" not in scope.split() and grant != "client_credentials":
        scope = "openid " + scope
    base = _base(request)
    if grant == "password":
        u = KC.find_user(realm, form.get("username") or "")
        if u is None or not _check_pw(form.get("password") or "", u.get("_pw")):
            return _oauth_error("invalid_grant", "Invalid user credentials")
        if not u["enabled"]:
            return _oauth_error("invalid_grant", "Account disabled")
        out = KC.issue(base, realm, cid, _subject(u), scope, True)
        _audit("iam.token.password", u["username"], None if realm == PLATFORM_REALM else realm)
        return out
    if grant == "client_credentials":
        if client["publicClient"] or not client["serviceAccountsEnabled"]:
            return _oauth_error("unauthorized_client",
                                "Client not enabled to retrieve service account")
        subj = {"sub": str(uuid.uuid5(uuid.NAMESPACE_URL, cid)),
                "username": f"service-account-{cid}", "role": client["service_role"]}
        # Keycloak: no refresh token for client_credentials by default
        return KC.issue(base, realm, cid, subj, form.get("scope") or "profile email", False)
    # refresh_token (RFC 6749 §6) — rotation: the old refresh token is spent
    rt = form.get("refresh_token") or ""
    try:
        rc = KC.verify(rt, typ="Refresh")
    except jwt_lite.JWTError as e:
        return _oauth_error("invalid_grant", "Token is not active" if e.reason == "expired"
                            else "Invalid refresh token")
    live = KC.refresh_jti.pop(rc["jti"], None)
    if live is None or live["client_id"] != cid:
        return _oauth_error("invalid_grant", "Invalid refresh token")
    subj = live["subject"]
    if not subj["username"].startswith("service-account-"):
        u = KC.users.get(live["realm"], {}).get(subj["sub"])
        if u is None or not u["enabled"]:
            return _oauth_error("invalid_grant", "User not found or disabled")
        subj = _subject(u)
    return KC.issue(base, live["realm"], cid, subj, rc.get("scope") or scope, True, sid=live["sid"])


@router.post("/realms/{realm}/protocol/openid-connect/token")
async def token_endpoint(realm: str, request: Request):
    if realm not in KC.realms:
        return JSONResponse({"error": "Realm does not exist"}, status_code=404)
    return _token(request, realm, await _form(request))


@router.post("/oauth2/token")
async def token_alias(request: Request):
    return _token(request, None, await _form(request))


def _bearer_claims(request: Request) -> Optional[dict]:
    authz = request.headers.get("authorization", "")
    if not authz.lower().startswith("bearer "):
        return None
    try:
        return KC.verify(authz[7:].strip())
    except jwt_lite.JWTError:
        return None


@router.api_route("/realms/{realm}/protocol/openid-connect/userinfo", methods=["GET", "POST"])
def userinfo(realm: str, request: Request):
    c = _bearer_claims(request)
    if c is None or not c["iss"].endswith(f"/realms/{realm}"):
        return JSONResponse({"error": "invalid_token", "error_description": "Token verification failed"},
                            status_code=401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
    keys = ("sub", "preferred_username", "email", "email_verified", "name", "tid", "trole")
    return {k: c[k] for k in keys if k in c}


@router.post("/realms/{realm}/protocol/openid-connect/token/introspect")
async def introspect(realm: str, request: Request):
    """RFC 7662 — confidential clients only."""
    form = await _form(request)
    cid, csec = _client_auth(request, form)
    client = KC.clients.get(cid or "")
    if client is None or client["publicClient"] or not _check_pw(csec or "", client["secret_hash"]):
        return _oauth_error("invalid_client", "Invalid client credentials", 401)
    try:
        c = KC.verify(form.get("token") or "")
    except jwt_lite.JWTError:
        return {"active": False}
    if not c["iss"].endswith(f"/realms/{realm}"):
        return {"active": False}
    return {**c, "active": True, "client_id": c.get("azp"), "username": c.get("preferred_username"),
            "token_type": "Bearer"}


@router.post("/realms/{realm}/protocol/openid-connect/logout", status_code=204)
async def logout(realm: str, request: Request):
    form = await _form(request)
    try:
        rc = KC.verify(form.get("refresh_token") or "", typ="Refresh")
    except jwt_lite.JWTError:
        return _oauth_error("invalid_grant", "Invalid refresh token")
    sid = rc.get("sid")
    for j in [j for j, v in KC.refresh_jti.items() if v["sid"] == sid]:
        KC.refresh_jti.pop(j, None)
    return None


@router.get("/realms/{realm}/protocol/openid-connect/auth")
def authorize(realm: str, request: Request):
    """Authorization endpoint. Interactive login is not emulated; with
    `kc_idp_hint=<alias>` and SSO configured, it redirects to the customer IdP the
    way Keycloak identity brokering does."""
    if realm not in KC.realms:
        return JSONResponse({"error": "Realm does not exist"}, status_code=404)
    q = request.query_params
    s = KC.sso.get(realm)
    if q.get("kc_idp_hint") and s and s["enabled"] and q["kc_idp_hint"] == s["alias"]:
        iss = KC.issuer(_base(request), realm)
        params = {"client_id": s["client_id"], "response_type": "code",
                  "scope": q.get("scope", "openid email profile"),
                  "redirect_uri": f"{iss}/broker/{s['alias']}/endpoint",
                  "state": q.get("state") or secrets.token_urlsafe(8)}
        return RedirectResponse(f"{s['issuer_url'].rstrip('/')}/protocol/openid-connect/auth?"
                                + urlencode(params), status_code=302)
    return JSONResponse({"error": "invalid_request",
                         "error_description": "interactive login is not emulated; use the "
                                              "password grant or kc_idp_hint for SSO"},
                        status_code=400)


# Keycloak admin (read only, operator) ----------------------------------------
@router.get("/admin/realms")
def admin_realms(request: Request) -> list:
    _require_operator(request)
    return list(KC.realms.values())


@router.get("/admin/realms/{realm}/users")
def admin_realm_users(realm: str, request: Request) -> list:
    _require_operator(request)
    if realm not in KC.realms:
        raise HTTPException(404, "Realm not found.")
    return [{k: v for k, v in u.items() if not k.startswith("_") and k != "realmRoles"}
            for u in KC.users[realm].values()]


# ── signup gate (pure ASGI) ────────────────────────────────────────────────
GATED_POSTS = ("/api/v1/orders", "/api/v1/k8s/clusters", "/api/v1/k8s/installs",
               "/api/v1/nvke/clusters", "/api/v1/allocations", "/api/v1/contracts",
               "/api/v1/api-keys")


class SignupGateMiddleware:
    """403 for order-type POSTs of a tenant whose signup is not active (US-01).

    The tenant is the body `tenant_id` or, without one, the caller's tenant
    (bearer/JWT or dev headers). Legacy tenants (no signup record) pass."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope["method"] != "POST"
                or scope["path"].rstrip("/") not in GATED_POSTS or not KC.signups):
            return await self.app(scope, receive, send)
        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)
        tid = None
        try:
            data = json.loads(body) if body else None
            if isinstance(data, dict) and isinstance(data.get("tenant_id"), str):
                tid = data["tenant_id"]
        except (ValueError, UnicodeDecodeError):
            pass
        if tid is None:
            from starlette.requests import Request as _Req
            from . import auth
            try:
                ident = auth.resolve(_Req(scope), frozenset())
            except auth.AuthError:
                ident = None
            if ident is not None and ident.role == "tenant":
                tid = ident.tenant_id
        st = tenant_status(tid)
        if st is not None and st != "active":
            resp = JSONResponse({"detail": "tenant pending verification" if st ==
                                 "pending_verification" else f"tenant signup {st}",
                                 "tenant_id": tid, "status": st}, status_code=403)
            return await resp(scope, receive, send)
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)
