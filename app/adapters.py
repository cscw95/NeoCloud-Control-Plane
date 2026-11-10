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
                       allocation_id: Optional[str] = None,
                       ib_pkey: Optional[int] = None) -> NicoSegment: ...
    def delete_segment(self, segment_id: str) -> NicoSegment: ...
    def list_segments(self) -> list: ...
    def attach_hosts(self, segment_id: str, host_ids: list,
                     purpose: str = "converged") -> NicoSegment: ...


class StorageAdapter(Protocol):
    """D4 — vendor storage control plane (default implementation: VAST VMS)."""
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
                       allocation_id: Optional[str] = None,
                       ib_pkey: Optional[int] = None) -> NicoSegment:
        return self._nico.create_segment(tenant_ref, vrf, l3vni,
                                         converged_vni, host_ids,
                                         allocation_id, ib_pkey)

    def delete_segment(self, segment_id: str) -> NicoSegment:
        return self._nico.delete_segment(segment_id)

    def list_segments(self) -> list:
        return self._nico.list_segments()

    def attach_hosts(self, segment_id: str, host_ids: list,
                     purpose: str = "converged") -> NicoSegment:
        return self._nico.attach_hosts(segment_id, host_ids, purpose)


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
        self._c = client or httpx.Client(base_url=base_url, timeout=30.0,
                                         headers=_emu_headers())

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
                       allocation_id: Optional[str] = None,
                       ib_pkey: Optional[int] = None) -> NicoSegment:
        return NicoSegment(**self._unwrap(self._c.post("/segments", json={
            "tenant_ref": tenant_ref, "vrf": vrf, "l3vni": l3vni,
            "converged_vni": converged_vni, "host_ids": host_ids,
            "allocation_id": allocation_id, "ib_pkey": ib_pkey})))

    def delete_segment(self, segment_id: str) -> NicoSegment:
        return NicoSegment(**self._unwrap(
            self._c.delete(f"/segments/{segment_id}")))

    def list_segments(self) -> list:
        return [NicoSegment(**s) for s in
                self._unwrap(self._c.get("/segments"))]

    def attach_hosts(self, segment_id: str, host_ids: list,
                     purpose: str = "converged") -> NicoSegment:
        return NicoSegment(**self._unwrap(self._c.patch(
            f"/segments/{segment_id}/hosts",
            json={"host_ids": host_ids, "purpose": purpose})))


# ---------------------------------------------------------------------------
# DSX OS baseline — real NICo REST (infra-controller rest-api/openapi/spec.yaml)
# ---------------------------------------------------------------------------
# Every path NOCP calls on the NICo REST surface, relative to the emulator root.
# Names follow spec.yaml v2 (`/v2/org/{org}/nico/...`); the NICo emulator records
# the same list in NICo-Emulator/docs/nico-rest-paths.md.
NICO_REST_PATHS = {
    "site":              "/v2/org/{org}/nico/site",
    "machine":           "/v2/org/{org}/nico/machine",
    "machine_one":       "/v2/org/{org}/nico/machine/{id}",
    "machine_power":     "/v2/org/{org}/nico/machine/{id}/power",
    "machine_health":    "/v2/org/{org}/nico/machine/{id}/health-report",
    "machine_validation": "/v2/org/{org}/nico/machine/{id}/validation/result",
    "machine_gpu_reset": "/v2/org/{org}/nico/machine/{id}/gpu/reset",   # emulator ext
    "instance":          "/v2/org/{org}/nico/instance",
    "instance_one":      "/v2/org/{org}/nico/instance/{id}",
    "instance_type":     "/v2/org/{org}/nico/instance/type",
    "allocation":        "/v2/org/{org}/nico/allocation",
    "vpc":               "/v2/org/{org}/nico/vpc",
    "subnet":            "/v2/org/{org}/nico/subnet",
    "nsg":               "/v2/org/{org}/nico/network-security-group",
    "ib_partition":      "/v2/org/{org}/nico/infiniband-partition",
    "nvlink_partition":  "/v2/org/{org}/nico/nvlink-logical-partition",
    "spectrumx_partition": "/v2/org/{org}/nico/spectrumx-partition",
    "rack":              "/v2/org/{org}/nico/rack",
    "rack_one":          "/v2/org/{org}/nico/rack/{id}",
    "rack_health":       "/v2/org/{org}/nico/rack/{id}/health-report",
    "tray":              "/v2/org/{org}/nico/tray",
    "tray_health":       "/v2/org/{org}/nico/tray/{id}/health-report",
    "nvlink_domain":     "/v2/org/{org}/nico/domain/nvlink",
    "firmware_baseline": "/v2/org/{org}/nico/firmware/baseline",      # emulator ext
    "firmware_drift":    "/v2/org/{org}/nico/firmware/drift",         # emulator ext
    "rack_firmware":     "/v2/org/{org}/nico/rack/{id}/firmware",
}

# NICo Machine → NOCP host state. `lifecycleState` (emulator extension, see
# NICo-Emulator/docs/nico-rest-paths.md) wins over the spec `status` enum.
# `assigned`/`InUse` covers several bridge sub-states (reserved, provisioning,
# provisioned, allocated) — the adapter resolves those on the bridge.
_LIFECYCLE_MAP = {
    "expected": "discovered", "discovered": "discovered", "validating": "discovered",
    "ready": "pool_ready", "sanitizing": "sanitizing", "failed": "quarantined",
}
_STATUS_MAP = {
    "initializing": "discovered", "ready": "pool_ready", "reset": "sanitizing",
    "error": "quarantined", "maintenance": "rma", "decommissioning": "rma",
    "decommissioned": "rma",
}
AMBIGUOUS = object()


def _emu_headers() -> dict:
    from .dsx import emu_headers
    return emu_headers()


def nico_root(url: str) -> str:
    """Emulator root from NOCP_NICO_URL (accepts either root or /nico-bridge base)."""
    return url.rstrip("/").rsplit("/nico-bridge", 1)[0]


def _machine_to_host(m: dict) -> NicoHost:
    """Map a NICo REST Machine object to the NicoHost contract.

    Accepts the native bridge shape too (an emulator may embed it), so the
    pipeline sees identical hosts in either API mode."""
    if "host_id" in m:
        return NicoHost(**{k: v for k, v in m.items() if k in NicoHost.model_fields})
    labels = m.get("labels") or {}
    meta = m.get("metadata") or {}
    lc = str(m.get("lifecycleState") or "").lower()
    status = str(m.get("status") or "").lower()
    state = (labels.get("nocp.state") or _LIFECYCLE_MAP.get(lc)
             or ("allocated" if lc == "assigned" or status == "inuse" else None)
             or _STATUS_MAP.get(status, "discovered"))
    mid = m.get("id", "")
    return NicoHost(
        host_id=mid,
        tray_id=(labels.get("trayId") or meta.get("trayId")
                 or (m.get("placementInRack") or {}).get("trayId")
                 or (mid[3:] if mid.startswith("nh-") else "")),
        # spec instanceTypeId is a UUID — the SKU is the controller machine type
        sku=(labels.get("sku") or m.get("controllerMachineType")
             or m.get("instanceTypeId") or ""),
        site=m.get("siteId") or "",
        firmware_ok=not m.get("firmwareDrift", False),
        state=state,
        cordoned=bool(m.get("maintenanceMessage")),
        instance_id=m.get("instanceId"),
        tenant_ref=m.get("tenantId"),
        bmc_ip=labels.get("bmcIp") or meta.get("bmcIp") or "",
        dpu_ip=labels.get("dpuIp") or meta.get("dpuIp") or "",
        host_ip=labels.get("hostIp") or meta.get("hostIp") or "",
    )


def _items(body) -> list:
    """NICo list responses are bare arrays; tolerate {items|results:[...]} envelopes."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("items", "results", "data"):
            if isinstance(body.get(k), list):
                return body[k]
    return []


class NicoRestClient:
    """Thin client over the NICo REST surface (v2.3 objects NOCP reads directly:
    racks, trays, health reports, validation, firmware, power actions)."""

    def __init__(self, root_url: str, org: str = "neocloud",
                 client: Optional[httpx.Client] = None) -> None:
        self.org = org
        self._c = client or httpx.Client(base_url=nico_root(root_url), timeout=15.0,
                                         headers=_emu_headers())

    def path(self, key: str, **kw) -> str:
        return NICO_REST_PATHS[key].format(org=self.org, **kw)

    def request(self, method: str, key: str, *, json: Optional[dict] = None,
                params: Optional[dict] = None, **kw):
        r = self._c.request(method, self.path(key, **kw), json=json, params=params)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail") or r.json().get("message") or r.text
            except ValueError:
                detail = r.text
            raise HTTPException(r.status_code, detail)
        return r.json() if r.content else {}

    def get(self, key: str, params: Optional[dict] = None, **kw):
        return self.request("GET", key, params=params, **kw)


class NicoRestAdapter(NicoHttpAdapter):
    """ComputeAdapter over the real NICo REST paths (NOCP_NICO_API=rest).

    Host reads use the spec `machine` resource. The order pipeline's host-state
    steps (reserve, provision job polling, allocate, release, sanitize, tenant
    segments) have no 1:1 public REST object — a REST Instance needs a tenant VPC
    and gets its own id — so they stay on the deprecated /nico-bridge, which
    shares one state with the REST surface inside the emulator. A machine in the
    REST `assigned` lifecycle is re-read on the bridge for its exact sub-state.
    A REST read answered with 404/405/501 falls back to the bridge."""

    _FALLBACK = {404, 405, 501}

    def __init__(self, url: str, org: str = "neocloud",
                 client: Optional[httpx.Client] = None) -> None:
        root = nico_root(url)
        c = client or httpx.Client(base_url=root, timeout=30.0, headers=_emu_headers())
        self.rest = NicoRestClient(root, org, client=c)
        super().__init__(root, client=_PrefixClient(c, "/nico-bridge"))

    def _rest_get(self, key: str, params: Optional[dict] = None, **kw):
        try:
            return self.rest.get(key, params=params, **kw)
        except HTTPException as e:
            if e.status_code in self._FALLBACK:
                return None
            raise

    def list_hosts(self) -> list[NicoHost]:
        body = self._rest_get("machine", params={"pageSize": 100})
        if body is None:
            return super().list_hosts()
        items = _items(body)
        hosts = [_machine_to_host(m) for m in items]
        total = len(items)
        page = 2
        while total == 100 * (page - 1):            # spec pagination (max 100)
            more = _items(self._rest_get("machine", params={"pageSize": 100,
                                                            "pageNumber": page}) or [])
            if not more:
                break
            hosts += [_machine_to_host(m) for m in more]
            total += len(more)
            page += 1
        if any(h.state.value == "allocated" for h in hosts):
            exact = {h.host_id: h for h in super().list_hosts()}
            hosts = [exact.get(h.host_id, h) if h.state.value == "allocated" else h
                     for h in hosts]
        return hosts

    def get_host(self, host_id: str) -> NicoHost:
        body = self._rest_get("machine_one", id=host_id)
        if body is None:
            return super().get_host(host_id)
        host = _machine_to_host(body)
        # REST folds several bridge states together: assigned (reserved..allocated)
        # and sanitizing (released, wipe pending) — re-read the exact one.
        if host.state.value in ("allocated", "sanitizing"):
            return super().get_host(host_id)
        return host


class _PrefixClient:
    """Wrap an httpx.Client so relative bridge paths get the /nico-bridge prefix."""

    def __init__(self, c: httpx.Client, prefix: str) -> None:
        self._c, self._p = c, prefix

    def __getattr__(self, name):
        fn = getattr(self._c, name)
        if name in ("get", "post", "put", "patch", "delete", "request"):
            def call(*a, **kw):
                if name == "request":
                    a = (a[0], self._p + a[1]) + a[2:]
                else:
                    a = (self._p + a[0],) + a[1:]
                return fn(*a, **kw)
            return call
        return fn


def nico_api_mode() -> str:
    """NOCP_NICO_API=rest|bridge (default rest) — only meaningful with NOCP_NICO_URL."""
    import os
    mode = os.environ.get("NOCP_NICO_API", "rest").strip().lower()
    return mode if mode in ("rest", "bridge") else "rest"


def build_http_adapter(url: str) -> ComputeAdapter:
    import os
    if nico_api_mode() == "bridge":
        base = url if url.rstrip("/").endswith("/nico-bridge") else nico_root(url) + "/nico-bridge"
        return NicoHttpAdapter(base)
    return NicoRestAdapter(url, org=os.environ.get("NOCP_NICO_ORG", "neocloud"))
