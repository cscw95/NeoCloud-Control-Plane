"""F24 firmware bundle demo — bundle catalog, iDRAC10 1.4.2 rule, idle-host plan,
Redfish SimpleUpdate (ImageURI + Oem.Nvidia.Bundle) in BMC → BF → GPU order.
Self-contained; format follows app/demo/engine.py. Deterministic: fixed bundle
version (re-run tolerates 409), plan on the first seeded fleet rack."""

VERSION = "vr-2026.08-demo"
COMPS = {"bmc": "1.4.2", "gpu": "97.11.0.demo", "nvswitch": "2.6.0", "cx": "40.45.1000", "bf": "3.2.0"}


def http(method, path, json=None, params=None):
    spec = {"method": method, "path": path, "as": "operator", "service": "nocp"}
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def _body(res):
    return res.body if isinstance(res.body, dict) else {}


def _catalog(res, ctx):
    b = _body(res)
    order = b.get("apply_order") or []
    return (order[:3] == ["bmc", "bf", "gpu"],
            f"번들 {len(b.get('bundles') or [])}개 · 적용 순서 {' → '.join(order)}",
            f"{len(b.get('bundles') or [])} bundles · apply order {' → '.join(order)}")


def _created(res, ctx):
    return (res.status in (201, 409), f"번들 {VERSION} 준비 (HTTP {res.status})",
            f"bundle {VERSION} ready (HTTP {res.status})")


def _refused(res, ctx):
    return (res.status == 400, "BMC(iDRAC10) 1.5.0 번들 거부 — 1.4.2 단일 숫자 규칙",
            "bundle with BMC (iDRAC10) 1.5.0 refused — single 1.4.2 rule")


def _rack(res, ctx):
    racks = res.body if isinstance(res.body, list) else []
    return (bool(racks), f"대상 랙 {racks[0]['id'] if racks else '-'}",
            f"target rack {racks[0]['id'] if racks else '-'}")


def _save_rack(res, ctx):
    ctx["rack"] = res.body[0]["id"]


def _planned(res, ctx):
    b = _body(res)
    return (res.status == 201 and b.get("status") == "scheduled",
            f"계획 {b.get('id')}: 유휴 호스트 {b.get('targets')}대 갱신 · {b.get('skipped')}대 제외",
            f"plan {b.get('id')}: {b.get('targets')} idle hosts updated · {b.get('skipped')} skipped")


def _save_plan(res, ctx):
    ctx["plan"] = res.body["id"]


def _executed(res, ctx):
    b = _body(res)
    r = b.get("result") or {}
    return (b.get("status") == "completed",
            f"Redfish SimpleUpdate 완료 ({r.get('source')}) · 순서 {' → '.join(r.get('order') or [])}",
            f"Redfish SimpleUpdate done ({r.get('source')}) · order {' → '.join(r.get('order') or [])}")


SCENARIO = {
    "id": "f24-firmware-bundle",
    "order": 24,
    "feature": "F24",
    "title_ko": "F24 펌웨어 번들 (nvfwupd형) · 유휴 호스트 롤아웃",
    "title_en": "F24 firmware bundles (nvfwupd-style) · idle-host rollout",
    "steps": [
        {"id": "01-catalog", "title_ko": "번들 목록", "title_en": "Bundle catalog",
         "call": http("GET", "/api/v1/firmware/bundles"), "expect": _catalog},
        {"id": "02-create", "title_ko": "새 번들 등록", "title_en": "Register a bundle",
         "call": http("POST", "/api/v1/firmware/bundles", {"version": VERSION, "components": COMPS}),
         "expect": _created},
        {"id": "03-bmc-rule", "title_ko": "iDRAC10 규칙 위반 거부", "title_en": "iDRAC10 rule enforced",
         "call": http("POST", "/api/v1/firmware/bundles",
                      {"version": "bad-bmc-demo", "components": {**COMPS, "bmc": "1.5.0"}}),
         "expect": _refused},
        {"id": "04-rack", "title_ko": "대상 랙 선택", "title_en": "Pick a target rack",
         "call": http("GET", "/api/v1/racks"), "expect": _rack, "save": _save_rack},
        {"id": "05-plan", "title_ko": "롤아웃 계획 (유휴 호스트만)", "title_en": "Rollout plan (idle hosts only)",
         "call": http("POST", "/api/v1/firmware/plans",
                      lambda c: {"rack": c["rack"], "bundle": VERSION, "window": "2026-11-12T02:00Z"}),
         "expect": _planned, "save": _save_plan},
        {"id": "06-execute", "title_ko": "실행 (Redfish SimpleUpdate)", "title_en": "Execute (Redfish SimpleUpdate)",
         "call": http("POST", lambda c: f"/api/v1/firmware/plans/{c['plan']}:execute"),
         "expect": _executed},
    ],
}
