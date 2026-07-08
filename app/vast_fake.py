"""Fake VAST Data Platform — VMS(VAST Management System) API simulator.

D4 StorageAdapter의 대역(fake). 실제 VAST VMS REST API(v3)의 개념 모델을
단순화해 시뮬레이션한다:

  - View        : NFS/S3 export 경로 (테넌트별 네임스페이스) — /api/v3/views
  - View Policy : 접근 제어 — 테넌트 VPC(VRF) 서브넷에만 export
  - Quota       : 용량 하드리밋 — /api/v3/quotas
  - QoS Policy  : 대역폭/IOPS 상한 — /api/v3/qospolicies

각 호출과 VMS 내부 동작(CNode export 활성화, 스냅샷 파기 등)을 trace로
기록한다. 실제 연동 시 LocalVastAdapter를 VMS REST 어댑터로 교체한다.
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
    export_subnet: str = ""            # 테넌트 VRF 측 서브넷만 접근 허용
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
