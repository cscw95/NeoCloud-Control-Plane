"""Service lifecycle (M1-lite / M3 / M4-lite) — order pipeline & reconcile.

Implements the developer-flow spec (F1/F4/F5 + reconcile) as a synchronous
saga for the MVP. The step sequence, state transitions and compensation
semantics are exactly what the Temporal workflow will encode later — each
`_step_*` block below maps 1:1 onto a future Temporal activity.

Guard rails:
  - Every state change goes through `advance_node`/`advance_order`, which
    enforce the allowed-transition tables (409 on violation).
  - Isolation is never assumed: acceptance re-runs the tenancy isolation
    report and fails the order if it does not pass.
  - Reconcile detects GHOST / ORPHAN / STATE_MISMATCH between the local
    NodeInstance mirror and NICo; only additive fixes are automatic, anything
    destructive is escalated as a finding.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from . import spec, tenancy
from .shared_services import SHARED
from .adapters import (
    ComputeAdapter,
    LocalNicoAdapter,
    LocalVastAdapter,
    NicoHttpAdapter,
    StorageAdapter,
    wait_job,
)
from .models import (
    AllocationCreate,
    AllocationScope,
    HwState,
    IsolationTier,
    LifecycleEvent,
    NodeInstance,
    NodeLifecycleState as NS,
    OrderCreate,
    OrderKind,
    OrderState as OS,
    PartitionCreate,
    Rack,
    ReconcileFinding,
    ReconcileReport,
    ServiceOrder,
    StorageAllocation,
)
from .nico_fake import FAKE_NICO, NicoHostState
from .store import STORE
from .trace import TRACER, emit
from .vast_fake import FAKE_VAST

router = APIRouter(prefix="/api/v1", tags=["lifecycle"])

DEFAULT_IMAGE = "ubuntu-24.04-nvidia"

# 상품 카탈로그 상수 (데모): 랙(NVL72)당 스토리지 용량·성능 배분
STORAGE_TB_PER_RACK = 500
STORAGE_GBPS_PER_RACK = 40
STORAGE_KIOPS_PER_RACK = 500
CPU_NODES_PER_TENANT = 5          # 테넌트당 기본 제공 범용 CPU 노드 (DPU 장착)


def get_adapter() -> ComputeAdapter:
    """Adapter seam. Set VRCM_NICO_URL to drive the standalone NICo emulator
    over REST (NicoHttpAdapter); unset uses the in-process FakeNico."""
    import os
    url = os.environ.get("VRCM_NICO_URL")
    if url:
        return NicoHttpAdapter(url)
    return LocalNicoAdapter(FAKE_NICO)


def get_storage_adapter() -> StorageAdapter:
    """D4 seam — swap for the real VAST VMS REST adapter later."""
    return LocalVastAdapter(FAKE_VAST)


# ---------------------------------------------------------------------------
# State machines
# ---------------------------------------------------------------------------
NODE_TRANSITIONS: dict[NS, set] = {
    NS.discovered:   {NS.validating},
    NS.validating:   {NS.pool_ready, NS.quarantined},
    NS.quarantined:  {NS.validating, NS.rma},
    NS.pool_ready:   {NS.reserved, NS.validating, NS.cordoned},
    NS.reserved:     {NS.provisioning, NS.pool_ready, NS.cordoned},
    NS.provisioning: {NS.allocated, NS.quarantined, NS.releasing},
    NS.allocated:    {NS.in_service, NS.releasing},
    NS.in_service:   {NS.cordoned, NS.draining},
    NS.cordoned:     {NS.draining, NS.in_service},              # 오탐 해제
    NS.draining:     {NS.releasing},
    NS.releasing:    {NS.sanitizing},
    NS.sanitizing:   {NS.pool_ready, NS.rma},
    NS.rma:          set(),
}

ORDER_TRANSITIONS: dict[OS, set] = {
    OS.received:        {OS.validated, OS.rejected},
    OS.validated:       {OS.reserved, OS.reclaiming, OS.rejected,
                         OS.compensating},          # reserve 단계 실패 시
    OS.reserved:        {OS.provisioning, OS.compensating},
    OS.provisioning:    {OS.isolating, OS.compensating},
    OS.isolating:       {OS.storage_binding, OS.compensating},
    OS.storage_binding: {OS.acceptance, OS.compensating},
    OS.acceptance:      {OS.delivered, OS.compensating},
    OS.delivered:       {OS.closed},
    OS.reclaiming:      {OS.closed},
    OS.compensating:    {OS.failed},
    OS.closed: set(), OS.rejected: set(), OS.failed: set(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def advance_node(node: NodeInstance, to: NS, detail: str = "") -> None:
    if to not in NODE_TRANSITIONS[node.state]:
        raise HTTPException(
            409, f"node '{node.id}': illegal transition "
                 f"{node.state.value} -> {to.value}")
    node.state = to
    node.history.append(LifecycleEvent(state=to.value, detail=detail, at=_now()))


def advance_order(order: ServiceOrder, to: OS, detail: str = "") -> None:
    if to not in ORDER_TRANSITIONS[order.state]:
        raise HTTPException(
            409, f"order '{order.id}': illegal transition "
                 f"{order.state.value} -> {to.value}")
    order.state = to
    order.history.append(LifecycleEvent(state=to.value, detail=detail, at=_now()))


# ---------------------------------------------------------------------------
# Node bootstrap — mirror NICo pool into NodeInstance (Day 0 done)
# ---------------------------------------------------------------------------
def bootstrap_nodes(adapter: Optional[ComputeAdapter] = None) -> int:
    """Register a NodeInstance per NICo host whose tray we know (idempotent)."""
    adapter = adapter or get_adapter()
    created = 0
    with STORE.lock:
        for host in adapter.list_hosts():
            tray = STORE.trays.get(host.tray_id)
            if not tray:
                continue                    # unknown host -> reconcile's GHOST
            nid = f"ni-{tray.id}"
            if nid in STORE.node_instances:
                continue
            rack = STORE.racks[tray.rack_id]
            STORE.node_instances[nid] = NodeInstance(
                id=nid, tray_id=tray.id, rack_id=rack.id, su_id=rack.su_id,
                blueprint_key=rack.blueprint_key, nico_host_id=host.host_id,
                state=NS.pool_ready,
                history=[LifecycleEvent(
                    state=NS.pool_ready.value,
                    detail="bootstrap: NICo Day-0 mirror", at=_now())],
            )
            created += 1
    return created


# ---------------------------------------------------------------------------
# M4-lite placement — NVL-domain (whole-rack) integrity
# ---------------------------------------------------------------------------
def _rack_sellable(rack: Rack) -> bool:
    """A rack is sellable only if unbound AND every node mirror is pool_ready
    (hardware eligibility: a rack with a cordoned/quarantined/stuck tray must
    never be placed — NVL domains are sold whole)."""
    if rack.tenant_id is not None or rack.state != HwState.ready:
        return False
    nodes = STORE.nodes_of_rack(rack.id)
    return (len(nodes) == len(rack.tray_ids)
            and all(n.state == NS.pool_ready for n in nodes))


def _su_allowed_for(su, tenant) -> bool:
    """SU 격리 정책 (M4) — create_allocation의 정책과 동일 기준을 배치
    단계에서 선반영해, 격리 단계에서 뒤늦게 실패하지 않도록 한다.

    ① bare_metal_dedicated 테넌트는 타 테넌트가 점유한 SU에 배치 불가.
    ② 타 dedicated 테넌트가 점유한 SU에는 누구도 배치 불가 (역방향 보호 —
       기존 dedicated 고객의 물리 격리 SLA 훼손 방지)."""
    if tenant is None:
        return True
    owners = {r.tenant_id for r in STORE.racks_of_su(su.id)
              if r.tenant_id and r.tenant_id != tenant.id}
    if not owners:
        return True
    if tenant.isolation_tier == IsolationTier.bare_metal_dedicated:
        return False
    return not any(
        STORE.tenants[o].isolation_tier == IsolationTier.bare_metal_dedicated
        for o in owners if o in STORE.tenants)


def select_racks(blueprint_key: str, count: int,
                 tenant=None) -> Optional[list[Rack]]:
    """Pick `count` sellable racks: best-fit single SU, else spill across SUs
    **within one site**.

    NVL-domain integrity is rack-granular (one NVL72 rack == one domain), so
    spanning SUs is allowed but a partial rack is not. 단, 사이트(AIFactory)
    간에는 IB 패브릭·NVLink가 물리적으로 이어지지 않으므로(STT 가산 ↔ IGIS
    안산) 하나의 클러스터가 사이트를 넘을 수 없다 — spill은 동일 사이트
    내에서만 허용된다. Generation mixing is forbidden by filtering on
    blueprint_key; tenant isolation policy filters SUs (see _su_allowed_for).
    """
    pools: list[tuple] = []                 # (su, free_racks, factory_id)
    for su in sorted(STORE.sus.values(), key=lambda s: s.id):
        if su.blueprint_key != blueprint_key:
            continue
        if not _su_allowed_for(su, tenant):
            continue
        free = [r for r in STORE.racks_of_su(su.id) if _rack_sellable(r)]
        fac = STORE.factory_of_su(su.id)
        pools.append((su, free, fac.id if fac else "(unknown)"))

    fits = [p for p in pools if len(p[1]) >= count]
    if fits:                                # best-fit: smallest sufficient SU
        _, free, _ = min(fits, key=lambda p: len(p[1]))
        return free[:count]

    # spill: 사이트 경계 안에서만 — 사이트 단위 best-fit 후 SU 단위 채움
    by_site: dict[str, list] = {}
    for su, free, fid in pools:
        by_site.setdefault(fid, []).append(free)
    candidates = [(sum(len(f) for f in su_frees), fid)
                  for fid, su_frees in by_site.items()
                  if sum(len(f) for f in su_frees) >= count]
    if not candidates:
        return None
    _, site_id = min(candidates)
    selected: list[Rack] = []
    for free in sorted(by_site[site_id], key=len, reverse=True):
        selected.extend(free[: count - len(selected)])
        if len(selected) == count:
            return selected
    return None


def availability_by_site(blueprint_key: str, tenant=None) -> dict:
    """테넌트 기준 사이트별 계약 가능 랙 수 — dedicated SU 격리·판매가능 반영.

    거절 사유·용량 안내에 사용: 물리 유휴와 달리 이 수치가 실제 주문 가능량."""
    out: dict[str, int] = {}
    for su in STORE.sus.values():
        if su.blueprint_key != blueprint_key:
            continue
        if not _su_allowed_for(su, tenant):
            continue
        free = sum(1 for r in STORE.racks_of_su(su.id) if _rack_sellable(r))
        fac = STORE.factory_of_su(su.id)
        name = fac.name if fac else "(unknown)"
        out[name] = out.get(name, 0) + free
    return out


def _nodes_for_racks(racks: list[Rack]) -> list[NodeInstance]:
    nodes: list[NodeInstance] = []
    for rack in racks:
        rack_nodes = STORE.nodes_of_rack(rack.id)
        if len(rack_nodes) != len(rack.tray_ids):
            raise HTTPException(
                500, f"rack '{rack.id}': node mirror incomplete "
                     f"({len(rack_nodes)}/{len(rack.tray_ids)}) — run reconcile")
        nodes.extend(sorted(rack_nodes, key=lambda n: n.id))
    return nodes


def _teardown_storage_and_segment(order_id: str, storage_ids: list,
                                  segment_id: Optional[str],
                                  adapter: ComputeAdapter) -> None:
    """D4 뷰 회수 + D2 VPC 해체 (best-effort — 실패는 기록 후 계속)."""
    storage = get_storage_adapter()
    for sid in list(storage_ids):
        sa = STORE.storage_allocs.get(sid)
        if sa and sa.state == "active":
            try:
                storage.delete_view(sa.view_path)
                sa.state = "reclaimed"
            except HTTPException as exc:
                emit("NeoCloudOS.D4", "VAST.VMS", "VAST-API",
                     "뷰 회수 실패", str(exc.detail), order_id=order_id)
    if segment_id:
        try:
            adapter.delete_segment(segment_id)
        except HTTPException as exc:
            emit("NeoCloudOS.D2", "NICo.APIService", "REST",
                 "segment 해체 실패", str(exc.detail), order_id=order_id)


# ---------------------------------------------------------------------------
# F1 — new order pipeline (synchronous saga; Temporal activities later)
# ---------------------------------------------------------------------------
def _compensate(order: ServiceOrder, nodes: list[NodeInstance],
                adapter: ComputeAdapter, reason: str) -> ServiceOrder:
    advance_order(order, OS.compensating, f"saga: {reason}")
    _teardown_storage_and_segment(order.id, order.storage_ids,
                                  order.segment_id, adapter)
    order.segment_id = None
    # release tenancy allocations (unbinds racks + removes NVLink partitions)
    for aid in list(order.allocation_ids):
        if aid in STORE.allocations:
            tenancy.delete_allocation(aid)
    order.allocation_ids.clear()
    _sync_cpu_nodes(order.tenant_id, None)     # 마지막 allocation이면 CPU 반납
    # roll nodes back, reverse order of progress; best-effort per node — a
    # rollback that itself fails is cordoned/escalated, never raises out
    for node in nodes:
        try:
            if node.state == NS.reserved:
                adapter.unreserve(node.nico_host_id)
                advance_node(node, NS.pool_ready, "saga: unreserved")
            elif node.state in (NS.provisioning, NS.allocated):
                if node.nico_instance_id:
                    wait_job(adapter, adapter.release(node.nico_instance_id))
                    node.nico_instance_id = None
                else:
                    adapter.abort_provision(node.nico_host_id)
                advance_node(node, NS.releasing, "saga: rollback release")
                job = wait_job(adapter, adapter.sanitize(node.nico_host_id))
                advance_node(node, NS.sanitizing, "saga: sanitizing")
                if job.state == "succeeded":
                    advance_node(node, NS.pool_ready, "saga: sanitized, pool return")
                else:
                    advance_node(node, NS.rma, f"saga: sanitize failed — {job.detail}")
        except HTTPException as exc:
            if NS.cordoned in NODE_TRANSITIONS[node.state]:
                advance_node(node, NS.cordoned,
                             f"saga rollback failed: {exc.detail} — reconcile 필요")
            else:
                node.history.append(LifecycleEvent(
                    state=node.state.value,
                    detail=f"saga rollback failed: {exc.detail} — manual "
                           "intervention", at=_now()))
        node.tenant_id = None
        node.order_id = None
    advance_order(order, OS.failed, reason)
    order.error = reason
    return order


def _sync_cpu_nodes(tenant_id: str, segment_id: Optional[str]) -> list:
    """테넌트 기본 CPU 노드 동기화 — allocation 보유 시 5대 보장, 없으면 반납.

    CPU 노드는 DPU 장착 범용 노드로, 테넌트 VPC(segment)에 HBN으로 연결된다
    (Ethernet 전용 — IB/NVLink 없음)."""
    s = STORE
    has_gpu = any(a.tenant_id == tenant_id for a in s.allocations.values())
    mine = [c for c in s.cpu_nodes.values() if c.tenant_id == tenant_id]
    if not has_gpu:                             # 마지막 회수 → 전량 반납
        for cn in mine:
            emit("NeoCloudOS.D2", f"DPU-Agent({cn.dpu_id})", "NVUE/HBN",
                 "CPU 노드 VPC 연결 해제",
                 f"{cn.id} 반납 — VRF 바인딩 제거, DHCP 임대 회수")
            cn.tenant_id = None
            cn.state = "pool_ready"
            cn.host_ip = ""
            cn.segment_id = None
        return []
    ni = s.netiso.get(tenant_id)
    need = CPU_NODES_PER_TENANT - len(mine)
    pool = [c for c in s.cpu_nodes.values() if c.state == "pool_ready"]
    for cn in pool[:max(0, need)]:
        idx = int(cn.id.rsplit("-", 1)[1])
        cn.tenant_id = tenant_id
        cn.state = "allocated"
        cn.host_ip = f"10.250.0.{idx + 1}"
        cn.segment_id = segment_id
        emit(f"DPU-DHCP({cn.dpu_id})", cn.id, "DHCP",
             "DISCOVER → ACK", f"CPU 노드 {cn.host_ip}/24 임대 (DPU 종단)",
             payload={"yiaddr": cn.host_ip, "node": cn.id})
        emit("NeoCloudOS.D2", f"DPU-Agent({cn.dpu_id})", "NVUE/HBN",
             "CPU 노드 VPC 연결 (FNN NVUE 렌더)",
             f"{cn.id} ({cn.cpu_arch} {cn.cores}c) → "
             f"vrf vpc_{ni.compute_l3vni if ni else '?'} 바인딩 — pf0hpf ACL 체인 "
             f"적용 · 테넌트 기본 제공 {CPU_NODES_PER_TENANT}대",
             payload={"node": cn.id, "dpu": cn.dpu_id,
                      "vrf": f"vpc_{ni.compute_l3vni}" if ni else None,
                      "template": "nvue_startup_fnn.conf",
                      "segment": segment_id, "host_ip": cn.host_ip})
        mine.append(cn)
    return mine


# -- 단계 컨텍스트 재구성 (승인 게이트 사이에도 order에서 복원 가능) ----------
def _ctx_nodes(order: ServiceOrder) -> list:
    return [STORE.node_instances[nid] for nid in order.node_ids
            if nid in STORE.node_instances]


def _ctx_racks(order: ServiceOrder) -> list:
    seen: list[str] = []
    for n in _ctx_nodes(order):
        if n.rack_id not in seen:
            seen.append(n.rack_id)
    return [STORE.racks[r] for r in seen if r in STORE.racks]


def _stage_validated(order: ServiceOrder, adapter: ComputeAdapter) -> None:
    s = STORE
    with s.lock:
        oid = order.id
        # -- validate ------------------------------------------------------
        tenant = s.tenants.get(order.tenant_id)
        if not tenant:
            advance_order(order, OS.rejected, f"unknown tenant '{order.tenant_id}'")
            order.error = order.history[-1].detail
            return
        if order.blueprint_key not in spec.BLUEPRINTS or order.racks < 1:
            advance_order(order, OS.rejected,
                          "invalid spec: blueprint_key/racks required")
            order.error = order.history[-1].detail
            return
        if order.storage_mode == "manual" and order.storage_tb <= 0:
            advance_order(order, OS.rejected,
                          "invalid storage spec: manual 모드는 용량(TB) 필수")
            order.error = order.history[-1].detail
            return
        advance_order(order, OS.validated, "policy: tenant/spec ok")

        # -- placement (M4-lite, 격리 정책 반영) -----------------------------
        racks = select_racks(order.blueprint_key, order.racks, tenant=tenant)
        if racks is None:
            avail = availability_by_site(order.blueprint_key, tenant)
            detail = " · ".join(f"{k} {v}랙"
                                for k, v in sorted(avail.items())) or "없음"
            advance_order(order, OS.rejected,
                          f"insufficient capacity: {order.racks}x "
                          f"{order.blueprint_key} rack(s) not available "
                          "within a single site — 이 테넌트 기준 계약 가능: "
                          f"{detail} (사이트 스팬 불가 · dedicated SU 격리로 "
                          "부분 점유 SU의 유휴 랙은 제외)")
            order.error = order.history[-1].detail
            return
        nodes = _nodes_for_racks(racks)
        order.node_ids = [n.id for n in nodes]
        emit("NeoCloudOS.M4", "NeoCloudOS.M1", "internal", "배치 결정",
             f"NVL 도메인 무결성 배치 — {len(racks)} rack / {len(nodes)} tray "
             "(세대 혼합 금지·전력 캡·비정상 트레이 랙 제외)",
             payload={"racks": [r.id for r in racks],
                      "blueprint": order.blueprint_key,
                      "power_cap_kw_total": sum(r.power_cap_kw for r in racks)},
             order_id=oid)


def _stage_reserved(order: ServiceOrder, adapter: ComputeAdapter) -> None:
    s = STORE
    with s.lock:
        oid = order.id
        nodes = _ctx_nodes(order)
        racks = _ctx_racks(order)
        # -- reserve ---------------------------------------------------------
        # 단계 진입을 먼저 기록 — ReserveHost 호출들이 'reserved' 단계 창에
        # 귀속되도록 (GET /orders/{id}/flow 의 시간창 버킷팅 기준)
        advance_order(order, OS.reserved,
                      f"{len(nodes)} node(s) across {len(racks)} rack(s)")
        reserved: list[NodeInstance] = []
        for node in nodes:
            if node.state != NS.pool_ready:
                _compensate(order, reserved, adapter,
                            f"node '{node.id}' not pool_ready")
                return
            emit("NeoCloudOS.D1", "NICo.APIService", "gRPC", "ReserveHost",
                 f"{node.nico_host_id} 예약 (주문 {oid})",
                 payload={"host_id": node.nico_host_id, "order_ref": oid},
                 order_id=oid, host_id=node.nico_host_id)
            try:
                adapter.reserve(node.nico_host_id)
            except HTTPException as exc:
                # NICo disagrees (host gone / not pool_ready) — cordon the
                # node so placement skips it, compensate, escalate via reconcile
                advance_node(node, NS.cordoned,
                             f"reserve failed: {exc.detail} — break-fix track")
                _compensate(order, reserved, adapter,
                            f"reserve failed on '{node.id}': {exc.detail}")
                return
            advance_node(node, NS.reserved, f"order {oid}")
            node.order_id = oid
            reserved.append(node)


def _stage_provisioning(order: ServiceOrder, adapter: ComputeAdapter) -> None:
    s = STORE
    with s.lock:
        oid = order.id
        nodes = _ctx_nodes(order)
        tenant = s.tenants[order.tenant_id]
        # -- provision (NICo Day 1) ------------------------------------------
        advance_order(order, OS.provisioning, f"image={DEFAULT_IMAGE}")
        for node in nodes:
            advance_node(node, NS.provisioning, "PXE: OS install + lockdown")
            emit("NeoCloudOS.D1", "NICo.APIService", "REST",
                 f"POST /hosts/{node.nico_host_id}/provision",
                 "베어메탈 프로비저닝 요청 — BMC 제어→DHCP→PXE→cloud-init은 "
                 "NICo가 오케스트레이션",
                 payload={"host_id": node.nico_host_id,
                          "image_ref": DEFAULT_IMAGE,
                          "cloud_init": ["static-ip", "ssh-key",
                                         "uefi-lockdown", "bmc-cred-rotate"]},
                 order_id=oid, host_id=node.nico_host_id)
            try:
                job = wait_job(adapter, adapter.provision(
                    node.nico_host_id, DEFAULT_IMAGE))
                if job.state != "succeeded":
                    raise HTTPException(502, f"provision failed: {job.detail}")
                emit("NeoCloudOS.D1", "NICo.APIService", "gRPC",
                     "AllocateInstance",
                     f"{node.nico_host_id} → 테넌트 {tenant.id} 인스턴스 생성",
                     payload={"host_id": node.nico_host_id,
                              "tenant_ref": tenant.id,
                              "instance_type": node.blueprint_key},
                     order_id=oid, host_id=node.nico_host_id)
                host = adapter.allocate(node.nico_host_id, tenant.id)
            except HTTPException as exc:
                advance_node(node, NS.quarantined, str(exc.detail))
                others = [n for n in nodes if n is not node]
                _compensate(order, others, adapter,
                            f"provision failed on '{node.id}': {exc.detail}")
                return
            node.nico_instance_id = host.instance_id
            node.tenant_id = tenant.id
            advance_node(node, NS.allocated, f"nico instance {host.instance_id}")


def _stage_isolating(order: ServiceOrder, adapter: ComputeAdapter) -> None:
    s = STORE
    with s.lock:
        oid = order.id
        nodes = _ctx_nodes(order)
        racks = _ctx_racks(order)
        tenant = s.tenants[order.tenant_id]
        # -- isolate: 3-plane 격리 (VPC/DPU + IB P_Key + NVLink 파티션) --------
        advance_order(order, OS.isolating, "3-plane 격리 구성 (ETH/IB/NVLink)")
        by_su: dict[str, list[str]] = {}
        for rack in racks:
            by_su.setdefault(rack.su_id, []).append(rack.id)
        try:
            for su_id, rack_ids in by_su.items():
                alloc = tenancy.create_allocation(AllocationCreate(
                    tenant_id=tenant.id, scope=AllocationScope.rack_set,
                    su_id=su_id, rack_ids=rack_ids))
                order.allocation_ids.append(alloc.id)

            # ① Ethernet: 테넌트 VPC(network segment) — DPU HBN이 강제
            ni = s.netiso[tenant.id]
            emit("NeoCloudOS.D2", "NICo.APIService", "REST", "POST /segments",
                 f"테넌트 VPC 생성 요청 — VRF {ni.vrf}, L3VNI "
                 f"{ni.compute_l3vni}, 대상 host {len(nodes)}대",
                 payload={"tenant_ref": tenant.id, "vrf": ni.vrf,
                          "l3vni": ni.compute_l3vni,
                          "converged_vni": ni.converged_vni,
                          "hosts": [n.nico_host_id for n in nodes]},
                 order_id=oid)
            seg = adapter.create_segment(
                tenant.id, ni.vrf, ni.compute_l3vni, ni.converged_vni,
                [n.nico_host_id for n in nodes],
                allocation_id=order.allocation_ids[0])
            order.segment_id = seg.segment_id

            # ② InfiniBand: UFM P_Key 파티션 (scale-out E-W)
            pkey = ni.ib_pkey or (0x8000 + s.next_seq("pkey"))
            ni.ib_pkey = pkey          # 테넌트당 1개 유지 (확장 시 재사용)
            emit("NeoCloudOS.D2", "UFM", "UFM",
                 "POST /ufmRest/resources/pkeys",
                 f"IB 파티션 — P_Key {hex(pkey)}에 테넌트 포트 GUID 바인딩 "
                 f"({len(nodes)} host, full-membership)",
                 payload={"pkey": hex(pkey), "index0": True,
                          "ip_over_ib": False, "membership": "full",
                          "guids": f"{len(nodes)} host ports"},
                 order_id=oid)

            # ③ NVLink: NMX 파티션 (rack = NVL 도메인 단위)
            for rack in racks:
                part = tenancy.create_partition(PartitionCreate(
                    rack_id=rack.id, tenant_id=tenant.id,
                    tray_ids=list(rack.tray_ids)))
                emit("NeoCloudOS.D2", "NMX", "NMX",
                     f"POST /nmx/v1/domains/{rack.id}/partitions",
                     f"NVLink 파티션 {part.partition_id} — {rack.id} "
                     f"트레이 {len(rack.tray_ids)}개 전체 (도메인 무결성 보존)",
                     payload={"domain": rack.id,
                              "partition_id": part.partition_id,
                              "trays": len(rack.tray_ids),
                              "tenant": tenant.id},
                     order_id=oid)
            # ④ 기본 제공 CPU 노드 — DPU 장착, 동일 VPC에 HBN 연결
            cpu_nodes = _sync_cpu_nodes(tenant.id, seg.segment_id)
            order.history[-1].detail = (
                f"VPC {seg.segment_id} (VRF {ni.vrf}/VNI {ni.compute_l3vni}) · "
                f"IB P_Key {hex(pkey)} · NVLink 파티션 {len(racks)}개 · "
                f"CPU 노드 {len(cpu_nodes)}대(DPU) VPC 연결")
        except HTTPException as exc:
            _compensate(order, nodes, adapter,
                        f"isolation failed: {exc.detail}")
            return


def _stage_storage_binding(order: ServiceOrder, adapter: ComputeAdapter) -> None:
    s = STORE
    with s.lock:
        oid = order.id
        nodes = _ctx_nodes(order)
        racks = _ctx_racks(order)
        tenant = s.tenants[order.tenant_id]
        ni = s.netiso[tenant.id]
        # -- storage: VAST VMS 제어 (D4) — 자동(랙 비례) / 수동(직접 지정) ------
        storage = get_storage_adapter()
        if order.storage_mode == "manual":
            capacity_tb = order.storage_tb
            gbps = order.storage_gbps or round(
                capacity_tb / STORAGE_TB_PER_RACK * STORAGE_GBPS_PER_RACK, 1)
            kiops = round(gbps / STORAGE_GBPS_PER_RACK
                          * STORAGE_KIOPS_PER_RACK, 1)
        else:
            capacity_tb = STORAGE_TB_PER_RACK * len(racks)
            gbps = STORAGE_GBPS_PER_RACK * len(racks)
            kiops = STORAGE_KIOPS_PER_RACK * len(racks)
        view_path = f"/tenants/{tenant.id}/{oid}"
        advance_order(order, OS.storage_binding,
                      f"VAST view {view_path} · {capacity_tb}TB · "
                      f"QoS {gbps}GB/s ({'수동 지정' if order.storage_mode == 'manual' else '자동 산정'})")
        try:
            storage.create_view(view_path, tenant.id,
                                export_subnet=f"vrf:{ni.vrf}",
                                allocation_id=order.allocation_ids[0])
            storage.set_quota(view_path, capacity_tb)
            storage.set_qos(view_path, gbps, kiops)
            sid = f"st-{s.next_seq('storage')}"
            s.storage_allocs[sid] = StorageAllocation(
                id=sid, tenant_id=tenant.id, order_id=oid,
                allocation_id=order.allocation_ids[0], view_path=view_path,
                capacity_tb=capacity_tb, qos_gbps=gbps)
            order.storage_ids.append(sid)
        except HTTPException as exc:
            _compensate(order, nodes, adapter,
                        f"storage binding failed: {exc.detail}")
            return


def _stage_acceptance(order: ServiceOrder, adapter: ComputeAdapter) -> None:
    s = STORE
    with s.lock:
        oid = order.id
        nodes = _ctx_nodes(order)
        tenant = s.tenants[order.tenant_id]
        # -- acceptance: isolation report must pass ---------------------------
        advance_order(order, OS.acceptance, "running isolation verification")
        for check, msg in (
                ("cross-vrf", "교차 VRF 도달성 부정 테스트 — 타 테넌트 VNI로 "
                              "ICMP/TCP 차단 확인"),
                ("ib-pkey", "IB 파티션 외 노드 조회 차단 확인 (P_Key 경계)"),
                ("nvlink", "NVLink 파티션 경계 검사 — 파티션 외 GPU P2P 불가")):
            emit("NeoCloudOS.D2", "IsolationVerifier", "internal",
                 f"verify:{check} → PASS", msg, order_id=oid)
        report = tenancy.isolation_report(tenant.id)
        if not report.ok:
            fails = [f.message for f in report.findings if f.severity == "fail"]
            _compensate(order, nodes, adapter,
                        f"isolation verification failed: {fails}")
            return
        # -- Shared Services(⑦): 클러스터 자격증명 발급 — IAM SA + Vault ------
        SHARED.issue_service_account(tenant.id, oid)
        SHARED.write_secret(f"tenants/{tenant.id}/{oid}/storage-s3",
                            "s3-access-key", tenant.id, oid)
        SHARED.write_secret(f"tenants/{tenant.id}/{oid}/oob-redfish",
                            "redfish-cred", tenant.id, oid)


def _stage_delivered(order: ServiceOrder, adapter: ComputeAdapter) -> None:
    s = STORE
    with s.lock:
        nodes = _ctx_nodes(order)
        report = tenancy.isolation_report(order.tenant_id)
        for node in nodes:
            advance_node(node, NS.in_service, "acceptance passed")
        _build_access_package(order)
        advance_order(order, OS.delivered,
                      f"isolation ok ({len(report.findings)} checks)")


def _build_access_package(order: ServiceOrder) -> None:
    """딜리버리 패키지 — 고객에게 전달하는 접속정보 + 보안 인증 정보(목업).

    client_secret은 여기서 1회만 노출된다(SEC09) — 이후에는 Vault 회전 대상.
    호출 전제: STORE.lock 보유."""
    s = STORE
    oid, tid = order.id, order.tenant_id
    iso = s.netiso.get(tid)
    views = [s.storage_allocs[i] for i in order.storage_ids
             if i in s.storage_allocs]
    secret = SHARED.pop_sa_secret(oid)
    order.access_package = {
        "issued_at": _now(),
        "note": "client_secret은 딜리버리 시 1회만 제공 — 이후 Vault에서 "
                "90일 주기 자동 회전 (SEC09)",
        "ssh_bastion": {
            "host": "bastion.neocloud.skt", "ip": "10.250.255.10",
            "user": tid, "auth": "ed25519 공개키 등록 + OTP(MFA)"},
        "api": {
            "base_url": "https://api.neocloud.skt/v1",
            "token_url": f"https://iam.neocloud.skt/realms/{tid}"
                         "/protocol/openid-connect/token",
            "client_id": f"sa-{oid}", "client_secret": secret,
            "scope": "nodes:read storage:mount telemetry:write"},
        "console": {
            "pam_url": "https://pam.neocloud.skt/sessions",
            "note": "노드 시리얼 콘솔 — PAM 승인 후 접속 (세션 녹화, TTL)"},
        "storage": [{
            "mount": f"vast-vip.tenant-data:{v.view_path}",
            "protocol": "NFS4/RDMA",
            "s3_endpoint": "https://s3.neocloud.skt",
            "credential_vault": f"secret/tenants/{tid}/{oid}/storage-s3",
        } for v in views],
        "network": {
            "vrf": iso.vrf if iso else None,
            "compute_l3vni": iso.compute_l3vni if iso else None,
            "ib_pkey": (hex(iso.ib_pkey)
                        if iso and iso.ib_pkey is not None else None)},
    }
    emit("NeoCloudOS.M1", "CustomerPortal", "internal",
         f"deliver:access-package → {oid}",
         "접속정보 + 보안 인증 패키지 발급 — SSH bastion·OIDC 자격증명"
         "(1회 노출)·스토리지 마운트·PAM 콘솔·VRF/P_Key", order_id=oid,
         payload={"client_id": f"sa-{oid}", "client_secret": "****(1회 노출)",
                  "storage_views": len(views)})
    SHARED.audit("neocloud-os", "delivery.access-package.issue", oid,
                 tenant_ref=tid)


FULFILL_STAGES = [
    (OS.validated, _stage_validated),
    (OS.reserved, _stage_reserved),
    (OS.provisioning, _stage_provisioning),
    (OS.isolating, _stage_isolating),
    (OS.storage_binding, _stage_storage_binding),
    (OS.acceptance, _stage_acceptance),
    (OS.delivered, _stage_delivered),
]
_TERMINAL_STATES = {OS.rejected, OS.failed, OS.closed, OS.delivered}


def run_new_order(body: OrderCreate, adapter: ComputeAdapter) -> ServiceOrder:
    s = STORE
    with s.lock:
        oid = f"ord-{s.next_seq('order')}"
        order = ServiceOrder(
            id=oid, tenant_id=body.tenant_id, kind=body.kind,
            blueprint_key=body.blueprint_key, racks=body.racks,
            approval_mode=body.approval_mode,
            storage_mode=body.storage_mode, storage_tb=body.storage_tb,
            storage_gbps=body.storage_gbps,
            history=[LifecycleEvent(state=OS.received.value, at=_now())],
        )
        s.orders[oid] = order
        emit("Portal/API", "NeoCloudOS.M1", "REST", f"POST /orders → {oid}",
             f"신규 개통 주문 접수 — {body.blueprint_key} x {body.racks} rack"
             + (" (운영 승인 대기)" if body.approval_mode else ""),
             payload={"tenant_id": body.tenant_id, "kind": "new",
                      "blueprint_key": body.blueprint_key,
                      "racks": body.racks,
                      "approval_mode": body.approval_mode},
             order_id=oid)
    if body.approval_mode:                 # 운영 포털 fulfillment 승인 큐로
        order.pending_stage = OS.validated.value
        return order
    for _, stage_fn in FULFILL_STAGES:     # 자동 모드: 전 단계 연속 실행
        stage_fn(order, adapter)
        if order.state in (OS.rejected, OS.failed):
            return order
    return order


def approve_next_stage(order_id: str, adapter: ComputeAdapter) -> ServiceOrder:
    """운영자 승인 — pending 단계 1개를 실행하고 다음 게이트에서 대기."""
    order = STORE.orders.get(order_id)
    if not order:
        raise HTTPException(404, f"order '{order_id}' not found")
    if not order.approval_mode or not order.pending_stage:
        raise HTTPException(409, f"order '{order_id}'는 승인 대기 상태가 아님")
    stage = OS(order.pending_stage)
    emit("Operator", "NeoCloudOS.M1", "internal",
         f"운영자 승인 → {stage.value}",
         f"fulfillment 게이트 통과 — {order.id} ({order.blueprint_key} "
         f"x {order.racks} rack)", order_id=order.id)
    order.pending_stage = None
    dict(FULFILL_STAGES)[stage](order, adapter)
    if order.state not in _TERMINAL_STATES:
        idx = [st for st, _ in FULFILL_STAGES].index(stage)
        order.pending_stage = FULFILL_STAGES[idx + 1][0].value
    return order


def reject_fulfillment(order_id: str, adapter: ComputeAdapter,
                       reason: str) -> ServiceOrder:
    """운영자 거절 — 진행분이 있으면 saga 보상으로 원복."""
    order = STORE.orders.get(order_id)
    if not order:
        raise HTTPException(404, f"order '{order_id}' not found")
    if not order.approval_mode or not order.pending_stage:
        raise HTTPException(409, f"order '{order_id}'는 승인 대기 상태가 아님")
    order.pending_stage = None
    emit("Operator", "NeoCloudOS.M1", "internal", "운영자 거절",
         f"{order.id} — {reason}", order_id=order.id)
    if order.state in (OS.received, OS.validated):
        advance_order(order, OS.rejected, f"운영자 거절: {reason}")
        order.error = order.history[-1].detail
        return order
    return _compensate(order, _ctx_nodes(order), adapter,
                       f"운영자 거절: {reason}")


# ---------------------------------------------------------------------------
# F4 — terminate / reclaim pipeline
# ---------------------------------------------------------------------------
def run_terminate_order(body: OrderCreate, adapter: ComputeAdapter) -> ServiceOrder:
    s = STORE
    with s.lock:
        oid = f"ord-{s.next_seq('order')}"
        order = ServiceOrder(
            id=oid, tenant_id=body.tenant_id, kind=body.kind,
            allocation_id=body.allocation_id,
            history=[LifecycleEvent(state=OS.received.value, at=_now())],
        )
        s.orders[oid] = order
        # 고객 포털 → NeoCloud OS 북바운드 호출도 CP 트레이스에 카운트
        # (신규 주문의 접수 emit과 대칭 — /arch·/flow received 버킷에 표시)
        emit("Portal/API", "NeoCloudOS.M1", "REST",
             f"POST /orders → {oid} (terminate)",
             f"자원 회수 주문 접수 — allocation {body.allocation_id} "
             f"(tenant {body.tenant_id})",
             payload={"tenant_id": body.tenant_id, "kind": "terminate",
                      "allocation_id": body.allocation_id},
             order_id=oid)

        alloc = s.allocations.get(body.allocation_id or "")
        if not alloc or alloc.tenant_id != body.tenant_id:
            advance_order(order, OS.rejected,
                          f"allocation '{body.allocation_id}' not found "
                          f"for tenant '{body.tenant_id}'")
            order.error = order.history[-1].detail
            return order
        advance_order(order, OS.validated, f"reclaiming allocation {alloc.id}")

        nodes = [n for rid in alloc.rack_ids for n in s.nodes_of_rack(rid)
                 if n.tenant_id == body.tenant_id]
        order.node_ids = [n.id for n in nodes]
        advance_order(order, OS.reclaiming, f"{len(nodes)} node(s)")

        # -- D2/D4 역순 해체: NVLink → IB → ETH(VPC) → 스토리지 --------------
        for rid in alloc.rack_ids:
            for part in s.partitions_of_rack(rid):
                if part.tenant_id == body.tenant_id:
                    emit("NeoCloudOS.D2", "NMX", "NMX",
                         f"DELETE /nmx/v1/domains/{rid}/partitions/"
                         f"{part.partition_id}",
                         f"NVLink 파티션 {part.partition_id} 해체", order_id=oid)
        # UFM: 부분 회수면 회수 랙의 포트 GUID만 언바인드(P_Key 유지),
        # 마지막 회수일 때만 파티션 자체를 제거한다
        ni = s.netiso.get(body.tenant_id)
        pkey = hex(ni.ib_pkey) if (ni and ni.ib_pkey) else "(미부여)"
        other_racks = [r for a in s.allocations.values()
                       if a.tenant_id == body.tenant_id and a.id != alloc.id
                       for r in a.rack_ids]
        ports = sum(len(s.racks[r].tray_ids) for r in alloc.rack_ids
                    if r in s.racks) * 8
        if other_racks:
            emit("NeoCloudOS.D2", "UFM", "UFM",
                 f"PATCH /ufmRest/resources/pkeys/{pkey}",
                 f"부분 회수 — 회수 랙 {len(alloc.rack_ids)}개의 포트 GUID "
                 f"{ports}개만 파티션에서 언바인드 (P_Key {pkey} 유지 — "
                 f"잔여 {len(other_racks)}랙이 계속 사용)",
                 order_id=oid,
                 payload={"pkey": pkey, "unbind_port_guids": ports,
                          "partition": "retained",
                          "remaining_racks": len(other_racks)})
        else:
            emit("NeoCloudOS.D2", "UFM", "UFM",
                 f"DELETE /ufmRest/resources/pkeys/{pkey}",
                 f"마지막 회수 — 테넌트 {body.tenant_id} P_Key {pkey} 파티션 "
                 f"제거 (전 포트 GUID {ports}개 언바인드)",
                 order_id=oid,
                 payload={"pkey": pkey, "unbind_port_guids": ports,
                          "partition": "deleted"})
        segment_id = next((sg.segment_id for sg in FAKE_NICO.list_segments()
                           if sg.allocation_id == alloc.id), None)
        storage_ids = [sa.id for sa in s.storage_allocs.values()
                       if sa.allocation_id == alloc.id and sa.state == "active"]
        _teardown_storage_and_segment(oid, storage_ids, segment_id, adapter)

        rma_nodes: list[str] = []
        stuck_nodes: list[str] = []
        for node in sorted(nodes, key=lambda n: n.id):
            # per-node isolation: one stuck node (e.g. NICo diverged) must not
            # block reclaiming the rest — it is escalated, not retried blindly
            try:
                if node.state == NS.in_service:
                    advance_node(node, NS.draining, "reclaim: tenant workload drain")
                if node.state in (NS.draining, NS.cordoned):
                    if node.state == NS.cordoned:
                        advance_node(node, NS.draining, "reclaim")
                    if node.nico_instance_id:
                        wait_job(adapter, adapter.release(node.nico_instance_id))
                        node.nico_instance_id = None
                    advance_node(node, NS.releasing, "reclaim: nico release")
                    job = wait_job(adapter, adapter.sanitize(node.nico_host_id))
                    advance_node(node, NS.sanitizing,
                                 "nvme erase / gpu+mem wipe / tpm reset / re-attest")
                    if job.state == "succeeded":
                        report = adapter.get_sanitize_report(node.nico_host_id)
                        advance_node(node, NS.pool_ready,
                                     f"sanitized ok ({len(report.steps)} steps) — "
                                     "pool return")
                    else:
                        advance_node(node, NS.rma, f"sanitize failed: {job.detail}")
                        rma_nodes.append(node.id)
            except HTTPException as exc:
                stuck_nodes.append(node.id)
                node.history.append(LifecycleEvent(
                    state=node.state.value,
                    detail=f"reclaim step failed: {exc.detail} — "
                           "manual intervention / reconcile required", at=_now()))
                node.order_id = oid
                continue                      # keep tenant binding — truthful
            node.tenant_id = None
            node.order_id = oid

        # release racks / partitions / allocation (audit stays in history)
        # Shared Services(⑦): 원 개통 주문의 서비스 계정·시크릿 폐기
        src_ord = next((o.id for o in s.orders.values()
                        if alloc.id in (o.allocation_ids or [])), None)
        if src_ord:
            SHARED.revoke_order_credentials(body.tenant_id, src_ord, oid)
        tenancy.delete_allocation(alloc.id)
        _sync_cpu_nodes(body.tenant_id, None)  # 마지막 allocation이면 CPU 반납
        detail = "reclaim complete"
        errors = []
        if rma_nodes:
            detail += f"; RMA escalated: {rma_nodes}"
            errors.append(f"sanitize failed on {rma_nodes} — physical disposal required")
        if stuck_nodes:
            detail += f"; stuck (NICo diverged): {stuck_nodes}"
            errors.append(f"reclaim incomplete on {stuck_nodes} — run reconcile")
        if errors:
            order.error = "; ".join(errors)
        advance_order(order, OS.closed, detail)
        return order


# ---------------------------------------------------------------------------
# M3 reconcile — NodeInstance mirror vs NICo (GHOST / ORPHAN / MISMATCH)
# ---------------------------------------------------------------------------
# NICo host state -> node states that are a legal mirror of it.
_MIRROR: dict = {
    NicoHostState.discovered:   {NS.discovered, NS.validating},
    NicoHostState.quarantined:  {NS.quarantined, NS.rma},
    # discovered/validating 허용: GHOST 등록 직후 풀 편입 전 온보딩 창
    NicoHostState.pool_ready:   {NS.pool_ready, NS.discovered, NS.validating},
    NicoHostState.reserved:     {NS.reserved},
    NicoHostState.provisioning: {NS.provisioning},
    NicoHostState.provisioned:  {NS.provisioning},
    NicoHostState.allocated:    {NS.allocated, NS.in_service,
                                 NS.cordoned, NS.draining},
    NicoHostState.released:     {NS.releasing},
    NicoHostState.sanitizing:   {NS.sanitizing},
    NicoHostState.rma:          {NS.rma},
}
_NODE_TERMINAL = {NS.rma}


def reconcile(adapter: ComputeAdapter) -> ReconcileReport:
    s = STORE
    findings: list[ReconcileFinding] = []
    ghosts = orphans = mismatches = 0

    with s.lock:
        hosts = {h.host_id: h for h in adapter.list_hosts()}
        total_hosts = len(hosts)
        for node in list(s.node_instances.values()):
            host = hosts.pop(node.nico_host_id, None)
            if host is None:                              # -- ORPHAN
                orphans += 1
                if node.state not in _NODE_TERMINAL and node.state != NS.cordoned:
                    if NS.cordoned in NODE_TRANSITIONS[node.state]:
                        advance_node(node, NS.cordoned,
                                     "reconcile: host vanished from NICo")
                    else:
                        node.history.append(LifecycleEvent(
                            state=node.state.value,
                            detail="reconcile: ORPHAN (host vanished from NICo)",
                            at=_now()))
                findings.append(ReconcileFinding(
                    kind="ORPHAN", severity="critical", node_id=node.id,
                    nico_host_id=node.nico_host_id,
                    message=f"node '{node.id}' has no NICo host — hardware "
                            "vanished or site controller out of sync"))
                continue
            allowed = _MIRROR.get(host.state, set())
            if node.state not in allowed:                 # -- STATE_MISMATCH
                mismatches += 1
                severity = ("critical"
                            if node.state == NS.in_service
                            and host.state in (NicoHostState.pool_ready,
                                               NicoHostState.released)
                            else "major")
                findings.append(ReconcileFinding(
                    kind="STATE_MISMATCH", severity=severity, node_id=node.id,
                    nico_host_id=host.host_id,
                    message=f"node '{node.id}' is '{node.state.value}' but "
                            f"NICo host is '{host.state.value}' — escalate, "
                            "no destructive auto-fix"))

        for host in hosts.values():                       # -- GHOST (additive)
            ghosts += 1
            nid = f"ni-{host.tray_id or host.host_id}"
            if nid not in s.node_instances:
                tray = s.trays.get(host.tray_id)
                rack = s.racks.get(tray.rack_id) if tray else None
                s.node_instances[nid] = NodeInstance(
                    id=nid, tray_id=host.tray_id, rack_id=rack.id if rack else "",
                    su_id=rack.su_id if rack else "",
                    blueprint_key=host.sku, nico_host_id=host.host_id,
                    state=NS.discovered,
                    history=[LifecycleEvent(
                        state=NS.discovered.value,
                        detail="reconcile: registered from NICo (GHOST)",
                        at=_now())],
                )
            findings.append(ReconcileFinding(
                kind="GHOST", severity="info", node_id=nid,
                nico_host_id=host.host_id,
                message=f"NICo host '{host.host_id}' unknown to control plane — "
                        "registered as discovered"))

    return ReconcileReport(
        checked_nodes=len(s.node_instances), checked_hosts=total_hosts,
        ghosts_registered=ghosts, orphans_cordoned=orphans,
        mismatches=mismatches,
        ok=not any(f.severity == "critical" for f in findings),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@router.post("/orders", status_code=201, response_model=ServiceOrder)
def create_order(body: OrderCreate) -> ServiceOrder:
    adapter = get_adapter()
    if body.kind == OrderKind.new:
        return run_new_order(body, adapter)
    if body.kind == OrderKind.terminate:
        return run_terminate_order(body, adapter)
    raise HTTPException(501, f"order kind '{body.kind.value}' not implemented "
                             "in this increment (expand/shrink: next)")


@router.post("/orders/{order_id}/approve", response_model=ServiceOrder)
def approve_order(order_id: str) -> ServiceOrder:
    """운영 포털 fulfillment — 다음 파이프라인 단계 1개 승인 실행."""
    return approve_next_stage(order_id, get_adapter())


@router.post("/orders/{order_id}/reject", response_model=ServiceOrder)
def reject_order(order_id: str, reason: str = "운영 정책") -> ServiceOrder:
    """운영 포털 fulfillment — 거절 (진행분은 saga 보상으로 원복)."""
    return reject_fulfillment(order_id, get_adapter(), reason)


@router.get("/orders")
def list_orders(tenant_id: Optional[str] = None) -> list:
    orders = list(STORE.orders.values())
    if tenant_id:
        orders = [o for o in orders if o.tenant_id == tenant_id]
    return orders


@router.get("/orders/{order_id}", response_model=ServiceOrder)
def get_order(order_id: str) -> ServiceOrder:
    order = STORE.orders.get(order_id)
    if not order:
        raise HTTPException(404, f"order '{order_id}' not found")
    return order


@router.get("/orders/{order_id}/flow")
def get_order_flow(order_id: str) -> dict:
    """주문 상태머신 단계별로 하부 호출 API 전체를 버킷팅해 반환.

    귀속 규칙 — 각 단계의 시간창 [stage.at, next_stage.at)에 대해:
      ① order_id 일치(제어면 발신), ② 이 주문의 노드 호스트에서 발생
      (NICo 내부 Redfish/DHCP/PXE/HBN, host_id 기준), ③ 무태그 시스템
      이벤트(VAST VMS 내부·NICo segment store 등 — order/host 무관 계층).
    ③은 동시 주문 시 교차 귀속될 수 있는 데모 단순화다(실구현은 correlation-id).
    /arch 아키텍처 플로우 화면이 소비한다. (payload는 /flow 트레이스에서 확인)
    """
    from datetime import datetime, timedelta

    order = STORE.orders.get(order_id)
    if not order:
        raise HTTPException(404, f"order '{order_id}' not found")
    hosts = set()
    for nid in order.node_ids:
        node = STORE.node_instances.get(nid)
        if node:
            hosts.add(node.nico_host_id)

    hist = order.history
    events = TRACER.query(limit=25000)
    stages = []
    for i, h in enumerate(hist):
        start = h.at
        if i + 1 < len(hist):
            end = hist[i + 1].at
        else:                       # 종결 단계: 이후 에뮬레이션 잡음 제외
            end = (datetime.fromisoformat(h.at)
                   + timedelta(seconds=2)).isoformat()
        apis = [e for e in events
                if start <= e.at < end
                and (e.order_id == order_id
                     or (e.host_id and e.host_id in hosts)
                     or (e.order_id is None and e.host_id is None))]
        by_ch: dict[str, int] = {}
        for e in apis:
            by_ch[e.channel] = by_ch.get(e.channel, 0) + 1
        stages.append({
            "state": h.state, "detail": h.detail, "at": h.at,
            "api_total": len(apis), "by_channel": by_ch,
            "apis": [{"seq": e.seq, "at": e.at, "src": e.src, "dst": e.dst,
                      "channel": e.channel, "op": e.op, "detail": e.detail,
                      "host_id": e.host_id} for e in apis],
        })
    return {"order_id": order_id, "kind": order.kind.value,
            "state": order.state.value, "tenant_id": order.tenant_id,
            "racks": order.racks, "nodes": len(order.node_ids),
            "error": order.error, "stages": stages}


@router.get("/nodes")
def list_nodes(state: Optional[str] = None,
               tenant_id: Optional[str] = None) -> list:
    nodes = list(STORE.node_instances.values())
    if state:
        nodes = [n for n in nodes if n.state.value == state]
    if tenant_id:
        nodes = [n for n in nodes if n.tenant_id == tenant_id]
    return nodes


@router.get("/cpu-nodes")
def list_cpu_nodes(tenant_id: Optional[str] = None) -> list:
    """범용 CPU 노드 (DPU 장착, 테넌트당 기본 5대 · VPC 연결)."""
    nodes = list(STORE.cpu_nodes.values())
    if tenant_id:
        nodes = [c for c in nodes if c.tenant_id == tenant_id]
    return sorted(nodes, key=lambda c: c.id)


@router.get("/nodes/summary")
def nodes_summary() -> dict:
    by_state: dict[str, int] = {}
    for n in STORE.node_instances.values():
        by_state[n.state.value] = by_state.get(n.state.value, 0) + 1
    return {"total": len(STORE.node_instances), "by_state": by_state}


@router.get("/nodes/{node_id}", response_model=NodeInstance)
def get_node(node_id: str) -> NodeInstance:
    node = STORE.node_instances.get(node_id)
    if not node:
        raise HTTPException(404, f"node '{node_id}' not found")
    return node


@router.post("/reconcile/run", response_model=ReconcileReport)
def run_reconcile() -> ReconcileReport:
    return reconcile(get_adapter())
