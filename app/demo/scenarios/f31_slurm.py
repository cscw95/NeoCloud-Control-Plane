"""F31 Slurm (Slinky) demo — sbatch, topology.conf, Health Validation prolog, node-failure requeue.

Self-contained; format follows app/demo/engine.py. Deterministic: step 00 resets Slurm and
F28 injections; the cluster always takes the first two VR NVL72 racks (36 nodes, 144 GPUs)."""

SCRIPT = ("#!/bin/bash\n#SBATCH --job-name={name}\n#SBATCH --nodes=18\n#SBATCH --gres=gpu:4\n"
          "#SBATCH --time=04:00:00\nsrun torchrun --nnodes=18 train.py\n")


def http(method, path, json=None):
    spec = {"method": method, "path": path, "as": "operator", "service": "nocp"}
    if json is not None:
        spec["json"] = json
    return spec


def _b(res):
    return res.body if isinstance(res.body, dict) else {}


def _p(suffix):
    return lambda ctx: f"/api/v1/slurm/clusters/{ctx['cid']}{suffix}"


def _ok(res, ctx):
    return (res.status < 400, f"HTTP {res.status}", f"HTTP {res.status}")


def _save_cluster(res, ctx):
    b = _b(res)
    ctx["cid"] = b["id"]
    ns = next(cr for cr in b["crs"] if cr["kind"] == "NodeSet")
    ctx["tray"] = ns["status"]["ordinalToNode"]


def _cluster(res, ctx):
    b = _b(res)
    kinds = [cr["kind"] for cr in b.get("crs") or []]
    return (b.get("nodes_total") == 36, f"{b.get('id')}: {kinds} · 노드 {b.get('nodes_total')} · GPU {b.get('gpus_total')}",
            f"{b.get('id')}: {kinds} · {b.get('nodes_total')} nodes · {b.get('gpus_total')} GPUs")


def _topo(res, ctx):
    text = res.body if isinstance(res.body, str) else ""
    lines = [x for x in text.splitlines() if x and not x.startswith("#")]
    return ("BlockName=block001" in text and "BlockSizes=18,36" in text, " | ".join(lines),
            " | ".join(lines))


def _job(key, state, single=None, restarts=None):
    def expect(res, ctx):
        b = _b(res)
        j = b.get("job") or (b.get("jobs") or [{}])[0]
        if key:
            ctx[key] = j.get("job_id")
        pl = (j.get("x-neocloud") or {}).get("placement") or {}
        hist = " → ".join(h["state"] for h in (j.get("x-neocloud") or {}).get("history") or [])
        ok = j.get("job_state") == [state]
        if single is not None:
            ok = ok and pl.get("single_block") is single
        if restarts is not None:
            ok = ok and j.get("restart_cnt") == restarts
        return (ok, f"job {j.get('job_id')} {j.get('job_state')} nodes={j.get('nodes') or '-'} "
                    f"blocks={pl.get('blocks')} restart_cnt={j.get('restart_cnt')} · {hist}",
                f"job {j.get('job_id')} {j.get('job_state')} nodes={j.get('nodes') or '-'} "
                f"blocks={pl.get('blocks')} restart_cnt={j.get('restart_cnt')} · {hist}")
    return expect


def _failed(res, ctx):
    b = _b(res)
    j = b.get("job") or {}
    return (b.get("state") == ["DOWN"] and j.get("job_state") == ["RUNNING"] and "gpu-005" not in (j.get("nodes") or ""),
            f"gpu-005 DOWN · job {j.get('job_id')} 재큐 후 {j.get('job_state')} nodes={j.get('nodes')} "
            f"restart_cnt={j.get('restart_cnt')} · F10 {b.get('remediation', {}).get('status')}",
            f"gpu-005 DOWN · job {j.get('job_id')} requeued → {j.get('job_state')} nodes={j.get('nodes')} "
            f"restart_cnt={j.get('restart_cnt')} · F10 {b.get('remediation', {}).get('status')}")


def _nodes(res, ctx):
    ns = _b(res).get("nodes") or []
    drained = [f"{n['name']}({n['reason']})" for n in ns if "DRAIN" in n["state"] or "DOWN" in n["state"]]
    return (len(drained) == 2, f"DRAIN/DOWN: {drained}", f"DRAIN/DOWN: {drained}")


SCENARIO = {
    "id": "f31-slurm",
    "order": 31,
    "feature": "F31",
    "title_ko": "F31 Slurm(Slinky) — sbatch · topology.conf · Health Validation prolog",
    "title_en": "F31 Slurm (Slinky) — sbatch · topology.conf · Health Validation prolog",
    "summary_ko": "같은 NVL72 블록 배치, prolog 실패 노드 DRAIN 후 재큐, 노드 장애 시 제외하고 재시작",
    "summary_en": "Same-NVL72-block placement, prolog failure drains + requeues, node failure restarts without it",
    "steps": [
        {"id": "00-reset", "title_ko": "초기화", "title_en": "Reset",
         "call": http("POST", "/api/v1/slurm/reset"), "expect": _ok},
        {"id": "01-reset-hv", "title_ko": "F28 주입 초기화", "title_en": "Reset F28 injections",
         "call": http("POST", "/api/v1/readiness/reset"), "expect": _ok},
        {"id": "02-cluster", "title_ko": "Slinky 클러스터 생성 (2랙)", "title_en": "Create Slinky cluster (2 racks)",
         "call": http("POST", "/api/v1/slurm/clusters", {"name": "demo-train", "rack_count": 2}),
         "expect": _cluster, "save": _save_cluster,
         "narration_ko": "Controller·NodeSet·RestApi CR(slinky.slurm.net/v1beta1)이 생긴다.",
         "narration_en": "Controller, NodeSet and RestApi CRs (slinky.slurm.net/v1beta1) are created."},
        {"id": "03-topology", "title_ko": "topology.conf (topology/block)", "title_en": "topology.conf (topology/block)",
         "call": http("GET", _p("/topology.conf")), "expect": _topo},
        {"id": "04-sbatch-1", "title_ko": "sbatch 18노드 → 한 블록", "title_en": "sbatch 18 nodes → one block",
         "call": http("POST", _p("/jobs"), {"job": {"script": SCRIPT.format(name="pretrain-a")}}),
         "expect": _job("j1", "RUNNING", single=True)},
        {"id": "05-inject", "title_ko": "블록2 노드(gpu-023)에 XID 79 주입", "title_en": "Inject XID 79 on block-2 node gpu-023",
         "call": http("POST", "/api/v1/readiness/inject",
                      {"node": lambda ctx: ctx["tray"]["23"], "gpu": 0, "signals": {"DCGM_FI_DEV_XID_ERROR": 79}}),
         "expect": _ok},
        {"id": "06-sbatch-2", "title_ko": "sbatch 18노드 → prolog 실패 → DRAIN·재큐",
         "title_en": "sbatch 18 nodes → prolog fails → DRAIN + requeue",
         "call": http("POST", _p("/jobs"), {"job": {"script": SCRIPT.format(name="pretrain-b")}}),
         "expect": _job("j2", "PENDING", restarts=1),
         "narration_ko": "prolog(F28)가 gpu-023을 막아 DRAIN, 남은 17노드로는 부족해 Resources 대기.",
         "narration_en": "The F28 prolog blocks gpu-023 (DRAIN); 17 nodes are not enough, so it waits on Resources."},
        {"id": "07-complete-1", "title_ko": "job 1 종료 → job 2 블록1에서 시작", "title_en": "Job 1 ends → job 2 starts on block 1",
         "call": http("POST", lambda ctx: f"/api/v1/slurm/clusters/{ctx['cid']}/jobs/{ctx['j1']}:complete", {"exit_code": 0}),
         "expect": _ok},
        {"id": "08-job-2", "title_ko": "job 2 상태", "title_en": "Job 2 state",
         "call": http("GET", lambda ctx: f"/api/v1/slurm/clusters/{ctx['cid']}/slurm/v0.0.42/job/{ctx['j2']}"),
         "expect": _job(None, "RUNNING", single=True)},
        {"id": "09-node-fail", "title_ko": "gpu-005 장애 → 제외하고 재시작", "title_en": "gpu-005 fails → restart without it",
         "call": http("POST", _p("/nodes/gpu-005:fail"), {"reason": "XID 79 GPU fell off the bus"}),
         "expect": _failed},
        {"id": "10-nodes", "title_ko": "노드 상태 (slurmrestd)", "title_en": "Node states (slurmrestd)",
         "call": http("GET", _p("/slurm/v0.0.42/nodes")), "expect": _nodes},
    ],
}
