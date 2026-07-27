"""Customer-plane ops API tests — storage / IAM / nodes (customer_ops).

Node restart requires real AI Infra Emulator integration, so here we verify
storage/IAM (pure NOCP state) plus tenant isolation / RBAC interactions. The
physical round-trip is covered in E2E.
"""


def _mk_tenant(client, name):
    r = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"})
    return r.json()["id"]


def _h(tid, role="admin"):
    return {"X-Tenant-Id": tid, "X-Tenant-Role": role}


# ── Storage ──────────────────────────────────────────────────────────────
def test_storage_volume_crud_and_scope(client):
    ta = _mk_tenant(client, "alpha")
    tb = _mk_tenant(client, "beta")
    r = client.post("/api/v1/storage/volumes", headers=_h(ta),
                    json={"name": "ckpt", "capacity_tb": 256})
    assert r.status_code == 201
    vid = r.json()["volume_id"]
    assert r.json()["path"] == f"/{ta}/ckpt"

    # Visible only in its own list
    assert [v["volume_id"] for v in
            client.get("/api/v1/storage/volumes", headers=_h(ta)).json()] == [vid]
    assert client.get("/api/v1/storage/volumes", headers=_h(tb)).json() == []

    # Snapshot / QoS
    s = client.post("/api/v1/storage/snapshots", headers=_h(ta),
                    json={"volume_id": vid}).json()
    assert s["state"] == "ready" and s["volume_id"] == vid
    q = client.patch(f"/api/v1/storage/volumes/{vid}/qos", headers=_h(ta),
                     json={"bw_gbps": 5000, "iops_k": 1500}).json()
    assert q["qos"] == {"bw_gbps": 5000.0, "iops_k": 1500.0}

    # Another tenant cannot operate on it (not owner → 404)
    assert client.patch(f"/api/v1/storage/volumes/{vid}/qos", headers=_h(tb),
                        json={"bw_gbps": 1, "iops_k": 1}).status_code == 404
    assert client.delete(f"/api/v1/storage/volumes/{vid}",
                         headers=_h(tb)).status_code == 404
    assert client.delete(f"/api/v1/storage/volumes/{vid}",
                         headers=_h(ta)).status_code == 204


def test_viewer_cannot_mutate_storage(client):
    ta = _mk_tenant(client, "alpha")
    r = client.post("/api/v1/storage/volumes", headers=_h(ta, "viewer"),
                    json={"name": "x"})
    assert r.status_code == 403
    assert r.json()["detail"] == "read-only role"


# ── IAM: API keys / members ──────────────────────────────────────────────
def test_api_key_issue_lists_without_secret(client):
    ta = _mk_tenant(client, "alpha")
    issued = client.post("/api/v1/api-keys", headers=_h(ta),
                         json={"name": "ci"}).json()
    assert issued["secret"].startswith("nc_sk_")   # shown once at issuance
    lst = client.get("/api/v1/api-keys", headers=_h(ta)).json()
    assert len(lst) == 1 and "secret" not in lst[0]  # no secret in the list
    assert client.delete(f"/api/v1/api-keys/{issued['key_id']}",
                         headers=_h(ta)).status_code == 204


def test_member_invite_and_scope(client):
    ta = _mk_tenant(client, "alpha")
    tb = _mk_tenant(client, "beta")
    m = client.post("/api/v1/members", headers=_h(ta),
                    json={"email": "e@a.com", "role": "member"}).json()
    assert m["state"] == "invited" and m["role"] == "member"
    assert len(client.get("/api/v1/members", headers=_h(ta)).json()) == 1
    assert client.get("/api/v1/members", headers=_h(tb)).json() == []
    # Invalid role
    assert client.post("/api/v1/members", headers=_h(ta),
                       json={"email": "x@a.com", "role": "root"}).status_code == 422


def test_customer_ops_require_tenant_header(client):
    """Admin-plane request without a header returns 400 (customer-plane-only surface)."""
    assert client.get("/api/v1/storage/volumes").status_code == 400
    assert client.get("/api/v1/api-keys").status_code == 400
