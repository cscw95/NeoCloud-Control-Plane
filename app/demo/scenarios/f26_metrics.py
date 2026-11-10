"""F26 Prometheus /metrics demo — NOCP, AI-Infra (DCGM · rack power · CDU) and NICo
scrape targets, each checked with the promtool-style lint in app/prom.py
(Prometheus text 0.0.4). Upstream emulators offline → those steps are skipped."""


def http(method, path, params=None, service="nocp"):
    spec = {"method": method, "path": path, "as": "none", "service": service}
    if params is not None:
        spec["params"] = params
    return spec


def _scrape(must, what_ko, what_en):
    def expect(res, ctx):
        if res.status is None or res.status >= 500 and not res.text:
            return (None, f"{what_ko} 미기동 — 건너뜀", f"{what_en} offline — skipped")
        from app import prom
        probs = prom.lint(res.text or "")
        fams = sum(1 for l in (res.text or "").splitlines() if l.startswith("# TYPE "))
        missing = [m for m in must if m not in (res.text or "")]
        ok = res.status == 200 and not probs and not missing
        ct = (res.headers or {}).get("content-type", "")
        return (ok, f"{what_ko}: 지표 묶음 {fams}개 · 형식 검사 문제 {len(probs)}건 · {ct}"
                    + (f" · 누락 {missing}" if missing else ""),
                f"{what_en}: {fams} families · {len(probs)} lint problems · {ct}"
                + (f" · missing {missing}" if missing else ""))
    return expect


SCENARIO = {
    "id": "f26-metrics",
    "order": 26,
    "feature": "F26",
    "title_ko": "F26 Prometheus 지표 내보내기 (NOCP · AI-Infra · NICo)",
    "title_en": "F26 Prometheus metrics export (NOCP · AI-Infra · NICo)",
    "steps": [
        {"id": "01-nocp", "title_ko": "NOCP /metrics (SLA·쿼터·감축·복구)",
         "title_en": "NOCP /metrics (SLA · quota · curtailment · recovery)",
         "call": http("GET", "/metrics"), "feature": "F26",
         "expect": _scrape(["nocp_sla_availability_ratio", "nocp_curtailments",
                            "nocp_token_quota_overrides_active", "nocp_remediation_runs_total"],
                           "NOCP", "NOCP")},
        {"id": "02-ai", "title_ko": "AI-Infra /metrics (랙 전력·CDU)", "title_en": "AI-Infra /metrics (rack power · CDU)",
         "call": http("GET", "/metrics", service="ai"), "feature": "F26",
         "expect": _scrape(["neocloud_rack_power_watts", "neocloud_cdu_heat_watts"], "AI-Infra", "AI-Infra")},
        {"id": "03-dcgm", "title_ko": "DCGM GPU 지표 (랙 1개)", "title_en": "DCGM GPU series (one rack)",
         "call": http("GET", "/metrics", {"rack": "su-1-rack-00"}, service="ai"), "feature": "F26",
         "expect": _scrape(["DCGM_FI_DEV_BOARD_POWER_WATTS", "DCGM_FI_DEV_XID_ERROR"],
                           "DCGM (su-1-rack-00)", "DCGM (su-1-rack-00)")},
        {"id": "04-nico", "title_ko": "NICo /metrics (machine 상태)", "title_en": "NICo /metrics (machine status)",
         "call": http("GET", "/metrics", service="nico"), "feature": "F26",
         "expect": _scrape(["nico_machines"], "NICo", "NICo")},
        {"id": "05-otlp", "title_ko": "OTLP push 상태 (기본 꺼짐)", "title_en": "OTLP push state (off by default)",
         "call": http("GET", "/api/v1/observability/otlp"), "feature": "F26",
         "expect": lambda res, ctx: (res.status == 200,
                                     f"OTLP endpoint {(res.body or {}).get('endpoint')} · push {(res.body or {}).get('pushes')}회",
                                     f"OTLP endpoint {(res.body or {}).get('endpoint')} · {(res.body or {}).get('pushes')} pushes")},
    ],
}
