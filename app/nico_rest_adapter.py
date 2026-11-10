"""F08 — ComputeAdapter writes on the real NICo REST surface (NOCP_NICO_API=rest).

Every pipeline step maps to a spec path of dsx-ai-factory/infra-controller
@9ace631fa361 rest-api/openapi/spec.yaml (prefix ``/v2/org/{org}/nico``); the
call order follows rest-api/openapi/example-workflows.md and the Temporal
instance flows rest-api/workflow/pkg/workflow/instance/{create,delete}.go:

  reserve / unreserve / provision   PATCH /machine/{id} {labels}       (x-neocloud hold:
                                      labels ``nocp.state``; NICo never auto-picks a held machine)
  allocate                          POST /vpc (tenant, once) → POST /allocation
                                      {name, tenantId, siteId, allocationConstraints} →
                                      POST /instance {name, tenantId, instanceTypeId, vpcId, machineId}
  create_segment                    POST /infiniband-partition, /nvlink-logical-partition,
                                      /spectrumx-partition → PATCH /vpc/{id} →
                                      PATCH /instance/{id} {infinibandInterfaces}
  release                           DELETE /instance/{id} → GET /instance/{id}/status-history
  sanitize                          GET /machine/{id} until Ready (cleanup = Reset → Ready)
                                      → partition / VPC / allocation DELETE once the
                                      tenant VPC is empty and its segment was released (B2-13)
  cordon                            PATCH /machine/{id} {maintenanceMessage}

There is no /nico-bridge call in this mode; the deprecated bridge is used only
with NOCP_NICO_API=bridge (``NicoHttpAdapter``). Each write is recorded on the
provisioning trace (channel REST, op ``METHOD /v2/org/...``) and in ``calls``.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from collections import deque
from typing import Optional

import httpx
from fastapi import HTTPException

from .adapters import (NicoHttpAdapter, NicoRestClient, _emu_headers, _items,
                       _machine_to_host, nico_root)
from .nico_fake import (SANITIZE_STEPS, NicoHost, NicoHostState, NicoJob,
                        NicoSegment, SanitizeReport)

HOLD = "nocp.state"                    # x-neocloud machine label (NICo-Emulator HOLD_LABEL)
IMAGE = "nocp.image"
HELD = ("reserved", "provisioning", "provisioned")
PKEY_LABEL = "neocloud.io/partition-key"   # x-neocloud (NICo-Emulator network.PKEY_LABEL)
SEG = "nocp.segment"                   # VPC label: active | deleting
TENANT = "nocp.tenant"

# extra spec paths the write path uses (NICO_REST_PATHS keeps the read set)
PATHS = {
    "vpc_one": "/v2/org/{org}/nico/vpc/{id}",
    "instance_history": "/v2/org/{org}/nico/instance/{id}/status-history",
    "machine_history": "/v2/org/{org}/nico/machine/{id}/status-history",
    "allocation_one": "/v2/org/{org}/nico/allocation/{id}",
    "allocation_constraint": "/v2/org/{org}/nico/allocation/{id}/constraint/{cid}",
    "ib_partition_one": "/v2/org/{org}/nico/infiniband-partition/{id}",
    "nvlink_partition_one": "/v2/org/{org}/nico/nvlink-logical-partition/{id}",
    "spectrumx_partition_one": "/v2/org/{org}/nico/spectrumx-partition/{id}",
    "operating_system": "/v2/org/{org}/nico/operating-system",
}


def _poll_s() -> float:
    return float(os.environ.get("NOCP_NICO_POLL_S", "0.25"))


class NicoRestAdapter(NicoHttpAdapter):
    """ComputeAdapter whose reads *and writes* use the NICo REST surface."""

    def __init__(self, url: str, org: str = "neocloud",
                 client: Optional[httpx.Client] = None) -> None:
        root = nico_root(url)
        self._c = client or httpx.Client(base_url=root, timeout=30.0,
                                         headers=_emu_headers())
        self.rest = NicoRestClient(root, org, client=self._c)
        self.org = org
        self._lock = threading.RLock()
        self._jobs: dict[str, NicoJob] = {}
        self._released: dict[str, tuple] = {}     # machine id -> (instance id, vpc id)
        self._seq = itertools.count(1)
        self.calls: deque = deque(maxlen=1000)    # every REST call (method, path, status)

    # -- transport -------------------------------------------------------
    def _path(self, key: str, **kw) -> str:
        from .adapters import NICO_REST_PATHS
        tpl = PATHS.get(key) or NICO_REST_PATHS[key]
        return tpl.format(org=self.org, **kw)

    def _req(self, method: str, key: str, *, json_body: Optional[dict] = None,
             params: Optional[dict] = None, trace: str = "", host_id: Optional[str] = None,
             raw: bool = False, **kw):
        path = self._path(key, **kw)
        r = self._c.request(method, path, json=json_body, params=params)
        self.calls.append((method, path, r.status_code))
        if method != "GET":
            try:
                from .trace import emit
                emit("NeoCloudOS.D1", "NICo.REST", "REST", f"{method} {path}",
                     trace or f"NICo REST {method} ({r.status_code})",
                     payload=json_body, host_id=host_id)
            except Exception:                     # noqa: BLE001 — trace is best effort
                pass
        if r.status_code >= 400:
            try:
                b = r.json()
                detail = (b.get("detail") or b.get("message") or r.text) if isinstance(b, dict) else r.text
                if isinstance(detail, dict):
                    detail = detail.get("message") or json.dumps(detail)
            except ValueError:
                detail = r.text
            raise HTTPException(r.status_code, detail)
        if raw:
            return r
        return r.json() if r.content else {}

    def _get(self, key: str, params: Optional[dict] = None, **kw):
        return self._req("GET", key, params=params, **kw)

    def _total(self, key: str, params: dict) -> int:
        r = self._req("GET", key, params={**params, "pageSize": 1}, raw=True)
        try:
            return int(json.loads(r.headers.get("X-Pagination", "{}")).get("total"))
        except (TypeError, ValueError):
            return len(_items(r.json()))

    def _all(self, key: str, params: Optional[dict] = None) -> list:
        out, page = [], 1
        while True:
            items = _items(self._get(key, params={**(params or {}), "pageSize": 100,
                                                  "pageNumber": page}))
            out += items
            if len(items) < 100:
                return out
            page += 1

    # -- host mapping ----------------------------------------------------
    @staticmethod
    def _host(m: dict) -> NicoHost:
        labels = dict(m.get("labels") or {})
        hold = labels.pop(HOLD, None)
        image = labels.pop(IMAGE, None)
        h = _machine_to_host({**m, "labels": labels})
        if hold in HELD and h.state == NicoHostState.pool_ready:
            h.state = NicoHostState(hold)
            h.image_ref = image
        return h

    def _machine(self, host_id: str) -> dict:
        return self._get("machine_one", id=host_id)

    def _label(self, host_id: str, m: Optional[dict] = None, **set_) -> dict:
        m = m or self._machine(host_id)
        labels = {k: v for k, v in (m.get("labels") or {}).items()
                  if k not in set_ or set_[k] is not None}
        labels.update({k: v for k, v in set_.items() if v is not None})
        return self._req("PATCH", "machine_one", id=host_id, json_body={"labels": labels},
                         trace=f"machine labels {set_}", host_id=host_id)

    def _job(self, op: str, host_id: str, state: str = "succeeded", detail: str = "") -> NicoJob:
        with self._lock:
            j = NicoJob(job_id=f"rest-job-{next(self._seq):05d}", op=op, host_id=host_id,
                        state=state, detail=detail)
            self._jobs[j.job_id] = j
            return j

    # -- reads -----------------------------------------------------------
    def list_hosts(self) -> list[NicoHost]:
        return [self._host(m) for m in self._all("machine")]

    def get_host(self, host_id: str) -> NicoHost:
        return self._host(self._machine(host_id))

    # -- host-state steps ------------------------------------------------
    def reserve(self, host_id: str) -> NicoHost:
        m = self._machine(host_id)
        held = (m.get("labels") or {}).get(HOLD) in HELD
        if m.get("lifecycleState") != "ready" or held or m.get("instanceId"):
            raise HTTPException(409, f"host {host_id} not pool_ready "
                                     f"({m.get('lifecycleState')}{', held' if held else ''})")
        return self._host(self._label(host_id, m, **{HOLD: "reserved"}))

    def unreserve(self, host_id: str) -> NicoHost:
        return self._host(self._label(host_id, **{HOLD: None, IMAGE: None}))

    def abort_provision(self, host_id: str) -> NicoHost:
        return self.unreserve(host_id)

    def provision(self, host_id: str, image_ref: str) -> NicoJob:
        """NICo installs the OS as part of instance creation (operatingSystemId);
        this step records the image on the held machine."""
        self._label(host_id, **{HOLD: "provisioned", IMAGE: image_ref})
        return self._job("provision", host_id, detail=f"image={image_ref} (booted on POST /instance)")

    def _os_id(self, image: Optional[str]) -> Optional[str]:
        if not image:
            return None
        try:
            for o in _items(self._get("operating_system", params={"pageSize": 100})):
                if o.get("name") == image or image in str(o.get("imageUrl") or ""):
                    return o["id"]
        except HTTPException:
            pass
        return None

    def _tenant_vpc(self, tenant: str, site: str) -> dict:
        for v in self._all("vpc", {"tenantId": tenant, "siteId": site}):
            if (v.get("labels") or {}).get(TENANT) == tenant:
                return v
        return self._req("POST", "vpc", json_body={
            "name": f"vpc-{tenant}", "siteId": site, "tenantId": tenant,
            "networkVirtualizationType": "FNN", "labels": {TENANT: tenant}},
            trace=f"tenant VPC for {tenant}")

    def _ensure_allocation(self, tenant: str, site: str, it_id: str) -> None:
        used = self._total("instance", {"tenantId": tenant, "siteId": site})
        for a in self._all("allocation", {"tenantId": tenant, "siteId": site}):
            for c in a.get("allocationConstraints") or []:
                if c.get("resourceTypeId") == it_id:
                    if int(c.get("constraintValue") or 0) < used + 1:
                        self._req("PATCH", "allocation_constraint", id=a["id"], cid=c["id"],
                                  json_body={"constraintValue": used + 1},
                                  trace=f"allocation {a['name']} → {used + 1}")
                    return
        self._req("POST", "allocation", json_body={
            "name": f"nocp-{tenant}-{it_id[:8]}", "tenantId": tenant, "siteId": site,
            "description": "NeoCloud OS order capacity",
            "allocationConstraints": [{"resourceType": "InstanceType", "resourceTypeId": it_id,
                                       "constraintType": "Reserved",
                                       "constraintValue": used + 1}]},
            trace=f"allocation for {tenant}")

    def allocate(self, host_id: str, tenant_ref: str) -> NicoHost:
        m = self._machine(host_id)
        site, it_id = m.get("siteId"), m.get("instanceTypeId")
        vpc = self._tenant_vpc(tenant_ref, site)
        self._ensure_allocation(tenant_ref, site, it_id)
        body = {"name": f"{tenant_ref}-{host_id}", "tenantId": tenant_ref,
                "instanceTypeId": it_id, "vpcId": vpc["id"], "machineId": host_id,
                "labels": {TENANT: tenant_ref}}
        os_id = self._os_id((m.get("labels") or {}).get(IMAGE))
        if os_id:
            body["operatingSystemId"] = os_id
        ib = (vpc.get("labels") or {}).get("nocp.ibPartitionId")
        if ib:                                     # expansion into a live segment
            body["infinibandInterfaces"] = [{"partitionId": ib, "isPhysical": True}]
        inst = self._req("POST", "instance", json_body=body, host_id=host_id,
                         trace=f"instance for {tenant_ref} on {host_id}")
        if ib:
            self._req("PATCH", "instance_one", id=inst["id"], host_id=host_id,
                      json_body={"infinibandInterfaces": body["infinibandInterfaces"]},
                      trace="bind DPU to tenant IB partition")
        m = self._label(host_id, **{HOLD: None, IMAGE: None})
        h = self._host(m)
        h.state, h.instance_id, h.tenant_ref = NicoHostState.allocated, inst["id"], tenant_ref
        return h

    def release(self, instance_id: str) -> NicoJob:
        inst = self._get("instance_one", id=instance_id)
        mid = inst.get("machineId") or ""
        self._req("DELETE", "instance_one", id=instance_id, host_id=mid,
                  trace=f"release instance {instance_id}")
        hist = self._get("instance_history", id=instance_id)
        if not any(h.get("status") == "Terminating" for h in hist):
            raise HTTPException(502, f"instance {instance_id} not terminating after DELETE")
        with self._lock:
            self._released[mid] = (instance_id, inst.get("vpcId"))
        return self._job("release", mid, detail="DELETE accepted → Terminating")

    def sanitize(self, host_id: str) -> NicoJob:
        return self._poll_sanitize(self._job("sanitize", host_id, state="running",
                                             detail="+".join(SANITIZE_STEPS)))

    def _poll_sanitize(self, job: NicoJob) -> NicoJob:
        m = self._machine(job.host_id)
        lc = m.get("lifecycleState")
        if lc == "ready" and not m.get("instanceId"):
            with self._lock:
                rel = self._released.pop(job.host_id, None)
            if rel:
                hist = self._get("instance_history", id=rel[0])
                job.detail = (hist[-1].get("message") if hist else "") or job.detail
                if rel[1]:
                    self._gc_vpc(rel[1])
            job.state = "succeeded"
        elif lc in ("failed", "assigned"):
            job.state, job.detail = "failed", f"machine {lc} after release"
        return job

    def get_job(self, job_id: str) -> NicoJob:
        with self._lock:
            j = self._jobs.get(job_id)
        if j is None:
            raise HTTPException(404, f"job {job_id} not found")
        if j.op == "sanitize" and j.state == "running":
            time.sleep(_poll_s())
            self._poll_sanitize(j)
        return j

    def get_sanitize_report(self, host_id: str) -> SanitizeReport:
        hist = self._get("machine_history", id=host_id)
        statuses = [h.get("status") for h in hist]
        last_reset = max((i for i, s in enumerate(statuses) if s == "Reset"), default=-1)
        ok = last_reset >= 0 and "Ready" in statuses[last_reset:]
        return SanitizeReport(host_id=host_id, passed=ok,
                              steps=[{"step": s, "ok": ok} for s in SANITIZE_STEPS])

    def cordon(self, host_id: str, reason: str = "") -> NicoHost:
        return self._host(self._req("PATCH", "machine_one", id=host_id, host_id=host_id,
                                    json_body={"maintenanceMessage": reason or "cordoned by NOCP"},
                                    trace=f"cordon {reason}"))

    # -- tenant segments (VPC + partitions) --------------------------------
    @staticmethod
    def _segment(v: dict) -> NicoSegment:
        lb = v.get("labels") or {}
        l3 = int(lb.get("nocp.l3vni") or v.get("vni") or 0)
        pkey = lb.get(PKEY_LABEL)
        return NicoSegment(
            segment_id=v["id"], tenant_ref=lb.get(TENANT) or v.get("tenantId") or "",
            vrf=lb.get("vrf") or "", l3vni=l3,
            converged_vni=int(lb.get("nocp.convergedVni") or 0),
            vrf_dataplane=f"vpc_{l3}",
            host_ids=[h for h in (lb.get("nocp.hosts") or "").split(",") if h],
            allocation_id=lb.get("nocp.allocationId") or None,
            ib_pkey=int(pkey, 16) if pkey else None,
            state="active" if lb.get(SEG) == "active" else "deleting")

    def _patch_vpc(self, v: dict, trace: str, **labels) -> dict:
        lb = {**(v.get("labels") or {}), **{k: str(x) for k, x in labels.items()}}
        body = {"labels": lb}
        if labels.get("nocp.nvlPartitionId"):
            body["nvLinkLogicalPartitionId"] = labels["nocp.nvlPartitionId"]
        return self._req("PATCH", "vpc_one", id=v["id"], json_body=body, trace=trace)

    def _vpc_instances(self, vpc_id: str) -> list:
        return self._all("instance", {"vpcId": vpc_id})

    def create_segment(self, tenant_ref: str, vrf: str, l3vni: int,
                       converged_vni: int, host_ids: list,
                       allocation_id: Optional[str] = None,
                       ib_pkey: Optional[int] = None) -> NicoSegment:
        site = self._machine(host_ids[0]).get("siteId") if host_ids else None
        if not site:
            raise HTTPException(400, "segment needs at least one host")
        v = self._tenant_vpc(tenant_ref, site)
        lb = v.get("labels") or {}
        ib_id = lb.get("nocp.ibPartitionId")
        if not ib_id:
            ib = self._req("POST", "ib_partition", json_body={
                "name": f"ib-{tenant_ref}", "siteId": site, "tenantId": tenant_ref,
                "labels": {TENANT: tenant_ref,
                           **({PKEY_LABEL: hex(ib_pkey)} if ib_pkey else {})}},
                trace=f"IB partition P_Key {hex(ib_pkey) if ib_pkey else 'auto'}")
            ib_id, got = ib["id"], ib.get("partitionKey")
            nvl = self._req("POST", "nvlink_partition", json_body={
                "name": f"nvl-{tenant_ref}", "siteId": site, "tenantId": tenant_ref},
                trace="NVLink logical partition")
            sx = self._req("POST", "spectrumx_partition", json_body={
                "name": f"sx-{tenant_ref}", "siteId": site, "tenantId": tenant_ref,
                "vni": converged_vni, "labels": {TENANT: tenant_ref}},
                trace=f"Spectrum-X partition VNI {converged_vni}")
            extra = {"nocp.ibPartitionId": ib_id, "nocp.nvlPartitionId": nvl["id"],
                     "nocp.sxPartitionId": sx["id"], PKEY_LABEL: got}
        else:
            extra = {}
        hosts = sorted(set(host_ids) | {h for h in (lb.get("nocp.hosts") or "").split(",") if h})
        v = self._patch_vpc(v, f"segment {vrf} L3VNI {l3vni}", **{
            SEG: "active", "vrf": vrf, "nocp.l3vni": l3vni,
            "nocp.convergedVni": converged_vni, "nocp.hosts": ",".join(hosts),
            "nocp.allocationId": allocation_id or "", **extra})
        mine = set(host_ids)
        for inst in self._vpc_instances(v["id"]):
            if inst.get("machineId") in mine and not any(
                    f.get("partitionId") == ib_id for f in inst.get("infinibandInterfaces") or []):
                self._req("PATCH", "instance_one", id=inst["id"], host_id=inst["machineId"],
                          json_body={"infinibandInterfaces": [{"partitionId": ib_id,
                                                               "isPhysical": True}]},
                          trace="bind DPU to tenant IB partition")
        return self._segment(v)

    def list_segments(self) -> list:
        return [self._segment(v) for v in self._all("vpc")
                if (v.get("labels") or {}).get(SEG)]

    def attach_hosts(self, segment_id: str, host_ids: list,
                     purpose: str = "converged") -> NicoSegment:
        v = self._get("vpc_one", id=segment_id)
        lb = v.get("labels") or {}
        cur = [h for h in (lb.get("nocp.hosts") or "").split(",") if h]
        added = [h for h in host_ids if h not in cur]
        sx = lb.get("nocp.sxPartitionId")
        if sx and added:
            want = set(added)
            for inst in self._vpc_instances(v["id"]):
                if inst.get("machineId") in want:
                    self._req("PATCH", "instance_one", id=inst["id"], host_id=inst["machineId"],
                              json_body={"spectrumXAttachments": [
                                  {"spectrumXPartitionId": sx, "device": "BlueField-4",
                                   "deviceInstance": 0, "attachmentType": "Physical"}]},
                              trace=f"{purpose} network attach")
        v = self._patch_vpc(v, f"attach {len(added)} {purpose} hosts",
                            **{"nocp.hosts": ",".join(cur + added)})
        return self._segment(v)

    def delete_segment(self, segment_id: str) -> NicoSegment:
        """Mark the tenant segment released. Partitions are deleted only after
        the VPC's last instance is DELETEd and its machine cleanup is done
        (B2-13) — immediately if the VPC is already empty."""
        v = self._get("vpc_one", id=segment_id)
        v = self._patch_vpc(v, "segment release requested", **{SEG: "deleting"})
        seg = self._segment(v)
        self._gc_vpc(segment_id)
        return seg

    def _gc_vpc(self, vpc_id: str) -> bool:
        try:
            v = self._get("vpc_one", id=vpc_id)
        except HTTPException as e:
            if e.status_code == 404:
                return True
            raise
        lb = v.get("labels") or {}
        if lb.get(SEG) != "deleting" or self._vpc_instances(vpc_id):
            return False
        for key, label in (("ib_partition_one", "nocp.ibPartitionId"),
                           ("spectrumx_partition_one", "nocp.sxPartitionId")):
            if lb.get(label):
                self._del(key, lb[label])
        self._req("DELETE", "vpc_one", id=vpc_id, trace=f"tenant VPC {vpc_id} deleted")
        if lb.get("nocp.nvlPartitionId"):         # VPC held the reference
            self._del("nvlink_partition_one", lb["nocp.nvlPartitionId"])
        tenant, site = lb.get(TENANT), v.get("siteId")
        if tenant and not any((x.get("labels") or {}).get(TENANT) == tenant
                              for x in self._all("vpc", {"tenantId": tenant, "siteId": site})):
            for a in self._all("allocation", {"tenantId": tenant, "siteId": site}):
                if str(a.get("name", "")).startswith(f"nocp-{tenant}-"):
                    self._del("allocation_one", a["id"])
        return True

    def _del(self, key: str, oid: str) -> None:
        try:
            self._req("DELETE", key, id=oid, trace=f"{key.removesuffix('_one')} {oid} deleted")
        except HTTPException as e:
            if e.status_code != 404:
                raise
