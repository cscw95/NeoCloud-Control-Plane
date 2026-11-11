"""(f) Phase-2 DoD ③ — a new rack goes through the 15 bring-up steps (DCIM → ZTP →
LLDP → NICo → RMS → firmware → validation) and becomes sellable; a cable-plan
mismatch fails it at step 04 first.

Contracts: F23 (NOCP `/api/v1/dcim/racks|cables|sellable`, `/api/v1/bringup/inject`,
`/api/v1/bringup/racks/{id}:start`; AI-Infra Config Manager `lldp-validate|lldp-faults`
behind them). The miswire is injected on the AI-Infra physical twin through NOCP;
with AI-Infra offline that injection is not applied, so the step-04 failure is
reported as skipped (LLDP unreachable → local fallback), never as failed.
"""

from ..engine import check, http
from ._common import link

# Each run brings up its own rack (serial DEMO-VR72-<uid>), so reruns never meet a
# rack an earlier run already made sellable.


def SERIAL(c):
    return f"DEMO-VR72-{c['uid'].upper()}"


def RACK(c):
    return f"bu-demo-vr72-{c['uid']}"                # NOCP slug of the serial


def PLAN(c):
    r = RACK(c)
    return [{"a": f"{r}-tray-{i:02d}:p0", "b": f"{r}-leaf-sw-0:swp{i + 1}"} for i in range(18)]


def MISWIRE(c):
    return {"a": PLAN(c)[1]["a"], "actual_b": f"{RACK(c)}-leaf-sw-0:swp31"}


def _registered(res, ctx):
    b = res.body
    if res.status == 201:
        ctx["rack"] = b.get("id")
    return check(res.status == 201 and ctx.get("rack") == RACK(ctx),
                 f"DCIM 등록 {ctx.get('rack')} · 상태 {b.get('state', 'planned')} · 케이블 {len(PLAN(ctx))}개",
                 f"DCIM registered {ctx.get('rack')} · state {b.get('state', 'planned')} · {len(PLAN(ctx))} cables")


def _miswired(res, ctx):
    applied = isinstance(res.body, dict) and res.body.get("applied") is True
    ctx["miswire"] = applied
    if not applied:
        return (None, "AI-Infra 미연결 — 오결선 주입 안 됨(건너뜀)",
                "AI-Infra offline — miswire not applied (skipped)")
    m = MISWIRE(ctx)
    return check(True, f"AI-Infra 물리 트윈: {m['a']} → {m['actual_b']} 로 잘못 연결",
                 f"AI-Infra twin: {m['a']} physically cabled to {m['actual_b']}")


def _cables(res, ctx):
    rows = res.body.get("results") or [] if isinstance(res.body, dict) else []
    bad = [c for c in rows if c.get("lldp_mismatch")]
    if not ctx.get("miswire"):
        return (None, "주입 없음 — 불일치 표시 확인 생략", "no injection — mismatch flag not checked")
    return check(len(bad) == 1, f"NetBox 형식 케이블 {len(rows)}개 중 불일치 {len(bad)}개",
                 f"{len(bad)} of {len(rows)} NetBox-shaped cables flagged as mismatched")


def _failed_at_04(res, ctx):
    b = res.body
    ctx["after_first"] = b.get("state") if isinstance(b, dict) else None
    if not ctx.get("miswire"):
        return (None, "AI-Infra 미연결 — 불일치 주입이 없어 04단계 실패를 보여줄 수 없음",
                "AI-Infra offline — no miswire, so step 04 cannot fail")
    steps = b.get("steps") or []
    s4 = next((s for s in steps if s.get("no") == 4), {})
    ok = b.get("state") == "failed" and b.get("failed_step") == 4
    return check(ok, f"04 {s4.get('key')} 실패: {s4.get('detail')} · 이후 단계 blocked",
                 f"04 {s4.get('key')} failed: {s4.get('detail')} · later steps blocked")


def _cleared(res, ctx):
    return check(res.status == 200, "현장 재결선 완료 (주입 해제)", "re-cabled on site (fault cleared)")


def _sellable(res, ctx):
    b = res.body
    if res.status == 409 and ctx.get("after_first") == "sellable":
        return check(True, "첫 실행에서 이미 판매 가능(LLDP 미연결 폴백) — 재시작 불필요",
                     "already sellable after the first run (LLDP offline fallback) — no restart needed")
    p = b.get("progress") or {}
    ok = b.get("state") == "sellable" and p.get("passed") == 15
    return check(ok, f"15단계 {p.get('passed')}/{p.get('total')} 통과 → {b.get('state')} ({b.get('source')})",
                 f"15 steps {p.get('passed')}/{p.get('total')} passed → {b.get('state')} ({b.get('source')})")


def _in_stock(res, ctx):
    rows = res.body if isinstance(res.body, list) else res.body.get("racks", [])
    ids = [r.get("id") or r.get("rack") for r in rows]
    ctx["sellable_racks"] = len(ids)
    return check(ctx["rack"] in ids, f"판매 가능 재고 {len(ids)}랙에 {ctx['rack']} 포함",
                 f"{ctx['rack']} is in sellable stock ({len(ids)} racks)")


SCENARIO = {
    "id": "f-rack-bringup",
    "order": 6,
    "feature": "F23",
    "title_ko": "신규 랙 bring-up 15단계 (04 케이블 불일치 실패 포함)",
    "title_en": "New rack bring-up, 15 steps (incl. cable mismatch failing at 04)",
    "summary_ko": "DCIM 등록부터 판매 가능 재고까지 자동 진행. 케이블이 계획과 다르면 04 LLDP 검증에서 멈춘다 (Phase 2 DoD ③).",
    "summary_en": "From DCIM to sellable stock automatically; a cable off-plan stops at step 04 LLDP validation.",
    "steps": [
        {"id": "01-dcim", "feature": "F23", "requires": ["POST /api/v1/dcim/racks"],
         "title_ko": "DCIM 랙 등록", "title_en": "Register the rack in DCIM",
         "narration_ko": "NetBox 대체 DCIM에 시리얼·위치·청사진·케이블 계획을 넣는다.",
         "narration_en": "Serial, location, blueprint and cable plan go into the NetBox-style DCIM.",
         "call": http("POST", "/api/v1/dcim/racks",
                      {"serial": SERIAL, "site": "gasan", "row": "R9", "position": lambda c: "U-" + c["uid"].upper(),
                       "blueprint_key": "vr-nvl72", "cable_plan": PLAN}),
         "expect": _registered,
         "links": [link("ops", "bringup", "운영 · bring-up", "Ops · bring-up"),
                   link("ops", "provisioning", "운영 · 프로비저닝", "Ops · provisioning")]},
        {"id": "02-miswire", "feature": "F23",
         "title_ko": "케이블 오결선 주입 (AI-Infra)", "title_en": "Inject a miswire (AI-Infra)",
         "requires": ["POST /api/v1/bringup/inject"],
         "call": http("POST", "/api/v1/bringup/inject",
                      lambda c: {"kind": "miswire", "rack": c["rack"], **MISWIRE(c)}),
         "expect": _miswired},
        {"id": "03-bringup-fail", "feature": "F23", "requires": ["POST /api/v1/bringup/racks/{id}:start"],
         "title_ko": "bring-up 시작 → 04 실패", "title_en": "Start bring-up → fails at 04",
         "narration_ko": "01 DCIM → 02 전원 → 03 ZTP → 04 LLDP 검증에서 계획과 다른 이웃을 찾아 멈춘다.",
         "narration_en": "01 DCIM → 02 power → 03 ZTP → 04 LLDP finds a neighbour off-plan and stops.",
         "expect_ko": "state=failed · failed_step=4", "expect_en": "state=failed · failed_step=4",
         "call": http("POST", lambda c: f"/api/v1/bringup/racks/{c['rack']}:start"),
         "expect": _failed_at_04},
        {"id": "04-cables", "feature": "F23", "requires": ["GET /api/v1/dcim/cables"],
         "title_ko": "케이블 불일치 확인 (NetBox 형식)", "title_en": "Cable mismatch (NetBox shape)",
         "call": http("GET", "/api/v1/dcim/cables", params=lambda c: {"rack": c["rack"]}),
         "expect": _cables},
        {"id": "05-recable", "feature": "F23", "requires": ["POST /api/v1/bringup/inject"],
         "title_ko": "현장 재결선", "title_en": "Re-cable on site",
         "call": http("POST", "/api/v1/bringup/inject", lambda c: {"kind": "clear", "rack": c["rack"]}),
         "expect": _cleared},
        {"id": "06-bringup-pass", "feature": "F23", "requires": ["POST /api/v1/bringup/racks/{id}:start"],
         "title_ko": "bring-up 재시작 → 15단계 통과", "title_en": "Restart bring-up → all 15 pass",
         "call": http("POST", lambda c: f"/api/v1/bringup/racks/{c['rack']}:start"),
         "expect": _sellable},
        {"id": "07-sellable", "feature": "F23", "requires": ["GET /api/v1/dcim/sellable"],
         "title_ko": "판매 가능 재고", "title_en": "Sellable stock",
         "call": http("GET", "/api/v1/dcim/sellable"), "expect": _in_stock,
         "links": [link("ops", "capacity", "운영 · 용량", "Ops · capacity")]},
    ],
}
