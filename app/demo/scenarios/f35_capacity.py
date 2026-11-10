"""F35 demo — capacity scenarios (DSX Sim stand-in): 104 Vera Rubin NVL72
racks on a 30 MW site, answered by a bundle of AI-Infra DPS simulate calls
(static vs dynamic provisioning per power profile, DPS 공개 문서 기준).

Deterministic: DPS simulate is a pure function of its inputs (no live
telemetry), so every run gives the same rack counts. The first step calls
DPS directly so the raw request/response is visible; the scenario then asks
NOCP for the bundled answer and a tighter 25 MW site that does not fit.
"""

from __future__ import annotations

from ._kcc_local import body, check, http

IT_KW = 24000.0            # 30 MW / PUE 1.25


def _dps(res, ctx):
    b = body(res)
    st, dy = b["static"], b["dynamic"]
    return check(res.status == 200 and st["racks_supported"] >= 104,
                 f"DPS simulate {IT_KW:,.0f} kW max-p: 정적 {st['racks_supported']}랙"
                 f"(랙당 {st['rack_budget_kw']} kW) · 동적 {dy['racks_supported']}랙"
                 f"(랙당 {dy['rack_budget_kw']} kW)",
                 f"DPS simulate {IT_KW:,.0f} kW max-p: static {st['racks_supported']} racks"
                 f" · dynamic {dy['racks_supported']} racks")


def _fits(res, ctx):
    b = body(res)
    ctx["scenario_id"] = b.get("id")
    rows = [f"{r['profile']}: 정적 {r['static']['racks_supported']} / 동적 "
            f"{r['dynamic']['racks_supported']}" for r in b.get("results", [])]
    return check(res.status == 201 and b["fits"] and b["source"] == "dps-simulate",
                 b["summary"]["ko"] + " · " + " · ".join(rows),
                 b["summary"]["en"])


def _not_fit(res, ctx):
    b = body(res)
    st = b["results"][0]["static"]
    return check(res.status == 201 and not st["fits"],
                 f"{b['summary']['ko']} 정적 max-p {st['racks_supported']}랙 · 동적 "
                 f"{b['results'][0]['dynamic']['racks_supported']}랙 "
                 f"(동적 {'충족' if b['results'][0]['dynamic']['fits'] else '부족'})",
                 b["summary"]["en"])


SCENARIO = {
    "id": "f35-capacity-scenarios",
    "order": 35,
    "feature": "F35",
    "title_ko": "F35 용량 시나리오: 104랙 · 30 MW (DPS simulate 묶음)",
    "title_en": "F35 capacity scenario: 104 racks · 30 MW (DPS simulate bundle)",
    "summary_ko": "사이트 30 MW(PUE 1.25 → IT 24 MW)에 Vera Rubin NVL72 104랙이 들어가는지 "
                  "전력 프로파일마다 DPS 정적·동적 산정을 묶어 판정한다. 25 MW에서는 "
                  "정적으로는 부족하고 동적 프로비저닝이 차이를 메운다.",
    "summary_en": "Does a 30 MW site (PUE 1.25 → 24 MW IT) hold 104 NVL72 racks? DPS "
                  "static vs dynamic per profile; at 25 MW only dynamic provisioning fits.",
    "steps": [
        {"id": "dps", "title_ko": "AI-Infra DPS simulate 원 호출 (max-p, 24 MW)",
         "title_en": "Raw AI-Infra DPS simulate (max-p, 24 MW)",
         "call": http("POST", "/dps/v1/simulate", service="ai", json={
             "operating_limit_kw": IT_KW, "profile": "max-p", "gpus_per_rack": 72}),
         "expect": _dps},
        {"id": "scenario", "title_ko": "104랙 · 30 MW 시나리오 (max-p·balanced·max-q 비교)",
         "title_en": "104 racks · 30 MW (compare max-p, balanced, max-q)",
         "call": http("POST", "/api/v1/capacity/scenarios", json={
             "racks": 104, "site_mw": 30, "pue": 1.25, "policy": "compare",
             "profile": ["max-p", "balanced", "max-q"], "name": "F35 demo 104 racks 30 MW"}),
         "expect": _fits},
        {"id": "tight", "title_ko": "같은 104랙을 25 MW 사이트에",
         "title_en": "Same 104 racks on a 25 MW site",
         "call": http("POST", "/api/v1/capacity/scenarios", json={
             "racks": 104, "site_mw": 25, "pue": 1.25, "policy": "compare",
             "profile": "max-p", "name": "F35 demo 104 racks 25 MW"}),
         "expect": _not_fit},
        {"id": "get", "title_ko": "저장된 시나리오 조회", "title_en": "Read the stored scenario",
         "call": http("GET", lambda c: f"/api/v1/capacity/scenarios/{c['scenario_id']}")},
    ],
}
