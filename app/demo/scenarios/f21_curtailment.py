"""F21 curtailment demo — reserved protected, token batch + spot shrink first,
SLA exception and status notice, restore and demand-response settlement.

Self-contained (no import of app/demo/engine.py); step format follows engine.py.
Step 00 resets the engine and claims three free racks of one power domain for
fixed demo tenants (reserved / on-demand / spot), so runs are repeatable.
"""

NAMES = {"reserved": "demo-f21-reserved", "on_demand": "demo-f21-ondemand",
         "spot": "demo-f21-spot"}


def http(method, path, json=None, params=None, service="nocp"):
    spec = {"method": method, "path": path, "as": "operator", "service": service}
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def link(route, ko, en):
    return {"console": "ops", "route": route, "label_ko": ko, "label_en": en}


def _body(res):
    return res.body if isinstance(res.body, dict) else {}


def _setup(ctx):
    from ... import curtailment, power
    from ...models import AllocationCreate, AllocationScope, IsolationTier, TenantCreate
    from ...store import STORE
    from ...tenancy import create_allocation, create_tenant
    curtailment.reset()
    tids = {}
    for kind, name in NAMES.items():
        t = next((t for t in STORE.tenants.values() if t.name == name), None)
        if t is None:
            t = create_tenant(TenantCreate(name=name, isolation_tier=IsolationTier.vm_multitenant))
        tids[kind] = t.id
    dom = None
    for d in power._local_domains():
        own = [r for r in d["racks"] if STORE.racks[r].tenant_id in tids.values()]
        free = [r for r in d["racks"] if STORE.racks[r].tenant_id is None]
        if len(own) >= 3 or len(free) >= 3:
            dom = d
            break
    if dom is None:
        raise RuntimeError("no power domain with 3 free racks")
    for kind in ("reserved", "on_demand", "spot"):
        if any(STORE.racks[r].tenant_id == tids[kind] for r in dom["racks"]):
            continue
        rid = next(r for r in dom["racks"] if STORE.racks[r].tenant_id is None)
        su = STORE.racks[rid].su_id
        create_allocation(AllocationCreate(tenant_id=tids[kind], su_id=su,
                                           scope=AllocationScope.rack_set, rack_ids=[rid]))
    ctx.update(domain=dom["id"], tenants=tids, reserved_tenant=tids["reserved"],
               reserved_racks=[r for r in dom["racks"]
                               if STORE.racks[r].tenant_id == tids["reserved"]])
    return {"domain": dom["id"], "tenants": tids, "reserved_racks": ctx["reserved_racks"]}


def _ready(res, ctx):
    return (True, f"도메인 {ctx['domain']} · 예약 랙 {', '.join(ctx['reserved_racks'])}",
            f"domain {ctx['domain']} · reserved racks {', '.join(ctx['reserved_racks'])}")


def _policy(ctx):
    return {"tiers": [{"name": "token-batch", "match": {"product": "token-api", "tier": "batch"},
                       "order": 1, "max_reduction_pct": 70},
                      {"name": "spot", "match": {"commitment": "spot"}, "order": 2,
                       "max_reduction_pct": 50},
                      {"name": "on-demand", "match": {"commitment": "on_demand"}, "order": 3,
                       "max_reduction_pct": 20}],
            "protect": ["reserved"], "commitments": {v: k for k, v in ctx["tenants"].items()}}


def _policy_ok(res, ctx):
    b = _body(res)
    return (res.status == 200 and b.get("protect") == ["reserved"],
            "정책: 예약 보호 > 온디맨드(20%) > 스팟(50%) > 토큰 Batch(70%) 순으로 감축",
            "policy: reserved protected; shed token batch (70%) → spot (50%) → on-demand (20%)")


def _run(res, ctx):
    b = _body(res)
    ctx["cid"] = b.get("id")
    acts = b.get("actions") or []
    hit = [a for a in acts if a.get("target") in ctx["reserved_racks"]
           or a.get("tenant_id") == ctx["reserved_tenant"]]
    kinds = sorted({a["kind"] for a in acts})
    return (res.status == 201 and not hit and b.get("achieved_kw", 0) > 0,
            f"요청 {b.get('requested_kw')} kW · 달성 {b.get('achieved_kw')} kW · 조치 {', '.join(kinds)} · "
            f"예약 고객 조치 0건 · 보호 고객 {(b.get('protected') or {}).get('tenant_count')}",
            f"requested {b.get('requested_kw')} kW · achieved {b.get('achieved_kw')} kW · "
            f"actions {', '.join(kinds)} · 0 on reserved · "
            f"{(b.get('protected') or {}).get('tenant_count')} protected")


def _quota(res, ctx):
    items = _body(res).get("items") or []
    f = sorted({o["factor"] for o in items if o.get("tier") == "batch"})
    return (bool(items) and all(o.get("tier") == "batch" for o in items),
            f"토큰 Batch override factor {f} (TPM 축소 → 429) · Interactive 유지",
            f"token batch override factor {f} (TPM cut → 429) · interactive untouched")


def _detail(res, ctx):
    b = _body(res)
    ex, nt = b.get("sla_exception") or {}, b.get("notice") or {}
    return (bool(b.get("sla_exception_id") and b.get("notice_id")),
            f"SLA 예외 {b.get('sla_exception_id')} ({ex.get('kind')}) · 상태 공지 {b.get('notice_id')}: "
            f"{nt.get('message_ko', '')[:40]}…",
            f"SLA exception {b.get('sla_exception_id')} ({ex.get('kind')}) · notice "
            f"{b.get('notice_id')}: {nt.get('message_en', '')[:40]}…")


def _restored(res, ctx):
    b = _body(res)
    s = b.get("settlement") or {}
    return (b.get("state") == "restored",
            f"감축 종료 → {b.get('state')} · 원복 {len(b.get('restored') or [])}건 · 정산 {s.get('kwh')} kWh · "
            f"₩{s.get('incentive_krw')}",
            f"curtailment ended → {b.get('state')} · {len(b.get('restored') or [])} restored · "
            f"settled {s.get('kwh')} kWh · KRW {s.get('incentive_krw')}")


def _settlements(res, ctx):
    items = _body(res).get("items") or []
    return (bool(items), f"수요반응 정산 기록 {len(items)}건 (F27 입력)",
            f"{len(items)} demand-response settlement record(s) (F27 input)")


SCENARIO = {
    "id": "f21-curtailment",
    "order": 21,
    "feature": "F21",
    "title_ko": "F21 감축 정책: 예약 보호 · Batch/스팟 우선 축소 · 원복 · 정산",
    "title_en": "F21 curtailment policy: reserved protected, batch/spot first, restore, settle",
    "summary_ko": "Load Target 감축량을 정책 등급 순서로 배분하고 예약 고객 랙은 건드리지 않는다.",
    "summary_en": "The load target is split over policy tiers in order; reserved racks are never touched.",
    "steps": [
        {"id": "00-setup", "title_ko": "데모 고객 3곳 (예약·온디맨드·스팟)",
         "title_en": "Three demo tenants (reserved, on-demand, spot)", "call": _setup, "expect": _ready},
        {"id": "01-policy", "title_ko": "감축 정책", "title_en": "Curtailment policy",
         "call": http("PUT", "/api/v1/power/curtailment-policy", _policy), "expect": _policy_ok},
        {"id": "02-curtail", "title_ko": "감축 실행 −600 kW", "title_en": "Curtail −600 kW",
         "narration_ko": "DSX Flex Load Target Set(F20)이 함께 생성되고 Power State Status로 준수를 확인한다.",
         "narration_en": "A DSX Flex Load Target Set (F20) is created too; Power State Status tracks compliance.",
         "call": http("POST", "/api/v1/power/curtailments",
                      lambda c: {"requested_kw": 600, "duration_s": 900, "domain": c["domain"]}),
         "expect": _run, "links": [link("dsx-power", "운영 · MaxLPS 전력", "Ops · MaxLPS power")]},
        {"id": "03-quota", "title_ko": "토큰 Batch 쿼터 축소", "title_en": "Token batch quota cut",
         "call": http("GET", "/api/v1/power/curtailments-quota-overrides"), "expect": _quota},
        {"id": "04-sla-notice", "title_ko": "SLA 예외 · 상태 공지", "title_en": "SLA exception · notice",
         "call": http("GET", lambda c: f"/api/v1/power/curtailments/{c['cid']}"), "expect": _detail},
        {"id": "05-end", "title_ko": "감축 종료 · 원복 · 정산", "title_en": "End · restore · settle",
         "call": http("POST", lambda c: f"/api/v1/power/curtailments/{c['cid']}/end"),
         "expect": _restored},
        {"id": "06-settlements", "title_ko": "수요반응 정산 기록", "title_en": "DR settlement records",
         "call": http("GET", "/api/v1/power/curtailments-settlements"), "expect": _settlements},
    ],
}
