"""Security hardening — bearer tokens, SSE token, SSRF, webhook allowlists,
CORS, cross-tenant access, secret handling."""
import pytest

from app import auth


@pytest.fixture()
def strict(monkeypatch):
    """Production posture: no dev operator default, no header trust."""
    monkeypatch.setenv("NOCP_DEV_OPERATOR_DEFAULT", "0")
    monkeypatch.setenv("NOCP_DEV_INSECURE_HEADERS", "0")
    monkeypatch.setenv("NOCP_TOKENS", "op-static=operator,ta-static=tenant:tnt-a:admin")


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


def _tenant(client, name):
    return client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"}).json()["id"]


def test_no_identity_is_401(client, strict):
    r = client.get("/api/v1/racks")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert client.get("/health").status_code == 200          # non-API untouched
    assert client.get("/api/v1/status").status_code != 401   # public surface


def test_headers_ignored_without_insecure_switch(client, strict):
    assert client.get("/api/v1/racks", headers={"X-Operator": "true"}).status_code == 401
    assert client.get("/api/v1/orders", headers={"X-Tenant-Id": "tnt-a"}).status_code == 401


def test_static_and_signed_tokens(client, strict):
    assert client.get("/api/v1/racks", headers=_bearer("op-static")).status_code == 200
    assert client.get("/api/v1/racks", headers=_bearer("wrong")).status_code == 401
    assert client.get("/api/v1/racks",
                      headers={"Authorization": "Basic abc"}).status_code == 401
    who = client.get("/api/v1/auth/whoami", headers=_bearer("ta-static")).json()
    assert who["role"] == "tenant" and who["tenant_id"] == "tnt-a"
    # operator mints a tenant token; tenants cannot mint
    r = client.post("/api/v1/auth/tokens", json={"role": "operator"},
                    headers=_bearer("op-static"))
    assert r.status_code == 201
    signed = r.json()["token"]
    assert signed.startswith("nocp1.")
    assert client.get("/api/v1/racks", headers=_bearer(signed)).status_code == 200
    assert client.post("/api/v1/auth/tokens", json={"role": "operator"},
                       headers=_bearer("ta-static")).status_code == 403
    # tampered signature
    bad = signed[:-2] + ("AA" if not signed.endswith("AA") else "BB")
    assert client.get("/api/v1/racks", headers=_bearer(bad)).status_code == 401


def test_expired_token_rejected(client, strict):
    tok = auth.mint("operator", ttl_s=-1)
    assert client.get("/api/v1/racks", headers=_bearer(tok)).status_code == 401


def test_token_identity_overrides_forged_headers(client, strict):
    # tenant token + forged X-Tenant-Id / X-Operator → still tenant tnt-a
    h = {**_bearer("ta-static"), "X-Tenant-Id": "tnt-b", "X-Operator": "true"}
    assert client.get("/api/v1/auth/whoami", headers=h).json()["tenant_id"] == "tnt-a"
    assert client.get("/api/v1/tenants/tnt-b/isolation", headers=h).status_code == 403
    assert client.get("/api/v1/oob/summary", headers=h).status_code == 403


def test_sse_requires_signed_events_token(client, strict):
    assert client.get("/api/v1/events?once=true&operator=true").status_code == 401
    r = client.post("/api/v1/events/token", headers=_bearer("op-static"))
    assert r.status_code == 201
    tok = r.json()["token"]
    assert client.get(f"/api/v1/events?once=true&token={tok}").status_code == 200
    # events token is not an API token, and ?token= only works on the stream
    assert client.get("/api/v1/racks", headers=_bearer(tok)).status_code == 401
    assert client.get(f"/api/v1/racks?token={tok}").status_code == 401
    api_tok = auth.mint("operator")
    assert client.get(f"/api/v1/events?once=true&token={api_tok}").status_code == 401
    assert client.post("/api/v1/events/token", headers=_bearer("ta-static")).status_code == 403


@pytest.mark.parametrize("path", [
    "/api/v1/infra/ufm/v1/../../etc/passwd",
    "/api/v1/infra/ufm/v1/%2e%2e/secret",
    "/api/v1/infra/http://169.254.169.254/latest",
    "/api/v1/infra//evil.example/x",
    "/api/v1/infra/ufm/v1/x@evil.example",
    "/api/v1/infra/admin/v1/x",
])
def test_infra_proxy_ssrf_rejected(client, path):
    assert client.get(path).status_code in (400, 403, 404)


def test_webhook_source_and_destination_allowlists(client, monkeypatch):
    # source allowlist (TestClient host = "testclient")
    monkeypatch.setenv("NOCP_WEBHOOK_SOURCES", "10.0.0.0/8")
    assert client.post("/api/v1/oob/events", json={"Events": []}).status_code == 403
    monkeypatch.delenv("NOCP_WEBHOOK_SOURCES")
    assert client.post("/api/v1/oob/events", json={"Events": []}).status_code == 202
    # shared secret mode
    monkeypatch.setenv("NOCP_WEBHOOK_SECRET", "s3cret")
    assert client.post("/api/v1/oob/events", json={"Events": []}).status_code == 403
    assert client.post("/api/v1/oob/events", json={"Events": []},
                       headers={"X-Webhook-Token": "s3cret"}).status_code == 202
    # subscription destinations
    r = client.post("/api/v1/oob/subscribe", json={"destination": "http://evil.example/hook"})
    assert r.status_code == 422
    r = client.post("/api/v1/oob/subscribe",
                    json={"destination": "http://user@127.0.0.1:8000/api/v1/oob/events"})
    assert r.status_code == 422


def test_cors_locked_to_console_origin(client):
    ok = client.options("/api/v1/racks", headers={
        "Origin": "http://localhost:8090", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:8090"
    bad = client.options("/api/v1/racks", headers={
        "Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers


def test_cross_tenant_access_is_denied(client):
    a, b = _tenant(client, "x-a"), _tenant(client, "x-b")
    hb = {"X-Tenant-Id": b}
    cid = client.post("/api/v1/nvke/clusters", json={"name": "a", "tenant_id": a}).json()["id"]
    t = client.post("/api/v1/tickets", json={"tenant_id": a, "subject": "x"})
    assert t.status_code == 201
    denied = [
        ("GET", f"/api/v1/tenants/{a}/isolation"),
        ("GET", f"/api/v1/isolation/{a}"),
        ("GET", f"/api/v1/nvke/clusters/{cid}"),
        ("GET", f"/api/v1/nvke/clusters/{cid}/kubeconfig"),
        ("POST", f"/api/v1/nvke/clusters/{cid}/upgrade"),
        ("DELETE", f"/api/v1/nvke/clusters/{cid}"),
        ("GET", "/api/v1/machines"),
        ("GET", "/api/v1/rma"),
    ]
    for m, p in denied:
        assert client.request(m, p, headers=hb, json={}).status_code == 403, (m, p)
    # forged body tenant on create
    assert client.post("/api/v1/nvke/clusters", headers=hb,
                       json={"name": "z", "tenant_id": a}).status_code == 403
    # list views only show own resources
    assert client.get("/api/v1/nvke/clusters", headers=hb).json() == []
    assert client.get(f"/api/v1/metering/usage?tenant={a}", headers=hb).json()["tenant"] == b
    tk = t.json()["id"]
    assert client.patch(f"/api/v1/tickets/{tk}", headers=hb,
                        json={"status": "closed"}).status_code == 403
    assert client.patch(f"/api/v1/tickets/{tk}", headers={"X-Tenant-Id": a},
                        json={"comment": "ok"}).status_code == 200
    # unknown cluster for tenant → 404, not leaked
    assert client.get("/api/v1/nvke/clusters/nvke-9999", headers=hb).status_code == 404


def test_kubeconfig_not_logged(client):
    from app import trace
    a = _tenant(client, "kc-co")
    cid = client.post("/api/v1/nvke/clusters", json={"name": "a", "tenant_id": a}).json()["id"]
    body = client.get(f"/api/v1/nvke/clusters/{cid}/kubeconfig",
                      headers={"X-Tenant-Id": a}).json()["kubeconfig"]
    dump = str([e.model_dump() for e in trace.TRACER.query(limit=5000)])
    assert "certificate-authority-data" not in dump and body not in dump
