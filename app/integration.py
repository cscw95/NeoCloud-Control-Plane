"""Integration status + system connectivity topology.

Reports how NeoCloud OS (VRCM) is wired to the standalone NICo Emulator and
draws the live connection graph for the verification console:
    VR NVL72 Digital Twin ↔ NICo Emulator ↔ Control-Plane ↔ Customer/Ops/Biz consoles
"""
import os
import time

import httpx
from fastapi import APIRouter

from . import __version__

router = APIRouter(prefix="/api/v1/integration", tags=["integration"])

# NICo emulator base (root). VRCM_NICO_URL points at the /nico-bridge base when
# the HTTP adapter is active; strip it to reach the emulator root for probing.
_env = os.environ.get("VRCM_NICO_URL", "")
NICO_BASE = (_env.rsplit("/nico-bridge", 1)[0] if _env else
             os.environ.get("NICO_EMULATOR_URL", "http://127.0.0.1:9000"))
CONSOLE_BASE = os.environ.get("NC_CONSOLE_URL", "http://127.0.0.1:8090")


def _adapter_mode() -> str:
    return "http" if _env else "local"


def _probe(url: str, timeout: float = 1.2):
    t0 = time.monotonic()
    try:
        r = httpx.get(url, timeout=timeout)
        return {"reachable": r.status_code < 400, "status": r.status_code,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "body": r.json() if "json" in r.headers.get("content-type", "")
                else None}
    except Exception as e:
        return {"reachable": False, "status": None,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "error": type(e).__name__}


@router.get("/nico")
def nico_status():
    """Live NICo Emulator integration status (server-side probe, no CORS)."""
    health = _probe(f"{NICO_BASE}/healthz")
    twin = _probe(f"{NICO_BASE}/emulator/v1/twin") if health["reachable"] else None
    tb = (twin or {}).get("body") or {}
    hb = health.get("body") or {}
    return {
        "adapter_mode": _adapter_mode(),          # http | local
        "adapter_active": _adapter_mode() == "http",
        "nico_url": NICO_BASE,
        "bridge_url": _env or f"{NICO_BASE}/nico-bridge",
        "reachable": health["reachable"],
        "latency_ms": health["latency_ms"],
        "version": hb.get("version"),
        "rack": hb.get("rack") or tb.get("rack_id"),
        "model": tb.get("model"),
        "compute_trays": hb.get("compute_trays") or tb.get("compute_trays"),
        "dpus": hb.get("dpus") or tb.get("dpus"),
        "gpus": tb.get("gpus"),
        "tenants": tb.get("tenants", []),
        "attachments": tb.get("attachments"),
    }


@router.get("/topology")
def topology():
    """System connectivity graph for the verification console diagram."""
    nico = nico_status()
    up = nico["reachable"]
    nodes = [
        {"id": "twin", "label": "VR NVL72 Digital Twin",
         "kind": "infra", "status": "up" if up else "unknown",
         "detail": (f"{nico['compute_trays']} trays · {nico['gpus']} GPU · "
                    f"{nico['dpus']} DPU") if up else "emulator offline"},
        {"id": "nico", "label": "NICo Emulator",
         "kind": "emulator", "status": "up" if up else "down",
         "detail": (f"v{nico['version']} · {nico['latency_ms']}ms · "
                    f"{len(nico['tenants'])} tenant(s)") if up
                   else f"unreachable @ {nico['nico_url']}"},
        {"id": "cp", "label": "NeoCloud OS Control-Plane (VRCM)",
         "kind": "control", "status": "up",
         "detail": f"v{__version__} · adapter: {nico['adapter_mode']}"},
        {"id": "customer", "label": "Customer Console",
         "kind": "console", "status": "up", "detail": "tenant self-service"},
        {"id": "ops", "label": "Operations Console",
         "kind": "console", "status": "up", "detail": "SRE / NOC"},
        {"id": "biz", "label": "Business Console",
         "kind": "console", "status": "up", "detail": "sales / exec"},
    ]
    edges = [
        {"from": "nico", "to": "twin", "label": "drives (Redfish/DPU/PXE)",
         "status": "up" if up else "down"},
        {"from": "cp", "to": "nico",
         "label": "NicoHttpAdapter" if nico["adapter_active"] else "FakeNico (in-process)",
         "status": ("up" if up else "down") if nico["adapter_active"] else "local"},
        {"from": "customer", "to": "cp", "label": "REST /api/v1", "status": "up"},
        {"from": "ops", "to": "cp", "label": "REST /api/v1", "status": "up"},
        {"from": "biz", "to": "cp", "label": "REST /api/v1", "status": "up"},
    ]
    return {"nodes": nodes, "edges": edges, "nico": nico,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
