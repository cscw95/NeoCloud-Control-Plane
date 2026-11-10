"""Integration status + system connectivity topology.

Reports how NeoCloud OS (NOCP) is wired to the standalone NICo Emulator and
draws the live connection graph for the verification console:
    VR NVL72 Digital Twin ↔ NICo Emulator ↔ Control-Plane ↔ Customer/Ops/Biz consoles
"""
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter

from . import __version__, metrics, trace

router = APIRouter(prefix="/api/v1/integration", tags=["integration"])

# NICo emulator base (root). NOCP_NICO_URL points at the /nico-bridge base when
# the HTTP adapter is active; strip it to reach the emulator root for probing.
_env = os.environ.get("NOCP_NICO_URL", "")
NICO_BASE = (_env.rsplit("/nico-bridge", 1)[0] if _env else
             os.environ.get("NICO_EMULATOR_URL", "http://127.0.0.1:9000"))
# AI Infra Emulator (:9100) — owns the physical twin/equipment. NICo drives it.
AI_INFRA_BASE = os.environ.get("AI_INFRA_URL", "http://127.0.0.1:9100")
CONSOLE_BASE = os.environ.get("NC_CONSOLE_URL", "http://127.0.0.1:8090")



def _emu_headers() -> dict:
    from .dsx import emu_headers
    return emu_headers()

def _adapter_mode() -> str:
    return "http" if _env else "local"


def _probe(url: str, timeout: float = 1.2):
    t0 = time.monotonic()
    try:
        r = httpx.get(url, timeout=timeout, headers=_emu_headers())
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
    """Live NICo Emulator integration status (server-side probe, no CORS).

    Since the split, the physical twin (/emulator/v1/twin) is owned by :9100 — the NICo-side
    values are aggregated from healthz (version, AI Infra reachability, scale) and the site
    controller orchestration (/emulator/v1/sites: managed hosts, jobs, tenants_served).
    The diagram boxes / right-hand cards show these values verbatim, so this is the sync source of truth."""
    health = _probe(f"{NICO_BASE}/healthz")
    hb = health.get("body") or {}
    ai = hb.get("ai_infra") or {}
    sites = (_probe(f"{NICO_BASE}/emulator/v1/sites", timeout=1.5)
             if health["reachable"] else None)
    sb = ((sites or {}).get("body") or {}).get("sites") or []
    orch = [(s.get("orchestration") or {}) for s in sb]
    tenants = sorted({t for o in orch for t in (o.get("tenants_served") or [])})
    trays = ai.get("compute_trays")
    return {
        "adapter_mode": _adapter_mode(),          # http | local
        "adapter_active": _adapter_mode() == "http",
        "nico_url": NICO_BASE,
        "bridge_url": _env or f"{NICO_BASE}/nico-bridge",
        "reachable": health["reachable"],
        "latency_ms": health["latency_ms"],
        "version": hb.get("version"),
        "sites": [s.get("site_id") for s in sb],
        "managed_hosts": sum(o.get("managed_hosts") or 0 for o in orch),
        "active_jobs": sum(o.get("active_jobs") or 0 for o in orch),
        "tenants": tenants,
        "rack": ai.get("rack"),
        "model": "VR NVL72" if trays else None,
        "compute_trays": trays,
        "gpus": trays * 4 if trays else None,     # VR: 4 GPU / compute tray
        "dpus": ai.get("dpus"),
        "ai_infra_reachable": bool(ai.get("reachable")),
    }


def _component_nodes(up: bool):
    """Probe the emulator planes beneath the twin (UFM, NetQ, DLC, VAST, Converged)."""
    comps = []

    def add(cid, label, probe_path, describe, edge_label, twin_label):
        # The physical plane lives in the AI Infra Emulator (:9100) — probe it there.
        st, detail = "down", "미기동"
        if up:
            p = _probe(f"{AI_INFRA_BASE}{probe_path}", timeout=0.9)
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
                          {"from": "twin", "to": cid, "label": twin_label,
                           "status": st if st != "unknown" else "up"}]})

    add("ufm", "UFM Enterprise — Quantum-X800 IB", "/ufm/v1/fabric/health",
        lambda b: (("unknown" if (b.get("links_degraded") or
                                  b.get("links_down")) else "up"),
                   f"sw {b['switches']['total']} · link "
                   f"{b['links_active']}/{b['links_total']}"
                   + (f" · deg {b['links_degraded']}"
                      if b.get("links_degraded") else "")),
        "UFM REST", "IB XDR rail (Fabric-A/B)")
    add("netq", "NetQ — Spectrum-X Ethernet", "/netq/v1/validation",
        lambda b: (("unknown" if (b["summary"].get("fail") or
                                  b["summary"].get("warn")) else "up"),
                   f"validation {b['summary'].get('pass', 0)} pass"
                   + (f" · {b['summary'].get('fail', 0)} fail"
                      if b["summary"].get("fail") else "")),
        "NetQ REST", "Dell PowerSwitch SN5610 leaf/spine")
    add("dlc", "Dell PowerCool CDU (DLC-2)", "/emulator/v1/obs/dlc/cdus",
        lambda b: (("unknown" if any((c.get("alarms") or []) for c in b)
                    else "up"),
                   # vendor/model as reported by AI-Infra (Dell PowerCool CDU C7000)
                   f"{(b[0].get('model') or 'CDU') if b else 'CDU'} · "
                   f"CDU {len(b)} · alarms "
                   f"{sum(len(c.get('alarms') or []) for c in b)}"),
        "Redfish/Modbus", "액랭 공급/회수")
    add("vast", "VAST Data (AI Storage)", "/vast/v1/clusters",
        lambda b: ("up", f"cluster {len(b.get('clusters', b) or [])}식"),
        "VMS REST", "NFS/S3")
    add("converged", "Converged Network", "/converged/v1/overview",
        lambda b: ("up", "CX-9/BF-4 storage·mgmt rail"),
        "Spectrum-X", "storage path")
    return comps


# Per-node access screen (opens a new window when a diagram node is clicked)
_AI = AI_INFRA_BASE
NODE_URLS = {
    "customer": "http://127.0.0.1:8090/customer/",
    "ops": "http://127.0.0.1:8090/ops/",
    "biz": "http://127.0.0.1:8090/biz/",
    "cp": "http://127.0.0.1:8000/",
    "nico": "http://127.0.0.1:9000/",
    "twin": f"{_AI}/#sec=control",
    "ufm": f"{_AI}/#sec=fabric",
    "netq": f"{_AI}/#sec=fabric",
    "dlc": "http://127.0.0.1:8090/ops/#/obs-dlc",
    "vast": f"{_AI}/#sec=storage",
    "converged": f"{_AI}/#sec=fabric",
}


@router.get("/topology")
def topology():
    """System connectivity graph for the verification console diagram."""
    nico = nico_status()
    up = nico["reachable"]
    # Reflect twin fault state — surface the obs summary (e.g. all racks Off) in node color/detail
    obs = _probe(f"{AI_INFRA_BASE}/emulator/v1/obs/summary", timeout=0.9) if up else None
    ob = (obs or {}).get("body") or {}
    racks_off = ob.get("racks_off") or 0
    alerts_open = ob.get("alerts_open") or 0
    twin_status = "up" if up else "unknown"
    g = ob.get("gpus") or {}
    twin_detail = (f"{ob.get('racks', '—')}랙 · {g.get('total', '—')} GPU · "
                   f"{ob.get('cooling', {}).get('cdus', '—')} CDU · "
                   f"{ob.get('tenants', '—')} 테넌트") if up else "AI Infra offline"
    if up and (racks_off or alerts_open):
        twin_status = "down" if racks_off >= (ob.get("racks") or 140) else "unknown"
        twin_detail = (f"장애: 알림 {alerts_open}건"
                       + (f" · 랙 Off {racks_off}" if racks_off else "")
                       + (f" · cordon {ob.get('racks_cordoned')}"
                          if ob.get("racks_cordoned") else ""))
    nodes = [
        {"id": "twin", "label": "AI Infra Emulator — VR NVL72 Twin",
         "kind": "infra", "status": twin_status,
         "detail": twin_detail},
        {"id": "nico", "label": "NICo Emulator",
         "kind": "emulator", "status": "up" if up else "down",
         "detail": (f"v{nico['version']} · {nico['latency_ms']}ms · "
                    f"사이트 {len(nico['sites'])} · "
                    f"호스트 {nico['managed_hosts']} · "
                    f"테넌트 {len(nico['tenants'])}") if up
                   else f"unreachable @ {nico['nico_url']}"},
        {"id": "cp", "label": "NeoCloud OS Control-Plane (NOCP)",
         "kind": "control", "status": "up",
         "detail": f"v{__version__} · adapter: {nico['adapter_mode']}"},
    ]
    # The three consoles — no hardcoding: probe the :8090 static server for real to reflect status
    con = _probe(f"{CONSOLE_BASE}/ops/", timeout=0.8)
    con_up = con["reachable"]
    for cid, label, detail in (
            ("customer", "Customer Console", "tenant self-service"),
            ("ops", "Operations Console", "SRE / NOC"),
            ("biz", "Business Console", "sales / exec")):
        nodes.append({"id": cid, "label": label,
                      "kind": "console",
                      "status": "up" if con_up else "down",
                      "detail": detail if con_up else "미기동 (:8090)"})
    edges = [
        {"from": "cp", "to": "nico",
         "label": "NicoHttpAdapter" if nico["adapter_active"] else "FakeNico (in-process)",
         "status": ("up" if up else "down") if nico["adapter_active"] else "local"},
        {"from": "customer", "to": "cp", "label": "REST /api/v1",
         "status": "up" if con_up else "down"},
        {"from": "ops", "to": "cp", "label": "REST /api/v1",
         "status": "up" if con_up else "down"},
        {"from": "biz", "to": "cp", "label": "REST /api/v1",
         "status": "up" if con_up else "down"},
        {"from": "nico", "to": "twin",
         "label": "drive (:9100 REST)", "status": "up" if up else "down"},
    ]
    # Physical-topology consistency edges: fabric→rack, CDU→rack (liquid cooling), rack→converged→storage
    plane_edge = {
        "ufm": ("twin", "IB XDR rail"),
        "netq": ("twin", "Spectrum-X Eth"),
        "dlc": ("twin", "액랭 공급/회수"),
        "converged": ("vast", "NFS/S3 경로"),
    }
    comps = _component_nodes(up)
    comp_status = {c["node"]["id"]: c["node"]["status"] for c in comps}
    for c in comps:
        nodes.append(c["node"])
    for cid, (dst, lbl) in plane_edge.items():
        st = comp_status.get(cid, "down")
        edges.append({"from": cid, "to": dst, "label": lbl,
                      "status": st if st != "unknown" else "up"})
    st = comp_status.get("converged", "down")
    edges.append({"from": "twin", "to": "converged", "label": "CX-9 / BF-4",
                  "status": st if st != "unknown" else "up"})
    for n in nodes:
        n["url"] = NODE_URLS.get(n["id"])
    return {"nodes": nodes, "edges": edges, "nico": nico,
            "region": {"label": "AI Infra Emulator (:9100) — 물리 트윈/장비",
                       "members": ["twin", "ufm", "netq", "dlc",
                                   "vast", "converged"],
                       "url": AI_INFRA_BASE + "/"},
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


@router.get("/consistency")
def consistency():
    """Cross-layer data consistency check — comparing tenant sets.

    Compares NOCP (tenants holding orders/allocations) vs the NICo Emulator bridge (segment
    tenant_ref) vs the AI Infra physical twin (DPU-attachment tenants), and reports orphans
    (orphan — exist only in the physical layer) and missing entries (missing — exist only in NOCP).
    Consumed by the 'data consistency' panel of the /flow verification console."""
    from .store import STORE

    with STORE.lock:
        nocp = sorted({a.tenant_id for a in STORE.allocations.values()}
                      | {c.tenant_id for c in STORE.cpu_nodes.values()
                         if c.tenant_id})
    # Read env at call time so it can be overridden on reset/test
    env = os.environ.get("NOCP_NICO_URL", "")
    nico_base = (env.rsplit("/nico-bridge", 1)[0] if env else
                 os.environ.get("NICO_EMULATOR_URL", "http://127.0.0.1:9000"))
    ai_base = os.environ.get("AI_INFRA_URL", "http://127.0.0.1:9100")
    adapter = "http" if env else "local"

    def _get(url, timeout=2.5):
        try:
            r = httpx.get(url, timeout=timeout, headers=_emu_headers())
            return r.json() if r.status_code < 400 else None
        except Exception:                          # noqa: BLE001
            return None

    from .adapters import nico_api_mode
    if nico_api_mode() == "bridge":
        segs = _get(nico_base + "/nico-bridge/segments")
        nico = (sorted({s.get("tenant_ref") for s in segs if s.get("tenant_ref")})
                if isinstance(segs, list) else None)
    else:                                          # F08: tenant VPCs on NICo REST
        org = os.environ.get("NOCP_NICO_ORG", "neocloud")
        vpcs = _get(f"{nico_base}/v2/org/{org}/nico/vpc?pageSize=100")
        nico = (sorted({(v.get("labels") or {}).get("nocp.tenant") or v.get("tenantId")
                        for v in vpcs if (v.get("labels") or {}).get("nocp.tenant")})
                if isinstance(vpcs, list) else None)
    twin = _get(ai_base + "/emulator/v1/twin")
    ai = sorted(twin.get("tenants") or []) if isinstance(twin, dict) else None

    findings = []
    if adapter == "local":
        findings.append({"severity": "info", "kind": "ADAPTER_LOCAL",
                         "message": "NOCP가 인프로세스(FakeNico) 어댑터로 동작 중 — "
                                    "주문이 물리 계층(:9000/:9100)에 반영되지 않는다. "
                                    "풀체인 정합은 NOCP_NICO_URL로 기동(Mode B)."})
    for name, remote in (("nico_emulator", nico), ("ai_infra", ai)):
        if remote is None:
            findings.append({"severity": "warn", "kind": "UNREACHABLE",
                             "message": f"{name} 미응답 — 비교 불가"})
            continue
        orphan = [t for t in remote if t not in nocp]
        missing = [t for t in nocp if t not in remote]
        if orphan:
            findings.append({"severity": "fail", "kind": "ORPHAN_TENANT",
                             "message": f"{name}에만 존재(NOCP에 없음): {orphan} "
                                        "— 전체 초기화 또는 회수 필요"})
        if missing and adapter == "http":
            findings.append({"severity": "warn", "kind": "MISSING_TENANT",
                             "message": f"NOCP에만 존재({name} 미반영): {missing}"})
    ok = not any(f["severity"] == "fail" for f in findings)
    return {"ok": ok, "adapter": adapter,
            "tenants": {"nocp": nocp, "nico_emulator": nico, "ai_infra": ai},
            "findings": findings,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# Real-time API call counts + key status — consumed by the /flow module-architecture live badge.
_twin_cache: dict = {"at": 0.0, "data": None}


def _twin_summary(now: float):
    """Best-effort probe of the AI Infra (:9100) obs summary — 5-second cache (avoids polling overhead)."""
    if now - _twin_cache["at"] < 5.0:
        return _twin_cache["data"]
    data = None
    p = _probe(f"{AI_INFRA_BASE}/emulator/v1/obs/summary", timeout=0.6)
    b = p.get("body") if p.get("reachable") else None
    if isinstance(b, dict):
        data = {"alerts_open": b.get("alerts_open"),
                "racks_off": b.get("racks_off"),
                "racks": b.get("racks"),
                "tenants": b.get("tenants")}
    _twin_cache.update(at=now, data=data)
    return data


def _trace_supplement(now: float):
    """Supplementary count for purely internal modules (trace-only emitters) — aggregated by src→module mapping."""
    counts: dict = defaultdict(int)
    last: dict = {}
    for ev in trace.TRACER.query(limit=2000):
        mid = metrics.module_for_trace_src(ev.src)
        if not mid:
            continue
        counts[mid] += 1
        try:
            last[mid] = datetime.fromisoformat(ev.at).timestamp()
        except Exception:                          # noqa: BLE001
            pass
    return counts, last


@router.get("/module-stats")
def module_stats():
    """Per-module real-time API call counts, EPS, recent activity + key status totals.

    Derives cumulative calls, calls-per-second (60s sliding), and last activity (seconds) for the
    14 Control-Plane modules (cp-* + d-*) from the middleware counters, and supplements purely
    internal modules with pipeline traces. totals aggregates the number of in-flight orders, jobs,
    tenants, and pipeline events from the lifecycle/tenancy store."""
    from .models import OrderState
    from .store import STORE

    now = time.time()
    mods_snap, grand_total, grand_eps = metrics.METRICS.snapshot(now)
    sup_counts, sup_last = _trace_supplement(now)

    modules = []
    for mid in metrics.MODULES:
        d = mods_snap[mid]
        last_s = d["last_active_s"]
        st = sup_last.get(mid)
        if st is not None:
            sup_s = round(now - st, 1)
            last_s = sup_s if last_s is None else min(last_s, sup_s)
        modules.append({
            "id": mid,
            "calls": d["calls"] + sup_counts.get(mid, 0),
            "eps": d["eps"],
            "last_active_s": last_s,
        })

    terminal = {OrderState.delivered, OrderState.closed,
                OrderState.rejected, OrderState.failed}
    inflight = {OrderState.provisioning, OrderState.isolating,
                OrderState.storage_binding, OrderState.k8s_installing}
    with STORE.lock:
        active_orders = sum(1 for o in STORE.orders.values()
                            if o.state not in terminal)
        active_jobs = sum(1 for o in STORE.orders.values()
                          if o.state in inflight)
        tenants = len({a.tenant_id for a in STORE.allocations.values()}
                      | {c.tenant_id for c in STORE.cpu_nodes.values()
                         if c.tenant_id})

    return {
        "modules": modules,
        "totals": {
            "calls": grand_total,
            "eps": grand_eps,
            "active_orders": active_orders,
            "active_jobs": active_jobs,
            "tenants": tenants,
            "pipeline_events": trace.TRACER.count(),
        },
        "twin": _twin_summary(now),
        "at": datetime.now(timezone.utc).isoformat(),
    }
