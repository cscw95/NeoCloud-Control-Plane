"""Pydantic data models for NOCP.

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
    """Placement unit within a site — used as a 'floor' in Phase 1."""
    id: str
    factory_id: str
    index: int
    name: str = ""                    # e.g. "Floor 1 · 6MW"
    power_mw: float = 0               # floor contracted power
    ready: str = ""                   # go-live timing (e.g. 2027-03)
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
    ib_pkey: Optional[int] = None     # UFM partition key (assigned at first isolation setup)


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
    policy: str                       # "maxq" | "maxp" | "dps-dynamic" (MaxLPS DPS)
    profile: Optional[str] = None     # WPPS: max-p | max-q | training | inference | balanced


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
    k8s_installing = "k8s_installing"   # Managed K8s option — K8s installed on top of BMaaS
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
    approval_mode: bool = False               # operator per-stage approval gate
    pending_stage: Optional[str] = None       # next stage awaiting approval
    storage_mode: str = "auto"                # auto | manual
    storage_tb: float = 0
    storage_gbps: float = 0
    managed_k8s: bool = False                 # Managed K8s option (BMaaS+K8s)
    k8s_version: str = ""                     # install version when option selected
    k8s_cluster_id: Optional[str] = None      # reference to the created K8s cluster
    contract_id: Optional[str] = None         # F06 reserved contract this order fulfils
    segment_id: Optional[str] = None          # tenant VPC (NICo segment)
    storage_ids: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    state: OrderState = OrderState.received
    error: Optional[str] = None
    # access & security credential package handed to the customer at delivery (client_secret shown once)
    access_package: Optional[dict] = None
    history: list[LifecycleEvent] = Field(default_factory=list)


class CpuNode(BaseModel):
    """General-purpose CPU node — DPU-equipped, attached to the tenant VPC (5 provided per tenant by default).

    Auxiliary compute for the GPU cluster (login/scheduler/data-prep). No IB/NVLink;
    only Ethernet (VPC) is isolation-connected via the DPU HBN."""
    id: str
    dpu_id: str
    dpu_sku: str = "BF3"
    cpu_arch: str = "AMD EPYC 9654"
    cores: int = 96
    mem_tb: float = 1.5
    state: str = "pool_ready"         # pool_ready | allocated
    role: str = "general"             # general | k8s_cp (Managed K8s control plane)
    nico_host_id: Optional[str] = None  # AI Infra Emulator (NICo) host reference
    tenant_id: Optional[str] = None
    order_id: Optional[str] = None    # k8s_cp — attributed to the provisioning order (flow bucketing)
    host_ip: str = ""
    segment_id: Optional[str] = None


class K8sCluster(BaseModel):
    """Managed K8s cluster — tenant K8s installed and managed as an option on top of
    a BMaaS provisioning. The 3 CP nodes are allocated from the CPU node pool via
    NICo (DPU isolation) and bound to the GPU workers over the Converged Network (VNI).
    DCGM telemetry is collected via the in-band (DCGM exporter DaemonSet) path."""
    id: str
    tenant_id: str
    order_id: str
    allocation_id: Optional[str] = None
    name: str = ""
    version: str = ""                 # K8s version (e.g. v1.32.4)
    nkd_version: str = ""             # NVIDIA Kubernetes Deployment version
    state: str = "installing"         # installing | running | deleting | deleted | failed
    api_vip: str = ""                 # kube-vip — API VIP on the Converged Network
    oidc_issuer: str = ""             # OIDC issuer for kubeconfig issuance
    cp_node_ids: list[str] = Field(default_factory=list)      # CpuNode.id ×3
    worker_node_ids: list[str] = Field(default_factory=list)  # NodeInstance.id
    gpus_total: int = 0
    addons: list[dict] = Field(default_factory=list)          # {name,version,status}
    dcgm_mode: str = "in-band"        # in-band(DCGM exporter) | oob(Redfish)
    conditions: list[dict] = Field(default_factory=list)      # install verification results
    # derived state for observing the install saga (R4) — consumed by the console stepper via polling
    stage: str = ""                   # current install stage (8 stages)
    stage_history: list[dict] = Field(default_factory=list)   # {name,status,ts}
    progress_pct: int = 0             # install progress (completed stages / 8)
    acceptance: Optional[dict] = None  # burn-in report {status,report_ts,checks}
    quarantined_nodes: list[str] = Field(default_factory=list)  # quarantined worker tray
    node_versions: dict = Field(default_factory=dict)          # per-node K8s version
    created_at: str = ""
    history: list[LifecycleEvent] = Field(default_factory=list)


class K8sInstall(BaseModel):
    """Managed K8s install record — shared by Day-1 (order option) and Day-2 (installs).

    Accumulates the install saga's stage progress, consumed by the console 'requests · tasks' board."""
    install_id: str
    cluster_id: Optional[str] = None  # confirmed at stage 1 (cp-reserve)
    tenant_id: str
    allocation_id: Optional[str] = None
    order_id: Optional[str] = None
    k8s_version: str = ""
    state: str = "running"            # running | succeeded | failed
    stage: str = ""                   # current stage name
    stages: list[dict] = Field(default_factory=list)  # {name,status,ts}
    created_at: str = ""


class K8sKubeconfig(BaseModel):
    """kubeconfig issuance history — manages mock PKI (serial), TTL, and revocation."""
    kubeconfig_id: str
    cluster_id: str
    tenant_id: str
    serial: str = ""                  # mock PKI certificate serial (hex)
    role: str = "edit"                # RBAC template (admin | edit | view …)
    ttl_h: int = 12
    issued_at: str = ""
    expires_at: str = ""
    revoked: bool = False
    revoked_at: Optional[str] = None
    kubeconfig_yaml: str = ""         # mock kubeconfig body


class K8sUpgrade(BaseModel):
    """Cluster upgrade saga — per node cordon→drain→upgrade→uncordon."""
    upgrade_id: str
    cluster_id: str
    tenant_id: str = ""
    from_version: str = ""
    target_version: str = ""
    state: str = "running"            # running | succeeded | failed
    current_node: Optional[str] = None
    node_progress: dict = Field(default_factory=dict)   # {done,total}
    events: list[dict] = Field(default_factory=list)    # {ts,node,step}
    created_at: str = ""


class StorageAllocation(BaseModel):
    id: str
    tenant_id: str
    order_id: str
    allocation_id: Optional[str] = None
    view_path: str                    # VAST view export path
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
    approval_mode: bool = False               # if True, route to the ops approval queue
    storage_mode: str = "auto"                # auto (rack-proportional) | manual (explicit)
    storage_tb: float = 0                     # capacity when manual
    storage_gbps: float = 0                   # QoS when manual (0=auto-computed)
    managed_k8s: bool = False                 # Managed K8s option — K8s installed after BMaaS
    k8s_version: str = "v1.32.4"              # K8s version when option selected
    contract_id: Optional[str] = None         # F06 reserved contract (racks/placement from it)


class TicketComment(BaseModel):
    at: str
    author: str                       # customer | operator
    text: str


class Ticket(BaseModel):
    """Trouble ticket — shared by the Customer/Business/Operator portals."""
    id: str
    tenant_id: str
    subject: str
    body: str = ""
    severity: str = "medium"          # low | medium | high | critical
    status: str = "open"              # open | in_progress | resolved
    type: str = "tech"                # tech | change | billing_dispute
    routed_to: str = "ops"            # ops(tech/change) | biz(billing_dispute·contract)
    change_scope: Optional[str] = None  # change ticket: in_contract | contract_amendment
    policy: Optional[str] = None      # billing_dispute: guidance on billing-dispute handling policy
    ref: Optional[str] = None         # related resource (node/cluster/order)
    created_at: str = ""
    updated_at: str = ""
    comments: list[TicketComment] = Field(default_factory=list)


class TicketCreate(BaseModel):
    tenant_id: str
    subject: str
    body: str = ""
    severity: str = "medium"
    type: str = "tech"                # tech | change | billing_dispute
    change_scope: Optional[str] = None  # change ticket scope (within contract / contract change)
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
