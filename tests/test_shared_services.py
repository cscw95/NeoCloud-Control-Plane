"""Shared Services ⑦ (IAM·Vault·PAM·Audit) — 파이프라인 연동·REST 검증."""


def _mk_tenant(client, name="iam-ai"):
    r = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _deliver(client, tid, racks=1):
    r = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": racks})
    order = r.json()
    assert order["state"] == "delivered", order.get("error")
    return order


def test_tenant_creation_provisions_iam_realm(client):
    tid = _mk_tenant(client)
    realm = client.get(f"/fake-shared/iam/realms/{tid}").json()
    assert realm["roles"] == ["tenant-admin", "ops-operator", "viewer"]
    portal = realm["clients"][0]
    assert portal["client_id"] == f"{tid}-portal" and portal["mfa"] is True
    audit = client.get(f"/fake-shared/audit?tenant_ref={tid}").json()
    assert any(a["action"] == "iam.realm.create" for a in audit)


def test_acceptance_issues_service_account_and_secrets(client):
    tid = _mk_tenant(client)
    order = _deliver(client, tid)
    oid = order["id"]

    realm = client.get(f"/fake-shared/iam/realms/{tid}").json()
    sa = next(c for c in realm["clients"] if c["client_id"] == f"sa-{oid}")
    assert sa["state"] == "active" and sa["tokens_issued"] >= 1

    secrets = client.get(f"/fake-shared/secrets?tenant_ref={tid}").json()
    kinds = {e["kind"] for e in secrets}
    assert {"s3-access-key", "redfish-cred"} <= kinds
    assert all(e["value_masked"].count("*") >= 4 for e in secrets)  # 값 마스킹

    # acceptance 버킷에 IAM/Vault 하부 호출이 잡혀야 한다
    flow = client.get(f"/api/v1/orders/{oid}/flow").json()
    by = {st["state"]: st["by_channel"] for st in flow["stages"]}
    assert by["acceptance"].get("OIDC/IAM", 0) >= 2      # SA 생성 + 토큰
    assert by["acceptance"].get("Vault", 0) == 2         # s3 + redfish


def test_token_issuance_requires_active_client(client):
    tid = _mk_tenant(client)
    tok = client.post("/fake-shared/iam/token",
                      json={"client_id": f"{tid}-portal"}).json()
    assert tok["token_type"] == "Bearer" and tok["access_token"]
    assert client.post("/fake-shared/iam/token",
                       json={"client_id": "no-such"}).status_code == 403
    audit = client.get("/fake-shared/audit").json()
    assert any(a["action"] == "iam.token.issue" and a["result"] == "denied"
               for a in audit)


def test_pam_session_lifecycle_with_audit(client):
    r = client.post("/fake-shared/pam/sessions", json={
        "operator": "oncall-kim", "target": "console:nh-su-1-rack-00-tray-00",
        "reason": "냉각수 유량 저하 점검", "ttl_s": 600})
    assert r.status_code == 201
    sess = r.json()
    assert sess["state"] == "active"
    closed = client.post(f"/fake-shared/pam/sessions/{sess['id']}/close").json()
    assert closed["state"] == "closed" and closed["closed_at"]
    actions = [a["action"] for a in client.get("/fake-shared/audit").json()]
    assert "pam.session.open" in actions and "pam.session.close" in actions


def test_terminate_revokes_service_account_and_purges_secrets(client):
    tid = _mk_tenant(client)
    order = _deliver(client, tid)
    alloc = order["allocation_ids"][0]

    done = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate", "allocation_id": alloc}).json()
    assert done["state"] == "closed", done.get("error")

    realm = client.get(f"/fake-shared/iam/realms/{tid}").json()
    sa = next(c for c in realm["clients"]
              if c["client_id"] == f"sa-{order['id']}")
    assert sa["state"] == "revoked"
    assert client.get(f"/fake-shared/secrets?tenant_ref={tid}").json() == []
    audit = client.get(f"/fake-shared/audit?tenant_ref={tid}").json()
    assert any(a["action"] == "iam.credentials.revoke" for a in audit)


def test_delivery_includes_access_package(client):
    """딜리버리 시 접속정보 + 보안 인증 패키지(1회 노출 secret) 제공."""
    tid = _mk_tenant(client, "pkg-ai")
    order = _deliver(client, tid)
    p = order["access_package"]
    assert p, "delivered 주문에 access_package 없음"
    assert {"ssh_bastion", "api", "console", "storage", "network"} <= set(p)
    assert p["ssh_bastion"]["user"] == tid and "MFA" in p["ssh_bastion"]["auth"]
    assert p["api"]["client_id"] == f"sa-{order['id']}"
    assert p["api"]["client_secret"].startswith("nc_")       # 원본 1회 노출
    assert p["storage"] and p["storage"][0]["mount"].startswith("vast-vip")
    assert p["network"]["vrf"] and p["network"]["ib_pkey"]

    # IAM 쪽에는 마스킹된 secret만 남는다
    realm = client.get(f"/fake-shared/iam/realms/{tid}").json()
    sa = next(c for c in realm["clients"] if c["client_id"] == f"sa-{order['id']}")
    assert "****" in sa["secret_masked"]
    assert p["api"]["client_secret"] not in str(realm)
    # 발급 이벤트가 감사 로그·주문 flow에 기록
    audit = client.get(f"/fake-shared/audit?tenant_ref={tid}").json()
    assert any(a["action"] == "delivery.access-package.issue" for a in audit)
