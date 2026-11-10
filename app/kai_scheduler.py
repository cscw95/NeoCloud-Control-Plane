"""F30 — KAI Scheduler emulation for NVKE clusters (queues, quotas, gang
scheduling, NVL72 clique-first placement, reclaim / preempt).

Upstream (Apache-2.0): NVIDIA/KAI-Scheduler v0.20.1 @ ff96561943a2
  CRDs   deployments/kai-scheduler/crds/scheduling.run.ai_queues.yaml      (Queue v2)
         deployments/kai-scheduler/crds/scheduling.run.ai_podgroups.yaml   (PodGroup v2alpha2)
         deployments/kai-scheduler/crds/kai.scheduler_topologies.yaml      (Topology v1alpha1)
         (copies in ref/upstream/kai-scheduler/)
  Flow   pkg/scheduler/actions/allocate/allocate.go
         pkg/scheduler/actions/consolidation/   (not modelled: logged as a no-op)
         pkg/scheduler/actions/reclaim/reclaim.go
         pkg/scheduler/actions/preempt/preempt.go
         stalegangeviction (gang with a lost member is evicted whole)
         PodGroup naming pkg/podgrouper/podgrouper/plugins/kubeflow/... → "pg-<job>-<uid>"
  Action order allocate → consolidation → reclaim → preempt → stalegangeviction
  is the documented KAI order; it is marked [미검증] in UPSTREAM-REFS.md.

Emulation, in-memory, one scheduler per NVKE cluster. Node inventory is the
cluster's GPU nodepool nodes (one node = one NVL72 compute tray, 4 GPUs); every
node carries `nvidia.com/gpu.clique` = <NVLink domain uuid>.<clique id> of its
rack (same value as F11 app/nvke_capi.py), so an NVL72 rack = one clique.

Scheduling semantics (simplified KAI):
- Queue `resources.gpu.{quota, limit, overQuotaWeight}` and `priority`
  (default 100). Fair share = quota first, then over-quota capacity split by
  queue priority (higher first) and overQuotaWeight.
- A workload is a gang: PodGroup.minMember = all pods; it runs only if every
  pod is placed in the same cycle (all-or-nothing).
- Topology: PodGroup.topologyConstraint{topology, preferredTopologyLevel |
  requiredTopologyLevel: nvidia.com/gpu.clique}. Placement picks one clique
  that fits (best fit), else (preferred only) the fewest cliques; nodes are
  bin-packed inside a clique.
- Priority classes (KAI defaults): train 50 / interactive-preemptible 75 →
  preemptible; build 100 / inference 125 → non-preemptible. A non-preemptible
  gang must fit inside its queue quota (NonPreemptibleOverQuota).
- reclaim: a pending gang whose queue is below its fair share evicts
  preemptible gangs of queues above their fair share.
- preempt: a pending gang evicts lower-priority preemptible gangs of its own queue.
- stalegangeviction: a running gang that lost a node (NVSentinel replace,
  cordon) is evicted whole and requeued.
- NeoCloud extension (x-neocloud): grid curtailment fences GPUs and evicts
  preemptible gangs first (DSX B7-04), restored on release.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import trace
from .spec import BLUEPRINTS

router = APIRouter(prefix="/api/v1/nvke/clusters/{cluster_id}", tags=["kai-scheduler"])

KAI_VERSION = "v0.20.1"
QUEUE_API = "scheduling.run.ai/v2"
PODGROUP_API = "scheduling.run.ai/v2alpha2"
TOPOLOGY_API = "kai.scheduler/v1alpha1"
PYTORCHJOB_API = "kubeflow.org/v1"
GPU = "nvidia.com/gpu"
CLIQUE_LABEL = "nvidia.com/gpu.clique"
LEAF_LABEL = "network.topology.nvidia.com/ib-leaf"
SPINE_LABEL = "network.topology.nvidia.com/ib-spine"
HOST_LABEL = "kubernetes.io/hostname"
TOPOLOGY_NAME = "nvl72-topology"
# same namespace / clique id as F11 app/nvke_capi.py (kept local: that module
# may not be merged yet)
_NVL_NS = uuid.UUID("6f1d8a52-6b1e-4c1a-9b7e-0c2a5e3f9d11")
_CLIQUE_ID = "1"

# KAI default priority classes (value, preemptible)
PRIORITY_CLASSES = {
    "train": (50, True),
    "interactive-preemptible": (75, True),
    "build": (100, False),
    "inference": (125, False),
}
KINDS = ("PyTorchJob", "DynamoGraphDeployment")
ACTIONS = ("allocate", "consolidation", "reclaim", "preempt", "stalegangeviction")
DEFAULT_PARENT = "default-parent-queue"
DEFAULT_QUEUE = "default-queue"
LOG_MAX = 200
MAX_WORKLOADS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uid(*parts: str) -> str:
    return str(uuid.uuid5(_NVL_NS, "/".join(parts)))


def clique_of(rack_id: str) -> str:
    try:                                    # F11 module wins when merged
        from . import nvke_capi            # type: ignore
        v = nvke_capi.clique_of(rack_id)
        if v:
            return v
    except Exception:                       # noqa: BLE001 — optional dependency
        pass
    return f"{uuid.uuid5(_NVL_NS, rack_id)}.{_CLIQUE_ID}"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class Queue(BaseModel):
    name: str
    display_name: str = ""
    parent_queue: Optional[str] = None
    priority: int = 100
    quota_gpus: float = 0
    limit_gpus: float = -1
    over_quota_weight: float = 1
    created_at: str = ""


class Workload(BaseModel):
    name: str
    kind: str
    namespace: str
    queue: str
    gpus: int
    gpus_per_pod: int
    pods: int
    priority_class: str
    priority: int
    preemptible: bool
    topology_mode: str = "preferred"        # preferred | required
    state: str = "Pending"                  # Pending | Running | Preempted | Completed
    uid: str = ""
    created_at: str = ""
    seq: int = 0
    started_at: Optional[str] = None
    placement: list[dict] = Field(default_factory=list)   # [{pod, node, rack, clique, gpus}]
    over_quota: bool = False
    reason: str = ""
    message: str = ""
    evictions: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    labels: dict = Field(default_factory=dict)


class _ClusterSched:
    def __init__(self, cid: str) -> None:
        self.cid = cid
        self.queues: dict[str, Queue] = {}
        self.jobs: dict[str, Workload] = {}
        self.log: list[dict] = []
        self.cycles = 0
        self.seq = 0
        self.fenced_gpus = 0
        self.curtail_reason = ""
        t = _now()
        self.queues[DEFAULT_PARENT] = Queue(name=DEFAULT_PARENT, quota_gpus=-1,
                                            limit_gpus=-1, created_at=t)
        self.queues[DEFAULT_QUEUE] = Queue(name=DEFAULT_QUEUE, parent_queue=DEFAULT_PARENT,
                                           quota_gpus=0, limit_gpus=-1, created_at=t)


class _Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.clusters: dict[str, _ClusterSched] = {}

    def reset(self) -> None:
        with self.lock:
            self.clusters.clear()


SCHED = _Store()


def reset() -> None:
    SCHED.reset()


def _nvke_cluster(cid: str):
    from . import nvke
    return nvke._get(cid)                   # 404/409 like the NVKE surface


def _sched(cid: str) -> _ClusterSched:
    _nvke_cluster(cid)
    with SCHED.lock:
        s = SCHED.clusters.get(cid)
        if s is None:
            s = SCHED.clusters[cid] = _ClusterSched(cid)
        return s


def _leaf_queues(s: _ClusterSched) -> list[Queue]:
    parents = {q.parent_queue for q in s.queues.values() if q.parent_queue}
    return [q for q in s.queues.values() if q.name not in parents]


# ---------------------------------------------------------------------------
# Node inventory (cluster nodepools → KAI nodes with topology labels)
# ---------------------------------------------------------------------------
def _gpu_per_node(instance_type: str) -> int:
    bp = BLUEPRINTS.get(instance_type)
    return bp.gpu_per_tray if bp is not None else 0


def nodes(cid: str) -> list[dict]:
    from . import nvke
    from .store import STORE
    c = _nvke_cluster(cid)
    out = []
    for pid in c.nodepool_ids:
        p = nvke.NVKE.nodepools.get(pid)
        if p is None:
            continue
        g = _gpu_per_node(p.instance_type)
        for n in p.nodes:
            if n.state == "removed" or not g:
                continue
            tray = STORE.trays.get(n.tray_id)
            if tray is None:
                continue
            rack = STORE.racks.get(tray.rack_id)
            su = rack.su_id if rack is not None else ""
            out.append({"name": n.name, "host": n.nico_host_id, "nodepool": p.name,
                        "rack": tray.rack_id,
                        "ready": n.state == "ready", "gpus": g,
                        "labels": {CLIQUE_LABEL: clique_of(tray.rack_id),
                                   LEAF_LABEL: f"leaf-{su}", SPINE_LABEL: "ib-core",
                                   HOST_LABEL: n.name}})
    out.sort(key=lambda x: (x["rack"], x["name"]))
    return out


def _usage(s: _ClusterSched, skip: frozenset = frozenset()) -> dict[str, int]:
    used: dict[str, int] = {}
    for j in s.jobs.values():
        if j.state == "Running" and j.name not in skip:
            for pl in j.placement:
                used[pl["node"]] = used.get(pl["node"], 0) + pl["gpus"]
    return used


def _fenced_nodes(s: _ClusterSched, inv: list[dict]) -> set[str]:
    """Curtailment fences whole nodes from the end of the inventory."""
    if s.fenced_gpus <= 0:
        return set()
    out, left = set(), s.fenced_gpus
    for n in reversed(inv):
        if left <= 0:
            break
        out.add(n["name"])
        left -= n["gpus"]
    return out


def _place(s: _ClusterSched, inv: list[dict], job: Workload,
           skip: frozenset = frozenset()) -> tuple[Optional[list[dict]], str]:
    """Gang placement, clique first. Returns (placement, reason)."""
    used = _usage(s, skip)
    fenced = _fenced_nodes(s, inv)
    by_clique: dict[str, list[dict]] = {}
    for n in inv:
        if not n["ready"] or n["name"] in fenced or n["gpus"] < job.gpus_per_pod:
            continue
        free = n["gpus"] - used.get(n["name"], 0)
        slots = free // job.gpus_per_pod
        if slots > 0:
            by_clique.setdefault(n["labels"][CLIQUE_LABEL], []).append(
                {"node": n["name"], "host": n["host"], "rack": n["rack"], "free": free,
                 "slots": slots,
                 "full": free == n["gpus"]})
    cap = {k: sum(x["slots"] for x in v) for k, v in by_clique.items()}
    need = job.pods
    if sum(cap.values()) < need:
        return None, (f"gang: {need} pods × {job.gpus_per_pod} GPU need "
                      f"{need * job.gpus_per_pod} GPUs, {sum(cap.values()) * job.gpus_per_pod} free")
    single = sorted((c, k) for k, c in cap.items() if c >= need)
    if single:
        chosen = [single[0][1]]                       # best fit: smallest clique that fits
    elif job.topology_mode == "required":
        return None, (f"topology: no single {CLIQUE_LABEL} domain has {need} free pod slots "
                      f"(largest {max(cap.values(), default=0)})")
    else:
        chosen, got = [], 0
        for k in sorted(cap, key=lambda k: (-cap[k], k)):   # fewest cliques
            chosen.append(k)
            got += cap[k]
            if got >= need:
                break
    out, left = [], need
    for k in chosen:
        # bin-pack: partially used nodes first, then by name
        for n in sorted(by_clique[k], key=lambda x: (x["full"], x["free"], x["node"])):
            while n["slots"] > 0 and left > 0:
                out.append({"pod": "", "node": n["node"], "host": n["host"], "rack": n["rack"],
                            "clique": k, "gpus": job.gpus_per_pod})
                n["slots"] -= 1
                left -= 1
        if left == 0:
            break
    for i, pl in enumerate(out):
        pl["pod"] = _pod_name(job, i)
    return out, ""


def _pod_name(job: Workload, i: int) -> str:
    if job.kind == "PyTorchJob":
        return f"{job.name}-master-0" if i == 0 else f"{job.name}-worker-{i - 1}"
    return f"{job.name}-worker-{i}"


# ---------------------------------------------------------------------------
# Fair share (quota first, then over-quota by priority / overQuotaWeight)
# ---------------------------------------------------------------------------
def _alloc(s: _ClusterSched, qname: str) -> int:
    return sum(j.gpus for j in s.jobs.values() if j.queue == qname and j.state == "Running")


def _requested(s: _ClusterSched, qname: str) -> int:
    return sum(j.gpus for j in s.jobs.values()
               if j.queue == qname and j.state in ("Running", "Pending", "Preempted"))


def fair_share(s: _ClusterSched, total: int) -> dict[str, float]:
    leaves = _leaf_queues(s)
    req = {q.name: float(_requested(s, q.name)) for q in leaves}
    cap = {q.name: req[q.name] if q.limit_gpus < 0 else min(req[q.name], q.limit_gpus)
           for q in leaves}
    share = {q.name: min(cap[q.name], max(q.quota_gpus, 0)) for q in leaves}
    left = max(0.0, total - sum(share.values()))
    for prio in sorted({q.priority for q in leaves}, reverse=True):
        group = [q for q in leaves if q.priority == prio]
        while left > 1e-9:
            want = [q for q in group if cap[q.name] - share[q.name] > 1e-9]
            if not want:
                break
            w = sum(max(q.over_quota_weight, 0) for q in want) or len(want)
            given = 0.0
            for q in want:
                part = left * ((max(q.over_quota_weight, 0) / w) if w else 1 / len(want))
                g = min(part, cap[q.name] - share[q.name])
                share[q.name] += g
                given += g
            left -= given
            if given <= 1e-9:
                break
    return {k: round(v, 3) for k, v in share.items()}


# ---------------------------------------------------------------------------
# Scheduling cycle
# ---------------------------------------------------------------------------
def _trace(title: str, detail: str, **payload) -> None:
    trace.emit("NeoCloudOS.NVKE", "KAI.Scheduler", "K8s", title, detail, payload=payload)


def _ev(job: Workload, reason: str, msg: str) -> None:
    job.events.append({"at": _now(), "reason": reason, "message": msg})
    del job.events[:-30]


def _log(s: _ClusterSched, action: str, msg: str, **kw) -> dict:
    e = {"cycle": s.cycles, "at": _now(), "action": action, "message": msg, **kw}
    s.log.append(e)
    del s.log[:-LOG_MAX]
    return e


def _run(s: _ClusterSched, job: Workload, placement: list[dict], action: str) -> None:
    job.state, job.placement, job.reason, job.message = "Running", placement, "", ""
    job.started_at = _now()
    q = s.queues[job.queue]
    job.over_quota = q.quota_gpus >= 0 and _alloc(s, job.queue) > q.quota_gpus
    cl = sorted({p["clique"] for p in placement})
    racks = sorted({p["rack"] for p in placement})
    msg = (f"{job.pods} pods bound on {len(cl)} clique(s) {', '.join(racks)}"
           + (" (over quota)" if job.over_quota else ""))
    _ev(job, "Scheduled", msg)
    _log(s, action, f"{job.name}: {msg}", workload=job.name, cliques=len(cl))


def _evict(s: _ClusterSched, job: Workload, action: str, by: str, why: str) -> None:
    job.evictions.append({"at": _now(), "action": action, "by": by, "reason": why,
                          "racks": sorted({p["rack"] for p in job.placement})})
    job.state, job.placement, job.over_quota = "Preempted", [], False
    job.reason = {"reclaim": "Reclaimed", "preempt": "Preempted",
                  "stalegangeviction": "StaleGang", "curtail": "Curtailed"}.get(action, "Evicted")
    job.message = why
    _ev(job, job.reason, f"gang evicted whole ({job.pods} pods): {why}")
    _log(s, action, f"{job.name} evicted by {by}: {why}", workload=job.name, victim=True)


def _order(s: _ClusterSched, share: dict[str, float]) -> list[Workload]:
    def key(j: Workload):
        q = s.queues[j.queue]
        fs = share.get(j.queue) or 0
        ratio = _alloc(s, j.queue) / fs if fs > 0 else float("inf")
        return (-q.priority, ratio, -j.priority, j.seq)
    return sorted([j for j in s.jobs.values() if j.state in ("Pending", "Preempted")], key=key)


def _quota_ok(s: _ClusterSched, job: Workload) -> tuple[bool, str, str]:
    q = s.queues[job.queue]
    after = _alloc(s, job.queue) + job.gpus
    if q.limit_gpus >= 0 and after > q.limit_gpus:
        return False, "OverLimit", (f"queue {q.name} limit {q.limit_gpus:g} GPU "
                                    f"(allocated {_alloc(s, q.name)}, job {job.gpus})")
    after_np = sum(j.gpus for j in s.jobs.values() if j.queue == job.queue
                   and j.state == "Running" and not j.preemptible) + job.gpus
    if not job.preemptible and q.quota_gpus >= 0 and after_np > q.quota_gpus:
        return False, "NonPreemptibleOverQuota", (
            f"non-preemptible job over queue {q.name} quota {q.quota_gpus:g} GPU")
    return True, "", ""


def _try_victims(s, inv, job, candidates, action) -> bool:
    """Evict the fewest candidates (in order) so the gang fits; all or nothing."""
    chosen: list[Workload] = []
    for v in candidates:
        chosen.append(v)
        pl, _ = _place(s, inv, job, frozenset(x.name for x in chosen))
        if pl is not None:
            for x in chosen:
                _evict(s, x, action, job.name,
                       f"{action} for {job.name} (queue {job.queue}, priority {job.priority})")
            _run(s, job, pl, action)
            return True
    return False


def cycle(cid: str, trigger: str = "") -> dict:
    """One KAI scheduling session: allocate → consolidation → reclaim → preempt
    → stalegangeviction. Returns the actions taken."""
    s = SCHED.clusters[cid]
    with SCHED.lock:
        s.cycles += 1
        start = len(s.log)
        inv = nodes(cid)
        total = sum(n["gpus"] for n in inv if n["ready"]) - s.fenced_gpus
        _log(s, "open-session", f"trigger={trigger or '-'} nodes={len(inv)} gpus={max(total, 0)}")
        share = fair_share(s, max(total, 0))
        # allocate
        for job in _order(s, share):
            ok, reason, msg = _quota_ok(s, job)
            if not ok:
                job.reason, job.message = reason, msg
                continue
            pl, why = _place(s, inv, job)
            if pl is not None:
                _run(s, job, pl, "allocate")
            else:
                job.reason, job.message = "Unschedulable", why
        _log(s, "consolidation", "not modelled (no-op)")
        # reclaim — pending gang of a queue under its fair share
        for job in _order(s, share):
            if job.state not in ("Pending", "Preempted"):
                continue
            ok, _, _ = _quota_ok(s, job)
            if not ok or _alloc(s, job.queue) + job.gpus > share.get(job.queue, 0) + 1e-9:
                continue
            victims = [v for v in s.jobs.values()
                       if v.state == "Running" and v.preemptible and v.queue != job.queue
                       and _alloc(s, v.queue) > share.get(v.queue, 0) + 1e-9]
            victims.sort(key=lambda v: (s.queues[v.queue].priority, v.priority, -v.seq))
            _try_victims(s, inv, job, victims, "reclaim")
        # preempt — same queue, lower priority, preemptible
        for job in _order(s, share):
            if job.state not in ("Pending", "Preempted"):
                continue
            ok, _, _ = _quota_ok(s, job)
            if not ok:
                continue
            victims = [v for v in s.jobs.values()
                       if v.state == "Running" and v.preemptible and v.queue == job.queue
                       and v.priority < job.priority]
            victims.sort(key=lambda v: (v.priority, -v.seq))
            _try_victims(s, inv, job, victims, "preempt")
        # stalegangeviction — a member's node is gone or not ready
        stale = _stale(s, inv)
        for j in stale:
            lost = sorted({p["node"] for p in j.placement if (p["node"], p["host"]) not in
                           {(n["name"], n["host"]) for n in inv if n["ready"]}})
            _evict(s, j, "stalegangeviction", "kai-scheduler",
                   f"gang member node(s) {', '.join(lost)} not ready")
        for j in _order(s, share):
            if j.reason in ("Unschedulable", "") and j.state in ("Pending", "Preempted"):
                _ev(j, "Unschedulable", j.message or "waiting")
        _log(s, "close-session", f"running={sum(j.state == 'Running' for j in s.jobs.values())}")
        actions = s.log[start:]
        if stale:                                  # requeued gangs get the next session
            actions = actions + cycle(cid, "stalegangeviction")["actions"]
        return {"cycle": s.cycles, "actions": actions}


def _stale(s: _ClusterSched, inv: list[dict]) -> list[Workload]:
    """Running gangs with a member on a node that is gone, replaced or not ready."""
    ready = {(n["name"], n["host"]) for n in inv if n["ready"]}
    return [j for j in s.jobs.values() if j.state == "Running"
            and any((p["node"], p["host"]) not in ready for p in j.placement)]


def _sync(s: _ClusterSched) -> None:
    """Lazy node watch: a replaced/cordoned node triggers a session (KAI reacts
    to the pod/node informer; here the next read notices)."""
    with SCHED.lock:
        if _stale(s, nodes(s.cid)):
            cycle(s.cid, "node-change")


def resync(cid: Optional[str] = None) -> None:
    """Re-run a session after node changes (NVSentinel replace, scale)."""
    with SCHED.lock:
        ids = [cid] if cid else list(SCHED.clusters)
    for c in ids:
        if c in SCHED.clusters:
            try:
                cycle(c, "node-change")
            except HTTPException:
                pass


# ---------------------------------------------------------------------------
# Views (KAI CRs + NeoCloud summary)
# ---------------------------------------------------------------------------
def _q(v: float):
    return int(v) if float(v).is_integer() else v


def queue_cr(s: _ClusterSched, q: Queue, share: dict[str, float]) -> dict:
    kids = sorted(x.name for x in s.queues.values() if x.parent_queue == q.name)
    names = {q.name, *kids}
    alloc = sum(_alloc(s, n) for n in names)
    alloc_np = sum(j.gpus for j in s.jobs.values()
                   if j.queue in names and j.state == "Running" and not j.preemptible)
    req = sum(_requested(s, n) for n in names)
    spec = {"priority": q.priority,
            "resources": {"gpu": {"quota": _q(q.quota_gpus), "limit": _q(q.limit_gpus),
                                  "overQuotaWeight": _q(q.over_quota_weight)},
                          "cpu": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
                          "memory": {"quota": -1, "limit": -1, "overQuotaWeight": 1}}}
    if q.display_name:
        spec["displayName"] = q.display_name
    if q.parent_queue:
        spec["parentQueue"] = q.parent_queue
    status = {"allocated": {GPU: str(alloc)}, "allocatedNonPreemptible": {GPU: str(alloc_np)},
              "requested": {GPU: str(req)},
              "conditions": [{"type": "Ready", "status": "True", "reason": "QueueReady",
                              "message": f"fair share {share.get(q.name, 0):g} GPU",
                              "lastTransitionTime": q.created_at}]}
    if kids:
        status["childQueues"] = kids
    return {"apiVersion": QUEUE_API, "kind": "Queue",
            "metadata": {"name": q.name, "uid": _uid(s.cid, "queue", q.name),
                         "creationTimestamp": q.created_at,
                         "labels": {"nvke.nvidia.com/cluster": s.cid}},
            "spec": spec, "status": status}


def queue_view(s: _ClusterSched, q: Queue, share: dict[str, float]) -> dict:
    jobs = [j for j in s.jobs.values() if j.queue == q.name and j.state != "Completed"]
    return {"name": q.name, "display_name": q.display_name, "parent_queue": q.parent_queue,
            "priority": q.priority, "quota_gpus": _q(q.quota_gpus),
            "limit_gpus": _q(q.limit_gpus), "over_quota_weight": _q(q.over_quota_weight),
            "allocated_gpus": _alloc(s, q.name), "requested_gpus": _requested(s, q.name),
            "fair_share_gpus": share.get(q.name),
            "workloads": {"running": sum(j.state == "Running" for j in jobs),
                          "pending": sum(j.state in ("Pending", "Preempted") for j in jobs)},
            "resource": queue_cr(s, q, share)}


def podgroup_cr(s: _ClusterSched, j: Workload) -> dict:
    level = "requiredTopologyLevel" if j.topology_mode == "required" else "preferredTopologyLevel"
    cond_ok = j.state == "Running"
    status: dict = {"resourcesStatus": {
        "allocated": {GPU: str(j.gpus if cond_ok else 0)},
        "allocatedNonPreemptible": {GPU: str(j.gpus if cond_ok and not j.preemptible else 0)},
        "requested": {GPU: str(j.gpus)}}}
    if not cond_ok and j.state != "Completed":
        status["schedulingConditions"] = [{
            "type": "UnschedulableOnNodePool", "nodePool": "default", "status": "True",
            "reason": j.reason or "Unschedulable", "message": j.message,
            "transitionID": str(s.cycles), "lastTransitionTime": _now(),
            "reasons": [{"reason": j.reason or "Unschedulable", "message": j.message,
                         "details": {"queueDetails": {"name": j.queue,
                                                       "podGroupRequestedResources": {GPU: str(j.gpus)},
                                                       "queueAllocatedResources": {GPU: str(_alloc(s, j.queue))}}}}]}]
    return {"apiVersion": PODGROUP_API, "kind": "PodGroup",
            "metadata": {"name": f"pg-{j.name}-{j.uid}", "namespace": j.namespace,
                         "uid": _uid(s.cid, "pg", j.name, j.uid),
                         "creationTimestamp": j.created_at,
                         "labels": {"kai.scheduler/queue": j.queue, **j.labels},
                         "ownerReferences": [{"apiVersion": PYTORCHJOB_API if j.kind == "PyTorchJob"
                                              else "nvidia.com/v1beta1", "kind": j.kind,
                                              "name": j.name, "uid": j.uid}]},
            "spec": {"minMember": j.pods, "queue": j.queue,
                     "priorityClassName": j.priority_class,
                     "preemptibility": "preemptible" if j.preemptible else "non-preemptible",
                     "topologyConstraint": {"topology": TOPOLOGY_NAME, level: CLIQUE_LABEL}},
            "status": status}


def job_cr(j: Workload) -> dict:
    if j.kind != "PyTorchJob":
        return {"apiVersion": "nvidia.com/v1beta1", "kind": j.kind,
                "metadata": {"name": j.name, "namespace": j.namespace, "uid": j.uid,
                             "labels": {"kai.scheduler/queue": j.queue}}}

    def tmpl():
        return {"metadata": {"labels": {"kai.scheduler/queue": j.queue}},
                "spec": {"schedulerName": "kai-scheduler",
                         "priorityClassName": j.priority_class,
                         "containers": [{"name": "pytorch", "image": "nvcr.io/nvidia/pytorch:26.09-py3",
                                         "resources": {"limits": {GPU: str(j.gpus_per_pod)}}}]}}
    specs = {"Master": {"replicas": 1, "restartPolicy": "OnFailure", "template": tmpl()}}
    if j.pods > 1:
        specs["Worker"] = {"replicas": j.pods - 1, "restartPolicy": "OnFailure", "template": tmpl()}
    cond = {"Running": "Running", "Completed": "Succeeded"}.get(j.state, "Created")
    return {"apiVersion": PYTORCHJOB_API, "kind": "PyTorchJob",
            "metadata": {"name": j.name, "namespace": j.namespace, "uid": j.uid,
                         "labels": {"kai.scheduler/queue": j.queue}},
            "spec": {"runPolicy": {"schedulingPolicy": {"queue": j.queue,
                                                        "priorityClass": j.priority_class,
                                                        "minAvailable": j.pods}},
                     "pytorchReplicaSpecs": specs},
            "status": {"conditions": [{"type": cond, "status": "True"}]}}


def placement_view(j: Workload) -> dict:
    by: dict[str, dict] = {}
    for p in j.placement:
        e = by.setdefault(p["clique"], {"clique": p["clique"], "rack": p["rack"],
                                        "nodes": [], "pods": 0, "gpus": 0})
        if p["node"] not in e["nodes"]:
            e["nodes"].append(p["node"])
        e["pods"] += 1
        e["gpus"] += p["gpus"]
    cl = sorted(by.values(), key=lambda x: x["rack"])
    return {"topology_level": CLIQUE_LABEL, "mode": j.topology_mode,
            "clique_count": len(cl), "single_clique": len(cl) == 1, "cliques": cl,
            "pods": [{"pod": p["pod"], "node": p["node"], "gpus": p["gpus"]} for p in j.placement]}


def workload_view(s: _ClusterSched, j: Workload) -> dict:
    return {"name": j.name, "kind": j.kind, "namespace": j.namespace, "queue": j.queue,
            "gpus": j.gpus, "gpus_per_pod": j.gpus_per_pod, "pods": j.pods,
            "priority_class": j.priority_class, "priority": j.priority,
            "preemptible": j.preemptible, "state": j.state, "reason": j.reason,
            "message": j.message, "over_quota": j.over_quota, "created_at": j.created_at,
            "started_at": j.started_at, "placement": placement_view(j),
            "evictions": j.evictions, "events": j.events[-10:],
            "podgroup": podgroup_cr(s, j), "job": job_cr(j)}


def topology_cr() -> dict:
    return {"apiVersion": TOPOLOGY_API, "kind": "Topology",
            "metadata": {"name": TOPOLOGY_NAME},
            "spec": {"levels": [{"nodeLabel": SPINE_LABEL, "alias": "spine"},
                                {"nodeLabel": LEAF_LABEL, "alias": "leaf"},
                                {"nodeLabel": CLIQUE_LABEL, "alias": "nvl72-clique"},
                                {"nodeLabel": HOST_LABEL}]}}


def _total(s: _ClusterSched) -> int:
    return max(0, sum(n["gpus"] for n in nodes(s.cid) if n["ready"]) - s.fenced_gpus)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class QueueCreate(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
    quota_gpus: float = 0
    priority: int = 100
    limit_gpus: float = -1
    over_quota_weight: float = 1
    parent_queue: Optional[str] = None
    display_name: str = ""


class WorkloadCreate(BaseModel):
    kind: str = "PyTorchJob"
    name: Optional[str] = Field(None, pattern=r"^[a-z0-9]([-a-z0-9]{0,50}[a-z0-9])?$")
    gpus: int
    queue: str = DEFAULT_QUEUE
    gpus_per_pod: int = 4
    priority_class: str = "train"
    preemptibility: Optional[str] = None          # preemptible | non-preemptible
    topology: str = "preferred"                    # preferred | required
    namespace: str = "default"
    labels: dict = Field(default_factory=dict)


class CurtailBody(BaseModel):
    gpus: int
    reason: str = "grid curtailment"


def _operator(request: Request) -> None:
    who = getattr(request.state, "identity", None)
    if who is not None and who.role == "tenant":
        raise HTTPException(403, "operator surface not allowed for tenant scope")


@router.get("/queues")
def list_queues(cluster_id: str) -> list:
    s = _sched(cluster_id)
    _sync(s)
    with SCHED.lock:
        share = fair_share(s, _total(s))
        return [queue_view(s, q, share) for q in s.queues.values()]


@router.post("/queues", status_code=201)
def create_queue(cluster_id: str, body: QueueCreate) -> dict:
    s = _sched(cluster_id)
    with SCHED.lock:
        if body.name in s.queues:
            raise HTTPException(409, f'queues.scheduling.run.ai "{body.name}" already exists')
        if body.parent_queue and body.parent_queue not in s.queues:
            raise HTTPException(404, f"parent queue '{body.parent_queue}' not found")
        if body.limit_gpus >= 0 and body.quota_gpus > body.limit_gpus:
            raise HTTPException(422, "quota_gpus must not exceed limit_gpus")
        q = Queue(name=body.name, display_name=body.display_name,
                  parent_queue=body.parent_queue or DEFAULT_PARENT, priority=body.priority,
                  quota_gpus=body.quota_gpus, limit_gpus=body.limit_gpus,
                  over_quota_weight=body.over_quota_weight, created_at=_now())
        s.queues[q.name] = q
        _trace("Queue create", f"{cluster_id}/{q.name} quota {q.quota_gpus:g} GPU "
               f"priority {q.priority}", cluster=cluster_id, queue=q.name)
        res = cycle(cluster_id, f"queue {q.name}")
        share = fair_share(s, _total(s))
        return {**queue_view(s, q, share), "cycle": res}


@router.get("/queues/{name}")
def get_queue(cluster_id: str, name: str) -> dict:
    s = _sched(cluster_id)
    with SCHED.lock:
        q = s.queues.get(name)
        if q is None:
            raise HTTPException(404, f'queues.scheduling.run.ai "{name}" not found')
        return queue_view(s, q, fair_share(s, _total(s)))


@router.delete("/queues/{name}")
def delete_queue(cluster_id: str, name: str) -> dict:
    s = _sched(cluster_id)
    with SCHED.lock:
        if name not in s.queues:
            raise HTTPException(404, f'queues.scheduling.run.ai "{name}" not found')
        if name in (DEFAULT_QUEUE, DEFAULT_PARENT):
            raise HTTPException(409, "default queues cannot be deleted")
        if any(j.queue == name and j.state != "Completed" for j in s.jobs.values()) \
                or any(q.parent_queue == name for q in s.queues.values()):
            raise HTTPException(409, f"queue '{name}' has workloads or child queues")
        del s.queues[name]
        return {"deleted": name}


@router.get("/workloads")
def list_workloads(cluster_id: str, queue: Optional[str] = None,
                   state: Optional[str] = None) -> list:
    s = _sched(cluster_id)
    _sync(s)
    with SCHED.lock:
        return [workload_view(s, j) for j in sorted(s.jobs.values(), key=lambda j: j.seq)
                if (not queue or j.queue == queue) and (not state or j.state == state)]


@router.post("/workloads", status_code=201)
def submit_workload(cluster_id: str, body: WorkloadCreate) -> dict:
    return submit(cluster_id, body)


def submit(cluster_id: str, body: WorkloadCreate) -> dict:
    s = _sched(cluster_id)
    if body.kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {list(KINDS)}")
    if body.gpus_per_pod not in (1, 2, 4):
        raise HTTPException(422, "gpus_per_pod must be 1, 2 or 4 (one NVL72 tray = 4 GPUs)")
    if body.gpus <= 0 or body.gpus % body.gpus_per_pod:
        raise HTTPException(422, f"gpus must be a positive multiple of gpus_per_pod ({body.gpus_per_pod})")
    if body.priority_class not in PRIORITY_CLASSES:
        raise HTTPException(422, f"priority_class must be one of {list(PRIORITY_CLASSES)}")
    if body.topology not in ("preferred", "required"):
        raise HTTPException(422, "topology must be 'preferred' or 'required'")
    if body.preemptibility not in (None, "preemptible", "non-preemptible"):
        raise HTTPException(422, "preemptibility must be 'preemptible' or 'non-preemptible'")
    with SCHED.lock:
        if body.queue not in s.queues or body.queue == DEFAULT_PARENT:
            raise HTTPException(404, f'queues.scheduling.run.ai "{body.queue}" not found')
        if any(q.parent_queue == body.queue for q in s.queues.values()):
            raise HTTPException(422, f"queue '{body.queue}' is a parent queue; submit to a leaf queue")
        if len(s.jobs) >= MAX_WORKLOADS:
            raise HTTPException(429, "too many workloads in this cluster")
        s.seq += 1
        name = body.name or f"job-{s.seq:04d}"
        if name in s.jobs and s.jobs[name].state != "Completed":
            raise HTTPException(409, f'{body.kind.lower()}s "{name}" already exists')
        prio, pre = PRIORITY_CLASSES[body.priority_class]
        if body.preemptibility:
            pre = body.preemptibility == "preemptible"
        j = Workload(name=name, kind=body.kind, namespace=body.namespace, queue=body.queue,
                     gpus=body.gpus, gpus_per_pod=body.gpus_per_pod,
                     pods=body.gpus // body.gpus_per_pod, priority_class=body.priority_class,
                     priority=prio, preemptible=pre, topology_mode=body.topology,
                     uid=_uid(cluster_id, "job", name, str(s.seq)), created_at=_now(),
                     seq=s.seq, labels=dict(body.labels))
        s.jobs[name] = j
        _ev(j, "Submitted", f"{j.kind} {j.gpus} GPU ({j.pods} pods) → queue {j.queue}")
        _trace("Workload submit", f"{cluster_id}/{name} {j.gpus} GPU queue {j.queue}",
               cluster=cluster_id, workload=name)
        res = cycle(cluster_id, f"submit {name}")
        preempted = sorted({a["workload"] for a in res["actions"]
                            if a.get("victim") and a.get("workload") != name})
        return {**workload_view(s, j), "preempted": preempted, "cycle": res}


@router.get("/workloads/{name}")
def get_workload(cluster_id: str, name: str) -> dict:
    s = _sched(cluster_id)
    _sync(s)
    with SCHED.lock:
        j = s.jobs.get(name)
        if j is None:
            raise HTTPException(404, f'workload "{name}" not found')
        return workload_view(s, j)


@router.delete("/workloads/{name}")
def delete_workload(cluster_id: str, name: str) -> dict:
    """Job finished / deleted: frees its GPUs, then a session reschedules pending gangs."""
    s = _sched(cluster_id)
    with SCHED.lock:
        j = s.jobs.get(name)
        if j is None or j.state == "Completed":
            raise HTTPException(404, f'workload "{name}" not found')
        j.state, j.placement = "Completed", []
        _ev(j, "Completed", "deleted / finished")
        res = cycle(cluster_id, f"complete {name}")
        started = sorted({a["workload"] for a in res["actions"]
                          if a.get("workload") and not a.get("victim") and a["action"] != "open-session"})
        return {"name": name, "state": "Completed", "started": started, "cycle": res}


@router.get("/scheduler")
def scheduler_view(cluster_id: str) -> dict:
    s = _sched(cluster_id)
    _sync(s)
    with SCHED.lock:
        inv = nodes(cluster_id)
        used = _usage(s)
        fenced = _fenced_nodes(s, inv)
        share = fair_share(s, _total(s))
        cliques: dict[str, dict] = {}
        for n in inv:
            c = cliques.setdefault(n["labels"][CLIQUE_LABEL], {
                "clique": n["labels"][CLIQUE_LABEL], "rack": n["rack"],
                "leaf": n["labels"][LEAF_LABEL], "nodes": 0, "gpus": 0, "used_gpus": 0,
                "fenced_gpus": 0, "not_ready": 0})
            c["nodes"] += 1
            c["gpus"] += n["gpus"]
            c["used_gpus"] += used.get(n["name"], 0)
            c["fenced_gpus"] += n["gpus"] if n["name"] in fenced else 0
            c["not_ready"] += 0 if n["ready"] else 1
        jobs = list(s.jobs.values())
        return {"cluster_id": cluster_id, "scheduler": "kai-scheduler", "version": KAI_VERSION,
                "actions": list(ACTIONS), "cycles": s.cycles,
                "capacity": {"nodes": len(inv), "gpus": sum(n["gpus"] for n in inv),
                             "schedulable_gpus": _total(s), "used_gpus": sum(used.values()),
                             "fenced_gpus": s.fenced_gpus, "curtail_reason": s.curtail_reason},
                "cliques": sorted(cliques.values(), key=lambda c: c["rack"]),
                "queues": [queue_view(s, q, share) for q in s.queues.values()],
                "workloads": {st: sum(j.state == st for j in jobs)
                              for st in ("Running", "Pending", "Preempted", "Completed")},
                "topology": topology_cr(), "priority_classes": {
                    k: {"value": v, "preemptible": p} for k, (v, p) in PRIORITY_CLASSES.items()},
                "log": s.log[-60:]}


@router.post("/scheduler/cycle")
def run_cycle(cluster_id: str) -> dict:
    _sched(cluster_id)
    return cycle(cluster_id, "manual")


@router.get("/scheduler/nodes")
def scheduler_nodes(cluster_id: str) -> list:
    s = _sched(cluster_id)
    with SCHED.lock:
        used = _usage(s)
        return [{**n, "used_gpus": used.get(n["name"], 0)} for n in nodes(cluster_id)]


@router.post("/scheduler/curtail")
def curtail(cluster_id: str, body: CurtailBody, request: Request) -> dict:
    """x-neocloud: grid curtailment fences GPUs; preemptible gangs go first."""
    _operator(request)
    s = _sched(cluster_id)
    if body.gpus <= 0:
        raise HTTPException(422, "gpus must be positive")
    with SCHED.lock:
        s.fenced_gpus, s.curtail_reason = body.gpus, body.reason
        inv = nodes(cluster_id)
        fenced = _fenced_nodes(s, inv)
        hit = [j for j in s.jobs.values() if j.state == "Running"
               and any(p["node"] in fenced for p in j.placement)]
        # free fenced nodes: evict preemptible gangs (lowest priority first);
        # a non-preemptible gang is only moved if it can be re-placed elsewhere
        evicted, kept = [], []
        for j in sorted(hit, key=lambda j: (not j.preemptible, j.priority, -j.seq)):
            if j.preemptible:
                _evict(s, j, "curtail", "dsx-flex", body.reason)
                evicted.append(j.name)
            else:
                kept.append(j.name)
        res = cycle(cluster_id, "curtail")
        return {"fenced_gpus": s.fenced_gpus, "evicted": evicted,
                "non_preemptible_kept": kept, "cycle": res}


@router.delete("/scheduler/curtail")
def release_curtail(cluster_id: str, request: Request) -> dict:
    _operator(request)
    s = _sched(cluster_id)
    with SCHED.lock:
        s.fenced_gpus, s.curtail_reason = 0, ""
        res = cycle(cluster_id, "curtail-released")
        started = sorted({a["workload"] for a in res["actions"] if a.get("workload") and not a.get("victim")})
        return {"fenced_gpus": 0, "started": started, "cycle": res}


@router.delete("/scheduler")
def reset_cluster(cluster_id: str) -> dict:
    """Drop queues / workloads of this cluster (demo reset)."""
    _nvke_cluster(cluster_id)
    with SCHED.lock:
        SCHED.clusters.pop(cluster_id, None)
    return {"reset": cluster_id}


# K8s-style list views of the KAI CRs (read-only)
@router.get("/apis/scheduling.run.ai/v2/queues")
def k8s_queues(cluster_id: str) -> dict:
    s = _sched(cluster_id)
    with SCHED.lock:
        share = fair_share(s, _total(s))
        return {"apiVersion": QUEUE_API, "kind": "QueueList", "metadata": {},
                "items": [queue_cr(s, q, share) for q in s.queues.values()]}


@router.get("/apis/scheduling.run.ai/v2alpha2/podgroups")
def k8s_podgroups(cluster_id: str) -> dict:
    s = _sched(cluster_id)
    with SCHED.lock:
        return {"apiVersion": PODGROUP_API, "kind": "PodGroupList", "metadata": {},
                "items": [podgroup_cr(s, j) for j in s.jobs.values() if j.state != "Completed"]}


@router.get("/apis/kai.scheduler/v1alpha1/topologies")
def k8s_topologies(cluster_id: str) -> dict:
    _sched(cluster_id)
    return {"apiVersion": TOPOLOGY_API, "kind": "TopologyList", "metadata": {},
            "items": [topology_cr()]}
