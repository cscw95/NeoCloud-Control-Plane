"""F28 Health Validation rules → Cluster Readiness gate demo.

NOCP asks NICo for a NodeVerdict per node (health-validation node-verdict.schema.json 0.5,
CEL rules over DCGM signals); an injected XID 79 fails the gate, clearing it passes again.
Self-contained; format follows app/demo/engine.py. Deterministic: fixed rack, fixed node,
step 00 resets rules and injections."""

RACK = "su-2-rack-00"
NODE = "su-2-rack-00-tray-03"


def http(method, path, json=None):
    spec = {"method": method, "path": path, "as": "operator", "service": "nocp"}
    if json is not None:
        spec["json"] = json
    return spec


def _b(res):
    return res.body if isinstance(res.body, dict) else {}


def _ok(res, ctx):
    return (res.status < 400, f"HTTP {res.status}", f"HTTP {res.status}")


def _rules(res, ctx):
    b = _b(res)
    rules = b.get("rules") or []
    names = ", ".join(r["name"] for r in rules[:4])
    return (len(rules) >= 8, f"규칙 {len(rules)}개 ({b.get('source')}): {names} …",
            f"{len(rules)} rules ({b.get('source')}): {names} …")


def _gate(want):
    def expect(res, ctx):
        b = _b(res)
        bad = b.get("nodes_failed") or []
        checks = sorted({c for n in b.get("nodes") or [] for c in n.get("failing_checks") or []})
        ok = b.get("ready") is want and (want or (bad == [NODE] and "gpu-xid-critical" in checks))
        return (ok, f"게이트 {'통과' if b.get('ready') else '실패'} — {b.get('nodes_total', 0) - len(bad)}/"
                    f"{b.get('nodes_total', 0)} 노드, 실패 {bad} {checks}",
                f"gate {'PASS' if b.get('ready') else 'FAIL'} — {b.get('nodes_total', 0) - len(bad)}/"
                f"{b.get('nodes_total', 0)} nodes, failed {bad} {checks}")
    return expect


SCENARIO = {
    "id": "f28-health-validation",
    "order": 28,
    "feature": "F28",
    "title_ko": "F28 Health Validation 규칙(CEL) · Cluster Readiness 게이트",
    "title_en": "F28 Health Validation rules (CEL) · Cluster Readiness gate",
    "summary_ko": "XID 79 주입 → NodeVerdict CRITICAL → 게이트 실패 → 해제 후 통과",
    "summary_en": "Inject XID 79 → NodeVerdict CRITICAL → gate fails → clear → passes",
    "steps": [
        {"id": "00-reset", "title_ko": "규칙·주입 초기화", "title_en": "Reset rules and injections",
         "call": http("POST", "/api/v1/readiness/reset"), "expect": _ok},
        {"id": "01-rules", "title_ko": "적용 규칙 조회", "title_en": "Rules in force",
         "call": http("GET", "/api/v1/readiness/rules"), "expect": _rules,
         "narration_ko": "규칙 = {name, expr(CEL, 참이면 결함), sensors}. NICo가 없으면 같은 엔진이 NOCP에서 돈다.",
         "narration_en": "Rule = {name, expr (CEL, true = fault), sensors}. Without NICo the same engine runs in NOCP."},
        {"id": "02-gate-pass", "title_ko": f"랙 {RACK} 게이트 (정상)", "title_en": f"Gate rack {RACK} (healthy)",
         "call": http("POST", "/api/v1/readiness/evaluate", {"rack": RACK, "who": "demo-f28"}),
         "expect": _gate(True)},
        {"id": "03-inject", "title_ko": f"{NODE} GPU0 XID 79 주입", "title_en": f"Inject XID 79 on {NODE} GPU0",
         "call": http("POST", "/api/v1/readiness/inject",
                      {"node": NODE, "gpu": 0, "signals": {"DCGM_FI_DEV_XID_ERROR": 79}}),
         "expect": _ok},
        {"id": "04-gate-fail", "title_ko": "게이트 재평가 (실패)", "title_en": "Re-evaluate gate (fails)",
         "call": http("POST", "/api/v1/readiness/evaluate", {"rack": RACK, "who": "demo-f28"}),
         "expect": _gate(False)},
        {"id": "05-clear", "title_ko": "주입 해제", "title_en": "Clear injection",
         "call": http("DELETE", f"/api/v1/readiness/inject/{NODE}"), "expect": _ok},
        {"id": "06-gate-pass", "title_ko": "게이트 재평가 (통과)", "title_en": "Re-evaluate gate (passes)",
         "call": http("POST", "/api/v1/readiness/evaluate", {"rack": RACK, "who": "demo-f28"}),
         "expect": _gate(True)},
    ],
}
