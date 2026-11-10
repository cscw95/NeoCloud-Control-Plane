"""F23 rack bring-up demo — DCIM → 15 steps, cable miswire fails step 04, fix, sellable.

Self-contained (no import of app/demo/engine.py) so it also loads on branches
without the v2-demo runner; the step format follows engine.py. Deterministic:
fixed serial SN-DEMO-0001, step 00 resets the demo rack and its LLDP faults.
"""

SERIAL = "SN-DEMO-0001"
RACK = "bu-sn-demo-0001"
PLAN = [{"a": f"{RACK}-tray-{i:02d}:p0", "b": f"{RACK}-leaf-sw-0:swp{i + 1}"} for i in range(18)]
MISWIRE = {"a": PLAN[4]["a"], "actual_b": f"{RACK}-leaf-sw-0:swp30"}


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


def _registered(res, ctx):
    b = _body(res)
    return (res.status == 201 and b.get("state") == "planned",
            f"DCIM 랙 {b.get('id')} 등록 · 상태 {b.get('state')} · 케이블 {len(b.get('cable_plan') or [])}개",
            f"DCIM rack {b.get('id')} registered · state {b.get('state')} · "
            f"{len(b.get('cable_plan') or [])} cables")


def _injected(res, ctx):
    b = _body(res)
    return (b.get("applied") is True,
            f"AI-Infra LLDP 주입: {MISWIRE['a']} → {MISWIRE['actual_b']} (HTTP {res.status})",
            f"AI-Infra LLDP fault: {MISWIRE['a']} → {MISWIRE['actual_b']} (HTTP {res.status})")


def _failed_at_4(res, ctx):
    b = _body(res)
    s4 = (b.get("steps") or [{}] * 4)[3]
    return (b.get("state") == "failed" and b.get("failed_step") == 4,
            f"04단계 실패: {s4.get('detail')} · 05–15단계 차단",
            f"step 04 failed: {s4.get('detail')} · steps 05–15 blocked")


def _cables(res, ctx):
    rows = _body(res).get("results") or []
    bad = [c for c in rows if c.get("lldp_mismatch")]
    return (len(bad) == 1,
            f"NetBox 케이블 {len(rows)}개 중 불일치 {len(bad)}개 표시",
            f"{len(bad)} of {len(rows)} NetBox cables flagged as mismatched")


def _cleared(res, ctx):
    return (res.status == 200, "현장 조치: 케이블 재결선(주입 해제)", "Field fix: cable re-seated (fault cleared)")


def _sellable(res, ctx):
    b = _body(res)
    p = b.get("progress") or {}
    return (b.get("state") == "sellable" and p.get("passed") == 15,
            f"재시도 → {b.get('state')} · {p.get('passed')}/15 단계 통과",
            f"retry → {b.get('state')} · {p.get('passed')}/15 steps passed")


def _pool(res, ctx):
    b = _body(res)
    ids = [r.get("id") for r in b.get("racks") or []]
    return (RACK in ids,
            f"판매 가능 재고: 랙 {b.get('rack_count')} · GPU {b.get('gpu_count')}",
            f"sellable inventory: {b.get('rack_count')} racks · {b.get('gpu_count')} GPUs")


SCENARIO = {
    "id": "f23-rack-bringup",
    "order": 23,
    "feature": "F23",
    "title_ko": "F23 랙 bring-up 15단계 (케이블 불일치 → 04단계 실패 → 조치 → 판매 가능)",
    "title_en": "F23 rack bring-up, 15 steps (miswire → step 04 fails → fix → sellable)",
    "summary_ko": "NetBox형 DCIM 등록 → ZTP → LLDP 검증 → NICo expected-rack → RMS 전원·펌웨어 → 증명 → 검증 → 재고.",
    "summary_en": "NetBox-style DCIM → ZTP → LLDP check → NICo expected-rack → RMS power/firmware → "
                  "attestation → validation → inventory.",
    "steps": [
        {"id": "00-reset", "title_ko": "데모 초기화", "title_en": "Reset demo rack",
         "call": http("POST", "/api/v1/bringup/demo/reset"), "expect": {"status": 200}},
        {"id": "01-register", "title_ko": "DCIM 랙 등록", "title_en": "Register rack in DCIM",
         "narration_ko": "시리얼·위치·케이블 계획 18개를 NetBox 대체 DCIM에 넣는다.",
         "narration_en": "Serial, position and an 18-cable plan go into the NetBox-style DCIM.",
         "call": http("POST", "/api/v1/dcim/racks",
                      {"serial": SERIAL, "site": "gasan", "row": "DEMO", "position": "01",
                       "blueprint_key": "vr-nvl72", "cable_plan": PLAN}),
         "expect": _registered, "links": [link("bringup", "운영 · bring-up", "Ops · bring-up")]},
        {"id": "02-inject-miswire", "title_ko": "케이블 오결선 주입", "title_en": "Inject a miswired cable",
         "call": http("POST", "/api/v1/bringup/inject",
                      {"kind": "miswire", "rack": RACK, **MISWIRE}),
         "expect": _injected},
        {"id": "03-start-fails", "title_ko": "bring-up 실행 → 04단계 실패",
         "title_en": "Start bring-up → step 04 fails",
         "call": http("POST", f"/api/v1/bringup/racks/{RACK}:start"), "expect": _failed_at_4},
        {"id": "04-cables", "title_ko": "케이블 불일치 확인 (NetBox 형식)",
         "title_en": "Check cable mismatch (NetBox shape)",
         "call": http("GET", "/api/v1/dcim/cables", params={"rack": RACK}), "expect": _cables},
        {"id": "05-fix", "title_ko": "케이블 조치", "title_en": "Fix the cable",
         "call": http("POST", "/api/v1/bringup/inject", {"kind": "clear", "rack": RACK}),
         "expect": _cleared},
        {"id": "06-retry", "title_ko": "재시도 → 15단계 통과", "title_en": "Retry → all 15 steps pass",
         "call": http("POST", f"/api/v1/bringup/racks/{RACK}:start"), "expect": _sellable},
        {"id": "07-inventory", "title_ko": "판매 가능 재고 확인", "title_en": "Sellable inventory",
         "call": http("GET", "/api/v1/dcim/sellable"), "expect": _pool},
    ],
}
