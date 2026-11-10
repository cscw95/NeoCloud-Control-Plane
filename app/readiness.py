"""F28 Cluster Readiness gate — Health Validation verdicts decide acceptance.

Flow (dsx-ai-factory/health-validation @ cc26732a510f, NICo spec.yaml @ 9ace631fa361):
  NOCP → NICo ``GET /v2/org/{org}/nico/machine/{id}/x-neocloud/health-verdict`` → NodeVerdict 0.5
  (NICo evaluates its CEL rules over DCGM observations; see NICo app/rest/health_rules.py).
  A node is ready when overall_severity < UNKNOWN; the gate is ready when every node is.
When NICo is unreachable (tests, NOCP_DSX_OFFLINE=1) the same engine runs locally
(app/health_validation.py, a copy of NICo's) with the catalog default rules, so the
verdict shape and decision are identical; ``source`` says which one answered.

Consumers: K8s acceptance stage (app/lifecycle.py ⑥, check ``health-validation-rules``),
Slurm prolog (app/slurm.py), rack bring-up step 12 can call ``gate(node_ids)``.

  GET    /api/v1/readiness/rules                    rules in force (NICo or local catalog)
  POST   /api/v1/readiness/evaluate                 {nodes:[tray ids]} | {rack} | {cluster_id}
  GET    /api/v1/readiness/clusters/{cluster_id}    gate over a K8s cluster's workers
  POST   /api/v1/readiness/inject                   operator: {node, signals, gpu?} fault injection
  DELETE /api/v1/readiness/inject/{node}            operator: clear
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from . import dsx
from . import health_validation as hv
from .store import STORE

router = APIRouter(prefix="/api/v1/readiness", tags=["readiness"])

_LOCAL: dict[str, Any] = {"rules": None, "overrides": {}}


def reset() -> None:
    _LOCAL.update(rules=None, overrides={})


def _org() -> str:
    return os.environ.get("NOCP_NICO_ORG", "neocloud")


def _nico(path: str) -> str:
    return f"/v2/org/{_org()}/nico{path}"


def local_rules() -> dict:
    if _LOCAL["rules"] is None:
        _LOCAL["rules"] = hv.seed_rules()
    return _LOCAL["rules"]


def rules() -> dict:
    body = dsx.nico_get(_nico("/x-neocloud/health-validation/rules"), timeout=2.0)
    if isinstance(body, list):
        return {"source": "nico", "rules": body}
    return {"source": "local", "rules": list(local_rules().values())}


def _local_observations(node: str) -> dict:
    tray = STORE.trays.get(node)
    gpus = len(tray.gpu_ids) if tray else 4
    healthy = not (tray and tray.state.value in ("faulted",))
    base = hv.baseline_observations(f"nh-{node}", gpus, healthy=healthy)
    return hv.apply_overrides(base, _LOCAL["overrides"].get(node) or {})


def verdict(node: str) -> dict:
    """NodeVerdict for one node (tray id) → {node, source, ready, verdict, failing_checks}."""
    body = dsx.nico_get(_nico(f"/machine/{node}/x-neocloud/health-verdict"), timeout=3.0)
    if isinstance(body, dict) and body.get("schema_version"):
        src = "nico"
    else:
        body = hv.node_verdict(f"nh-{node}", list(local_rules().values()), _local_observations(node))
        src = "local"
    return {"node": node, "source": src, "ready": hv.ready(body),
            "overall_severity": body["overall_severity"],
            "failing_checks": hv.failing_checks(body), "verdict": body}


def gate(nodes: list[str], who: str = "readiness") -> dict:
    """Readiness gate over nodes. Emits one trace event; never raises."""
    results = [verdict(n) for n in nodes]
    bad = [r["node"] for r in results if not r["ready"]]
    out = {"ready": not bad and bool(results), "nodes_total": len(results),
           "nodes_failed": bad, "source": sorted({r["source"] for r in results}) or ["local"],
           "schema_version": hv.SCHEMA_VERSION, "nodes": results}
    try:
        from .trace import emit
        emit("NeoCloudOS.Readiness", "NICo(HealthValidation)", "Validation",
             f"readiness gate {'PASS' if out['ready'] else 'FAIL'} — {who}",
             f"{len(results) - len(bad)}/{len(results)} nodes ready"
             + (f"; failed {bad}" if bad else ""),
             payload={"who": who, "ready": out["ready"], "failed": bad})
    except Exception:                               # noqa: BLE001 — trace is best effort
        pass
    return out


def cluster_nodes(cluster_id: str) -> list[str]:
    c = STORE.k8s_clusters.get(cluster_id)
    if c is None:
        raise HTTPException(404, f"cluster {cluster_id} not found")
    out = []
    for nid in c.worker_node_ids:
        ni = STORE.node_instances.get(nid)
        out.append(ni.tray_id if ni else nid.removeprefix("ni-"))
    return out


def inject(node: str, body: dict) -> dict:
    """Fault injection on both sides (NICo hook when reachable, local mirror always)."""
    try:
        _LOCAL["overrides"][node] = hv.merge_override(_LOCAL["overrides"].get(node), body)
    except hv.RuleError as e:
        raise HTTPException(400, str(e)) from None
    st, _ = dsx.nico_send("PUT", f"/emulator/v1/rest/machines/{node}/observations", body)
    return {"node": node, "overrides": _LOCAL["overrides"][node], "nico": st}


def clear(node: str) -> dict:
    _LOCAL["overrides"].pop(node, None)
    st, _ = dsx.nico_send("DELETE", f"/emulator/v1/rest/machines/{node}/observations")
    return {"node": node, "overrides": {}, "nico": st}


def _operator(request: Request) -> None:
    if request.headers.get("x-tenant-id"):
        raise HTTPException(403, "operator only")


# ── routes ────────────────────────────────────────────────────────────
@router.get("/rules")
def get_rules() -> dict:
    return rules()


@router.post("/evaluate")
def evaluate(body: dict = Body(...)) -> dict:
    nodes = body.get("nodes")
    if body.get("cluster_id"):
        nodes = cluster_nodes(body["cluster_id"])
    elif body.get("rack"):
        rack = STORE.racks.get(body["rack"])
        if rack is None:
            raise HTTPException(404, f"rack {body['rack']} not found")
        nodes = list(rack.tray_ids)
    if not isinstance(nodes, list) or not nodes or len(nodes) > 512:
        raise HTTPException(400, "nodes (1..512 tray ids), rack or cluster_id is required")
    return gate([str(n) for n in nodes], who=body.get("who", "api"))


@router.get("/clusters/{cluster_id}")
def cluster_gate(cluster_id: str) -> dict:
    return {"cluster_id": cluster_id, **gate(cluster_nodes(cluster_id), who=cluster_id)}


@router.post("/inject")
def post_inject(request: Request, body: dict = Body(...)) -> dict:
    _operator(request)
    node = body.get("node")
    if not node or node not in STORE.trays:
        raise HTTPException(404, f"node {node} not found")
    return inject(node, body)


@router.delete("/inject/{node}")
def delete_inject(node: str, request: Request) -> dict:
    _operator(request)
    return clear(node)


@router.post("/reset")
def post_reset(request: Request) -> dict:
    _operator(request)
    reset()
    dsx.nico_send("POST", "/emulator/v1/rest/health-validation/reset")
    return {"reset": True}


def acceptance_check(nodes: list[str], cluster_id: str) -> dict:
    """K8s acceptance (lifecycle ⑥) check entry from the gate."""
    g = gate(nodes, who=cluster_id)
    return {"name": "health-validation-rules", "status": "pass" if g["ready"] else "fail",
            "detail": (f"Health Validation {len(nodes) - len(g['nodes_failed'])}/{len(nodes)} "
                       f"노드 통과 (NodeVerdict {hv.SCHEMA_VERSION}, {','.join(g['source'])})"
                       + (f" — 실패 {g['nodes_failed']}" if g["nodes_failed"] else "")),
            "value": f"{len(nodes) - len(g['nodes_failed'])}/{len(nodes)}",
            "failed_nodes": g["nodes_failed"]}
