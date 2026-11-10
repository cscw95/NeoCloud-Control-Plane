"""F30 — KAI scheduling on an NVKE cluster: queues/quotas, gang, NVL72
clique-first placement, reclaim, stale-gang eviction after a node fault and
grid curtailment (preemptible first).

Self-contained (plain HTTP call specs, expect → (ok, detail_ko, detail_en)) so it
runs under the v2-demo runner (app/demo/engine.py) and in tests/test_kai_scheduler.py.
Re-runnable: step 01/02 delete the previous run's cluster of the demo tenant.
"""

TENANT = "demo-f30-sched"
TID = f"tnt-{TENANT}"


def _base(c):
    return f"/api/v1/nvke/clusters/{c['cluster_id']}"


def _ok(cond, ko, en):
    return bool(cond), ko, en


def _tenant(res, ctx):
    ctx["tenant_id"] = res.body["id"] if res.status == 201 else TID
    return _ok(res.status in (201, 409), f"테넌트 {ctx['tenant_id']} 준비 (HTTP {res.status})",
               f"tenant {ctx['tenant_id']} ready (HTTP {res.status})")


def _old(res, ctx):
    rows = res.body if isinstance(res.body, list) else []
    ctx["old"] = next((c["id"] for c in rows if c.get("source") == "nvke"), "none")
    return _ok(res.status == 200, f"이전 실행 클러스터: {ctx['old']}", f"previous run cluster: {ctx['old']}")


def _deleted(res, ctx):
    return _ok(res.status in (200, 404), f"이전 클러스터 정리 (HTTP {res.status})",
               f"previous cluster cleaned up (HTTP {res.status})")


def _cluster(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    if res.status == 201:
        ctx["cluster_id"] = b["id"]
    racks = sorted({n.get("tray_id", "").rsplit("-tray-", 1)[0]
                    for p in b.get("nodepools") or [] for n in p.get("nodes") or []})
    # >= 2: on a reused stack the host replaced in a previous run stays cordoned
    # (RMA), so the pool can spill onto a third rack
    return _ok(res.status == 201 and b.get("node_count") == 36 and len(racks) >= 2,
               f"클러스터 {b.get('id')} · 노드 {b.get('node_count')}대 · 144 GPU · NVL72 클리크 {len(racks)}개",
               f"cluster {b.get('id')} · {b.get('node_count')} nodes · 144 GPU · {len(racks)} NVL72 cliques")


def _queue(name):
    def f(res, ctx):
        b = res.body if isinstance(res.body, dict) else {}
        g = ((b.get("resource") or {}).get("spec") or {}).get("resources", {}).get("gpu", {})
        return _ok(res.status == 201 and b.get("resource", {}).get("kind") == "Queue",
                   f"Queue {name} · quota {g.get('quota')} GPU · priority {b.get('priority')}",
                   f"Queue {name} · quota {g.get('quota')} GPU · priority {b.get('priority')}")
    return f


def _big(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    pl = b.get("placement") or {}
    return _ok(res.status == 201 and b.get("state") == "Running" and pl.get("clique_count", 0) >= 2
               and b.get("over_quota"),
               f"갱 {b.get('pods')}개 파드 동시 배치 · 클리크 {pl.get('clique_count')}개 · 쿼터 초과분은 회수 대상",
               f"gang of {b.get('pods')} pods bound together · {pl.get('clique_count')} cliques · over-quota share is reclaimable")


def _prod(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    pl = b.get("placement") or {}
    racks = ", ".join(c.get("rack", "") for c in pl.get("cliques") or [])
    return _ok(res.status == 201 and b.get("state") == "Running" and b.get("preempted") == ["research-llm"],
               f"prod 큐가 쿼터를 되찾음(reclaim): research-llm 갱 전체 축출 → prod-ft 클리크 {pl.get('clique_count')}개 ({racks})",
               f"prod queue reclaims its quota: research-llm gang evicted whole → prod-ft on {pl.get('clique_count')} clique(s) ({racks})")


def _view(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    cap = b.get("capacity") or {}
    used = ", ".join(f"{c['rack']} {c['used_gpus']}/{c['gpus']}" for c in b.get("cliques") or [])
    return _ok(res.status == 200 and b.get("workloads", {}).get("Preempted") == 1,
               f"사용 {cap.get('used_gpus')}/{cap.get('gpus')} GPU · 클리크별 {used} · 대기(축출) 1건",
               f"used {cap.get('used_gpus')}/{cap.get('gpus')} GPU · per clique {used} · 1 preempted waiting")


def _done(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    return _ok(res.status == 200 and "research-llm" in (b.get("started") or []),
               "prod-ft 종료 → 다음 세션에서 research-llm 갱 재시작",
               "prod-ft finished → research-llm gang restarts in the next session")


def _node(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    pods = (b.get("placement") or {}).get("pods") or []
    ctx["node"] = pods[-1]["node"] if pods else "none"
    return _ok(res.status == 200 and b.get("state") == "Running" and pods,
               f"research-llm 실행 중 · 장애 주입 대상 노드 {ctx['node']}",
               f"research-llm running · fault target node {ctx['node']}")


def _replaced(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    return _ok(res.status == 200, f"NVSentinel 노드 교체 {b.get('old_host')} → {b.get('new_host')}",
               f"NVSentinel node replace {b.get('old_host')} → {b.get('new_host')}")


def _stale(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    ev = [e for e in b.get("evictions") or [] if e.get("action") == "stalegangeviction"]
    return _ok(res.status == 200 and ev and b.get("state") == "Running",
               "갱 구성원 노드 유실 → 갱 전체 축출(stalegangeviction) 후 정상 노드에 재배치",
               "gang member node lost → whole gang evicted (stalegangeviction) and re-placed on healthy nodes")


def _curtail(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    return _ok(res.status == 200 and "research-llm" in (b.get("evicted") or []),
               f"전력 감축으로 {b.get('fenced_gpus')} GPU 차단 · 선점 가능 작업 우선 축출: {', '.join(b.get('evicted') or [])}",
               f"curtailment fences {b.get('fenced_gpus')} GPUs · preemptible jobs go first: {', '.join(b.get('evicted') or [])}")


def _release(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    return _ok(res.status == 200 and "research-llm" in (b.get("started") or []),
               "감축 종료 → GPU 차단 해제 · research-llm 재시작",
               "curtailment over → GPUs unfenced · research-llm restarts")


def _podgroups(res, ctx):
    items = (res.body or {}).get("items") or [] if isinstance(res.body, dict) else []
    pg = next((i for i in items if i["metadata"]["name"].startswith("pg-research-llm-")), {})
    sp = pg.get("spec") or {}
    return _ok(res.status == 200 and sp.get("minMember") == 36,
               f"PodGroup {pg.get('metadata', {}).get('name', '-')[:28]}… minMember {sp.get('minMember')} · queue {sp.get('queue')} · {sp.get('preemptibility')}",
               f"PodGroup {pg.get('metadata', {}).get('name', '-')[:28]}… minMember {sp.get('minMember')} · queue {sp.get('queue')} · {sp.get('preemptibility')}")


def _step(sid, ko, en, method, path, expect, json=None, nko="", nen="", save=None):
    s = {"id": sid, "title_ko": ko, "title_en": en, "feature": "F30",
         "call": {"method": method, "path": path, **({"json": json} if json is not None else {})},
         "expect": expect, "narration_ko": nko, "narration_en": nen,
         "links": [{"console": "ops", "route": "nvke-sched", "label_ko": "운영 · 스케줄러",
                    "label_en": "Ops · scheduler"}]}
    if save:
        s["save"] = save
    return s


SCENARIO = {
    "id": "f30-kai-scheduling",
    "order": 30,
    "feature": "F30",
    "title_ko": "F30 KAI 스케줄링: 큐·쿼터·갱·클리크 우선·선점",
    "title_en": "F30 KAI scheduling: queues, quotas, gang, clique-first, preemption",
    "summary_ko": "NVL72 2랙 클러스터에서 쿼터 회수(reclaim), 노드 장애 시 갱 재배치, 전력 감축 시 선점 가능 작업 우선 축출을 본다.",
    "summary_en": "On a 2-rack NVL72 cluster: quota reclaim, gang re-placement after a node fault, curtailment evicting preemptible work first.",
    "steps": [
        _step("00-tenant", "데모 테넌트", "Demo tenant", "POST", "/api/v1/tenants", _tenant,
              {"name": TENANT, "isolation_tier": "k8s_namespace"}),
        _step("01-find-old", "이전 실행 확인", "Find previous run", "GET",
              lambda c: f"/api/v1/nvke/clusters?tenant_id={c['tenant_id']}&include_nkd=false", _old),
        _step("02-cleanup", "이전 클러스터 삭제", "Delete previous cluster", "DELETE",
              lambda c: f"/api/v1/nvke/clusters/{c['old']}", _deleted),
        _step("03-cluster", "NVL72 2랙 NVKE 클러스터", "2-rack NVL72 NVKE cluster", "POST",
              "/api/v1/nvke/clusters", _cluster,
              lambda c: {"name": "sched-demo", "tenant_id": c["tenant_id"],
                         "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 36}]},
              "노드마다 nvidia.com/gpu.clique 라벨 = 랙의 NVLink 도메인. KAI Topology가 이 라벨을 쓴다.",
              "Each node carries nvidia.com/gpu.clique = its rack's NVLink domain; the KAI Topology uses it."),
        _step("04-queue-prod", "Queue prod (quota 72, priority 200)", "Queue prod (quota 72, priority 200)",
              "POST", lambda c: _base(c) + "/queues", _queue("prod"),
              {"name": "prod", "quota_gpus": 72, "priority": 200}),
        _step("05-queue-research", "Queue research (quota 72)", "Queue research (quota 72)",
              "POST", lambda c: _base(c) + "/queues", _queue("research"),
              {"name": "research", "quota_gpus": 72, "priority": 100}),
        _step("06-research-144", "research: PyTorchJob 144 GPU", "research: PyTorchJob 144 GPU",
              "POST", lambda c: _base(c) + "/workloads", _big,
              {"kind": "PyTorchJob", "name": "research-llm", "gpus": 144, "queue": "research",
               "priority_class": "train"},
              "빈 자원이 있어 쿼터(72)를 넘어 144 GPU 전부 사용. 36개 파드는 갱으로 한꺼번에 묶인다.",
              "Free capacity lets research run 144 GPUs over its 72 quota; 36 pods bind as one gang."),
        _step("07-prod-72", "prod: PyTorchJob 72 GPU (build)", "prod: PyTorchJob 72 GPU (build)",
              "POST", lambda c: _base(c) + "/workloads", _prod,
              {"kind": "PyTorchJob", "name": "prod-ft", "gpus": 72, "queue": "prod",
               "priority_class": "build"},
              "prod는 쿼터 안쪽이므로 쿼터를 넘긴 research를 회수(reclaim)한다.",
              "prod is within quota, so it reclaims from research, which is over quota."),
        _step("08-scheduler", "스케줄러 상태", "Scheduler state", "GET",
              lambda c: _base(c) + "/scheduler", _view),
        _step("09-complete", "prod-ft 종료", "prod-ft finishes", "DELETE",
              lambda c: _base(c) + "/workloads/prod-ft", _done),
        _step("10-node", "research-llm 노드 확인", "research-llm nodes", "GET",
              lambda c: _base(c) + "/workloads/research-llm", _node),
        _step("11-fault", "장애 주입: 노드 교체 (XID 79)", "Inject fault: node replace (XID 79)", "POST",
              lambda c: _base(c) + f"/nodes/{c['node']}/replace", _replaced,
              {"reason": "XID 79 GPU fallen off the bus (demo)", "source": "nvsentinel"}),
        _step("12-stale-gang", "갱 재배치 확인", "Gang re-placed", "GET",
              lambda c: _base(c) + "/workloads/research-llm", _stale),
        _step("13-curtail", "전력 감축: 72 GPU 차단", "Grid curtailment: fence 72 GPUs", "POST",
              lambda c: _base(c) + "/scheduler/curtail", _curtail,
              {"gpus": 72, "reason": "DSX Flex load target -10 MW (demo)"}),
        _step("14-release", "감축 종료", "Curtailment released", "DELETE",
              lambda c: _base(c) + "/scheduler/curtail", _release),
        _step("15-podgroups", "PodGroup CR (scheduling.run.ai/v2alpha2)", "PodGroup CR (scheduling.run.ai/v2alpha2)",
              "GET", lambda c: _base(c) + "/apis/scheduling.run.ai/v2alpha2/podgroups", _podgroups),
    ],
}
