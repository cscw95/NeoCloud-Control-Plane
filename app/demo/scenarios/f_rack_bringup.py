"""(f) Phase-2 DoD ③ — a new rack goes through the 15 bring-up steps (DCIM → ZTP →
LLDP → NICo → RMS → firmware → validation) and becomes sellable; a cable-plan
mismatch fails it at step 04 first.

Contracts: F23 (NOCP `/api/v1/dcim/racks`, `/api/v1/bringup/racks/{id}:start`;
AI-Infra Config Manager `/config-manager/v1/lldp-validate|lldp-faults`). The miswire
is injected on AI-Infra (physical twin); with AI-Infra offline that step is
skipped and step 04 cannot detect it (LLDP unreachable → local fallback).
"""

from ..engine import check, http
from ._common import link

SERIAL = "DEMO-VR72-0001"
PLAN = [{"a": "leaf-a/swp1", "b": "tray-00/cx8-p0"},
        {"a": "leaf-a/swp2", "b": "tray-01/cx8-p0"},
        {"a": "leaf-b/swp1", "b": "tray-00/cx8-p1"}]


def _registered(res, ctx):
    b = res.body
    if res.status == 201:
        ctx["rack"] = b.get("id")
    elif res.status == 409:
        ctx["rack"] = ctx.get("rack") or "rk-" + SERIAL.lower()
    return check(res.status in (201, 409) and ctx.get("rack"),
                 f"DCIM 등록 {ctx.get('rack')} · 상태 {b.get('state', 'planned')} · 케이블 {len(PLAN)}개",
                 f"DCIM registered {ctx.get('rack')} · state {b.get('state', 'planned')} · {len(PLAN)} cables")


def _miswired(res, ctx):
    ctx["miswire"] = True
    return check(res.status == 200, "AI-Infra 물리 트윈: leaf-a/swp2 → tray-02 로 잘못 연결",
                 "AI-Infra twin: leaf-a/swp2 physically cabled to tray-02")


def _failed_at_04(res, ctx):
    b = res.body
    if not ctx.get("miswire"):
        return (None, "AI-Infra 미연결 — 불일치 주입이 없어 04단계 실패를 보여줄 수 없음",
                "AI-Infra offline — no miswire, so step 04 cannot fail")
    steps = b.get("steps") or []
    s4 = next((s for s in steps if s.get("no") == 4), {})
    ok = b.get("state") == "failed" and b.get("failed_step") == 4
    return check(ok, f"04 {s4.get('key')} 실패: {s4.get('detail')} · 이후 단계 blocked",
                 f"04 {s4.get('key')} failed: {s4.get('detail')} · later steps blocked")


def _cleared(res, ctx):
    return check(res.status == 200, "현장 재결선 완료 (불일치 제거)", "re-cabled on site (miswire cleared)")


def _sellable(res, ctx):
    b = res.body
    p = b.get("progress") or {}
    ok = b.get("state") == "sellable" and p.get("passed") == p.get("total") == 15
    return check(ok, f"15단계 {p.get('passed')}/{p.get('total')} 통과 → {b.get('state')} ({b.get('source')})",
                 f"15 steps {p.get('passed')}/{p.get('total')} passed → {b.get('state')} ({b.get('source')})")


def _in_stock(res, ctx):
    rows = res.body if isinstance(res.body, list) else res.body.get("racks", [])
    ids = [r.get("id") or r.get("rack") for r in rows]
    return check(ctx["rack"] in ids, f"판매 가능 재고 {len(ids)}랙에 {ctx['rack']} 포함",
                 f"{ctx['rack']} is in sellable stock ({len(ids)} racks)")


SCENARIO = {
    "id": "f-rack-bringup",
    "order": 6,
    "feature": "F23",
    "title_ko": "(f) 신규 랙 bring-up 15단계 (04 케이블 불일치 실패 포함)",
    "title_en": "(f) New rack bring-up, 15 steps (incl. cable mismatch failing at 04)",
    "summary_ko": "DCIM 등록부터 판매 가능 재고까지 자동 진행. 케이블이 계획과 다르면 04 LLDP 검증에서 멈춘다 (Phase 2 DoD ③).",
    "summary_en": "From DCIM to sellable stock automatically; a cable off-plan stops at step 04 LLDP validation.",
    "steps": [
        {"id": "01-dcim", "feature": "F23", "requires": ["POST /api/v1/dcim/racks"],
         "title_ko": "DCIM 랙 등록", "title_en": "Register the rack in DCIM",
         "narration_ko": "NetBox 대체 DCIM에 시리얼·위치·청사진·케이블 계획을 넣는다.",
         "narration_en": "Serial, location, blueprint and cable plan go into the NetBox-style DCIM.",
         "call": http("POST", "/api/v1/dcim/racks",
                      {"serial": SERIAL, "site": "aif-igis-ansan-f2", "row": "R9", "position": "U01",
                       "blueprint_key": "vr-nvl72", "cable_plan": PLAN}),
         "expect": _registered,
         "links": [link("ops", "provisioning", "운영 · 프로비저닝 (bring-up)", "Ops · provisioning (bring-up)")]},
        {"id": "02-miswire", "feature": "F23", "requires": ["POST /api/v1/dcim/racks"],
         "title_ko": "케이블 오결선 주입 (AI-Infra)", "title_en": "Inject a miswire (AI-Infra)",
         "call": http("POST", "/config-manager/v1/lldp-faults",
                      lambda c: {"rack": c["rack"], "a": "leaf-a/swp2", "actual_b": "tray-02/cx8-p0"},
                      service="ai"),
         "expect": _miswired},
        {"id": "03-bringup-fail", "feature": "F23", "requires": ["POST /api/v1/bringup/racks/{id}:start"],
         "title_ko": "bring-up 시작 → 04 실패", "title_en": "Start bring-up → fails at 04",
         "narration_ko": "01 DCIM → 02 전원 → 03 ZTP → 04 LLDP 검증에서 계획과 다른 이웃을 찾아 멈춘다.",
         "narration_en": "01 DCIM → 02 power → 03 ZTP → 04 LLDP finds a neighbour off-plan and stops.",
         "expect_ko": "state=failed · failed_step=4", "expect_en": "state=failed · failed_step=4",
         "call": http("POST", lambda c: f"/api/v1/bringup/racks/{c['rack']}:start"),
         "expect": _failed_at_04},
        {"id": "04-recable", "feature": "F23", "requires": ["POST /api/v1/dcim/racks"],
         "title_ko": "현장 재결선", "title_en": "Re-cable on site",
         "call": http("DELETE", "/config-manager/v1/lldp-faults",
                      params=lambda c: {"rack": c["rack"]}, service="ai"),
         "expect": _cleared},
        {"id": "05-bringup-pass", "feature": "F23", "requires": ["POST /api/v1/bringup/racks/{id}:start"],
         "title_ko": "bring-up 재시작 → 15단계 통과", "title_en": "Restart bring-up → all 15 pass",
         "call": http("POST", lambda c: f"/api/v1/bringup/racks/{c['rack']}:start"),
         "expect": _sellable},
        {"id": "06-sellable", "feature": "F23", "requires": ["GET /api/v1/dcim/sellable"],
         "title_ko": "판매 가능 재고", "title_en": "Sellable stock",
         "call": http("GET", "/api/v1/dcim/sellable"), "expect": _in_stock,
         "links": [link("ops", "capacity", "운영 · 용량", "Ops · capacity")]},
    ],
}
