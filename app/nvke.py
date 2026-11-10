"""NVKE Managed Kubernetes (DSX OS Runtime layer, dsx-ai-factory/nvke v0.23.1).

Object model follows NVKE: Cluster (dedicated control plane + etcd per tenant),
NodePool (instance type, release, K8s version), Node, Release (node image),
kubeconfig. Each NodePool node is a NICo Instance obtained through the same
ComputeAdapter seam as BMaaS (reserve → provision → allocate; scale-down and
delete run release → sanitize).

The legacy NKD clusters (`/api/v1/k8s/*`, order-driven install saga) stay as
they are; `/api/v1/nvke/clusters` lists them too, mapped onto the NVKE model
with `source: "nkd"` (read-only through this surface).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import trace
from .adapters import wait_job
from .dsx import VERSIONS
from .store import STORE

router = APIRouter(prefix="/api/v1/nvke", tags=["nvke"])

NVKE_VERSION = VERSIONS["nvke"]

# Release = NVKE node image + K8s patch + AICR recipe pin (N/N-1 policy).
RELEASES = [
    {"id": "nvke-0.23.1-k8s-1.34", "nvke_version": NVKE_VERSION, "k8s_minor": "1.34",
     "k8s_version": "v1.34.1", "node_image": "nvke-ubuntu-24.04-k8s-1.34",
     "cni": "ovn-kubernetes", "default": True},
    {"id": "nvke-0.23.1-k8s-1.33", "nvke_version": NVKE_VERSION, "k8s_minor": "1.33",
     "k8s_version": "v1.33.5", "node_image": "nvke-ubuntu-24.04-k8s-1.33",
     "cni": "ovn-kubernetes", "default": False},
]

# AICR-validated component set installed on every workload cluster.
AICR_COMPONENTS = {
    "aicr": "v1.2.0",
    "gpu-operator": "v26.7.2",
    "network-operator": "v26.10.0",
    "dra-driver-nvidia-gpu": "v0.5.1",
    "kai-scheduler": "v0.16.16",
    "nvsentinel": "v1.31.0",
    "dcgm-exporter": "4.9.0",
    "ovn-kubernetes": "bundled",
}

INSTANCE_TYPES = ("vr-nvl72", "gb300-nvl72", "gb200-nvl72", "cpu-epyc")
NODE_IMAGE_PREFIX = "nvke-ubuntu-24.04"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _release(k8s: Optional[str] = None, release_id: Optional[str] = None) -> dict:
    for r in RELEASES:
        if release_id and r["id"] == release_id:
            return r
        if k8s and k8s.lstrip("v") in (r["k8s_minor"], r["k8s_version"].lstrip("v")):
            return r
    if not k8s and not release_id:
        return next(r for r in RELEASES if r["default"])
    raise HTTPException(422, f"unsupported release/k8s version "
                             f"(supported: {[r['k8s_minor'] for r in RELEASES]})")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class NodePoolSpec(BaseModel):
    name: str
    instance_type: str = "vr-nvl72"
    count: int = Field(1, ge=0, le=72)
    labels: dict = Field(default_factory=dict)


class ClusterCreate(BaseModel):
    name: str
    tenant_id: str
    k8s_version: Optional[str] = None      # "1.34" | "v1.34.1"
    release: Optional[str] = None          # release id (wins over k8s_version)
    nodepools: list[NodePoolSpec] = Field(default_factory=list)


class ClusterUpdate(BaseModel):
    name: Optional[str] = None
    labels: Optional[dict] = None


class NodePoolUpdate(BaseModel):
    count: Optional[int] = Field(None, ge=0, le=72)
    labels: Optional[dict] = None


class UpgradeRequest(BaseModel):
    k8s_version: Optional[str] = None
    release: Optional[str] = None


class ReplaceRequest(BaseModel):
    reason: str = "nvsentinel: node unhealthy"
    source: str = "nvsentinel"


class NvkeNode(BaseModel):
    name: str
    nico_host_id: str
    tray_id: str = ""                      # physical tray (GPU pools) — rack claim
    instance_id: Optional[str] = None
    host_ip: str = ""
    state: str = "ready"                   # ready | replacing | removed
    k8s_version: str = ""


class NvkeNodePool(BaseModel):
    id: str
    cluster_id: str
    name: str
    instance_type: str
    count: int
    release: str
    k8s_version: str
    node_image: str
    labels: dict = Field(default_factory=dict)
    nodes: list[NvkeNode] = Field(default_factory=list)
    state: str = "ready"                   # ready | scaling | upgrading | deleting
    created_at: str = ""


class NvkeCluster(BaseModel):
    id: str
    name: str
    tenant_id: str
    source: str = "nvke"                   # nvke | nkd (legacy, read-only here)
    nvke_version: str = NVKE_VERSION
    release: str
    k8s_version: str
    state: str = "provisioning"            # provisioning | running | upgrading | deleted
    control_plane: dict = Field(default_factory=dict)
    cni: str = "ovn-kubernetes"
    aicr_recipe: dict = Field(default_factory=dict)
    nodepool_ids: list[str] = Field(default_factory=list)
    labels: dict = Field(default_factory=dict)
    upgrades: list[dict] = Field(default_factory=list)
    replacements: list[dict] = Field(default_factory=list)
    created_at: str = ""


class _NvkeStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.clusters: dict[str, NvkeCluster] = {}
        self.nodepools: dict[str, NvkeNodePool] = {}
        self.claimed_racks: dict[str, str] = {}   # rack_id -> cluster_id
        self._seq = 0

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self._seq += 1
            return f"{prefix}-{self._seq:04d}"

    def reset(self) -> None:
        with self.lock:
            self.clusters.clear()
            self.nodepools.clear()
            self.claimed_racks.clear()
            self._seq = 0


NVKE = _NvkeStore()


def _adapter():
    from .lifecycle import get_adapter     # late import — lifecycle imports a lot
    return get_adapter()


def _emit(title: str, detail: str, **payload) -> None:
    trace.emit("NeoCloudOS.NVKE", "NICo.APIService", "REST", title, detail,
               payload=payload)


def aicr_recipe(release: dict, instance_types: list[str]) -> dict:
    platform = ("vera-rubin-nvl72" if "vr-nvl72" in instance_types
                else (instance_types[0] if instance_types else "vera-rubin-nvl72"))
    return {"recipe": f"aicr-{AICR_COMPONENTS['aicr']}/{platform}/k8s-{release['k8s_minor']}",
            "platform": platform, "k8s": release["k8s_minor"],
            "validated": True, "components": dict(AICR_COMPONENTS)}


# ---------------------------------------------------------------------------
# NICo Instance plumbing for node pools
# ---------------------------------------------------------------------------
def _eligible(h, tenant_id: str) -> int:
    """Placement rank for a host (lower first), -1 when not usable by NVKE.

    GPU trays: NVL72 racks are tenant-dedicated (one NVLink domain per tenant),
    so a tray is eligible only on a rack the tenant already owns (rank 0) or a
    free rack (rank 1) that NVKE then claims. CPU hosts: the NOCP CPU-node pool
    entry must be pool_ready."""
    tray = STORE.trays.get(h.tray_id)
    if tray is not None:
        rack = STORE.racks.get(tray.rack_id)
        if rack is None:
            return -1
        if rack.tenant_id == tenant_id:
            return 0
        return 1 if rack.tenant_id is None and rack.state.value == "ready" else -1
    cn = _cpu_node(h.host_id)
    return 0 if cn is None or cn.state == "pool_ready" else -1


def _cpu_node(host_id: str):
    return next((c for c in STORE.cpu_nodes.values() if c.nico_host_id == host_id), None)


def _claim(host_id: str, tray_id: str, tenant_id: str, cluster_id: str) -> None:
    from .models import DPUMode
    from .tenancy import _bind_rack
    tray = STORE.trays.get(tray_id)
    if tray is not None:
        rack = STORE.racks[tray.rack_id]
        if rack.tenant_id is None:
            _bind_rack(rack.id, tenant_id, DPUMode.dpu)
            NVKE.claimed_racks[rack.id] = cluster_id
        return
    cn = _cpu_node(host_id)
    if cn is not None:
        cn.state, cn.role, cn.tenant_id = "allocated", "nvke", tenant_id


def _unclaim(node: "NvkeNode") -> None:
    from .tenancy import _release_rack
    tray = STORE.trays.get(node.tray_id)
    if tray is not None:
        rack_id = tray.rack_id
        still = any(n.state != "removed" and n is not node
                    and n.tray_id in STORE.trays
                    and STORE.trays[n.tray_id].rack_id == rack_id
                    for p in NVKE.nodepools.values() for n in p.nodes)
        if not still and NVKE.claimed_racks.pop(rack_id, None):
            _release_rack(rack_id)
        return
    cn = _cpu_node(node.nico_host_id)
    if cn is not None and cn.role == "nvke":
        cn.state, cn.role, cn.tenant_id = "pool_ready", "general", None


def _acquire_nodes(pool: NvkeNodePool, n: int, tenant_id: str) -> list[NvkeNode]:
    adapter = _adapter()
    used = {nd.nico_host_id for p in NVKE.nodepools.values() for nd in p.nodes}
    ranked = []
    for h in adapter.list_hosts():
        if (h.sku != pool.instance_type or h.state.value != "pool_ready"
                or h.cordoned or h.host_id in used):
            continue
        rank = _eligible(h, tenant_id)
        if rank >= 0:
            ranked.append((rank, h.host_id, h))
    ranked.sort(key=lambda x: (x[0], x[1]))
    free = [h for _, _, h in ranked]
    if len(free) < n:
        raise HTTPException(409, f"insufficient '{pool.instance_type}' capacity: "
                                 f"need {n}, pool_ready {len(free)}")
    got: list[NvkeNode] = []
    for h in free[:n]:
        adapter.reserve(h.host_id)
        job = wait_job(adapter, adapter.provision(h.host_id, pool.node_image))
        if job.state != "succeeded":
            raise HTTPException(502, f"nico provision failed on {h.host_id}: {job.detail}")
        host = adapter.allocate(h.host_id, tenant_id)
        _claim(host.host_id, h.tray_id, tenant_id, pool.cluster_id)
        node = NvkeNode(name=f"{pool.name}-{len(pool.nodes) + len(got)}",
                        nico_host_id=host.host_id, tray_id=h.tray_id,
                        instance_id=host.instance_id,
                        host_ip=host.host_ip, k8s_version=pool.k8s_version)
        got.append(node)
        _emit("NVKE node join", f"{node.name} ← NICo instance {host.instance_id}",
              cluster=pool.cluster_id, nodepool=pool.id, host=host.host_id)
    return got


def _return_node(node: NvkeNode, sanitize: bool = True) -> None:
    adapter = _adapter()
    if node.instance_id:
        wait_job(adapter, adapter.release(node.instance_id))
    if sanitize:
        try:
            wait_job(adapter, adapter.sanitize(node.nico_host_id))
        except HTTPException:
            pass                            # host already moved on (e.g. RMA)
    _unclaim(node)
    node.state = "removed"


def _scale(pool: NvkeNodePool, count: int, tenant_id: str) -> None:
    live = [n for n in pool.nodes if n.state != "removed"]
    if count > len(live):
        pool.nodes.extend(_acquire_nodes(pool, count - len(live), tenant_id))
    else:
        for node in live[count:]:
            _return_node(node)
        pool.nodes = [n for n in pool.nodes if n.state != "removed"]
    pool.count = count


# ---------------------------------------------------------------------------
# Legacy NKD clusters mapped onto the NVKE model
# ---------------------------------------------------------------------------
def _from_nkd(c) -> dict:
    minor = ".".join(c.version.lstrip("v").split(".")[:2]) if c.version else ""
    return {
        "id": c.id, "name": c.name or c.id, "tenant_id": c.tenant_id,
        "source": "nkd", "nvke_version": None, "nkd_version": c.nkd_version,
        "release": f"nkd-{c.nkd_version}", "k8s_version": c.version,
        "state": c.state, "cni": "cilium",
        "control_plane": {"dedicated": True, "nodes": len(c.cp_node_ids),
                          "endpoint": f"https://{c.api_vip}:6443" if c.api_vip else ""},
        "aicr_recipe": {"recipe": None, "k8s": minor, "validated": False,
                        "components": {a["name"]: a.get("version") for a in c.addons}},
        "nodepools": [{"id": f"{c.id}-workers", "name": "gpu-workers",
                       "instance_type": "gpu", "count": len(c.worker_node_ids)}],
        "order_id": c.order_id, "created_at": c.created_at,
    }


def _view(c: NvkeCluster) -> dict:
    d = c.model_dump()
    d["nodepools"] = [NVKE.nodepools[p].model_dump() for p in c.nodepool_ids
                      if p in NVKE.nodepools]
    d["node_count"] = sum(p["count"] for p in d["nodepools"])
    return d


def _get(cluster_id: str) -> NvkeCluster:
    c = NVKE.clusters.get(cluster_id)
    if c is None or c.state == "deleted":
        if cluster_id in STORE.k8s_clusters:
            raise HTTPException(409, f"'{cluster_id}' is a legacy NKD cluster — "
                                     "manage it through /api/v1/k8s")
        raise HTTPException(404, f"nvke cluster '{cluster_id}' not found")
    return c


def cluster_tenant(cluster_id: str) -> Optional[str]:
    """Owner tenant for the tenancy guard (NVKE or legacy NKD id)."""
    c = NVKE.clusters.get(cluster_id)
    if c is not None:
        return c.tenant_id
    k = STORE.k8s_clusters.get(cluster_id)
    return k.tenant_id if k is not None else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/releases")
def list_releases() -> dict:
    return {"nvke_version": NVKE_VERSION, "k8s_supported": VERSIONS["k8s"],
            "releases": RELEASES, "instance_types": list(INSTANCE_TYPES),
            "aicr_components": AICR_COMPONENTS}


@router.get("/clusters")
def list_clusters(tenant_id: Optional[str] = None,
                  include_nkd: bool = Query(True)) -> list:
    with NVKE.lock:
        out = [_view(c) for c in NVKE.clusters.values() if c.state != "deleted"]
    if include_nkd:
        out += [_from_nkd(c) for c in STORE.k8s_clusters.values()
                if c.state != "deleted"]
    if tenant_id:
        out = [c for c in out if c["tenant_id"] == tenant_id]
    return out


@router.post("/clusters", status_code=201)
def create_cluster(body: ClusterCreate) -> dict:
    if body.tenant_id not in STORE.tenants:
        raise HTTPException(404, f"tenant '{body.tenant_id}' not found")
    rel = _release(body.k8s_version, body.release)
    for np in body.nodepools:
        if np.instance_type not in INSTANCE_TYPES:
            raise HTTPException(422, f"unknown instance_type '{np.instance_type}'")
    with NVKE.lock:
        cid = NVKE.next_id("nvke")
        c = NvkeCluster(
            id=cid, name=body.name, tenant_id=body.tenant_id, release=rel["id"],
            k8s_version=rel["k8s_version"], created_at=_now(),
            control_plane={"dedicated": True, "etcd": "dedicated",
                           "etcd_encryption": True, "replicas": 3,
                           "endpoint": f"https://{cid}.nvke.neocloud.local:6443",
                           "attestation": "TPM + SPIFFE/SPIRE"},
            aicr_recipe=aicr_recipe(rel, [p.instance_type for p in body.nodepools]),
        )
        NVKE.clusters[cid] = c
        _emit("NVKE cluster create", f"{cid} ({rel['k8s_version']}) for {body.tenant_id}",
              cluster=cid, tenant=body.tenant_id)
        try:
            for spec in body.nodepools:
                _add_nodepool(c, spec)
        except HTTPException:
            for pid in list(c.nodepool_ids):        # roll back partial pools
                _delete_nodepool(c, pid)
            del NVKE.clusters[cid]
            raise
        c.state = "running"
        return _view(c)


@router.get("/clusters/{cluster_id}")
def get_cluster(cluster_id: str) -> dict:
    c = NVKE.clusters.get(cluster_id)
    if c is None or c.state == "deleted":
        k = STORE.k8s_clusters.get(cluster_id)
        if k is not None:
            return _from_nkd(k)
        raise HTTPException(404, f"nvke cluster '{cluster_id}' not found")
    return _view(c)


@router.patch("/clusters/{cluster_id}")
def update_cluster(cluster_id: str, body: ClusterUpdate) -> dict:
    c = _get(cluster_id)
    if body.name is not None:
        c.name = body.name
    if body.labels is not None:
        c.labels = body.labels
    return _view(c)


@router.delete("/clusters/{cluster_id}")
def delete_cluster(cluster_id: str) -> dict:
    with NVKE.lock:
        c = _get(cluster_id)
        for pid in list(c.nodepool_ids):
            _delete_nodepool(c, pid)
        c.state = "deleted"
        _emit("NVKE cluster delete", f"{cluster_id} — nodes returned to NICo",
              cluster=cluster_id)
        return {"id": cluster_id, "state": "deleted"}


def _add_nodepool(c: NvkeCluster, spec: NodePoolSpec) -> NvkeNodePool:
    rel = _release(release_id=c.release)
    if any(NVKE.nodepools[p].name == spec.name for p in c.nodepool_ids):
        raise HTTPException(409, f"nodepool '{spec.name}' already exists")
    pool = NvkeNodePool(
        id=NVKE.next_id("np"), cluster_id=c.id, name=spec.name,
        instance_type=spec.instance_type, count=0, release=rel["id"],
        k8s_version=c.k8s_version, node_image=rel["node_image"],
        labels=spec.labels, created_at=_now())
    NVKE.nodepools[pool.id] = pool
    c.nodepool_ids.append(pool.id)
    try:
        _scale(pool, spec.count, c.tenant_id)
    except HTTPException:
        _delete_nodepool(c, pool.id)
        raise
    return pool


def _delete_nodepool(c: NvkeCluster, pool_id: str) -> None:
    pool = NVKE.nodepools.pop(pool_id, None)
    if pool is None:
        return
    for node in pool.nodes:
        if node.state != "removed":
            _return_node(node)
    if pool_id in c.nodepool_ids:
        c.nodepool_ids.remove(pool_id)


def _pool(c: NvkeCluster, pool_id: str) -> NvkeNodePool:
    if pool_id not in c.nodepool_ids:
        raise HTTPException(404, f"nodepool '{pool_id}' not found in {c.id}")
    return NVKE.nodepools[pool_id]


@router.get("/clusters/{cluster_id}/nodepools")
def list_nodepools(cluster_id: str) -> list:
    c = _get(cluster_id)
    return [NVKE.nodepools[p].model_dump() for p in c.nodepool_ids]


@router.post("/clusters/{cluster_id}/nodepools", status_code=201)
def create_nodepool(cluster_id: str, body: NodePoolSpec) -> dict:
    if body.instance_type not in INSTANCE_TYPES:
        raise HTTPException(422, f"unknown instance_type '{body.instance_type}'")
    with NVKE.lock:
        c = _get(cluster_id)
        return _add_nodepool(c, body).model_dump()


@router.get("/clusters/{cluster_id}/nodepools/{pool_id}")
def get_nodepool(cluster_id: str, pool_id: str) -> dict:
    return _pool(_get(cluster_id), pool_id).model_dump()


@router.patch("/clusters/{cluster_id}/nodepools/{pool_id}")
def update_nodepool(cluster_id: str, pool_id: str, body: NodePoolUpdate) -> dict:
    with NVKE.lock:
        c = _get(cluster_id)
        pool = _pool(c, pool_id)
        if body.labels is not None:
            pool.labels = body.labels
        if body.count is not None and body.count != pool.count:
            _scale(pool, body.count, c.tenant_id)
        return pool.model_dump()


@router.delete("/clusters/{cluster_id}/nodepools/{pool_id}")
def delete_nodepool(cluster_id: str, pool_id: str) -> dict:
    with NVKE.lock:
        c = _get(cluster_id)
        _pool(c, pool_id)
        _delete_nodepool(c, pool_id)
        return {"id": pool_id, "state": "deleted"}


def _kubeconfig_yaml(cid: str, name: str, endpoint: str, tenant: str) -> str:
    return (
        "apiVersion: v1\nkind: Config\n"
        f"clusters:\n- name: {name}\n  cluster:\n    server: {endpoint}\n"
        "    certificate-authority-data: LS0tLS1CRUdJTi1FTVVMQVRFRC1DQS0tLS0t\n"
        f"contexts:\n- name: {name}\n  context:\n    cluster: {name}\n"
        f"    user: {tenant}-oidc\n"
        f"current-context: {name}\n"
        f"users:\n- name: {tenant}-oidc\n  user:\n    exec:\n"
        "      apiVersion: client.authentication.k8s.io/v1\n"
        "      command: kubectl\n"
        "      args: [oidc-login, get-token, "
        f"--oidc-issuer-url=https://oidc.neocloud.local/{tenant}, "
        f"--oidc-client-id={cid}]\n")


def require_owner(request, tenant_id: str) -> None:
    """Credentials are released only to the owning tenant (never the operator)."""
    who = getattr(request.state, "identity", None)
    if who is None or who.role != "tenant":
        raise HTTPException(403, "kubeconfig is only released to the owning tenant")
    if who.tenant_id != tenant_id:
        raise HTTPException(403, "tenant scope violation")


@router.get("/clusters/{cluster_id}/kubeconfig")
def kubeconfig(cluster_id: str, request: Request, format: str = Query("json")):
    c = NVKE.clusters.get(cluster_id)
    if c is not None and c.state != "deleted":
        name, endpoint, tenant = c.name, c.control_plane["endpoint"], c.tenant_id
    elif cluster_id in STORE.k8s_clusters:
        k = STORE.k8s_clusters[cluster_id]
        name, tenant = k.name or k.id, k.tenant_id
        endpoint = f"https://{k.api_vip or k.id}:6443"
    else:
        raise HTTPException(404, f"nvke cluster '{cluster_id}' not found")
    require_owner(request, tenant)
    text = _kubeconfig_yaml(cluster_id, name, endpoint, tenant)
    if format == "yaml":
        return PlainTextResponse(text, media_type="application/yaml", headers={
            "Content-Disposition": f'attachment; filename="{cluster_id}-kubeconfig.yaml"'})
    return {"cluster_id": cluster_id, "format": "yaml", "kubeconfig": text,
            "auth": "oidc", "expires_in_s": 3600}


@router.post("/clusters/{cluster_id}/upgrade")
def upgrade(cluster_id: str, body: UpgradeRequest) -> dict:
    with NVKE.lock:
        c = _get(cluster_id)
        rel = _release(body.k8s_version, body.release)
        cur = _release(release_id=c.release)
        if rel["id"] == cur["id"]:
            raise HTTPException(409, f"already on {rel['id']}")
        if tuple(map(int, rel["k8s_minor"].split("."))) < tuple(
                map(int, cur["k8s_minor"].split("."))):
            raise HTTPException(422, "downgrade not supported")
        c.state = "upgrading"
        steps = [{"step": "control-plane", "status": "succeeded", "at": _now()}]
        for pid in c.nodepool_ids:                 # surge-1 rolling per node pool
            pool = NVKE.nodepools[pid]
            for node in pool.nodes:
                node.k8s_version = rel["k8s_version"]
            pool.release, pool.k8s_version = rel["id"], rel["k8s_version"]
            pool.node_image = rel["node_image"]
            steps.append({"step": f"nodepool/{pool.name}", "nodes": len(pool.nodes),
                          "strategy": "rolling (maxSurge=1)", "status": "succeeded",
                          "at": _now()})
        rec = {"id": NVKE.next_id("upg"), "from": cur["k8s_version"],
               "to": rel["k8s_version"], "release": rel["id"], "state": "succeeded",
               "steps": steps, "at": _now()}
        c.upgrades.append(rec)
        c.release, c.k8s_version = rel["id"], rel["k8s_version"]
        c.aicr_recipe = aicr_recipe(rel, [NVKE.nodepools[p].instance_type
                                          for p in c.nodepool_ids])
        c.state = "running"
        _emit("NVKE upgrade", f"{cluster_id} {rec['from']} → {rec['to']}",
              cluster=cluster_id)
        return {"cluster": _view(c), "upgrade": rec}


@router.get("/clusters/{cluster_id}/aicr-recipe")
def get_recipe(cluster_id: str) -> dict:
    data = get_cluster(cluster_id)
    return {"cluster_id": cluster_id, **data["aicr_recipe"]}


def replace_node(cluster_id: str, node_ref: str, reason: str,
                 source: str = "nvsentinel") -> dict:
    """NVSentinel → node replacement: cordon/drain the faulty node, cordon its
    NICo host (RMA track, no sanitize back to pool), join a fresh instance."""
    with NVKE.lock:
        c = _get(cluster_id)
        for pid in c.nodepool_ids:
            pool = NVKE.nodepools[pid]
            for i, node in enumerate(pool.nodes):
                if node_ref not in (node.name, node.nico_host_id):
                    continue
                adapter = _adapter()
                adapter.cordon(node.nico_host_id, reason)
                node.state = "replacing"
                new = _acquire_nodes(pool, 1, c.tenant_id)[0]
                new.name = node.name
                _return_node(node, sanitize=False)
                pool.nodes[i] = new
                rec = {"id": NVKE.next_id("repl"), "node": node.name,
                       "old_host": node.nico_host_id, "new_host": new.nico_host_id,
                       "reason": reason, "source": source,
                       "steps": ["cordon", "drain", "nico-cordon", "join-replacement",
                                 "release-faulty"], "at": _now()}
                c.replacements.append(rec)
                _emit("NVKE node replace", f"{node.name}: {node.nico_host_id} → "
                                           f"{new.nico_host_id} ({source})",
                      cluster=cluster_id)
                return rec
    raise HTTPException(404, f"node '{node_ref}' not found in {cluster_id}")


@router.post("/clusters/{cluster_id}/nodes/{node_ref}/replace")
def replace(cluster_id: str, node_ref: str, body: ReplaceRequest) -> dict:
    return replace_node(cluster_id, node_ref, body.reason, body.source)


def node_hours(tenant_id: Optional[str] = None) -> list[dict]:
    """Live NVKE nodes for metering (k8s node-hour)."""
    out = []
    for c in NVKE.clusters.values():
        if c.state == "deleted" or (tenant_id and c.tenant_id != tenant_id):
            continue
        for pid in c.nodepool_ids:
            p = NVKE.nodepools[pid]
            for n in p.nodes:
                out.append({"cluster_id": c.id, "tenant_id": c.tenant_id,
                            "nodepool": p.name, "instance_type": p.instance_type,
                            "host": n.nico_host_id, "since": p.created_at})
    return out
