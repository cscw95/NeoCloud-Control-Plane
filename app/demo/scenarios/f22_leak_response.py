"""F22 leak demo — Redfish LeakDetectedCritical on an NVKE node's tray →
NVSentinel HealthEvent → cordon → drain → replace (workload moved).

Self-contained (no import of app/demo/engine.py); step format follows engine.py.
The leak arrives as the DMTF Redfish Event the AI-Infra EventService pushes
(`Environmental.1.5.LeakDetectedCritical`, origin Chassis/{tray}/ThermalSubsystem/
LeakDetection); live, `POST /emulator/v1/oob/inject {kind: leak}` on AI-Infra
produces the same push.
"""

from datetime import datetime, timezone


def http(method, path, json=None, params=None):
    spec = {"method": method, "path": path, "as": "operator", "service": "nocp"}
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def link(route, ko, en):
    return {"console": "ops", "route": route, "label_ko": ko, "label_en": en}


def _body(res):
    return res.body if isinstance(res.body, dict) else {}


def _setup(ctx):
    from ... import leak_response
    from ...models import IsolationTier, TenantCreate
    from ...nvke import ClusterCreate, create_cluster
    from ...store import STORE
    from ...tenancy import create_tenant
    leak_response.reset()
    t = next((t for t in STORE.tenants.values() if t.name == "demo-f22-leak"), None) or \
        create_tenant(TenantCreate(name="demo-f22-leak",
                                   isolation_tier=IsolationTier.bare_metal_dedicated))
    c = create_cluster(ClusterCreate(name=f"demo-leak-{datetime.now():%H%M%S}", tenant_id=t.id,
                                     nodepools=[{"name": "gpu", "instance_type": "vr-nvl72",
                                                 "count": 2}]))
    c = c if isinstance(c, dict) else c.model_dump()
    node = c["nodepools"][0]["nodes"][0]
    ctx.update(tenant_id=t.id, cluster_id=c["id"], victim_host=node["nico_host_id"],
               tray=node.get("tray_id") or node["nico_host_id"].removeprefix("nh-"))
    return {"cluster": c["id"], "nodes": [n["nico_host_id"] for n in c["nodepools"][0]["nodes"]]}


def _ready(res, ctx):
    return (True, f"NVKE {ctx['cluster_id']} · 노드 2대 · 대상 트레이 {ctx['tray']}",
            f"NVKE {ctx['cluster_id']} · 2 nodes · target tray {ctx['tray']}")


def _event(ctx):
    return {"Events": [{
        "EventType": "Alert", "EventId": f"leak-{ctx['tray']}-{datetime.now():%H%M%S%f}",
        "EventTimestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "MessageId": "Environmental.1.5.LeakDetectedCritical", "Severity": "Critical",
        "Message": f"Coolant leak detected on {ctx['tray']} (Critical)",
        "OriginOfCondition": {"@odata.id":
                              f"/redfish/v1/Chassis/{ctx['tray']}/ThermalSubsystem/LeakDetection"}}]}


def _accepted(res, ctx):
    return (res.status == 202, "Redfish LeakDetectedCritical 수신 (EventService 형식)",
            "Redfish LeakDetectedCritical received (EventService shape)")


def _response(res, ctx):
    items = [r for r in _body(res).get("items") or [] if r.get("tray_id") == ctx["tray"]]
    r = items[-1] if items else {}
    ctx["resp_id"] = r.get("id")
    steps = " → ".join(s["name"] for s in r.get("steps") or [])
    he = r.get("health_event") or {}
    return (r.get("state") == "moved" and r.get("within_kpi"),
            f"{r.get('id')}: {steps} · HealthEvent {he.get('recommendedAction')} · "
            f"{r.get('elapsed_s')}초 (≤300초)",
            f"{r.get('id')}: {steps} · HealthEvent {he.get('recommendedAction')} · "
            f"{r.get('elapsed_s')} s (≤300 s)")


def _moved(res, ctx):
    hosts = [n["nico_host_id"] for p in _body(res).get("nodepools") or [] for n in p["nodes"]]
    return (ctx["victim_host"] not in hosts and len(hosts) == 2,
            f"누수 트레이 {ctx['victim_host']} 제외 · 노드 {len(hosts)}대 유지 (워크로드 이전)",
            f"leaking tray {ctx['victim_host']} removed · {len(hosts)} nodes kept (workload moved)")


SCENARIO = {
    "id": "f22-leak-response",
    "order": 22,
    "feature": "F22",
    "title_ko": "F22 누수 → cordon → 워크로드 이전",
    "title_en": "F22 leak → cordon → workload move",
    "summary_ko": "트레이 냉각수 누수(Critical)를 NVSentinel HealthEvent로 바꿔 격리·이전한다. Warning은 공지만.",
    "summary_en": "A Critical tray leak becomes an NVSentinel HealthEvent: cordon, drain, replace. Warning = notice only.",
    "steps": [
        {"id": "00-setup", "title_ko": "NVKE 클러스터 (노드 2대)", "title_en": "NVKE cluster (2 nodes)",
         "call": _setup, "expect": _ready},
        {"id": "01-inject", "title_ko": "누수 Critical 주입", "title_en": "Inject a Critical leak",
         "call": http("POST", "/api/v1/oob/events", _event), "expect": _accepted,
         "links": [link("dsx-oob", "운영 · OOB", "Ops · OOB")]},
        {"id": "02-response", "title_ko": "자동 대응 기록", "title_en": "Automatic response record",
         "call": http("GET", "/api/v1/oob/leak-responses"), "expect": _response},
        {"id": "03-nodes", "title_ko": "노드 목록: 이전 확인", "title_en": "Node list: move confirmed",
         "call": http("GET", lambda c: f"/api/v1/nvke/clusters/{c['cluster_id']}"), "expect": _moved},
    ],
}
