"""Terminal demo — F08 (NOCP→NICo writes on REST, zero /nico-bridge) + F12 (NICo
machine state on the Exchange → NOCP bus). Prints each call (curl form) and a
one-line result; raw request/response with --raw.

    .venv/bin/python scripts/demo_f08_f12.py [--nocp URL] [--nico URL] [--raw]

Dev-mode stack (operator default); with secure NOCP pass --token and --emu-key.
"""
import argparse
import json
import sys
import time

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--nocp", default="http://127.0.0.1:8000")
ap.add_argument("--nico", default="http://127.0.0.1:9000")
ap.add_argument("--token")
ap.add_argument("--emu-key")
ap.add_argument("--raw", action="store_true")
a = ap.parse_args()
TENANT, P = "tnt-demo-f08", "/v2/org/neocloud/nico"
nh = {"Authorization": f"Bearer {a.token}"} if a.token else {}
eh = {"X-API-Key": a.emu_key} if a.emu_key else {}
nocp = httpx.Client(base_url=a.nocp, headers=nh, timeout=120)
nico = httpx.Client(base_url=a.nico, headers=eh, timeout=30)
fails = 0


def call(c, method, path, body=None, params=None):
    r = c.request(method, path, json=body, params=params)
    q = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    print(f"\n$ curl -X {method} {c.base_url}{path}{q}" + (f" -d '{json.dumps(body)}'" if body else ""))
    print(f"  ← HTTP {r.status_code}")
    if a.raw:
        print("  " + r.text[:1500].replace("\n", "\n  "))
    try:
        return r, r.json()
    except ValueError:
        return r, None


def step(n, ko, en, ok, detail):
    global fails
    fails += 0 if ok else 1
    print(f"[{n}] {'PASS' if ok else 'FAIL'} {ko} / {en} — {detail}")


print("F08 + F12 demo —", a.nocp, "→", a.nico)
nocp.post("/api/v1/tenants", json={"name": "demo-f08", "isolation_tier": "bare_metal_dedicated"})
_, t = call(nocp, "GET", f"/api/v1/tenants/{TENANT}")
for al in (t or {}).get("allocations", []):
    call(nocp, "POST", "/api/v1/orders", {"tenant_id": TENANT, "kind": "terminate", "allocation_id": al["id"]})
_, b0 = call(nico, "GET", "/emulator/v1/bridge-calls")
base = (b0 or {}).get("count", 0)
step(1, "bridge 호출 기준값", "bridge call baseline", True, f"{base}")
_, o = call(nocp, "POST", "/api/v1/orders", {"tenant_id": TENANT, "kind": "new",
                                             "blueprint_key": "vr-nvl72", "racks": 1})
oid = (o or {}).get("id")
for _ in range(120):
    o = nocp.get(f"/api/v1/orders/{oid}").json()
    if o.get("state") in ("delivered", "failed", "rolled_back", "rejected"):
        break
    time.sleep(1)
step(2, "베어메탈 1랙 주문", "order one rack", o.get("state") == "delivered", f"{oid} {o.get('state')}")
_, inst = call(nico, "GET", f"{P}/instance", params={"tenantId": TENANT, "pageSize": 100})
inst = inst or []
step(3, "NICo REST 인스턴스", "NICo REST instances", len(inst) == 18,
     f"{len(inst)} × {inst[0]['status'] if inst else '-'} · vpc {inst[0].get('vpcId') if inst else '-'}")
_, ib = call(nico, "GET", f"{P}/infiniband-partition", params={"tenantId": TENANT})
step(4, "테넌트 IB 파티션", "tenant IB partition", len(ib or []) == 1,
     f"P_Key {[p['partitionKey'] for p in ib or []]}")
_, ev = call(nocp, "GET", "/api/v1/events/recent", params={"topic": "machine", "limit": 1000})
mine = [e["data"] for e in ev or [] if e["data"].get("machine_id") in {i["machineId"] for i in inst}]
last = mine[-1] if mine else {}
step(5, "F12 machine 이벤트", "F12 machine events", bool(mine),
     f"{len(mine)} events · {last.get('channel')} {last.get('from')} → {last.get('to')}")
t = nocp.get(f"/api/v1/tenants/{TENANT}").json()
alloc = (t.get("allocations") or [{}])[0].get("id")
_, to = call(nocp, "POST", "/api/v1/orders", {"tenant_id": TENANT, "kind": "terminate", "allocation_id": alloc})
tid = (to or {}).get("id")
for _ in range(180):
    to = nocp.get(f"/api/v1/orders/{tid}").json()
    if to.get("state") in ("closed", "failed"):
        break
    time.sleep(1)
step(6, "반납 주문", "terminate order", to.get("state") == "closed", f"{tid} {to.get('state')}")
if inst:
    _, h = call(nico, "GET", f"{P}/instance/{inst[0]['id']}/status-history")
    step(7, "상태 이력", "status history", bool(h) and h[-1]["message"].startswith("Terminated"),
         " → ".join(x["status"] for x in h or []) + f" · {(h or [{}])[-1].get('message')}")
_, ib = call(nico, "GET", f"{P}/infiniband-partition", params={"tenantId": TENANT})
step(8, "파티션 삭제 (B2-13)", "partition deleted (B2-13)", ib == [], f"{len(ib or [])} left")
_, b1 = call(nico, "GET", "/emulator/v1/bridge-calls")
n = (b1 or {}).get("count", 0) - base
step(9, "bridge 호출 0건", "zero bridge calls", n == 0, f"{n} new /nico-bridge calls")
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
