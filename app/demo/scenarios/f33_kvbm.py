"""F33 demo — KV cache tiers G1 HBM → G2 LPDDR5X → G3 NVMe → G4 VAST, KV hits
from every tier counted as usage.prompt_tokens_details.cached_tokens, and the
cached tokens billed on their own invoice line (₩200 / 1M vs input ₩800 / 1M).

AI-Infra side: /clear_kv_blocks, /dynamo/v1/kv/tiers(/config) (DYN_KVBM_*
keys, Dynamo kvbm-configuration.mdx), /dynamo/v1/generate. Deterministic:
every tier is cleared and shrunk to 150 blocks (16 tokens each) first, so
three 2000-token prefixes walk down the tiers the same way on every run.
Billing side: the cached/uncached split of the three re-requests becomes
gateway-shaped F04 metering events (as app/token_api.py _emit_usage writes
them), scaled ×100,000 requests, then an invoice run for last month.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ._kcc_local import body, check, http

DEP = "dgd-llama-70b"
TENANT = "f33-kv-demo"
REQUESTS = 100_000
SMALL = {"deployment": DEP, "num_gpu_blocks_override": 150,
         "DYN_KVBM_CPU_CACHE_OVERRIDE_NUM_BLOCKS": 150}


def _gen(prefix):
    return http("POST", "/dynamo/v1/generate", service="ai", json={
        "deployment": DEP, "prompt_tokens": 2000, "max_tokens": 8,
        "prefix_hash": f"f33-{prefix}"})


def _stored(res, ctx):
    r = body(res)
    return check(res.status == 200 and r.get("kv_hit_tier") in (None, "G1"),
                 f"새 prefix 저장 (tier={r.get('kv_hit_tier')}, cached_tokens="
                 f"{r.get('usage', {}).get('prompt_tokens_details', {}).get('cached_tokens')})",
                 f"new prefix stored (tier={r.get('kv_hit_tier')})")


def _hit(tier):
    def expect(res, ctx):
        r = body(res)
        c = r["usage"]["prompt_tokens_details"]["cached_tokens"]
        ok = res.status == 200 and r.get("kv_hit_tier") == tier and c == 2000
        return check(ok,
                     f"{tier}에서 KV 적재: cached_tokens {c} / prompt 2000, prefill "
                     f"{r.get('prefill_ms')} ms, 적재 {r.get('kv_onboard_ms')} ms (TTFT 포함)",
                     f"onboarded from {tier}: cached_tokens {c}/2000, prefill "
                     f"{r.get('prefill_ms')} ms, onboard {r.get('kv_onboard_ms')} ms")
    return expect


def _save_usage(res, ctx):
    ctx.setdefault("usages", []).append(body(res)["usage"])


def _tiers(res, ctx):
    t = {x["tier"]: x for x in body(res)["deployments"][0]["tiers"]}
    e = tuple(t[k]["entries"] for k in ("G1", "G2", "G3", "G4"))
    return check(e == (1, 1, 1, 2),
                 f"계층별 prefix 수 G1/G2/G3/G4 = {e} · G1 HBM {t['G1']['unit']['hbm_gb_per_gpu']:.0f}"
                 f" GB/GPU · G4 VAST {t['G4']['vast']['cluster']}:{t['G4']['vast']['view_path']}",
                 f"prefixes per tier G1/G2/G3/G4 = {e} · G4 VAST {t['G4']['vast']['cluster']}")


def _period(ctx):
    now = datetime.now(timezone.utc)
    p0 = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
          - timedelta(days=1)).replace(day=1)
    ctx["period"] = p0.strftime("%Y-%m")
    ctx["ts"] = (p0 + timedelta(days=3, hours=9)).isoformat()
    return ctx["period"]


def _meter(ctx):
    """Gateway → F04 metering events from the three re-request usages."""
    from ...store import STORE
    _period(ctx)
    cached = sum(u["prompt_tokens_details"]["cached_tokens"] for u in ctx["usages"])
    prompt = sum(u["prompt_tokens"] for u in ctx["usages"])
    parts = {"input": (prompt - cached) * REQUESTS, "cached": cached * REQUESTS}
    evs = [{"idempotency_key": f"gw:f33-demo:{tt}", "source": "gateway",
            "tenant_id": ctx["tenant_id"], "product": "token-api", "metric": "token",
            "quantity": q, "unit": "tokens", "ts": ctx["ts"],
            "attrs": {"model": "llama-3.3-70b-instruct", "token_type": tt,
                      "tier": "interactive", "request_id": "f33-demo"}}
           for tt, q in parts.items() if q > 0]
    cur = getattr(STORE, "metering_events", None)
    if isinstance(cur, dict):
        for e in evs:
            cur[e["idempotency_key"]] = e
    else:
        cur = [e for e in (cur or []) if not str(e.get("idempotency_key", "")
                                                ).startswith("gw:f33-demo:")]
        STORE.metering_events = cur + evs
    return {"tenant_id": ctx["tenant_id"], "period": ctx["period"],
            "events": [{"token_type": e["attrs"]["token_type"], "quantity": e["quantity"]}
                       for e in evs]}


def _metered(res, ctx):
    q = {e["token_type"]: e["quantity"] for e in body(res)["events"]}
    return check(q.get("cached", 0) > 0,
                 f"F04 이벤트: cached {q.get('cached', 0):,} 토큰 · input {q.get('input', 0):,}"
                 f" 토큰 (×{REQUESTS:,} 요청)",
                 f"F04 events: cached {q.get('cached', 0):,} · input {q.get('input', 0):,}"
                 f" tokens (×{REQUESTS:,} requests)")


def _invoice(res, ctx):
    lines = [ln for ln in body(res).get("lines", []) if ln.get("metric") == "token"]
    by = {ln["token_type"]: ln for ln in lines}
    c = by.get("cached") or {}
    i = by.get("input") or {}
    return check(c.get("unit_price") == 200 and c.get("amount", 0) > 0 and "input" in by,
                 f"청구서 별도 행: cached {c.get('quantity', 0):,.0f} 토큰 × ₩200/1M = "
                 f"₩{c.get('amount', 0):,} · input {i.get('quantity', 0):,.0f} 토큰 × ₩800/1M = "
                 f"₩{i.get('amount', 0):,}",
                 f"separate invoice line: cached {c.get('quantity', 0):,.0f} tok × ₩200/1M = "
                 f"₩{c.get('amount', 0):,} · input ₩{i.get('amount', 0):,}")


SCENARIO = {
    "id": "f33-kv-tiers",
    "order": 33,
    "feature": "F33",
    "title_ko": "F33 KV 캐시 계층 G1–G4(HBM·LPDDR5X·NVMe·VAST) → cached_tokens → 청구서 별도 행",
    "title_en": "F33 KV cache tiers G1–G4 (HBM·LPDDR5X·NVMe·VAST) → cached_tokens → invoice line",
    "summary_ko": "작게 줄인 계층에서 prefix가 G1→G2→G3로 내려가고 G4(VAST)에 복제된다. "
                  "다시 요청하면 각 계층에서 적재되어 cached_tokens 2000으로 집계되고 "
                  "청구서에 ₩200/1M 단가의 cached 행이 따로 붙는다.",
    "summary_en": "Prefixes walk G1→G2→G3 with a G4 (VAST) copy; re-requests onboard "
                  "from each tier as cached_tokens 2000 and bill on a ₩200/1M line.",
    "steps": [
        {"id": "reset", "title_ko": "KV 전 계층 비우기", "title_en": "Clear every KV tier",
         "call": http("POST", "/clear_kv_blocks", service="ai")},
        {"id": "config", "title_ko": "시연용 계층 크기: G1·G2 = 150 블록",
         "title_en": "Demo tier sizes: G1 and G2 = 150 blocks",
         "call": http("PUT", "/dynamo/v1/kv/tiers/config", service="ai", json=SMALL)},
        {"id": "p1", "title_ko": "prefix p1 (2000 토큰)", "title_en": "prefix p1 (2000 tokens)",
         "call": _gen("p1"), "expect": _stored},
        {"id": "p2", "title_ko": "prefix p2 → p1이 G2·G4로 내려감",
         "title_en": "prefix p2 → p1 offloads to G2 and G4", "call": _gen("p2"),
         "expect": _stored},
        {"id": "p3", "title_ko": "prefix p3 → p1이 G3로, p2가 G2로",
         "title_en": "prefix p3 → p1 to G3, p2 to G2", "call": _gen("p3"), "expect": _stored},
        {"id": "tiers", "title_ko": "계층 상태 조회", "title_en": "Tier occupancy",
         "call": http("GET", "/dynamo/v1/kv/tiers", service="ai", params={"deployment": DEP}),
         "expect": _tiers},
        {"id": "hit-g2", "title_ko": "p2 재요청 → G2(LPDDR5X)에서 적재",
         "title_en": "re-request p2 → onboard from G2", "call": _gen("p2"),
         "expect": _hit("G2"), "save": _save_usage},
        {"id": "hit-g3", "title_ko": "p1 재요청 → G3(NVMe)에서 적재",
         "title_en": "re-request p1 → onboard from G3", "call": _gen("p1"),
         "expect": _hit("G3"), "save": _save_usage},
        {"id": "g2g3-off", "title_ko": "G2·G3 끄기 (G4 VAST만 남김)",
         "title_en": "Disable G2/G3 (only G4 VAST left)",
         "call": http("PUT", "/dynamo/v1/kv/tiers/config", service="ai", json={
             **SMALL, "DYN_KVBM_CPU_CACHE_OVERRIDE_NUM_BLOCKS": 0,
             "DYN_KVBM_DISK_CACHE_OVERRIDE_NUM_BLOCKS": 0})},
        {"id": "hit-g4", "title_ko": "p3 재요청 → G4(VAST)에서 적재",
         "title_en": "re-request p3 → onboard from G4 (VAST)", "call": _gen("p3"),
         "expect": _hit("G4"), "save": _save_usage},
        {"id": "restore", "title_ko": "계층 설정 원복", "title_en": "Restore tier config",
         "call": http("PUT", "/dynamo/v1/kv/tiers/config", service="ai", json={})},
        {"id": "tenant", "title_ko": "청구 테넌트", "title_en": "Billing tenant",
         "call": http("POST", "/api/v1/tenants", json={
             "name": TENANT, "isolation_tier": "bare_metal_dedicated"}),
         "expect": {"status": [201, 409]},
         "save": lambda res, ctx: ctx.update(tenant_id=body(res).get("id")
                                             or f"tnt-{TENANT}")},
        {"id": "meter", "title_ko": "사용량 → F04 미터링 이벤트(cached 분리)",
         "title_en": "Usage → F04 metering events (cached split)",
         "call": _meter, "expect": _metered},
        {"id": "invoice-run", "title_ko": "지난달 청구서 실행", "title_en": "Run last month's invoice",
         "call": http("POST", "/api/v1/billing/invoices:run",
                      json={"period": lambda c: c["period"],
                            "tenant_id": lambda c: c["tenant_id"]}),
         "save": lambda res, ctx: ctx.update(invoice_id=body(res)["invoices"][0]["id"])},
        {"id": "invoice", "title_ko": "청구서의 cached 토큰 별도 행",
         "title_en": "Cached-token line on the invoice",
         "call": http("GET", lambda c: f"/api/v1/billing/invoices/{c['invoice_id']}"),
         "expect": _invoice},
    ],
}
