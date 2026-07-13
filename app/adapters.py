"""D1 ComputeAdapter — the seam between the order pipeline and NICo.

The pipeline only ever talks to the `ComputeAdapter` protocol, so the backing
implementation can be swapped without touching core logic:

  - LocalNicoAdapter : in-process calls into the FakeNico simulator (default;
                       used by tests and the MVP deployment).
  - NicoHttpAdapter  : same contract over REST — the shape the real NICo
                       integration will take (endpoint paths to be pinned to
                       the official NICo API reference before production).

All operations are idempotent-or-guarded: NICo enforces host-state
preconditions, so a retried call fails loudly (409) instead of corrupting
state. `wait_job` implements the poll-until-converged pattern — completion is
judged by observed job state, never assumed from a 2xx on submission.
"""

from __future__ import annotations

from typing import Optional, Protocol

import httpx
from fastapi import HTTPException

from .nico_fake import FakeNico, NicoHost, NicoJob, NicoSegment, SanitizeReport
from .vast_fake import FakeVast, VastView


class ComputeAdapter(Protocol):
    def list_hosts(self) -> list[NicoHost]: ...
    def get_host(self, host_id: str) -> NicoHost: ...
    def reserve(self, host_id: str) -> NicoHost: ...
    def unreserve(self, host_id: str) -> NicoHost: ...
    def provision(self, host_id: str, image_ref: str) -> NicoJob: ...
    def allocate(self, host_id: str, tenant_ref: str) -> NicoHost: ...
    def release(self, instance_id: str) -> NicoJob: ...
    def abort_provision(self, host_id: str) -> NicoHost: ...
    def sanitize(self, host_id: str) -> NicoJob: ...
    def get_sanitize_report(self, host_id: str) -> SanitizeReport: ...
    def get_job(self, job_id: str) -> NicoJob: ...
    def cordon(self, host_id: str, reason: str = "") -> NicoHost: ...
    def create_segment(self, tenant_ref: str, vrf: str, l3vni: int,
                       converged_vni: int, host_ids: list,
                       allocation_id: Optional[str] = None) -> NicoSegment: ...
    def delete_segment(self, segment_id: str) -> NicoSegment: ...
    def list_segments(self) -> list: ...


class StorageAdapter(Protocol):
    """D4 — 벤더 스토리지 컨트롤 플레인 (기본 구현: VAST VMS)."""
    def create_view(self, path: str, tenant_ref: str, export_subnet: str,
                    allocation_id: Optional[str] = None) -> VastView: ...
    def set_quota(self, path: str, capacity_tb: float) -> VastView: ...
    def set_qos(self, path: str, gbps: float, iops_k: float) -> VastView: ...
    def delete_view(self, path: str) -> VastView: ...
    def list_views(self) -> list: ...


def wait_job(adapter: ComputeAdapter, job: NicoJob, max_polls: int = 60) -> NicoJob:
    """Poll a NICo job to terminal state (fake jobs tick per poll, no sleeps)."""
    for _ in range(max_polls):
        if job.state != "running":
            return job
        job = adapter.get_job(job.job_id)
    raise HTTPException(
        504, f"nico job '{job.job_id}' ({job.op}) still running "
             f"after {max_polls} polls")


class LocalNicoAdapter:
    """In-process adapter over the FakeNico simulator."""

    def __init__(self, nico: FakeNico) -> None:
        self._nico = nico

    def list_hosts(self) -> list[NicoHost]:
        return self._nico.list_hosts()

    def get_host(self, host_id: str) -> NicoHost:
        return self._nico.get_host(host_id)

    def reserve(self, host_id: str) -> NicoHost:
        return self._nico.reserve(host_id)

    def unreserve(self, host_id: str) -> NicoHost:
        return self._nico.unreserve(host_id)

    def provision(self, host_id: str, image_ref: str) -> NicoJob:
        return self._nico.provision(host_id, image_ref)

    def allocate(self, host_id: str, tenant_ref: str) -> NicoHost:
        return self._nico.allocate(host_id, tenant_ref)

    def release(self, instance_id: str) -> NicoJob:
        return self._nico.release(instance_id)

    def abort_provision(self, host_id: str) -> NicoHost:
        return self._nico.abort_provision(host_id)

    def sanitize(self, host_id: str) -> NicoJob:
        return self._nico.sanitize(host_id)

    def get_sanitize_report(self, host_id: str) -> SanitizeReport:
        return self._nico.get_sanitize_report(host_id)

    def get_job(self, job_id: str) -> NicoJob:
        return self._nico.get_job(job_id)

    def cordon(self, host_id: str, reason: str = "") -> NicoHost:
        return self._nico.cordon(host_id, reason)

    def create_segment(self, tenant_ref: str, vrf: str, l3vni: int,
                       converged_vni: int, host_ids: list,
                       allocation_id: Optional[str] = None) -> NicoSegment:
        return self._nico.create_segment(tenant_ref, vrf, l3vni,
                                         converged_vni, host_ids, allocation_id)

    def delete_segment(self, segment_id: str) -> NicoSegment:
        return self._nico.delete_segment(segment_id)

    def list_segments(self) -> list:
        return self._nico.list_segments()


class LocalVastAdapter:
    """In-process D4 adapter over the FakeVast simulator."""

    def __init__(self, vast: FakeVast) -> None:
        self._vast = vast

    def create_view(self, path: str, tenant_ref: str, export_subnet: str,
                    allocation_id: Optional[str] = None) -> VastView:
        return self._vast.create_view(path, tenant_ref, export_subnet,
                                      allocation_id)

    def set_quota(self, path: str, capacity_tb: float) -> VastView:
        return self._vast.set_quota(path, capacity_tb)

    def set_qos(self, path: str, gbps: float, iops_k: float) -> VastView:
        return self._vast.set_qos(path, gbps, iops_k)

    def delete_view(self, path: str) -> VastView:
        return self._vast.delete_view(path)

    def list_views(self) -> list:
        return self._vast.list_views()


class NicoHttpAdapter:
    """REST adapter — same contract as LocalNicoAdapter over HTTP.

    Paths currently mirror the /fake-nico router; swap `base_url` (and pin the
    real NICo paths + JWT auth) when integrating the actual site controller.
    """

    def __init__(self, base_url: str, client: Optional[httpx.Client] = None) -> None:
        self._c = client or httpx.Client(base_url=base_url, timeout=30.0)

    def _unwrap(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            detail = resp.json().get("detail", resp.text)
            raise HTTPException(resp.status_code, detail)
        return resp.json()

    def list_hosts(self) -> list[NicoHost]:
        return [NicoHost(**h) for h in self._unwrap(self._c.get("/hosts"))]

    def get_host(self, host_id: str) -> NicoHost:
        return NicoHost(**self._unwrap(self._c.get(f"/hosts/{host_id}")))

    def reserve(self, host_id: str) -> NicoHost:
        return NicoHost(**self._unwrap(self._c.post(f"/hosts/{host_id}/reserve")))

    def unreserve(self, host_id: str) -> NicoHost:
        return NicoHost(**self._unwrap(self._c.post(f"/hosts/{host_id}/unreserve")))

    def provision(self, host_id: str, image_ref: str) -> NicoJob:
        return NicoJob(**self._unwrap(self._c.post(
            f"/hosts/{host_id}/provision", json={"image_ref": image_ref})))

    def allocate(self, host_id: str, tenant_ref: str) -> NicoHost:
        return NicoHost(**self._unwrap(self._c.post(
            "/instances", json={"host_id": host_id, "tenant_ref": tenant_ref})))

    def release(self, instance_id: str) -> NicoJob:
        return NicoJob(**self._unwrap(self._c.delete(f"/instances/{instance_id}")))

    def abort_provision(self, host_id: str) -> NicoHost:
        return NicoHost(**self._unwrap(self._c.post(f"/hosts/{host_id}/abort-provision")))

    def sanitize(self, host_id: str) -> NicoJob:
        return NicoJob(**self._unwrap(self._c.post(f"/hosts/{host_id}/sanitize")))

    def get_sanitize_report(self, host_id: str) -> SanitizeReport:
        return SanitizeReport(**self._unwrap(
            self._c.get(f"/hosts/{host_id}/sanitize-report")))

    def get_job(self, job_id: str) -> NicoJob:
        return NicoJob(**self._unwrap(self._c.get(f"/jobs/{job_id}")))

    def cordon(self, host_id: str, reason: str = "") -> NicoHost:
        return NicoHost(**self._unwrap(self._c.post(
            f"/hosts/{host_id}/cordon", json={"reason": reason})))

    def create_segment(self, tenant_ref: str, vrf: str, l3vni: int,
                       converged_vni: int, host_ids: list,
                       allocation_id: Optional[str] = None) -> NicoSegment:
        return NicoSegment(**self._unwrap(self._c.post("/segments", json={
            "tenant_ref": tenant_ref, "vrf": vrf, "l3vni": l3vni,
            "converged_vni": converged_vni, "host_ids": host_ids,
            "allocation_id": allocation_id})))

    def delete_segment(self, segment_id: str) -> NicoSegment:
        return NicoSegment(**self._unwrap(
            self._c.delete(f"/segments/{segment_id}")))

    def list_segments(self) -> list:
        return [NicoSegment(**s) for s in
                self._unwrap(self._c.get("/segments"))]
