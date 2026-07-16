"""Managed K8s 운영 API — 콘솔 10개 서브뷰의 Live 데이터 표면 (R3).

lifecycle.py의 설치 saga(K8S_INSTALL_STAGES)가 축적한 상태를 읽어
overview·installs·nodes·acceptance·nodepools·addons·services·kubeconfig·
upgrades·CVE·health-events·storage·metrics를 노출한다. 설치/업그레이드
전이는 기존 trace emit 패턴을 재사용해 /orders/{id}/flow·/trace에 나타난다.

락 순서 주의: EMULATOR(_lock)와 STORE(lock)를 함께 쓸 때는 반드시
EMULATOR 조회를 먼저 끝낸 뒤 STORE.lock을 잡는다 (sync_from_store가
EMULATOR._lock → STORE.lock 순서로 중첩 획득하므로 역순은 교착).
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .lifecycle import (
    K8S_CP_NODES_PER_CLUSTER,
    K8S_CP_IMAGE,
    K8S_INSTALL_STAGES,
    K8S_MANAGED_ADDONS,
    K8S_NKD_VERSION,
    K8S_OPTIONAL_ADDONS,
    K8S_SUPPORTED_VERSIONS,
    _now,
)
from .models import (
    K8sCluster,
    K8sKubeconfig,
    K8sUpgrade,
    LifecycleEvent,
    NodeLifecycleState as NS,
)
from .store import STORE
from .trace import emit
from .tray_emu import EMULATOR
from .vast_fake import FAKE_VAST

router = APIRouter(prefix="/api/v1", tags=["managed-k8s"])

K8S_WORKER_IMAGE = "dgx-os-7.0"        # GPU 워커 OS (DGX OS — NKD 워커 경로)

# RBAC 템플릿 — kubeconfig 발급 시 role → ClusterRole 매핑 (OIDC 그룹 클레임)
K8S_RBAC_TEMPLATES = [
    {"role": "admin", "cluster_role": "cluster-admin", "scope": "cluster",
     "audience": "테넌트 플랫폼 운영자",
     "detail": "전체 리소스 관리 — 노드·네임스페이스·CRD 포함"},
    {"role": "edit", "cluster_role": "edit", "scope": "namespace",
     "audience": "ML 엔지니어",
     "detail": "워크로드 배포·수정 — RBAC·ResourceQuota 변경 불가"},
    {"role": "view", "cluster_role": "view", "scope": "namespace",
     "audience": "감사·옵저버", "detail": "읽기 전용"},
    {"role": "batch-operator", "cluster_role": "edit + kueue-batch-admin",
     "scope": "namespace", "audience": "배치 잡 운영자",
     "detail": "Kueue 큐·잡 우선순위 관리 (GPU 쿼터 내)"},
]

# 정적 큐레이션 CVE 목록 — 업그레이드·패치 탭이 소비 (영향/패치 버전 포함)
K8S_CVES = [
    {"cve_id": "CVE-2026-32871", "component": "containerd",
     "severity": "critical", "cvss": 9.1,
     "summary": "CRI 이미지 언팩 경로 탈출 — 악성 이미지 pull 시 호스트 "
                "파일시스템 쓰기 가능",
     "affected_versions": "containerd < 2.0.4 (K8s v1.32.4 · NKD 25.06 번들)",
     "affects_clusters": ["v1.32.4"],
     "patched_in": "containerd 2.0.5 — K8s v1.33.2 / NKD 25.06.2",
     "action": "v1.33.2 업그레이드 권고", "published": "2026-05-18"},
    {"cve_id": "CVE-2025-1974", "component": "ingress-nginx",
     "severity": "critical", "cvss": 9.8,
     "summary": "IngressNightmare — admission webhook 경유 임의 설정 주입 "
                "→ RCE",
     "affected_versions": "ingress-nginx < 1.12.1",
     "affects_clusters": ["v1.32.4", "v1.33.2"],
     "patched_in": "ingress-nginx 1.12.1 (선택 애드온 채널)",
     "action": "admission webhook 외부 노출 차단 + 애드온 갱신",
     "published": "2025-03-24"},
    {"cve_id": "CVE-2026-0192", "component": "gpu-operator",
     "severity": "high", "cvss": 7.8,
     "summary": "driver-daemonset 권한 상승 — hostPath 마운트 경합으로 "
                "컨테이너 탈출",
     "affected_versions": "gpu-operator < 25.3.0",
     "affects_clusters": [],
     "patched_in": "gpu-operator 25.3.0 (NKD 25.06 번들 — 기적용)",
     "action": "조치 불필요 (번들 버전 이미 패치)", "published": "2026-01-29"},
    {"cve_id": "CVE-2026-1288", "component": "kube-apiserver",
     "severity": "medium", "cvss": 5.3,
     "summary": "aggregated API 프록시 헤더 스머글링 — 감사 로그 우회",
     "affected_versions": "K8s ≤ v1.32.4",
     "affects_clusters": ["v1.32.4"],
     "patched_in": "K8s v1.33.2",
     "action": "v1.33.2 업그레이드 권고", "published": "2026-04-07"},
]


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------
def _cluster_or_404(cluster_id: str) -> K8sCluster:
    cluster = STORE.k8s_clusters.get(cluster_id)
    if not cluster:
        raise HTTPException(404, f"k8s cluster '{cluster_id}' not found")
    return cluster


def _pub_state(cluster: K8sCluster) -> str:
    """내부 상태 → 콘솔 계약 상태 (running→active, 나머지는 그대로)."""
    return {"running": "active"}.get(cluster.state, cluster.state)


def _worker_tray_map(cluster: K8sCluster) -> dict:
    """워커 tray_id → NodeInstance (호출 전제: STORE.lock 미보유여도 안전)."""
    with STORE.lock:
        out = {}
        for nid in cluster.worker_node_ids:
            node = STORE.node_instances.get(nid)
            if node:
                out[node.tray_id] = node
        return out


def _ai_infra_faults(limit: int = 100) -> list:
    """AI Infra(:9100) 전 도메인 장애 피드 best-effort — 실체인 기동 시만."""
    if not os.environ.get("NOCP_NICO_URL"):
        return []
    try:
        import httpx

        from .integration import AI_INFRA_BASE
        r = httpx.get(f"{AI_INFRA_BASE}/emulator/v1/faults",
                      params={"limit": limit}, timeout=1.2)
        if r.status_code != 200:
            return []
        return r.json().get("recent", [])
    except Exception:                              # noqa: BLE001 — 표면화만
        return []


_AI_KIND = {"gpu": "gpu-xid", "cooling": "thermal", "dlc": "thermal",
            "fabric": "link", "ufm": "link", "netq": "link", "dpu": "dpu"}


def _scan_health(cluster: K8sCluster, trays: dict) -> tuple[list, set]:
    """헬스 이벤트 수집 — 로컬 XID 에피소드 + AI Infra obs/faults 병합.

    반환: (이벤트 목록[최신순], 열린 GPU 장애가 있는 워커 tray 집합)."""
    events: list[dict] = []
    open_gpu: set[str] = set()
    # ① 로컬 tray 에뮬레이터 XID 에피소드 (주입·랜덤 공통 경로)
    for rec in EMULATOR.faults(limit=200)["recent"]:
        tray = rec.get("tray_id")
        if tray not in trays:
            continue
        open_ = not rec.get("resolved")
        if open_:
            open_gpu.add(tray)
        events.append({
            "ts": rec.get("at") or rec.get("started_at"),
            "node": tray,
            "severity": "critical" if open_ else "info",
            "kind": "gpu-xid",
            "message": rec.get("detail")
            or f"XID {rec.get('xid')} — GPU{rec.get('gpu')}"
               f" ({'open' if open_ else 'resolved'})",
            "action": "quarantined" if open_ else "none",
        })
    # ② AI Infra 물리 트윈 피드 (host_id→tray 매핑 가능한 항목만)
    for f in _ai_infra_faults():
        kind = _AI_KIND.get(f.get("kind") or "")
        if not kind:
            continue
        raw = str(f.get("tray_id") or "")
        tray = raw[4:].rsplit("-g", 1)[0] if raw.startswith("GPU-") else raw
        if tray not in trays:
            continue
        open_ = not f.get("resolved")
        if open_ and kind == "gpu-xid":
            open_gpu.add(tray)
        events.append({
            "ts": f.get("at"), "node": tray,
            "severity": f.get("severity")
            or ("critical" if open_ else "info"),
            "kind": kind, "message": f.get("detail", ""),
            "action": (("quarantined" if kind == "gpu-xid"
                        else "remediating") if open_ else "none"),
        })
    events.sort(key=lambda e: e["ts"] or "", reverse=True)
    return events, open_gpu


def _hot_spare_for(node) -> Optional[dict]:
    """동일 blueprint의 pool_ready 트레이 — 격리 노드 대체(hot-spare) 제안."""
    spare = next(
        (n for n in sorted(STORE.node_instances.values(), key=lambda x: x.id)
         if n.state == NS.pool_ready
         and n.blueprint_key == node.blueprint_key), None)
    if not spare:
        return None
    return {"node": spare.tray_id, "host_id": spare.nico_host_id,
            "action": "replace-node — hot-spare 트레이로 교체 제안"}


# ---------------------------------------------------------------------------
# Overview / Installs
# ---------------------------------------------------------------------------
@router.get("/k8s/overview")
def k8s_overview() -> dict:
    """Managed K8s 집계 — 콘솔 Overview KPI가 소비."""
    # EMULATOR 조회를 먼저 (락 순서 — 모듈 docstring 참조)
    recent = EMULATOR.faults(limit=200)["recent"]
    open_trays = {r.get("tray_id") for r in recent if not r.get("resolved")}
    with STORE.lock:
        live = [c for c in STORE.k8s_clusters.values()
                if c.state != "deleted"]
        by_state = {"installing": 0, "active": 0, "failed": 0, "deleting": 0}
        tenants: dict[str, int] = {}
        health_open = 0
        for c in live:
            st = _pub_state(c)
            if st in by_state:
                by_state[st] += 1
            tenants[c.tenant_id] = tenants.get(c.tenant_id, 0) + 1
            if c.state in ("running", "installing"):
                for nid in c.worker_node_ids:
                    node = STORE.node_instances.get(nid)
                    if node and node.tray_id in open_trays:
                        health_open += 1
        return {
            "clusters_total": len(live),
            "by_state": by_state,
            "installs_active": sum(1 for i in STORE.k8s_installs.values()
                                   if i.state == "running"),
            "upgrades_active": sum(1 for u in STORE.k8s_upgrades.values()
                                   if u.state == "running"),
            "health_events_open": health_open,
            "versions": {"supported": K8S_SUPPORTED_VERSIONS,
                         "nkd": K8S_NKD_VERSION},
            "tenants": [{"tenant_id": t, "clusters": n}
                        for t, n in sorted(tenants.items())],
        }


@router.get("/k8s/installs")
def list_k8s_installs(tenant_id: Optional[str] = None) -> list:
    """설치 saga 기록 — Day-1(주문 옵션)·Day-2(installs) 공통 보드."""
    with STORE.lock:
        installs = list(STORE.k8s_installs.values())
    if tenant_id:
        installs = [i for i in installs if i.tenant_id == tenant_id]
    return sorted(installs, key=lambda i: i.install_id, reverse=True)


# ---------------------------------------------------------------------------
# 클러스터 상세 표면 — nodes / acceptance / nodepools
# ---------------------------------------------------------------------------
@router.get("/k8s/clusters/{cluster_id}/nodes")
def cluster_nodes(cluster_id: str) -> list:
    """CP·GPU 워커 노드 목록 — quarantine(R6)·업그레이드 drain 상태 반영."""
    with STORE.lock:
        cluster = _cluster_or_404(cluster_id)
    trays = _worker_tray_map(cluster)
    _, open_gpu = _scan_health(cluster, trays)
    with STORE.lock:
        cluster.quarantined_nodes = sorted(open_gpu)
        bootstrap_done = any(h["name"] == "nkd-bootstrap"
                             and h["status"] == "done"
                             for h in cluster.stage_history)
        base_ready = cluster.state == "running" or bootstrap_done
        draining = {u.current_node
                    for u in STORE.k8s_upgrades.values()
                    if u.cluster_id == cluster_id and u.state == "running"
                    and u.current_node}
        out = []
        for cn_id in cluster.cp_node_ids:
            cn = STORE.cpu_nodes.get(cn_id)
            state = ("Draining" if cn_id in draining
                     else "Ready" if base_ready else "NotReady")
            out.append({
                "name": cn_id, "role": "cp",
                "host_id": cn.nico_host_id if cn else None,
                "state": state,
                "version": cluster.node_versions.get(cn_id, cluster.version),
                "ip": cn.host_ip if cn else "",
                "gpu_count": 0,
                "conditions": ["control-plane",
                               f"etcd quorum {K8S_CP_NODES_PER_CLUSTER}"],
            })
        for i, nid in enumerate(cluster.worker_node_ids):
            node = STORE.node_instances.get(nid)
            if not node:
                continue
            tray = STORE.trays.get(node.tray_id)
            name = node.tray_id
            if name in open_gpu:
                state = "Quarantined"
                conditions = ["XID fault open — NVSentinel cordon·격리",
                              "hot-spare 교체 제안 (health-events 참조)"]
            elif name in draining or node.state == NS.draining:
                state = "Draining"
                conditions = ["cordon — 업그레이드/회수 진행"]
            elif base_ready:
                state = "Ready"
                conditions = ["nvidia.com/gpu.present=true",
                              f"rack {node.rack_id}"]
            else:
                state = "NotReady"
                conditions = ["kubeadm join 대기 (설치 진행 중)"]
            out.append({
                "name": name, "role": "gpu-worker",
                "host_id": node.nico_host_id, "state": state,
                "version": cluster.node_versions.get(name, cluster.version),
                "ip": f"10.250.2.{i + 1}",
                "gpu_count": len(tray.gpu_ids) if tray else 4,
                "conditions": conditions,
            })
        return out


@router.get("/k8s/clusters/{cluster_id}/acceptance")
def cluster_acceptance(cluster_id: str) -> dict:
    """Acceptance/burn-in 리포트 — 설치 saga 6단계가 저장한 결과."""
    with STORE.lock:
        cluster = _cluster_or_404(cluster_id)
        if cluster.acceptance:
            return cluster.acceptance
        if cluster.state == "installing":
            return {"status": "running", "report_ts": None,
                    "checks": [{"name": n, "status": "pending",
                                "detail": "burn-in 대기", "value": None}
                               for n in ("node-ready", "nccl-allreduce",
                                         "dcgm-diag", "storage-mount")]}
        if cluster.state == "failed":
            return {"status": "fail", "report_ts": None, "checks": []}
        return {"status": "n/a", "report_ts": None, "checks": []}


@router.get("/k8s/clusters/{cluster_id}/nodepools")
def cluster_nodepools(cluster_id: str) -> list:
    """노드풀 — CP 풀(CPU·kubeadm HA) + GPU 워커 풀(NVL72 트레이)."""
    with STORE.lock:
        cluster = _cluster_or_404(cluster_id)
        cn = next((STORE.cpu_nodes[i] for i in cluster.cp_node_ids
                   if i in STORE.cpu_nodes), None)
        machine_cp = (f"{cn.cpu_arch} {cn.cores}c/{cn.mem_tb}TB"
                      if cn else "cpu-epyc")
        rack = None
        for nid in cluster.worker_node_ids:
            node = STORE.node_instances.get(nid)
            if node and node.rack_id in STORE.racks:
                rack = STORE.racks[node.rack_id]
                break
        machine_gpu = (f"{rack.model} 트레이 (4× {rack.gpu_arch})"
                       if rack else "NVL72 compute tray")
        return [
            {"name": "control-plane", "role": "cp",
             "count": len(cluster.cp_node_ids), "machine": machine_cp,
             "image": K8S_CP_IMAGE, "version": cluster.version},
            {"name": "gpu-workers", "role": "gpu-worker",
             "count": len(cluster.worker_node_ids), "machine": machine_gpu,
             "image": K8S_WORKER_IMAGE, "version": cluster.version},
        ]


# ---------------------------------------------------------------------------
# 애드온
# ---------------------------------------------------------------------------
class _AddonBody(BaseModel):
    name: str


def _addon_view(a: dict) -> dict:
    return {"name": a["name"], "version": a["version"],
            "role": a.get("role", ""),
            "status": {"running": "installed"}.get(
                a.get("status", ""), a.get("status", "installed")),
            "channel": a.get("channel", "nkd-bundle")}


@router.get("/k8s/clusters/{cluster_id}/addons")
def cluster_addons(cluster_id: str) -> list:
    """애드온 상태 — 설치 saga가 installed로 전이, 미설치분은 pending."""
    with STORE.lock:
        cluster = _cluster_or_404(cluster_id)
        out = [_addon_view(a) for a in cluster.addons]
        if cluster.state == "installing":     # 카탈로그 기준 대기분 표시
            have = {a["name"] for a in cluster.addons}
            out += [{**_addon_view(a), "status": "pending"}
                    for a in K8S_MANAGED_ADDONS if a["name"] not in have]
        return out


@router.post("/k8s/clusters/{cluster_id}/addons", status_code=201)
def add_cluster_addon(cluster_id: str, body: _AddonBody) -> list:
    """선택 애드온 추가 — K8S_OPTIONAL_ADDONS 카탈로그 기반."""
    with STORE.lock:
        cluster = _cluster_or_404(cluster_id)
        if cluster.state != "running":
            raise HTTPException(409, f"cluster '{cluster_id}'는 "
                                     f"{cluster.state} 상태 — 애드온 추가 불가")
        addon = next((a for a in K8S_OPTIONAL_ADDONS
                      if a["name"] == body.name), None)
        if not addon:
            raise HTTPException(404, f"선택 애드온 '{body.name}' 없음 — "
                                     "카탈로그: "
                                     f"{[a['name'] for a in K8S_OPTIONAL_ADDONS]}")
        if any(a["name"] == body.name for a in cluster.addons):
            raise HTTPException(409, f"애드온 '{body.name}' 이미 설치됨")
        emit("NeoCloudOS.M6(K8sMgr)", f"K8s({cluster.id})", "K8s",
             f"addon install — {addon['name']} {addon['version']} (선택)",
             addon["role"], payload={"cluster": cluster.id, **addon},
             order_id=cluster.order_id)
        cluster.addons.append({**addon, "status": "running",
                               "channel": "optional"})
        cluster.history.append(LifecycleEvent(
            state=cluster.state,
            detail=f"addon 추가 — {addon['name']} {addon['version']}",
            at=_now()))
        return [_addon_view(a) for a in cluster.addons]


# ---------------------------------------------------------------------------
# 네트워킹·외부노출 (kube-vip VIP + in-process fake LB/DNS)
# ---------------------------------------------------------------------------
@router.get("/k8s/clusters/{cluster_id}/services")
def cluster_services(cluster_id: str) -> dict:
    with STORE.lock:
        cluster = _cluster_or_404(cluster_id)
        seq = int(cluster.id.rsplit("-", 1)[1]) if "-" in cluster.id else 0
        active = "active" if cluster.state == "running" else "pending"
        return {
            "api_vip": cluster.api_vip,
            "entries": [
                {"name": "kube-apiserver", "type": "LB",
                 "vip_or_host": cluster.api_vip, "ports": [6443],
                 "target": f"kube-vip — CP {K8S_CP_NODES_PER_CLUSTER}노드 "
                           "L2/ARP (Converged Network)",
                 "state": active},
                {"name": f"{cluster.name}.k8s.neocloud.skt", "type": "DNS",
                 "vip_or_host": f"{cluster.name}.k8s.neocloud.skt",
                 "ports": [], "target": cluster.api_vip,
                 "state": active},
                {"name": "ingress-default", "type": "Ingress",
                 "vip_or_host": f"10.250.3.{(seq % 200) + 10}",
                 "ports": [80, 443],
                 "target": "ingress-nginx-controller (fake F5 LB VIP)",
                 "state": active},
            ],
        }


# ---------------------------------------------------------------------------
# 접근관리 — kubeconfig 발급/폐기 + RBAC 템플릿
# ---------------------------------------------------------------------------
class _KubeconfigBody(BaseModel):
    role: str = "edit"
    ttl_h: int = 12


def _kubeconfig_yaml(cluster: K8sCluster, role: str, serial: str) -> str:
    """모의 kubeconfig — OIDC exec-plugin 방식 (가짜 PKI CA 시리얼 포함)."""
    return (
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        f"- name: {cluster.name}\n"
        "  cluster:\n"
        f"    server: https://{cluster.api_vip}:6443\n"
        f"    certificate-authority-data: <mock-ca-bundle serial={serial}>\n"
        "contexts:\n"
        f"- name: {cluster.name}-{role}\n"
        f"  context: {{cluster: {cluster.name}, user: oidc-{role}}}\n"
        f"current-context: {cluster.name}-{role}\n"
        "users:\n"
        f"- name: oidc-{role}\n"
        "  user:\n"
        "    exec:\n"
        "      apiVersion: client.authentication.k8s.io/v1\n"
        "      command: kubectl\n"
        "      args: [oidc-login, get-token, "
        f"--oidc-issuer-url={cluster.oidc_issuer}]\n")


@router.get("/k8s/clusters/{cluster_id}/kubeconfigs")
def list_kubeconfigs(cluster_id: str) -> list:
    with STORE.lock:
        _cluster_or_404(cluster_id)
        return sorted((k for k in STORE.k8s_kubeconfigs.values()
                       if k.cluster_id == cluster_id),
                      key=lambda k: k.kubeconfig_id, reverse=True)


@router.post("/k8s/clusters/{cluster_id}/kubeconfigs", status_code=201,
             response_model=K8sKubeconfig)
def issue_kubeconfig(cluster_id: str, body: _KubeconfigBody) -> K8sKubeconfig:
    """kubeconfig 발급 — 가짜 PKI 시리얼 + TTL (OIDC exec-plugin 모의)."""
    from datetime import datetime, timedelta, timezone
    with STORE.lock:
        cluster = _cluster_or_404(cluster_id)
        if cluster.state != "running":
            raise HTTPException(409, f"cluster '{cluster_id}'는 "
                                     f"{cluster.state} 상태 — 발급 불가")
        roles = {t["role"] for t in K8S_RBAC_TEMPLATES}
        if body.role not in roles:
            raise HTTPException(422, f"role '{body.role}' — 지원 템플릿: "
                                     f"{sorted(roles)}")
        ttl = max(1, min(body.ttl_h, 720))
        kid = f"kc-{STORE.next_seq('kubeconfig')}"
        serial = secrets.token_hex(8)
        now = datetime.now(timezone.utc)
        kc = K8sKubeconfig(
            kubeconfig_id=kid, cluster_id=cluster_id,
            tenant_id=cluster.tenant_id, serial=serial, role=body.role,
            ttl_h=ttl, issued_at=now.isoformat(),
            expires_at=(now + timedelta(hours=ttl)).isoformat(),
            kubeconfig_yaml=_kubeconfig_yaml(cluster, body.role, serial))
        STORE.k8s_kubeconfigs[kid] = kc
        emit("NeoCloudOS.M6(K8sMgr)", "SharedServices.IAM", "internal",
             f"kubeconfig 발급 — {kid}",
             f"{cluster.name} role={body.role} TTL {ttl}h · PKI 시리얼 "
             f"{serial} (OIDC exec-plugin)",
             payload={"cluster": cluster_id, "kubeconfig_id": kid,
                      "role": body.role, "ttl_h": ttl, "serial": serial},
             order_id=cluster.order_id)
        return kc


@router.delete("/k8s/clusters/{cluster_id}/kubeconfigs/{kubeconfig_id}")
def revoke_kubeconfig(cluster_id: str, kubeconfig_id: str) -> K8sKubeconfig:
    """kubeconfig 폐기(revoke) — 이력은 보존, 시리얼은 CRL 등재로 간주."""
    with STORE.lock:
        _cluster_or_404(cluster_id)
        kc = STORE.k8s_kubeconfigs.get(kubeconfig_id)
        if not kc or kc.cluster_id != cluster_id:
            raise HTTPException(404,
                                f"kubeconfig '{kubeconfig_id}' not found")
        if kc.revoked:
            raise HTTPException(409, f"'{kubeconfig_id}' 이미 폐기됨")
        kc.revoked = True
        kc.revoked_at = _now()
        emit("NeoCloudOS.M6(K8sMgr)", "SharedServices.IAM", "internal",
             f"kubeconfig 폐기 — {kubeconfig_id}",
             f"PKI 시리얼 {kc.serial} CRL 등재 (즉시 실효)",
             payload={"cluster": cluster_id, "kubeconfig_id": kubeconfig_id,
                      "serial": kc.serial})
        return kc


@router.get("/k8s/rbac-templates")
def rbac_templates() -> list:
    return K8S_RBAC_TEMPLATES


# ---------------------------------------------------------------------------
# 업그레이드 saga — 노드별 cordon→drain→upgrade→uncordon
# ---------------------------------------------------------------------------
class _UpgradeBody(BaseModel):
    target_version: str


def _upgrade_step_delay() -> float:
    """노드 스텝당 지연(s) — NOCP_K8S_UPGRADE_DELAY로 재정의. 기본:
    실체인 기동 0.15s(콘솔 관찰용), 인프로세스 0(테스트 즉시 완료)."""
    raw = os.environ.get("NOCP_K8S_UPGRADE_DELAY")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 0.0
    return 0.15 if os.environ.get("NOCP_NICO_URL") else 0.0


def _run_upgrade(up: K8sUpgrade, node_names: list) -> None:
    """업그레이드 saga 본체 (백그라운드 스레드) — 노드 단위 롤링."""
    delay = _upgrade_step_delay()
    for name in node_names:
        with STORE.lock:
            up.current_node = name
        for step in ("cordon", "drain", "upgrade", "uncordon"):
            if delay > 0:
                time.sleep(delay)
            with STORE.lock:
                up.events.append({"ts": _now(), "node": name, "step": step})
                cluster = STORE.k8s_clusters.get(up.cluster_id)
                emit("NeoCloudOS.M6(K8sMgr)", f"K8s({up.cluster_id})", "K8s",
                     f"upgrade:{step} — {name}",
                     f"{name} {step} ({up.from_version} → "
                     f"{up.target_version})",
                     payload={"cluster": up.cluster_id, "node": name,
                              "step": step, "target": up.target_version},
                     order_id=cluster.order_id if cluster else None)
        with STORE.lock:
            up.node_progress["done"] = up.node_progress.get("done", 0) + 1
            cluster = STORE.k8s_clusters.get(up.cluster_id)
            if cluster:
                cluster.node_versions[name] = up.target_version
    with STORE.lock:
        up.state = "succeeded"
        up.current_node = None
        cluster = STORE.k8s_clusters.get(up.cluster_id)
        if cluster:
            cluster.version = up.target_version
            cluster.history.append(LifecycleEvent(
                state=cluster.state,
                detail=f"upgraded {up.from_version} → {up.target_version} "
                       f"(노드 {up.node_progress.get('total', 0)}대 롤링)",
                at=_now()))
            emit("NeoCloudOS.M6(K8sMgr)", f"K8s({cluster.id})", "K8s",
                 f"upgrade complete — {up.target_version}",
                 f"{cluster.name} 전 노드 롤링 완료 · CP→워커 순",
                 payload={"cluster": cluster.id, "upgrade_id": up.upgrade_id,
                          "version": up.target_version},
                 order_id=cluster.order_id)


@router.post("/k8s/clusters/{cluster_id}/upgrades", status_code=201,
             response_model=K8sUpgrade)
def start_upgrade(cluster_id: str, body: _UpgradeBody) -> K8sUpgrade:
    """업그레이드 시작 — N/N-1 검증 후 백그라운드 롤링 saga."""
    with STORE.lock:
        cluster = _cluster_or_404(cluster_id)
        if cluster.state != "running":
            raise HTTPException(409, f"cluster '{cluster_id}'는 "
                                     f"{cluster.state} 상태 — 업그레이드 불가")
        tv = body.target_version
        if tv not in K8S_SUPPORTED_VERSIONS:
            raise HTTPException(422, f"지원 버전 {K8S_SUPPORTED_VERSIONS} "
                                     f"(N/N-1 정책, 요청: '{tv}')")
        if tv == cluster.version:
            raise HTTPException(409, f"이미 {tv} — 업그레이드 대상 아님")
        cur = (K8S_SUPPORTED_VERSIONS.index(cluster.version)
               if cluster.version in K8S_SUPPORTED_VERSIONS else -1)
        if cur >= 0 and K8S_SUPPORTED_VERSIONS.index(tv) < cur:
            raise HTTPException(409, f"다운그레이드 불가: {cluster.version} "
                                     f"→ {tv}")
        if any(u.cluster_id == cluster_id and u.state == "running"
               for u in STORE.k8s_upgrades.values()):
            raise HTTPException(409, "이미 진행 중인 업그레이드가 있음")
        # 롤링 순서: CP 먼저(마이너 스큐 정책), 그다음 GPU 워커
        node_names = list(cluster.cp_node_ids)
        for nid in cluster.worker_node_ids:
            node = STORE.node_instances.get(nid)
            if node:
                node_names.append(node.tray_id)
        uid = f"k8u-{STORE.next_seq('k8s_upgrade')}"
        up = K8sUpgrade(
            upgrade_id=uid, cluster_id=cluster_id,
            tenant_id=cluster.tenant_id, from_version=cluster.version,
            target_version=tv, state="running",
            node_progress={"done": 0, "total": len(node_names)},
            created_at=_now())
        STORE.k8s_upgrades[uid] = up
        emit("Portal/API", "NeoCloudOS.M6(K8sMgr)", "REST",
             f"POST /k8s/clusters/{cluster_id}/upgrades → {uid}",
             f"{cluster.name} {cluster.version} → {tv} — 노드 "
             f"{len(node_names)}대 롤링 (cordon→drain→upgrade→uncordon)",
             payload={"cluster": cluster_id, "target_version": tv,
                      "nodes": len(node_names)}, order_id=cluster.order_id)
    threading.Thread(target=_run_upgrade, args=(up, node_names),
                     name=f"k8s-upgrade-{uid}", daemon=True).start()
    return up


@router.get("/k8s/clusters/{cluster_id}/upgrades")
def list_upgrades(cluster_id: str) -> list:
    with STORE.lock:
        _cluster_or_404(cluster_id)
        return sorted((u for u in STORE.k8s_upgrades.values()
                       if u.cluster_id == cluster_id),
                      key=lambda u: u.upgrade_id, reverse=True)


@router.get("/k8s/cves")
def list_cves() -> list:
    """정적 큐레이션 CVE — 업그레이드·패치 탭 (영향 버전·패치 버전)."""
    return K8S_CVES


# ---------------------------------------------------------------------------
# 장애관리(NVSentinel) — AI Infra obs/faults → 노드 매핑 (R6)
# ---------------------------------------------------------------------------
@router.get("/k8s/clusters/{cluster_id}/health-events")
def cluster_health_events(cluster_id: str) -> list:
    """헬스 이벤트 — 열린 GPU 장애는 해당 워커를 Quarantined로 전이시키고
    hot-spare 교체 제안을 첨부한다 (fault 주입 → quarantine E2E 경로)."""
    with STORE.lock:
        cluster = _cluster_or_404(cluster_id)
    trays = _worker_tray_map(cluster)
    events, open_gpu = _scan_health(cluster, trays)
    with STORE.lock:
        newly = open_gpu - set(cluster.quarantined_nodes)
        cluster.quarantined_nodes = sorted(open_gpu)
        for tray in sorted(newly):
            emit("NVSentinel", "NeoCloudOS.M6(K8sMgr)", "internal",
                 f"quarantine — {tray}",
                 "열린 XID 장애 — 워커 cordon·격리, hot-spare 교체 제안",
                 payload={"cluster": cluster.id, "node": tray},
                 order_id=cluster.order_id)
        for ev in events:
            if ev["action"] == "quarantined":
                node = trays.get(ev["node"])
                ev["hot_spare"] = _hot_spare_for(node) if node else None
    return events


# ---------------------------------------------------------------------------
# 스토리지(GDS) — VAST 뷰의 테넌트 스코프 PVC 변환
# ---------------------------------------------------------------------------
@router.get("/k8s/clusters/{cluster_id}/storage")
def cluster_storage(cluster_id: str) -> list:
    with STORE.lock:
        cluster = _cluster_or_404(cluster_id)
    out = []
    for v in FAKE_VAST.list_views():
        if v.tenant_ref != cluster.tenant_id or v.state != "active":
            continue
        out.append({
            "pvc": f"pvc-{v.path.rsplit('/', 1)[-1]}",
            "namespace": "default",
            "mode": "RWX",
            "capacity_tb": v.capacity_tb,
            "used_tb": round(v.capacity_tb * ((v.id % 40) + 25) / 100, 1),
            "storage_class": "vast-nfs-rdma-gds",
            "gds": True,
        })
    return out


# ---------------------------------------------------------------------------
# 모니터링 — 클러스터 랙 스코프 DCGM 집계 (기존 tray 에뮬레이터 재사용)
# ---------------------------------------------------------------------------
@router.get("/k8s/clusters/{cluster_id}/metrics")
def cluster_metrics(cluster_id: str) -> dict:
    with STORE.lock:
        cluster = _cluster_or_404(cluster_id)
        tenant_id = cluster.tenant_id
        gpus_total, dcgm_mode = cluster.gpus_total, cluster.dcgm_mode
    # EMULATOR 조회는 STORE.lock 밖에서 (락 순서)
    summary = next((c for c in EMULATOR.clusters()
                    if c.tenant_id == tenant_id), None)
    hist = EMULATOR.history(tenant_id=tenant_id, limit=60)
    return {
        "gpu_util_pct": summary.avg_util_pct if summary else 0.0,
        "gpu_temp_max_c": summary.max_gpu_temp_c if summary else 0.0,
        "ecc_correctable": summary.ecc_corrected_total if summary else 0,
        "ib_bw_tbs": summary.nvlink_tbps if summary else 0.0,
        "gpus_total": gpus_total,
        "dcgm_mode": dcgm_mode,
        "ticks": [{"ts": h["at"], "util": h["avg_util_pct"],
                   "temp": h["max_gpu_temp_c"]} for h in hist],
    }
