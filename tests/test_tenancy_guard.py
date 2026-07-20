"""고객면 테넌트 격리·RBAC 가드 (app/tenancy_guard.py) 회귀 테스트.

계약:
  - 헤더 없음 → 기존(관리면) 동작 그대로.
  - X-Tenant-Id 존재 시:
      · 경로 테넌트(/tenants/{tid}/...) 불일치 → 403 tenant scope violation
      · 주문 단건·k8s 클러스터 하위의 소유 테넌트 불일치 → 403
      · 목록(/orders,/k8s/clusters,/k8s/installs,/tickets,/billing/usage,
        /fake-vast/views,/fake-nico/hosts,...) → 자기 테넌트 것만 반환
  - X-Tenant-Role: viewer → 변경 액션 403 read-only role (GET은 전 역할 허용,
    member/admin은 변경 허용).
  - 공개 표면(/status,/public/*,/spec)은 헤더와 무관하게 무변경.
"""


def _hdr(tid, role=None):
    h = {"X-Tenant-Id": tid}
    if role:
        h["X-Tenant-Role"] = role
    return h


def _mk_tenant(client, name):
    r = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _order(client, tid, blueprint="vr-nvl72", racks=1, **kw):
    r = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new", "blueprint_key": blueprint,
        "racks": racks, **kw})
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["state"] == "delivered", o.get("error")
    return o


def _two_tenants(client):
    """A(vr-nvl72 1랙) · B(gb200-nvl72 1랙) — SU가 달라 dedicated 공존."""
    ta = _mk_tenant(client, "alpha-corp")
    tb = _mk_tenant(client, "beta-labs")
    oa = _order(client, ta, "vr-nvl72")
    ob = _order(client, tb, "gb200-nvl72")
    return ta, tb, oa, ob


# ---------------------------------------------------------------------------
# 0) 관리면 무회귀 — 헤더 없으면 기존 동작 그대로
# ---------------------------------------------------------------------------
def test_no_header_admin_plane_unchanged(client):
    ta, tb, oa, ob = _two_tenants(client)
    orders = client.get("/api/v1/orders").json()
    assert {o["tenant_id"] for o in orders} == {ta, tb}
    tenants = client.get("/api/v1/tenants").json()
    assert {t["id"] for t in tenants} == {ta, tb}
    # 단건도 교차 조회 가능 (운영 포털)
    assert client.get(f"/api/v1/orders/{oa['id']}").status_code == 200
    assert client.get(f"/api/v1/orders/{ob['id']}").status_code == 200
    assert client.get(f"/api/v1/tenants/{tb}/sla-report").status_code == 200


def test_no_header_ops_approval_queue_unchanged(client):
    """운영 승인 큐(approval_mode) — 헤더 없이 기존 게이트 진행."""
    ta = _mk_tenant(client, "alpha-corp")
    r = client.post("/api/v1/orders", json={
        "tenant_id": ta, "kind": "new", "blueprint_key": "vr-nvl72",
        "racks": 1, "approval_mode": True})
    o = r.json()
    while o["pending_stage"]:
        o = client.post(f"/api/v1/orders/{o['id']}/approve").json()
        assert o["state"] not in ("failed", "rejected"), o.get("error")
    assert o["state"] == "delivered"


# ---------------------------------------------------------------------------
# 1) 경로 테넌트 스코프 — /tenants/{tid}/...
# ---------------------------------------------------------------------------
def test_tenant_path_scope(client):
    ta, tb, _, _ = _two_tenants(client)
    for path in (f"/api/v1/tenants/{tb}",
                 f"/api/v1/tenants/{tb}/sla-report",
                 f"/api/v1/tenants/{tb}/rca-reports",
                 f"/api/v1/tenants/{tb}/termination",
                 f"/api/v1/tenants/{tb}/isolation"):
        r = client.get(path, headers=_hdr(ta))
        assert r.status_code == 403, path
        assert r.json() == {"detail": "tenant scope violation"}
    r = client.post(f"/api/v1/tenants/{tb}/termination", headers=_hdr(ta))
    assert r.status_code == 403
    assert r.json() == {"detail": "tenant scope violation"}
    # 자기 테넌트는 정상
    assert client.get(f"/api/v1/tenants/{ta}/sla-report",
                      headers=_hdr(ta)).status_code == 200
    assert client.get(f"/api/v1/tenants/{ta}/rca-reports",
                      headers=_hdr(ta)).status_code == 200


# ---------------------------------------------------------------------------
# 2) 주문 단건 스코프 — /orders/{id} 및 하위
# ---------------------------------------------------------------------------
def test_order_scope(client):
    ta, tb, oa, ob = _two_tenants(client)
    for path in (f"/api/v1/orders/{ob['id']}",
                 f"/api/v1/orders/{ob['id']}/flow",
                 f"/api/v1/orders/{ob['id']}/acceptance-report"):
        r = client.get(path, headers=_hdr(ta))
        assert r.status_code == 403, path
        assert r.json() == {"detail": "tenant scope violation"}
    r = client.post(f"/api/v1/orders/{ob['id']}/acceptance",
                    headers=_hdr(ta), json={"decision": "approve"})
    assert r.status_code == 403
    # 자기 주문은 정상
    assert client.get(f"/api/v1/orders/{oa['id']}",
                      headers=_hdr(ta)).status_code == 200
    assert client.get(f"/api/v1/orders/{oa['id']}/flow",
                      headers=_hdr(ta)).status_code == 200
    # 없는 주문은 403이 아니라 기존 404 유지
    assert client.get("/api/v1/orders/ord-none",
                      headers=_hdr(ta)).status_code == 404


# ---------------------------------------------------------------------------
# 3) k8s 클러스터 하위 스코프 — /k8s/clusters/{id}/*
# ---------------------------------------------------------------------------
def test_k8s_cluster_scope(client):
    ta = _mk_tenant(client, "alpha-corp")
    tb = _mk_tenant(client, "beta-labs")
    oa = _order(client, ta, "vr-nvl72", managed_k8s=True,
                k8s_version="v1.32.4")
    cid = oa["k8s_cluster_id"]
    assert cid
    for path in (f"/api/v1/k8s/clusters/{cid}",
                 f"/api/v1/k8s/clusters/{cid}/nodes",
                 f"/api/v1/k8s/clusters/{cid}/kubeconfigs",
                 f"/api/v1/k8s/clusters/{cid}/addons"):
        r = client.get(path, headers=_hdr(tb))
        assert r.status_code == 403, path
        assert r.json() == {"detail": "tenant scope violation"}
    r = client.post(f"/api/v1/k8s/clusters/{cid}/kubeconfigs",
                    headers=_hdr(tb), json={"role": "edit"})
    assert r.status_code == 403
    # 소유 테넌트는 정상
    assert client.get(f"/api/v1/k8s/clusters/{cid}",
                      headers=_hdr(ta)).status_code == 200
    r = client.post(f"/api/v1/k8s/clusters/{cid}/kubeconfigs",
                    headers=_hdr(ta), json={"role": "edit"})
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# 4) 목록 필터링 — 고객 콘솔 소비 목록은 자기 테넌트 것만
# ---------------------------------------------------------------------------
def test_list_filtering(client):
    ta, tb, oa, ob = _two_tenants(client)
    client.post("/api/v1/tickets", json={
        "tenant_id": ta, "subject": "a", "body": "a", "severity": "low",
        "type": "tech"})
    client.post("/api/v1/tickets", json={
        "tenant_id": tb, "subject": "b", "body": "b", "severity": "low",
        "type": "tech"})

    orders = client.get("/api/v1/orders", headers=_hdr(ta)).json()
    assert orders and all(o["tenant_id"] == ta for o in orders)

    # 쿼리로 타 테넌트를 지정해도 헤더 테넌트로 강제
    forced = client.get(f"/api/v1/orders?tenant_id={tb}",
                        headers=_hdr(ta)).json()
    assert forced == orders

    tickets = client.get("/api/v1/tickets", headers=_hdr(ta)).json()
    assert len(tickets) == 1 and tickets[0]["tenant_id"] == ta

    usage = client.get("/api/v1/billing/usage", headers=_hdr(ta)).json()
    assert usage["tenant_id"] == ta
    assert usage["lines"] and all(l["tenant_id"] == ta
                                  for l in usage["lines"])

    nodes = client.get("/api/v1/nodes", headers=_hdr(ta)).json()
    assert nodes and all(n["tenant_id"] == ta for n in nodes)

    tenants = client.get("/api/v1/tenants", headers=_hdr(ta)).json()
    assert [t["id"] for t in tenants] == [ta]

    views = client.get("/fake-vast/views", headers=_hdr(ta)).json()
    assert views and all(v["tenant_ref"] == ta for v in views)

    hosts = client.get("/fake-nico/hosts", headers=_hdr(ta)).json()
    assert hosts and all(h["tenant_ref"] == ta for h in hosts)

    # 헤더 없으면 전체 그대로
    assert len(client.get("/api/v1/tickets").json()) == 2
    all_hosts = client.get("/fake-nico/hosts").json()
    assert {h["tenant_ref"] for h in all_hosts} >= {ta, tb, None}


def test_k8s_list_filtering(client):
    ta = _mk_tenant(client, "alpha-corp")
    tb = _mk_tenant(client, "beta-labs")
    _order(client, ta, "vr-nvl72", managed_k8s=True)
    _order(client, tb, "gb200-nvl72", managed_k8s=True)
    clusters = client.get("/api/v1/k8s/clusters", headers=_hdr(ta)).json()
    assert len(clusters) == 1 and clusters[0]["tenant_id"] == ta
    installs = client.get("/api/v1/k8s/installs", headers=_hdr(ta)).json()
    assert installs and all(i["tenant_id"] == ta for i in installs)
    # 관리면은 전체
    assert len(client.get("/api/v1/k8s/clusters").json()) == 2


# ---------------------------------------------------------------------------
# 5) RBAC — viewer 읽기 전용 · member/admin 변경 허용
# ---------------------------------------------------------------------------
def test_viewer_read_only(client):
    ta, tb, oa, ob = _two_tenants(client)
    ro = {"detail": "read-only role"}
    v = _hdr(ta, "viewer")

    r = client.post("/api/v1/orders", headers=v, json={
        "tenant_id": ta, "kind": "new", "blueprint_key": "vr-nvl72",
        "racks": 1})
    assert (r.status_code, r.json()) == (403, ro)
    r = client.post("/api/v1/tickets", headers=v, json={
        "tenant_id": ta, "subject": "x", "body": "x", "severity": "low",
        "type": "tech"})
    assert (r.status_code, r.json()) == (403, ro)
    r = client.post(f"/api/v1/orders/{oa['id']}/acceptance", headers=v,
                    json={"decision": "approve"})
    assert (r.status_code, r.json()) == (403, ro)
    r = client.post(f"/api/v1/tenants/{ta}/termination", headers=v)
    assert (r.status_code, r.json()) == (403, ro)
    r = client.post("/api/v1/k8s/installs", headers=v, json={
        "tenant_id": ta, "allocation_id": oa["allocation_ids"][0]})
    assert (r.status_code, r.json()) == (403, ro)

    # GET은 viewer 포함 전 역할 허용
    assert client.get("/api/v1/orders", headers=v).status_code == 200
    assert client.get(f"/api/v1/tenants/{ta}/sla-report",
                      headers=v).status_code == 200


def test_viewer_kubeconfig_blocked_member_allowed(client):
    ta = _mk_tenant(client, "alpha-corp")
    oa = _order(client, ta, "vr-nvl72", managed_k8s=True)
    cid = oa["k8s_cluster_id"]
    base = f"/api/v1/k8s/clusters/{cid}/kubeconfigs"

    r = client.post(base, headers=_hdr(ta, "viewer"), json={"role": "edit"})
    assert (r.status_code, r.json()) == (403, {"detail": "read-only role"})

    r = client.post(base, headers=_hdr(ta, "member"), json={"role": "edit"})
    assert r.status_code == 201
    kid = r.json()["kubeconfig_id"]

    r = client.delete(f"{base}/{kid}", headers=_hdr(ta, "viewer"))
    assert (r.status_code, r.json()) == (403, {"detail": "read-only role"})
    r = client.delete(f"{base}/{kid}", headers=_hdr(ta, "admin"))
    assert r.status_code == 200


def test_member_and_admin_can_mutate(client):
    ta = _mk_tenant(client, "alpha-corp")
    r = client.post("/api/v1/orders", headers=_hdr(ta, "member"), json={
        "tenant_id": ta, "kind": "new", "blueprint_key": "vr-nvl72",
        "racks": 1})
    assert r.status_code == 201 and r.json()["state"] == "delivered"
    r = client.post("/api/v1/tickets", headers=_hdr(ta, "admin"), json={
        "tenant_id": ta, "subject": "x", "body": "x", "severity": "low",
        "type": "tech"})
    assert r.status_code == 201
    # 역할 헤더 생략 = member 기본 (변경 허용)
    r = client.post("/api/v1/tickets", headers=_hdr(ta), json={
        "tenant_id": ta, "subject": "y", "body": "y", "severity": "low",
        "type": "tech"})
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# 6) 공개 표면 무영향 — /status, /public/*, /spec
# ---------------------------------------------------------------------------
def test_public_endpoints_unaffected(client):
    ta = _mk_tenant(client, "alpha-corp")
    v = _hdr(ta, "viewer")
    assert client.get("/api/v1/status", headers=v).status_code == 200
    assert client.get("/api/v1/spec", headers=v).status_code == 200
    r = client.post("/api/v1/public/inquiries", headers=v, json={
        "company": "acme", "name": "kim", "email": "kim@acme.io",
        "gpu_scale": "1024", "message": "quote"})
    assert r.status_code == 201            # viewer여도 공개 표면은 무개입
    assert client.get("/health", headers=v).status_code == 200


def test_body_tenant_forgery_blocked(client):
    """고객면 변경 바디의 tenant_id 위조 → 403, 일치 시 정상."""
    ta = _mk_tenant(client, "alpha-corp")
    tb = _mk_tenant(client, "beta-labs")
    h = {"X-Tenant-Id": ta, "X-Tenant-Role": "admin"}
    r = client.post("/api/v1/tickets", headers=h, json={
        "tenant_id": tb, "type": "tech", "subject": "forged", "body": "x"})
    assert r.status_code == 403
    assert r.json()["detail"] == "tenant scope violation"
    r = client.post("/api/v1/tickets", headers=h, json={
        "tenant_id": ta, "type": "tech", "subject": "ok", "body": "x"})
    assert r.status_code in (200, 201)


def test_admin_surface_blocked_for_tenant_scope(client):
    """고객면 헤더로 /api/v1/admin/* 접근 → 403, 관리면(무헤더)은 기존 동작."""
    r = client.post("/api/v1/admin/reset-all",
                    headers={"X-Tenant-Id": "tnt-fin-corp"})
    assert r.status_code == 403
