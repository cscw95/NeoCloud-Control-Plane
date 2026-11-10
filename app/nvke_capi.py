"""F11 NVKE depth — Cluster API + CAPNICo objects, IMEX ComputeDomain, Topograph labels.

Each NVKE cluster gets an emulated management-cluster view built from its node
pools (app/nvke.py owns the NICo instances; this module derives and keeps the
K8s objects and their phase history):

  Cluster (cluster.x-k8s.io/v1beta2) ── infrastructureRef → NicoCluster
  MachineDeployment per (node pool, rack)  — template.spec.failureDomain = rack
    └ MachineSet → Machine ── infrastructureRef → NicoMachine (= NICo instance)
  ComputeDomain (resource.nvidia.com/v1beta1) per (GPU pool, rack) = IMEX domain
  Node labels: nvidia.com/gpu.clique, topology.kubernetes.io/zone, Topograph tiers

Upstream sources (ref/upstream/UPSTREAM-REFS.md):
  - CAPI v1.14.4 @35c6caebb004: core/config/crd/bases/cluster.x-k8s.io_*.yaml,
    api/core/v1beta2/machine_phase_types.go (Machine phases),
    core/reconcilers/machine/machine_controller_phases.go (Pending → Provisioning
    → Provisioned → Running), machine_controller_noderef.go (nodeRef by providerID),
    machine_controller.go (delete: drain → Deleting → Deleted).
  - CAPNICo v0.1.0 @25f6fd3b807a: controllers/nicomachine_controller.go (lookup by
    name, then POST /v2/org/{org}/nico/instance), controllers/failuredomain.go,
    internal/nico/client.go (ProviderIDPrefix "nico://").
  - k8s-dra-driver-gpu v25.12.0 @e775fdb63e59: crds/resource.nvidia.com_computedomains.yaml,
    resource.nvidia.com_computedomaincliques.yaml.
  - Topograph v1.1.0 @3ca60d7eac21: docs/api.md (POST /v1/generate → 202 + uid,
    GET /v1/topology?uid=, POST /v1/lookup), docs/architecture.md (k8s engine
    writes fabric tier labels closest-first + accelerator.topograph.run/domain;
    `fabricLabels` overrides the tier keys).
  - NVKE v0.23.1 @333c0db74d8d: agent/pkg/agent/proto/node_agent.proto
    (ExecuteKubeadmJoin, GetSpireAgentStatus, KubeadmUpgradeApply).

Emulator simplifications: phases advance synchronously inside one request; an
in-place K8s upgrade keeps the NICo instance (v1beta2 `Updating` phase) instead
of re-imaging into a new instance id.
"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import trace
from .store import STORE

router = APIRouter(prefix="/api/v1/nvke", tags=["nvke-capi"])
topograph_router = APIRouter(prefix="/api/v1/topograph", tags=["topograph"])

CAPI_V = "cluster.x-k8s.io/v1beta2"
INFRA_V = "infrastructure.cluster.x-k8s.io/v1alpha1"
DRA_V = "resource.nvidia.com/v1beta1"
NS = "nvke-system"
PROVIDER_PREFIX = "nico://"                       # CAPNICo internal/nico/client.go
ZONE_LABEL = "topology.kubernetes.io/zone"       # CAPNICo failureDomainLabelKey default
CLIQUE_LABEL = "nvidia.com/gpu.clique"           # GFD / DRA ComputeDomain clique
LEAF_LABEL = "network.topology.nvidia.com/ib-leaf"
SPINE_LABEL = "network.topology.nvidia.com/ib-spine"
ACCEL_LABEL = "accelerator.topograph.run/domain"  # Topograph k8s engine default
FABRIC_LABELS = [LEAF_LABEL, SPINE_LABEL]         # Topograph engine param fabricLabels
NVL_NS = uuid.UUID("6f1d8a52-6b1e-4c1a-9b7e-0c2a5e3f9d11")
CLIQUE_ID = "1"   # one NVLink partition per tenant-dedicated NVL72 rack


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _org() -> str:
    return os.environ.get("NOCP_NICO_ORG", "neocloud")


def _h(*parts: str, n: int = 5) -> str:
    return hashlib.sha1("/".join(parts).encode()).hexdigest()[:n]


def _short(rack_id: str) -> str:
    return rack_id.rsplit("-rack-", 1)[-1] if "-rack-" in rack_id else rack_id


def _cond(ctype: str, status: bool, reason: str, msg: str = "") -> dict:
    return {"type": ctype, "status": "True" if status else "False",
            "reason": reason, "message": msg, "lastTransitionTime": _now()}


def _meta(name: str, cluster: str, labels: Optional[dict] = None) -> dict:
    return {"name": name, "namespace": NS,
            "labels": {"cluster.x-k8s.io/cluster-name": cluster, **(labels or {})},
            "creationTimestamp": _now()}


# ---------------------------------------------------------------------------
# Topology facts (NOCP inventory = AI-Infra fabric source of truth)
# ---------------------------------------------------------------------------
def rack_of(node) -> str:
    tray = STORE.trays.get(getattr(node, "tray_id", "") or "")
    if tray is not None:
        return tray.rack_id
    return "cpu-pool"


def _site_tag(rack_id: str) -> str:
    rack = STORE.racks.get(rack_id)
    if rack is None:
        return "site"
    for f in STORE.factories.values():
        for bid in f.block_ids:
            for did in STORE.blocks[bid].du_ids:
                if rack.su_id in STORE.dus[did].su_ids:
                    return (f.site.split()[-1] if f.site else f.id).lower()
    return "site"


def nvlink_domain_id(rack_id: str) -> str:
    """NVLink domain (cluster) UUID of an NVL72 rack — stable per rack."""
    return str(uuid.uuid5(NVL_NS, rack_id))


def clique_of(rack_id: str) -> Optional[str]:
    if rack_id not in STORE.racks:
        return None
    return f"{nvlink_domain_id(rack_id)}.{CLIQUE_ID}"


def fabric_of(rack_id: str) -> dict:
    rack = STORE.racks.get(rack_id)
    if rack is None:
        return {}
    tag = _site_tag(rack_id)
    return {LEAF_LABEL: f"leaf-{rack.su_id}", SPINE_LABEL: f"{tag}-ib-core"}


def node_labels(cluster, pool, node) -> dict:
    rack = rack_of(node)
    labels = {"kubernetes.io/hostname": node.name,
              "node.kubernetes.io/instance-type": pool.instance_type,
              "nvke.nvidia.com/nodepool": pool.name,
              ZONE_LABEL: rack, **pool.labels}
    clique = clique_of(rack)
    if clique and pool.instance_type != "cpu-epyc":
        labels[CLIQUE_LABEL] = clique
        labels[ACCEL_LABEL] = nvlink_domain_id(rack)
        labels["nvidia.com/gpu.present"] = "true"
    labels.update(fabric_of(rack))
    return labels


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class _CapiStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.clusters: dict[str, dict] = {}
        self.topograph: dict[str, dict] = {}

    def reset(self) -> None:
        with self.lock:
            self.clusters.clear()
            self.topograph.clear()


CAPI = _CapiStore()


def _state(cid: str) -> dict:
    return CAPI.clusters.setdefault(cid, {
        "cluster": None, "nico_cluster": None, "mds": {}, "mss": {}, "machines": {},
        "nico_machines": {}, "by_host": {}, "compute_domains": {}, "history": [],
        "joins": {}})


def _log(st: dict, kind: str, name: str, phase: str, detail: str = "") -> None:
    st["history"].append({"at": _now(), "kind": kind, "name": name,
                          "phase": phase, "detail": detail})
    del st["history"][:-400]


def _emit(title: str, detail: str, dst: str = "NICo.APIService", **payload) -> None:
    trace.emit("NVKE.CAPI", dst, "REST", title, detail, payload=payload)


def _live_nodes(c):
    from .nvke import NVKE
    for pid in c.nodepool_ids:
        pool = NVKE.nodepools.get(pid)
        if pool is None:
            continue
        for n in pool.nodes:
            if n.state != "removed":
                yield pool, n


def _md_name(c, pool, rack: str) -> str:
    return f"{c.id}-{pool.name}-{_short(rack)}"


# ---------------------------------------------------------------------------
# Machine lifecycle (machine_controller_phases.go order)
# ---------------------------------------------------------------------------
def _create_machine(st: dict, c, pool, node, md: str, ms: str, rack: str) -> None:
    mname = f"{ms}-{_h(node.nico_host_id, node.instance_id or '')}"
    infra = f"{mname}-nico"
    mspec = {"bootstrap": {"dataSecretName": f"{mname}-bootstrap"},
                    "clusterName": c.id,
                    "infrastructureRef": {"apiGroup": "infrastructure.cluster.x-k8s.io",
                                          "kind": "NicoMachine", "name": infra},
                    "failureDomain": rack, "version": node.k8s_version}
    m = {"apiVersion": CAPI_V, "kind": "Machine",
         "metadata": _meta(mname, c.id, {"cluster.x-k8s.io/deployment-name": md,
                                         "cluster.x-k8s.io/set-name": ms,
                                         "nvke.nvidia.com/nodepool": pool.name}),
         "spec": mspec,
         "status": {"phase": "Pending", "failureDomain": rack,
                    "initialization": {"bootstrapDataSecretCreated": True,
                                       "infrastructureProvisioned": False},
                    "conditions": [_cond("Ready", False, "WaitingForInfrastructure")]}}
    st["machines"][mname] = m
    st["by_host"][node.nico_host_id] = mname
    _log(st, "Machine", mname, "Pending", f"MachineSet {ms} created Machine (failureDomain={rack})")

    # CAPNICo NicoMachine reconcile: lookup by name (idempotent) → create instance.
    req = {"name": node.name, "tenantId": c.tenant_id, "instanceTypeId": pool.instance_type,
           "vpcId": f"vpc-{c.tenant_id}", "machineId": node.nico_host_id,
           "userData": f"#cloud-config\n# nvke bootstrap {mname}\n",
           "labels": {ZONE_LABEL: rack, "nvke.nvidia.com/cluster": c.id}}
    nm = {"apiVersion": INFRA_V, "kind": "NicoMachine",
          "metadata": _meta(infra, c.id, {"cluster.x-k8s.io/deployment-name": md}),
          "spec": {"instanceTypeID": pool.instance_type, "vpcID": req["vpcId"],
                   "machineID": node.nico_host_id, "failureDomainLabelKey": ZONE_LABEL,
                   "interfaces": [{"subnetID": f"subnet-{c.tenant_id}", "physical": True}],
                   "providerID": f"{PROVIDER_PREFIX}{node.instance_id}"},
          "status": {"ready": False, "instanceID": node.instance_id,
                     "machineID": node.nico_host_id, "failureDomain": rack,
                     "initialization": {"provisioned": False}, "conditions": []},
          "x-neocloud": {"nico_requests": [
              {"method": "GET", "path": f"/v2/org/{_org()}/nico/instance?name={node.name}",
               "status": 200, "result": "not found → create"},
              {"method": "POST", "path": f"/v2/org/{_org()}/nico/instance", "body": req,
               "status": 201, "result": {"id": node.instance_id, "status": "Pending"}},
              {"method": "GET", "path": f"/v2/org/{_org()}/nico/instance/{node.instance_id}",
               "status": 200, "result": {"id": node.instance_id, "status": "Ready"}}]}}
    st["nico_machines"][infra] = nm
    m["status"]["phase"] = "Provisioning"
    _log(st, "Machine", mname, "Provisioning",
         f"CAPNICo POST /v2/org/{_org()}/nico/instance (machineId {node.nico_host_id})")
    _emit("CAPNICo create instance", f"{infra} → NICo instance {node.instance_id}",
          cluster=c.id, machine=mname, body=req)

    # Instance Ready → providerID → NicoMachine ready → Provisioned.
    nm["status"].update({"ready": True, "initialization": {"provisioned": True},
                         "addresses": [{"type": "InternalIP", "address": node.host_ip or "0.0.0.0"},
                                       {"type": "Hostname", "address": node.name}],
                         "tpmEkPubHash": _h("ek", node.nico_host_id, n=64)})
    nm["status"]["conditions"] = [_cond("Ready", True, "InstanceReady")]
    m["spec"]["providerID"] = f"{PROVIDER_PREFIX}{node.instance_id}"
    m["status"]["initialization"]["infrastructureProvisioned"] = True
    m["status"]["addresses"] = nm["status"]["addresses"]
    m["status"]["phase"] = "Provisioned"
    _log(st, "Machine", mname, "Provisioned", f"providerID {m['spec']['providerID']}")

    # nvke-agent join (node_agent.proto) → kubelet registers → nodeRef → Running.
    spiffe = f"spiffe://{c.id}.nvke.neocloud.local/node/{node.name}"
    st["joins"][node.nico_host_id] = {
        "agent": "nvke-agent", "state": "Joined", "spiffe_id": spiffe,
        "steps": [
            {"step": "tpm-attestation", "source": "NICo measured-boot",
             "status": "verified", "tpm_ek_pub_hash": nm["status"]["tpmEkPubHash"]},
            {"step": "spire-agent", "rpc": "NodeAgent.GetSpireAgentStatus",
             "status": "healthy", "spiffe_id": spiffe},
            {"step": "kubeadm-join", "rpc": "NodeAgent.ExecuteKubeadmJoin",
             "status": "succeeded"},
            {"step": "kubelet-registered", "status": "succeeded", "node": node.name}]}
    m["status"]["nodeRef"] = {"name": node.name}
    m["status"]["nodeInfo"] = {
        "architecture": "arm64" if pool.instance_type != "cpu-epyc" else "amd64",
        "bootID": _h("boot", mname, n=32), "containerRuntimeVersion": "containerd://2.1.4",
        "kernelVersion": "6.8.0-nvidia", "kubeProxyVersion": node.k8s_version,
        "kubeletVersion": node.k8s_version, "machineID": _h("mid", node.nico_host_id, n=32),
        "operatingSystem": "linux", "osImage": pool.node_image,
        "systemUUID": str(uuid.uuid5(NVL_NS, node.nico_host_id))}
    m["status"]["phase"] = "Running"
    m["status"]["conditions"] = [_cond("Ready", True, "NodeReady"),
                                 _cond("NodeHealthy", True, "NodeReady")]
    _log(st, "Machine", mname, "Running", f"nodeRef {node.name} (nvke-agent ExecuteKubeadmJoin)")


def _delete_machine(st: dict, mname: str, reason: str) -> None:
    m = st["machines"].pop(mname, None)
    if m is None:
        return
    node = (m["status"].get("nodeRef") or {}).get("name", "")
    m["status"]["phase"] = "Deleting"
    m["status"]["deletion"] = {"nodeDrainStartTime": _now()}
    _log(st, "Machine", mname, "Deleting", f"cordon + drain {node} ({reason})")
    infra = m["spec"]["infrastructureRef"]["name"]
    nm = st["nico_machines"].pop(infra, None)
    if nm is not None:
        _log(st, "NicoMachine", infra, "Deleted",
             f"CAPNICo DELETE /v2/org/{_org()}/nico/instance/{nm['status'].get('instanceID')}")
    _log(st, "Machine", mname, "Deleted", "node removed from cluster")
    for host, name in list(st["by_host"].items()):
        if name == mname:
            del st["by_host"][host]
            st["joins"].pop(host, None)


# ---------------------------------------------------------------------------
# Reconcile (called by app/nvke.py after every mutation)
# ---------------------------------------------------------------------------
def reconcile(c, reason: str = "reconcile") -> dict:
    with CAPI.lock:
        st = _state(c.id)
        if st["cluster"] is None:
            st["nico_cluster"] = {
                "apiVersion": INFRA_V, "kind": "NicoCluster",
                "metadata": _meta(c.id, c.id),
                "spec": {"siteID": "site-" + _site_tag(next(
                    (rack_of(n) for _, n in _live_nodes(c)), "")),
                         "failureDomainLabelKey": ZONE_LABEL},
                "status": {"ready": True, "initialization": {"provisioned": True},
                           "failureDomains": [], "conditions": [_cond("Ready", True, "SiteReady")]}}
            st["cluster"] = {
                "apiVersion": CAPI_V, "kind": "Cluster", "metadata": _meta(c.id, c.id),
                "spec": {"infrastructureRef": {"apiGroup": "infrastructure.cluster.x-k8s.io",
                                               "kind": "NicoCluster", "name": c.id},
                         "controlPlaneRef": {"apiGroup": "controlplane.nvke.nvidia.com",
                                             "kind": "NVKEControlPlane", "name": c.id},
                         "controlPlaneEndpoint": {"host": f"{c.id}.nvke.neocloud.local",
                                                  "port": 6443}},
                "status": {"phase": "Pending", "conditions": []}}
            _log(st, "Cluster", c.id, "Pending", "NVKECluster → CAPI Cluster created")
            st["cluster"]["status"]["phase"] = "Provisioning"
            _log(st, "Cluster", c.id, "Provisioning", "NicoCluster ready, control plane starting")

        desired: dict[str, tuple] = {}
        for pool, node in _live_nodes(c):
            rack = rack_of(node)
            md = _md_name(c, pool, rack)
            desired.setdefault(md, (pool, rack, []))[2].append(node)

        live_hosts = {n.nico_host_id for _, n in _live_nodes(c)}
        for host, mname in list(st["by_host"].items()):
            if host not in live_hosts:
                _delete_machine(st, mname, reason)

        for md, (pool, rack, nodes) in desired.items():
            ms = f"{md}-{_h(pool.id)}"
            if md not in st["mds"]:
                st["mds"][md] = {
                    "apiVersion": CAPI_V, "kind": "MachineDeployment",
                    "metadata": _meta(md, c.id, {"nvke.nvidia.com/nodepool": pool.name}),
                    "spec": {"clusterName": c.id, "replicas": 0,
                             "selector": {"matchLabels": {
                                 "cluster.x-k8s.io/deployment-name": md}},
                             "rollout": {"strategy": {"type": "RollingUpdate",
                                                      "rollingUpdate": {"maxSurge": 1,
                                                                        "maxUnavailable": 0}}},
                             "template": {"metadata": {"labels": {
                                 "cluster.x-k8s.io/deployment-name": md}},
                                 "spec": {"clusterName": c.id,
                                          "bootstrap": {"dataSecretName": f"{md}-bootstrap"},
                                          "infrastructureRef": {
                                              "apiGroup": "infrastructure.cluster.x-k8s.io",
                                              "kind": "NicoMachineTemplate", "name": f"{md}-tmpl"},
                                          "failureDomain": rack,
                                          "version": pool.k8s_version}}},
                    "status": {"phase": "ScalingUp"}}
                st["mss"][ms] = {"apiVersion": CAPI_V, "kind": "MachineSet",
                                 "metadata": _meta(ms, c.id, {
                                     "cluster.x-k8s.io/deployment-name": md}),
                                 "spec": {"clusterName": c.id, "replicas": 0,
                                          "selector": st["mds"][md]["spec"]["selector"],
                                          "template": st["mds"][md]["spec"]["template"]}}
                _log(st, "MachineDeployment", md, "ScalingUp",
                     f"pool {pool.name} · failureDomain {rack}")
            mdo = st["mds"][md]
            for node in nodes:
                if node.nico_host_id not in st["by_host"]:
                    _create_machine(st, c, pool, node, md, ms, rack)
            n = len(nodes)
            mdo["spec"]["replicas"] = n
            mdo["spec"]["template"]["spec"]["version"] = pool.k8s_version
            up = sum(1 for m in st["machines"].values()
                     if m["metadata"]["labels"].get("cluster.x-k8s.io/deployment-name") == md
                     and m["spec"]["version"] == pool.k8s_version)
            mdo["status"] = {"phase": "Running", "replicas": n, "readyReplicas": n,
                             "availableReplicas": n, "upToDateReplicas": up,
                             "selector": f"cluster.x-k8s.io/deployment-name={md}",
                             "observedGeneration": 1}
            st["mss"][ms]["spec"]["replicas"] = n

        for md in [m for m in st["mds"] if m not in desired]:
            st["mds"].pop(md)
            for ms in [s for s in st["mss"] if s.startswith(md + "-")]:
                st["mss"].pop(ms)
            _log(st, "MachineDeployment", md, "Deleted", "pool or failure domain emptied")

        racks = sorted({r for _, r, _ in desired.values()})
        fds = [{"name": r, "controlPlane": False,
                "attributes": {"rack": r, "nvlink_domain": nvlink_domain_id(r)}
                if r in STORE.racks else {"rack": r}} for r in racks]
        st["nico_cluster"]["status"]["failureDomains"] = fds
        cl = st["cluster"]
        cl["status"]["failureDomains"] = [{"name": f["name"], "controlPlane": False}
                                          for f in fds]
        if c.state in ("running", "upgrading") and cl["status"]["phase"] != "Provisioned":
            cl["status"]["phase"] = "Provisioned"
            cl["status"]["conditions"] = [_cond("Available", True, "Available"),
                                          _cond("ControlPlaneInitialized", True, "Initialized")]
            _log(st, "Cluster", c.id, "Provisioned", "control plane ready (3 replicas, etcd encrypted)")
        _reconcile_compute_domains(st, c)
        return st


def drop(c) -> None:
    with CAPI.lock:
        st = CAPI.clusters.get(c.id)
        if st is None:
            return
        for mname in list(st["machines"]):
            _delete_machine(st, mname, "cluster delete")
        CAPI.clusters.pop(c.id, None)


def machine_for(cid: str, host_id: str) -> Optional[dict]:
    st = CAPI.clusters.get(cid) or {}
    name = (st.get("by_host") or {}).get(host_id)
    return st["machines"].get(name) if name else None


# ---------------------------------------------------------------------------
# In-place K8s upgrade (CAPI v1beta2 Machine phase Updating) — per MachineDeployment
# ---------------------------------------------------------------------------
def rollout(c, pool, to_version: str, max_unavailable: int = 1) -> dict:
    """Roll one node pool to `to_version`, `max_unavailable` machines at a time:
    cordon + drain → nvke-agent KubeadmUpgradeApply → kubelet → uncordon."""
    with CAPI.lock:
        st = reconcile(c, "pre-upgrade")
        from_versions = sorted({n.k8s_version for n in pool.nodes if n.state != "removed"})
        batches, batch = [], []
        for node in [n for n in pool.nodes if n.state != "removed"]:
            batch.append(node)
            if len(batch) >= max(1, max_unavailable):
                batches.append(batch)
                batch = []
        if batch:
            batches.append(batch)
        for i, b in enumerate(batches):
            for node in b:
                m = machine_for(c.id, node.nico_host_id)
                if m is None:
                    continue
                m["status"]["phase"] = "Updating"
                _log(st, "Machine", m["metadata"]["name"], "Updating",
                     f"batch {i + 1}/{len(batches)}: cordon + drain {node.name}, "
                     f"NodeAgent.KubeadmUpgradeApply {node.k8s_version} → {to_version}")
                node.k8s_version = to_version
                m["spec"]["version"] = to_version
                m["status"]["nodeInfo"]["kubeletVersion"] = to_version
                m["status"]["nodeInfo"]["kubeProxyVersion"] = to_version
                m["status"]["phase"] = "Running"
                _log(st, "Machine", m["metadata"]["name"], "Running",
                     f"{node.name} kubelet {to_version}, uncordoned")
        for mdo in st["mds"].values():
            if mdo["metadata"]["labels"].get("nvke.nvidia.com/nodepool") == pool.name:
                mdo["spec"]["rollout"]["strategy"]["rollingUpdate"] = {
                    "maxSurge": 0, "maxUnavailable": max(1, max_unavailable)}
        return {"step": f"nodepool/{pool.name}", "nodes": sum(len(b) for b in batches),
                "strategy": f"in-place rolling (maxUnavailable={max(1, max_unavailable)})",
                "batches": len(batches), "from": from_versions, "to": to_version,
                "status": "succeeded", "at": _now()}


# ---------------------------------------------------------------------------
# IMEX ComputeDomain (DRA driver CRD shape)
# ---------------------------------------------------------------------------
def _cd_obj(c, name: str, pool_name: str, racks: list[str], mode: str, auto: bool) -> dict:
    return {"apiVersion": DRA_V, "kind": "ComputeDomain",
            "metadata": {"name": name, "namespace": "default", "creationTimestamp": _now(),
                         "labels": {"nvke.nvidia.com/nodepool": pool_name}},
            "spec": {"numNodes": 0,
                     "channel": {"allocationMode": mode,
                                 "resourceClaimTemplate": {"name": f"{name}-channel"}}},
            "status": {"status": "NotReady", "nodes": []},
            "x-neocloud": {"racks": racks, "auto": auto, "nodepool": pool_name}}


def _reconcile_compute_domains(st: dict, c) -> None:
    from .nvke import NVKE
    gpu_racks: dict[str, set] = {}
    for pool, node in _live_nodes(c):
        if pool.instance_type == "cpu-epyc":
            continue
        r = rack_of(node)
        if r in STORE.racks:
            gpu_racks.setdefault(pool.name, set()).add(r)
    want = {f"{p}-{_short(r)}-imex": (p, r)
            for p, rs in gpu_racks.items() for r in rs}
    for name in [n for n, cd in st["compute_domains"].items()
                 if cd["x-neocloud"]["auto"] and n not in want]:
        st["compute_domains"].pop(name)
        _log(st, "ComputeDomain", name, "Deleted", "rack left the node pool")
    for name, (p, r) in want.items():
        if name not in st["compute_domains"]:
            st["compute_domains"][name] = _cd_obj(c, name, p, [r], "Single", True)
            _log(st, "ComputeDomain", name, "Created", f"IMEX domain = NVL72 rack {r}")
    pools = {NVKE.nodepools[p].name: NVKE.nodepools[p] for p in c.nodepool_ids
             if p in NVKE.nodepools}
    for cd in st["compute_domains"].values():
        ext = cd["x-neocloud"]
        pool = pools.get(ext["nodepool"])
        members = [n for n in (pool.nodes if pool else [])
                   if n.state != "removed" and rack_of(n) in ext["racks"]]
        members.sort(key=lambda n: (rack_of(n), n.name))
        cd["spec"]["numNodes"] = len(members)
        cd["status"]["nodes"] = [{"name": n.name, "ipAddress": n.host_ip or "0.0.0.0",
                                  "cliqueID": clique_of(rack_of(n)), "index": i,
                                  "status": "Ready"} for i, n in enumerate(members)]
        cd["status"]["status"] = "Ready" if members else "NotReady"


def _cliques(cd: dict) -> list[dict]:
    by: dict[str, list] = {}
    for n in cd["status"]["nodes"]:
        by.setdefault(n["cliqueID"], []).append(n)
    return [{"apiVersion": DRA_V, "kind": "ComputeDomainClique",
             "metadata": {"name": f"{cd['metadata']['name']}.{cid}", "namespace": "default"},
             "daemons": [{"nodeName": d["name"], "ipAddress": d["ipAddress"],
                          "cliqueID": cid, "index": i, "status": d["status"]}
                         for i, d in enumerate(ds)]} for cid, ds in sorted(by.items())]


def _imex_channels(cd: dict) -> list[dict]:
    """Channels handed to workloads: Single → one channel (0); All → the IMEX
    domain maximum (2048, IMEX `IMEX_CHANNEL_COUNT` default)."""
    n = 1 if cd["spec"]["channel"]["allocationMode"] == "Single" else 2048
    shown = min(n, 4)
    chans = [{"channel": i, "device": f"/dev/nvidia-caps-imex-channels/channel{i}"}
             for i in range(shown)]
    return [{"count": n, "devices": chans, "claim_template":
             cd["spec"]["channel"]["resourceClaimTemplate"]["name"]}]


def _cd_view(cd: dict) -> dict:
    ext = cd["x-neocloud"]
    return {"name": cd["metadata"]["name"], "nodepool": ext["nodepool"], "racks": ext["racks"],
            "auto": ext["auto"], "status": cd["status"]["status"],
            "num_nodes": cd["spec"]["numNodes"],
            "cliques": sorted({n["cliqueID"] for n in cd["status"]["nodes"]}),
            "imex_channels": _imex_channels(cd), "computeDomain": cd,
            "computeDomainCliques": _cliques(cd)}


# ---------------------------------------------------------------------------
# Routes (F11 contract)
# ---------------------------------------------------------------------------
def _cluster(cid: str):
    from .nvke import _get
    return _get(cid)


@router.get("/clusters/{cluster_id}/capi")
def get_capi(cluster_id: str, objects: bool = False) -> dict:
    c = _cluster(cluster_id)
    from .nvke import NVKE
    with NVKE.lock:
        st = reconcile(c)
        out = {
            "cluster": {"name": c.id, "phase": st["cluster"]["status"]["phase"],
                        "infrastructure": "NicoCluster",
                        "failure_domains": [f["name"] for f in
                                            st["cluster"]["status"].get("failureDomains", [])]},
            "machine_deployments": [
                {"name": n, "nodepool": md["metadata"]["labels"]["nvke.nvidia.com/nodepool"],
                 "replicas": md["spec"]["replicas"],
                 "failure_domain": md["spec"]["template"]["spec"]["failureDomain"],
                 "version": md["spec"]["template"]["spec"]["version"],
                 "phase": md["status"]["phase"],
                 "ready_replicas": md["status"]["readyReplicas"],
                 "up_to_date_replicas": md["status"]["upToDateReplicas"]}
                for n, md in sorted(st["mds"].items())],
            "machines": [
                {"name": n, "phase": m["status"]["phase"],
                 "provider_id": m["spec"].get("providerID"),
                 "node": (m["status"].get("nodeRef") or {}).get("name"),
                 "failure_domain": m["spec"]["failureDomain"], "version": m["spec"]["version"]}
                for n, m in sorted(st["machines"].items())],
            "nico_machines": [
                {"name": n, "nico_instance_id": nm["status"]["instanceID"],
                 "nico_machine_id": nm["status"]["machineID"],
                 "rack_id": nm["status"]["failureDomain"],
                 "phase": "Ready" if nm["status"]["ready"] else "Provisioning"}
                for n, nm in sorted(st["nico_machines"].items())],
            "history": st["history"][-60:],
        }
        if objects:
            out["objects"] = ([st["cluster"], st["nico_cluster"]] + list(st["mds"].values())
                              + list(st["mss"].values()) + list(st["machines"].values())
                              + list(st["nico_machines"].values()))
        return out


@router.get("/clusters/{cluster_id}/nodes")
def list_nodes(cluster_id: str) -> list:
    c = _cluster(cluster_id)
    from .nvke import NVKE
    with NVKE.lock:
        st = reconcile(c)
        out = []
        for pool, n in _live_nodes(c):
            m = machine_for(c.id, n.nico_host_id) or {}
            join = st["joins"].get(n.nico_host_id) or {}
            out.append({
                "name": n.name, "nodepool": pool.name, "nico_instance_id": n.instance_id,
                "nico_machine_id": n.nico_host_id, "rack_id": rack_of(n),
                "k8s_version": n.k8s_version,
                "provider_id": f"{PROVIDER_PREFIX}{n.instance_id}",
                "machine": (m.get("metadata") or {}).get("name"),
                "machine_phase": (m.get("status") or {}).get("phase"),
                "labels": node_labels(c, pool, n),
                "attestation": {"tpm": "verified", "spiffe_id": join.get("spiffe_id"),
                                "tpm_ek_pub_hash": next(
                                    (s.get("tpm_ek_pub_hash") for s in join.get("steps", [])
                                     if s["step"] == "tpm-attestation"), None)},
                "join": {"agent": "nvke-agent", "state": join.get("state", "Pending"),
                         "steps": join.get("steps", [])}})
        return out


class ComputeDomainCreate(BaseModel):
    name: str
    nodepool: str
    racks: list[str] = Field(default_factory=list)
    allocation_mode: str = "Single"


@router.get("/clusters/{cluster_id}/compute-domains")
def list_compute_domains(cluster_id: str) -> list:
    c = _cluster(cluster_id)
    from .nvke import NVKE
    with NVKE.lock:
        st = reconcile(c)
        return [_cd_view(cd) for _, cd in sorted(st["compute_domains"].items())]


@router.post("/clusters/{cluster_id}/compute-domains", status_code=201)
def create_compute_domain(cluster_id: str, body: ComputeDomainCreate) -> dict:
    c = _cluster(cluster_id)
    if body.allocation_mode not in ("Single", "All"):
        raise HTTPException(422, "allocation_mode must be Single or All")
    from .nvke import NVKE
    with NVKE.lock:
        st = reconcile(c)
        pool_racks = {rack_of(n) for p, n in _live_nodes(c) if p.name == body.nodepool}
        if not pool_racks:
            raise HTTPException(404, f"nodepool '{body.nodepool}' has no nodes in {cluster_id}")
        racks = body.racks or sorted(pool_racks)
        bad = [r for r in racks if r not in pool_racks or r not in STORE.racks]
        if bad:
            raise HTTPException(422, f"racks not in nodepool '{body.nodepool}': {bad}")
        if body.name in st["compute_domains"]:
            raise HTTPException(409, f"ComputeDomain '{body.name}' already exists")
        st["compute_domains"][body.name] = _cd_obj(c, body.name, body.nodepool, racks,
                                                   body.allocation_mode, False)
        _log(st, "ComputeDomain", body.name, "Created",
             f"{len(racks)} NVLink domain(s), allocationMode {body.allocation_mode}")
        _reconcile_compute_domains(st, c)
        return _cd_view(st["compute_domains"][body.name])


@router.delete("/clusters/{cluster_id}/compute-domains/{name}")
def delete_compute_domain(cluster_id: str, name: str) -> dict:
    c = _cluster(cluster_id)
    with CAPI.lock:
        st = _state(c.id)
        if st["compute_domains"].pop(name, None) is None:
            raise HTTPException(404, f"ComputeDomain '{name}' not found")
        _log(st, "ComputeDomain", name, "Deleted", "by request")
        return {"name": name, "state": "deleted"}


# ---------------------------------------------------------------------------
# Topograph (docs/api.md) — k8s engine over NVKE nodes
# ---------------------------------------------------------------------------
class TopographRequest(BaseModel):
    provider: dict = Field(default_factory=dict)
    engine: dict = Field(default_factory=dict)
    nodes: list = Field(default_factory=list)


def _request_id(body: TopographRequest) -> str:
    """uid from provider name/params + engine name/params (creds, nodes excluded)."""
    import json
    key = json.dumps({"p": body.provider.get("name", "nvke"), "pp": body.provider.get("params", {}),
                      "e": body.engine.get("name", "k8s"), "ep": body.engine.get("params", {})},
                     sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _topology(body: TopographRequest) -> dict:
    from .nvke import NVKE
    params = body.engine.get("params") or {}
    cid = (body.provider.get("params") or {}).get("cluster_id")
    fabric = params.get("fabricLabels") or FABRIC_LABELS
    accel = params.get("acceleratorLabel") or ACCEL_LABEL
    with NVKE.lock:
        clusters = [NVKE.clusters[cid]] if cid in NVKE.clusters else [
            x for x in NVKE.clusters.values() if x.state != "deleted"]
        domains: dict[str, dict] = {}
        labels: dict[str, dict] = {}
        for c in clusters:
            reconcile(c, "topograph")
            for pool, n in _live_nodes(c):
                rack = rack_of(n)
                fab = fabric_of(rack)
                tiers = [fab.get(LEAF_LABEL), fab.get(SPINE_LABEL)]
                lab = {fabric[i]: t for i, t in enumerate(tiers) if t and i < len(fabric)}
                if rack in STORE.racks and pool.instance_type != "cpu-epyc":
                    lab[accel] = nvlink_domain_id(rack)
                    domains.setdefault(nvlink_domain_id(rack), {
                        "rack": rack, "clique": clique_of(rack), "nodes": []})["nodes"].append(n.name)
                labels[n.name] = lab
    return {"engine": body.engine.get("name", "k8s"), "status": "OK",
            "acceleratorDomains": [{"id": k, **v} for k, v in sorted(domains.items())],
            "nodeLabels": labels}


@topograph_router.get("/healthz")
def topograph_health() -> dict:
    return {"status": "ok"}


@topograph_router.post("/v1/generate", status_code=202)
def topograph_generate(body: TopographRequest):
    from fastapi.responses import PlainTextResponse
    engine = body.engine.get("name", "k8s")
    if engine not in ("k8s", "slurm", "slinky", "nfd", "graph"):
        raise HTTPException(400, f"unsupported engine {engine!r}")
    uid = _request_id(body)
    with CAPI.lock:
        CAPI.topograph[uid] = {"uid": uid, "request": body.model_dump(),
                               "result": _topology(body), "at": _now()}
    _emit("Topograph generate", f"uid {uid} engine {engine}", dst="Topograph")
    return PlainTextResponse(uid, status_code=202)


@topograph_router.get("/v1/topology")
def topograph_result(uid: str) -> dict:
    rec = CAPI.topograph.get(uid)
    if rec is None:
        raise HTTPException(404, f"request {uid} not found")
    return rec["result"]


@topograph_router.post("/v1/lookup")
def topograph_lookup(body: TopographRequest) -> dict:
    return topograph_result(_request_id(body))
