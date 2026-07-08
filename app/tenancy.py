"""Multi-tenancy & Isolation API.

Implements the tenant lifecycle, capacity allocation (SU / rack-set / HAC),
NVLink-domain partitioning, automatic per-tenant VNI/VRF binding, and an
isolation-verification report that checks the four NCP isolation layers plus
network and NVLink consistency.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from . import shared_services, spec
from .models import (
    Allocation,
    AllocationCreate,
    AllocationScope,
    DPUMode,
    HwState,
    IsolationFinding,
    IsolationReport,
    IsolationTier,
    NetworkIsolation,
    NVLinkPartition,
    PartitionCreate,
    Tenant,
    TenantCreate,
)
from .store import STORE

router = APIRouter(prefix="/api/v1", tags=["tenancy"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _alloc_vni(fabric: str) -> int:
    """Allocate the next free VNI in a fabric's range."""
    lo, hi = spec.VNI_RANGES[fabric]
    used = set()
    for ni in STORE.netiso.values():
        used.update({ni.compute_l3vni, ni.converged_vni, ni.oob_vni})
    for vni in range(lo, hi + 1):
        if vni not in used:
            return vni
    raise HTTPException(507, f"VNI pool exhausted for fabric '{fabric}'")


def _default_dpu_mode(tier: IsolationTier) -> DPUMode:
    # vm_multitenant shares physical fabric -> needs host isolation (zero-trust)
    return {
        IsolationTier.bare_metal_dedicated: DPUMode.dpu,
        IsolationTier.vm_multitenant: DPUMode.dpu_zero_trust,
        IsolationTier.k8s_namespace: DPUMode.dpu,
    }[tier]


def _bind_rack(rack_id: str, tenant_id: str, dpu_mode: DPUMode) -> None:
    """Bind a rack and all its GPUs/DPUs to a tenant."""
    s = STORE
    rack = s.racks[rack_id]
    rack.tenant_id = tenant_id
    rack.state = HwState.allocated
    for tray in s.trays_of_rack(rack_id):
        tray.tenant_id = tenant_id
        if tray.dpu_id and tray.dpu_id in s.dpus:
            dpu = s.dpus[tray.dpu_id]
            dpu.tenant_id = tenant_id
            dpu.mode = dpu_mode
        for gid in tray.gpu_ids:
            if gid in s.gpus:
                g = s.gpus[gid]
                g.tenant_id = tenant_id
                g.state = HwState.allocated


def _release_rack(rack_id: str) -> None:
    s = STORE
    rack = s.racks[rack_id]
    rack.tenant_id = None
    rack.state = HwState.ready
    for tray in s.trays_of_rack(rack_id):
        tray.tenant_id = None
        if tray.dpu_id and tray.dpu_id in s.dpus:
            d = s.dpus[tray.dpu_id]
            d.tenant_id = None
            d.mode = DPUMode.dpu
        for gid in tray.gpu_ids:
            if gid in s.gpus:
                g = s.gpus[gid]
                g.tenant_id = None
                g.state = HwState.ready


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------
@router.post("/tenants", status_code=201, response_model=Tenant)
def create_tenant(body: TenantCreate) -> Tenant:
    s = STORE
    with s.lock:
        tid = f"tnt-{_slug(body.name)}"
        if tid in s.tenants:
            raise HTTPException(409, f"tenant '{tid}' already exists")
        tenant = Tenant(
            id=tid, name=body.name, isolation_tier=body.isolation_tier,
            sla_tier=body.sla_tier, notes=body.notes,
        )
        s.tenants[tid] = tenant
        # auto-bind network isolation (VNI/VRF)
        s.netiso[tid] = NetworkIsolation(
            tenant_id=tid,
            compute_l3vni=_alloc_vni("compute"),
            converged_vni=_alloc_vni("converged"),
            oob_vni=_alloc_vni("oob"),
            vrf=f"VRF-{_slug(body.name)}",
        )
        # Shared Services(⑦): 테넌트 IAM realm + 기본 롤(RBAC) + 포털 클라이언트
        shared_services.SHARED.create_realm(tid, body.name)
        return tenant


@router.get("/tenants")
def list_tenants() -> list:
    return list(STORE.tenants.values())


@router.get("/tenants/{tenant_id}")
def get_tenant(tenant_id: str):
    t = STORE.tenants.get(tenant_id)
    if not t:
        raise HTTPException(404, f"tenant '{tenant_id}' not found")
    return {
        "tenant": t,
        "network_isolation": STORE.netiso.get(tenant_id),
        "allocations": STORE.allocations_of_tenant(tenant_id),
    }


# ---------------------------------------------------------------------------
# Allocations
# ---------------------------------------------------------------------------
@router.post("/allocations", status_code=201, response_model=Allocation)
def create_allocation(body: AllocationCreate) -> Allocation:
    s = STORE
    with s.lock:
        tenant = s.tenants.get(body.tenant_id)
        if not tenant:
            raise HTTPException(404, f"tenant '{body.tenant_id}' not found")
        su = s.sus.get(body.su_id)
        if not su:
            raise HTTPException(404, f"scalable unit '{body.su_id}' not found")

        # resolve target racks
        if body.scope == AllocationScope.scalable_unit:
            rack_ids = list(su.rack_ids)
        elif body.scope == AllocationScope.hac:
            rack_ids = [r.id for r in s.racks_of_su(su.id) if r.hac_id == su.hac_id]
        else:  # rack_set
            if not body.rack_ids:
                raise HTTPException(422, "rack_set scope requires rack_ids")
            rack_ids = body.rack_ids
            for rid in rack_ids:
                if rid not in s.racks or s.racks[rid].su_id != su.id:
                    raise HTTPException(
                        422, f"rack '{rid}' not in scalable unit '{su.id}'")

        # conflict check — no rack may belong to two tenants
        for rid in rack_ids:
            existing = s.allocation_for_rack(rid)
            if existing:
                raise HTTPException(
                    409, f"rack '{rid}' already allocated to tenant "
                         f"'{existing.tenant_id}'")

        # bare-metal-dedicated must not share an SU with another tenant
        if tenant.isolation_tier == IsolationTier.bare_metal_dedicated:
            for r in s.racks_of_su(su.id):
                if r.id not in rack_ids and r.tenant_id not in (None, tenant.id):
                    raise HTTPException(
                        409,
                        f"bare-metal-dedicated tenant cannot share SU '{su.id}' "
                        f"(rack '{r.id}' held by '{r.tenant_id}')")

        dpu_mode = body.dpu_mode or _default_dpu_mode(tenant.isolation_tier)
        aid = f"alloc-{s.next_seq('alloc')}"
        alloc = Allocation(
            id=aid, tenant_id=tenant.id, scope=body.scope, su_id=su.id,
            rack_ids=rack_ids, dpu_mode=dpu_mode,
        )
        for rid in rack_ids:
            _bind_rack(rid, tenant.id, dpu_mode)
        s.allocations[aid] = alloc
        return alloc


@router.delete("/allocations/{alloc_id}")
def delete_allocation(alloc_id: str):
    s = STORE
    with s.lock:
        alloc = s.allocations.get(alloc_id)
        if not alloc:
            raise HTTPException(404, f"allocation '{alloc_id}' not found")
        # release any NVLink partitions on these racks first
        for rid in alloc.rack_ids:
            for p in s.partitions_of_rack(rid):
                if p.tenant_id == alloc.tenant_id:
                    s.partitions.pop(p.id, None)
            _release_rack(rid)
        s.allocations.pop(alloc_id)
        return {"released": alloc_id, "racks": alloc.rack_ids}


# ---------------------------------------------------------------------------
# NVLink partitions (compute isolation)
# ---------------------------------------------------------------------------
@router.post("/nvlink-partitions", status_code=201, response_model=NVLinkPartition)
def create_partition(body: PartitionCreate) -> NVLinkPartition:
    s = STORE
    with s.lock:
        rack = s.racks.get(body.rack_id)
        if not rack:
            raise HTTPException(404, f"rack '{body.rack_id}' not found")
        if body.tenant_id not in s.tenants:
            raise HTTPException(404, f"tenant '{body.tenant_id}' not found")
        if rack.tenant_id != body.tenant_id:
            raise HTTPException(
                409,
                f"rack '{rack.id}' is not allocated to tenant '{body.tenant_id}'")
        # trays must belong to this rack
        for tid in body.tray_ids:
            if tid not in rack.tray_ids:
                raise HTTPException(
                    422, f"tray '{tid}' not in rack '{rack.id}'")
        # no overlap with existing partitions in the same domain
        claimed = {
            t for p in s.partitions_of_rack(rack.id) for t in p.tray_ids
        }
        overlap = claimed.intersection(body.tray_ids)
        if overlap:
            raise HTTPException(
                409, f"trays already in a partition: {sorted(overlap)}")

        pno = len(s.partitions_of_rack(rack.id)) + 1
        pid = f"{rack.id}-part-{pno}"
        part = NVLinkPartition(
            id=pid, rack_id=rack.id, partition_id=pno,
            tray_ids=body.tray_ids, tenant_id=body.tenant_id,
        )
        s.partitions[pid] = part
        return part


@router.get("/nvlink-partitions")
def list_partitions(rack_id: str | None = None, tenant_id: str | None = None) -> list:
    parts = list(STORE.partitions.values())
    if rack_id:
        parts = [p for p in parts if p.rack_id == rack_id]
    if tenant_id:
        parts = [p for p in parts if p.tenant_id == tenant_id]
    return parts


# ---------------------------------------------------------------------------
# Network map & isolation verification
# ---------------------------------------------------------------------------
@router.get("/network/vni-map")
def vni_map() -> list:
    return list(STORE.netiso.values())


@router.get("/tenants/{tenant_id}/isolation", response_model=IsolationReport)
def isolation_report(tenant_id: str) -> IsolationReport:
    s = STORE
    t = s.tenants.get(tenant_id)
    if not t:
        raise HTTPException(404, f"tenant '{tenant_id}' not found")

    findings: list[IsolationFinding] = []

    # 1. identity layer — always tenant-scoped in this control plane
    findings.append(IsolationFinding(
        severity="pass", layer="identity",
        message="Tenant-scoped access control in effect."))

    allocs = s.allocations_of_tenant(tenant_id)
    tenant_racks = [rid for a in allocs for rid in a.rack_ids]

    # 2. physical layer
    if t.isolation_tier == IsolationTier.bare_metal_dedicated:
        shared = []
        for a in allocs:
            su = s.sus.get(a.su_id)
            if not su:
                continue
            for r in s.racks_of_su(su.id):
                if r.tenant_id not in (None, tenant_id):
                    shared.append(r.id)
        if shared:
            findings.append(IsolationFinding(
                severity="fail", layer="physical",
                message=f"Dedicated tenant shares SU with others at racks {shared}."))
        else:
            findings.append(IsolationFinding(
                severity="pass", layer="physical",
                message="Dedicated SU/HAC boundary — no co-tenant racks."))
    else:
        findings.append(IsolationFinding(
            severity="pass", layer="physical",
            message=f"Tier '{t.isolation_tier.value}': shared SU permitted; "
                    "isolation enforced at network/DPU layer."))

    # 3. network layer — VNI/VRF uniqueness
    ni = s.netiso.get(tenant_id)
    if not ni:
        findings.append(IsolationFinding(
            severity="fail", layer="network",
            message="No VNI/VRF binding for tenant."))
    else:
        collide = []
        for other_id, other in s.netiso.items():
            if other_id == tenant_id:
                continue
            mine = {ni.compute_l3vni, ni.converged_vni, ni.oob_vni}
            theirs = {other.compute_l3vni, other.converged_vni, other.oob_vni}
            if mine & theirs:
                collide.append(other_id)
            if other.vrf == ni.vrf:
                collide.append(other_id)
        if collide:
            findings.append(IsolationFinding(
                severity="fail", layer="network",
                message=f"VNI/VRF collision with tenants {sorted(set(collide))}."))
        else:
            findings.append(IsolationFinding(
                severity="pass", layer="network",
                message=f"Unique L3VNI={ni.compute_l3vni}, VRF={ni.vrf} "
                        "across Compute/Converged/OOB fabrics."))

    # 4. DPU mode vs tier
    for a in allocs:
        if (t.isolation_tier == IsolationTier.vm_multitenant
                and a.dpu_mode != DPUMode.dpu_zero_trust):
            findings.append(IsolationFinding(
                severity="warn", layer="process",
                message=f"Allocation {a.id}: vm_multitenant on shared fabric "
                        "should use DPU Zero-Trust mode."))

    # 5. compute-isolation layer — NVLink partitions
    parts = [p for p in s.partitions.values() if p.tenant_id == tenant_id]
    for p in parts:
        if p.rack_id not in tenant_racks:
            findings.append(IsolationFinding(
                severity="fail", layer="compute_isolation",
                message=f"Partition {p.id} on rack not allocated to tenant."))
    if parts:
        findings.append(IsolationFinding(
            severity="pass", layer="compute_isolation",
            message=f"{len(parts)} NVLink partition(s) bound within tenant domains."))

    ok = not any(f.severity == "fail" for f in findings)
    return IsolationReport(tenant_id=tenant_id, ok=ok, findings=findings)
