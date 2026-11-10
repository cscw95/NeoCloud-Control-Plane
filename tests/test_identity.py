"""F07 — signup, orgs/users/roles, OIDC issuer, JWT acceptance (HMAC kept)."""

import base64
import time

import pytest

from app import auth, identity, jwt_lite
from app.store import STORE
from tests._schema import assert_valid

SIGNUP = {"org_name": "Acme Bio", "business_reg_no": "214-86-00001",
          "admin_email": "admin@acme-bio.example", "admin_name": "Kim Admin",
          "public_sector": False}
OP = {"Authorization": f"Bearer {auth.mint('operator')}"}


def _signup(client, **kw):
    r = client.post("/api/v1/signup", json={**SIGNUP, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _password_token(client, s, tid=None):
    tid = tid or s["tenant_id"]
    r = client.post(f"/realms/{tid}/protocol/openid-connect/token",
                    data={"grant_type": "password", "client_id": f"{tid}-portal",
                          "username": s["admin"]["username"],
                          "password": s["admin"]["initial_password"]})
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def _order(tid):
    return {"tenant_id": tid, "kind": "new", "blueprint_key": "vr-nvl72", "racks": 1}


def test_signup_creates_pending_tenant_realm_admin(client):
    s = _signup(client)
    tid = s["tenant_id"]
    assert tid == "tnt-acme-bio" and s["status"] == "pending_verification"
    assert STORE.tenants[tid].status == "pending_verification"
    assert tid in identity.KC.realms and s["issuer"].endswith(f"/realms/{tid}")
    users = client.get(f"/api/v1/tenants/{tid}/users", headers=OP).json()
    assert [u["role"] for u in users] == ["admin"]
    assert any(m["tenant_id"] == tid for m in STORE.members.values())
    assert client.get("/api/v1/signup?status=pending_verification", headers=OP).json()[0]["tenant_id"] == tid


def test_signup_validation_and_duplicate(client):
    assert client.post("/api/v1/signup", json={**SIGNUP, "business_reg_no": "12-3"}).status_code == 422
    assert client.post("/api/v1/signup", json={**SIGNUP, "admin_email": "nope"}).status_code == 422
    _signup(client)
    r = client.post("/api/v1/signup", json={**SIGNUP, "org_name": "Acme Two"})
    assert r.status_code == 409 and r.json()["tenant_id"] == "tnt-acme-bio"
    assert identity.brn_checksum_ok("214-86-00001") and not identity.brn_checksum_ok("214-86-00002")


def test_unverified_tenant_order_403_then_verify_allows(client):
    s = _signup(client)
    tid = s["tenant_id"]
    jwt = _password_token(client, s)["access_token"]
    # tenant JWT and operator both blocked while pending
    r = client.post("/api/v1/orders", json=_order(tid), headers=_bearer(jwt))
    assert r.status_code == 403 and r.json()["status"] == "pending_verification"
    assert client.post("/api/v1/orders", json=_order(tid), headers=OP).status_code == 403
    assert client.post("/api/v1/api-keys", json={"name": "k"}, headers=_bearer(jwt)).status_code == 403
    # only an operator verifies
    assert client.post(f"/api/v1/signup/{tid}/verify", headers=_bearer(jwt)).status_code == 403
    v = client.post(f"/api/v1/signup/{tid}/verify", headers=OP)
    assert v.status_code == 200 and v.json()["status"] == "active"
    r = client.post("/api/v1/orders", json=_order(tid), headers=_bearer(jwt))
    assert r.status_code == 201, r.text
    # legacy tenants (no signup record) are not gated
    t = client.post("/api/v1/tenants", json={"name": "legacy", "isolation_tier": "bare_metal_dedicated"})
    assert client.post("/api/v1/orders", json=_order(t.json()["id"])).status_code == 201


def test_rejected_signup_stays_blocked(client):
    tid = _signup(client)["tenant_id"]
    r = client.post(f"/api/v1/signup/{tid}/verify", json={"approve": False, "note": "bad docs"}, headers=OP)
    assert r.json()["status"] == "rejected"
    assert client.post("/api/v1/orders", json=_order(tid), headers=OP).status_code == 403


def test_jwt_identity_scope_and_expiry(client):
    s = _signup(client)
    tid = s["tenant_id"]
    tok = _password_token(client, s)
    assert_valid(tok, "token-response.schema.json")
    _, claims = jwt_lite.unverified(tok["access_token"])
    assert_valid(claims, "jwt-access-claims.schema.json")
    assert claims["tid"] == tid and claims["trole"] == "admin"
    w = client.get("/api/v1/auth/whoami", headers=_bearer(tok["access_token"])).json()
    assert w == {"authenticated": True, "role": "tenant", "tenant_id": tid,
                 "tenant_role": "admin", "via": "jwt"}
    # other tenant's surface → 403
    assert client.get("/api/v1/tenants/tnt-other/sso", headers=_bearer(tok["access_token"])).status_code == 403
    # expired JWT → 401
    old = dict(claims, exp=int(time.time()) - 120, iat=int(time.time()) - 600)
    expired = identity.KC._sign(old)
    r = client.get("/api/v1/auth/whoami", headers=_bearer(expired))
    assert r.json() == {"authenticated": False} or r.status_code == 401
    assert client.get(f"/api/v1/tenants/{tid}/users", headers=_bearer(expired)).status_code == 401
    # tampered signature → 401
    bad = tok["access_token"][:-4] + ("AAAA" if not tok["access_token"].endswith("AAAA") else "BBBB")
    assert client.get(f"/api/v1/tenants/{tid}/users", headers=_bearer(bad)).status_code == 401


def test_hmac_tokens_still_work(client):
    tok = auth.mint("tenant", tenant_id="tnt-x", tenant_role="viewer")
    w = client.get("/api/v1/auth/whoami", headers=_bearer(tok)).json()
    assert w["via"] == "token" and w["tenant_id"] == "tnt-x"
    assert client.get("/api/v1/auth/whoami", headers=OP).json()["role"] == "operator"


def test_client_credentials_refresh_rotation_and_errors(client):
    s = _signup(client)
    tid = s["tenant_id"]
    url = f"/realms/{tid}/protocol/openid-connect/token"
    cc = client.post(url, data={"grant_type": "client_credentials", "client_id": s["client"]["client_id"],
                                "client_secret": s["client"]["client_secret"]})
    assert cc.status_code == 200 and "refresh_token" not in cc.json()
    _, c = jwt_lite.unverified(cc.json()["access_token"])
    assert c["preferred_username"] == f"service-account-{tid}-api" and c["trole"] == "member"

    pw = _password_token(client, s)
    r1 = client.post(url, data={"grant_type": "refresh_token", "client_id": f"{tid}-portal",
                                "refresh_token": pw["refresh_token"]})
    assert r1.status_code == 200 and r1.json()["refresh_token"] != pw["refresh_token"]
    again = client.post(url, data={"grant_type": "refresh_token", "client_id": f"{tid}-portal",
                                   "refresh_token": pw["refresh_token"]})
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"

    cases = [
        ({"grant_type": "password", "client_id": f"{tid}-portal", "username": s["admin"]["username"],
          "password": "wrong"}, 400, "invalid_grant"),
        ({"grant_type": "client_credentials", "client_id": f"{tid}-api", "client_secret": "x"}, 401,
         "unauthorized_client"),
        ({"grant_type": "client_credentials", "client_id": "nope"}, 401, "invalid_client"),
        ({"grant_type": "implicit", "client_id": f"{tid}-portal"}, 400, "unsupported_grant_type"),
        ({"client_id": f"{tid}-portal"}, 400, "invalid_request"),
    ]
    for form, st, err in cases:
        r = client.post(url, data=form)
        assert r.status_code == st and r.json()["error"] == err, (form, r.text)
        assert_valid(r.json(), "oauth-error.schema.json")


def test_oauth2_alias_with_basic_auth(client):
    s = _signup(client)
    basic = base64.b64encode(f"{s['client']['client_id']}:{s['client']['client_secret']}".encode()).decode()
    r = client.post("/oauth2/token", data={"grant_type": "client_credentials"},
                    headers={"Authorization": f"Basic {basic}"})
    assert r.status_code == 200, r.text
    jwt = r.json()["access_token"]
    assert client.get("/api/v1/auth/whoami", headers=_bearer(jwt)).json()["via"] == "jwt"
    # Basic on a normal API path is still rejected
    assert client.get("/api/v1/tenants", headers={"Authorization": f"Basic {basic}"}).status_code == 401


def test_discovery_and_jwks_contract(client):
    tid = _signup(client)["tenant_id"]
    for path in (f"/realms/{tid}/.well-known/openid-configuration", "/.well-known/openid-configuration"):
        d = client.get(path).json()
        assert_valid(d, "oidc-discovery.schema.json")
        assert set(identity.GRANTS) <= set(d["grant_types_supported"])
    d = client.get(f"/realms/{tid}/.well-known/openid-configuration").json()
    assert d["token_endpoint"].endswith(f"/realms/{tid}/protocol/openid-connect/token")
    assert d["issuer"].endswith(f"/realms/{tid}")
    assert client.get("/oauth2/jwks").json() == {"keys": []}          # HS256: never published
    assert client.get("/realms/nope/.well-known/openid-configuration").status_code == 404


def test_rs256_jwks_verifies_token(client, monkeypatch):
    monkeypatch.setenv("NOCP_JWT_ALG", "RS256")
    s = _signup(client)
    jwks = client.get(f"/realms/{s['tenant_id']}/protocol/openid-connect/certs").json()
    assert_valid(jwks, "jwks.schema.json")
    assert len(jwks["keys"]) == 1
    tok = _password_token(client, s)["access_token"]
    hdr, _ = jwt_lite.unverified(tok)
    assert hdr["alg"] == "RS256" and hdr["kid"] == jwks["keys"][0]["kid"]
    key = jwt_lite.RSAKey.from_jwk(jwks["keys"][0])
    claims = jwt_lite.decode(tok, keys=[key], algorithms=("RS256",))
    assert claims["tid"] == s["tenant_id"]
    assert client.get("/api/v1/auth/whoami", headers=_bearer(tok)).json()["via"] == "jwt"


def test_users_roles_and_viewer_read_only(client):
    s = _signup(client)
    tid = s["tenant_id"]
    client.post(f"/api/v1/signup/{tid}/verify", headers=OP)
    adm = _bearer(_password_token(client, s)["access_token"])
    roles = client.get(f"/api/v1/tenants/{tid}/roles", headers=adm).json()
    assert [r["name"] for r in roles] == ["admin", "member", "viewer"]
    r = client.post(f"/api/v1/tenants/{tid}/users", json={"email": "v@acme-bio.example", "role": "viewer"},
                    headers=adm)
    assert r.status_code == 201 and r.json()["initial_password"]
    vt = client.post(f"/realms/{tid}/protocol/openid-connect/token",
                     data={"grant_type": "password", "client_id": f"{tid}-portal",
                           "username": "v@acme-bio.example", "password": r.json()["initial_password"]})
    vh = _bearer(vt.json()["access_token"])
    assert client.get(f"/api/v1/tenants/{tid}/users", headers=vh).status_code == 200
    assert client.post("/api/v1/orders", json=_order(tid), headers=vh).status_code == 403  # read-only role
    assert client.post(f"/api/v1/tenants/{tid}/users", json={"email": "x@a.example"},
                       headers=vh).status_code == 403
    # last admin can be neither demoted nor deleted
    admin_id = s["admin"]["user_id"]
    assert client.patch(f"/api/v1/tenants/{tid}/users/{admin_id}", json={"role": "member"},
                        headers=adm).status_code == 409
    assert client.delete(f"/api/v1/tenants/{tid}/users/{admin_id}", headers=adm).status_code == 409
    vid = r.json()["id"]
    assert client.patch(f"/api/v1/tenants/{tid}/users/{vid}", json={"enabled": False},
                        headers=adm).json()["enabled"] is False
    bad = client.post(f"/realms/{tid}/protocol/openid-connect/token",
                      data={"grant_type": "password", "client_id": f"{tid}-portal",
                            "username": "v@acme-bio.example", "password": r.json()["initial_password"]})
    assert bad.status_code == 400
    assert client.delete(f"/api/v1/tenants/{tid}/users/{vid}", headers=adm).status_code == 204


def test_sso_config_broker_redirect_and_login(client):
    s = _signup(client)
    tid = s["tenant_id"]
    adm = _bearer(_password_token(client, s)["access_token"])
    assert client.get(f"/api/v1/tenants/{tid}/sso", headers=adm).json()["enabled"] is False
    assert client.put(f"/api/v1/tenants/{tid}/sso", json={"issuer_url": "http://idp", "client_id": "c"},
                      headers=adm).status_code == 422
    body = {"issuer_url": "https://login.acme.example/realms/corp", "client_id": "neocloud",
            "client_secret": "s3cret", "allowed_domains": ["acme.example"]}
    v = client.put(f"/api/v1/tenants/{tid}/sso", json=body, headers=adm).json()
    assert v["enabled"] and v["identity_provider"]["providerId"] == "oidc"
    assert "s3cret" not in str(v) and v["client_secret_set"] is True
    r = client.get(f"/realms/{tid}/protocol/openid-connect/auth",
                   params={"client_id": f"{tid}-portal", "kc_idp_hint": "customer-idp", "state": "xyz"},
                   follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://login.acme.example/realms/corp/protocol/openid-connect/auth?")
    lg = client.post(f"/api/v1/tenants/{tid}/sso/simulate-login", json={"email": "lee@acme.example"}, headers=adm)
    assert lg.status_code == 200 and lg.json()["first_login"] is True
    assert lg.json()["user"]["federationLink"] == "customer-idp"
    _, c = jwt_lite.unverified(lg.json()["token"]["access_token"])
    assert c["trole"] == "member" and c["email"] == "lee@acme.example"
    assert client.post(f"/api/v1/tenants/{tid}/sso/simulate-login", json={"email": "x@evil.example"},
                       headers=adm).status_code == 403


def test_public_sector_signup_marks_tenant(client):
    s = _signup(client, org_name="Gov Agency", business_reg_no="101-82-00003", public_sector=True)
    tid = s["tenant_id"]
    assert STORE.tenants[tid].sector == "public"
    assert client.get(f"/api/v1/signup/{tid}", headers=OP).json()["guard_policy"] == "enforce"


def test_userinfo_introspect_logout(client):
    s = _signup(client)
    tid = s["tenant_id"]
    tok = _password_token(client, s)
    ui = client.get(f"/realms/{tid}/protocol/openid-connect/userinfo", headers=_bearer(tok["access_token"]))
    assert ui.status_code == 200 and ui.json()["email"] == SIGNUP["admin_email"]
    intro = client.post(f"/realms/{tid}/protocol/openid-connect/token/introspect",
                        data={"token": tok["access_token"], "client_id": s["client"]["client_id"],
                              "client_secret": s["client"]["client_secret"]})
    assert intro.json()["active"] is True and intro.json()["tid"] == tid
    out = client.post(f"/realms/{tid}/protocol/openid-connect/logout",
                      data={"client_id": f"{tid}-portal", "refresh_token": tok["refresh_token"]})
    assert out.status_code == 204
    r = client.post(f"/realms/{tid}/protocol/openid-connect/token",
                    data={"grant_type": "refresh_token", "client_id": f"{tid}-portal",
                          "refresh_token": tok["refresh_token"]})
    assert r.status_code == 400


def test_operator_client_credentials_and_admin_api(client, monkeypatch):
    monkeypatch.setenv("NOCP_OIDC_OPS_SECRET", "ops-secret")
    identity.reset()
    r = client.post("/realms/neocloud/protocol/openid-connect/token",
                    data={"grant_type": "client_credentials", "client_id": "nocp-ops",
                          "client_secret": "ops-secret"})
    assert r.status_code == 200
    h = _bearer(r.json()["access_token"])
    assert client.get("/api/v1/auth/whoami", headers=h).json()["role"] == "operator"
    tid = _signup(client)["tenant_id"]
    assert client.post(f"/api/v1/signup/{tid}/verify", headers=h).json()["status"] == "active"
    realms = [x["realm"] for x in client.get("/admin/realms", headers=h).json()]
    assert realms == ["neocloud", tid]
    assert client.get(f"/admin/realms/{tid}/users", headers=h).json()[0]["username"] == SIGNUP["admin_email"]


@pytest.mark.parametrize("brn,ok", [("214-86-00001", True), ("1018200003", None)])
def test_brn_format_accepts_with_or_without_dashes(client, brn, ok):
    r = client.post("/api/v1/signup", json={**SIGNUP, "business_reg_no": brn})
    assert r.status_code == 201
