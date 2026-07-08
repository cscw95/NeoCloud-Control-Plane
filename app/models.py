"""Pydantic data models for VRCM.

Two domains:
  1. Inventory & Topology  — the physical/logical hierarchy
       AIFactory > ComputeBlock > DeploymentUnit > ScalableUnit > Rack(NVL72)
                 > ComputeTray > {GPU, CPU, BlueFieldDPU} + NVLinkSwitchTray
       Racks are multi-generation: GB200 / GB300 / Vera Rubin (see spec.BLUEPRINTS).
  2. Multi-tenancy & Isolation — Tenant, Allocation, NVLinkPartition, network
       (VNI/VRF) bindings.

IDs are deterministic, human-readable slugs (e.g. "su-1/rack-03/tray-07/gpu-2").
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ===========================================================================
# Enums
# ===========================================================================
class HwState(str, Enum):
    provisioning = "provisioning"
    ready = "ready"
    allocated = "allocated"
    draining = "draining"
    faulted = "faulted"
    maintenance = "maintenance"


class IsolationTier(str, Enum):
    bare_metal_dedicated = "bare_metal_dedicated"
    vm_multitenant = "vm_multitenant"
    k8s_namespace = "k8s_namespace"


class DPUMode(str, Enum):
    nic = "nic"
    dpu = "dpu"
    dpu_zero_trust = "dpu_zero_trust"


class AllocationScope(str, Enum):
    scalable_unit = "scalable_unit"
    rack_set = "rack_set"
    hac = "hac"


# ===========================================================================
# Inventory leaf components
# ===========================================================================
class GPU(BaseModel):
    id: str
    tray_id: str
    index: int                        # 0..3 within tray
    arch: str                         # Blackwell / Blackwell Ultra / Rubin
    hbm_gb: int
    hbm_type: str                     # HBM3e / HBM4
    dies: int
    state: HwState = HwState.ready
    tenant_id: Optional[str] = None


class CPU(BaseModel):
    id: str
    tray_id: str
    index: int                        # 0..1 within tray
    arch: str                         # Grace / Vera
    cores: int
    mem_tb: float


class BlueFieldDPU(BaseModel):
    id: str
    tray_id: str
    sku: str                          # BF3 / BF4-B4240V
    bandwidth_gbps: int
    mode: DPUMode = DPUMode.dpu
    tenant_id: Optional[str] = None


class ComputeTray(BaseModel):
    id: str
    rack_id: str
    index: int                        # 0..17 within rack
    gpu_ids: list[str] = Field(default_factory=list)
    cpu_ids: list[str] = Field(default_factory=list)
    dpu_id: Optional[str] = None
    connectx_supernics: int = 0
    state: HwState = HwState.ready
    tenant_id: Optional[str] = None


class NVLinkSwitchTray(BaseModel):
    id: str
    rack_id: str
    index: int
    nvswitch_asics: int


class Rack(BaseModel):
    """One NVL72 rack == one NVLink domain (72 GPUs)."""
    id: str
    su_id: str
    index: int
    blueprint_key: str                # gb200-nvl72 / gb300-nvl72 / vr-nvl72
    model: str
    generation: str                   # MGX Gen 1.1 / 1.2
    gpu_arch: str
    hac_id: str
    tdp_kw: int
    power_cap_kw: int
    cooling: str
    tray_ids: list[str] = Field(default_factory=list)
    nvlink_switch_tray_ids: list[str] = Field(default_factory=list)
    state: HwState = HwState.ready
    tenant_id: Optional[str] = None


class ScalableUnit(BaseModel):
    id: str
    du_id: Optional[str] = None
    index: int
    hac_id: str
    blueprint_key: str                # SUs are homogeneous in this MVP
    rack_ids: list[str] = Field(default_factory=list)
    cmx_racks: int
    state: HwState = HwState.ready


class DeploymentUnit(BaseModel):
    id: str
    block_id: Optional[str] = None
    index: int
    su_ids: list[str] = Field(default_factory=list)


class ComputeBlock(BaseModel):
    """사이트 내 배치 단위 — Phase 1에서는 '층(floor)'으로 사용."""
    id: str
    factory_id: str
    index: int
    name: str = ""                    # 예: "1층 · 6MW"
    power_mw: float = 0               # 층 계약 전력
    ready: str = ""                   # 가동 시기 (예: 2027-03)
    du_ids: list[str] = Field(default_factory=list)


class AIFactory(BaseModel):
    id: str
    name: str
    site: str
    design_power_mw: float
    block_ids: list[str] = Field(default_factory=list)


# ===========================================================================
# Multi-tenancy & isolation
# ===========================================================================
class Tenant(BaseModel):
    id: str
    name: str
    isolation_tier: IsolationTier
    sla_tier: str = "standard"
    notes: Optional[str] = None


class NetworkIsolation(BaseModel):
    tenant_id: str
    compute_l3vni: int
    converged_vni: int
    oob_vni: int
    vrf: str
    ib_pkey: Optional[int] = None     # UFM 파티션 키 (첫 격리 구성 시 부여)


class Allocation(BaseModel):
    id: str
    tenant_id: str
    scope: AllocationScope
    su_id: str
    rack_ids: list[str] = Field(default_factory=list)
    dpu_mode: DPUMode
    state: HwState = HwState.allocated


class NVLinkPartition(BaseModel):
    id: str
    rack_id: str
    partition_id: int
    tray_ids: list[str] = Field(default_factory=list)
    tenant_id: str
    state: HwState = HwState.allocated


# ===========================================================================
# API request bodies
# ===========================================================================
class TenantCreate(BaseModel):
    name: str
    isolation_tier: IsolationTier
    sla_tier: str = "standard"
    notes: Optional[str] = None


class AllocationCreate(BaseModel):
    tenant_id: str
    scope: AllocationScope
    su_id: str
    rack_ids: Optional[list[str]] = None
    dpu_mode: Optional[DPUMode] = None


class PartitionCreate(BaseModel):
    rack_id: str
    tenant_id: str
    tray_ids: list[str]


class PowerPolicyUpdate(BaseModel):
    policy: str                       # "maxq" | "maxp"


# ===========================================================================
# Aggregations / reports
# ===========================================================================
class InventorySummary(BaseModel):
    factories: int
    scalable_units: int
    racks: int
    compute_trays: int
    gpus: int
    cpus: int
    dpus: int
    hbm_total_tb: float
    design_power_mw: float
    capped_power_mw: float
    gpus_by_state: dict[str, int]
    gpus_by_arch: dict[str, int]
    racks_by_generation: dict[str, int]


class IsolationFinding(BaseModel):
    severity: str
    layer: str
    message: str


class IsolationReport(BaseModel):
    tenant_id: str
    ok: bool
    findings: list[IsolationFinding]


# ===========================================================================
# Service lifecycle (M3/M1) — state machines for node instances & orders
#
# NodeInstance mirrors one NICo-managed host (1 host == 1 ComputeTray).
# Its state is a superset of the NICo host state: NICo knows nothing about
# NeoCloud-side states like `reserved`/`in_service`; the reconcile loop
# detects and escalates divergence (see lifecycle.reconcile).
# ===========================================================================
class NodeLifecycleState(str, Enum):
    discovered = "discovered"
    validating = "validating"
    quarantined = "quarantined"
    pool_ready = "pool_ready"
    reserved = "reserved"
    provisioning = "provisioning"
    allocated = "allocated"
    in_service = "in_service"
    cordoned = "cordoned"
    draining = "draining"
    releasing = "releasing"
    sanitizing = "sanitizing"
    rma = "rma"


class OrderKind(str, Enum):
    new = "new"
    expand = "expand"
    shrink = "shrink"
    terminate = "terminate"


class OrderState(str, Enum):
    received = "received"
    validated = "validated"
    reserved = "reserved"
    provisioning = "provisioning"
    isolating = "isolating"
    storage_binding = "storage_binding"
    acceptance = "acceptance"
    delivered = "delivered"
    reclaiming = "reclaiming"
    closed = "closed"
    rejected = "rejected"
    compensating = "compensating"
    failed = "failed"


class LifecycleEvent(BaseModel):
    state: str
    detail: str = ""
    at: str                           # ISO-8601 UTC


class NodeInstance(BaseModel):
    id: str                           # "ni-{tray_id}"
    tray_id: str
    rack_id: str
    su_id: str
    blueprint_key: str
    nico_host_id: str                 # external SoT reference (NICo)
    nico_instance_id: Optional[str] = None
    tenant_id: Optional[str] = None
    order_id: Optional[str] = None
    state: NodeLifecycleState = NodeLifecycleState.discovered
    history: list[LifecycleEvent] = Field(default_factory=list)


class ServiceOrder(BaseModel):
    id: str
    tenant_id: str
    kind: OrderKind
    blueprint_key: Optional[str] = None
    racks: int = 0
    allocation_id: Optional[str] = None       # terminate target
    allocation_ids: list[str] = Field(default_factory=list)
    approval_mode: bool = False               # 운영자 단계별 승인 게이트
    pending_stage: Optional[str] = None       # 승인 대기 중인 다음 단계
    storage_mode: str = "auto"                # auto | manual
    storage_tb: float = 0
    storage_gbps: float = 0
    segment_id: Optional[str] = None          # tenant VPC (NICo segment)
    storage_ids: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    state: OrderState = OrderState.received
    error: Optional[str] = None
    # 딜리버리 시 고객에게 전달하는 접속·보안 인증 패키지 (client_secret 1회 노출)
    access_package: Optional[dict] = None
    history: list[LifecycleEvent] = Field(default_factory=list)


class CpuNode(BaseModel):
    """범용 CPU 노드 — DPU 장착, 테넌트 VPC에 연결 (기본 제공 5대/테넌트).

    GPU 클러스터의 보조 컴퓨트(로그인/스케줄러/데이터 준비용). IB/NVLink 없음,
    Ethernet(VPC)만 DPU HBN으로 격리 연결된다."""
    id: str
    dpu_id: str
    dpu_sku: str = "BF3"
    cpu_arch: str = "AMD EPYC 9654"
    cores: int = 96
    mem_tb: float = 1.5
    state: str = "pool_ready"         # pool_ready | allocated
    tenant_id: Optional[str] = None
    host_ip: str = ""
    segment_id: Optional[str] = None


class StorageAllocation(BaseModel):
    id: str
    tenant_id: str
    order_id: str
    allocation_id: Optional[str] = None
    view_path: str                    # VAST view export 경로
    capacity_tb: float
    qos_gbps: float
    protocol: str = "NFSoRDMA"
    state: str = "active"             # active | reclaimed


class OrderCreate(BaseModel):
    tenant_id: str
    kind: OrderKind
    blueprint_key: Optional[str] = None
    racks: int = 0
    allocation_id: Optional[str] = None
    approval_mode: bool = False               # True면 운영 승인 큐로
    storage_mode: str = "auto"                # auto(랙 비례) | manual(직접 지정)
    storage_tb: float = 0                     # manual일 때 용량
    storage_gbps: float = 0                   # manual일 때 QoS (0=자동 산정)


class TicketComment(BaseModel):
    at: str
    author: str                       # customer | operator
    text: str


class Ticket(BaseModel):
    """트러블 티켓 — Customer/Business/Operator 포털 공용."""
    id: str
    tenant_id: str
    subject: str
    body: str = ""
    severity: str = "medium"          # low | medium | high | critical
    status: str = "open"              # open | in_progress | resolved
    ref: Optional[str] = None         # 관련 리소스 (node/cluster/order)
    created_at: str = ""
    updated_at: str = ""
    comments: list[TicketComment] = Field(default_factory=list)


class TicketCreate(BaseModel):
    tenant_id: str
    subject: str
    body: str = ""
    severity: str = "medium"
    ref: Optional[str] = None


class TicketUpdate(BaseModel):
    status: Optional[str] = None
    comment: Optional[str] = None
    author: str = "operator"


class ReconcileFinding(BaseModel):
    kind: str                         # GHOST | ORPHAN | STATE_MISMATCH
    severity: str                     # info | major | critical
    node_id: Optional[str] = None
    nico_host_id: Optional[str] = None
    message: str


class ReconcileReport(BaseModel):
    checked_nodes: int
    checked_hosts: int
    ghosts_registered: int
    orphans_cordoned: int
    mismatches: int
    ok: bool
    findings: list[ReconcileFinding] = Field(default_factory=list)
