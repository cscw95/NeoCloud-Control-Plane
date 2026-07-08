"""Service-lifecycle tests: order pipeline (F1/F4 saga), fake NICo, reconcile.

Seed (conftest): SU-1 = gb200-nvl72 (14 racks x 18 trays), SU-2 = vr-nvl72.
Fake NICo + node bootstrap run in the app lifespan, so every test starts with
540 pool_ready nodes mirroring 540 NICo hosts (gb200 SU 14랙 + vr SU 16랙).
"""

import pytest
from fastapi import HTTPException

from app import lifecycle
from app.models import NodeLifecycleState as NS
from app.nico_fake import FAKE_NICO, NicoHostState
from app.store import STORE

TRAYS_PER_RACK = 18
TOTAL_NODES = (14 + 16) * TRAYS_PER_RACK       # gb200 SU 14랙 + vr SU 16랙(NCP RD)


def _mk_tenant(client, name="acme-ai"):
    r = client.post("/api/v1/tenants", json={
        "name": name, "isolation_tier": "bare_metal_dedicated"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _new_order(client, tenant_id, racks=1, blueprint="vr-nvl72"):
    return client.post("/api/v1/orders", json={
        "tenant_id": tenant_id, "kind": "new",
        "blueprint_key": blueprint, "racks": racks})


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------
def test_bootstrap_pool(client):
    r = client.get("/api/v1/nodes/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == TOTAL_NODES
    assert body["by_state"] == {"pool_ready": TOTAL_NODES}
    assert len(FAKE_NICO.hosts) == TOTAL_NODES


# ---------------------------------------------------------------------------
# F1 — new-order happy path
# ---------------------------------------------------------------------------
def test_new_order_delivers_cluster(client):
    tid = _mk_tenant(client)
    r = _new_order(client, tid, racks=2)
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["state"] == "delivered"
    assert len(order["node_ids"]) == 2 * TRAYS_PER_RACK
    assert len(order["allocation_ids"]) == 1          # best-fit single SU

    # order history walks the full state machine; storage bound via VAST
    states = [e["state"] for e in order["history"]]
    assert states == ["received", "validated", "reserved", "provisioning",
                      "isolating", "storage_binding", "acceptance", "delivered"]
    storage_evt = [e for e in order["history"] if e["state"] == "storage_binding"]
    assert "VAST" in storage_evt[0]["detail"]
    iso_evt = [e for e in order["history"] if e["state"] == "isolating"]
    assert "P_Key" in iso_evt[0]["detail"] and "VPC" in iso_evt[0]["detail"]

    # nodes in service, bound to tenant, mirrored in NICo as allocated
    nodes = client.get(f"/api/v1/nodes?tenant_id={tid}").json()
    assert len(nodes) == 2 * TRAYS_PER_RACK
    assert {n["state"] for n in nodes} == {"in_service"}
    host = FAKE_NICO.get_host(nodes[0]["nico_host_id"])
    assert host.state == NicoHostState.allocated
    assert host.tenant_ref == tid

    # tenancy allocation exists and racks are bound
    tenant = client.get(f"/api/v1/tenants/{tid}").json()
    assert len(tenant["allocations"]) == 1
    assert len(tenant["allocations"][0]["rack_ids"]) == 2


def test_order_rejected_on_insufficient_capacity(client):
    tid = _mk_tenant(client)
    r = _new_order(client, tid, racks=17)             # vr 랙은 16개뿐 (1 SU)
    order = r.json()
    assert order["state"] == "rejected"
    assert "insufficient capacity" in order["error"]
    # nothing leaked: pool unchanged
    assert client.get("/api/v1/nodes/summary").json()["by_state"] == {
        "pool_ready": TOTAL_NODES}


def test_order_with_job_latency_polls_to_convergence(client):
    """Jobs stay 'running' for 2 polls — pipeline must poll, not assume."""
    FAKE_NICO.job_latency = 2
    try:
        tid = _mk_tenant(client)
        order = _new_order(client, tid, racks=1).json()
        assert order["state"] == "delivered"
    finally:
        FAKE_NICO.job_latency = 0


# ---------------------------------------------------------------------------
# F1 — saga compensation on provision failure
# ---------------------------------------------------------------------------
def test_provision_failure_compensates(client):
    tid = _mk_tenant(client)
    # placement is deterministic: first vr rack is su-2-rack-00, first tray 00
    victim = "nh-su-2-rack-00-tray-00"
    FAKE_NICO.inject_failure(victim, "provision")

    order = _new_order(client, tid, racks=1).json()
    assert order["state"] == "failed"
    assert "provision failed" in order["error"]

    summary = client.get("/api/v1/nodes/summary").json()["by_state"]
    assert summary.get("quarantined") == 1            # the victim, break-fix track
    assert summary.get("pool_ready") == TOTAL_NODES - 1

    # no allocation left behind, victim host quarantined in NICo
    tenant = client.get(f"/api/v1/tenants/{tid}").json()
    assert tenant["allocations"] == []
    assert FAKE_NICO.get_host(victim).state == NicoHostState.quarantined


# ---------------------------------------------------------------------------
# F4 — terminate: drain -> release -> sanitize -> pool return
# ---------------------------------------------------------------------------
def test_terminate_sanitizes_and_returns_pool(client):
    tid = _mk_tenant(client)
    order = _new_order(client, tid, racks=1).json()
    alloc_id = order["allocation_ids"][0]

    r = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate", "allocation_id": alloc_id})
    assert r.status_code == 201, r.text
    term = r.json()
    assert term["state"] == "closed"
    assert term["error"] is None

    # everything back in the pool; racks unbound; NICo hosts pool_ready
    assert client.get("/api/v1/nodes/summary").json()["by_state"] == {
        "pool_ready": TOTAL_NODES}
    tenant = client.get(f"/api/v1/tenants/{tid}").json()
    assert tenant["allocations"] == []

    # sanitize evidence: full step list recorded (audit trail for security SLA)
    node = client.get(f"/api/v1/nodes/{term['node_ids'][0]}").json()
    report = FAKE_NICO.get_sanitize_report(node["nico_host_id"])
    assert report.passed and len(report.steps) == 7
    assert any("pool return" in e["detail"] for e in node["history"])


def test_sanitize_failure_goes_rma(client):
    tid = _mk_tenant(client)
    order = _new_order(client, tid, racks=1).json()
    victim = "nh-su-2-rack-00-tray-00"
    FAKE_NICO.inject_failure(victim, "sanitize")

    term = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate",
        "allocation_id": order["allocation_ids"][0]}).json()
    assert term["state"] == "closed"
    assert "RMA" in term["history"][-1]["detail"]
    assert "physical disposal" in term["error"]

    summary = client.get("/api/v1/nodes/summary").json()["by_state"]
    assert summary.get("rma") == 1
    assert summary.get("pool_ready") == TOTAL_NODES - 1
    assert FAKE_NICO.get_host(victim).state == NicoHostState.rma


# ---------------------------------------------------------------------------
# reconcile — GHOST / ORPHAN / STATE_MISMATCH
# ---------------------------------------------------------------------------
def test_reconcile_ghost_is_registered_additively(client):
    FAKE_NICO.add_ghost("nh-ghost-01")
    report = client.post("/api/v1/reconcile/run").json()
    assert report["ghosts_registered"] == 1
    kinds = {f["kind"] for f in report["findings"]}
    assert kinds == {"GHOST"}
    ghost = client.get("/api/v1/nodes/ni-nh-ghost-01").json()
    assert ghost["state"] == "discovered"


def test_reconcile_orphan_is_cordoned_and_critical(client):
    victim = "nh-su-1-rack-00-tray-00"
    FAKE_NICO.hosts.pop(victim)
    report = client.post("/api/v1/reconcile/run").json()
    assert report["orphans_cordoned"] == 1
    assert report["ok"] is False
    finding = report["findings"][0]
    assert finding["kind"] == "ORPHAN" and finding["severity"] == "critical"
    # orphaned pool node is pulled from the sellable pool
    node = client.get("/api/v1/nodes/ni-su-1-rack-00-tray-00").json()
    assert node["state"] == "cordoned"


def test_order_survives_vanished_host(client):
    """Regression: NICo 4xx mid-reserve must compensate, never leak HTTP 4xx."""
    tid = _mk_tenant(client)
    FAKE_NICO.hosts.pop("nh-su-2-rack-00-tray-05")    # host gone, mirror stale

    r = _new_order(client, tid, racks=1)
    assert r.status_code == 201                        # graceful failed order
    order = r.json()
    assert order["state"] == "failed"
    assert "reserve failed" in order["error"]

    summary = client.get("/api/v1/nodes/summary").json()["by_state"]
    assert summary.get("cordoned") == 1                # victim pulled from pool
    assert summary.get("pool_ready") == TOTAL_NODES - 1
    assert client.get(f"/api/v1/tenants/{tid}").json()["allocations"] == []

    # retry now skips the unhealthy rack and delivers on another one
    retry = _new_order(client, tid, racks=1).json()
    assert retry["state"] == "delivered"
    alloc = client.get(f"/api/v1/tenants/{tid}").json()["allocations"][0]
    assert "su-2-rack-00" not in alloc["rack_ids"]


def test_cpu_nodes_default_allocation_and_reclaim(client):
    """테넌트당 기본 CPU 노드 5대 — DPU로 VPC 연결, 마지막 회수 시 반납."""
    tid = _mk_tenant(client)
    assert len(client.get("/api/v1/cpu-nodes").json()) == 60      # 풀 시드

    order = _new_order(client, tid, racks=1).json()
    assert order["state"] == "delivered"
    assert "CPU 노드 5대" in [e for e in order["history"]
                              if e["state"] == "isolating"][0]["detail"]
    mine = client.get(f"/api/v1/cpu-nodes?tenant_id={tid}").json()
    assert len(mine) == 5
    assert all(c["state"] == "allocated" and c["host_ip"].startswith("10.250.")
               and c["segment_id"] == order["segment_id"] for c in mine)

    # 확장해도 5대 유지
    _new_order(client, tid, racks=1)
    assert len(client.get(f"/api/v1/cpu-nodes?tenant_id={tid}").json()) == 5

    # 부분 회수 → 유지, 전체 회수 → 반납
    orders = client.get(f"/api/v1/orders?tenant_id={tid}").json()
    allocs = [o["allocation_ids"][0] for o in orders if o["kind"] == "new"]
    client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate", "allocation_id": allocs[0]})
    assert len(client.get(f"/api/v1/cpu-nodes?tenant_id={tid}").json()) == 5
    client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate", "allocation_id": allocs[1]})
    assert client.get(f"/api/v1/cpu-nodes?tenant_id={tid}").json() == []
    pool = client.get("/api/v1/cpu-nodes").json()
    assert all(c["state"] == "pool_ready" for c in pool)


def test_manual_storage_allocation(client):
    """스토리지 수동 지정 — 용량/QoS가 주문 값으로 프로비저닝."""
    tid = _mk_tenant(client)
    order = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new", "blueprint_key": "vr-nvl72",
        "racks": 1, "storage_mode": "manual",
        "storage_tb": 1200, "storage_gbps": 96}).json()
    assert order["state"] == "delivered"
    view = client.get("/fake-vast/views").json()[0]
    assert view["capacity_tb"] == 1200 and view["qos_gbps"] == 96
    detail = [e for e in order["history"]
              if e["state"] == "storage_binding"][0]["detail"]
    assert "수동 지정" in detail

    # manual인데 용량 미지정 → 검증 단계에서 rejected
    bad = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new", "blueprint_key": "vr-nvl72",
        "racks": 1, "storage_mode": "manual"}).json()
    assert bad["state"] == "rejected" and "storage" in bad["error"]


def test_placement_respects_dedicated_su_isolation(client):
    """버그 회귀(tnt-skb-001): dedicated 테넌트 배치가 타 테넌트 점유 SU를
    선택해 격리 단계에서 실패하던 문제 — 배치 단계에서 정책 선반영."""
    a = _mk_tenant(client, name="alpha-ded")
    order_a = _new_order(client, a, racks=2).json()      # su-2(vr)에 2랙 점유
    assert order_a["state"] == "delivered"

    # dedicated 신규 테넌트는 su-2를 공유할 수 없음 → 남는 vr SU 없음 →
    # (기존 버그: isolating에서 failed) → 이제 배치 단계에서 정책적 rejected
    b = _mk_tenant(client, name="bravo-ded")
    order_b = _new_order(client, b, racks=1).json()
    assert order_b["state"] == "rejected"
    assert "insufficient capacity" in order_b["error"]
    # 잔재 없음 — 예약/격리까지 가지 않음
    assert client.get(f"/api/v1/tenants/{b}").json()["allocations"] == []

    # 같은 테넌트의 확장은 자기 SU 공유 허용 (기존 동작 유지)
    expand = _new_order(client, a, racks=1).json()
    assert expand["state"] == "delivered"

    # gb200 SU(su-1)는 비어 있으므로 dedicated 신규 배치 가능
    order_b2 = client.post("/api/v1/orders", json={
        "tenant_id": b, "kind": "new",
        "blueprint_key": "gb200-nvl72", "racks": 1}).json()
    assert order_b2["state"] == "delivered"

    # 역방향 보호: vm_multitenant 테넌트도 dedicated가 점유한 SU에는 배치 불가
    r = client.post("/api/v1/tenants", json={
        "name": "charlie-vm", "isolation_tier": "vm_multitenant"})
    cvm = r.json()["id"]
    order_c = _new_order(client, cvm, racks=1).json()    # vr SU는 alpha 전용
    assert order_c["state"] == "rejected"
    # 기존 dedicated 테넌트의 격리 리포트는 계속 PASS
    assert client.get(f"/api/v1/tenants/{a}/isolation").json()["ok"] is True


def test_placement_skips_racks_with_unhealthy_nodes(client):
    node = STORE.node_instances["ni-su-2-rack-01-tray-00"]
    lifecycle.advance_node(node, NS.cordoned, "test: break-fix")

    tid = _mk_tenant(client)
    order = _new_order(client, tid, racks=13).json()   # 14 vr racks - 1 unhealthy
    assert order["state"] == "delivered"
    alloc = client.get(f"/api/v1/tenants/{tid}").json()["allocations"][0]
    assert "su-2-rack-01" not in alloc["rack_ids"]
    # asking for all 14 must now be rejected (capacity, not crash)
    tid2 = _mk_tenant(client, name="other-ai")
    r2 = _new_order(client, tid2, racks=14).json()
    assert r2["state"] == "rejected"
    assert "insufficient capacity" in r2["error"]


def test_reconcile_detects_divergence_behind_our_back(client):
    tid = _mk_tenant(client)
    order = _new_order(client, tid, racks=1).json()
    node = client.get(f"/api/v1/nodes/{order['node_ids'][0]}").json()

    # NICo releases the host without telling us (e.g. operator side channel)
    FAKE_NICO.hosts[node["nico_host_id"]].state = NicoHostState.pool_ready

    report = client.post("/api/v1/reconcile/run").json()
    assert report["mismatches"] == 1
    finding = [f for f in report["findings"] if f["kind"] == "STATE_MISMATCH"][0]
    assert finding["severity"] == "critical"          # in_service vs pool_ready
    assert finding["node_id"] == node["id"]
    # detection only — node state must NOT be auto-mutated
    assert client.get(f"/api/v1/nodes/{node['id']}").json()["state"] == "in_service"


# ---------------------------------------------------------------------------
# state-machine guard
# ---------------------------------------------------------------------------
def test_illegal_transition_is_rejected(client):
    node = next(iter(STORE.node_instances.values()))
    assert node.state == NS.pool_ready
    with pytest.raises(HTTPException) as exc:
        lifecycle.advance_node(node, NS.in_service)   # pool_ready -/-> in_service
    assert exc.value.status_code == 409
    assert node.state == NS.pool_ready                # unchanged


def test_fake_nico_guards_preconditions(client):
    host_id = "nh-su-2-rack-01-tray-00"
    with pytest.raises(HTTPException) as exc:
        FAKE_NICO.provision(host_id, "img")           # must be reserved first
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# full-stack detail: trace bus / VPC(segment) / VAST storage
# ---------------------------------------------------------------------------
def test_full_stack_trace_segment_and_storage(client):
    tid = _mk_tenant(client)
    order = _new_order(client, tid, racks=1).json()
    assert order["state"] == "delivered"
    assert order["segment_id"] and order["storage_ids"]

    # 시스템 세부 트레이스 — 전 채널이 기록되어야 한다
    tr = client.get("/api/v1/trace?limit=2000").json()
    channels = {e["channel"] for e in tr}
    for ch in ("REST", "gRPC", "Redfish", "DHCP", "PXE", "cloud-init",
               "NVUE/HBN", "UFM", "NMX", "VAST-API", "internal"):
        assert ch in channels, f"missing channel: {ch}"

    # BMC→DHCP→PXE 메시지 페이로드 확인
    dhcp = [e for e in tr if e["channel"] == "DHCP"][0]
    assert dhcp["payload"]["yiaddr"].startswith("10.")
    redfish = [e for e in tr if e["channel"] == "Redfish"
               and "Reset" in e["op"]][0]
    assert redfish["payload"] == {"ResetType": "ForceRestart"}

    # 호스트에 IP·VPC 바인딩 반영
    node = client.get(f"/api/v1/nodes/{order['node_ids'][0]}").json()
    host = client.get(f"/fake-nico/hosts/{node['nico_host_id']}").json()
    assert host["host_ip"].startswith("10.")
    assert host["segment_id"] == order["segment_id"]

    # order_id 필터
    mine = client.get(f"/api/v1/trace?order_id={order['id']}&limit=2000").json()
    assert mine and all(e["order_id"] == order["id"] for e in mine)

    # VAST 뷰·쿼터·QoS
    views = client.get("/fake-vast/views").json()
    assert len(views) == 1
    assert views[0]["capacity_tb"] == 500 and views[0]["qos_gbps"] == 40
    assert views[0]["tenant_ref"] == tid

    # 회수 시 스토리지·VPC 완전 해체
    term = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate",
        "allocation_id": order["allocation_ids"][0]}).json()
    assert term["state"] == "closed"
    assert client.get("/fake-vast/views").json() == []
    assert client.get("/fake-nico/segments").json() == []
    host = client.get(f"/fake-nico/hosts/{node['nico_host_id']}").json()
    assert host["host_ip"] == "" and host["segment_id"] is None


# ---------------------------------------------------------------------------
# /flow 검증 콘솔 + demo-only endpoints
# ---------------------------------------------------------------------------
def test_flow_console_served(client):
    r = client.get("/flow")
    assert r.status_code == 200
    assert "동작 검증 콘솔" in r.text
    assert "/api/v1/reconcile/run" in r.text        # console drives real APIs


def test_demo_endpoints_stage_reconcile_scenarios(client):
    # GHOST via REST
    r = client.post("/fake-nico/hosts/ghost", json={"host_id": "nh-ghost-ui"})
    assert r.status_code == 201
    # ORPHAN via REST
    assert client.delete("/fake-nico/hosts/nh-su-1-rack-00-tray-01").status_code == 200
    # MISMATCH via REST (force state behind the control plane's back)
    r = client.patch("/fake-nico/hosts/nh-su-1-rack-00-tray-02/state",
                     json={"state": "released"})
    assert r.status_code == 200 and r.json()["state"] == "released"

    rep = client.post("/api/v1/reconcile/run").json()
    assert rep["ghosts_registered"] == 1
    assert rep["orphans_cordoned"] == 1
    assert rep["mismatches"] == 1

    # job latency config for the poll demo
    r = client.patch("/fake-nico/config", json={"job_latency": 3})
    assert r.json() == {"job_latency": 3}
    client.patch("/fake-nico/config", json={"job_latency": 0})


# ---------------------------------------------------------------------------
# NicoHttpAdapter parity — same contract over the REST surface
# ---------------------------------------------------------------------------
def test_http_adapter_parity(client):
    from fastapi.testclient import TestClient

    from app.adapters import NicoHttpAdapter, wait_job
    from app.main import app

    http = TestClient(app, base_url="http://testserver/fake-nico")
    adapter = NicoHttpAdapter(base_url="", client=http)

    host_id = "nh-su-1-rack-01-tray-00"
    adapter.reserve(host_id)
    assert wait_job(adapter, adapter.provision(host_id, "img")).state == "succeeded"
    host = adapter.allocate(host_id, "tnt-x")
    assert host.instance_id
    assert wait_job(adapter, adapter.release(host.instance_id)).state == "succeeded"
    assert wait_job(adapter, adapter.sanitize(host_id)).state == "succeeded"
    assert adapter.get_host(host_id).state == NicoHostState.pool_ready
    assert adapter.get_sanitize_report(host_id).passed


def test_dpu_isolation_reflects_real_nico_behavior(client):
    """DPU isolation 세부 동작 — 실 NICo(infra-controller) 재현 회귀.

    carbide 기록 → dpu-agent gRPC 폴링 → FNN NVUE 렌더/HBN 적용(호스트당 1건,
    카운트 규약 유지) → BGP 수렴 검증이 isolating 트레이스에 나타나야 한다."""
    r = client.post("/api/v1/tenants", json={
        "name": "dpu-iso", "isolation_tier": "bare_metal_dedicated"})
    tid = r.json()["id"]
    order = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 1}).json()
    assert order["state"] == "delivered"

    seg = [s for s in client.get("/fake-nico/segments").json()
           if s["tenant_ref"] == tid][0]
    assert seg["virtualizer"] == "fnn"
    assert seg["vrf_dataplane"] == f"vpc_{seg['l3vni']}"

    flow = client.get(f"/api/v1/orders/{order['id']}/flow").json()
    iso = [st for st in flow["stages"] if st["state"] == "isolating"][0]
    assert iso["by_channel"]["NVUE/HBN"] == 18 + 5        # 트레이 18 + CPU 5
    assert iso["by_channel"].get("gRPC", 0) >= 1          # config fetcher 폴링
    calls = " ".join(c["op"] + (c.get("detail") or "")
                     for c in iso["apis"])
    assert "GetManagedHostNetworkConfig" in calls
    assert "pf0hpf" in calls and "BGP summary" in calls
    assert f"vpc_{seg['l3vni']}" in calls                 # 실 VRF 명명 규칙


def test_partial_reclaim_control_plane_api_flow(client):
    """부분 SU 회수 — CP 하부 API 누락 회귀 (실사용 이슈).

    잔여 할당이 있으면 UFM은 포트 언바인드(P_Key 유지)여야 하고, 회수에도
    개통과 대칭으로 호스트별 HBN 해체·DHCP lease 회수·EVPN withdraw가
    reclaiming 버킷에 나타나야 한다."""
    r = client.post("/api/v1/tenants", json={
        "name": "partial-ai", "isolation_tier": "bare_metal_dedicated"})
    tid = r.json()["id"]

    def order(racks):
        o = client.post("/api/v1/orders", json={
            "tenant_id": tid, "kind": "new",
            "blueprint_key": "vr-nvl72", "racks": racks}).json()
        assert o["state"] == "delivered", o.get("error")
        return o

    o1, o2 = order(2), order(1)
    pkey = client.get(f"/api/v1/fabric/ib?tenant_id={tid}").json()[
        "selected"]["pkey"]

    # ── 부분 회수 (1랙) — 잔여 2랙 유지 ───────────────────────────────
    t = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate",
        "allocation_id": o2["allocation_ids"][0]}).json()
    assert t["state"] == "closed"
    stages = {s["state"]: s for s in client.get(
        f"/api/v1/orders/{t['id']}/flow").json()["stages"]}
    # 고객 포털 → NeoCloud OS 북바운드 호출이 received 버킷에 카운트
    assert stages["received"]["by_channel"].get("REST") == 1
    assert any("POST /orders" in a["op"]
               for a in stages["received"]["apis"])
    rec = stages["reclaiming"]
    by = rec["by_channel"]
    assert by.get("NVUE/HBN") == 18                  # 호스트별 FNN 해체
    assert by.get("DHCP") == 18                      # lease 회수
    assert by.get("gRPC", 0) >= 1                    # agent 폴링 감지
    calls = " ".join(a["op"] + (a.get("detail") or "") for a in rec["apis"])
    assert f"PATCH /ufmRest/resources/pkeys/{pkey}" in calls
    assert "P_Key" in calls and "유지" in calls
    assert "EVPN withdraw" in calls

    sel = client.get(f"/api/v1/fabric/ib?tenant_id={tid}").json()["selected"]
    assert sel["racks"] == 2 and sel["pkey"] == pkey  # 잔여 클러스터 무영향

    # ── 마지막 회수 — 이때만 P_Key 파티션 제거 ──────────────────────────
    t2 = client.post("/api/v1/orders", json={
        "tenant_id": tid, "kind": "terminate",
        "allocation_id": o1["allocation_ids"][0]}).json()
    assert t2["state"] == "closed"
    rec2 = {s["state"]: s for s in client.get(
        f"/api/v1/orders/{t2['id']}/flow").json()["stages"]}["reclaiming"]
    calls2 = " ".join(a["op"] + (a.get("detail") or "") for a in rec2["apis"])
    assert f"DELETE /ufmRest/resources/pkeys/{pkey}" in calls2
    assert "파티션 제거" in calls2
