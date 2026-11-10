"""F11 NVKE depth demo — 2-rack node pool → CAPI/CAPNICo objects, 2 cliques,
2 IMEX ComputeDomains, Topograph labels, replacement in the same rack, upgrade.

Self-contained (no import of app/demo/engine.py); the step format follows
engine.py. Deterministic on a fresh stack (`scripts/demo_all.sh --reset-all`):
fixed tenant `tnt-f11-demo-co`, cluster deleted in the last step. Repeated runs
without a reset leave the replaced host cordoned (RMA) and trays sanitizing in
NICo, so the pool may span three racks and the replacement may leave the rack;
the steps report that instead of failing.
"""

TENANT_NAME = "f11-demo-co"
TENANT = "tnt-f11-demo-co"
CLIQUE = "nvidia.com/gpu.clique"
ZONE = "topology.kubernetes.io/zone"


def http(method, path, json=None, params=None):
    spec = {"method": method, "path": path, "as": "operator", "service": "nocp"}
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def link(console, route, ko, en):
    return {"console": console, "route": route, "label_ko": ko, "label_en": en}


def _b(res):
    return res.body if isinstance(res.body, (dict, list)) else {}


def _cid(ctx):
    return ctx["cluster_id"]


def _tenant(res, ctx):
    return (res.status in (201, 409), f"데모 테넌트 {TENANT} 준비 (HTTP {res.status})",
            f"demo tenant {TENANT} ready (HTTP {res.status})")


def _created(res, ctx):
    b = _b(res)
    return (res.status == 201 and b.get("node_count") == 36,
            f"클러스터 {b.get('id')} · 노드 {b.get('node_count')}대 (GPU 144) · "
            f"etcd 암호화 {b.get('control_plane', {}).get('etcd_encryption')}",
            f"cluster {b.get('id')} · {b.get('node_count')} nodes (144 GPUs) · "
            f"etcd encryption {b.get('control_plane', {}).get('etcd_encryption')}")


def _save_cid(res, ctx):
    ctx["cluster_id"] = res.body["id"]


def _capi(res, ctx):
    b = _b(res)
    mds = b.get("machine_deployments") or []
    nm = b.get("nico_machines") or []
    ctx["racks"] = len(mds)   # 2 on a fresh seed; 3 when trays are still sanitizing
    return (b.get("cluster", {}).get("phase") == "Provisioned" and len(mds) >= 2
            and len(nm) == 36,
            f"CAPI Cluster {b.get('cluster', {}).get('phase')} · MachineDeployment {len(mds)}개 "
            f"(failureDomain {', '.join(m['failure_domain'] for m in mds)}) · NicoMachine {len(nm)}개",
            f"CAPI Cluster {b.get('cluster', {}).get('phase')} · {len(mds)} MachineDeployments "
            f"(failureDomain {', '.join(m['failure_domain'] for m in mds)}) · {len(nm)} NicoMachines")


def _nodes(res, ctx):
    rows = _b(res) if isinstance(res.body, list) else []
    cliques = sorted({n["labels"].get(CLIQUE) for n in rows})
    joined = sum(1 for n in rows if n["join"]["state"] == "Joined")
    ok = len(rows) == 36 and len(cliques) == ctx.get("racks", 2) and joined == 36
    return (ok, f"노드 {len(rows)}대 · gpu.clique {len(cliques)}종 · nvke-agent 합류 {joined}대 · TPM 검증",
            f"{len(rows)} nodes · {len(cliques)} gpu.clique values · {joined} joined via nvke-agent · TPM verified")


def _cds(res, ctx):
    rows = _b(res) if isinstance(res.body, list) else []
    ready = [c for c in rows if c["status"] == "Ready"]
    return (len(rows) == ctx.get("racks", 2) and len(ready) == len(rows),
            f"IMEX ComputeDomain {len(rows)}개 Ready (랙 = IMEX 도메인) · 노드 "
            f"{'/'.join(str(r['num_nodes']) for r in rows)}",
            f"{len(rows)} IMEX ComputeDomains Ready (rack = IMEX domain) · nodes "
            f"{'/'.join(str(r['num_nodes']) for r in rows)}")


def _generated(res, ctx):
    return (res.status == 202 and isinstance(res.body, str) and len(res.body) == 32,
            f"Topograph 요청 접수 202 · uid {res.body}", f"Topograph accepted 202 · uid {res.body}")


def _save_uid(res, ctx):
    ctx["uid"] = res.body


def _topology(res, ctx):
    b = _b(res)
    doms = b.get("acceleratorDomains") or []
    return (b.get("status") == "OK" and len(doms) == ctx.get("racks", 2),
            f"가속기 도메인 {len(doms)}개 · 노드 라벨 {len(b.get('nodeLabels') or {})}개 (ib-leaf/ib-spine)",
            f"{len(doms)} accelerator domains · {len(b.get('nodeLabels') or {})} node label sets (ib-leaf/ib-spine)")


def _scaled(res, ctx):
    b = _b(res)
    return (res.status == 200 and len(b.get("nodes") or []) == 34,
            "노드풀 36 → 34 축소 (두 번째 랙에 여유 트레이 2개)",
            "node pool scaled 36 → 34 (two spare trays in the second rack)")


def _save_pool(res, ctx):
    ctx["pool_id"] = res.body["nodepools"][0]["id"]


def _replaced(res, ctx):
    b = _b(res)
    kept = b.get("failure_domain_kept") is True
    ko = (f"같은 랙 {b.get('failure_domain')} 유지" if kept
          else f"랙 {b.get('failure_domain')}에 여유 트레이 없음 → 다른 랙으로 대체")
    en = (f"same rack {b.get('failure_domain')} kept" if kept
          else f"no spare tray in {b.get('failure_domain')} → placed in another rack")
    return (res.status == 200 and b.get("new_host") != b.get("old_host"),
            f"NVSentinel 교체: {b.get('old_host')} → {b.get('new_host')} · {ko}",
            f"NVSentinel replace: {b.get('old_host')} → {b.get('new_host')} · {en}")


def _upgraded(res, ctx):
    b = _b(res)
    up = b.get("upgrade") or {}
    st = (up.get("steps") or [{}, {}])
    return (res.status == 200 and up.get("to") == "v1.34.1",
            f"업그레이드 {up.get('from')} → {up.get('to')} · 제어부 먼저, 노드풀 {st[-1].get('batches')}배치 in-place",
            f"upgrade {up.get('from')} → {up.get('to')} · control plane first, pool in {st[-1].get('batches')} in-place batches")


def _after(res, ctx):
    b = _b(res)
    ms = b.get("machines") or []
    ok = ms and all(m["version"] == "v1.34.1" and m["phase"] == "Running" for m in ms)
    return (bool(ok), f"Machine {len(ms)}대 모두 v1.34.1 Running", f"all {len(ms)} Machines v1.34.1 Running")


def _deleted(res, ctx):
    return (res.status == 200, "데모 클러스터 삭제 · NICo 인스턴스 반납",
            "demo cluster deleted · NICo instances returned")


SCENARIO = {
    "id": "f11-nvke-capi",
    "order": 11,
    "feature": "F11",
    "title_ko": "F11 NVKE 심화 (CAPI·CAPNICo · IMEX ComputeDomain · Topograph · 교체 · 업그레이드)",
    "title_en": "F11 NVKE depth (CAPI·CAPNICo · IMEX ComputeDomain · Topograph · replace · upgrade)",
    "summary_ko": "2랙 노드풀 → MachineDeployment(랙=failureDomain) → NicoMachine = NICo 인스턴스 → "
                  "클리크 라벨 2종·ComputeDomain 2개 → Topograph → 같은 랙 교체 → in-place 업그레이드.",
    "summary_en": "2-rack pool → MachineDeployments (rack = failureDomain) → NicoMachine = NICo instance → "
                  "2 clique labels, 2 ComputeDomains → Topograph → same-rack replace → in-place upgrade.",
    "steps": [
        {"id": "00-tenant", "title_ko": "데모 테넌트", "title_en": "Demo tenant",
         "call": http("POST", "/api/v1/tenants",
                      {"name": TENANT_NAME, "isolation_tier": "bare_metal_dedicated"}),
         "expect": _tenant},
        {"id": "01-create", "title_ko": "2랙 노드풀 클러스터 생성", "title_en": "Create a 2-rack cluster",
         "narration_ko": "vr-nvl72 트레이 36대(GPU 144) 노드풀. NVKE가 CAPI Cluster·MachineDeployment를 만든다.",
         "narration_en": "36 vr-nvl72 trays (144 GPUs). NVKE creates the CAPI Cluster and MachineDeployments.",
         "call": http("POST", "/api/v1/nvke/clusters",
                      {"name": "f11-demo", "tenant_id": TENANT, "k8s_version": "1.33",
                       "control_plane": {"etcd_encryption": True},
                       "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 36}]}),
         "expect": _created, "save": lambda res, ctx: (_save_cid(res, ctx), _save_pool(res, ctx)),
         "links": [link("customer", "nvke", "고객 · NVKE", "Customer · NVKE")]},
        {"id": "02-capi", "title_ko": "CAPI·CAPNICo 객체", "title_en": "CAPI · CAPNICo objects",
         "call": http("GET", lambda ctx: f"/api/v1/nvke/clusters/{_cid(ctx)}/capi"),
         "expect": _capi, "links": [link("ops", "mk8s", "운영 · Managed K8s", "Ops · Managed K8s")]},
        {"id": "03-nodes", "title_ko": "노드 라벨·합류·증명", "title_en": "Node labels, join, attestation",
         "call": http("GET", lambda ctx: f"/api/v1/nvke/clusters/{_cid(ctx)}/nodes"),
         "expect": _nodes},
        {"id": "04-imex", "title_ko": "IMEX ComputeDomain", "title_en": "IMEX ComputeDomains",
         "call": http("GET", lambda ctx: f"/api/v1/nvke/clusters/{_cid(ctx)}/compute-domains"),
         "expect": _cds},
        {"id": "05-topograph", "title_ko": "Topograph 생성 요청", "title_en": "Topograph generate",
         "call": http("POST", "/api/v1/topograph/v1/generate",
                      lambda ctx: {"provider": {"name": "nvke", "params": {"cluster_id": _cid(ctx)}},
                                   "engine": {"name": "k8s", "params": {"fabricLabels": [
                                       "network.topology.nvidia.com/ib-leaf",
                                       "network.topology.nvidia.com/ib-spine"]}}}),
         "expect": _generated, "save": _save_uid},
        {"id": "06-topology", "title_ko": "Topograph 결과", "title_en": "Topograph result",
         "call": http("GET", "/api/v1/topograph/v1/topology",
                      params=lambda ctx: {"uid": ctx["uid"]}),
         "expect": _topology},
        {"id": "07-scale", "title_ko": "노드풀 34로 축소", "title_en": "Scale pool to 34",
         "call": http("PATCH", lambda ctx: f"/api/v1/nvke/clusters/{_cid(ctx)}/nodepools/{ctx['pool_id']}",
                      {"count": 34}),
         "expect": _scaled},
        {"id": "08-replace", "title_ko": "NVSentinel 노드 교체 (같은 랙)", "title_en": "NVSentinel replace (same rack)",
         "call": http("POST", lambda ctx: f"/api/v1/nvke/clusters/{_cid(ctx)}/nodes/gpu-20/replace",
                      {"reason": "XID 79 (GPU fallen off the bus)", "source": "nvsentinel"}),
         "expect": _replaced},
        {"id": "09-upgrade", "title_ko": "K8s 1.33 → 1.34 업그레이드", "title_en": "Upgrade K8s 1.33 → 1.34",
         "call": http("POST", lambda ctx: f"/api/v1/nvke/clusters/{_cid(ctx)}/upgrade",
                      {"k8s_version": "1.34", "max_unavailable": 9}),
         "expect": _upgraded},
        {"id": "10-after", "title_ko": "업그레이드 후 Machine", "title_en": "Machines after upgrade",
         "call": http("GET", lambda ctx: f"/api/v1/nvke/clusters/{_cid(ctx)}/capi"),
         "expect": _after},
        {"id": "11-cleanup", "title_ko": "정리", "title_en": "Clean up",
         "call": http("DELETE", lambda ctx: f"/api/v1/nvke/clusters/{_cid(ctx)}"),
         "expect": _deleted},
    ],
}
