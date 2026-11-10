"""DSX baseline smoke — NOCP in-process (TestClient) against a live NICo
emulator (and AI-Infra if up). Usage:

    NOCP_NICO_URL=http://127.0.0.1:9000 python scripts/dsx_smoke.py

Exit code 0 when every check passes. Uses dev switches (implicit operator +
X-Tenant-Id header trust); upstreams that are down are reported, not failed,
for the AI-Infra-backed views (they fall back to local data).
"""
import os
import sys

os.environ.setdefault("NOCP_DEV_OPERATOR_DEFAULT", "1")
os.environ.setdefault("NOCP_DEV_INSECURE_HEADERS", "1")
os.environ.setdefault("NOCP_OOB_AUTOSUBSCRIBE", "0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))


with TestClient(app) as c:
    v = c.get("/api/v1/platform/versions").json()
    check("versions matrix", v["dsx_os"] == "2026.05", f"nico_api={v['nico_api']}")
    check("nico emulator live /versions", (v["live"] or {}).get("nico_emulator") is not None)
    from app.lifecycle import get_adapter
    hosts = get_adapter().list_hosts()
    check("REST machine list via adapter", len(hosts) > 0,
          f"{len(hosts)} hosts, adapter={type(get_adapter()).__name__}")
    tid = c.post("/api/v1/tenants", json={"name": "smoke",
                                          "isolation_tier": "bare_metal_dedicated"}).json()["id"]
    sku = next((h.sku for h in hosts if h.state.value == "pool_ready"), "vr-nvl72")
    r = c.post("/api/v1/nvke/clusters", json={
        "name": "smoke", "tenant_id": tid, "k8s_version": "1.34",
        "nodepools": [{"name": "gpu", "instance_type": sku, "count": 1}]})
    check("NVKE cluster on NICo instances", r.status_code == 201, r.text[:160])
    if r.status_code == 201:
        cid = r.json()["id"]
        node = r.json()["nodepools"][0]["nodes"][0]
        h = get_adapter().get_host(node["nico_host_id"])
        check("node host allocated in NICo", h.state.value == "allocated" and h.tenant_ref == tid,
              f"{h.host_id} {h.state.value}")
        k = c.get(f"/api/v1/nvke/clusters/{cid}/kubeconfig", headers={"X-Tenant-Id": tid})
        check("kubeconfig (owner)", k.status_code == 200)
        d = c.delete(f"/api/v1/nvke/clusters/{cid}")
        h = get_adapter().get_host(node["nico_host_id"])
        check("delete returns host to pool", d.status_code == 200 and h.state.value == "pool_ready",
              h.state.value)
    m = c.get("/api/v1/machines").json()
    check("machines view", m["machines"], f"source={m['source']}")
    print("oob summary source:", c.get("/api/v1/oob/summary").json()["source"])
    print("power domains source:", c.get("/api/v1/power/domains").json()["source"])
    print("firmware drift source:", c.get("/api/v1/firmware/drift").json()["source"])

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} PASS")
sys.exit(1 if failed else 0)
