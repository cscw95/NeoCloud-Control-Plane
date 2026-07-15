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
# AI Infra Emulator (:9100) — 물리 트윈/장비를 소유. NICo가 이를 드라이브.
AI_INFRA_BASE = os.environ.get("AI_INFRA_URL", "http://127.0.0.1:9100")
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
        # 물리 플레인은 AI Infra Emulator(:9100)에 있다 — 거기서 프로브.
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
        "NetQ REST", "SN5600 leaf/spine")
    add("dlc", "SMCI in-row CDU (DLC-2)", "/emulator/v1/obs/dlc/cdus",
        lambda b: (("unknown" if any((c.get("alarms") or []) for c in b)
                    else "up"),
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


# 노드별 접속 화면 (다이어그램 클릭 시 새 창)
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
    # 트윈 장애 상태 반영 — 전체 랙 Off 등 obs 요약을 노드 색·상세에 표시
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
        {"from": "nico", "to": "twin",
         "label": "drive (:9100 REST)", "status": "up" if up else "down"},
    ]
    # 물리 구성 정합 엣지: 패브릭→랙, CDU→랙(액랭), 랙→converged→스토리지
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
    """계층 간 데이터 정합성 점검 — 테넌트 집합 비교.

    NOCP(주문/할당 보유 테넌트) vs NICo Emulator 브리지(세그먼트 tenant_ref)
    vs AI Infra 물리 트윈(DPU attachment 테넌트)을 비교해 고아(orphan —
    물리에만 존재)·미반영(missing — NOCP에만 존재)을 보고한다.
    /flow 검증 콘솔의 '데이터 정합성' 패널이 소비한다."""
    from .store import STORE

    with STORE.lock:
        nocp = sorted({a.tenant_id for a in STORE.allocations.values()}
                      | {c.tenant_id for c in STORE.cpu_nodes.values()
                         if c.tenant_id})
    # 리셋/테스트 시 오버라이드 가능하도록 호출 시점에 env를 읽는다
    env = os.environ.get("NOCP_NICO_URL", "")
    nico_base = (env.rsplit("/nico-bridge", 1)[0] if env else
                 os.environ.get("NICO_EMULATOR_URL", "http://127.0.0.1:9000"))
    ai_base = os.environ.get("AI_INFRA_URL", "http://127.0.0.1:9100")
    adapter = "http" if env else "local"

    def _get(url, timeout=2.5):
        try:
            r = httpx.get(url, timeout=timeout)
            return r.json() if r.status_code < 400 else None
        except Exception:                          # noqa: BLE001
            return None

    segs = _get(nico_base + "/nico-bridge/segments")
    nico = (sorted({s.get("tenant_ref") for s in segs if s.get("tenant_ref")})
            if isinstance(segs, list) else None)
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
