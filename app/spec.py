"""Hardware & topology specifications for NVIDIA NVL72 / DSX AI Factory.

Sources:
  - NVIDIA DSX Facilities Infrastructure Design Guide v1.0 (2026-03-12)
      MGX Gen 1.1 = GB200/GB300 NVL72 ; Gen 1.2 = Vera Rubin NVL72 ; Gen 1.3 = future
  - NVIDIA Cloud Partner: Vera Rubin NVL72 Systems Reference Design (PRD12771-001 v3.0)
  - Public NVIDIA specs (GTC 2024/2025) for GB200/GB300.

The cluster is multi-generation: a single AI Factory can host GB200, GB300 and
Vera Rubin racks side by side. Each rack is described by a RackBlueprint; the
SU/DU/Block hierarchy and the 14-racks-per-SU rule are generation-agnostic.

GB300 power is marked `preliminary=True` (public estimate, subject to change).
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Generation-agnostic building-block constants (DSX Facilities)
# ---------------------------------------------------------------------------
RACKS_PER_SU = 14                 # legacy 기본값 — 세대별 값은 blueprint.racks_per_su
CMX_RACKS_PER_SU = 2              # Context Memory racks per SU (RD 권장 2 = 32 CMS 섀시)
SU_PER_DU_MIN = 3
SU_PER_DU_MAX = 4                 # 4th SU only if MaxQ < 200 kW/rack
DU_PER_COMPUTE_BLOCK = 6
BLOCKS_PER_250MW_FACTORY = 4
OOB_SWITCHES_PER_RACK = 2         # SN2201 (in-rack)


# ---------------------------------------------------------------------------
# Per-rack blueprint
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RackBlueprint:
    key: str
    model: str
    mgx_generation: str
    gpu_arch: str
    cpu_arch: str
    # tray composition
    compute_trays: int
    nvlink_switch_trays: int
    nvswitch_asics_per_tray: int
    gpu_per_tray: int
    cpu_per_tray: int
    # accelerator specs
    gpu_hbm_gb: int
    gpu_hbm_type: str
    gpu_dies: int
    cpu_cores: int
    cpu_mem_tb: float
    nvlink_gen: str
    nvlink_per_gpu_tbps: float
    # power
    rack_tdp_kw: int
    maxq_rack_kw: int             # throughput-optimized cap
    maxp_rack_kw: int             # full-power cap (== tdp)
    power_shelf_kw: int
    power_shelves: int
    cooling: str                  # "liquid" | "hybrid"
    # networking host adapters
    dpu_sku: str
    dpu_bw_gbps: int
    dpu_per_tray: int
    connectx_supernics_per_tray: int
    racks_per_su: int = RACKS_PER_SU   # 세대별 SU 구성 (VR=16, NCP RD 확정)
    hac_tdp_mw: float = 0.0            # 1 SU = 1 HAC 열설계전력 (IT 기준)
    preliminary: bool = False     # spec not finalized (public estimate)

    @property
    def gpu_per_rack(self) -> int:
        return self.compute_trays * self.gpu_per_tray

    @property
    def cpu_per_rack(self) -> int:
        return self.compute_trays * self.cpu_per_tray

    @property
    def gpu_per_su(self) -> int:
        return self.gpu_per_rack * self.racks_per_su


# ---------------------------------------------------------------------------
# Catalog — three supported generations (all NVL72 = 72-GPU NVLink domain)
# ---------------------------------------------------------------------------
BLUEPRINTS: dict[str, RackBlueprint] = {
    "gb200-nvl72": RackBlueprint(
        key="gb200-nvl72", model="GB200 NVL72", mgx_generation="MGX Gen 1.1",
        gpu_arch="Blackwell (B200)", cpu_arch="Grace",
        compute_trays=18, nvlink_switch_trays=9, nvswitch_asics_per_tray=2,
        gpu_per_tray=4, cpu_per_tray=2,
        gpu_hbm_gb=192, gpu_hbm_type="HBM3e", gpu_dies=2,
        cpu_cores=72, cpu_mem_tb=0.48,
        nvlink_gen="NVLink5", nvlink_per_gpu_tbps=1.8,
        # NVIDIA 공칭 120kW/rack (벤더별 TDP 132·EDPp ~1.5× 편차 존재).
        # MaxQ 공식 수치 미공개 — TDP 운용(캡 없음)으로 표기 ('26.7 웹 조사)
        rack_tdp_kw=120, maxq_rack_kw=120, maxp_rack_kw=120,
        power_shelf_kw=33, power_shelves=8, cooling="hybrid",
        dpu_sku="BF3", dpu_bw_gbps=400, dpu_per_tray=1,
        connectx_supernics_per_tray=4,
    ),
    "gb300-nvl72": RackBlueprint(
        key="gb300-nvl72", model="GB300 NVL72", mgx_generation="MGX Gen 1.1",
        gpu_arch="Blackwell Ultra (B300)", cpu_arch="Grace",
        compute_trays=18, nvlink_switch_trays=9, nvswitch_asics_per_tray=2,
        gpu_per_tray=4, cpu_per_tray=2,
        gpu_hbm_gb=288, gpu_hbm_type="HBM3e", gpu_dies=2,
        cpu_cores=72, cpu_mem_tb=0.48,
        nvlink_gen="NVLink5", nvlink_per_gpu_tbps=1.8,
        # Lenovo 공칭 TDP 135kW/rack (피크 EDPp ~155kW · HPE/SMC 132~140 편차).
        # MaxQ 공식 수치 미공개 — TDP 운용으로 표기 ('26.7 웹 조사, preliminary)
        rack_tdp_kw=135, maxq_rack_kw=135, maxp_rack_kw=135,
        power_shelf_kw=33, power_shelves=8, cooling="liquid",
        dpu_sku="BF3", dpu_bw_gbps=800, dpu_per_tray=1,
        connectx_supernics_per_tray=8, preliminary=True,
    ),
    "vr-nvl72": RackBlueprint(
        key="vr-nvl72", model="Vera Rubin NVL72", mgx_generation="MGX Gen 1.2",
        gpu_arch="Rubin", cpu_arch="Vera",
        compute_trays=18, nvlink_switch_trays=9, nvswitch_asics_per_tray=4,
        gpu_per_tray=4, cpu_per_tray=2,
        gpu_hbm_gb=288, gpu_hbm_type="HBM4", gpu_dies=2,
        cpu_cores=88, cpu_mem_tb=1.44,
        nvlink_gen="NVLink6", nvlink_per_gpu_tbps=3.6,
        # 전력 정책 (확정): TDP 227kW 기준 MaxP=227 · MaxQ=187
        rack_tdp_kw=227, maxq_rack_kw=187, maxp_rack_kw=227,
        power_shelf_kw=110, power_shelves=4, cooling="liquid",
        dpu_sku="BF4-B4240V", dpu_bw_gbps=800, dpu_per_tray=1,
        connectx_supernics_per_tray=8,
        # NCP RD (RD-12835-001-01 v02): 1 SU = 16× VR NVL72 랙 = 1,152 GPU,
        # 1 SU = 1 HAC(2열×8랙, 테넌트 격리 자연 경계), HAC TDP ~3.9MW
        # (컴퓨트 227kW×16 + IB XDR 스파인랙 56kW×4 + Eth/CMX 포함)
        racks_per_su=16, hac_tdp_mw=3.9,
    ),
}

DEFAULT_BLUEPRINT = "vr-nvl72"

# ---------------------------------------------------------------------------
# Compute fabric — IB XDR 듀얼 네트워크 (NCP RD RD-12835-001-01 v02, VR 기준)
# ---------------------------------------------------------------------------
IB_FABRIC = {
    "networks": ["Fabric-A", "Fabric-B"],   # 별도 서브넷 — UFM(HA) 2세트 필요
    "rails_per_network": 4,                 # 트레이 SuperNIC 8 = 네트워크당 4
    "leaves_per_network_per_su": 16,        # 레일당 4 리프 × 4 rail
    "node_links_per_su": 2304,              # 800G — 네트워크당 1,152
    "link_speed_gbps": 800,
    "tier2_max_su": 9,                      # 2-tier: 1~9 SU (10,368 GPU)
    "tier3_max_su": 32,                     # 3-tier: 10~32 SU (36,864 GPU)
    "ib_spine_racks_per_su": 4, "ib_spine_rack_kw": 56,
}

# 컨버지드(Ethernet) 패브릭 — 3-tier 기준 SU당 spine 4 + leaf 12(NVL72 8 + CMX 4)
CONVERGED_FABRIC = {
    "tier2_max_su": 4,                      # 8 SU 이상은 3-tier 필수
    "spines_per_su_tier3": 4,
    "leaves_per_su_tier3": 12,              # NVL72용 8 + CMX용 4
}

# ---------------------------------------------------------------------------
# 냉각 (DLC — NCP RA): 1 SU = 1 HAC(2열×8랙), in-row CDU가 TCS 루프 공급
# ---------------------------------------------------------------------------
COOLING = {
    "cdu_per_su": 2,                  # HAC당 in-row CDU 이중화(2N) — RA 구성 예시
    "cdu_type": "Liquid-to-Liquid in-row CDU",
    "cdu_capacity_kw": 2000,          # 기당 열교환 용량 (예시 — HAC 3.9MW 대비 2N)
    "loop": "시설 FWS ↔ CDU ↔ TCS(랙 매니폴드/콜드플레이트)",
}


def get_blueprint(key: str) -> RackBlueprint:
    if key not in BLUEPRINTS:
        raise KeyError(
            f"unknown rack blueprint '{key}'; "
            f"choose from {sorted(BLUEPRINTS)}")
    return BLUEPRINTS[key]


# ---------------------------------------------------------------------------
# Converged-network throughput targets per GPU (NCP RD, Table 5)
# ---------------------------------------------------------------------------
TARGET_GBPS_PER_GPU = {
    "core_to_su": 22.2,
    "core_to_cmx": 100.0,
    "core_to_hps": 16.0,
    "core_to_edge": 0.05,
}

# ---------------------------------------------------------------------------
# Cooling design points (DSX Facilities, Key Mechanical Design Points)
# ---------------------------------------------------------------------------
TCS_MAX_SUPPLY_C = 45.0
TCS_FLOW_LPM_PER_KW = 1.5
CDU_CAPACITY_MW = 2.3
CDU_REDUNDANCY = "N+1"
AIR_FLOW_CFM_PER_KW = 150

# ---------------------------------------------------------------------------
# Multi-tenancy isolation reference (NCP RD, Multitenancy section)
# ---------------------------------------------------------------------------
ISOLATION_LAYERS = [
    "identity",
    "process",
    "compute_virtualization",
    "compute_isolation",
]
FOUNDATIONAL_VRFS = ["STORAGE", "EXIT"]
VNI_RANGES = {
    "compute": (10_000, 19_999),
    "converged": (20_000, 29_999),
    "oob": (30_000, 39_999),
}
