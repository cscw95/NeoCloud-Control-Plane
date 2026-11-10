"""(g) US-04 — NVKE cluster with a clique-aware 2-rack nodepool: CAPI objects
(Cluster / MachineDeployment / NicoMachine, failure domain = rack), one IMEX
ComputeDomain per NVL72 rack, `nvidia.com/gpu.clique` labels, kubeconfig.

Contracts: F11 (`/api/v1/nvke/clusters/{id}/capi|nodes|compute-domains`). Cluster
create and kubeconfig exist on main.
"""

import re

from ..engine import check, http
from ._common import link, tenant_step


def _created(res, ctx):
    b = res.body
    if res.status == 201:
        ctx["cluster_id"] = b["id"]
    racks = sorted({n.get("tray_id", "").rsplit("-tray-", 1)[0]
                    for p in b.get("nodepools") or [] for n in p.get("nodes") or []})
    ctx["racks"] = ",".join(racks)
    return check(res.status == 201 and b.get("node_count") == 36 and len(racks) == 2,
                 f"클러스터 {b.get('id')} · 노드 {b.get('node_count')}대 (144 GPU) · 랙 {', '.join(racks)}",
                 f"cluster {b.get('id')} · {b.get('node_count')} nodes (144 GPU) · racks {', '.join(racks)}")


def _capi_ok(res, ctx):
    b = res.body
    md = b.get("machine_deployments") or []
    nm = b.get("nico_machines") or []
    fds = sorted({m.get("failure_domain") for m in md} | {m.get("rack_id") for m in nm})
    return check(b.get("cluster") and nm and len([f for f in fds if f]) >= 2,
                 f"CAPI Cluster {(b.get('cluster') or {}).get('phase')} · MachineDeployment {len(md)} · NicoMachine {len(nm)} · failure domain(랙) {len(fds)}",
                 f"CAPI Cluster {(b.get('cluster') or {}).get('phase')} · {len(md)} MachineDeployments · {len(nm)} NicoMachines · {len(fds)} failure domains (racks)")


def _cliques_ok(res, ctx):
    nodes = res.body if isinstance(res.body, list) else res.body.get("nodes", [])
    cliques = sorted({(n.get("labels") or {}).get("nvidia.com/gpu.clique") for n in nodes} - {None})
    fmt_ok = all(re.match(r"^[^.]+\.[^.]+$", c) for c in cliques)
    att = sum((n.get("attestation") or {}).get("tpm") == "verified" for n in nodes)
    return check(len(cliques) == 2 and fmt_ok,
                 f"gpu.clique 라벨 {len(cliques)}종 ({', '.join(cliques)}) · TPM 증명 {att}/{len(nodes)}",
                 f"{len(cliques)} gpu.clique labels ({', '.join(cliques)}) · TPM attested {att}/{len(nodes)}")


def _domains_ok(res, ctx):
    rows = res.body if isinstance(res.body, list) else res.body.get("items", [res.body])
    return check(len(rows) == 2, f"IMEX ComputeDomain {len(rows)}개 (NVL72 랙당 1개)",
                 f"{len(rows)} IMEX ComputeDomains (one per NVL72 rack)")


def _kubeconfig_ok(res, ctx):
    kc = res.body.get("kubeconfig", "") if isinstance(res.body, dict) else ""
    srv = re.search(r"server:\s*(\S+)", kc)
    return check("apiVersion: v1" in kc and "kind: Config" in kc,
                 f"kubeconfig 발급 · server {srv.group(1) if srv else '-'} · 인증 {res.body.get('auth')}",
                 f"kubeconfig issued · server {srv.group(1) if srv else '-'} · auth {res.body.get('auth')}")


SCENARIO = {
    "id": "g-nvke-clique",
    "order": 7,
    "feature": "F11",
    "story": "US-04",
    "title_ko": "(g) NVKE 클러스터: 클리크 인식 노드풀 + kubeconfig",
    "title_en": "(g) NVKE cluster: clique-aware nodepool + kubeconfig",
    "summary_ko": "2랙 노드풀을 만들면 랙 단위 NVLink 클리크·IMEX 도메인이 생기고 kubeconfig가 나온다 (US-04).",
    "summary_en": "A 2-rack nodepool yields per-rack NVLink cliques, IMEX domains and a kubeconfig (US-04).",
    "steps": [
        tenant_step("01-tenant", "demo-g-nvke", tier="k8s_namespace"),
        {"id": "02-create", "title_ko": "2랙 노드풀 클러스터 생성", "title_en": "Create a 2-rack nodepool cluster",
         "narration_ko": "NVL72 2랙 = 36노드 · 144 GPU. 노드는 NICo 인스턴스(NicoMachine)로 붙는다.",
         "narration_en": "Two NVL72 racks = 36 nodes, 144 GPUs, attached as NICo instances (NicoMachine).",
         "call": http("POST", "/api/v1/nvke/clusters",
                      lambda c: {"name": "demo-clique", "tenant_id": c["tenant_id"],
                                 "k8s_version": "1.34",
                                 "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 36}]}),
         "expect": _created,
         "links": [link("customer", "nvke", "고객 · NVKE", "Customer · NVKE")]},
        {"id": "03-capi", "feature": "F11", "requires": ["GET /api/v1/nvke/clusters/{id}/capi"],
         "title_ko": "CAPI 객체", "title_en": "CAPI objects",
         "call": http("GET", lambda c: f"/api/v1/nvke/clusters/{c['cluster_id']}/capi"),
         "expect": _capi_ok,
         "links": [link("ops", "orchestration", "운영 · Managed K8s", "Ops · Managed K8s")]},
        {"id": "04-cliques", "feature": "F11", "requires": ["GET /api/v1/nvke/clusters/{id}/nodes"],
         "title_ko": "노드 · 클리크 라벨", "title_en": "Nodes · clique labels",
         "expect_ko": "nvidia.com/gpu.clique 2종 (<domainId>.<cliqueId>)",
         "expect_en": "2 nvidia.com/gpu.clique values (<domainId>.<cliqueId>)",
         "call": http("GET", lambda c: f"/api/v1/nvke/clusters/{c['cluster_id']}/nodes"),
         "expect": _cliques_ok},
        {"id": "05-compute-domains", "feature": "F11",
         "requires": ["GET /api/v1/nvke/clusters/{id}/compute-domains"],
         "title_ko": "IMEX ComputeDomain", "title_en": "IMEX ComputeDomains",
         "call": http("GET", lambda c: f"/api/v1/nvke/clusters/{c['cluster_id']}/compute-domains"),
         "expect": _domains_ok},
        {"id": "06-kubeconfig", "title_ko": "kubeconfig 발급", "title_en": "Issue kubeconfig",
         "call": http("GET", lambda c: f"/api/v1/nvke/clusters/{c['cluster_id']}/kubeconfig",
                      as_=lambda c: f"tenant:{c['tenant_id']}"),
         "expect": _kubeconfig_ok},
    ],
}
