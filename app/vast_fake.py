"""Fake VAST Data Platform — VMS(VAST Management System) API simulator.

Stands in (fake) for the D4 StorageAdapter. Simulates a simplified conceptual
model of the real VAST VMS REST API (v3):

  - View        : NFS/S3 export path (per-tenant namespace) — /api/v3/views
  - View Policy : access control — export only to the tenant VPC(VRF) subnet
  - Quota       : capacity hard limit — /api/v3/quotas
  - QoS Policy  : bandwidth/IOPS ceiling — /api/v3/qospolicies

Each call and its internal VMS actions (CNode export activation, snapshot
destruction, etc.) are recorded to trace. For real integration, replace
LocalVastAdapter with a VMS REST adapter.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .trace import emit


class VastView(BaseModel):
    id: int
    path: str                          # e.g. /tenants/tnt-x/ord-1
    tenant_ref: str
    allocation_id: Optional[str] = None
    protocols: list = ["NFS4", "RDMA"]
    capacity_tb: float = 0
    qos_gbps: float = 0
    qos_iops_k: float = 0
    export_subnet: str = ""            # allow access only from the tenant VRF-side subnet
    state: str = "active"


class FakeVast:
    def __init__(self) -> None:
        self.views: dict[str, VastView] = {}    # keyed by path
        self._seq = 0

    def reset(self) -> None:
        self.views.clear()
        self._seq = 0

    def create_view(self, path: str, tenant_ref: str, export_subnet: str,
                    allocation_id: Optional[str] = None) -> VastView:
        if path in self.views:
            raise HTTPException(409, f"vast: view '{path}' already exists")
        self._seq += 1
        view = VastView(id=self._seq, path=path, tenant_ref=tenant_ref,
                        export_subnet=export_subnet, allocation_id=allocation_id)
        self.views[path] = view
        emit("NeoCloudOS.D4", "VAST.VMS", "VAST-API", "POST /api/v3/views",
             f"테넌트 뷰 생성 — {path} (NFSoRDMA), export는 테넌트 VRF "
             f"서브넷 {export_subnet}로 제한",
             payload={"path": path, "protocols": view.protocols,
                      "policy": {"flavor": "NFS4",
                                 "vip_pool": "tenant-data",
                                 "host_access": [export_subnet]},
                      "create_dir": True})
        emit("VAST.VMS", "VAST.CNodes", "internal",
             "뷰 export 활성화",
             f"전 CNode에 {path} export 전파 — RDMA 타깃 등록, "
             "테넌트 외 서브넷 mount 거부",
             payload={"view_id": view.id, "vip_pool": "tenant-data"})
        return view

    def set_quota(self, path: str, capacity_tb: float) -> VastView:
        view = self._view(path)
        view.capacity_tb = capacity_tb
        emit("NeoCloudOS.D4", "VAST.VMS", "VAST-API", "POST /api/v3/quotas",
             f"용량 쿼터 — {path} hard limit {capacity_tb}TB",
             payload={"path": path, "hard_limit": f"{capacity_tb}TB",
                      "soft_limit": f"{capacity_tb * 0.9:.0f}TB",
                      "alarm_at": "90%"})
        return view

    def set_qos(self, path: str, gbps: float, iops_k: float) -> VastView:
        view = self._view(path)
        view.qos_gbps = gbps
        view.qos_iops_k = iops_k
        emit("NeoCloudOS.D4", "VAST.VMS", "VAST-API",
             "POST /api/v3/qospolicies",
             f"QoS 정책 — {path} 대역폭 {gbps}GB/s · {iops_k}K IOPS "
             "(noisy-neighbor 차단, 성능 SLA 근거)",
             payload={"attached_view": path, "limit_by": "BW_IOPS",
                      "max_bw_gbps": gbps, "max_iops_k": iops_k})
        return view

    def delete_view(self, path: str) -> VastView:
        view = self.views.pop(path, None)
        if not view:
            raise HTTPException(404, f"vast: view '{path}' not found")
        view.state = "deleted"
        emit("NeoCloudOS.D4", "VAST.VMS", "VAST-API",
             f"DELETE /api/v3/views/{view.id}",
             f"뷰 회수 — {path}: export 해제, 스냅샷 전체 파기, "
             "쿼터/QoS 정책 제거 (테넌트 데이터 완전 삭제 증적)",
             payload={"view_id": view.id, "purge_snapshots": True,
                      "remove_dir": True})
        emit("VAST.VMS", "VAST.CNodes", "internal", "export 회수·데이터 파기",
             f"{path} — CNode export 제거, 대상 elements 소거 큐 등록",
             payload={"view_id": view.id})
        return view

    def _view(self, path: str) -> VastView:
        view = self.views.get(path)
        if not view:
            raise HTTPException(404, f"vast: view '{path}' not found")
        return view

    def list_views(self) -> list:
        return list(self.views.values())


FAKE_VAST = FakeVast()

router = APIRouter(prefix="/fake-vast", tags=["fake-vast"])


@router.get("/views", response_model=list[VastView])
def list_views() -> list[VastView]:
    return FAKE_VAST.list_views()
