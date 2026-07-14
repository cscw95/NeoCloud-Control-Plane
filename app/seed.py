"""Blueprint-based provisioning + default seed for NOCP.

`provision_scalable_unit` expands a generation blueprint (GB200 / GB300 / Vera
Rubin) into a fully-populated SU. The same path backs both the default seed and
the POST /scalable-units API, so all SUs are structurally identical regardless
of how they were created. The default seed builds a *mixed-generation* factory
to exercise multi-generation inventory and allocation.
"""

from __future__ import annotations

from . import spec
from .models import (
    AIFactory,
    BlueFieldDPU,
    ComputeBlock,
    ComputeTray,
    CPU,
    DeploymentUnit,
    GPU,
    HwState,
    NVLinkSwitchTray,
    Rack,
    ScalableUnit,
)
from .store import Store


def _pad(n: int, width: int = 2) -> str:
    return str(n).zfill(width)


def provision_scalable_unit(
    store: Store,
    *,
    factory_id: str,
    du_id: str | None,
    su_index: int,
    blueprint_key: str = spec.DEFAULT_BLUEPRINT,
    power_policy: str = "maxq",
    racks: int | None = None,
) -> ScalableUnit:
    """Expand one SU from the given blueprint and register every entity.

    `racks`로 SU 랙 수를 지정할 수 있다(기본 = 세대별 표준, VR은 NCP RD 기준
    16랙) — 표준의 배수가 아닌 층은 표준 SU + 잔여 SU로 분할해 시드한다."""
    bp = spec.get_blueprint(blueprint_key)
    cap_kw = bp.maxq_rack_kw if power_policy == "maxq" else bp.maxp_rack_kw
    rack_count = racks or bp.racks_per_su
    su_id = f"su-{su_index}"
    hac_id = f"{su_id}-hac"

    su = ScalableUnit(
        id=su_id, du_id=du_id, index=su_index, hac_id=hac_id,
        blueprint_key=bp.key, cmx_racks=spec.CMX_RACKS_PER_SU,
        state=HwState.ready,
    )

    for r in range(rack_count):
        rack_id = f"{su_id}-rack-{_pad(r)}"
        rack = Rack(
            id=rack_id, su_id=su_id, index=r,
            blueprint_key=bp.key, model=bp.model, generation=bp.mgx_generation,
            gpu_arch=bp.gpu_arch, hac_id=hac_id, tdp_kw=bp.rack_tdp_kw,
            power_cap_kw=cap_kw, cooling=bp.cooling, state=HwState.ready,
        )

        for t in range(bp.compute_trays):
            tray_id = f"{rack_id}-tray-{_pad(t)}"
            tray = ComputeTray(
                id=tray_id, rack_id=rack_id, index=t,
                connectx_supernics=bp.connectx_supernics_per_tray,
                state=HwState.ready,
            )
            for g in range(bp.gpu_per_tray):
                gid = f"{tray_id}-gpu-{g}"
                store.gpus[gid] = GPU(
                    id=gid, tray_id=tray_id, index=g, arch=bp.gpu_arch,
                    hbm_gb=bp.gpu_hbm_gb, hbm_type=bp.gpu_hbm_type,
                    dies=bp.gpu_dies, state=HwState.ready,
                )
                tray.gpu_ids.append(gid)
            for c in range(bp.cpu_per_tray):
                cid = f"{tray_id}-cpu-{c}"
                store.cpus[cid] = CPU(
                    id=cid, tray_id=tray_id, index=c, arch=bp.cpu_arch,
                    cores=bp.cpu_cores, mem_tb=bp.cpu_mem_tb,
                )
                tray.cpu_ids.append(cid)
            if bp.dpu_per_tray:
                did = f"{tray_id}-dpu"
                store.dpus[did] = BlueFieldDPU(
                    id=did, tray_id=tray_id, sku=bp.dpu_sku,
                    bandwidth_gbps=bp.dpu_bw_gbps,
                )
                tray.dpu_id = did

            store.trays[tray_id] = tray
            rack.tray_ids.append(tray_id)

        for n in range(bp.nvlink_switch_trays):
            nid = f"{rack_id}-nvsw-{n}"
            store.nvlink_trays[nid] = NVLinkSwitchTray(
                id=nid, rack_id=rack_id, index=n,
                nvswitch_asics=bp.nvswitch_asics_per_tray,
            )
            rack.nvlink_switch_tray_ids.append(nid)

        store.racks[rack_id] = rack
        su.rack_ids.append(rack_id)

    store.sus[su_id] = su
    if du_id and du_id in store.dus:
        du = store.dus[du_id]
        if su_id not in du.su_ids:
            du.su_ids.append(su_id)
    return su


# Phase 1 실배치 계획 — 2개 사이트 × 각 2개 층, 전량 Vera Rubin NVL72
#   STT 가산: 2,592 GPU (36랙) / IGIS 안산: 7,488 GPU (104랙) = 10,080 GPU
PHASE1_SITES = [
    {"id": "aif-stt-gasan", "name": "STT 가산", "site": "Seoul · STT Gasan",
     "design_power_mw": 9.0,
     "floors": [
         {"name": "1층 · 6MW",  "power_mw": 6.0,  "ready": "2027-03", "racks": 24},
         {"name": "2층 · 3MW",  "power_mw": 3.0,  "ready": "2027-09", "racks": 12},
     ]},
    {"id": "aif-igis-ansan", "name": "IGIS 안산", "site": "Ansan · IGIS",
     "design_power_mw": 23.3,
     "floors": [
         {"name": "1층 · 12.9MW", "power_mw": 12.9, "ready": "2027-11", "racks": 54},
         {"name": "2층 · 10.4MW", "power_mw": 10.4, "ready": "2028-01", "racks": 50},
     ]},
]


def _su_sizes(racks: int, per_su: int) -> list[int]:
    """층 랙 수 → SU 분할 (표준 per_su + 잔여).

    VR = 16랙/SU (NCP RD): 24→[16,8], 54→[16,16,16,6], 50→[16,16,16,2]."""
    sizes: list[int] = []
    while racks > per_su:
        sizes.append(per_su)
        racks -= per_su
    sizes.append(racks)
    return sizes


def seed_default(store: Store, *, blueprints: list[str] | None = None) -> None:
    """Seed Phase 1 사이트 구성 (기본) 또는 단순 시드(blueprints 지정 시).

    기본: PHASE1_SITES — STT 가산(1,728+864 GPU) + IGIS 안산(3,888+3,600 GPU)
    = 140랙 / 2,520트레이 / 10,080 GPU, 전량 vr-nvl72 (IT MaxQ 28MW).
    테스트는 conftest에서 blueprints 명시 시드(레거시 경로)를 사용한다."""
    store.reset()

    if blueprints:                    # 레거시/테스트 경로 — 단일 팩토리
        factory = AIFactory(
            id="aif-skt-01", name="SKT NVL72 AI Factory 01",
            site="Seoul Metro (Gasan)", design_power_mw=250.0,
        )
        block = ComputeBlock(id="block-1", factory_id=factory.id, index=1)
        du = DeploymentUnit(id="du-1", block_id=block.id, index=1)
        store.factories[factory.id] = factory
        store.blocks[block.id] = block
        store.dus[du.id] = du
        factory.block_ids.append(block.id)
        block.du_ids.append(du.id)
        for i, bp_key in enumerate(blueprints, start=1):
            provision_scalable_unit(
                store, factory_id=factory.id, du_id=du.id, su_index=i,
                blueprint_key=bp_key,
            )
    else:                             # Phase 1 — 2사이트 × 2층, 전량 VR
        su_idx = 0
        blk_idx = 0
        for site in PHASE1_SITES:
            factory = AIFactory(
                id=site["id"], name=site["name"], site=site["site"],
                design_power_mw=site["design_power_mw"],
            )
            store.factories[factory.id] = factory
            for fl_no, floor in enumerate(site["floors"], start=1):
                blk_idx += 1
                block = ComputeBlock(
                    id=f"{factory.id}-f{fl_no}", factory_id=factory.id,
                    index=blk_idx, name=floor["name"],
                    power_mw=floor["power_mw"], ready=floor["ready"],
                )
                store.blocks[block.id] = block
                factory.block_ids.append(block.id)
                du = DeploymentUnit(id=f"du-{blk_idx}", block_id=block.id,
                                    index=blk_idx)
                store.dus[du.id] = du
                block.du_ids.append(du.id)
                vr_per_su = spec.get_blueprint("vr-nvl72").racks_per_su
                for n_racks in _su_sizes(floor["racks"], vr_per_su):
                    su_idx += 1
                    provision_scalable_unit(
                        store, factory_id=factory.id, du_id=du.id,
                        su_index=su_idx, blueprint_key="vr-nvl72",
                        racks=n_racks,
                    )

    # 범용 CPU 노드 풀 (DPU 장착, 테넌트당 기본 5대 자동 제공 + Managed K8s
    # 옵션 시 CP 3대 추가). AI Infra Emulator(NICo)에 호스트로 등록되어
    # DPU 기반 isolation으로 IP·OS 설치 후 테넌트에 할당된다.
    from .models import CpuNode
    for n in range(1, 61):
        nid = f"cpu-node-{n:02d}"
        store.cpu_nodes[nid] = CpuNode(id=nid, dpu_id=f"{nid}-dpu",
                                       nico_host_id=f"nh-{nid}")


def seed_demo_samples(store: Store) -> None:
    """데모 리셋/시드 직후 콘솔 메뉴(알림·인시던트·티켓)가 비지 않도록
    샘플 데이터를 시드한다 — detail/body 의 "(sample)" 표기로 구분.

    - 트레이 에뮬레이터 fault_log: 샘플 XID 장애 2건 (resolved 1 / open 1)
    - 티켓: 비어 있으면 샘플 2건 (open 1 / resolved 1)

    기본(Phase-1) 데모 시드 경로에서만 호출된다 — 테스트의 blueprints 명시
    시드는 결정적 상태를 위해 샘플 없이 시작한다."""
    from datetime import datetime, timezone

    from .models import Ticket
    from .tray_emu import EMULATOR

    EMULATOR.seed_sample_faults()

    if not store.tickets:
        now = datetime.now(timezone.utc).isoformat()
        samples = [
            ("GPU 노드 상태 점검 요청", "critical", "open",
             "training job 중 XID 79 알림 수신 — 해당 트레이 점검 요청 (sample)"),
            ("스토리지 마운트 지연 문의", "medium", "resolved",
             "VAST view 마운트 지연 문의 — 재시도 후 정상 확인 (sample)"),
        ]
        with store.lock:
            for subject, sev, status, body in samples:
                tid = f"tck-{store.next_seq('ticket')}"
                store.tickets[tid] = Ticket(
                    id=tid, tenant_id="tnt-fin-corp", subject=subject, body=body,
                    severity=sev, status=status, created_at=now,
                    updated_at=now)
