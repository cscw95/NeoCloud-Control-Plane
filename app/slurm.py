"""F31 Slurm on Kubernetes (Slinky) emulation — sbatch, topology.conf, Health Validation prolog.

Upstream (Apache-2.0):
  SlinkyProject/slurm-operator v1.2.4 @ 0222d1c37d2f — CRDs slinky.slurm.net/v1beta1
    Controller (slurmctld), NodeSet (slurmd pods), RestApi (slurmrestd);
    copies in tests/contracts/slinky.slurm.net_*.yaml; flow controller_sync.go, nodeset/.
  slurmrestd OpenAPI v0.0.42 (SchedMD) — POST job/submit, GET jobs|job/{id}|nodes|partitions,
    DELETE job/{id}, POST node/{name}; responses carry {meta, errors, warnings}.
  dsx-ai-factory/topograph v1.1.0 @ 3ca60d7eac21 — slurm engine writes topology.conf
    (topology/block: BlockName= Nodes=, BlockSizes=; topology/tree: SwitchName= Nodes=|Switches=).
  Slurm prolog semantics (slurm.conf PrologFlags/SchedulerParameters, public docs): a failing
    prolog DRAINs the node and requeues the job; with nohold_on_prolog_fail it is not held.

Flow: sbatch → PENDING → placement (same NVL72 block first) → CONFIGURING (prolog = F28
Health Validation per allocated node, app/readiness.py) → RUNNING; prolog failure → node
DRAIN + job REQUEUED (restart_cnt+1) → re-placed without that node. Node failure while
running (S4): node DOWN, job NODE_FAIL → requeue with the node excluded, F10
remediation started when app.remediation exists (stub otherwise).
State is in-memory; the clock is the request (no background threads).

NeoCloud surface (plan F31):          POST /api/v1/slurm/clusters/{id}/jobs  (sbatch)
slurmrestd passthrough per cluster:   /api/v1/slurm/clusters/{id}/slurm/v0.0.42/...
"""
from __future__ import annotations

import hashlib
import re
import shlex
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import readiness
from .store import STORE

API = "v0.0.42"
SLURM_VERSION = {"major": "25", "minor": "05", "micro": "0"}
GROUP = "slinky.slurm.net/v1beta1"
NODESET = "gpu"
router = APIRouter(prefix="/api/v1/slurm", tags=["slurm-slinky"])

_LOCK = threading.RLock()
_S: dict[str, Any] = {"clusters": {}, "seq": 0}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"}


def reset() -> None:
    with _LOCK:
        _S.update(clusters={}, seq=0)


def _ts() -> int:
    return int(time.time())


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _num(v: Optional[int]) -> dict:
    """slurmrestd uint32_no_val / uint64_no_val struct."""
    return {"set": v is not None, "infinite": False, "number": v if v is not None else 0}


def _meta() -> dict:
    return {"plugin": {"type": "openapi/slurmctld", "name": "Slurm OpenAPI slurmctld",
                       "data_parser": f"data_parser/{API}", "accounting_storage": ""},
            "client": {"source": "nocp", "user": "slurm", "group": "slurm"},
            "command": [], "slurm": {"version": SLURM_VERSION, "release": "25.05.0",
                                     "cluster": ""}}


def _envelope(cluster: dict, **body) -> dict:
    m = _meta()
    m["slurm"]["cluster"] = cluster["name"]
    return {**body, "meta": m, "errors": [], "warnings": []}


def _slurm_error(status: int, code: int, msg: str):
    raise HTTPException(status, {"errors": [{"description": msg, "error_number": code,
                                             "error": msg, "source": "slurmctld"}],
                                 "warnings": [], "meta": _meta()})


# ── hostlist ──────────────────────────────────────────────────────────
def hostlist(names: list[str]) -> str:
    """['gpu-000','gpu-001','gpu-003'] → 'gpu-[000-001,003]' (Slurm hostlist compression)."""
    groups: dict[str, list[str]] = {}
    plain = []
    for n in names:
        m = re.match(r"^(.*?)(\d+)$", n)
        if m:
            groups.setdefault(m.group(1), []).append(m.group(2))
        else:
            plain.append(n)
    out = list(plain)
    for prefix, nums in groups.items():
        nums = sorted(set(nums), key=int)
        if len(nums) == 1:
            out.append(prefix + nums[0])
            continue
        width = len(nums[0])
        ranges, start, prev = [], nums[0], nums[0]
        for x in nums[1:] + [None]:
            if x is not None and int(x) == int(prev) + 1:
                prev = x
                continue
            ranges.append(start if start == prev else f"{start}-{prev}")
            if x is not None:
                start = prev = x
        out.append(f"{prefix}[{','.join(ranges)}]" if width else prefix)
    return ",".join(out)


def expand_hostlist(expr: str) -> list[str]:
    out = []
    for part in re.findall(r"[^,\[]+(?:\[[^\]]*\])?", expr or ""):
        m = re.match(r"^(.*)\[(.*)\]$", part)
        if not m:
            out.append(part)
            continue
        for r in m.group(2).split(","):
            a, _, b = r.partition("-")
            w = len(a)
            for i in range(int(a), int(b or a) + 1):
                out.append(f"{m.group(1)}{i:0{w}d}")
    return out


# ── clusters ──────────────────────────────────────────────────────────
def _pick_racks(body: dict) -> list:
    if body.get("racks"):
        racks = []
        for rid in body["racks"]:
            if rid not in STORE.racks:
                raise HTTPException(404, f"rack {rid} not found")
            racks.append(STORE.racks[rid])
        return racks
    count = int(body.get("rack_count", 2))
    vr = [r for r in STORE.racks.values() if r.blueprint_key == "vr-nvl72"]
    pool = vr or list(STORE.racks.values())
    if count < 1 or count > len(pool):
        raise HTTPException(400, f"rack_count must be 1..{len(pool)}")
    return sorted(pool, key=lambda r: r.id)[:count]


def _controller_conf(plugin: str) -> str:
    return "\n".join([
        f"TopologyPlugin={plugin}",
        "PrologFlags=Alloc,Contain",
        "SchedulerParameters=nohold_on_prolog_fail",
        "JobRequeue=1",
        "GresTypes=gpu",
        "SelectType=select/cons_tres",
    ])


def _cr(kind: str, name: str, ns: str, spec: dict, status: dict) -> dict:
    return {"apiVersion": GROUP, "kind": kind,
            "metadata": {"name": name, "namespace": ns,
                         "labels": {"app.kubernetes.io/managed-by": "slurm-operator",
                                    "neocloud.io/feature": "F31"}},
            "spec": spec, "status": status}


def _condition(ctype: str, ok: bool, reason: str, msg: str) -> dict:
    return {"type": ctype, "status": "True" if ok else "False", "reason": reason,
            "message": msg, "lastTransitionTime": _iso(), "observedGeneration": 1}


def crs(c: dict) -> list[dict]:
    """Slinky CRs for the cluster (shape of the v1beta1 CRDs)."""
    nodes = list(c["nodes"].values())
    count = lambda st: sum(1 for n in nodes if st in n["state"])  # noqa: E731
    ns, name = c["namespace"], c["name"]
    controller = _cr("Controller", name, ns, {
        "clusterName": name,
        "prologScriptRefs": [{"name": f"{name}-prolog-health-validation"}],
        "extraConf": _controller_conf(c["topology_plugin"]),
        "slurmctld": {"image": "ghcr.io/slinkyproject/slurmctld:25.05"},
    }, {"conditions": [_condition("Available", True, "Ready", "slurmctld is ready")]})
    nodeset = _cr("NodeSet", f"{name}-{NODESET}", ns, {
        "controllerRef": {"name": name},
        "replicas": len(nodes),
        "ordinalPadding": 3,
        "partition": {"enabled": True,
                      "config": f"Default=YES MaxTime=UNLIMITED State=UP"},
        "syncTopology": True,
        "slurmd": {"image": "ghcr.io/slinkyproject/slurmd:25.05",
                   "args": ["--conf", "Gres=gpu:4 Feature=nvl72"]},
    }, {"replicas": len(nodes), "readyReplicas": len(nodes) - count("DOWN"),
        "availableReplicas": len(nodes) - count("DOWN"), "updatedReplicas": len(nodes),
        "unavailableReplicas": count("DOWN"), "observedGeneration": 1,
        "nodeSetHash": hashlib.sha256(f"{name}/{len(nodes)}".encode()).hexdigest()[:10],
        "slurmIdle": sum(1 for n in nodes if n["state"] == ["IDLE"]),
        "slurmAllocated": count("ALLOCATED"), "slurmDrain": count("DRAIN"),
        "slurmDown": count("DOWN"),
        "ordinalToNode": {str(i): n["tray_id"] for i, n in enumerate(nodes)},
        "selector": f"app.kubernetes.io/instance={name}-{NODESET}",
        "conditions": [_condition("Available", count("DOWN") < len(nodes), "MinimumReplicasAvailable",
                                  f"{len(nodes) - count('DOWN')}/{len(nodes)} slurmd ready")]})
    restapi = _cr("RestApi", f"{name}-restapi", ns, {
        "controllerRef": {"name": name}, "replicas": 1,
        "slurmrestd": {"image": "ghcr.io/slinkyproject/slurmrestd:25.05"},
    }, {"conditions": [_condition("Available", True, "Ready", f"slurmrestd {API} serving")]})
    return [controller, nodeset, restapi]


def prolog_script(c: dict) -> str:
    return ("#!/bin/bash\n"
            "# Slurm prolog (PrologFlags=Alloc) — F28 Health Validation before any task starts.\n"
            "# Non-zero exit → slurmd DRAINs the node, slurmctld requeues the job.\n"
            "health-validation check --node \"$SLURMD_NODENAME\" --catalog nico "
            "--fail-on CRITICAL,UNKNOWN --output /var/log/slurm/hv-$SLURM_JOB_ID.json\n")


def _public(c: dict) -> dict:
    nodes = list(c["nodes"].values())
    jobs = list(c["jobs"].values())
    return {"id": c["id"], "name": c["name"], "tenant_id": c["tenant_id"],
            "namespace": c["namespace"], "created_at": c["created_at"],
            "topology_plugin": c["topology_plugin"], "slurmrestd": API,
            "racks": c["racks"], "partitions": list(c["partitions"]),
            "nodes_total": len(nodes), "gpus_total": sum(n["gpus"] for n in nodes),
            "nodes_by_state": {s: sum(1 for n in nodes if s in n["state"])
                               for s in ("IDLE", "ALLOCATED", "DRAIN", "DOWN")},
            "jobs_by_state": {s: sum(1 for j in jobs if j["job_state"][0] == s)
                              for s in sorted({j["job_state"][0] for j in jobs})},
            "prolog": {"health_validation": True, "script": prolog_script(c)},
            "endpoints": {"sbatch": f"/api/v1/slurm/clusters/{c['id']}/jobs",
                          "slurmrestd": f"/api/v1/slurm/clusters/{c['id']}/slurm/{API}",
                          "topology_conf": f"/api/v1/slurm/clusters/{c['id']}/topology.conf"}}


def _tenant(request: Request) -> Optional[str]:
    return request.headers.get("x-tenant-id") or None


def _cluster(cid: str, request: Optional[Request] = None) -> dict:
    c = _S["clusters"].get(cid)
    if c is None:
        raise HTTPException(404, f"slurm cluster {cid} not found")
    t = _tenant(request) if request is not None else None
    if t and c["tenant_id"] != t:
        raise HTTPException(403, "tenant scope violation")
    return c


def create_cluster(body: dict, tenant: Optional[str] = None) -> dict:
    name = str(body.get("name") or "slurm")
    if not re.match(r"^[a-z0-9]([-a-z0-9]{0,40}[a-z0-9])?$", name):
        raise HTTPException(400, "name must be a DNS label (≤42 chars)")
    plugin = body.get("topology_plugin", "topology/block")
    if plugin not in ("topology/block", "topology/tree"):
        raise HTTPException(400, "topology_plugin must be topology/block or topology/tree")
    racks = _pick_racks(body)
    with _LOCK:
        _S["seq"] += 1
        cid = f"slurm-{_S['seq']:03d}"
        nodes: dict[str, dict] = {}
        i = 0
        for r in racks:
            for tid in r.tray_ids:
                tray = STORE.trays.get(tid)
                n = f"{NODESET}-{i:03d}"
                nodes[n] = {"name": n, "tray_id": tid, "rack_id": r.id, "su_id": r.su_id,
                            "gpus": len(tray.gpu_ids) if tray else 4, "state": ["IDLE"],
                            "reason": "", "job_id": None, "features": ["nvl72", r.blueprint_key]}
                i += 1
        partition = body.get("partition", "gpu")
        c = {"id": cid, "name": name, "tenant_id": tenant or body.get("tenant_id"),
             "namespace": body.get("namespace", "slurm"), "created_at": _iso(),
             "topology_plugin": plugin, "racks": [r.id for r in racks],
             "partitions": {partition: {"default": True}}, "nodes": nodes, "jobs": {},
             "next_job_id": 1, "events": []}
        _S["clusters"][cid] = c
    _event(c, "cluster", f"Slinky Controller/NodeSet/RestApi created — {len(nodes)} nodes "
                         f"on {len(racks)} NVL72 racks, {plugin}")
    return c


def _event(c: dict, kind: str, msg: str, **extra) -> None:
    c["events"].append({"at": _iso(), "kind": kind, "message": msg, **extra})
    del c["events"][:-200]


# ── topology.conf (topograph slurm engine) ────────────────────────────
def topology_conf(c: dict, plugin: Optional[str] = None) -> str:
    plugin = plugin or c["topology_plugin"]
    by_rack: dict[str, list[str]] = {}
    for n in c["nodes"].values():
        by_rack.setdefault(n["rack_id"], []).append(n["name"])
    lines = [f"# topology.conf for {c['name']} — generated in topograph slurm-engine format",
             f"# plugin: {plugin}; one NVL72 rack = one NVLink domain"]
    if plugin == "topology/block":
        for i, (rack, names) in enumerate(sorted(by_rack.items()), 1):
            lines.append(f"BlockName=block{i:03d} Nodes={hostlist(names)}  # {rack}")
        base = min(len(v) for v in by_rack.values())
        sizes, s = [], base
        while s <= sum(len(v) for v in by_rack.values()):
            sizes.append(s)
            s *= 2
        lines.append("BlockSizes=" + ",".join(map(str, sizes)))
    else:
        sus: dict[str, list[str]] = {}
        for rack, names in sorted(by_rack.items()):
            leaf = f"leaf-{rack}"
            lines.append(f"SwitchName={leaf} Nodes={hostlist(names)}")
            su = c["nodes"][names[0]]["su_id"]
            sus.setdefault(su, []).append(leaf)
        for su, leaves in sorted(sus.items()):
            lines.append(f"SwitchName=spine-{su} Switches={hostlist(leaves)}")
        if len(sus) > 1:
            lines.append(f"SwitchName=core Switches={hostlist([f'spine-{s}' for s in sorted(sus)])}")
    return "\n".join(lines) + "\n"


# ── sbatch parsing ────────────────────────────────────────────────────
_SHORT = {"-J": "--job-name", "-p": "--partition", "-N": "--nodes", "-n": "--ntasks",
          "-t": "--time", "-x": "--exclude", "-o": "--output", "-A": "--account",
          "-G": "--gpus", "-w": "--nodelist"}


def parse_sbatch(script: str) -> dict:
    """#SBATCH directives (before the first command line) → option dict."""
    opts: dict[str, Any] = {}
    for line in script.splitlines()[1:] if script.startswith("#!") else script.splitlines():
        s = line.strip()
        if not s or (s.startswith("#") and not s.startswith("#SBATCH")):
            continue
        if not s.startswith("#SBATCH"):
            break
        toks = shlex.split(s[len("#SBATCH"):].split(" #")[0])
        j = 0
        while j < len(toks):
            t = toks[j]
            if t in _SHORT:
                key, val = _SHORT[t], toks[j + 1] if j + 1 < len(toks) else ""
                j += 2
            elif t.startswith("--"):
                key, _, val = t.partition("=")
                if not _ and j + 1 < len(toks) and not toks[j + 1].startswith("-") and \
                        key not in ("--exclusive", "--requeue", "--no-requeue"):
                    val, j = toks[j + 1], j + 1
                j += 1
            else:
                j += 1
                continue
            opts[key.lstrip("-")] = val if val != "" else True
    return opts


def _gpus_per_node(opts: dict, job: dict) -> int:
    tres = job.get("tres_per_node") or ""
    for src in (opts.get("gres"), tres.replace("gres/", "") if tres else None):
        if src and isinstance(src, str):
            m = re.match(r"^gpu(?::[A-Za-z0-9_]+)?:(\d+)$", src)
            if m:
                return int(m.group(1))
    if opts.get("gpus-per-node"):
        return int(str(opts["gpus-per-node"]).split(":")[-1])
    return 4


def _job_desc(body: dict) -> tuple[dict, str]:
    """Accept slurmrestd v0.0.42 JobSubmitReq {job:{script,...}} or legacy {script, job}."""
    job = dict(body.get("job") or {})
    script = job.get("script") or body.get("script") or ""
    if not isinstance(script, str) or not script.strip():
        _slurm_error(400, 2072, "batch job script is empty")       # ESLURM_JOB_SCRIPT_MISSING
    if len(script) > 65536:
        _slurm_error(400, 2072, "batch job script too large")
    return job, script


def _int(v, default):
    try:
        return int(str(v).split("-")[0]) if v not in (None, "", True) else default
    except ValueError:
        _slurm_error(400, 2002, f"invalid count {v!r}")            # ESLURM_INVALID_NODE_COUNT


def _time_limit(v) -> Optional[int]:
    if v in (None, "", True):
        return None
    if isinstance(v, int):
        return v
    parts = [int(x) for x in re.split(r"[-:]", str(v)) if x.isdigit()]
    if "-" in str(v):                     # days-hours[:minutes]
        d, h, m = (parts + [0, 0])[:3]
        return d * 1440 + h * 60 + m
    if len(parts) == 3:
        return parts[0] * 60 + parts[1]
    if len(parts) == 2:
        return parts[0]
    return parts[0] if parts else None


def submit(c: dict, body: dict, user: str = "tenant") -> dict:
    job, script = _job_desc(body)
    opts = parse_sbatch(script)
    nodes = _int(job.get("minimum_nodes") or job.get("nodes") or opts.get("nodes"), 1)
    gpn = _gpus_per_node(opts, job)
    part = job.get("partition") or opts.get("partition") or next(iter(c["partitions"]))
    if part not in c["partitions"]:
        _slurm_error(400, 2000, f"invalid partition name specified: {part}")  # ESLURM_INVALID_PARTITION_NAME
    per_node = max(n["gpus"] for n in c["nodes"].values())
    if nodes < 1 or nodes > len(c["nodes"]) or gpn > per_node:
        _slurm_error(400, 2014, "Requested node configuration is not available")  # ESLURM_REQUESTED_NODE_CONFIG_UNAVAILABLE
    segment = _int(job.get("segment_size") or opts.get("segment"), 0)
    if segment and nodes % segment:
        _slurm_error(400, 2014, "--segment must divide --nodes")
    requeue = job.get("requeue")
    if requeue is None:
        requeue = not opts.get("no-requeue")
    with _LOCK:
        jid = c["next_job_id"]
        c["next_job_id"] += 1
        j = {"job_id": jid, "name": job.get("name") or opts.get("job-name") or "sbatch",
             "user_name": user, "account": job.get("account") or opts.get("account") or "",
             "partition": part, "job_state": ["PENDING"], "state_reason": "None",
             "nodes": "", "node_count": _num(nodes), "tasks": _num(_int(job.get("tasks") or opts.get("ntasks"), nodes)),
             "tres_per_node": f"gres/gpu:{gpn}", "tres_req_str": f"cpu={nodes * 72},gres/gpu={nodes * gpn},node={nodes}",
             "segment_size": segment or None, "requeue": bool(requeue), "restart_cnt": 0,
             "batch_flag": True, "command": "sbatch", "script": script,
             "current_working_directory": job.get("current_working_directory") or "/home/slurm",
             "standard_output": job.get("standard_output") or opts.get("output") or "slurm-%j.out",
             "time_limit": _num(_time_limit(job.get("time_limit") or opts.get("time"))),
             "submit_time": _num(_ts()), "start_time": _num(None), "end_time": _num(None),
             "exit_code": {"status": ["PENDING"], "return_code": _num(None)},
             "excluded_nodes": sorted(set(expand_hostlist(job.get("excluded_nodes") or opts.get("exclude") or ""))),
             "x-neocloud": {"history": [], "prolog": [], "placement": None}}
        c["jobs"][jid] = j
        _hist(c, j, "PENDING", "submitted")
        schedule(c)
    return j


def _hist(c: dict, j: dict, state: str, msg: str) -> None:
    j["x-neocloud"]["history"].append({"at": _iso(), "state": state, "message": msg})
    _event(c, "job", f"job {j['job_id']} {state}: {msg}", job_id=j["job_id"])


# ── scheduling / prolog ───────────────────────────────────────────────
def _free(c: dict, j: dict, gpn: int) -> dict[str, list[str]]:
    by_rack: dict[str, list[str]] = {}
    for n in c["nodes"].values():
        if n["state"] == ["IDLE"] and n["gpus"] >= gpn and n["name"] not in j["excluded_nodes"]:
            by_rack.setdefault(n["rack_id"], []).append(n["name"])
    return by_rack


def place(c: dict, j: dict) -> Optional[dict]:
    """topology/block placement: whole job in one NVL72 block when it fits (best fit);
    with --segment each segment stays inside one block; otherwise fewest blocks."""
    need = j["node_count"]["number"]
    gpn = int(j["tres_per_node"].split(":")[-1])
    free = _free(c, j, gpn)
    if sum(len(v) for v in free.values()) < need:
        return None
    seg = j.get("segment_size") or 0
    if seg:
        picked, blocks = [], []
        for rack, names in sorted(free.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            while len(names) >= seg and len(picked) < need:
                picked += names[:seg]
                names = names[seg:]
                blocks.append(rack)
        if len(picked) < need:
            return None
        return {"nodes": picked, "blocks": sorted(set(blocks)), "single_block": len(set(blocks)) == 1}
    fits = [(len(v), r) for r, v in free.items() if len(v) >= need]
    if fits:
        _, rack = min(fits)
        return {"nodes": sorted(free[rack])[:need], "blocks": [rack], "single_block": True}
    picked, blocks = [], []
    for rack, names in sorted(free.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        take = names[:need - len(picked)]
        picked += take
        blocks.append(rack)
        if len(picked) == need:
            break
    return {"nodes": picked, "blocks": blocks, "single_block": False}


def _run_prolog(c: dict, j: dict, nodes: list[str]) -> list[str]:
    """F28 Health Validation per allocated node; returns the nodes that failed."""
    failed = []
    for name in nodes:
        n = c["nodes"][name]
        v = readiness.verdict(n["tray_id"])
        j["x-neocloud"]["prolog"].append({
            "node": name, "tray_id": n["tray_id"], "attempt": j["restart_cnt"] + 1,
            "exit_code": 0 if v["ready"] else 1, "overall_severity": v["overall_severity"],
            "failing_checks": v["failing_checks"], "source": v["source"],
            "trace_id": v["verdict"]["trace_id"]})
        if not v["ready"]:
            failed.append(name)
            n["state"] = ["IDLE", "DRAIN"]
            n["reason"] = f"Prolog error: health-validation {','.join(v['failing_checks'])}"
            _event(c, "node", f"{name} DRAIN — {n['reason']}", node=name)
    return failed


def schedule(c: dict) -> None:
    """FIFO over pending jobs (priority = submit order); requeues after prolog failures."""
    with _LOCK:
        for j in sorted(c["jobs"].values(), key=lambda x: x["job_id"]):
            if j["job_state"][0] != "PENDING":
                continue
            for _ in range(8):                         # bounded prolog-retry loop
                p = place(c, j)
                if p is None:
                    if j["state_reason"] != "Resources":
                        _hist(c, j, "PENDING", "waiting for resources")
                    j["state_reason"] = "Resources"
                    break
                for name in p["nodes"]:
                    c["nodes"][name].update(state=["ALLOCATED"], job_id=j["job_id"])
                j["job_state"], j["nodes"] = ["CONFIGURING"], hostlist(p["nodes"])
                _hist(c, j, "CONFIGURING", f"allocated {j['nodes']} "
                                           f"({'one NVL72 block' if p['single_block'] else 'blocks ' + ','.join(p['blocks'])}); "
                                           "running prolog")
                failed = _run_prolog(c, j, p["nodes"])
                if failed:
                    for name in p["nodes"]:
                        n = c["nodes"][name]
                        n["job_id"] = None
                        if name not in failed:
                            n["state"] = ["IDLE"]
                    j["restart_cnt"] += 1
                    j["job_state"], j["nodes"], j["state_reason"] = ["PENDING"], "", "launch failed requeued"
                    _hist(c, j, "REQUEUED", f"prolog failed on {hostlist(failed)} → nodes drained, "
                                            "job requeued (nohold_on_prolog_fail)")
                    continue
                j["job_state"], j["state_reason"] = ["RUNNING"], "None"
                j["start_time"] = _num(_ts())
                j["exit_code"] = {"status": ["PENDING"], "return_code": _num(None)}
                j["x-neocloud"]["placement"] = p
                _hist(c, j, "RUNNING", f"prolog passed on {len(p['nodes'])} nodes")
                break


def _release(c: dict, j: dict) -> None:
    for n in c["nodes"].values():
        if n["job_id"] == j["job_id"]:
            n["job_id"] = None
            if "ALLOCATED" in n["state"]:
                n["state"] = ["IDLE"]


def finish(c: dict, j: dict, state: str, rc: int) -> dict:
    with _LOCK:
        if j["job_state"][0] in TERMINAL:
            return j
        was_running = j["job_state"][0] in ("RUNNING", "CONFIGURING")
        _release(c, j)
        j["job_state"] = [state]
        j["end_time"] = _num(_ts())
        j["exit_code"] = {"status": ["SUCCESS" if rc == 0 and state == "COMPLETED" else "ERROR"],
                          "return_code": _num(rc)}
        _hist(c, j, state, f"exit {rc}" if was_running else "cancelled while pending")
        schedule(c)
    return j


def fail_node(c: dict, name: str, reason: str) -> dict:
    """S4: node failure → DOWN; its job NODE_FAIL → requeue without the node; F10 run."""
    with _LOCK:
        n = c["nodes"].get(name)
        if n is None:
            _slurm_error(404, 2008, f"Invalid node name specified: {name}")  # ESLURM_INVALID_NODE_NAME
        n["state"], n["reason"] = ["DOWN"], reason or "Not responding"
        _event(c, "node", f"{name} DOWN — {n['reason']}", node=name)
        remediation = _remediate(n, reason)
        out = {"node": name, "state": n["state"], "remediation": remediation, "job": None}
        jid = n["job_id"]
        n["job_id"] = None
        if jid is not None:
            j = c["jobs"][jid]
            _release(c, j)
            _hist(c, j, "NODE_FAIL", f"{name} failed: {n['reason']}")
            if j["requeue"]:
                j["restart_cnt"] += 1
                j["excluded_nodes"] = sorted(set(j["excluded_nodes"]) | {name})
                j["job_state"], j["nodes"], j["state_reason"] = ["PENDING"], "", "BeginTime"
                _hist(c, j, "REQUEUED", f"requeued from checkpoint excluding {name} "
                                        f"(restart_cnt={j['restart_cnt']})")
                schedule(c)
            else:
                j["job_state"] = ["NODE_FAIL"]
                j["end_time"] = _num(_ts())
            out["job"] = {"job_id": j["job_id"], "job_state": j["job_state"], "nodes": j["nodes"],
                          "restart_cnt": j["restart_cnt"]}
        return out


def _remediate(n: dict, reason: str) -> dict:
    try:
        from .remediation import start_run           # F10 (feat/v2-v2-recovery-sla)
    except ImportError:
        return {"status": "stub", "detail": "F10 app.remediation not present on this branch"}
    try:
        run = start_run(n["tray_id"], reason or "slurm node failure", "slurm")
        return {"status": "started", "run": run if isinstance(run, dict) else str(run)}
    except Exception as e:                             # noqa: BLE001 — never break slurm on F10
        return {"status": "error", "detail": str(e)}


def node_view(c: dict, n: dict) -> dict:
    job = c["jobs"].get(n["job_id"]) if n["job_id"] else None
    return {"name": n["name"], "hostname": n["name"], "state": n["state"], "reason": n["reason"],
            "partitions": list(c["partitions"]), "gres": f"gpu:{n['gpus']}",
            "gres_used": f"gpu:{n['gpus'] if job else 0}", "cpus": 72, "real_memory": 1536000,
            "features": n["features"], "active_features": n["features"],
            "x-neocloud": {"tray_id": n["tray_id"], "rack_id": n["rack_id"], "block": n["rack_id"],
                           "job_id": n["job_id"]}}


def job_view(j: dict) -> dict:
    return {k: v for k, v in j.items() if k != "script"}


def _submit_response(c: dict, j: dict) -> dict:
    res = {"job_id": j["job_id"], "step_id": "batch", "error_code": 0, "error": "",
           "job_submit_user_msg": ""}
    return _envelope(c, job_id=j["job_id"], step_id="batch", job_submit_user_msg="", result=res)


def _job(c: dict, job_id: int) -> dict:
    j = c["jobs"].get(int(job_id))
    if j is None:
        _slurm_error(404, 2017, f"Invalid job id specified: {job_id}")   # ESLURM_INVALID_JOB_ID
    return j


def _user(request: Request) -> str:
    return _tenant(request) or "operator"


# ── NeoCloud routes ───────────────────────────────────────────────────
@router.post("/clusters", status_code=201)
def post_cluster(request: Request, body: dict = Body(default={})) -> dict:
    c = create_cluster(body, _tenant(request))
    return {**_public(c), "crs": crs(c)}


@router.get("/clusters")
def list_clusters(request: Request) -> list:
    t = _tenant(request)
    return [_public(c) for c in _S["clusters"].values() if not t or c["tenant_id"] == t]


@router.get("/clusters/{cid}")
def get_cluster(cid: str, request: Request) -> dict:
    c = _cluster(cid, request)
    return {**_public(c), "crs": crs(c), "events": c["events"][-50:]}


@router.delete("/clusters/{cid}", status_code=202)
def delete_cluster(cid: str, request: Request) -> dict:
    _cluster(cid, request)
    _S["clusters"].pop(cid, None)
    return {"id": cid, "deleted": True}


@router.get("/clusters/{cid}/crs")
def get_crs(cid: str, request: Request) -> list:
    return crs(_cluster(cid, request))


@router.get("/clusters/{cid}/topology.conf", response_class=PlainTextResponse)
def get_topology(cid: str, request: Request, plugin: Optional[str] = None) -> str:
    if plugin not in (None, "topology/block", "topology/tree"):
        raise HTTPException(400, "plugin must be topology/block or topology/tree")
    return topology_conf(_cluster(cid, request), plugin)


@router.post("/clusters/{cid}/jobs")
def sbatch(cid: str, request: Request, body: dict = Body(...)) -> dict:
    """sbatch emulation (plan F31) — slurmrestd JobSubmitReq in, job_submit response out,
    plus the job record (x-neocloud: placement, prolog verdicts, history)."""
    c = _cluster(cid, request)
    j = submit(c, body, _user(request))
    return {**_submit_response(c, j), "job": job_view(j)}


@router.get("/clusters/{cid}/jobs")
def list_jobs(cid: str, request: Request) -> dict:
    c = _cluster(cid, request)
    return _envelope(c, jobs=[job_view(j) for j in c["jobs"].values()])


@router.post("/clusters/{cid}/jobs/{job_id}:complete")
def complete_job(cid: str, job_id: int, request: Request, body: dict = Body(default={})) -> dict:
    """Emulator: the batch script exits (exit_code 0 → COMPLETED, else FAILED)."""
    c = _cluster(cid, request)
    j = _job(c, job_id)
    if j["job_state"][0] != "RUNNING":
        raise HTTPException(409, f"job {job_id} is {j['job_state'][0]}, not RUNNING")
    rc = int(body.get("exit_code", 0))
    return job_view(finish(c, j, "COMPLETED" if rc == 0 else "FAILED", rc))


@router.post("/clusters/{cid}/nodes/{name}:fail")
def post_fail_node(cid: str, name: str, request: Request, body: dict = Body(default={})) -> dict:
    """Emulator (operator): node failure while running → S4 recovery."""
    if _tenant(request):
        raise HTTPException(403, "operator only")
    return fail_node(_cluster(cid), name, body.get("reason", "Not responding (XID 79)"))


@router.post("/reset")
def post_reset(request: Request) -> dict:
    if _tenant(request):
        raise HTTPException(403, "operator only")
    reset()
    return {"reset": True}


# ── slurmrestd v0.0.42 passthrough ────────────────────────────────────
R = "/clusters/{cid}/slurm/" + API


@router.get(R + "/ping")
def rest_ping(cid: str, request: Request) -> dict:
    c = _cluster(cid, request)
    return _envelope(c, pings=[{"hostname": f"{c['name']}-controller-0", "pinged": "UP",
                                "latency": 120, "mode": "primary", "responding": True,
                                "primary": True}])


@router.post(R + "/job/submit")
def rest_submit(cid: str, request: Request, body: dict = Body(...)) -> dict:
    c = _cluster(cid, request)
    return _submit_response(c, submit(c, body, _user(request)))


@router.get(R + "/jobs")
def rest_jobs(cid: str, request: Request) -> dict:
    return list_jobs(cid, request)


@router.get(R + "/job/{job_id}")
def rest_job(cid: str, job_id: int, request: Request) -> dict:
    c = _cluster(cid, request)
    return _envelope(c, jobs=[job_view(_job(c, job_id))])


@router.delete(R + "/job/{job_id}")
def rest_cancel(cid: str, job_id: int, request: Request) -> dict:
    """scancel → CANCELLED (pending or running)."""
    c = _cluster(cid, request)
    j = _job(c, job_id)
    if j["job_state"][0] not in TERMINAL:
        finish(c, j, "CANCELLED", 0)
        j["exit_code"]["status"] = ["SIGNALED"]
    return _envelope(c)


@router.get(R + "/nodes")
def rest_nodes(cid: str, request: Request) -> dict:
    c = _cluster(cid, request)
    return _envelope(c, nodes=[node_view(c, n) for n in c["nodes"].values()])


@router.get(R + "/node/{name}")
def rest_node(cid: str, name: str, request: Request) -> dict:
    c = _cluster(cid, request)
    n = c["nodes"].get(name)
    if n is None:
        _slurm_error(404, 2008, f"Invalid node name specified: {name}")
    return _envelope(c, nodes=[node_view(c, n)])


@router.post(R + "/node/{name}")
def rest_update_node(cid: str, name: str, request: Request, body: dict = Body(...)) -> dict:
    """scontrol update NodeName= State=DRAIN|RESUME Reason= (update_node_msg)."""
    if _tenant(request):
        raise HTTPException(403, "operator only")
    c = _cluster(cid)
    n = c["nodes"].get(name)
    if n is None:
        _slurm_error(404, 2008, f"Invalid node name specified: {name}")
    st = body.get("state") or []
    st = [st] if isinstance(st, str) else st
    with _LOCK:
        if "RESUME" in st or "IDLE" in st:
            n["state"] = ["ALLOCATED"] if n["job_id"] else ["IDLE"]
            n["reason"] = ""
            _event(c, "node", f"{name} RESUME", node=name)
            schedule(c)
        elif "DRAIN" in st:
            n["state"] = (["ALLOCATED", "DRAIN"] if n["job_id"] else ["IDLE", "DRAIN"])
            n["reason"] = body.get("reason") or "drained by operator"
            _event(c, "node", f"{name} DRAIN — {n['reason']}", node=name)
        elif "DOWN" in st:
            return {**fail_node(c, name, body.get("reason") or "set DOWN by operator"), **_envelope(c)}
        else:
            _slurm_error(400, 2008, f"unsupported node state {st}")
    return _envelope(c)


@router.get(R + "/partitions")
def rest_partitions(cid: str, request: Request) -> dict:
    c = _cluster(cid, request)
    names = [n["name"] for n in c["nodes"].values()]
    return _envelope(c, partitions=[{
        "name": p, "nodes": {"configured": hostlist(names), "total": len(names)},
        "partition": {"state": ["UP"]}, "flags": ["DEFAULT"] if meta.get("default") else [],
        "tres": {"configured": f"cpu={72 * len(names)},gres/gpu={sum(n['gpus'] for n in c['nodes'].values())},node={len(names)}"},
        "topology": c["topology_plugin"]} for p, meta in c["partitions"].items()])
