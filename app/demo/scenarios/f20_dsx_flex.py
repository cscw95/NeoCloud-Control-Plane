"""F20 DSX Flex 1.7.0 demo — Load Target Set → Power State Status → (no shed) →
30 s sustained breach → PowerBreachAlert → PowerBreachEnforcement → compliant →
clear (load_constraint null) → enforcement reverted.

Self-contained (no import of app/demo/engine.py); step format follows engine.py.
All calls go to AI-Infra (`service: "ai"`); the emulator clock (`/flex/v1/clock`)
makes the 30 s breach window deterministic. Raw CloudEvents (specversion 1.0.2)
are shown by step 07 (`/flex/v1/messages`).
"""

FEED = "pd-aif-stt-gasan-f1"


def http(method, path, json=None, params=None):
    spec = {"method": method, "path": path, "as": "operator", "service": "ai"}
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def _body(res):
    return res.body if isinstance(res.body, dict) else {}


def _reset(ctx):
    from ... import dsx
    body = dsx.ai_get("/flex/v1/load-target-sets", params={"site": FEED}) or {}
    n = 0
    for lt in body.get("items", []):
        if lt.get("state") in ("scheduled", "active"):
            dsx.ai_send("DELETE", f"/flex/v1/load-target-sets/{lt['id']}")
            n += 1
    msgs = (dsx.ai_get("/flex/v1/messages", params={"limit": 1}) or {}).get("items", [])
    ctx["since"] = msgs[-1]["seq"] if msgs else 0
    return {"cleared": n, "since": ctx["since"]}


def _cleared(res, ctx):
    return (True, f"이전 목표 {_body(res).get('cleared', 0)}건 해제 · 메시지 기준점 {ctx.get('since')}",
            f"cleared {_body(res).get('cleared', 0)} old targets · message cursor {ctx.get('since')}")


def _baseline(res, ctx):
    b = _body(res)
    ctx["baseline_kw"] = b.get("measured_kw")
    return (res.status == 200 and b.get("compliance") == "ok",
            f"{FEED} 측정 {b.get('measured_kw')} kW · 목표 없음 · {b.get('compliance')}",
            f"{FEED} measured {b.get('measured_kw')} kW · no target · {b.get('compliance')}")


def _reduce_kw(ctx):
    """−400 kW, or half the feed when it carries less than 800 kW (what ran before
    may have released racks on this feed); a target of 0 kW could never be met."""
    base = ctx.get("baseline_kw") or 0
    return 400 if base >= 800 else max(1, round(base / 2))


def _lts(res, ctx):
    b = _body(res)
    ctx["lts_id"] = b.get("id")
    ce = b.get("cloudevent") or {}
    return (res.status == 201 and ce.get("specversion") == "1.0.2",
            f"Load Target Set {b.get('id')} · 목표 {b.get('target_kw')} kW (−{_reduce_kw(ctx)}) · "
            f"{ce.get('type')} specversion {ce.get('specversion')}",
            f"Load Target Set {b.get('id')} · target {b.get('target_kw')} kW (−{_reduce_kw(ctx)}) · "
            f"{ce.get('type')} specversion {ce.get('specversion')}")


def _breach_now(res, ctx):
    b = _body(res)
    return (b.get("compliance") == "breach",
            f"아무도 줄이지 않음 → 측정 {b.get('measured_kw')} > 목표 {b.get('target_kw')} kW · breach",
            f"nobody shed → measured {b.get('measured_kw')} > target {b.get('target_kw')} kW · breach")


def _advanced(res, ctx):
    return (res.status == 200, f"Flex Agent 시계 +35초 (지속 기준 {_body(res).get('breach_after_s')}초)",
            f"Flex Agent clock +35 s (sustain window {_body(res).get('breach_after_s')} s)")


def _alert_enf(res, ctx):
    items = _body(res).get("items") or []
    types = [m["cloudevent"]["type"] for m in items]
    alert = next((m["cloudevent"]["data"] for m in items
                  if m["cloudevent"]["type"] == "grid.powerbreach.alert.v1"), {})
    enf = next((m["cloudevent"]["data"] for m in items
                if m["cloudevent"]["type"] == "grid.powerbreach.enforcement.v1"), {})
    return ("grid.powerbreach.alert.v1" in types and enf.get("action") == "applied",
            f"PowerBreachAlert {alert.get('status')}/{alert.get('severity')} · shed_hints "
            f"{len(alert.get('shed_hints') or [])}개 → Enforcement {enf.get('action')}/{enf.get('code')}",
            f"PowerBreachAlert {alert.get('status')}/{alert.get('severity')} · "
            f"{len(alert.get('shed_hints') or [])} shed_hints → Enforcement {enf.get('action')}/{enf.get('code')}")


def _ok(res, ctx):
    b = _body(res)
    return (b.get("compliance") == "ok",
            f"DPS 추가 하향 후 측정 {b.get('measured_kw')} kW ≤ 목표 {b.get('target_kw')} kW · ok",
            f"after the DPS cut measured {b.get('measured_kw')} kW ≤ target {b.get('target_kw')} kW · ok")


def _reverted(res, ctx):
    b = _body(res)
    return (b.get("state") == "cleared",
            f"목표 해제(load_constraint null) → {b.get('state')} · DPS 하향 원복(enforcement reverted)",
            f"target cleared (load_constraint null) → {b.get('state')} · DPS cut reverted")


SCENARIO = {
    "id": "f20-dsx-flex",
    "order": 20,
    "feature": "F20",
    "title_ko": "F20 DSX Flex 1.7.0 메시지 (Load Target → Status → Breach → Enforcement)",
    "title_en": "F20 DSX Flex 1.7.0 messages (Load Target → Status → Breach → Enforcement)",
    "summary_ko": "원본 AsyncAPI(dsx-flex.yaml 1.7.0) 형식의 CloudEvents로 감축 목표·상태·초과·강제 조치를 주고받는다.",
    "summary_en": "Load target, status, breach and enforcement exchanged as upstream dsx-flex.yaml 1.7.0 CloudEvents.",
    "steps": [
        {"id": "00-reset", "title_ko": "이전 목표 해제", "title_en": "Clear old targets",
         "call": _reset, "expect": _cleared},
        {"id": "01-status", "title_ko": "Power State Status (기준)", "title_en": "Power State Status (baseline)",
         "call": http("GET", "/flex/v1/power-state-status", params={"site": FEED}), "expect": _baseline},
        {"id": "02-load-target", "title_ko": "Load Target Set −400 kW", "title_en": "Load Target Set −400 kW",
         "call": http("POST", "/flex/v1/load-target-sets",
                      {"site": FEED, "reduce_kw": _reduce_kw, "duration_s": 900}), "expect": _lts},
        {"id": "03-breach", "title_ko": "감축 전: 목표 초과", "title_en": "Before shedding: over target",
         "call": http("GET", "/flex/v1/power-state-status", params={"site": FEED}), "expect": _breach_now},
        {"id": "04-clock", "title_ko": "30초 지속 (모사 시계)", "title_en": "Sustained 30 s (emulated clock)",
         "call": http("POST", "/flex/v1/clock", {"advance_s": 35}), "expect": _advanced},
        {"id": "05-messages", "title_ko": "Breach Alert → Enforcement 원문", "title_en": "Breach Alert → Enforcement (raw)",
         "call": http("GET", "/flex/v1/messages", params=lambda c: {"since": c.get("since", 0)}),
         "expect": _alert_enf},
        {"id": "06-compliant", "title_ko": "강제 조치 후 준수", "title_en": "Compliant after enforcement",
         "call": http("GET", "/flex/v1/power-state-status", params={"site": FEED}), "expect": _ok},
        {"id": "07-clear", "title_ko": "목표 해제 · 원복", "title_en": "Clear target · revert",
         "call": http("DELETE", lambda c: f"/flex/v1/load-target-sets/{c['lts_id']}"),
         "expect": _reverted},
    ],
}
