"""CONSOLE-BUGS Round 2: machines paging, demo tenants, termination 'none'."""
from datetime import datetime, timezone

import httpx

from app import dsx


def test_machines_offset_paging_local(client):
    seen, offset = [], 0
    while True:
        r = client.get("/api/v1/machines", params={"limit": 200, "offset": offset}).json()
        assert r["source"] == "local-fallback" and len(r["machines"]) <= 200
        seen += [m["id"] for m in r["machines"]]
        if r["next_offset"] is None:
            break
        offset = r["next_offset"]
    assert len(seen) == len(set(seen)) == r["total"]
    assert client.get("/api/v1/machines", params={"limit": 3000}).status_code == 422


def test_machines_offset_paging_nico_pages():
    """NICo REST pageSize 100 → NOCP stitches pages for [offset, offset+limit)."""
    from fastapi.testclient import TestClient
    from app.main import app
    ids = [f"m-{i:04d}" for i in range(2520)]

    def handler(req):
        if req.url.path.endswith("/nico/machine"):
            n, sz = int(req.url.params.get("pageNumber", 1)), int(req.url.params["pageSize"])
            return httpx.Response(200, json=[{"id": x} for x in ids[(n - 1) * sz:n * sz]])
        return httpx.Response(404, json={})
    dsx.set_transport(httpx.MockTransport(handler))
    try:
        with TestClient(app) as c:
            got, offset = [], 0
            for _ in range(10):
                r = c.get("/api/v1/machines", params={"limit": 1000, "offset": offset}).json()
                assert r["source"] == "nico"
                got += [m["id"] for m in r["machines"]]
                if r["next_offset"] is None:
                    break
                offset = r["next_offset"]
            assert got == ids
            r = c.get("/api/v1/machines", params={"limit": 50, "offset": 130}).json()
            assert [m["id"] for m in r["machines"]] == ids[130:180]
    finally:
        dsx.set_transport(None)


def test_demo_seed_default_tenant_reads_200(client):
    assert client.post("/api/v1/admin/reseed").status_code == 200
    tid = "tnt-fin-corp"
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    h = {"X-Tenant-Id": tid, "X-Tenant-Role": "admin"}
    for path in (f"/api/v1/tenants/{tid}/isolation", f"/api/v1/tenants/{tid}/termination",
                 f"/api/v1/tenants/{tid}/sla-report?month={month}",
                 f"/api/v1/tenants/{tid}/rca-reports", f"/fake-shared/iam/realms/{tid}"):
        assert client.get(path, headers=h).status_code == 200, path
    t = client.get(f"/api/v1/tenants/{tid}/termination", headers=h).json()
    assert t["state"] == "none" and t["termination_id"] is None
    assert client.get("/api/v1/tenants/tnt-nope/termination").status_code == 404
