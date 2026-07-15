"""Full-chain end-to-end proof across the whole NeoCloud OS system.

Chain:  Business console (deal) -> NOCP Control-Plane (order lifecycle) ->
        NICo Emulator (bridge: reserve/provision/allocate + DPU isolation) ->
        Operations (approval gates) -> Customer (delivered cluster/telemetry).

Also validates the integration-status endpoints + NICo site controllers.
Run (all three services up):  PYTHONPATH=. .venv/bin/python scripts/e2e_full_chain.py
"""
import sys, time, httpx

NOCP = "http://127.0.0.1:8000/api/v1"
NICO = "http://127.0.0.1:9000"
AI   = "http://127.0.0.1:9100"   # 물리 트윈은 AI Infra Emulator 소유
c = httpx.Client(timeout=15)
ok = fail = 0
def check(label, cond, extra=""):
    global ok, fail
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))
    ok += bool(cond); fail += (not cond)

print("NeoCloud OS — full-chain E2E\n")

# 0) all services reachable
check("NOCP reachable", c.get(NOCP + "/spec").status_code == 200)
check("NICo emulator reachable", c.get(NICO + "/healthz").status_code == 200)
consoles = httpx.get("http://127.0.0.1:8090/", timeout=5).status_code
check("Consoles reachable (:8090)", consoles == 200)

# 1) NOCP <-> NICo integration status (Control-Plane view)
integ = c.get(NOCP + "/integration/nico").json()
check("Control-Plane sees NICo emulator", integ["reachable"], f"v{integ.get('version')}")
topo = c.get(NOCP + "/integration/topology").json()
nodes = {n["id"]: n["status"] for n in topo["nodes"]}
check("topology graph complete (twin/nico/cp/consoles)",
      all(k in nodes for k in ("twin", "nico", "cp", "customer", "ops", "biz")))

# 2) NICo emulator = full cluster + per-site controllers
twin = c.get(AI + "/emulator/v1/twin").json()
check("AI Infra twin = full VR NVL72 cluster (140 racks / 10,080 GPU)",
      twin["racks"] == 140 and twin["gpus"] == 10080, f"{twin['racks']}r/{twin['gpus']}g")
srv = c.get(NICO + "/emulator/v1/sites").json()["sites"]
check("NICo per-site controllers (gasan+ansan, all services healthy)",
      len(srv) == 2 and all(s["service_ok"] == s["service_total"] for s in srv),
      f"{[(s['nico_instance'], s['service_ok'], s['service_total']) for s in srv]}")
hosts = c.get(NICO + "/nico-bridge/hosts?limit=5000").json()
check("NICo bridge exposes full fleet to NOCP (2,520 hosts)", len(hosts) == 2520)

# 3) Business -> Control-Plane: a real order (simulating deal conversion)
tenant = "e2e-corp"
c.post(NOCP + "/tenants", json={"name": tenant, "isolation_tier": "bare_metal_dedicated"})
o = c.post(NOCP + "/orders", json={"tenant_id": f"tnt-{tenant}", "kind": "new",
           "blueprint_key": "vr-nvl72", "racks": 2, "approval_mode": True}).json()
oid = o["id"]
check("Business->CP: approval-mode order created", o["state"] in ("received", "validated"),
      f"{oid} {o['state']}")

# 4) Operations: walk the approval gates to delivery
gates = 0
for _ in range(10):
    cur = c.get(NOCP + f"/orders/{oid}").json()
    if not cur.get("pending_stage"):
        break
    c.post(NOCP + f"/orders/{oid}/approve"); gates += 1
final = c.get(NOCP + f"/orders/{oid}").json()
check("Operations: approval gates -> delivered", final["state"] == "delivered",
      f"{gates} gates")

# 5) Customer: the tenant now has a live cluster in the control-plane
fab = c.get(NOCP + "/fabric/ib").json()
mine = [t for t in fab["tenants"] if t["tenant_id"] == f"tnt-{tenant}"]
check("Customer: delivered cluster visible (racks+P_Key)",
      bool(mine) and mine[0]["racks"] == 2, mine[0]["pkey"] if mine else "-")

# 6) NICo DPU isolation engine independently verified (inter-tenant deny)
r = c.post(NICO + "/emulator/v1/scenarios/inter-tenant-isolation/run", json={}).json()
check("NICo DPU isolation: inter-tenant default-deny", r["passed"])

# cleanup the e2e order (reclaim) to keep state tidy
try:
    t = c.get(NOCP + f"/tenants/tnt-{tenant}").json()
    alloc = (t.get("allocations") or [{}])[0].get("id")
    if alloc:
        c.post(NOCP + "/orders", json={"tenant_id": f"tnt-{tenant}",
               "kind": "terminate", "allocation_id": alloc})
except Exception:
    pass

print(f"\n결과: {ok} PASS · {fail} FAIL")
sys.exit(1 if fail else 0)
