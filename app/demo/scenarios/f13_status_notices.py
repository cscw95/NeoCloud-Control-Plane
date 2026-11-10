"""F13 status-page notices demo — maintenance (scheduled) and grid curtailment
(active) notices appear on the public status page and in /metrics, then the
curtailment notice is resolved. Self-contained; format follows app/demo/engine.py.
Deterministic: step 00 removes earlier demo notices (source "demo")."""
from datetime import datetime, timedelta, timezone


def http(method, path, json=None, params=None, as_="operator"):
    spec = {"method": method, "path": path, "as": as_, "service": "nocp"}
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def _b(res):
    return res.body if isinstance(res.body, dict) else {}


def _maint(ctx):
    t0 = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    return {"kind": "maintenance", "components": ["Storage", "managed-k8s"],
            "start": t0.isoformat(), "end": (t0 + timedelta(hours=2)).isoformat(),
            "message_ko": "VAST Data 스토리지 펌웨어 점검 (2시간)",
            "message_en": "VAST Data storage firmware maintenance (2 hours)", "source": "demo"}


GRID = {"kind": "grid_curtailment", "components": ["bmaas", "token-api"],
        "message_ko": "전력망 감축 요청 대응 중 — 예약 고객 보호, 토큰 Batch 등급 일시 축소",
        "message_en": "Responding to a grid curtailment request — reserved customers protected, "
                      "token batch tier temporarily reduced", "source": "demo"}


def _created(state):
    def expect(res, ctx):
        b = _b(res)
        return (res.status == 201 and b.get("state") == state,
                f"공지 {b.get('id')} · 종류 {b.get('kind')} · 상태 {b.get('state')}",
                f"notice {b.get('id')} · kind {b.get('kind')} · state {b.get('state')}")
    return expect


def _page(res, ctx):
    ns = {n["id"]: n for n in _b(res).get("notices", [])}
    ok = ctx["maint"] in ns and ctx["grid"] in ns
    return (ok, f"상태 페이지 공지 {len(ns)}건 (점검 {ns.get(ctx['maint'], {}).get('state')}, "
                f"감축 {ns.get(ctx['grid'], {}).get('state')})",
            f"{len(ns)} notices on the status page (maintenance "
            f"{ns.get(ctx['maint'], {}).get('state')}, curtailment {ns.get(ctx['grid'], {}).get('state')})")


def _metric(res, ctx):
    line = next((l for l in (res.text or "").splitlines()
                 if l.startswith('nocp_status_notices{kind="grid_curtailment",state="resolved"}')), "")
    ok = res.status == 200 and line and float(line.split()[-1]) >= 1
    return (bool(ok), f"/metrics: {line or '없음'}", f"/metrics: {line or 'missing'}")


SCENARIO = {
    "id": "f13-status-notices",
    "order": 13,
    "feature": "F13",
    "title_ko": "F13 상태 페이지 공지 유형 (장애 · 점검 · 그리드 감축)",
    "title_en": "F13 status-page notice types (incident · maintenance · grid curtailment)",
    "steps": [
        {"id": "00-reset", "title_ko": "이전 시연 공지 정리", "title_en": "Remove earlier demo notices",
         "call": http("POST", "/api/v1/status/notices/demo/reset"), "feature": "F13"},
        {"id": "01-maintenance", "title_ko": "계획 점검 공지 (내일)", "title_en": "Scheduled maintenance (tomorrow)",
         "call": http("POST", "/api/v1/status/notices", _maint),
         "expect": _created("scheduled"), "save": lambda res, ctx: ctx.update(maint=_b(res)["id"]),
         "feature": "F13"},
        {"id": "02-grid", "title_ko": "그리드 감축 공지 (진행 중)", "title_en": "Grid curtailment notice (active)",
         "call": http("POST", "/api/v1/status/notices", GRID), "expect": _created("active"),
         "save": lambda res, ctx: ctx.update(grid=_b(res)["id"]), "feature": "F13"},
        {"id": "03-page", "title_ko": "공개 상태 페이지 확인", "title_en": "Public status page",
         "call": http("GET", "/api/v1/status", as_="none"), "expect": _page, "feature": "F13"},
        {"id": "04-resolve", "title_ko": "감축 종료 → 공지 해제", "title_en": "Curtailment over → resolve",
         "call": http("POST", lambda ctx: f"/api/v1/status/notices/{ctx['grid']}/resolve", {}),
         "expect": lambda res, ctx: (_b(res).get("state") == "resolved",
                                     f"상태 {_b(res).get('state')} · 해제 {_b(res).get('resolved_at')}",
                                     f"state {_b(res).get('state')} · resolved {_b(res).get('resolved_at')}"),
         "feature": "F13"},
        {"id": "05-metrics", "title_ko": "Prometheus 지표 반영", "title_en": "Reflected in Prometheus metrics",
         "call": http("GET", "/metrics", as_="none"), "expect": _metric, "feature": "F26"},
    ],
}
