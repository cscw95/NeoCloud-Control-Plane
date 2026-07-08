"""Inventory & Topology API.

Read access to the full AIFactory > Block > DU > SU > Rack > Tray > GPU graph,
capacity/power aggregations, blueprint-based SU provisioning, and per-rack
MaxQ/MaxP power-policy control.
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, HTTPException

from pydantic import BaseModel

from . import spec
from .models import (
    HwState,
    InventorySummary,
    PowerPolicyUpdate,
    Rack,
    ScalableUnit,
)
from .seed import provision_scalable_unit
from .store import STORE

router = APIRouter(prefix="/api/v1", tags=["inventory"])


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------
@router.get("/inventory/summary", response_model=InventorySummary)
def inventory_summary() -> InventorySummary:
    s = STORE
    with s.lock:
        design_kw = sum(r.tdp_kw for r in s.racks.values())
        capped_kw = sum(r.power_cap_kw for r in s.racks.values())
        gpu_states = Counter(g.state.value for g in s.gpus.values())
        gpu_arch = Counter(g.arch for g in s.gpus.values())
        rack_gen = Counter(f"{r.model}" for r in s.racks.values())
        return InventorySummary(
            factories=len(s.factories),
            scalable_units=len(s.sus),
            racks=len(s.racks),
            compute_trays=len(s.trays),
            gpus=len(s.gpus),
            cpus=len(s.cpus),
            dpus=len(s.dpus),
            hbm_total_tb=round(
                sum(g.hbm_gb for g in s.gpus.values()) / 1000, 2
            ),
            design_power_mw=round(design_kw / 1000, 2),
            capped_power_mw=round(capped_kw / 1000, 2),
            gpus_by_state=dict(gpu_states),
            gpus_by_arch=dict(gpu_arch),
            racks_by_generation=dict(rack_gen),
        )


@router.get("/topology/tree")
def topology_tree() -> dict:
    """Nested topology with leaf counts (GPUs summarized, not expanded)."""
    s = STORE
    with s.lock:
        def rack_node(r: Rack) -> dict:
            gpus = s.gpus_of_rack(r.id)
            return {
                "id": r.id,
                "index": r.index,
                "model": r.model,
                "generation": r.generation,
                "gpu_arch": r.gpu_arch,
                "cooling": r.cooling,
                "hac_id": r.hac_id,
                "state": r.state.value,
                "tenant_id": r.tenant_id,
                "tdp_kw": r.tdp_kw,
                "power_cap_kw": r.power_cap_kw,
                "compute_trays": len(r.tray_ids),
                "nvlink_switch_trays": len(r.nvlink_switch_tray_ids),
                "gpus": len(gpus),
                "gpus_allocated": sum(1 for g in gpus if g.tenant_id),
            }

        def su_node(su: ScalableUnit) -> dict:
            racks = s.racks_of_su(su.id)
            return {
                "id": su.id,
                "index": su.index,
                "hac_id": su.hac_id,
                "blueprint_key": su.blueprint_key,
                "model": s.racks[su.rack_ids[0]].model if su.rack_ids else None,
                "cmx_racks": su.cmx_racks,
                "state": su.state.value,
                "gpu_count": sum(len(s.gpus_of_rack(r.id)) for r in racks),
                "racks": [rack_node(r) for r in racks],
            }

        factories = []
        for f in s.factories.values():
            blocks = []
            for bid in f.block_ids:
                b = s.blocks[bid]
                dus = []
                for did in b.du_ids:
                    d = s.dus[did]
                    dus.append({
                        "id": d.id,
                        "index": d.index,
                        "scalable_units": [
                            su_node(s.sus[x]) for x in d.su_ids if x in s.sus
                        ],
                    })
                blocks.append({"id": b.id, "index": b.index,
                               "name": b.name, "power_mw": b.power_mw,
                               "ready": b.ready, "deployment_units": dus})
            factories.append({
                "id": f.id, "name": f.name, "site": f.site,
                "design_power_mw": f.design_power_mw, "compute_blocks": blocks,
            })
        return {"factories": factories}


# ---------------------------------------------------------------------------
# Entity listings
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 장비 헬스 — VRCM 물리 계층(HwState) 취합 + 운영 조치 (운영 포털 소비)
# ---------------------------------------------------------------------------
_OPERATOR_STATES = {HwState.ready, HwState.faulted, HwState.maintenance}


class EquipmentStateUpdate(BaseModel):
    kind: str                          # rack | tray | gpu
    id: str
    state: str                         # ready | faulted | maintenance
    note: str = ""


@router.get("/topology/su-composition")
def su_composition(su_id: str) -> dict:
    """SU(HAC) 단위 시스템 구성 — 컴퓨트·CMX·IB·Converged·CDU(DLC)·CPU 풀.

    NCP RA/RD 기준: 1 SU = 1 HAC(2열×8랙), CMX 2랙(32 CMS), IB 듀얼 패브릭
    리프 32(망당 16)·스파인 랙 4(56kW), Converged spine 4 + leaf 12,
    in-row CDU 2N. 사이트 공용 CPU 노드 풀은 참고 항목으로 병기."""
    s = STORE
    su = s.sus.get(su_id)
    if not su:
        raise HTTPException(404, f"scalable unit '{su_id}' not found")
    bp = spec.get_blueprint(su.blueprint_key)
    fac = s.factory_of_su(su_id)
    racks = len(su.rack_ids)
    trays = racks * bp.compute_trays
    ib, conv, cool = spec.IB_FABRIC, spec.CONVERGED_FABRIC, spec.COOLING
    return {
        "su_id": su.id, "hac_id": su.hac_id,
        "site": fac.name if fac else None,
        "blueprint": bp.key, "model": bp.model,
        "layout": "1 SU = 1 HAC — 2열 × 8랙 (테넌트 격리 자연 경계)",
        "compute": {"racks": racks, "trays": trays,
                    "gpus": racks * bp.gpu_per_rack, "nodes": trays,
                    "rack_kw_maxq": bp.maxq_rack_kw,
                    "rack_kw_maxp": bp.maxp_rack_kw},
        "cmx": {"racks": su.cmx_racks,
                "cms_chassis": su.cmx_racks * 16,
                "note": "Context Memory — KV 캐시 오프로드"},
        "ib": {"networks": len(ib["networks"]),
               "leaves": ib["leaves_per_network_per_su"] * len(ib["networks"]),
               "leaves_per_network": ib["leaves_per_network_per_su"],
               "spine_racks": ib["ib_spine_racks_per_su"],
               "spine_rack_kw": ib["ib_spine_rack_kw"],
               "node_links_800g": trays * bp.connectx_supernics_per_tray},
        "converged": {"spines": conv["spines_per_su_tier3"],
                      "leaves": conv["leaves_per_su_tier3"],
                      "note": "NVL72용 8 + CMX용 4 (3-tier 기준)"},
        "cooling": {"cdu": cool["cdu_per_su"], "type": cool["cdu_type"],
                    "capacity_kw_each": cool["cdu_capacity_kw"],
                    "loop": cool["loop"],
                    "hac_tdp_mw": bp.hac_tdp_mw, "cooling_class": bp.cooling},
        "cpu_pool": {"total": len(s.cpu_nodes),
                     "allocated": sum(1 for c in s.cpu_nodes.values()
                                      if c.tenant_id),
                     "per_tenant": 5,
                     "note": "사이트 공용 풀 — DPU 장착·테넌트 VPC 연결"},
    }


@router.get("/inventory/sites")
def inventory_sites() -> dict:
    """사이트별 자원·유효 상태 집계 — 멀티사이트(N개) 일반화.

    사이트 간에는 IB/NVLink가 이어지지 않아 클러스터가 사이트를 넘을 수
    없으므로, 판매 가능(유효) 용량·할당·비정상 상태는 사이트 단위로
    분리 관리한다. factories를 순회하므로 사이트가 늘어나도 그대로 동작."""
    from .lifecycle import _rack_sellable       # 함수 내 import — 순환 방지
    s = STORE
    with s.lock:
        dedicated = {t.id for t in s.tenants.values()
                     if t.isolation_tier.value == "bare_metal_dedicated"}
        sites = []
        for f in s.factories.values():
            su_ids = [su for b in f.block_ids
                      for d in s.blocks[b].du_ids
                      for su in s.dus[d].su_ids]
            racks = [s.racks[r] for su in su_ids
                     for r in s.sus[su].rack_ids]
            rack_ids = {r.id for r in racks}
            nodes = [n for n in s.node_instances.values()
                     if n.rack_id in rack_ids]
            sellable = [r for r in racks if _rack_sellable(r)]
            allocated = [r for r in racks if r.tenant_id]
            unhealthy = [r for r in racks
                         if r.state in (HwState.faulted, HwState.maintenance)]
            # 신규 계약 가능: dedicated 테넌트가 부분 점유한 SU의 잔여 랙은
            # 격리 정책(양방향)으로 잠기므로 제외 — 유휴 ≠ 계약 가능
            contractable = 0
            for su in su_ids:
                su_racks = s.racks_of_su(su)
                owners = {r.tenant_id for r in su_racks if r.tenant_id}
                if owners & dedicated:
                    continue
                contractable += sum(1 for r in su_racks if _rack_sellable(r))
            gpus_of = lambda rs: sum(len(s.gpus_of_rack(r.id)) for r in rs)
            sites.append({
                "factory_id": f.id, "name": f.name, "site": f.site,
                "racks_total": len(racks),
                "racks_sellable": len(sellable),      # 물리 유휴 (정상·미할당)
                "racks_contractable": contractable,   # 신규 계약 가능 (격리 반영)
                "racks_locked_by_isolation": len(sellable) - contractable,
                "racks_allocated": len(allocated),
                "racks_unhealthy": len(unhealthy),
                "gpus_total": gpus_of(racks),
                "gpus_sellable": gpus_of(sellable),
                "gpus_allocated": gpus_of(allocated),
                "power_cap_kw": sum(r.power_cap_kw for r in racks),
                "nodes_by_state": dict(
                    Counter(n.state.value for n in nodes)),
                "tenants": sorted({r.tenant_id for r in allocated}),
                # 사이트 내 최대 단일 클러스터(신규 계약) 규모 — 스팬 불가 +
                # dedicated SU 격리 반영
                "max_single_cluster_racks": contractable,
                "floors": [{"name": s.blocks[b].name,
                            "ready": s.blocks[b].ready,
                            "power_mw": s.blocks[b].power_mw}
                           for b in f.block_ids],
            })
        keys = ("racks_total", "racks_sellable", "racks_contractable",
                "racks_locked_by_isolation", "racks_allocated",
                "racks_unhealthy", "gpus_total", "gpus_sellable",
                "gpus_allocated", "power_cap_kw")
        return {"sites": sites,
                "totals": {k: sum(x[k] for x in sites) for k in keys}}


@router.get("/health/equipment")
def equipment_health() -> dict:
    """사이트/층별 장비(랙·트레이·GPU) HwState 집계 + 비정상 장비 리스트."""
    s = STORE
    with s.lock:
        def count_states(items) -> dict:
            return dict(Counter(x.state.value for x in items))

        sites = []
        for f in s.factories.values():
            floors = []
            for bid in f.block_ids:
                b = s.blocks[bid]
                racks = [s.racks[r] for d in b.du_ids
                         for su_id in s.dus[d].su_ids
                         for r in s.sus[su_id].rack_ids]
                trays = [t for r in racks for t in s.trays_of_rack(r.id)]
                gpus = [g for r in racks for g in s.gpus_of_rack(r.id)]
                floors.append({
                    "block_id": b.id, "name": b.name, "ready": b.ready,
                    "power_mw": b.power_mw,
                    "racks": len(racks), "gpus": len(gpus),
                    "rack_states": count_states(racks),
                    "gpu_states": count_states(gpus),
                    "unhealthy": sum(1 for r in racks if r.state in
                                     (HwState.faulted, HwState.maintenance))
                                 + sum(1 for t in trays if t.state in
                                       (HwState.faulted, HwState.maintenance)),
                })
            sites.append({"factory_id": f.id, "name": f.name,
                          "site": f.site, "floors": floors})

        bad = []
        for kind, coll in (("rack", s.racks), ("tray", s.trays),
                           ("gpu", s.gpus)):
            for item in coll.values():
                if item.state in (HwState.faulted, HwState.maintenance):
                    bad.append({"kind": kind, "id": item.id,
                                "state": item.state.value,
                                "tenant_id": getattr(item, "tenant_id", None)})

        breakfix = sum(1 for n in s.node_instances.values()
                       if n.state.value in ("quarantined", "rma", "cordoned"))
        return {
            "sites": sites,
            "totals": {
                "racks": count_states(s.racks.values()),
                "trays": count_states(s.trays.values()),
                "gpus": count_states(s.gpus.values()),
                "unhealthy_equipment": len(bad),
                "breakfix_nodes": breakfix,
            },
            "faulted_equipment": sorted(bad, key=lambda x: x["id"])[:100],
        }


@router.patch("/equipment/state")
def set_equipment_state(body: EquipmentStateUpdate) -> dict:
    """운영자 장비 조치 — 장애(faulted)/정비(maintenance)/복구(ready).

    faulted/maintenance 랙은 M4 배치(_rack_sellable)에서 자동 제외된다."""
    s = STORE
    try:
        new_state = HwState(body.state)
    except ValueError:
        raise HTTPException(422, f"invalid state '{body.state}'")
    if new_state not in _OPERATOR_STATES:
        raise HTTPException(422, "operator states: ready|faulted|maintenance")
    coll = {"rack": s.racks, "tray": s.trays, "gpu": s.gpus}.get(body.kind)
    if coll is None:
        raise HTTPException(422, "kind must be rack|tray|gpu")
    item = coll.get(body.id)
    if not item:
        raise HTTPException(404, f"{body.kind} '{body.id}' not found")
    with s.lock:
        # 테넌트 할당 중인 장비를 ready로 되돌리면 allocated로 복원
        if new_state == HwState.ready and getattr(item, "tenant_id", None):
            item.state = HwState.allocated
        else:
            item.state = new_state
    return {"kind": body.kind, "id": body.id, "state": item.state.value,
            "note": body.note}


@router.get("/factories")
def list_factories() -> list:
    return list(STORE.factories.values())


@router.get("/scalable-units")
def list_sus() -> list:
    return list(STORE.sus.values())


@router.get("/scalable-units/{su_id}")
def get_su(su_id: str):
    su = STORE.sus.get(su_id)
    if not su:
        raise HTTPException(404, f"scalable unit '{su_id}' not found")
    return su


@router.get("/racks")
def list_racks(su_id: str | None = None, tenant_id: str | None = None) -> list:
    racks = list(STORE.racks.values())
    if su_id:
        racks = [r for r in racks if r.su_id == su_id]
    if tenant_id:
        racks = [r for r in racks if r.tenant_id == tenant_id]
    return racks


@router.get("/racks/{rack_id}")
def get_rack(rack_id: str):
    rack = STORE.racks.get(rack_id)
    if not rack:
        raise HTTPException(404, f"rack '{rack_id}' not found")
    return rack


@router.get("/racks/{rack_id}/gpus")
def get_rack_gpus(rack_id: str) -> list:
    if rack_id not in STORE.racks:
        raise HTTPException(404, f"rack '{rack_id}' not found")
    return STORE.gpus_of_rack(rack_id)


@router.get("/trays/{tray_id}")
def get_tray(tray_id: str):
    tray = STORE.trays.get(tray_id)
    if not tray:
        raise HTTPException(404, f"tray '{tray_id}' not found")
    return tray


# ---------------------------------------------------------------------------
# Provisioning & power policy
# ---------------------------------------------------------------------------
@router.post("/scalable-units", status_code=201)
def provision_su(
    blueprint_key: str = spec.DEFAULT_BLUEPRINT,
    factory_id: str = "aif-skt-01",
    du_id: str = "du-1",
):
    s = STORE
    if factory_id not in s.factories:
        raise HTTPException(404, f"factory '{factory_id}' not found")
    if blueprint_key not in spec.BLUEPRINTS:
        raise HTTPException(
            422, f"unknown blueprint '{blueprint_key}'; "
                 f"choose from {sorted(spec.BLUEPRINTS)}")
    existing = {su.index for su in s.sus.values()}
    idx = s.next_seq("su_index_provisioned")
    while idx in existing:
        idx = s.next_seq("su_index_provisioned")
    su = provision_scalable_unit(
        s, factory_id=factory_id, du_id=du_id, su_index=idx,
        blueprint_key=blueprint_key,
    )
    bp = spec.get_blueprint(blueprint_key)
    return {"provisioned": su.id, "model": bp.model, "gpu_count": bp.gpu_per_su}


@router.post("/racks/{rack_id}/power-policy")
def set_power_policy(rack_id: str, body: PowerPolicyUpdate):
    rack = STORE.racks.get(rack_id)
    if not rack:
        raise HTTPException(404, f"rack '{rack_id}' not found")
    policy = body.policy.lower()
    if policy not in ("maxq", "maxp"):
        raise HTTPException(422, "policy must be 'maxq' or 'maxp'")
    bp = spec.get_blueprint(rack.blueprint_key)   # per-generation caps
    rack.power_cap_kw = bp.maxq_rack_kw if policy == "maxq" else bp.maxp_rack_kw
    return {"rack_id": rack_id, "model": rack.model, "policy": policy,
            "power_cap_kw": rack.power_cap_kw}


@router.get("/blueprints")
def list_blueprints() -> list:
    """Supported rack generations (GB200 / GB300 / Vera Rubin)."""
    out = []
    for bp in spec.BLUEPRINTS.values():
        out.append({
            "key": bp.key, "model": bp.model, "generation": bp.mgx_generation,
            "gpu_arch": bp.gpu_arch, "cpu_arch": bp.cpu_arch,
            "gpu_per_rack": bp.gpu_per_rack, "gpu_per_su": bp.gpu_per_su,
            "gpu_hbm_gb": bp.gpu_hbm_gb, "gpu_hbm_type": bp.gpu_hbm_type,
            "nvlink_gen": bp.nvlink_gen, "nvlink_per_gpu_tbps": bp.nvlink_per_gpu_tbps,
            "rack_tdp_kw": bp.rack_tdp_kw, "maxq_rack_kw": bp.maxq_rack_kw,
            "maxp_rack_kw": bp.maxp_rack_kw, "cooling": bp.cooling,
            "cpu_cores": bp.cpu_cores, "cpu_mem_tb": bp.cpu_mem_tb,
            "dpu_sku": bp.dpu_sku, "dpu_bw_gbps": bp.dpu_bw_gbps,
            "connectx_supernics_per_tray": bp.connectx_supernics_per_tray,
            "preliminary": bp.preliminary,
        })
    return out


@router.get("/spec")
def get_spec() -> dict:
    """Expose generation-agnostic constants + blueprint catalog."""
    return {
        "racks_per_su": {k: bp.racks_per_su
                         for k, bp in spec.BLUEPRINTS.items()},
        "default_blueprint": spec.DEFAULT_BLUEPRINT,
        "blueprints": sorted(spec.BLUEPRINTS),
        "ib_fabric": spec.IB_FABRIC,
        "converged_fabric": spec.CONVERGED_FABRIC,
        "tcs_max_supply_c": spec.TCS_MAX_SUPPLY_C,
        "isolation_layers": spec.ISOLATION_LAYERS,
    }
