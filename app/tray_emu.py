"""Virtual compute-tray emulation — per-tray runtime behaviour for tenant
clusters.

Every ComputeTray gets a TrayRuntime. Trays whose NodeInstance is in_service
run a workload profile bound to their tenant cluster (training / inference /
idle); everything else sits in standby. A background ticker (started in the
app lifespan, 2 s period) advances the simulation:

  - per-GPU: utilization random-walk toward the profile target, HBM usage,
    temperature (follows util, 45 °C liquid-cooling floor context), power
    (idle→max scaled by the rack blueprint TDP), ECC-corrected counters and
    rare XID faults (63/79/48) that recover after a few ticks,
  - per-tray: CPU util, NVLink TX/RX, aggregate power including base load,
  - training profile dips to a checkpoint phase periodically (util down,
    storage burst semantics).

XID events are also emitted onto the trace bus (DCGM → NVSentinel path) so
the observability story stays connected. Deterministic behaviour is not a
goal here — tests assert thresholds, not exact values.
"""

from __future__ import annotations

import asyncio
import random
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import spec
from .models import NodeLifecycleState as NS
from .store import STORE
from .trace import emit

TICK_SECONDS = 2.0
GPU_IDLE_W = 90.0
TRAY_BASE_W = 800.0            # CPU/DPU/NIC/보드 기본 부하

PROFILES = {
    "training":  {"util": (88, 99), "nvlink": (2.2, 3.4), "ckpt_every": 40},
    "inference": {"util": (35, 75), "nvlink": (0.5, 1.5), "ckpt_every": 0},
    "idle":      {"util": (0, 4),   "nvlink": (0.0, 0.05), "ckpt_every": 0},
}
TOKENS_K_PER_GPU = {"Rubin": 60.0, "Blackwell Ultra (B300)": 38.0,
                    "Blackwell (B200)": 30.0}
XID_POOL = [63, 79, 48]        # row-remap / GPU fallen off bus / DBE


class GpuTelemetry(BaseModel):
    index: int
    state: str = "idle"            # idle | active | throttled | fault
    util_pct: float = 0.0
    hbm_used_gb: float = 0.0
    hbm_total_gb: int = 0
    temp_c: float = 34.0
    power_w: float = GPU_IDLE_W
    ecc_corrected: int = 0
    xid_events: list[int] = Field(default_factory=list)
    fault_ttl: int = 0


class TrayRuntime(BaseModel):
    tray_id: str
    host_id: str
    rack_id: str
    tenant_id: Optional[str] = None
    workload: str = "standby"      # standby | training | inference | idle | checkpoint
    job_name: Optional[str] = None
    cpu_util_pct: float = 2.0
    nvlink_tx_tbps: float = 0.0
    nvlink_rx_tbps: float = 0.0
    power_w: float = 350.0
    gpu_max_w: float = 1200.0
    gpu_arch: str = ""
    step: int = 0
    gpus: list[GpuTelemetry] = Field(default_factory=list)


class ClusterSummary(BaseModel):
    tenant_id: str
    profile: str
    trays: int
    gpus: int
    avg_util_pct: float
    power_kw: float
    power_cap_kw: float
    max_gpu_temp_c: float
    nvlink_tbps: float
    tokens_ks: float               # 추정 토큰 처리량 (K tokens/s)
    ecc_corrected_total: int
    fault_gpus: int


class TrayEmulator:
    HISTORY_LEN = 240                # 틱 단위 시계열 (2s 틱 기준 ≈ 8분)

    TICK_S = 2                       # 1틱 = 2초 (run_loop 주기 — TTR 환산 기준)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.trays: dict[str, TrayRuntime] = {}
        self.cluster_profile: dict[str, str] = {}    # tenant_id -> profile
        self.step = 0
        self.history_global: deque = deque(maxlen=self.HISTORY_LEN)
        self.history_tenants: dict[str, deque] = {}
        # GPU 장애 에피소드 로그 — 감지→조치→복구 라이프사이클 (MTTR 산출)
        self.fault_log: deque = deque(maxlen=500)

    # -- topology alignment ---------------------------------------------------
    def sync_from_store(self) -> None:
        """Create/refresh runtimes; bind in_service trays to their tenant."""
        with self._lock, STORE.lock:
            live_ids = set()
            for tray in STORE.trays.values():
                live_ids.add(tray.id)
                rack = STORE.racks.get(tray.rack_id)
                bp = spec.BLUEPRINTS.get(rack.blueprint_key) if rack else None
                rt = self.trays.get(tray.id)
                if rt is None:
                    rt = TrayRuntime(
                        tray_id=tray.id, host_id=f"nh-{tray.id}",
                        rack_id=tray.rack_id,
                        gpu_arch=bp.gpu_arch if bp else "",
                        gpu_max_w=round(
                            (rack.tdp_kw * 1000 * 0.72 / bp.gpu_per_rack)
                            if (rack and bp) else 1200.0),
                        gpus=[GpuTelemetry(
                            index=i, hbm_total_gb=bp.gpu_hbm_gb if bp else 192)
                            for i in range(bp.gpu_per_tray if bp else 4)],
                    )
                    self.trays[tray.id] = rt
                node = STORE.node_instances.get(f"ni-{tray.id}")
                if node and node.state == NS.in_service and node.tenant_id:
                    if rt.tenant_id != node.tenant_id:      # 신규 인도
                        rt.tenant_id = node.tenant_id
                        rt.job_name = f"{node.tenant_id}-job-{node.order_id}"
                        self.cluster_profile.setdefault(node.tenant_id,
                                                        "training")
                    rt.workload = self.cluster_profile.get(
                        node.tenant_id, "training")
                else:
                    if rt.tenant_id is not None:            # 회수됨
                        rt.tenant_id = None
                        rt.job_name = None
                        for g in rt.gpus:
                            g.state, g.util_pct = "idle", 0.0
                            g.hbm_used_gb, g.xid_events = 0.0, []
                    rt.workload = "standby"
            for tid in list(self.trays):
                if tid not in live_ids:
                    del self.trays[tid]
            active_tenants = {rt.tenant_id for rt in self.trays.values()
                              if rt.tenant_id}
            for t in list(self.cluster_profile):
                if t not in active_tenants:
                    del self.cluster_profile[t]

    def set_profile(self, tenant_id: str, profile: str) -> None:
        if profile not in PROFILES:
            raise HTTPException(422, f"unknown profile '{profile}' "
                                     f"(choose from {sorted(PROFILES)})")
        self.sync_from_store()          # 인도 직후에도 즉시 반영되도록
        with self._lock:
            if tenant_id not in self.cluster_profile:
                raise HTTPException(404, f"no active cluster for '{tenant_id}'")
            self.cluster_profile[tenant_id] = profile

    # -- simulation ------------------------------------------------------------
    def tick(self, n: int = 1) -> int:
        self.sync_from_store()
        with self._lock:
            for _ in range(n):
                self.step += 1
                for rt in self.trays.values():
                    self._advance(rt)
                self._record_locked()
        return self.step

    def _record_locked(self) -> None:
        """틱마다 전역/테넌트별 시계열 스냅샷 기록 (라인 그래프용)."""
        at = datetime.now(timezone.utc).isoformat()
        summaries = self._summaries_locked()
        active_gpus = sum(c.gpus for c in summaries)
        total_gpus = sum(len(rt.gpus) for rt in self.trays.values())
        util = (sum(c.avg_util_pct * c.gpus for c in summaries) / active_gpus
                if active_gpus else 0.0)
        self.history_global.append({
            "at": at, "step": self.step,
            "total_gpus": total_gpus, "active_gpus": active_gpus,
            "alloc_pct": round(active_gpus / total_gpus * 100, 1)
                          if total_gpus else 0.0,
            "avg_util_pct": round(util, 1),
            "power_kw": round(sum(c.power_kw for c in summaries), 1),
            "tokens_ks": round(sum(c.tokens_ks for c in summaries), 1),
            "max_gpu_temp_c": max((c.max_gpu_temp_c for c in summaries),
                                  default=0.0),
            "fault_gpus": sum(c.fault_gpus for c in summaries),
        })
        live = set()
        for c in summaries:
            live.add(c.tenant_id)
            self.history_tenants.setdefault(
                c.tenant_id, deque(maxlen=self.HISTORY_LEN)).append({
                    "at": at, "step": self.step, "gpus": c.gpus,
                    "avg_util_pct": c.avg_util_pct, "power_kw": c.power_kw,
                    "power_cap_kw": c.power_cap_kw,
                    "max_gpu_temp_c": c.max_gpu_temp_c,
                    "nvlink_tbps": c.nvlink_tbps, "tokens_ks": c.tokens_ks,
                    "fault_gpus": c.fault_gpus,
                    "ecc": c.ecc_corrected_total,
                })
        for t in list(self.history_tenants):      # 회수된 테넌트 시계열 정리
            if t not in live:
                del self.history_tenants[t]

    def history(self, tenant_id: Optional[str] = None,
                limit: int = 180) -> list:
        with self._lock:
            src = (self.history_tenants.get(tenant_id)
                   if tenant_id else self.history_global)
            return list(src or [])[-limit:]

    def _advance(self, rt: TrayRuntime) -> None:
        rt.step = self.step
        if rt.workload == "standby" or rt.tenant_id is None:
            rt.cpu_util_pct = max(0.5, min(4.0, rt.cpu_util_pct
                                           + random.uniform(-0.5, 0.5)))
            rt.nvlink_tx_tbps = rt.nvlink_rx_tbps = 0.0
            for g in rt.gpus:
                g.state, g.util_pct = "idle", 0.0
                g.temp_c = round(random.uniform(31, 36), 1)
                g.power_w = GPU_IDLE_W
            rt.power_w = round(TRAY_BASE_W / 2
                               + sum(g.power_w for g in rt.gpus))
            return

        profile_name = self.cluster_profile.get(rt.tenant_id, "training")
        prof = PROFILES[profile_name]
        in_ckpt = (prof["ckpt_every"]
                   and self.step % prof["ckpt_every"] < 4)
        rt.workload = "checkpoint" if in_ckpt else profile_name
        lo, hi = prof["util"]
        target = random.uniform(lo, hi) if not in_ckpt else random.uniform(18, 30)

        for g in rt.gpus:
            if g.fault_ttl > 0:                     # XID 복구 대기
                g.fault_ttl -= 1
                g.state, g.util_pct, g.power_w = "fault", 0.0, GPU_IDLE_W
                g.temp_c = max(40.0, g.temp_c - 3)
                if g.fault_ttl == 0:                # 복구 완료 — 에피소드 마감
                    for rec in reversed(self.fault_log):
                        if (rec["tray_id"] == rt.tray_id
                                and rec["gpu"] == g.index
                                and rec["resolved_at"] is None):
                            rec["resolved_at"] = datetime.now(
                                timezone.utc).isoformat()
                            rec["ttr_s"] = round(
                                (self.step - rec["started_step"])
                                * self.TICK_S, 1)
                            rec["state"] = "resolved"
                            break
                continue
            g.util_pct = round(min(100.0, max(0.0,
                g.util_pct + (target - g.util_pct) * 0.35
                + random.uniform(-3, 3))), 1)
            g.state = "active" if g.util_pct > 8 else "idle"
            g.temp_c = round(min(92.0, 34 + g.util_pct * 0.50
                                 + random.uniform(-1.5, 1.5)), 1)
            if g.temp_c > 88:
                g.state = "throttled"
            g.power_w = round(GPU_IDLE_W + g.util_pct / 100.0
                              * (rt.gpu_max_w - GPU_IDLE_W))
            base_hbm = 0.55 if profile_name == "training" else 0.35
            g.hbm_used_gb = round(g.hbm_total_gb
                                  * (base_hbm + 0.4 * g.util_pct / 100), 1)
            if random.random() < 0.002:
                g.ecc_corrected += 1
            if random.random() < 0.0001:            # 희귀 XID 폴트 (~수천 GPU·수십 틱당 1건)
                xid = random.choice(XID_POOL)
                g.xid_events.append(xid)
                g.fault_ttl = 5
                g.state = "fault"
                self.fault_log.append({
                    "tray_id": rt.tray_id, "host_id": rt.host_id,
                    "gpu": g.index, "xid": xid, "tenant_id": rt.tenant_id,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "started_step": self.step,
                    "action": "NVSentinel — cordon/drain 지정 후 복구 절차",
                    "tta_s": self.TICK_S,       # 감지→조치 1틱 내 (자동)
                    "resolved_at": None, "ttr_s": None, "state": "open"})
                emit(f"DCGM({rt.tray_id})", "NVSentinel", "internal",
                     f"XID {xid} 감지",
                     f"GPU{g.index} — cordon/drain 후보, fault_ttl 5틱 "
                     "(자동 복구 시뮬레이션)",
                     payload={"tray": rt.tray_id, "gpu": g.index, "xid": xid,
                              "tenant": rt.tenant_id},
                     host_id=rt.host_id)

        util_avg = sum(g.util_pct for g in rt.gpus) / max(1, len(rt.gpus))
        lo_nv, hi_nv = prof["nvlink"]
        scale = util_avg / 100.0
        rt.nvlink_tx_tbps = round(random.uniform(lo_nv, hi_nv) * scale, 2)
        rt.nvlink_rx_tbps = round(random.uniform(lo_nv, hi_nv) * scale, 2)
        rt.cpu_util_pct = round(min(100, 15 + util_avg * 0.45
                                    + random.uniform(-4, 4)), 1)
        rt.power_w = round(TRAY_BASE_W + sum(g.power_w for g in rt.gpus))

    # -- read model --------------------------------------------------------------
    def snapshot(self, tenant_id: Optional[str] = None,
                 rack_id: Optional[str] = None) -> list:
        with self._lock:
            out = [rt for rt in self.trays.values()
                   if (tenant_id is None or rt.tenant_id == tenant_id)
                   and (rack_id is None or rt.rack_id == rack_id)]
            return sorted(out, key=lambda r: r.tray_id)

    def tray(self, tray_id: str) -> TrayRuntime:
        with self._lock:
            rt = self.trays.get(tray_id)
            if not rt:
                raise HTTPException(404, f"emu: tray '{tray_id}' not found")
            return rt

    def clusters(self) -> list:
        with self._lock:
            return self._summaries_locked()

    def _summaries_locked(self) -> list:
            by_t: dict[str, list] = {}
            for rt in self.trays.values():
                if rt.tenant_id:
                    by_t.setdefault(rt.tenant_id, []).append(rt)
            out = []
            for tid, trays in sorted(by_t.items()):
                gpus = [g for rt in trays for g in rt.gpus]
                racks = {rt.rack_id for rt in trays}
                cap = sum(STORE.racks[r].power_cap_kw
                          for r in racks if r in STORE.racks)
                coef = TOKENS_K_PER_GPU.get(trays[0].gpu_arch, 30.0)
                out.append(ClusterSummary(
                    tenant_id=tid,
                    profile=self.cluster_profile.get(tid, "training"),
                    trays=len(trays), gpus=len(gpus),
                    avg_util_pct=round(sum(g.util_pct for g in gpus)
                                       / max(1, len(gpus)), 1),
                    power_kw=round(sum(rt.power_w for rt in trays) / 1000, 1),
                    power_cap_kw=cap,
                    max_gpu_temp_c=max((g.temp_c for g in gpus), default=0),
                    nvlink_tbps=round(sum(rt.nvlink_tx_tbps for rt in trays), 1),
                    tokens_ks=round(sum(g.util_pct / 100 * coef
                                        for g in gpus), 1),
                    ecc_corrected_total=sum(g.ecc_corrected for g in gpus),
                    fault_gpus=sum(1 for g in gpus if g.state == "fault"),
                ))
            return out

    def reset(self) -> None:
        with self._lock:
            self.trays.clear()
            self.cluster_profile.clear()
            self.history_global.clear()
            self.history_tenants.clear()
            self.fault_log.clear()
            self.step = 0

    # -- GPU 장애 조치 지표 (가용성·MTTR) --------------------------------------
    def faults(self, tenant_id: Optional[str] = None, limit: int = 30) -> dict:
        with self._lock:
            items = [f for f in self.fault_log
                     if not tenant_id or f["tenant_id"] == tenant_id]
            gpus = sum(len(rt.gpus) for rt in self.trays.values()
                       if not tenant_id or rt.tenant_id == tenant_id)
        open_ = [f for f in items if f["resolved_at"] is None]
        resolved = [f for f in items if f["resolved_at"] is not None]
        mttr = (round(sum(f["ttr_s"] for f in resolved) / len(resolved), 1)
                if resolved else None)
        mtta = (round(sum(f["tta_s"] for f in items) / len(items), 1)
                if items else None)
        avail = (round((1 - len(open_) / gpus) * 100, 3) if gpus else None)
        return {
            "gpus_total": gpus,
            "faults_open": len(open_),
            "faults_resolved": len(resolved),
            "availability_pct": avail,      # 정상 GPU / 전체 (진행 장애 제외)
            "mtta_s": mtta,                 # 감지→조치 (NVSentinel 자동)
            "mttr_s": mttr,                 # 감지→복구 평균
            "recent": (open_ + resolved)[-limit:][::-1],
        }

    async def run_loop(self) -> None:
        """Background ticker — started in the app lifespan."""
        try:
            while True:
                await asyncio.sleep(TICK_SECONDS)
                self.tick()
        except asyncio.CancelledError:
            pass


EMULATOR = TrayEmulator()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/v1/emu", tags=["emulation"])


class _ProfileBody(BaseModel):
    profile: str                    # training | inference | idle


@router.get("/faults")
def faults(tenant_id: Optional[str] = None, limit: int = 30) -> dict:
    """GPU 장애 조치 지표 — 가용성·MTTA·MTTR + 최근 에피소드."""
    return EMULATOR.faults(tenant_id, limit)


@router.get("/status")
def status() -> dict:
    active = sum(1 for rt in EMULATOR.trays.values() if rt.tenant_id)
    return {"step": EMULATOR.step, "tick_seconds": TICK_SECONDS,
            "trays_total": len(EMULATOR.trays), "trays_active": active,
            "profiles": sorted(PROFILES)}


@router.get("/clusters", response_model=list[ClusterSummary])
def clusters() -> list[ClusterSummary]:
    return EMULATOR.clusters()


@router.get("/history")
def history(tenant_id: Optional[str] = None, limit: int = 180) -> list:
    """시계열 스냅샷 — tenant_id 없으면 전역(전체 GPU 사용 현황)."""
    return EMULATOR.history(tenant_id=tenant_id, limit=min(max(limit, 1), 240))


@router.get("/trays")
def trays(tenant_id: Optional[str] = None,
          rack_id: Optional[str] = None) -> list:
    return EMULATOR.snapshot(tenant_id=tenant_id, rack_id=rack_id)


@router.get("/trays/{tray_id:path}", response_model=TrayRuntime)
def tray_detail(tray_id: str) -> TrayRuntime:
    return EMULATOR.tray(tray_id)


@router.post("/tick")
def tick(n: int = 1) -> dict:
    return {"step": EMULATOR.tick(min(max(n, 1), 200))}


@router.post("/clusters/{tenant_id}/workload")
def set_workload(tenant_id: str, body: _ProfileBody) -> dict:
    EMULATOR.set_profile(tenant_id, body.profile)
    emit("Operator", f"EMU({tenant_id})", "internal",
         f"워크로드 프로파일 변경 → {body.profile}",
         "테넌트 클러스터 전 트레이에 적용")
    return {"tenant_id": tenant_id, "profile": body.profile}
