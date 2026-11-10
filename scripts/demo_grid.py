#!/usr/bin/env python3
"""Terminal demo for F20 (DSX Flex messages), F21 (curtailment policy) and F22
(leak → cordon → workload move) against a live stack.

  python scripts/demo_grid.py f20|f21|f22|all
  env: NOCP_URL (default http://127.0.0.1:8000), AI_INFRA_URL (http://127.0.0.1:9100),
       NOCP_TOKEN (operator bearer, optional), EMU_API_KEY (AI-Infra X-API-Key, optional)

Every step prints the curl command and a one-line summary of the response; F20
also prints the raw CloudEvents (specversion 1.0.2) the Flex Agent emitted."""
import json
import os
import sys
import time
import urllib.error
import urllib.request

NOCP = os.environ.get("NOCP_URL", "http://127.0.0.1:8000").rstrip("/")
AI = os.environ.get("AI_INFRA_URL", "http://127.0.0.1:9100").rstrip("/")
FEED = os.environ.get("DEMO_FEED", "pd-aif-stt-gasan-f1")


def call(method, url, body=None, quiet=False):
    hdr = {"Content-Type": "application/json"}
    if url.startswith(NOCP) and os.environ.get("NOCP_TOKEN"):
        hdr["Authorization"] = "Bearer " + os.environ["NOCP_TOKEN"]
    if url.startswith(AI) and os.environ.get("EMU_API_KEY"):
        hdr["X-API-Key"] = os.environ["EMU_API_KEY"]
    data = json.dumps(body).encode() if body is not None else None
    if not quiet:
        print(f"\n$ curl -X {method} {url}" + (f" -d '{json.dumps(body)}'" if body else ""))
    req = urllib.request.Request(url, data=data, method=method, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            st, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        st, raw = e.code, e.read()
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        out = raw.decode(errors="replace")
    return st, out


def say(st, ko, en):
    print(f"  → HTTP {st} · {ko}\n               {en}")


def f20():
    print("\n=== F20 DSX Flex 1.7.0: Load Target → Status → Breach → Enforcement ===")
    for lt in call("GET", f"{AI}/flex/v1/load-target-sets?site={FEED}", quiet=True)[1].get("items", []):
        if lt["state"] in ("scheduled", "active"):
            call("DELETE", f"{AI}/flex/v1/load-target-sets/{lt['id']}", quiet=True)
    msgs = call("GET", f"{AI}/flex/v1/messages?limit=1", quiet=True)[1].get("items", [])
    since = msgs[-1]["seq"] if msgs else 0
    st, b = call("GET", f"{AI}/flex/v1/power-state-status?site={FEED}")
    say(st, f"측정 {b['measured_kw']} kW · {b['compliance']}", f"measured {b['measured_kw']} kW · {b['compliance']}")
    st, lt = call("POST", f"{AI}/flex/v1/load-target-sets",
                  {"site": FEED, "reduce_kw": 400, "duration_s": 900})
    say(st, f"Load Target Set {lt['id']} 목표 {lt['target_kw']} kW", f"Load Target Set {lt['id']} target {lt['target_kw']} kW")
    st, b = call("GET", f"{AI}/flex/v1/power-state-status?site={FEED}")
    say(st, f"감축 전 {b['measured_kw']} > {b['target_kw']} kW · {b['compliance']}",
        f"before shedding {b['measured_kw']} > {b['target_kw']} kW · {b['compliance']}")
    st, b = call("POST", f"{AI}/flex/v1/clock", {"advance_s": 35})
    say(st, "모사 시계 +35초 (30초 지속 기준)", "emulated clock +35 s (30 s sustain window)")
    st, b = call("GET", f"{AI}/flex/v1/messages?since={since}")
    for m in b.get("items", []):
        ce = m["cloudevent"]
        d = ce["data"]
        extra = d.get("status") or d.get("action") or ""
        print(f"    [{m['mqtt_topic']}] {ce['type']} specversion={ce['specversion']} {extra}")
    alert = next((m["cloudevent"] for m in b.get("items", [])
                  if m["cloudevent"]["type"] == "grid.powerbreach.alert.v1"), None)
    if alert:
        print("    raw PowerBreachAlert:\n" + json.dumps(alert, ensure_ascii=False, indent=2)[:1500])
    st, b = call("GET", f"{AI}/flex/v1/power-state-status?site={FEED}")
    say(st, f"강제 조치 후 {b['measured_kw']} ≤ {b['target_kw']} kW · {b['compliance']}",
        f"after enforcement {b['measured_kw']} ≤ {b['target_kw']} kW · {b['compliance']}")
    st, b = call("DELETE", f"{AI}/flex/v1/load-target-sets/{lt['id']}")
    say(st, f"목표 해제 → {b['state']} (enforcement reverted)", f"target cleared → {b['state']} (enforcement reverted)")


def _tenant(name, tier="vm_multitenant"):
    st, b = call("POST", f"{NOCP}/api/v1/tenants", {"name": name, "isolation_tier": tier}, quiet=True)
    if st == 201:
        return b["id"]
    return next(t["id"] for t in call("GET", f"{NOCP}/api/v1/tenants", quiet=True)[1]
                if t["name"] == name)


def f21():
    print("\n=== F21 curtailment: reserved protected, batch/spot first, restore, settle ===")
    tids = {k: _tenant(f"demo-f21-{k}") for k in ("reserved", "on_demand", "spot")}
    doms = call("GET", f"{NOCP}/api/v1/power/domains", quiet=True)[1]["domains"]
    dom = doms[0]
    racks = dom.get("racks") or []
    racks = racks if isinstance(racks, list) else []
    if not racks:
        tele = call("GET", f"{NOCP}/api/v1/power/telemetry?domain={dom['id']}", quiet=True)[1]
        racks = [r["rack_id"] for d in tele.get("domains", []) for g in d.get("groups", [])
                 for r in g.get("racks", [])]
    reserved = []
    for kind, tid in tids.items():
        for rid in racks:
            r = call("GET", f"{NOCP}/api/v1/racks/{rid}", quiet=True)[1]
            if r.get("tenant_id") == tid:
                break
            if r.get("tenant_id") is None:
                call("POST", f"{NOCP}/api/v1/allocations", {"tenant_id": tid, "su_id": r["su_id"],
                                                            "scope": "rack_set", "rack_ids": [rid]}, quiet=True)
                break
        if kind == "reserved":
            reserved = [x for x in racks[:40]
                        if call("GET", f"{NOCP}/api/v1/racks/{x}", quiet=True)[1].get("tenant_id") == tid]
    pol = {"tiers": [{"name": "token-batch", "match": {"product": "token-api", "tier": "batch"}, "order": 1, "max_reduction_pct": 70},
                     {"name": "spot", "match": {"commitment": "spot"}, "order": 2, "max_reduction_pct": 50},
                     {"name": "on-demand", "match": {"commitment": "on_demand"}, "order": 3, "max_reduction_pct": 20}],
           "protect": ["reserved"], "commitments": {v: k for k, v in tids.items()}}
    st, b = call("PUT", f"{NOCP}/api/v1/power/curtailment-policy", pol)
    say(st, "정책: 예약 보호, Batch 70% → 스팟 50% → 온디맨드 20%", "policy: reserved protected; batch 70% → spot 50% → on-demand 20%")
    st, run = call("POST", f"{NOCP}/api/v1/power/curtailments",
                   {"requested_kw": 600, "duration_s": 900, "domain": dom["id"]})
    kinds = sorted({a["kind"] for a in run.get("actions", [])})
    hit = [a for a in run.get("actions", []) if a.get("target") in reserved or a.get("tenant_id") == tids["reserved"]]
    say(st, f"요청 {run.get('requested_kw')} · 달성 {run.get('achieved_kw')} kW · {kinds} · 예약 조치 {len(hit)}건 · LTS {run.get('load_target_id')}",
        f"requested {run.get('requested_kw')} · achieved {run.get('achieved_kw')} kW · {kinds} · {len(hit)} on reserved")
    for a in run.get("actions", []):
        print(f"    {a['tier_name']:<12} {a['kind']:<15} {a['target']:<28} {a['amount']} {a['unit']}")
    st, b = call("GET", f"{NOCP}/api/v1/power/curtailments/{run['id']}")
    say(st, f"SLA 예외 {b.get('sla_exception_id')} · 공지 {b.get('notice_id')} · 전력상태 {b.get('power_state')}",
        f"SLA exception {b.get('sla_exception_id')} · notice {b.get('notice_id')} · power state {b.get('power_state')}")
    st, b = call("POST", f"{NOCP}/api/v1/power/curtailments/{run['id']}/end")
    s = b.get("settlement") or {}
    say(st, f"종료 → {b.get('state')} · 정산 {s.get('kwh')} kWh ₩{s.get('incentive_krw')}",
        f"ended → {b.get('state')} · settled {s.get('kwh')} kWh KRW {s.get('incentive_krw')}")


def f22():
    print("\n=== F22 leak → cordon → workload move ===")
    tid = _tenant("demo-f22-leak", "bare_metal_dedicated")
    st, c = call("POST", f"{NOCP}/api/v1/nvke/clusters",
                 {"name": f"demo-leak-{int(time.time())}", "tenant_id": tid,
                  "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 2}]})
    node = c["nodepools"][0]["nodes"][0]
    tray = node.get("tray_id") or node["nico_host_id"].removeprefix("nh-")
    say(st, f"NVKE {c['id']} 노드 2대 · 대상 {tray}", f"NVKE {c['id']} 2 nodes · target {tray}")
    st, b = call("POST", f"{AI}/emulator/v1/oob/inject", {"kind": "leak", "target": tray, "severity": "Critical"})
    say(st, "AI-Infra 누수 주입 → EventService 푸시", "AI-Infra leak injected → EventService push")
    resp = None
    for _ in range(10):
        items = call("GET", f"{NOCP}/api/v1/oob/leak-responses", quiet=True)[1].get("items", [])
        resp = next((r for r in items if r["tray_id"] == tray), None)
        if resp:
            break
        time.sleep(0.5)
    if not resp:
        print("  (push not received — posting the Redfish event to NOCP directly)")
        call("POST", f"{NOCP}/api/v1/oob/events", {"Events": [{
            "EventType": "Alert", "EventId": f"leak-{tray}-{int(time.time())}",
            "MessageId": "Environmental.1.5.LeakDetectedCritical", "Severity": "Critical",
            "Message": f"Coolant leak on {tray}",
            "OriginOfCondition": {"@odata.id": f"/redfish/v1/Chassis/{tray}/ThermalSubsystem/LeakDetection"}}]})
        items = call("GET", f"{NOCP}/api/v1/oob/leak-responses", quiet=True)[1].get("items", [])
        resp = next((r for r in items if r["tray_id"] == tray), {})
    st, resp = call("GET", f"{NOCP}/api/v1/oob/leak-responses/{resp.get('id')}")
    say(st, f"{resp.get('id')}: {' → '.join(s['name'] for s in resp.get('steps', []))} · {resp.get('state')} · {resp.get('elapsed_s')}초",
        f"{resp.get('id')}: {' → '.join(s['name'] for s in resp.get('steps', []))} · {resp.get('state')} · {resp.get('elapsed_s')} s")
    print("    HealthEvent: " + json.dumps(resp.get("health_event"), ensure_ascii=False))
    st, c2 = call("GET", f"{NOCP}/api/v1/nvke/clusters/{c['id']}")
    hosts = [n["nico_host_id"] for p in c2["nodepools"] for n in p["nodes"]]
    say(st, f"노드 {hosts} (누수 {node['nico_host_id']} 제외)", f"nodes {hosts} (leaking {node['nico_host_id']} removed)")
    call("POST", f"{AI}/emulator/v1/oob/recover", {"kind": "leak", "target": tray}, quiet=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    for name, fn in (("f20", f20), ("f21", f21), ("f22", f22)):
        if what in (name, "all"):
            fn()
