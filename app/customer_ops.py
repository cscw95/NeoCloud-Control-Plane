"""고객면 운영 API — 각 콘솔 메뉴 액션을 실제 에뮬레이터/모델 상태에 연동.

계약: 테넌트 격리·RBAC는 TenancyGuardMiddleware(X-Tenant-Id/X-Tenant-Role)가
담당한다. 여기 핸들러는 헤더의 테넌트로 소유를 확정하고, 물리 효과는 실제
에뮬레이터로 위임한다:
  - 노드 재기동/교체 → AI Infra Emulator `/emulator/v1/trayops/*`
    (power_cycle → post → nico_discovery → dhcp_ip → boot → … → tenant rejoin)
  - 스토리지 볼륨/스냅샷/QoS → VAST 뷰 모델(초기 시드는 `/vast/v1/views`)
  - API 키 / 조직 멤버 → 테넌트 IAM 실렘(shared_services)

목록 GET은 헤더 테넌트로 자체 필터하며(미들웨어 필터 대상 외 신규 경로),
변경 메서드의 viewer 차단·타 테넌트 차단은 미들웨어가 이미 수행한다.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .store import STORE
from .trace import emit as _emit

AI_INFRA_BASE = os.environ.get("AI_INFRA_URL", "http://127.0.0.1:9100").rstrip("/")


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()

router = APIRouter(prefix="/api/v1", tags=["customer-ops"])


def _tenant(x_tenant_id: Optional[str]) -> str:
    if not x_tenant_id:
        raise HTTPException(400, "X-Tenant-Id header required (customer plane)")
    return x_tenant_id


def _ai_post(path: str, timeout: float = 6.0) -> dict:
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"{AI_INFRA_BASE}{path}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"AI Infra unreachable: {e}") from e
    if r.status_code >= 400:
        raise HTTPException(502, f"AI Infra error {r.status_code}: {r.text[:180]}")
    return r.json() if r.content else {}


def _ai_get(path: str, timeout: float = 4.0):
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{AI_INFRA_BASE}{path}")
        if r.status_code >= 400:
            return None
        return r.json()
    except httpx.HTTPError:
        return None


def _tray_of(node_id: str) -> str:
    """콘솔 node_id(nh-… / host_id) → AI Infra tray_id."""
    return node_id[3:] if node_id.startswith("nh-") else node_id


def _owns_node(node_id: str, tid: str) -> bool:
    """해당 노드가 테넌트 소유인지 확인 — NICo 브리지 호스트의 tenant_ref 기준."""
    from .lifecycle import get_adapter
    tray = _tray_of(node_id)
    try:
        for h in get_adapter().list_hosts():
            hid = h.get("host_id") if isinstance(h, dict) else getattr(h, "host_id", None)
            if hid in (node_id, f"nh-{tray}"):
                ref = (h.get("tenant_ref") if isinstance(h, dict)
                       else getattr(h, "tenant_ref", None))
                return ref == tid
    except Exception:
        pass
    # 보조: store 노드 인스턴스
    n = STORE.node_instances.get(node_id) or STORE.node_instances.get(f"nh-{tray}")
    return bool(n and getattr(n, "tenant_id", None) == tid)


# ── 노드 재기동 / 하드웨어 교체 (AI Infra trayops 실 라이프사이클) ──────────
@router.post("/nodes/{node_id}/reboot")
def node_reboot(node_id: str, x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    if not _owns_node(node_id, tid):
        raise HTTPException(403, "tenant scope violation")
    op = _ai_post(f"/emulator/v1/trayops/{_tray_of(node_id)}/reboot")
    with STORE.lock:
        STORE.node_ops[op.get("op_id", node_id)] = {**op, "tenant_id": tid,
                                                    "node_id": node_id}
    _emit("customer", "ai-infra", "trayops", "node.reboot",
               f"{node_id} 재기동 라이프사이클 개시", None)
    return op


@router.post("/nodes/{node_id}/replace")
def node_replace(node_id: str, x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    if not _owns_node(node_id, tid):
        raise HTTPException(403, "tenant scope violation")
    op = _ai_post(f"/emulator/v1/trayops/{_tray_of(node_id)}/replace")
    with STORE.lock:
        STORE.node_ops[op.get("op_id", node_id)] = {**op, "tenant_id": tid,
                                                    "node_id": node_id}
    _emit("customer", "ai-infra", "trayops", "node.replace",
               f"{node_id} 하드웨어 교체 라이프사이클 개시", None)
    return op


@router.get("/nodes/{node_id}/lifecycle")
def node_lifecycle(node_id: str, x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    tray = _tray_of(node_id)
    ops = _ai_get("/emulator/v1/obs/tray-ops") or {}
    inflight = ops.get("inflight") or []
    recent = ops.get("recent") or ops.get("history") or []
    mine = [o for o in (inflight + recent)
            if o.get("tray_id") == tray and o.get("tenant_id") in (tid, None)]
    return {"node_id": node_id, "tray_id": tray,
            "active": [o for o in inflight if o.get("tray_id") == tray],
            "ops": mine[:10]}


# ── 스토리지: 볼륨 / 스냅샷 / QoS (VAST 뷰 모델) ───────────────────────────
def _seed_volumes(tid: str) -> None:
    """테넌트 볼륨이 비어 있으면 VAST 실 뷰에서 1회 시드."""
    if any(v["tenant_id"] == tid for v in STORE.storage_volumes.values()):
        return
    views = _ai_get("/vast/v1/views") or []
    if isinstance(views, dict):
        views = views.get("views") or []
    for v in views:
        if v.get("tenant_id") != tid:
            continue
        vid = f"vol-{STORE.next_seq('vol'):04d}"
        STORE.storage_volumes[vid] = {
            "volume_id": vid, "tenant_id": tid,
            "path": v.get("path"), "cluster": v.get("cluster"),
            "protocols": v.get("protocols", ["NFS"]),
            "capacity_tb": v.get("capacity_tb", 100.0),
            "used_tb": v.get("used_tb", 0.0),
            "quota_tb": v.get("quota_tb", v.get("capacity_tb", 100.0)),
            "qos": v.get("qos", {"bw_gbps": 1000.0, "iops_k": 200.0}),
            "state": v.get("state", "active"), "created_at": _iso()}


class _VolumeBody(BaseModel):
    name: str
    capacity_tb: float = 100.0
    protocol: str = "NFS"
    qos_bw_gbps: float = 1000.0
    qos_iops_k: float = 200.0


class _QosBody(BaseModel):
    bw_gbps: float
    iops_k: float


@router.get("/storage/volumes")
def list_volumes(x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    _seed_volumes(tid)
    with STORE.lock:
        return [v for v in STORE.storage_volumes.values()
                if v["tenant_id"] == tid]


@router.post("/storage/volumes", status_code=201)
def create_volume(body: _VolumeBody, x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    with STORE.lock:
        vid = f"vol-{STORE.next_seq('vol'):04d}"
        v = {"volume_id": vid, "tenant_id": tid,
             "path": f"/{tid}/{body.name}", "cluster": "vast-ansan",
             "protocols": [body.protocol], "capacity_tb": body.capacity_tb,
             "used_tb": 0.0, "quota_tb": body.capacity_tb,
             "qos": {"bw_gbps": body.qos_bw_gbps, "iops_k": body.qos_iops_k},
             "state": "active", "created_at": _iso()}
        STORE.storage_volumes[vid] = v
    _emit("customer", "vast", "storage", "volume.create",
               f"{body.name} ({body.capacity_tb}TB) 생성", None)
    return v


@router.patch("/storage/volumes/{vid}/qos")
def set_qos(vid: str, body: _QosBody, x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    v = STORE.storage_volumes.get(vid)
    if not v or v["tenant_id"] != tid:
        raise HTTPException(404, "volume not found")
    v["qos"] = {"bw_gbps": body.bw_gbps, "iops_k": body.iops_k}
    _emit("customer", "vast", "storage", "volume.qos",
               f"{vid} QoS {body.bw_gbps}Gbps/{body.iops_k}K IOPS", None)
    return v


@router.delete("/storage/volumes/{vid}", status_code=204)
def delete_volume(vid: str, x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    v = STORE.storage_volumes.get(vid)
    if not v or v["tenant_id"] != tid:
        raise HTTPException(404, "volume not found")
    del STORE.storage_volumes[vid]
    return None


class _SnapBody(BaseModel):
    volume_id: str
    note: str = ""


@router.get("/storage/snapshots")
def list_snapshots(x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    return [s for s in STORE.storage_snapshots.values() if s["tenant_id"] == tid]


@router.post("/storage/snapshots", status_code=201)
def create_snapshot(body: _SnapBody, x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    v = STORE.storage_volumes.get(body.volume_id)
    if not v or v["tenant_id"] != tid:
        raise HTTPException(404, "volume not found")
    with STORE.lock:
        sid = f"snap-{STORE.next_seq('snap'):04d}"
        s = {"snapshot_id": sid, "tenant_id": tid, "volume_id": body.volume_id,
             "path": v["path"], "size_tb": round(v["used_tb"], 1),
             "note": body.note, "created_at": _iso(), "state": "ready"}
        STORE.storage_snapshots[sid] = s
    _emit("customer", "vast", "storage", "snapshot.create",
               f"{body.volume_id} 스냅샷", None)
    return s


# ── IAM: API 키 / 조직 멤버 ────────────────────────────────────────────────
class _ApiKeyBody(BaseModel):
    name: str
    scope: str = "read-write"


@router.get("/api-keys")
def list_api_keys(x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    # 시크릿은 목록에서 제외(발급 시 1회만 노출)
    return [{k: v for k, v in a.items() if k != "secret"}
            for a in STORE.api_keys.values() if a["tenant_id"] == tid]


@router.post("/api-keys", status_code=201)
def create_api_key(body: _ApiKeyBody, x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    with STORE.lock:
        kid = f"key-{STORE.next_seq('apikey'):04d}"
        seq = STORE.next_seq("apikey_secret")
        secret = f"nc_sk_{tid[4:]}_{seq:08x}{'a1b2c3d4e5f6':.12s}"
        a = {"key_id": kid, "tenant_id": tid, "name": body.name,
             "scope": body.scope, "prefix": secret[:14] + "…",
             "secret": secret, "created_at": _iso(), "state": "active",
             "last_used": None}
        STORE.api_keys[kid] = a
    _emit("customer", "iam", "shared", "apikey.issue",
               f"{body.name} 발급", None)
    return a  # 발급 시에만 secret 포함(1회 노출)


@router.delete("/api-keys/{kid}", status_code=204)
def revoke_api_key(kid: str, x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    a = STORE.api_keys.get(kid)
    if not a or a["tenant_id"] != tid:
        raise HTTPException(404, "api key not found")
    del STORE.api_keys[kid]
    return None


class _MemberBody(BaseModel):
    email: str
    role: str = "member"     # admin | member | viewer


@router.get("/members")
def list_members(x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    return [m for m in STORE.members.values() if m["tenant_id"] == tid]


@router.post("/members", status_code=201)
def invite_member(body: _MemberBody, x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    if body.role not in ("admin", "member", "viewer"):
        raise HTTPException(422, "role must be admin|member|viewer")
    with STORE.lock:
        mid = f"mbr-{STORE.next_seq('member'):04d}"
        m = {"member_id": mid, "tenant_id": tid, "email": body.email,
             "role": body.role, "state": "invited", "invited_at": _iso(),
             "mfa": False}
        STORE.members[mid] = m
    _emit("customer", "iam", "shared", "member.invite",
               f"{body.email} ({body.role}) 초대", None)
    return m


@router.patch("/members/{mid}")
def update_member(mid: str, body: _MemberBody,
                  x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    m = STORE.members.get(mid)
    if not m or m["tenant_id"] != tid:
        raise HTTPException(404, "member not found")
    m["role"] = body.role
    return m


@router.delete("/members/{mid}", status_code=204)
def remove_member(mid: str, x_tenant_id: Optional[str] = Header(None)):
    tid = _tenant(x_tenant_id)
    m = STORE.members.get(mid)
    if not m or m["tenant_id"] != tid:
        raise HTTPException(404, "member not found")
    del STORE.members[mid]
    return None
