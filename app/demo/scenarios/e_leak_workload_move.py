"""(e) Phase-2 DoD ② — coolant leak → tray cordon → NVKE workload moved to a
spare node (F22 on top of F10).

The leak arrives as a Redfish LeakDetection event (Chassis/{tray}/ThermalSubsystem/
LeakDetection, the envelope AI-Infra's `POST /emulator/v1/oob/inject` pushes).
Before F22 the pre-v2 manual node replace (POST /api/v1/nvke/clusters/{id}/nodes/
{node}/replace) stands in for the automatic move.
"""

from datetime import datetime, timezone

from ..engine import Result, check, http
from ._common import link, tenant_step


def _cluster_ok(res, ctx):
    b = res.body
    if res.status == 201:
        ctx["cluster_id"] = b["id"]
    pools = b.get("nodepools") or []
    nodes = [n for p in pools for n in (p.get("nodes") or [])]
    if nodes:
        ctx["victim_node"] = nodes[0].get("name") or nodes[0].get("nico_host_id")
        ctx["victim_host"] = nodes[0].get("nico_host_id")
    return check(res.status == 201 and b.get("state") == "running" and nodes,
                 f"NVKE {b.get('id')} running · 노드 {len(nodes)}대",
                 f"NVKE {b.get('id')} running · {len(nodes)} nodes")


def _inject(ctx):
    from ... import oob
    host = ctx["victim_host"]
    tray = host.removeprefix("nh-")
    ev = {"Events": [{
        "EventType": "Alert", "EventId": f"leak-{ctx['run_id']}",
        "MessageId": "EnvironmentalEvent.1.0.LeakDetected",
        "Message": f"Coolant leak detected on {tray} (Critical)", "Severity": "Critical",
        "EventTimestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "OriginOfCondition": {"@odata.id": f"/redfish/v1/Chassis/{tray}/ThermalSubsystem/LeakDetection"}}]}
    recs = oob.ingest(ev)
    ctx["leak_rack"] = recs[0].get("rack") if recs else None
    return Result(202, {"accepted": len(recs), "topic": recs[0]["topic"] if recs else None,
                        "rack": ctx["leak_rack"], "event": ev})


def _injected(res, ctx):
    return check(res.body.get("topic") == "leak" and res.body.get("rack"),
                 f"누수 Critical 수신 · 토픽 leak · 랙 {res.body.get('rack')}",
                 f"leak Critical received · topic leak · rack {res.body.get('rack')}")


def _rack_leak(res, ctx):
    leak = res.body.get("leak") if isinstance(res.body, dict) else None
    return check(str(leak) == "Critical", f"랙 {ctx['leak_rack']} 누수 상태 {leak}",
                 f"rack {ctx['leak_rack']} leak state {leak}")


def _moved(res, ctx):
    runs = res.body if isinstance(res.body, list) else res.body.get("runs", [])
    leak_runs = [r for r in runs if "leak" in str((r.get("trigger") or {}).get("kind", "")).lower()]
    r = leak_runs[0] if leak_runs else None
    return check(r and r.get("outcome") == "replaced",
                 f"누수 트리거 복구 run {r.get('id') if r else '-'} · {r.get('outcome') if r else '-'} (≤5분 모사)",
                 f"leak-triggered run {r.get('id') if r else '-'} · {r.get('outcome') if r else '-'} (≤5 min emulated)")


def _replaced_view(res, ctx):
    pools = res.body.get("nodepools") or []
    hosts = [n.get("nico_host_id") for p in pools for n in (p.get("nodes") or [])]
    gone = ctx["victim_host"] not in hosts
    return check(gone, f"고장 트레이 {ctx['victim_host']} 제외 · 노드 {len(hosts)}대 유지 (워크로드 이전)",
                 f"leaking tray {ctx['victim_host']} removed · {len(hosts)} nodes kept (workload moved)")


SCENARIO = {
    "id": "e-leak-workload-move",
    "order": 5,
    "feature": "F22/F10",
    "title_ko": "누수 → cordon → 워크로드 이전",
    "title_en": "Leak → cordon → workload move",
    "summary_ko": "트레이 냉각수 누수가 감지되면 해당 노드를 격리하고 NVKE 워크로드를 예비 노드로 옮긴다 (Phase 2 DoD ②).",
    "summary_en": "A coolant leak cordons the tray and moves the NVKE workload to a spare node (Phase 2 DoD ②).",
    "steps": [
        tenant_step("01-tenant", "demo-e-leak", tier="k8s_namespace"),
        {"id": "02-nvke", "title_ko": "NVKE 클러스터 (노드 4대)", "title_en": "NVKE cluster (4 nodes)",
         "call": http("POST", "/api/v1/nvke/clusters",
                      lambda c: {"name": "demo-leak", "tenant_id": c["tenant_id"],
                                 "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 4}]}),
         "expect": _cluster_ok,
         "links": [link("customer", "nvke", "고객 · NVKE", "Customer · NVKE")]},
        {"id": "03-inject-leak", "title_ko": "누수 Critical 주입", "title_en": "Inject a Critical leak",
         "narration_ko": "Redfish LeakDetection 이벤트로 트레이 누수를 알린다(운영자 주입).",
         "narration_en": "A Redfish LeakDetection event reports the tray leak (operator injection).",
         "call": _inject, "expect": _injected,
         "links": [link("ops", "dsx-oob", "운영 · OOB (Leak 히트맵)", "Ops · OOB (leak heatmap)")]},
        {"id": "04-rack-state", "title_ko": "랙 누수 상태", "title_en": "Rack leak state",
         "call": http("GET", lambda c: f"/api/v1/oob/racks/{c['leak_rack']}"), "expect": _rack_leak},
        {"id": "05-auto-move", "feature": "F22", "requires": ["GET /api/v1/remediation/runs"],
         "title_ko": "자동 cordon · 교체 run", "title_en": "Automatic cordon · replace run",
         "narration_ko": "Critical 누수는 F10 교체 run을 만든다. Warning이면 공지만 한다.",
         "narration_en": "A Critical leak opens an F10 replace run; a Warning only posts a notice.",
         "call": http("GET", "/api/v1/remediation/runs", params=lambda c: {"tenant_id": c["tenant_id"]}),
         "fallback": http("POST", lambda c: f"/api/v1/nvke/clusters/{c['cluster_id']}/nodes/{c['victim_node']}/replace",
                          {"reason": "leak Critical (manual, pre-F22)", "source": "oob-event"}),
         "expect": _moved,
         "links": [link("ops", "dsx-rma", "운영 · RMA·복구", "Ops · RMA & remediation")]},
        {"id": "06-nodes", "title_ko": "노드 목록: 이전 확인", "title_en": "Node list: move confirmed",
         "call": http("GET", lambda c: f"/api/v1/nvke/clusters/{c['cluster_id']}"),
         "expect": _replaced_view},
    ],
}
