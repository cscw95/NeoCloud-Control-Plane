"""F08 demo — a bare-metal order runs end to end on the NICo REST surface:
POST /vpc → /allocation → /instance, partitions, then release (DELETE /instance →
status-history → partition DELETE) with zero /nico-bridge calls (NICo call log).
Self-contained; format follows app/demo/engine.py (v2-demo). Needs NOCP started
with NOCP_NICO_URL (REST mode); NICo steps report skipped in local FakeNico mode.
Deterministic: fixed tenant, 1 rack; step 00 terminates leftovers of a prior run."""

TENANT = "tnt-demo-f08"
P = "/v2/org/neocloud/nico"


def http(method, path, json=None, params=None, service="nocp"):
    spec = {"method": method, "path": path, "as": "operator", "service": service}
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def nico(method, path, params=None):
    return http(method, path, params=params, service="nico")


def _b(res):
    return res.body


def _offline(res):
    return res.status == 0


def _cleanup(ctx):
    """Terminate any allocation a previous demo run left for TENANT."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.store import STORE
    tc = TestClient(app, raise_server_exceptions=False)
    try:
        from app.demo.engine import _token_for
        h = {"Authorization": f"Bearer {_token_for('operator')}"}
    except Exception:                       # noqa: BLE001 — engine not merged yet
        h = {"X-Operator": "true"}
    tc.post("/api/v1/tenants", json={"name": TENANT.removeprefix("tnt-"),
                                     "isolation_tier": "bare_metal_dedicated"}, headers=h)
    gone = 0
    for a in [a for a in STORE.allocations.values() if a.tenant_id == TENANT]:
        tc.post("/api/v1/orders", json={"tenant_id": TENANT, "kind": "terminate",
                                        "allocation_id": a.id}, headers=h)
        gone += 1
    return {"terminated": gone}


def _ready(res, ctx):
    return (True, f"이전 실행 정리: 회수 {_b(res).get('terminated', 0)}건",
            f"cleanup: {_b(res).get('terminated', 0)} allocation(s) terminated")


def _bridge0(res, ctx):
    if _offline(res):
        return (None, "NICo 미연결(로컬 FakeNico 모드)", "NICo offline (local FakeNico mode)")
    ctx["bridge0"] = _b(res).get("count", 0)
    return (True, f"NICo /nico-bridge 호출 기록 {ctx['bridge0']}건 (기준값)",
            f"NICo /nico-bridge call log: {ctx['bridge0']} (baseline)")


def _delivered(res, ctx):
    b = _b(res) if isinstance(_b(res), dict) else {}
    ctx["order"] = b.get("id")
    return (b.get("state") == "delivered", f"주문 {b.get('id')} 상태 {b.get('state')}",
            f"order {b.get('id')} state {b.get('state')}")


def _instances(res, ctx):
    if _offline(res):
        return (None, "NICo 미연결", "NICo offline")
    items = _b(res) if isinstance(_b(res), list) else []
    if items:
        ctx["inst"] = items[0]["id"]
    ok = len(items) == 18 and all(i.get("status") == "Ready" and i.get("vpcId") for i in items)
    return (ok, f"NICo REST 인스턴스 {len(items)}개 Ready (VPC {items[0].get('vpcId') if items else '-'})",
            f"{len(items)} NICo REST instances Ready (VPC {items[0].get('vpcId') if items else '-'})")


def _partitions(want):
    def expect(res, ctx):
        if _offline(res):
            return (None, "NICo 미연결", "NICo offline")
        items = _b(res) if isinstance(_b(res), list) else []
        keys = [p.get("partitionKey") for p in items]
        return (len(items) == want, f"IB 파티션 {len(items)}개 {keys}",
                f"{len(items)} IB partition(s) {keys}")
    return expect


def _machine_events(res, ctx):
    evs = [e for e in (_b(res) if isinstance(_b(res), list) else [])
           if e.get("data", {}).get("instance_state") == "ready"]
    if not evs:
        return (None, "machine 이벤트 없음(EventService 구독 전)", "no machine events (not subscribed)")
    d = evs[-1]["data"]
    return (True, f"F12 {d['channel']}: {d['from']} → {d['to']}",
            f"F12 {d['channel']}: {d['from']} → {d['to']}")


def _alloc_id(res, ctx):
    allocs = (_b(res) or {}).get("allocations") or []
    ctx["alloc"] = allocs[0]["id"] if allocs else None
    return (bool(allocs), f"회수 대상 할당 {ctx['alloc']}", f"allocation to release {ctx['alloc']}")


def _closed(res, ctx):
    b = _b(res) if isinstance(_b(res), dict) else {}
    return (b.get("state") == "closed", f"회수 주문 {b.get('id')} {b.get('state')}",
            f"terminate order {b.get('id')} {b.get('state')}")


def _history(res, ctx):
    if _offline(res):
        return (None, "NICo 미연결", "NICo offline")
    h = _b(res) if isinstance(_b(res), list) else []
    seq = " → ".join(x.get("status", "") for x in h)
    last = h[-1].get("message", "") if h else ""
    return (any(x.get("status") == "Terminating" for x in h) and last.startswith("Terminated"),
            f"상태 이력 {seq} · {last}", f"status history {seq} · {last}")


def _bridge_delta(res, ctx):
    if _offline(res):
        return (None, "NICo 미연결", "NICo offline")
    n = _b(res).get("count", 0) - ctx.get("bridge0", 0)
    return (n == 0, f"이번 시연 중 /nico-bridge 호출 {n}건", f"/nico-bridge calls during the demo: {n}")


SCENARIO = {
    "id": "f08-nico-rest",
    "order": 8,
    "feature": "F08",
    "title_ko": "F08 할당·반납을 NICo 공식 REST로 (bridge 0건)",
    "title_en": "F08 allocate / release over the official NICo REST (zero bridge calls)",
    "summary_ko": "주문 → POST /vpc·/allocation·/instance·파티션 → 반납 DELETE /instance → "
                  "status-history 확인 → 파티션 DELETE. NICo 호출 기록으로 bridge 0건 확인.",
    "summary_en": "Order → POST /vpc, /allocation, /instance, partitions → release DELETE /instance "
                  "→ status-history → partition DELETE; NICo call log shows zero bridge calls.",
    "steps": [
        {"id": "00-cleanup", "title_ko": "이전 실행 정리", "title_en": "Clean up a previous run",
         "call": _cleanup, "expect": _ready},
        {"id": "01-baseline", "title_ko": "bridge 호출 기준값", "title_en": "Bridge call baseline",
         "call": nico("GET", "/emulator/v1/bridge-calls"), "expect": _bridge0},
        {"id": "02-order", "title_ko": "베어메탈 1랙 주문", "title_en": "Order one bare-metal rack",
         "call": http("POST", "/api/v1/orders", {"tenant_id": TENANT, "kind": "new",
                                                  "blueprint_key": "vr-nvl72", "racks": 1}),
         "expect": _delivered},
        {"id": "03-instances", "title_ko": "NICo REST 인스턴스", "title_en": "NICo REST instances",
         "call": nico("GET", f"{P}/instance", {"tenantId": TENANT, "pageSize": 100}),
         "expect": _instances},
        {"id": "04-partitions", "title_ko": "테넌트 IB 파티션", "title_en": "Tenant IB partition",
         "call": nico("GET", f"{P}/infiniband-partition", {"tenantId": TENANT}),
         "expect": _partitions(1)},
        {"id": "05-machine-events", "title_ko": "F12 machine 이벤트", "title_en": "F12 machine events",
         "call": http("GET", "/api/v1/events/recent", params={"topic": "machine", "limit": 1000}),
         "expect": _machine_events},
        {"id": "06-alloc", "title_ko": "할당 조회", "title_en": "Find the allocation",
         "call": http("GET", f"/api/v1/tenants/{TENANT}"), "expect": _alloc_id},
        {"id": "07-terminate", "title_ko": "반납 (DELETE /instance → 정리 대기)",
         "title_en": "Release (DELETE /instance → wait for cleanup)",
         "call": http("POST", "/api/v1/orders", lambda ctx: {
             "tenant_id": TENANT, "kind": "terminate", "allocation_id": ctx["alloc"]}),
         "expect": _closed},
        {"id": "08-status-history", "title_ko": "인스턴스 상태 이력", "title_en": "Instance status history",
         "call": nico("GET", lambda ctx: f"{P}/instance/{ctx['inst']}/status-history"),
         "expect": _history},
        {"id": "09-partitions-gone", "title_ko": "파티션 삭제 확인 (B2-13)",
         "title_en": "Partitions deleted (B2-13)",
         "call": nico("GET", f"{P}/infiniband-partition", {"tenantId": TENANT}),
         "expect": _partitions(0)},
        {"id": "10-bridge-zero", "title_ko": "bridge 호출 0건", "title_en": "Zero bridge calls",
         "call": nico("GET", "/emulator/v1/bridge-calls"), "expect": _bridge_delta},
    ],
}
