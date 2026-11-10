"""F32 — Marketplace: model catalog and one-click deploy to the token API.

Deploy = a Dynamo `DynamoGraphDeployment` (nvidia.com/v1beta1) custom resource
created on AI-Infra (the Dynamo operator emulation), after which the model is
served by the OpenAI-compatible token API (F01 gateway `/v1/chat/completions`,
model list from AI-Infra `/dynamo/v1/models`).

Upstream (Apache-2.0): ai-dynamo/dynamo
  CRD   deploy/operator/config/crd/bases/nvidia.com_dynamographdeployments.yaml
        (copy in ref/upstream/dynamo/; AI-Infra app/dynamo.py serves it at
        /apis/nvidia.com/v1beta1/namespaces/{ns}/dynamographdeployments)
  Graph Frontend + VllmWorker (aggregated) | VllmPrefillWorker + VllmDecodeWorker
        (disaggregated), components[].type frontend|worker|prefill|decode
  Grove (ai-dynamo/grove) gang-schedules the DGD pods; for a dedicated deploy
        bound to an NVKE cluster the DGD is also submitted to that cluster's
        KAI queue (F30, priority class `inference`, non-preemptible).

Fallback: AI-Infra unreachable or without the DGD route (pre-F01 build) → the
deployment is recorded with `engine: "stub"` and a locally rendered DGD.
Catalog prices are NeoCloud assumptions (USD per 1M tokens / per GPU-hour).
"""

from __future__ import annotations

import importlib.util
import re
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import dsx, trace
from .store import STORE

router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace"])

DGD_API = "nvidia.com/v1beta1"
DGD_PATH = "/apis/nvidia.com/v1beta1/namespaces/{ns}/dynamographdeployments"
GPU_HOUR_USD = 6.50                  # assumption: dedicated VR NVL72 GPU-hour
MAX_DEPLOYMENTS = 200

# Engine models (same ids as AI-Infra app/dynamo.py MODELS and the F01 gateway).
CATALOG = {
    "llama-3.3-70b-instruct": {
        "name": "Llama 3.3 70B Instruct", "publisher": "Meta", "license": "Llama 3.3 Community",
        "params_b": 70, "context_length": 131072, "tp": 4, "reasoning": False,
        "modalities": ["text"], "category": "chat",
        "description_ko": "범용 대화·요약. 4 GPU 텐서 병렬, 지연이 낮다.",
        "description_en": "General chat and summarisation. 4-GPU tensor parallel, low latency.",
        "price": {"input": 0.60, "cached_input": 0.15, "output": 0.90}},
    "qwen3-235b-a22b": {
        "name": "Qwen3 235B A22B", "publisher": "Alibaba Qwen", "license": "Apache-2.0",
        "params_b": 235, "context_length": 131072, "tp": 8, "reasoning": False,
        "modalities": ["text"], "category": "chat",
        "description_ko": "MoE(활성 22B) 다국어 모델. 8 GPU.",
        "description_en": "MoE (22B active) multilingual model. 8 GPUs.",
        "price": {"input": 0.90, "cached_input": 0.22, "output": 1.80}},
    "deepseek-r1": {
        "name": "DeepSeek R1", "publisher": "DeepSeek", "license": "MIT",
        "params_b": 671, "context_length": 131072, "tp": 8, "reasoning": True,
        "modalities": ["text"], "category": "reasoning",
        "description_ko": "추론(reasoning) 토큰을 내는 671B MoE. usage에 reasoning_tokens가 잡힌다.",
        "description_en": "671B MoE that emits reasoning tokens (usage reasoning_tokens).",
        "price": {"input": 1.50, "cached_input": 0.38, "output": 4.00}},
    "kanana-1.5-8b-instruct": {
        "name": "Kanana 1.5 8B Instruct", "publisher": "Kakao", "license": "Apache-2.0",
        "params_b": 8, "context_length": 32768, "tp": 1, "reasoning": False,
        "modalities": ["text"], "category": "chat",
        "description_ko": "한국어 특화 소형 모델. 1 GPU, 가장 싸다.",
        "description_en": "Small Korean-first model. 1 GPU, cheapest.",
        "price": {"input": 0.10, "cached_input": 0.03, "output": 0.20}},
}

# Tier → DGD graph shape
TIERS = {
    "serverless": {"mode": "agg", "prefill": 0, "decode": 1, "billing": "per_token",
                   "service_tiers": ["interactive", "batch"],
                   "label_ko": "서버리스 (공유, 토큰당 과금)",
                   "label_en": "Serverless (shared, pay per token)"},
    "dedicated": {"mode": "disagg", "prefill": 1, "decode": 2, "billing": "per_gpu_hour",
                  "service_tiers": ["interactive", "batch"],
                  "label_ko": "전용 (분리형 prefill/decode, GPU 시간 과금)",
                  "label_en": "Dedicated (disaggregated prefill/decode, per GPU-hour)"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.deps: dict[str, dict] = {}
        self.seq = 0

    def reset(self) -> None:
        with self.lock:
            self.deps.clear()
            self.seq = 0


MKT = _Store()


def reset() -> None:
    MKT.reset()


def _gateway_present() -> bool:
    return importlib.util.find_spec(f"{__package__}.token_api") is not None


def _gpus(model: str, tier: str) -> int:
    t = TIERS[tier]
    return CATALOG[model]["tp"] * (t["prefill"] + t["decode"])


def _tiers(model: str) -> list[dict]:
    m = CATALOG[model]
    out = []
    for tid, t in TIERS.items():
        gpus = _gpus(model, tid)
        out.append({"id": tid, "label_ko": t["label_ko"], "label_en": t["label_en"],
                    "mode": t["mode"], "gpus": gpus, "billing": t["billing"],
                    "service_tiers": t["service_tiers"],
                    "price": ({"usd_per_1m_tokens": m["price"]} if t["billing"] == "per_token"
                              else {"usd_per_hour": round(gpus * GPU_HOUR_USD, 2),
                                    "usd_per_gpu_hour": GPU_HOUR_USD})})
    return out


def _engine_models() -> tuple[dict, str]:
    body = None if dsx.offline() else dsx.ai_get("/dynamo/v1/models", timeout=2.0)
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return {x.get("id"): x for x in body["data"] if isinstance(x, dict)}, "ai-infra"
    return {}, "stub"


def _who(request: Request):
    return getattr(request.state, "identity", None)


def _scope_tenant(request: Request, tenant_id: Optional[str]) -> Optional[str]:
    who = _who(request)
    if who is not None and who.role == "tenant":
        if tenant_id and tenant_id != who.tenant_id:
            raise HTTPException(403, "tenant scope violation")
        return who.tenant_id
    return tenant_id


def _k8s_name(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")[:63].rstrip("-")


def dgd_manifest(name: str, ns: str, model: str, tier: str, tenant_id: str,
                 racks: Optional[list[str]] = None) -> dict:
    t, m = TIERS[tier], CATALOG[model]
    pod = {"spec": {"containers": [{"name": "main", "resources": {
        "limits": {"nvidia.com/gpu": str(m["tp"])}}}]}}
    comps = [{"name": "Frontend", "type": "frontend", "replicas": 1}]
    if t["mode"] == "agg":
        comps.append({"name": "VllmWorker", "type": "worker", "replicas": t["decode"],
                      "modelRef": {"name": model}, "podTemplate": pod})
    else:
        comps.append({"name": "VllmPrefillWorker", "type": "prefill", "replicas": t["prefill"],
                      "modelRef": {"name": model}, "podTemplate": pod})
        comps.append({"name": "VllmDecodeWorker", "type": "decode", "replicas": t["decode"],
                      "modelRef": {"name": model}, "podTemplate": pod})
    ann = {"neocloud.io/tenant": tenant_id, "neocloud.io/marketplace-tier": tier}
    if racks:
        ann["neocloud.io/racks"] = ",".join(racks)
    return {"apiVersion": DGD_API, "kind": "DynamoGraphDeployment",
            "metadata": {"name": name, "namespace": ns,
                         "labels": {"nvidia.com/dynamo-model": model,
                                    "neocloud.io/marketplace": "true"},
                         "annotations": ann},
            "spec": {"backendFramework": "vllm", "components": comps}}


def _stub_status(manifest: dict) -> dict:
    comps = {}
    for c in manifest["spec"]["components"]:
        gpus = int(((c.get("podTemplate") or {}).get("spec", {}).get("containers") or [{}])[0]
                   .get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 0) or 0)
        comps[c["name"]] = {"componentKind": "Deployment", "replicas": c["replicas"],
                            "readyReplicas": c["replicas"], "availableReplicas": c["replicas"],
                            "gpusPerReplica": gpus}
    return {**manifest, "status": {"state": "successful", "conditions": [{
        "type": "Ready", "status": "True", "reason": "AllComponentsReady",
        "message": "stub: AI-Infra DGD route unavailable", "lastTransitionTime": _now()}],
        "components": comps}}


_STATE = {"successful": "Ready", "pending": "Degraded", "failed": "Failed"}


def _view(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


class DeployBody(BaseModel):
    model: str
    tier: str = "serverless"
    tenant_id: Optional[str] = None
    name: Optional[str] = None
    racks: Optional[list[str]] = None
    cluster_id: Optional[str] = None        # dedicated: also gang-schedule on this NVKE cluster
    queue: Optional[str] = None


@router.get("/models")
def list_models() -> dict:
    live, src = _engine_models()
    with MKT.lock:
        counts: dict[str, int] = {}
        for d in MKT.deps.values():
            counts[d["model"]] = counts.get(d["model"], 0) + 1
    data = []
    for mid, m in CATALOG.items():
        e = live.get(mid)
        data.append({"id": mid, "object": "model", "owned_by": m["publisher"],
                     **{k: v for k, v in m.items() if k != "price"},
                     "price_usd_per_1m_tokens": m["price"], "tiers": _tiers(mid),
                     "engine": "dynamo" if e else "stub",
                     "engine_deployments": len((e or {}).get("deployments") or []),
                     "marketplace_deployments": counts.get(mid, 0)})
    return {"object": "list", "source": src, "gateway": _gateway_present(),
            "tiers": list(TIERS), "data": data}


@router.get("/models/{model_id}")
def get_model(model_id: str) -> dict:
    for m in list_models()["data"]:
        if m["id"] == model_id:
            return m
    raise HTTPException(404, f"model '{model_id}' not in catalog")


@router.post("/deploy", status_code=201)
def deploy(body: DeployBody, request: Request) -> dict:
    if body.model not in CATALOG:
        raise HTTPException(404, f"model '{body.model}' not in catalog; one of {list(CATALOG)}")
    if body.tier not in TIERS:
        raise HTTPException(422, f"tier must be one of {list(TIERS)}")
    tenant = _scope_tenant(request, body.tenant_id)
    if not tenant:
        raise HTTPException(422, "tenant_id is required")
    if tenant not in STORE.tenants:
        raise HTTPException(404, f"tenant '{tenant}' not found")
    if body.cluster_id and body.tier != "dedicated":
        raise HTTPException(422, "cluster_id is only valid for the dedicated tier")
    with MKT.lock:
        if len(MKT.deps) >= MAX_DEPLOYMENTS:
            raise HTTPException(429, "too many marketplace deployments")
        MKT.seq += 1
        seq = MKT.seq
    did = f"mdep-{seq:04d}"
    ns = _k8s_name(tenant)
    name = _k8s_name(body.name or f"mkt-{body.model}-{seq:04d}")
    manifest = dgd_manifest(name, ns, body.model, body.tier, tenant, body.racks)
    path = DGD_PATH.format(ns=ns)
    st, resp = dsx.ai_send("POST", path, manifest)
    if st is not None and st < 300 and isinstance(resp, dict):
        engine, cr = "dynamo", resp
    elif st is None or st == 404 and not (isinstance(resp, dict) and resp.get("kind") == "Status"):
        engine, cr = "stub", _stub_status(manifest)       # AI-Infra off / pre-F01 build
    else:
        msg = resp.get("message") if isinstance(resp, dict) else str(resp)
        raise HTTPException(st if st in (409, 422, 503) else 502,
                            f"Dynamo operator rejected the DynamoGraphDeployment: {msg}")
    state = _STATE.get((cr.get("status") or {}).get("state"), "Pending")
    sched = None
    if body.cluster_id:
        sched = _schedule(body, name, tenant)
    gpus = _gpus(body.model, body.tier)
    d = {"id": did, "name": name, "namespace": ns, "tenant_id": tenant, "model": body.model,
         "tier": body.tier, "state": state, "engine": engine, "gpus": gpus,
         "billing": TIERS[body.tier]["billing"],
         "price": _tiers(body.model)[list(TIERS).index(body.tier)]["price"],
         "endpoint": {"base_url": "/v1", "chat_completions": "/v1/chat/completions",
                      "model": body.model, "service_tiers": TIERS[body.tier]["service_tiers"],
                      "gateway": "token_api" if _gateway_present() else "pending (F01)",
                      "curl": ("curl -s $NEOCLOUD/v1/chat/completions -H \"Authorization: Bearer "
                               "$NEOCLOUD_API_KEY\" -H 'Content-Type: application/json' -d "
                               f"'{{\"model\":\"{body.model}\",\"messages\":[{{\"role\":\"user\","
                               "\"content\":\"hello\"}],\"stream\":true}'")},
         "dgd": cr, "scheduler": sched, "created_at": _now(),
         "upstream": {"request": {"method": "POST", "path": path, "body": manifest},
                      "response": {"status": st, "body": resp if engine == "dynamo" else None}}}
    with MKT.lock:
        MKT.deps[did] = d
    trace.emit("NeoCloudOS.Marketplace", "Dynamo.Operator", "K8s", "DGD create",
               f"{ns}/{name} {body.model} ({body.tier}, {engine})",
               payload={"deployment": did, "tenant": tenant})
    return _view(d)


def _schedule(body: DeployBody, name: str, tenant: str) -> dict:
    from . import kai_scheduler, nvke
    c = nvke._get(body.cluster_id)
    if c.tenant_id != tenant:
        raise HTTPException(403, "tenant scope violation")
    gpus, tp = _gpus(body.model, body.tier), CATALOG[body.model]["tp"]
    w = kai_scheduler.submit(body.cluster_id, kai_scheduler.WorkloadCreate(
        kind="DynamoGraphDeployment", name=name[:52].rstrip("-"), gpus=gpus,
        gpus_per_pod=tp if tp in (1, 2) else 4,           # TP8 spans two trays (MNNVL)
        queue=body.queue or kai_scheduler.DEFAULT_QUEUE, priority_class="inference",
        namespace=_k8s_name(tenant)))
    return {"cluster_id": body.cluster_id, "workload": w["name"], "queue": w["queue"],
            "state": w["state"], "reason": w["reason"], "placement": w["placement"]}


@router.get("/deployments")
def list_deployments(request: Request, tenant_id: Optional[str] = None) -> list:
    tenant = _scope_tenant(request, tenant_id)
    with MKT.lock:
        return [_view(d) for d in MKT.deps.values() if not tenant or d["tenant_id"] == tenant]


def _owned(request: Request, did: str) -> dict:
    d = MKT.deps.get(did)
    if d is None:
        raise HTTPException(404, f"deployment '{did}' not found")
    who = _who(request)
    if who is not None and who.role == "tenant" and who.tenant_id != d["tenant_id"]:
        raise HTTPException(403, "tenant scope violation")
    return d


@router.get("/deployments/{did}")
def get_deployment(did: str, request: Request) -> dict:
    with MKT.lock:
        d = _owned(request, did)
        if d["engine"] == "dynamo":                       # refresh CR status
            st, cr = dsx.ai_send("GET", DGD_PATH.format(ns=d["namespace"]) + "/" + d["name"])
            if st == 200 and isinstance(cr, dict):
                d["dgd"] = cr
                d["state"] = _STATE.get((cr.get("status") or {}).get("state"), d["state"])
        return _view(d)


@router.delete("/deployments/{did}")
def delete_deployment(did: str, request: Request) -> dict:
    with MKT.lock:
        d = _owned(request, did)
        st = None
        if d["engine"] == "dynamo":
            st, _ = dsx.ai_send("DELETE", DGD_PATH.format(ns=d["namespace"]) + "/" + d["name"])
        if d.get("scheduler"):
            from . import kai_scheduler
            try:
                kai_scheduler.delete_workload(d["scheduler"]["cluster_id"], d["scheduler"]["workload"])
            except HTTPException:
                pass
        del MKT.deps[did]
    trace.emit("NeoCloudOS.Marketplace", "Dynamo.Operator", "K8s", "DGD delete",
               f"{d['namespace']}/{d['name']}", payload={"deployment": did})
    return {"deleted": did, "dgd": d["name"], "upstream_status": st}
