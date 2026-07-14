"""Fake NVIDIA NICo (Infra Controller) — Day 0/1/2 simulator.

Stands in for the real NICo site controller until its REST/gRPC contract is
pinned down, so the D1 ComputeAdapter and the order pipeline can be developed
and tested against realistic semantics:

  - 1 managed host == 1 ComputeTray (host_id = "nh-{tray_id}").
  - Host state machine (simplified from the NICo Day 0/1/2 lifecycle):
        pool_ready -> reserved -> provisioning -> provisioned -> allocated
                   -> released -> sanitizing -> pool_ready | rma
        (validate: discovered/quarantined -> pool_ready)
  - Async-job semantics: provision/sanitize return a job that the caller must
    poll. `job_latency` controls how many polls a job stays "running" so tests
    can exercise the poll-until-converged pattern deterministically (default 0
    = jobs finish on creation; no wall-clock sleeps anywhere).
  - Fault injection: `inject_failure(host_id, op)` makes the next `op` on that
    host fail (provision -> quarantined, sanitize -> rma), consumed on use.

Exposed both as an in-process object (FAKE_NICO, used by LocalNicoAdapter) and
as a REST router under /fake-nico with the same shapes NicoHttpAdapter speaks.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from enum import Enum
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import spec
from .store import STORE, Store
from .trace import emit


def _ips_for_tray(tray_id: str) -> Optional[dict]:
    """Deterministic management-plane addressing per tray.

    OOB(BMC)와 DPU 언더레이는 Day 0에 고정, 호스트 IP는 프로비저닝 시
    DPU-측 DHCP가 /30로 할당한다 (테넌트 트래픽의 언더레이 유출 차단)."""
    m = re.match(r"su-(\d+)-rack-(\d+)-tray-(\d+)", tray_id)
    if not m:
        return None
    su, rack, tray = (int(g) for g in m.groups())
    net = su * 16 + rack
    return {
        "bmc_ip": f"10.99.{net}.{tray + 1}",
        "dpu_ip": f"172.16.{net}.{tray + 1}",
        "host_ip": f"10.{100 + su}.{rack}.{tray * 4 + 2}",   # /30 host addr
    }


class NicoHostState(str, Enum):
    discovered = "discovered"
    quarantined = "quarantined"
    pool_ready = "pool_ready"
    reserved = "reserved"
    provisioning = "provisioning"
    provisioned = "provisioned"
    allocated = "allocated"
    released = "released"
    sanitizing = "sanitizing"
    rma = "rma"


class NicoHost(BaseModel):
    host_id: str
    tray_id: str = ""                 # physical mapping (LLDP/serial match)
    sku: str = ""                     # blueprint key
    site: str = ""                    # 소속 사이트(팩토리) — NICo 인스턴스 단위
    state: NicoHostState = NicoHostState.pool_ready
    firmware_ok: bool = True
    attested: bool = True
    cordoned: bool = False
    instance_id: Optional[str] = None
    tenant_ref: Optional[str] = None
    image_ref: Optional[str] = None
    bmc_ip: str = ""                  # OOB — Day 0 고정
    dpu_ip: str = ""                  # DPU 언더레이 — Day 0 고정
    host_ip: str = ""                 # 프로비저닝 시 DPU-DHCP 할당, 소거 시 회수
    segment_id: Optional[str] = None  # 소속 VPC(network segment)


class NicoSegment(BaseModel):
    """Tenant VPC — L3 VXLAN/EVPN 격리 도메인 (DPU HBN이 강제).

    실 NICo(infra-controller) 대응: VPC virtualizer는 FNN(L3 EVPN, 신규) 또는
    ETV(legacy Ethernet Virtualizer) — crates/agent/src/nvue.rs의
    VpcVirtualizationType. 데이터플레인 VRF 명명은 `vpc_<vni>`
    (templates/tests/full_nvue_startup_fnn_l3.yaml.expected)."""
    segment_id: str
    tenant_ref: str
    vrf: str                          # NeoCloud OS 논리 VRF 이름 (VRF-<tenant>)
    l3vni: int
    converged_vni: int
    virtualizer: str = "fnn"          # fnn(L3 EVPN) | etv(legacy)
    vrf_dataplane: str = ""           # HBN 실측 VRF — vpc_<l3vni>
    host_ids: list[str] = Field(default_factory=list)
    allocation_id: Optional[str] = None
    state: str = "active"


class NicoJob(BaseModel):
    job_id: str
    op: str                           # provision | sanitize | release
    host_id: str
    state: str = "running"            # running | succeeded | failed
    detail: str = ""
    remaining_polls: int = 0


SANITIZE_STEPS = [
    "nvme_secure_erase",
    "gpu_memory_wipe",
    "system_memory_wipe",
    "tpm_reset",
    "re_attestation",
    "firmware_revalidation",
    "network_state_clear",
]


class SanitizeReport(BaseModel):
    host_id: str
    passed: bool
    steps: list[dict] = Field(default_factory=list)   # [{step, ok}]


class FakeNico:
    def __init__(self) -> None:
        self.hosts: dict[str, NicoHost] = {}
        self.jobs: dict[str, NicoJob] = {}
        self.segments: dict[str, NicoSegment] = {}
        self.sanitize_reports: dict[str, SanitizeReport] = {}
        self.job_latency = 0          # polls a job stays running before finishing
        self._fail_flags: dict[str, set] = {}
        self._seq = 0

    # -- seeding / test helpers ----------------------------------------------
    def seed_from_store(self, store: Store) -> None:
        """Register one host per ComputeTray, as if Day 0 already passed."""
        self.hosts.clear()
        self.jobs.clear()
        self.segments.clear()
        self.sanitize_reports.clear()
        self._fail_flags.clear()
        self._seq = 0
        # SU → 사이트(팩토리) 매핑 — 사이트별 별개 NICo 인스턴스 표현용
        su_site = {}
        for f in store.factories.values():
            for bid in f.block_ids:
                for did in store.blocks[bid].du_ids:
                    for sid in store.dus[did].su_ids:
                        su_site[sid] = f.name
        for tray in store.trays.values():
            rack = store.racks.get(tray.rack_id)
            host_id = f"nh-{tray.id}"
            ips = _ips_for_tray(tray.id) or {}
            su_id = tray.rack_id.rsplit("-rack-", 1)[0]
            self.hosts[host_id] = NicoHost(
                host_id=host_id, tray_id=tray.id,
                sku=rack.blueprint_key if rack else "",
                site=su_site.get(su_id, ""),
                state=NicoHostState.pool_ready,
                bmc_ip=ips.get("bmc_ip", ""), dpu_ip=ips.get("dpu_ip", ""),
            )
        # 범용 CPU 노드 풀 — AI Infra Emulator 인벤토리에 호스트로 등록.
        # GPU 트레이와 동일한 Day1 경로(rshim BFB → BMC Redfish → DPU-DHCP →
        # PXE → cloud-init)로 프로비저닝되고 DPU isolation으로 테넌트에 할당.
        for cn in store.cpu_nodes.values():
            host_id = cn.nico_host_id or f"nh-{cn.id}"
            idx = int(cn.id.rsplit("-", 1)[1])
            self.hosts[host_id] = NicoHost(
                host_id=host_id, tray_id=cn.id, sku="cpu-epyc",
                site="", state=NicoHostState.pool_ready,
                bmc_ip=f"10.254.1.{idx}", dpu_ip=f"10.255.1.{idx}",
            )

    def add_ghost(self, host_id: str, sku: str = "vr-nvl72") -> NicoHost:
        """A host NICo knows about but the control plane does not (reconcile)."""
        host = NicoHost(host_id=host_id, sku=sku, state=NicoHostState.pool_ready)
        self.hosts[host_id] = host
        return host

    def inject_failure(self, host_id: str, op: str) -> None:
        self._fail_flags.setdefault(host_id, set()).add(op)

    def _consume_failure(self, host_id: str, op: str) -> bool:
        flags = self._fail_flags.get(host_id, set())
        if op in flags:
            flags.discard(op)
            return True
        return False

    # -- internals -------------------------------------------------------------
    def _host(self, host_id: str) -> NicoHost:
        host = self.hosts.get(host_id)
        if not host:
            raise HTTPException(404, f"nico: host '{host_id}' not found")
        return host

    def _require(self, host: NicoHost, *states: NicoHostState) -> None:
        if host.state not in states:
            raise HTTPException(
                409, f"nico: host '{host.host_id}' is '{host.state.value}', "
                     f"expected one of {sorted(s.value for s in states)}")

    def _new_job(self, op: str, host: NicoHost, fail: bool) -> NicoJob:
        self._seq += 1
        job = NicoJob(
            job_id=f"job-{self._seq}", op=op, host_id=host.host_id,
            remaining_polls=self.job_latency,
        )
        self.jobs[job.job_id] = job
        if job.remaining_polls <= 0:
            self._finish_job(job, fail)
        elif fail:                    # remember to fail on completion
            job.detail = "will-fail"
        return job

    def _finish_job(self, job: NicoJob, fail: bool) -> None:
        host = self._host(job.host_id)
        if job.op == "provision":
            if fail:
                job.state, job.detail = "failed", "firmware mismatch during PXE"
                host.state = NicoHostState.quarantined
                host.host_ip = ""
                emit("NICo.APIService", "NeoCloudOS.D1", "REST",
                     f"job {job.job_id} FAILED",
                     "PXE 중 펌웨어 불일치 — 호스트 quarantine (break-fix 트랙)",
                     payload={"job_id": job.job_id, "error": job.detail},
                     host_id=host.host_id)
            else:
                job.state = "succeeded"
                host.state = NicoHostState.provisioned
                emit("NICo.APIService", "NeoCloudOS.D1", "REST",
                     f"job {job.job_id} succeeded",
                     f"OS 설치 완료 — host_ip={host.host_ip}, UEFI 잠금 적용",
                     payload={"job_id": job.job_id, "host_ip": host.host_ip,
                              "image_ref": host.image_ref},
                     host_id=host.host_id)
        elif job.op == "release":
            job.state = "succeeded"
            host.state = NicoHostState.released
            emit("NICo.APIService", "NeoCloudOS.D1", "REST",
                 f"job {job.job_id} succeeded",
                 f"인스턴스 {host.instance_id} 해제 — 테넌트 바인딩 제거, "
                 "sanitize 대기",
                 payload={"instance_id": host.instance_id,
                          "tenant_ref": host.tenant_ref},
                 host_id=host.host_id)
            host.instance_id = None
            host.tenant_ref = None
        elif job.op == "sanitize":
            steps, passed = [], True
            for step in SANITIZE_STEPS:
                ok = not (fail and step == "re_attestation")
                steps.append({"step": step, "ok": ok})
                emit("NICo.Sanitizer", f"Host({host.tray_id or host.host_id})",
                     "internal", f"sanitize: {step}",
                     "통과" if ok else "실패 — 소거 중단, RMA 판정",
                     host_id=host.host_id)
                if not ok:
                    passed = False
                    break
            self.sanitize_reports[host.host_id] = SanitizeReport(
                host_id=host.host_id, passed=passed, steps=steps)
            if passed:
                job.state = "succeeded"
                host.state = NicoHostState.pool_ready
                host.attested = True
                host.image_ref = None
                host.host_ip = ""      # DHCP 임대 회수
            else:
                job.state, job.detail = "failed", "re-attestation failed"
                host.state = NicoHostState.rma

    # -- public API (mirrors the REST surface) ---------------------------------
    def list_hosts(self) -> list[NicoHost]:
        return list(self.hosts.values())

    def get_host(self, host_id: str) -> NicoHost:
        return self._host(host_id)

    def validate(self, host_id: str) -> NicoHost:
        """Day 0 re-validation (SKU/firmware/attestation)."""
        host = self._host(host_id)
        self._require(host, NicoHostState.discovered, NicoHostState.quarantined)
        host.state = (NicoHostState.pool_ready
                      if host.firmware_ok else NicoHostState.quarantined)
        host.attested = host.firmware_ok
        return host

    def reserve(self, host_id: str) -> NicoHost:
        host = self._host(host_id)
        self._require(host, NicoHostState.pool_ready)
        host.state = NicoHostState.reserved
        return host

    def unreserve(self, host_id: str) -> NicoHost:
        host = self._host(host_id)
        self._require(host, NicoHostState.reserved)
        host.state = NicoHostState.pool_ready
        return host

    def provision(self, host_id: str, image_ref: str) -> NicoJob:
        """Day 1 프로비저닝 — DPU provisioning → BMC 제어 → DPU-DHCP → PXE
        → cloud-init.

        각 서브스텝을 실제 메시지 페이로드와 함께 trace로 기록한다."""
        host = self._host(host_id)
        self._require(host, NicoHostState.reserved)
        host.state = NicoHostState.provisioning
        host.image_ref = image_ref
        ips = _ips_for_tray(host.tray_id) or {}
        bmc = f"BMC({host.bmc_ip or host.host_id})"

        # 0. DPU provisioning — 호스트 부트 전제: DPU OS(BFB)·agent·부트 서비스
        #    (실 NICo: rshim으로 BFB 푸시, forge-dpu-agent systemd, 테넌트 간
        #     재프로비저닝은 admin-cli dpu/reprovision — 멱등 검증 후 스킵)
        emit("NICo.APIService", f"DPU-BMC({host.bmc_ip or host.host_id})",
             "rshim",
             "DPU provisioning — BFB 이미지 푸시·검증",
             "BlueField bootstream(BFB)으로 DPU OS(DOCA) 설치 — "
             "forge-dpu-agent(systemd)·OTel agent 포함, 버전 일치 시 스킵(멱등)",
             payload={"bfb": "doca-2.x + forge-dpu-agent",
                      "transport": "rshim (BMC 경유)",
                      "idempotent": True}, host_id=host_id)
        emit(f"DPU-Agent({ips.get('dpu_ip', host.dpu_ip or '?')})",
             "DPU(부트 서비스)", "internal",
             "DPU 서비스 구성 — DPU-DHCP 서버·부트 프록시 준비",
             f"DPU-DHCP 기동: 테넌트 /30 풀을 pf0hpf에 바인딩 · iPXE 릴레이 "
             "(호스트↔NICo.PXE) 활성 · HBN 컨테이너 헬스 확인 — 이후 호스트 "
             "PXE 부트가 DPU에서 종단됨",
             payload={"dhcp": {"bind": "pf0hpf", "pool": "/30 per-host",
                               "underlay": "차단"},
                      "boot_proxy": "iPXE relay → NICo.PXE",
                      "hbn": "container healthy"}, host_id=host_id)

        # 1. BMC(Redfish): PXE 원스부트 설정 + 강제 재시작 (DPU 경유 부트)
        emit("NICo.APIService", bmc, "Redfish",
             "PATCH /redfish/v1/Systems/Self",
             "부트소스 오버라이드 — 다음 1회 PXE (DPU 인터페이스 경유)",
             payload={"Boot": {"BootSourceOverrideTarget": "Pxe",
                               "BootSourceOverrideEnabled": "Once",
                               "BootSourceOverrideMode": "UEFI"}},
             host_id=host_id)
        emit("NICo.APIService", bmc, "Redfish",
             "POST /redfish/v1/Systems/Self/Actions/ComputerSystem.Reset",
             "호스트 강제 재시작 — PXE 부트 개시",
             payload={"ResetType": "ForceRestart"}, host_id=host_id)

        # 2. DPU-측 DHCP: 호스트 IP 할당 (per-host DHCP, 언더레이 차단)
        #    CPU 노드(트레이 아님)는 전용 대역 10.250.1.x/30에서 임대
        if host.sku == "cpu-epyc":
            host.host_ip = f"10.250.1.{int(host.tray_id.rsplit('-', 1)[1])}"
        else:
            host.host_ip = ips.get("host_ip", "")
        emit(f"DPU-DHCP({host.dpu_ip})", f"Host({host.tray_id})", "DHCP",
             "DISCOVER → OFFER → REQUEST → ACK",
             f"호스트 {host.host_ip}/30 임대 — DPU가 DHCP 종단, "
             "테넌트 트래픽의 언더레이 도달 원천 차단",
             payload={"yiaddr": host.host_ip, "subnet": "255.255.255.252",
                      "router": host.host_ip.rsplit(".", 1)[0] + "."
                      + str(int(host.host_ip.rsplit(".", 1)[1]) - 1)
                      if host.host_ip else "", "lease_s": 86400,
                      "next-server": "nico-pxe.mgmt", "filename": "boot.ipxe"},
             host_id=host_id)

        # 3. PXE/iPXE: 부트 아티팩트 서빙 + OS 이미지 설치
        emit(f"Host({host.tray_id})", "NICo.PXE", "PXE",
             "GET /boot.ipxe → kernel/initrd → 이미지 스트리밍",
             f"테넌트 OS 이미지 '{image_ref}' 설치 (HTTP, DPU 경유)",
             payload={"image_ref": image_ref, "artifacts":
                      ["vmlinuz", "initrd.img", f"{image_ref}.rootfs.img"]},
             host_id=host_id)

        # 4. cloud-init: 호스트 IP 고정·잠금(lockdown)
        emit("NICo.PXE", f"Host({host.tray_id})", "cloud-init",
             "cloud-init meta/user-data 적용",
             "호스트 IP 고정(netplan) · SSH 키 주입 · UEFI 잠금 · "
             "BMC 자격증명 로테이션 · in-band host→BMC 차단",
             payload={"netplan": {"ethernets": {"enp1s0": {
                          "addresses": [f"{host.host_ip}/30"]}}},
                      "uefi_lockdown": True, "bmc_credential_rotate": True,
                      "inband_bmc_block": True},
             host_id=host_id)

        return self._new_job("provision", host,
                             fail=self._consume_failure(host_id, "provision"))

    def allocate(self, host_id: str, tenant_ref: str) -> NicoHost:
        host = self._host(host_id)
        self._require(host, NicoHostState.provisioned)
        self._seq += 1
        host.state = NicoHostState.allocated
        host.instance_id = f"inst-{self._seq}"
        host.tenant_ref = tenant_ref
        emit("NICo.APIService", f"FMDS({host.tray_id})", "internal",
             "인스턴스 메타데이터 등록",
             f"instance {host.instance_id} → tenant {tenant_ref} — "
             "호스트 로컬 메타데이터 서비스(FMDS) 갱신",
             payload={"instance_id": host.instance_id, "tenant": tenant_ref,
                      "host_ip": host.host_ip, "instance_type": host.sku},
             host_id=host_id)
        return host

    def release(self, instance_id: str) -> NicoJob:
        for host in self.hosts.values():
            if host.instance_id == instance_id:
                self._require(host, NicoHostState.allocated)
                return self._new_job("release", host, fail=False)
        raise HTTPException(404, f"nico: instance '{instance_id}' not found")

    def abort_provision(self, host_id: str) -> NicoHost:
        """Roll a provisioned-but-never-allocated host back for sanitize."""
        host = self._host(host_id)
        self._require(host, NicoHostState.provisioned, NicoHostState.provisioning)
        host.state = NicoHostState.released
        return host

    def sanitize(self, host_id: str) -> NicoJob:
        host = self._host(host_id)
        self._require(host, NicoHostState.released)
        host.state = NicoHostState.sanitizing
        return self._new_job("sanitize", host,
                             fail=self._consume_failure(host_id, "sanitize"))

    def get_job(self, job_id: str) -> NicoJob:
        job = self.jobs.get(job_id)
        if not job:
            raise HTTPException(404, f"nico: job '{job_id}' not found")
        if job.state == "running":
            job.remaining_polls -= 1
            if job.remaining_polls <= 0:
                self._finish_job(job, fail=(job.detail == "will-fail"))
        return job

    def cordon(self, host_id: str, reason: str = "") -> NicoHost:
        host = self._host(host_id)
        host.cordoned = True
        return host

    # -- network segments (tenant VPC, DPU HBN 강제) ---------------------------
    # 실 NICo(NVIDIA/infra-controller) DPU isolation 동작을 재현:
    #   ① API(carbide)가 VPC/network-segment 생성 — virtualizer FNN(L3 EVPN)
    #   ② 각 BlueField의 dpu-agent(periodic_config_fetcher)가 gRPC로
    #      ManagedHostNetworkConfigResponse 폴링 (crates/agent/src/main_loop.rs)
    #   ③ agent가 NVUE 템플릿(nvue_startup_fnn.conf) 렌더 → HBN 컨테이너에
    #      적용: nv config apply → ifreload -a → neighmgr 재시작 (hbn.rs)
    #   ④ BGP summary 검증 — ToR·routeserver peer / 테넌트 경로 광고 (hbn.rs)
    def create_segment(self, tenant_ref: str, vrf: str, l3vni: int,
                       converged_vni: int, host_ids: list,
                       allocation_id: Optional[str] = None) -> NicoSegment:
        self._seq += 1
        dp_vrf = f"vpc_{l3vni}"           # HBN 데이터플레인 VRF 명명 규칙
        seg = NicoSegment(
            segment_id=f"seg-{self._seq}", tenant_ref=tenant_ref, vrf=vrf,
            l3vni=l3vni, converged_vni=converged_vni,
            vrf_dataplane=dp_vrf,
            host_ids=list(host_ids), allocation_id=allocation_id)
        self.segments[seg.segment_id] = seg
        emit("NICo.APIService(carbide)", "NICo.NetworkStore", "internal",
             f"VPC + network-segment {seg.segment_id} 생성",
             f"테넌트 VPC — virtualizer FNN(L3 EVPN) · 데이터플레인 VRF {dp_vrf} · "
             f"L3VNI {l3vni} · host {len(host_ids)}대. desired state로 기록되고 "
             "각 DPU agent가 폴링으로 수렴(선언적)",
             payload={"segment_id": seg.segment_id,
                      "virtualizer": "fnn (VpcVirtualizationType::Fnn)",
                      "vrf": dp_vrf, "l3vni": l3vni,
                      "converged_vni": converged_vni,
                      "route_target": f"auto (65100:{l3vni})",
                      "hosts": len(host_ids)})
        sample = self.hosts.get(host_ids[0]) if host_ids else None
        emit(f"DPU-Agent({sample.dpu_ip if sample else 'fleet'})",
             "NICo.APIService(carbide)", "gRPC",
             "GetManagedHostNetworkConfig (periodic_config_fetcher)",
             f"전 DPU agent가 config_fetch_interval 주기로 desired config 폴링 — "
             f"ManagedHostNetworkConfigResponse{{tenant_interfaces, NSG, "
             f"routing_profile}} 수신 (host {len(host_ids)}대 수렴 시작)",
             payload={"rpc": "ManagedHostNetworkConfig",
                      "returns": ["tenant_interfaces(vlan·vni·prefix)",
                                  "network_security_groups",
                                  "interface_routing_profiles",
                                  "instance_metadata(FMDS)"]})
        # ③ 각 DPU: NVUE 렌더 → HBN 적용 (호스트당 1 이벤트 — 카운트 규약 유지)
        for hid in host_ids:
            host = self.hosts.get(hid)
            if not host:
                continue
            host.segment_id = seg.segment_id
            emit(f"DPU-Agent({host.dpu_ip})", f"HBN({host.tray_id})", "NVUE/HBN",
                 "FNN(L3 EVPN) NVUE 렌더 → HBN 적용 (폴링 수렴)",
                 f"vrf {dp_vrf}·EVPN VNI {l3vni} 바인딩 · pf0hpf_if ACL 체인 · "
                 f"언더레이 eBGP(p0/p1 unnumbered) · FMDS 링크넷 · "
                 f"호스트 {host.host_ip or '(미할당)'} → {dp_vrf}",
                 payload={
                     "template": "nvue_startup_fnn.conf (FNN · L3 EVPN)",
                     "nv_set": {
                         f"vrf {dp_vrf}": {
                             "evpn vni": l3vni,
                             "loopback": "테넌트 앵커 /32",
                             "router bgp": "ipv4-unicast + l2vpn-evpn"},
                         "interface pf0hpf_if": {
                             "vrf": dp_vrf,
                             "acl": ["p0000_deny_prefixes_ipv4",
                                     "p0004_security_policy_override_"
                                     "{v4,v6}_{ingress,egress}",
                                     f"p0010_{dp_vrf}_isolation_ipv4",
                                     "NSG rules"]},
                         "interface pf0dpu1_if": {
                             "ip": "169.254.169.253/30", "vrf": dp_vrf,
                             "용도": "FMDS 인스턴스 메타데이터 링크넷"},
                         "nve vxlan": {"source": "lo",
                                       "arp-nd-suppress": "on"},
                         "router bgp": {
                             "underlay": "p0_if/p1_if unnumbered eBGP",
                             "routeserver": "multihop-255 · update-source lo"
                                            " · l2vpn-evpn",
                             "route-map": "dpu_to_evpn / leak_to_underlay",
                             "community": "BYOIP_LEAK 65100:01/02"}},
                     "apply": "HBN 컨테이너: nv config apply → ifreload -a → "
                              "neighmgr 재시작 · forge-arp-accept 정책"},
                 host_id=hid)
        # ④ 적용 후 BGP 수렴 검증 (세그먼트당 1회 — 대표 요약)
        emit("DPU-Agent(fleet)", "HBN(vtysh)", "internal",
             "BGP summary 수렴 검증",
             f"ToR peers Established · routeserver l2vpn-evpn Established · "
             f"{dp_vrf} 테넌트 경로(EVPN type-5) 광고 확인 — 실패 시 "
             "unhealthy 보고 후 재수렴",
             payload={"checks": ["tor_peers", "route_server_peers",
                                 "tenant_routes_advertised"],
                      "vrf": dp_vrf, "result": "PASS"})
        return seg

    def attach_hosts(self, segment_id: str, host_ids: list,
                     purpose: str = "converged") -> NicoSegment:
        """기존 테넌트 VPC에 호스트 추가 — Managed K8s CP(CPU) 노드를
        Converged Network(VNI)로 GPU 워커와 묶는 경로.

        create_segment와 동일한 선언적 수렴: carbide desired state 갱신 →
        DPU agent 폴링 감지 → 호스트별 NVUE 렌더/HBN 적용 → BGP 수렴 검증."""
        seg = self.segments.get(segment_id)
        if not seg:
            raise HTTPException(404, f"nico: segment '{segment_id}' not found")
        dp = seg.vrf_dataplane or seg.vrf
        new_ids = [h for h in host_ids if h not in seg.host_ids]
        seg.host_ids.extend(new_ids)
        emit("NICo.APIService(carbide)", "NICo.NetworkStore", "internal",
             f"network-segment {segment_id} 호스트 추가 ({purpose})",
             f"desired state 갱신 — host {len(new_ids)}대 추가, "
             f"Converged VNI {seg.converged_vni} 바인딩 대상 (CPU CP 노드 ↔ "
             "GPU 워커 east-west)",
             payload={"segment_id": segment_id, "add_hosts": new_ids,
                      "converged_vni": seg.converged_vni, "purpose": purpose})
        for hid in new_ids:
            host = self.hosts.get(hid)
            if not host:
                continue
            host.segment_id = segment_id
            emit(f"DPU-Agent({host.dpu_ip})", f"HBN({host.tray_id})",
                 "NVUE/HBN",
                 "Converged Network 바인딩 — NVUE 렌더 → HBN 적용",
                 f"vrf {dp} · Converged VNI {seg.converged_vni} — "
                 f"{host.tray_id} pf0hpf_if ACL 체인 · "
                 f"호스트 {host.host_ip or '(미할당)'} → {dp} "
                 "(스토리지/K8s API east-west 경로)",
                 payload={"vrf": dp, "converged_vni": seg.converged_vni,
                          "template": "nvue_startup_fnn.conf",
                          "interface": "pf0hpf_if", "host_ip": host.host_ip},
                 host_id=hid)
        emit("DPU-Agent(fleet)", "HBN(vtysh)", "internal",
             "BGP summary 수렴 검증 (호스트 추가분)",
             f"{dp} — 추가 host {len(new_ids)}대 EVPN 경로 광고 확인",
             payload={"vrf": dp, "added": len(new_ids), "result": "PASS"})
        return seg

    def delete_segment(self, segment_id: str) -> NicoSegment:
        """VPC 해체 — 개통(create_segment)과 대칭인 4단계를 그대로 밟는다:
        carbide 기록 → agent 폴링 감지 → 호스트별 NVUE 재렌더/DHCP 회수 →
        EVPN withdraw 확인."""
        seg = self.segments.pop(segment_id, None)
        if not seg:
            raise HTTPException(404, f"nico: segment '{segment_id}' not found")
        dp = seg.vrf_dataplane or seg.vrf
        emit("NICo.APIService(carbide)", "NICo.NetworkStore", "internal",
             f"network segment {segment_id} 해체",
             f"desired state에서 VPC 제거 — DPU {len(seg.host_ids)}대의 agent가 "
             "폴링으로 감지해 테넌트 구성을 걷어낸다",
             payload={"segment_id": segment_id, "vrf": dp,
                      "l3vni": seg.l3vni, "hosts": len(seg.host_ids)})
        sample = self.hosts.get(seg.host_ids[0]) if seg.host_ids else None
        emit(f"DPU-Agent({sample.dpu_ip if sample else 'fleet'})",
             "NICo.APIService(carbide)", "gRPC",
             "GetManagedHostNetworkConfig (periodic_config_fetcher)",
             f"desired config에서 segment {segment_id} 소멸 감지 — "
             f"host {len(seg.host_ids)}대 해체 수렴 시작",
             payload={"rpc": "ManagedHostNetworkConfig",
                      "delta": f"segment {segment_id} removed"})
        for hid in seg.host_ids:
            host = self.hosts.get(hid)
            if not host:
                continue
            if host.segment_id == segment_id:
                host.segment_id = None
            if host.host_ip:
                emit(f"DPU-DHCP({host.dpu_ip})", host.host_id, "DHCP",
                     "RELEASE", f"lease {host.host_ip} 회수 — 테넌트 VRF "
                     "제거로 임대 무효화",
                     payload={"released": host.host_ip}, host_id=hid)
            emit(f"DPU-Agent({host.dpu_ip})", f"HBN({host.tray_id})",
                 "NVUE/HBN", "FNN NVUE 재렌더 — 테넌트 구성 제거",
                 f"vrf {dp} 삭제 · EVPN VNI {seg.l3vni} 언바인딩 · "
                 "pf0hpf_if ACL 체인·FMDS 링크넷 제거 → ifreload -a",
                 payload={"nv_unset": [f"vrf {dp}",
                                       f"nve vxlan vni {seg.l3vni}",
                                       f"interface pf0hpf_if acl "
                                       f"p0010_{dp}_isolation_ipv4",
                                       "interface pf0dpu1_if (FMDS 링크넷)"],
                          "apply": "nv config apply → ifreload -a"},
                 host_id=hid)
        emit("DPU-Agent(fleet)", "HBN(vtysh)", "internal",
             "EVPN withdraw 확인",
             f"{dp} type-5 경로 철회 전파 확인 — 언더레이·routeserver 광고 "
             "소멸, 타 테넌트 영향 없음",
             payload={"vrf": dp, "evpn": "type-5 withdraw", "result": "PASS"})
        seg.state = "deleted"
        return seg

    def list_segments(self) -> list:
        return list(self.segments.values())

    def get_sanitize_report(self, host_id: str) -> SanitizeReport:
        report = self.sanitize_reports.get(host_id)
        if not report:
            raise HTTPException(404, f"nico: no sanitize report for '{host_id}'")
        return report


# process-wide singleton (swapped for the real NICo endpoint in production)
FAKE_NICO = FakeNico()


# ---------------------------------------------------------------------------
# REST surface — same shapes NicoHttpAdapter speaks against the real thing
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/fake-nico", tags=["fake-nico"])


class _AllocateBody(BaseModel):
    host_id: str
    tenant_ref: str


class _ProvisionBody(BaseModel):
    image_ref: str


class _InjectBody(BaseModel):
    op: str                           # provision | sanitize


@router.get("/hosts", response_model=list[NicoHost])
def list_hosts() -> list[NicoHost]:
    # 활성 어댑터 경유 — http 모드면 실개통이 반영된 에뮬레이터 브리지를,
    # local 모드면 in-process FakeNico를 읽는다(자산 인벤토리 정합).
    from .lifecycle import get_adapter
    try:
        return get_adapter().list_hosts()
    except Exception:
        return FAKE_NICO.list_hosts()


@router.get("/hosts/{host_id}", response_model=NicoHost)
def get_host(host_id: str) -> NicoHost:
    from .lifecycle import get_adapter
    try:
        return get_adapter().get_host(host_id)
    except Exception:
        return FAKE_NICO.get_host(host_id)


@router.post("/hosts/{host_id}/reserve", response_model=NicoHost)
def reserve(host_id: str) -> NicoHost:
    return FAKE_NICO.reserve(host_id)


@router.post("/hosts/{host_id}/unreserve", response_model=NicoHost)
def unreserve(host_id: str) -> NicoHost:
    return FAKE_NICO.unreserve(host_id)


@router.post("/hosts/{host_id}/provision", response_model=NicoJob)
def provision(host_id: str, body: _ProvisionBody) -> NicoJob:
    return FAKE_NICO.provision(host_id, body.image_ref)


@router.post("/instances", response_model=NicoHost, status_code=201)
def allocate(body: _AllocateBody) -> NicoHost:
    return FAKE_NICO.allocate(body.host_id, body.tenant_ref)


@router.delete("/instances/{instance_id}", response_model=NicoJob)
def release(instance_id: str) -> NicoJob:
    return FAKE_NICO.release(instance_id)


@router.post("/hosts/{host_id}/abort-provision", response_model=NicoHost)
def abort_provision(host_id: str) -> NicoHost:
    return FAKE_NICO.abort_provision(host_id)


@router.post("/hosts/{host_id}/cordon", response_model=NicoHost)
def cordon(host_id: str, body: Optional[dict] = None) -> NicoHost:
    return FAKE_NICO.cordon(host_id, (body or {}).get("reason", ""))


@router.post("/hosts/{host_id}/sanitize", response_model=NicoJob)
def sanitize(host_id: str) -> NicoJob:
    return FAKE_NICO.sanitize(host_id)


@router.get("/hosts/{host_id}/sanitize-report", response_model=SanitizeReport)
def sanitize_report(host_id: str) -> SanitizeReport:
    return FAKE_NICO.get_sanitize_report(host_id)


@router.get("/jobs/{job_id}", response_model=NicoJob)
def get_job(job_id: str) -> NicoJob:
    return FAKE_NICO.get_job(job_id)


class _SegmentBody(BaseModel):
    tenant_ref: str
    vrf: str
    l3vni: int
    converged_vni: int
    host_ids: list[str] = Field(default_factory=list)
    allocation_id: Optional[str] = None


@router.get("/segments", response_model=list[NicoSegment])
def list_segments() -> list[NicoSegment]:
    return FAKE_NICO.list_segments()


@router.post("/segments", response_model=NicoSegment, status_code=201)
def create_segment(body: _SegmentBody) -> NicoSegment:
    return FAKE_NICO.create_segment(
        body.tenant_ref, body.vrf, body.l3vni, body.converged_vni,
        body.host_ids, body.allocation_id)


@router.delete("/segments/{segment_id}", response_model=NicoSegment)
def delete_segment(segment_id: str) -> NicoSegment:
    return FAKE_NICO.delete_segment(segment_id)


class _AttachBody(BaseModel):
    host_ids: list[str] = Field(default_factory=list)
    purpose: str = "converged"


@router.patch("/segments/{segment_id}/hosts", response_model=NicoSegment)
def attach_hosts(segment_id: str, body: _AttachBody) -> NicoSegment:
    """세그먼트에 호스트 추가 — Managed K8s CP 노드의 Converged 바인딩."""
    return FAKE_NICO.attach_hosts(segment_id, body.host_ids, body.purpose)


@router.post("/hosts/{host_id}/inject")
def inject(host_id: str, body: _InjectBody) -> dict:
    FAKE_NICO.inject_failure(host_id, body.op)
    return {"injected": body.op, "host_id": host_id}


# ---------------------------------------------------------------------------
# NICo read API surface — site / catalog / instances / leases / per-host
# drill-down (hardware, health, attestation). /nico 대시보드가 소비한다.
# ---------------------------------------------------------------------------
_FIRMWARE = {
    "vr-nvl72":    {"bios": "VR01.04", "bmc": "3.02", "cpld": "0x21",
                    "dpu_bfb": "bf-bundle-3.1.0", "gpu_vbios": "99.10.2F"},
    "gb300-nvl72": {"bios": "GB03.11", "bmc": "2.18", "cpld": "0x1c",
                    "dpu_bfb": "bf-bundle-2.9.1", "gpu_vbios": "98.00.4A"},
    "gb200-nvl72": {"bios": "GB02.27", "bmc": "2.14", "cpld": "0x1a",
                    "dpu_bfb": "bf-bundle-2.7.0", "gpu_vbios": "97.00.1A"},
}


def _mac_for_tray(tray_id: str) -> str:
    m = re.match(r"su-(\d+)-rack-(\d+)-tray-(\d+)", tray_id)
    if not m:
        return "0c:42:a1:00:00:00"
    su, rack, tray = (int(g) for g in m.groups())
    return f"0c:42:a1:{su:02x}:{rack:02x}:{tray:02x}"


@router.get("/site")
def site_info() -> dict:
    by_state = Counter(h.state.value for h in FAKE_NICO.hosts.values())
    services = [
        {"name": "API Service", "proto": "gRPC/mTLS", "state": "ok",
         "detail": "상태머신·PostgreSQL 단일 기록자"},
        {"name": "JSON API (REST)", "proto": "HTTPS/JWT", "state": "ok",
         "detail": "운영자·ISV 북바운드"},
        {"name": "DHCP", "proto": "UDP67→gRPC", "state": "ok",
         "detail": "요청을 gRPC로 변환, IP는 API Service가 관리"},
        {"name": "PXE", "proto": "HTTP", "state": "ok",
         "detail": "iPXE 스크립트·cloud-init·OS 이미지 서빙"},
        {"name": "Hardware Health", "proto": "Redfish", "state": "ok",
         "detail": "BMC 센서 폴링 → Prometheus export"},
        {"name": "SSH Console", "proto": "SSH", "state": "ok",
         "detail": "BMC 시리얼 콘솔 상시 연결 → Loki 스트림"},
        {"name": "DNS (authoritative)", "proto": "DNS", "state": "ok",
         "detail": "NICo 위임 존 응답"},
        {"name": "DNS (recursive/unbound)", "proto": "DNS", "state": "ok",
         "detail": "OOB 네트워크 리졸버"},
        {"name": "Site Agent", "proto": "Temporal", "state": "ok",
         "detail": "중앙 NICo REST 연동 워커"},
    ]
    # 사이트별 별개 NICo 인스턴스 — 가산·안산은 독립 클러스터(패브릭·컨트롤러)
    sites = []
    for name in sorted({h.site for h in FAKE_NICO.hosts.values() if h.site}):
        hs = [h for h in FAKE_NICO.hosts.values() if h.site == name]
        sites.append({
            "site_id": "nico-" + name.split()[-1].lower(), "name": name,
            "ha_nodes": 3,
            "hosts": len(hs),
            "hosts_by_state": dict(Counter(h.state.value for h in hs)),
            "instances": sum(1 for h in hs if h.instance_id),
            "racks": len({h.tray_id.rsplit("-tray-", 1)[0]
                          for h in hs if h.tray_id}),
        })
    return {"site": "aif-skt-01", "controller_version": "fake-nico/1.0",
            "ha_nodes": 3, "services": services,
            "sites": sites,
            "counts": {"hosts_by_state": dict(by_state),
                       "instances": sum(1 for h in FAKE_NICO.hosts.values()
                                        if h.instance_id),
                       "segments": len(FAKE_NICO.segments),
                       "jobs": len(FAKE_NICO.jobs)}}


@router.get("/instance-types")
def instance_types() -> list:
    out = []
    for key, bp in spec.BLUEPRINTS.items():
        hosts = [h for h in FAKE_NICO.hosts.values() if h.sku == key]
        out.append({
            "key": key, "model": bp.model,
            "gpus": bp.gpu_per_tray, "gpu_arch": bp.gpu_arch,
            "hbm_gb": bp.gpu_hbm_gb, "hbm_type": bp.gpu_hbm_type,
            "cpus": bp.cpu_per_tray, "cpu_arch": bp.cpu_arch,
            "cpu_cores": bp.cpu_cores,
            "dpu": bp.dpu_sku, "dpu_bw_gbps": bp.dpu_bw_gbps,
            "nvlink": bp.nvlink_gen,
            "hosts_total": len(hosts),
            "hosts_available": sum(1 for h in hosts
                                   if h.state == NicoHostState.pool_ready)})
    return out


@router.get("/instances")
def list_instances() -> list:
    return [{"instance_id": h.instance_id, "host_id": h.host_id,
             "tray_id": h.tray_id, "tenant_ref": h.tenant_ref,
             "host_ip": h.host_ip, "instance_type": h.sku,
             "segment_id": h.segment_id}
            for h in FAKE_NICO.hosts.values() if h.instance_id]


@router.get("/jobs")
def list_jobs(limit: int = 50) -> list:
    jobs = sorted(FAKE_NICO.jobs.values(),
                  key=lambda j: int(j.job_id.split("-")[1]), reverse=True)
    return jobs[:min(limit, 200)]


@router.get("/dhcp/leases")
def dhcp_leases() -> list:
    return [{"host_id": h.host_id, "tray_id": h.tray_id, "ip": h.host_ip,
             "mac": _mac_for_tray(h.tray_id), "lease_s": 86400,
             "dhcp_server": h.dpu_ip}
            for h in FAKE_NICO.hosts.values() if h.host_ip]


@router.get("/hosts/{host_id}/hardware")
def host_hardware(host_id: str) -> dict:
    host = FAKE_NICO.get_host(host_id)
    tray = STORE.trays.get(host.tray_id)
    if not tray:
        raise HTTPException(404, f"nico: no hardware map for '{host_id}'")
    gpus = [STORE.gpus[g] for g in tray.gpu_ids if g in STORE.gpus]
    cpus = [STORE.cpus[c] for c in tray.cpu_ids if c in STORE.cpus]
    dpu = STORE.dpus.get(tray.dpu_id) if tray.dpu_id else None
    return {"host_id": host_id, "sku": host.sku,
            "gpus": [{"id": g.id, "arch": g.arch, "hbm_gb": g.hbm_gb,
                      "hbm_type": g.hbm_type, "dies": g.dies} for g in gpus],
            "cpus": [{"id": c.id, "arch": c.arch, "cores": c.cores,
                      "mem_tb": c.mem_tb} for c in cpus],
            "dpu": ({"id": dpu.id, "sku": dpu.sku,
                     "bw_gbps": dpu.bandwidth_gbps,
                     "mode": dpu.mode.value} if dpu else None),
            "connectx_supernics": tray.connectx_supernics,
            "firmware": _FIRMWARE.get(host.sku, {}),
            "bmc_ip": host.bmc_ip, "dpu_ip": host.dpu_ip,
            "mac": _mac_for_tray(host.tray_id)}


def _health_payload(host: NicoHost) -> dict:
    """BMC(Redfish) 센서 뷰 — 활성 트레이는 에뮬레이터 텔레메트리를 반영."""
    from .tray_emu import EMULATOR
    if host.sku == "cpu-epyc":            # CPU 노드 — GPU 센서 없음(공랭)
        active = host.state == NicoHostState.allocated
        return {"host_id": host.host_id, "tray_id": host.tray_id,
                "tenant_ref": host.tenant_ref, "instance_id": host.instance_id,
                "host_ip": host.host_ip, "nico_state": host.state.value,
                "state": "ok" if active else "standby",
                "power_w": 780 if active else 180,
                "gpu_temp_c": [], "cpu_temp_c": 58.0 if active else 34.0,
                "coolant_supply_c": None, "coolant_return_c": None,
                "leak_detected": False, "pump": "n/a",
                "psu": [{"id": i, "status": "ok"} for i in range(1, 3)]}
    rt = EMULATOR.trays.get(host.tray_id)
    if rt and rt.tenant_id:
        gpu_temps = [g.temp_c for g in rt.gpus]
        power = rt.power_w
        coolant_in = round(43 + max(gpu_temps, default=40) * 0.02, 1)
        state = ("warning" if any(g.state in ("fault", "throttled")
                                  for g in rt.gpus) else "ok")
    else:
        gpu_temps = [round(32 + i * 0.7, 1) for i in range(4)]
        power, coolant_in, state = 420, 43.0, "standby"
    return {"host_id": host.host_id, "tray_id": host.tray_id,
            "tenant_ref": host.tenant_ref, "instance_id": host.instance_id,
            "host_ip": host.host_ip, "nico_state": host.state.value,
            "state": state, "power_w": power,
            "gpu_temp_c": gpu_temps,
            "coolant_supply_c": coolant_in,
            "coolant_return_c": round(coolant_in + power / 1400, 1),
            "leak_detected": False, "pump": "ok",
            "psu": [{"id": i, "status": "ok"} for i in range(1, 5)]}


@router.get("/health")
def bulk_health(tenant_ref: Optional[str] = None,
                limit: int = 600) -> list:
    """벌크 BMC 센서 — Hardware Health 서비스의 사이트 집계 뷰.

    운영 포털의 테넌트 Observability가 NICo REST를 직접 소비한다."""
    hosts = [h for h in FAKE_NICO.hosts.values()
             if tenant_ref is None or h.tenant_ref == tenant_ref]
    return [_health_payload(h) for h in
            sorted(hosts, key=lambda h: h.host_id)[:min(limit, 3000)]]


@router.get("/hosts/{host_id}/health")
def host_health(host_id: str) -> dict:
    return _health_payload(FAKE_NICO.get_host(host_id))


@router.get("/hosts/{host_id}/attestation")
def host_attestation(host_id: str) -> dict:
    host = FAKE_NICO.get_host(host_id)
    def pcr(n: int) -> str:
        return hashlib.sha256(f"{host_id}:pcr{n}".encode()).hexdigest()[:32]
    return {"host_id": host_id, "attested": host.attested,
            "measured_boot": "passed" if host.attested else "pending",
            "tpm": {"PCR0": pcr(0), "PCR2": pcr(2),
                    "PCR4": pcr(4), "PCR7": pcr(7)},
            "policy": "firmware-baseline + secure-boot chain"}


@router.get("/sanitize-reports")
def sanitize_reports() -> list:
    return list(FAKE_NICO.sanitize_reports.values())


# ---------------------------------------------------------------------------
# Demo/test-only endpoints (the real NICo has none of these) — they exist so
# the /flow verification console can stage reconcile scenarios and exercise
# the job-polling path from a browser.
# ---------------------------------------------------------------------------
class _GhostBody(BaseModel):
    host_id: str
    sku: str = "vr-nvl72"


class _StateBody(BaseModel):
    state: NicoHostState


class _ConfigBody(BaseModel):
    job_latency: int


@router.post("/hosts/ghost", response_model=NicoHost, status_code=201)
def create_ghost(body: _GhostBody) -> NicoHost:
    """Stage a GHOST: a host NICo knows but the control plane does not."""
    return FAKE_NICO.add_ghost(body.host_id, body.sku)


@router.delete("/hosts/{host_id}")
def delete_host(host_id: str) -> dict:
    """Stage an ORPHAN: make a mirrored host vanish from NICo."""
    if host_id not in FAKE_NICO.hosts:
        raise HTTPException(404, f"nico: host '{host_id}' not found")
    FAKE_NICO.hosts.pop(host_id)
    return {"deleted": host_id}


@router.patch("/hosts/{host_id}/state", response_model=NicoHost)
def force_state(host_id: str, body: _StateBody) -> NicoHost:
    """Stage a STATE_MISMATCH: flip host state behind the control plane's back."""
    host = FAKE_NICO.get_host(host_id)
    host.state = body.state
    return host


@router.patch("/config")
def set_config(body: _ConfigBody) -> dict:
    """Set job latency (polls a job stays running) to demo poll-until-converged."""
    FAKE_NICO.job_latency = max(0, body.job_latency)
    return {"job_latency": FAKE_NICO.job_latency}
