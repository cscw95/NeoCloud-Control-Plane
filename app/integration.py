"""Integration status + system connectivity topology.

Reports how NeoCloud OS (NOCP) is wired to the standalone NICo Emulator and
draws the live connection graph for the verification console:
    VR NVL72 Digital Twin ↔ NICo Emulator ↔ Control-Plane ↔ Customer/Ops/Biz consoles
"""
import os
import time

import httpx
from fastapi import APIRouter

from . import __version__

router = APIRouter(prefix="/api/v1/integration", tags=["integration"])

# NICo emulator base (root). NOCP_NICO_URL points at the /nico-bridge base when
# the HTTP adapter is active; strip it to reach the emulator root for probing.
_env = os.environ.get("NOCP_NICO_URL", "")
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


def _component_nodes(up: bool):
    """트윈 하부 에뮬레이터 플레인(UFM·NetQ·DLC·VAST·Converged) 프로브."""
    comps = []

    def add(cid, label, probe_path, describe, edge_label, twin_label):
        st, detail = "down", "미기동"
        if up:
            p = _probe(f"{NICO_BASE}{probe_path}", timeout=0.9)
            if p["reachable"]:
                try:
                    st, detail = describe(p.get("body"))
                except Exception:
                    st, detail = "unknown", "응답 파싱 실패"
            else:
                st, detail = "down", "미배포"
        comps.append({"node": {"id": cid, "label": label, "kind": "plane",
                               "status": st, "detail": detail},
                      "edges": [
                          {"from": "nico", "to": cid, "label": edge_label,
                           "status": st if st != "unknown" else "up"},
                          {"from": cid, "to": "twin", "label": twin_label,
                           "status": st if st != "unknown" else "up"}]})

    add("ufm", "UFM Enterprise (IB)", "/ufm/v1/fabric/health",
        lambda b: (("unknown" if (b.get("links_degraded") or
                                  b.get("links_down")) else "up"),
                   f"sw {b['switches']['total']} · link "
                   f"{b['links_active']}/{b['links_total']}"
                   + (f" · deg {b['links_degraded']}"
                      if b.get("links_degraded") else "")),
        "UFM REST", "Quantum-X800 IB")
    add("netq", "NetQ (Ethernet)", "/netq/v1/validation",
        lambda b: (("unknown" if (b["summary"].get("fail") or
                                  b["summary"].get("warn")) else "up"),
                   f"validation {b['summary'].get('pass', 0)} pass"
                   + (f" · {b['summary'].get('fail', 0)} fail"
                      if b["summary"].get("fail") else "")),
        "NetQ REST", "Spectrum-X Eth")
    add("dlc", "SMCI DLC / CDU", "/emulator/v1/obs/dlc/cdus",
        lambda b: (("unknown" if any((c.get("alarms") or []) for c in b)
                    else "up"),
                   f"CDU {len(b)} · alarms "
                   f"{sum(len(c.get('alarms') or []) for c in b)}"),
        "Redfish/Modbus", "액랭 DLC-2")
    add("vast", "VAST Data (AI Storage)", "/vast/v1/clusters",
        lambda b: ("up", f"cluster {len(b.get('clusters', b) or [])}식"),
        "VMS REST", "NFS/S3 views")
    add("converged", "Converged Network", "/converged/v1/overview",
        lambda b: ("up", "storage/mgmt rail"),
        "Spectrum-X", "CX-9/BF-4 rail")
    return comps


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
        {"id": "cp", "label": "NeoCloud OS Control-Plane (NOCP)",
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
        {"from": "cp", "to": "nico",
         "label": "NicoHttpAdapter" if nico["adapter_active"] else "FakeNico (in-process)",
         "status": ("up" if up else "down") if nico["adapter_active"] else "local"},
        {"from": "customer", "to": "cp", "label": "REST /api/v1", "status": "up"},
        {"from": "ops", "to": "cp", "label": "REST /api/v1", "status": "up"},
        {"from": "biz", "to": "cp", "label": "REST /api/v1", "status": "up"},
    ]
    for c in _component_nodes(up):
        nodes.append(c["node"])
        edges.extend(c["edges"])
    return {"nodes": nodes, "edges": edges, "nico": nico,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
