"""F07 demo — self signup → pending (403 on orders) → operator verify → OIDC password
grant (JWT, Keycloak paths) → JWT and the old HMAC token both accepted → expired JWT
401 → customer SSO (Keycloak identity brokering, emulated).

Self-contained; format follows app/demo/engine.py (WAVE-v2 시연 요건). Deterministic:
fixed org and business number, step 00 withdraws the previous run's signup.
The gated call is POST /api/v1/api-keys (no racks are bound, so reruns stay clean).
"""

import time

ORG = "demo-f07-acme"
TID = "tnt-demo-f07-acme"
BRN = "214-86-00001"
EMAIL = "admin@demo-f07-acme.example"


def http(method, path, json=None, as_="operator", headers=None):
    spec = {"method": method, "path": path, "as": as_, "service": "nocp"}
    if json is not None:
        spec["json"] = json
    if headers is not None:
        spec["headers"] = headers
    return spec


def _b(res):
    return res.body if isinstance(res.body, dict) else {}


def _bearer(ctx):
    return {"Authorization": "Bearer " + ctx["jwt"]}


def _expired_bearer(ctx):
    from app import identity, jwt_lite
    _, c = jwt_lite.unverified(ctx["jwt"])
    now = int(time.time())
    return {"Authorization": "Bearer " + identity.KC._sign(dict(c, iat=now - 900, exp=now - 600))}


def _reset_ok(res, ctx):
    return (res.status in (204, 404), f"이전 시연 가입 정리 (HTTP {res.status})",
            f"previous demo signup withdrawn (HTTP {res.status})")


def _signup_ok(res, ctx):
    b = _b(res)
    return (res.status == 201 and b.get("status") == "pending_verification",
            f"가입 접수 → {b.get('tenant_id')} · {b.get('status')} · realm {b.get('realm')} · 관리자 {(b.get('admin') or {}).get('username')}",
            f"signup → {b.get('tenant_id')} · {b.get('status')} · realm {b.get('realm')} · admin {(b.get('admin') or {}).get('username')}")


def _save_signup(res, ctx):
    b = _b(res)
    ctx["tenant_id"] = b["tenant_id"]
    ctx["password"] = b["admin"]["initial_password"]


def _discovery_ok(res, ctx):
    b = _b(res)
    ok = b.get("issuer", "").endswith(f"/realms/{TID}") and "password" in b.get("grant_types_supported", [])
    return (ok, f"OIDC Discovery: issuer {b.get('issuer')} · token {b.get('token_endpoint')}",
            f"OIDC Discovery: issuer {b.get('issuer')} · token {b.get('token_endpoint')}")


def _token_ok(res, ctx):
    from app import jwt_lite
    b = _b(res)
    if res.status != 200:
        return (False, f"토큰 발급 실패 HTTP {res.status}", f"token failed HTTP {res.status}")
    h, c = jwt_lite.unverified(b["access_token"])
    return (c.get("tid") == TID and c.get("trole") == "admin",
            f"JWT {h['alg']} · sub {c['sub'][:8]}… · tid {c['tid']} · trole {c['trole']} · {b['expires_in']}초",
            f"JWT {h['alg']} · sub {c['sub'][:8]}… · tid {c['tid']} · trole {c['trole']} · {b['expires_in']} s")


def _whoami(via):
    def expect(res, ctx):
        b = _b(res)
        return (b.get("via") == via, f"whoami → {b.get('role')} {b.get('tenant_id') or ''} (via {b.get('via')})",
                f"whoami → {b.get('role')} {b.get('tenant_id') or ''} (via {b.get('via')})")
    return expect


def _status(want, ko, en):
    def expect(res, ctx):
        d = _b(res).get("detail") or _b(res).get("status") or ""
        return (res.status == want, f"{ko}: HTTP {res.status} {d}", f"{en}: HTTP {res.status} {d}")
    return expect


def _verified(res, ctx):
    st = _b(res).get("status")
    return (st == "active", f"사업자 확인 완료 → {st}", f"business verified → {st}")


def _sso_ok(res, ctx):
    b = _b(res)
    return (b.get("enabled") is True, f"고객 IdP 등록: {b.get('issuer_url')} (alias {b.get('alias')})",
            f"customer IdP set: {b.get('issuer_url')} (alias {b.get('alias')})")


def _sso_login_ok(res, ctx):
    b = _b(res)
    u = b.get("user") or {}
    return (res.status == 200 and u.get("federationLink") == "customer-idp",
            f"SSO 첫 로그인 → 사용자 {u.get('username')} 자동 생성 · 역할 {u.get('role')}",
            f"SSO first login → user {u.get('username')} created · role {u.get('role')}")


SCENARIO = {
    "id": "f07-identity",
    "order": 7,
    "feature": "F07",
    "story": "US-01",
    "title_ko": "F07 셀프 가입 · 조직 · OIDC/JWT · SSO",
    "title_en": "F07 self signup · organization · OIDC/JWT · SSO",
    "summary_ko": "가입하면 테넌트·realm·관리자가 생기고, 확인 전 주문은 403, 확인 후 OIDC 토큰(JWT)으로 바로 쓴다. 기존 HMAC 토큰도 그대로 통한다.",
    "summary_en": "Signup creates tenant, realm and admin; orders are 403 until verified; then the OIDC JWT works, and the old HMAC token still does.",
    "steps": [
        {"id": "00-reset", "title_ko": "초기화", "title_en": "Reset",
         "call": http("DELETE", f"/api/v1/signup/{TID}"), "expect": _reset_ok},
        {"id": "01-signup", "title_ko": "셀프 가입", "title_en": "Self signup",
         "narration_ko": "사업자번호·관리자 이메일로 가입하면 테넌트가 '확인 대기'로 생긴다.",
         "narration_en": "Signing up creates the tenant as pending verification.",
         "call": http("POST", "/api/v1/signup",
                      {"org_name": ORG, "business_reg_no": BRN, "admin_email": EMAIL,
                       "admin_name": "Demo Admin", "public_sector": False}, as_="none"),
         "expect": _signup_ok, "save": _save_signup},
        {"id": "02-discovery", "title_ko": "OIDC Discovery 문서", "title_en": "OIDC discovery document",
         "call": http("GET", f"/realms/{TID}/.well-known/openid-configuration", as_="none"),
         "expect": _discovery_ok},
        {"id": "03-token", "title_ko": "관리자 로그인 (password grant → JWT)",
         "title_en": "Admin sign-in (password grant → JWT)",
         "call": http("POST", f"/realms/{TID}/protocol/openid-connect/token",
                      lambda c: {"grant_type": "password", "client_id": f"{TID}-portal",
                                 "username": EMAIL, "password": c["password"]}, as_="none"),
         "expect": _token_ok, "save": lambda r, c: c.__setitem__("jwt", r.body["access_token"])},
        {"id": "04-whoami-jwt", "title_ko": "JWT로 NOCP 호출", "title_en": "Call NOCP with the JWT",
         "call": http("GET", "/api/v1/auth/whoami", as_="none", headers=_bearer),
         "expect": _whoami("jwt")},
        {"id": "05-gated", "title_ko": "확인 전 주문 → 403", "title_en": "Order before verification → 403",
         "call": http("POST", "/api/v1/api-keys", {"name": "demo-key"}, as_="none", headers=_bearer),
         "expect": _status(403, "확인 전 차단", "blocked before verification")},
        {"id": "06-verify", "title_ko": "운영자 사업자 확인", "title_en": "Operator verifies the business",
         "call": http("POST", f"/api/v1/signup/{TID}/verify"), "expect": _verified},
        {"id": "07-allowed", "title_ko": "확인 후 주문 허용", "title_en": "Order allowed after verification",
         "call": http("POST", "/api/v1/api-keys", {"name": "demo-key"}, as_="none", headers=_bearer),
         "expect": _status(201, "주문(API 키 발급) 허용", "order (API key) allowed")},
        {"id": "08-expired", "title_ko": "만료된 JWT → 401", "title_en": "Expired JWT → 401",
         "call": http("GET", f"/api/v1/tenants/{TID}/users", as_="none", headers=_expired_bearer),
         "expect": _status(401, "만료 거부", "expired rejected")},
        {"id": "09-hmac", "title_ko": "기존 HMAC 토큰도 그대로", "title_en": "Old HMAC token still works",
         "call": http("GET", "/api/v1/auth/whoami"), "expect": _whoami("token")},
        {"id": "10-sso", "title_ko": "고객 IdP(SSO) 연동 설정", "title_en": "Set up customer IdP (SSO)",
         "call": http("PUT", f"/api/v1/tenants/{TID}/sso",
                      {"issuer_url": "https://login.demo-f07-acme.example/realms/corp",
                       "client_id": "neocloud", "allowed_domains": ["demo-f07-acme.example"]},
                      as_="none", headers=_bearer),
         "expect": _sso_ok},
        {"id": "11-sso-login", "title_ko": "SSO 첫 로그인 (모사)", "title_en": "SSO first login (emulated)",
         "call": http("POST", f"/api/v1/tenants/{TID}/sso/simulate-login",
                      {"email": "lee@demo-f07-acme.example", "name": "Lee SM"}, as_="none",
                      headers=_bearer),
         "expect": _sso_login_ok},
    ],
}
