"""DSX OS baseline — shared upstream access, version matrix, product catalog.

NOCP sits on top of two emulators:
  - NICo Emulator     (:9000)  — site controller, real NICo REST paths
  - AI-Infra Emulator (:9100)  — Redfish / DPS (MaxLPS) / DSX Exchange twin

The DSX routers (nvke, power, oob, ncp) read upstream state through the helpers
here. Every helper is best-effort: an unreachable upstream returns None and the
router answers from NOCP's own store, tagging the payload with
`"source": "local-fallback"` instead of `"ai-infra"` / `"nico"`.

`NOCP_DSX_OFFLINE=1` skips upstream calls entirely (deterministic tests).
Tests swap the HTTP transport through `set_transport()`.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter

# Baseline version matrix — identical across the four repos (PLAN.md).
VERSIONS = {
    "dsx_os": "2026.05",
    "nico": "2.3.0",
    "nvke": "0.23.1",
    "dps": "0.10",
    "mission_control": "2.5",
    "doca": "3.x",
    "gpu_operator": "AICR recipe",
    "k8s": ["1.33", "1.34"],
    "platform": "Vera Rubin NVL72",
}

# DSX OS 4-layer mapping of the NOCP modules (README / console version screen).
LAYERS = {
    "provisioning": ["NICo REST adapter", "BMaaS order pipeline", "firmware baseline/drift"],
    "runtime": ["NVKE managed K8s", "AICR recipe", "GPU/Network Operator"],
    "workload": ["KAI/Run:ai (tenant-installed)", "metering"],
    "fleet_health": ["OOB monitoring", "RMA workflow", "NVSentinel → node replace"],
}

_transport: Optional[httpx.BaseTransport] = None


def set_transport(transport: Optional[httpx.BaseTransport]) -> None:
    """Test hook — route every upstream call through e.g. httpx.MockTransport."""
    global _transport
    _transport = transport


def ai_infra_url() -> str:
    return os.environ.get("AI_INFRA_URL", "http://127.0.0.1:9100").rstrip("/")


def nico_url() -> str:
    env = os.environ.get("NOCP_NICO_URL", "")
    if env:
        return env.rstrip("/").rsplit("/nico-bridge", 1)[0]
    return os.environ.get("NICO_EMULATOR_URL", "http://127.0.0.1:9000").rstrip("/")


def offline() -> bool:
    return os.environ.get("NOCP_DSX_OFFLINE", "0") == "1" and _transport is None


def call(method: str, url: str, *, json: Any = None, params: Optional[dict] = None,
         timeout: float = 3.0) -> tuple[Optional[int], Any]:
    """One upstream call → (status, body). (None, None) when unreachable/offline."""
    if offline():
        return None, None
    try:
        with httpx.Client(transport=_transport, timeout=timeout) as c:
            r = c.request(method, url, json=json, params=params)
    except Exception:                              # noqa: BLE001 — best effort
        return None, None
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = r.text
    return r.status_code, body


def ai_get(path: str, params: Optional[dict] = None, timeout: float = 3.0):
    """GET on AI-Infra; body on 2xx, else None."""
    st, body = call("GET", ai_infra_url() + path, params=params, timeout=timeout)
    return body if st is not None and st < 400 else None


def ai_send(method: str, path: str, json: Any = None, timeout: float = 5.0):
    return call(method, ai_infra_url() + path, json=json, timeout=timeout)


def nico_get(path: str, params: Optional[dict] = None, timeout: float = 3.0):
    st, body = call("GET", nico_url() + path, params=params, timeout=timeout)
    return body if st is not None and st < 400 else None


def nico_send(method: str, path: str, json: Any = None, timeout: float = 5.0):
    return call(method, nico_url() + path, json=json, timeout=timeout)


def nico_rest_path(key: str, **kw) -> str:
    from .adapters import NICO_REST_PATHS
    return NICO_REST_PATHS[key].format(
        org=os.environ.get("NOCP_NICO_ORG", "neocloud"), **kw)


# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/v1", tags=["dsx-platform"])


@router.get("/platform/versions")
def platform_versions() -> dict:
    """DSX OS baseline version matrix + live versions reported by the emulators."""
    from .adapters import nico_api_mode
    live = {
        "nico_emulator": nico_get("/versions", timeout=1.0),
        "ai_infra": ai_get("/versions", timeout=1.0),
    }
    return {**VERSIONS, "layers": LAYERS,
            "nico_api": nico_api_mode() if os.environ.get("NOCP_NICO_URL") else "local",
            "live": live}


CATALOG = [
    {
        "id": "bmaas",
        "name": "Bare-Metal as a Service",
        "layer": "provisioning",
        "description": "Dedicated NVL72 racks/trays provisioned by NICo "
                       "(DPU-isolated VPC, IB P_Key, NVLink partition).",
        "order_api": "POST /api/v1/orders",
        "units": ["instance-hour", "gpu-hour"],
        "instance_types": ["vr-nvl72", "gb300-nvl72", "gb200-nvl72", "cpu-epyc"],
    },
    {
        "id": "managed-k8s",
        "engine": "nvke",
        "name": "Managed Kubernetes (NVKE)",
        "layer": "runtime",
        "description": "Per-tenant control plane + etcd, NodePools backed by "
                       "NICo Instances, AICR-validated GPU/Network Operator recipe.",
        "order_api": "POST /api/v1/nvke/clusters",
        "units": ["k8s-node-hour", "gpu-hour"],
        "k8s_versions": VERSIONS["k8s"],
        "nvke_version": VERSIONS["nvke"],
    },
]


@router.get("/catalog")
def catalog() -> dict:
    return {"products": CATALOG, "versions": VERSIONS}
