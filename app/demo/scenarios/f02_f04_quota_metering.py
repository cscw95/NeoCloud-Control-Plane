"""F02 quotas + F04 metering demo — TPM/RPM buckets, 429, override, usage
events with idempotency keys, engine sync, gateway↔engine reconciliation,
NICo hour events, usage token lines.

Deterministic: fixed tenant, fixed idempotency keys and request ids, fixed
NICo timestamps. Re-runs are safe because the metering log drops repeated
keys (first write wins) and PUT quota restarts the bucket full. The gateway
429 step needs F01 (/v1/chat/completions); without it the step is pending.

Expected numbers (every run):
  quota batch tpm 6000 rpm 6 → override factor 0.3 → effective tpm 1800 rpm 1
  gateway 3 requests 1,000 tokens, engine 2 requests 820 tokens (req-f04-3
  missing at the engine, req-f04-2 +20 reasoning) → diff 18% → warning
  NICo 2 h 30 m on a 4-GPU tray → instance_hour 2.5, gpu_hour 10.0

Format: the demo runner (app/demo/engine.py, feat/v2-demo). Without the
runner scripts/demo_quota_meter.py runs the same SCENARIO with run_local().
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

try:                                      # demo runner (v2-demo)
    from ..engine import check, http
except ImportError:                       # runner not merged yet: same helpers
    def http(method, path, json=None, params=None, as_="operator",
             service="nocp", headers=None):
        spec = {"method": method, "path": path, "as": as_, "service": service}
        if headers is not None:
            spec["headers"] = headers
        if json is not None:
            spec["json"] = json
        if params is not None:
            spec["params"] = params
        return spec

    def check(cond, ko, en):
        return bool(cond), ko, en

TENANT = "f02-f04-demo"
TID = f"tnt-{TENANT}"
MODEL = "llama-3.3-70b-instruct"
PERIOD = datetime.now(timezone.utc).strftime("%Y-%m")
TS = datetime.now(timezone.utc).replace(day=1, hour=9, minute=0, second=0,
                                        microsecond=0).isoformat()

# gateway usage per request (token_type → tokens) and what the engine reported
GW = {"req-f04-1": {"input": 300, "cached": 100, "output": 100},
      "req-f04-2": {"input": 200, "output": 100},
      "req-f04-3": {"input": 150, "output": 50}}
ENGINE = [{"seq": 1, "request_id": "req-f04-1", "tenant_id": TID, "model": MODEL,
           "deployment": "dgd-demo", "at": TS,
           "usage": {"prompt_tokens": 400, "completion_tokens": 100, "total_tokens": 500,
                     "prompt_tokens_details": {"cached_tokens": 100},
                     "completion_tokens_details": {"reasoning_tokens": 0}}},
          {"seq": 2, "request_id": "req-f04-2", "tenant_id": TID, "model": MODEL,
           "deployment": "dgd-demo", "at": TS,
           "usage": {"prompt_tokens": 200, "completion_tokens": 120, "total_tokens": 320,
                     "prompt_tokens_details": {"cached_tokens": 0},
                     "completion_tokens_details": {"reasoning_tokens": 20}}}]


def _gw_events() -> list[dict]:
    return [{"idempotency_key": f"gw:{rid}:{tt}", "source": "gateway", "tenant_id": TID,
             "product": "token-api", "metric": "token", "quantity": q, "unit": "tokens",
             "ts": TS, "attrs": {"model": MODEL, "token_type": tt, "tier": "batch",
                                 "request_id": rid}}
            for rid, parts in GW.items() for tt, q in parts.items()]


def _until(ctx):
    return datetime.fromtimestamp(time.time() + 900, timezone.utc).isoformat(timespec="seconds")


def _rid(ctx, n):
    ctx.setdefault("run", str(int(time.time() * 1000)))
    return f"demo-f02-{ctx['run']}-{n}"


# -- expectations -------------------------------------------------------------
def _tenant_ok(res, ctx):
    return check(res.status in (201, 409), f"테넌트 {TID} 준비 (HTTP {res.status})",
                 f"tenant {TID} ready (HTTP {res.status})")


def _put_ok(res, ctx):
    b = res.body
    return check(res.status == 200 and b.get("tpm") == 6000 and b.get("rpm") == 6,
                 f"batch 등급 TPM {b.get('tpm')} · RPM {b.get('rpm')} · 월 한도 {b.get('monthly_token_cap')}",
                 f"batch tier TPM {b.get('tpm')} · RPM {b.get('rpm')} · monthly cap {b.get('monthly_token_cap')}")


def _override_ok(res, ctx):
    e = res.body.get("effective", {})
    return check(res.status == 201 and e.get("tpm") == 1800 and e.get("rpm") == 1,
                 f"감축 override ×{e.get('factor')} → 유효 TPM {e.get('tpm')} · RPM {e.get('rpm')} "
                 f"(만료 {res.body.get('override', {}).get('until')})",
                 f"curtailment override ×{e.get('factor')} → effective TPM {e.get('tpm')} · "
                 f"RPM {e.get('rpm')} (until {res.body.get('override', {}).get('until')})")


def _key_ok(res, ctx):
    return check(res.status == 201 and res.body.get("key"), f"API 키 {res.body.get('id')} 발급",
                 f"API key {res.body.get('id')} issued")


def _chat1_ok(res, ctx):
    return check(res.status == 200, f"첫 batch 요청 통과 · 남은 요청 "
                                    f"{res.headers.get('x-ratelimit-remaining-requests')}",
                 f"first batch call admitted · remaining requests "
                 f"{res.headers.get('x-ratelimit-remaining-requests')}")


def _chat2_429(res, ctx):
    err = (res.body or {}).get("error", {}) if isinstance(res.body, dict) else {}
    ra = res.headers.get("retry-after")
    return check(res.status == 429 and err.get("type") == "rate_limit_exceeded" and ra,
                 f"두 번째 요청 429 {err.get('code')} · Retry-After {ra}s · "
                 f"reset-requests {res.headers.get('x-ratelimit-reset-requests')}",
                 f"second call 429 {err.get('code')} · Retry-After {ra}s · "
                 f"reset-requests {res.headers.get('x-ratelimit-reset-requests')}")


def _clear_ok(res, ctx):
    return check(res.status == 200 and res.body.get("override") is None,
                 f"override 해제 → TPM {res.body.get('effective', {}).get('tpm')} 복원",
                 f"override cleared → TPM {res.body.get('effective', {}).get('tpm')} restored")


def _batch_ok(res, ctx):
    b = res.body
    n = b.get("accepted", 0) + b.get("duplicates", 0)
    return check(res.status == 200 and n == 7 and b.get("rejected") == 0,
                 f"게이트웨이 usage 이벤트 7건 (신규 {b.get('accepted')} · 중복 {b.get('duplicates')})",
                 f"7 gateway usage events (new {b.get('accepted')} · duplicate {b.get('duplicates')})")


def _dup_ok(res, ctx):
    return check(res.status == 200 and res.body.get("duplicate") is True,
                 "같은 멱등 키 재전송 → 200 duplicate (1건만 보관)",
                 "same idempotency key again → 200 duplicate (stored once)")


def _engine_ok(res, ctx):
    b = res.body
    return check(res.status == 200 and b.get("ingested", 0) + b.get("duplicates", 0) == 6,
                 f"엔진 usage 레코드 {b.get('records')}건 → 이벤트 6건 (커서 {b.get('cursor')})",
                 f"{b.get('records')} engine usage records → 6 events (cursor {b.get('cursor')})")


def _recon_ok(res, ctx):
    b = res.body
    ok = (b.get("gateway_tokens") == 1000 and b.get("engine_tokens") == 820
          and b.get("mismatches") == ["req-f04-2", "req-f04-3"] and b.get("status") == "warning")
    return check(ok, f"게이트웨이 {b.get('gateway_tokens')} vs 엔진 {b.get('engine_tokens')} 토큰 · "
                     f"차이 {b.get('diff_pct')}% > {b.get('threshold_pct')}% 경고 · 불일치 {b.get('mismatches')}",
                 f"gateway {b.get('gateway_tokens')} vs engine {b.get('engine_tokens')} tokens · "
                 f"diff {b.get('diff_pct')}% > {b.get('threshold_pct')}% warning · mismatches {b.get('mismatches')}")


def _nico_open(res, ctx):
    return check(res.status == 200 and res.body.get("action") in ("opened", "noop"),
                 f"NICo assigned/ready → 할당 시작 ({res.body.get('action')})",
                 f"NICo assigned/ready → allocation opened ({res.body.get('action')})")


def _nico_close(res, ctx):
    q = {e["metric"]: e["quantity"] for e in res.body.get("events", [])}
    return check(res.status == 200 and q == {"instance_hour": 2.5, "gpu_hour": 10.0},
                 f"NICo waitingforcleanup → instance_hour {q.get('instance_hour')} · gpu_hour {q.get('gpu_hour')}",
                 f"NICo waitingforcleanup → instance_hour {q.get('instance_hour')} · gpu_hour {q.get('gpu_hour')}")


def _usage_ok(res, ctx):
    t = res.body.get("tokens_by_type", {})
    return check(res.status == 200 and t.get("input") == 650 and t.get("output") == 250,
                 f"/metering/usage tokens_by_type {t} (청구 원천 = 게이트웨이)",
                 f"/metering/usage tokens_by_type {t} (billed source = gateway)")


def _ce_ok(res, ctx):
    d = res.body.get("data", [])
    return check(res.status == 200 and d and d[0].get("specversion") == "1.0",
                 f"CloudEvents 1.0 봉투 {len(d)}건 (type {d[0].get('type') if d else '-'})",
                 f"{len(d)} CloudEvents 1.0 envelopes (type {d[0].get('type') if d else '-'})")


def _link(route, ko, en):
    return [{"console": "ops", "route": route, "label_ko": ko, "label_en": en}]


SCENARIO = {
    "id": "f02-f04-quota-metering",
    "order": 12,
    "feature": "F02",
    "title_ko": "(F02·F04) 토큰 쿼터 · 계측 대조",
    "title_en": "(F02·F04) Token quotas · metering reconciliation",
    "summary_ko": "TPM/RPM 버킷 → 감축 override → 429 → usage 이벤트 멱등 → 엔진 대조 → NICo 시간 → 청구 원천.",
    "summary_en": "TPM/RPM buckets → curtailment override → 429 → idempotent usage events → engine "
                  "reconciliation → NICo hours → billing source.",
    "steps": [
        {"id": "01-tenant", "title_ko": "데모 테넌트", "title_en": "Demo tenant",
         "call": http("POST", "/api/v1/tenants",
                      {"name": TENANT, "isolation_tier": "bare_metal_dedicated"}),
         "expect": _tenant_ok},
        {"id": "02-quota", "feature": "F02", "title_ko": "batch 등급 쿼터 설정",
         "title_en": "Set batch-tier quota",
         "narration_ko": "테넌트·등급별 토큰 버킷. 용량 = 분당 한도, 연속 리필.",
         "narration_en": "Token bucket per tenant × tier; capacity = per-minute limit, continuous refill.",
         "call": http("PUT", f"/api/v1/token/quotas/{TID}",
                      {"tier": "batch", "tpm": 6000, "rpm": 6, "monthly_token_cap": 10_000_000}),
         "expect": _put_ok, "links": _link("token-quotas", "운영 · 쿼터", "Ops · quotas")},
        {"id": "03-override", "feature": "F02", "title_ko": "감축 override ×0.3 (F21 경로)",
         "title_en": "Curtailment override ×0.3 (F21 path)",
         "call": http("POST", f"/api/v1/token/quotas/{TID}/override",
                      lambda c: {"tier": "batch", "factor": 0.3, "until": _until(c),
                                 "reason": "demo: grid load target"}),
         "expect": _override_ok},
        {"id": "04-key", "feature": "F01", "title_ko": "API 키 발급", "title_en": "Issue API key",
         "requires": ["POST /api/v1/token/keys"],
         "call": http("POST", "/api/v1/token/keys", {"tenant_id": TID, "name": "f02-demo"}),
         "expect": _key_ok, "save": lambda r, c: c.update(api_key=r.body["key"])},
        {"id": "05-chat-ok", "feature": "F01", "title_ko": "batch 요청 1 (통과)",
         "title_en": "Batch call 1 (admitted)", "requires": ["POST /v1/chat/completions"],
         "call": http("POST", "/v1/chat/completions",
                      {"model": MODEL, "max_tokens": 8, "service_tier": "batch",
                       "messages": [{"role": "user", "content": "hello"}]},
                      as_="none", headers=lambda c: {"Authorization": f"Bearer {c['api_key']}",
                                                     "x-request-id": _rid(c, 1)}),
         "expect": _chat1_ok},
        {"id": "06-chat-429", "feature": "F02", "title_ko": "batch 요청 2 → 429",
         "title_en": "Batch call 2 → 429", "requires": ["POST /v1/chat/completions"],
         "narration_ko": "유효 RPM 1이라 두 번째 요청은 OpenAI 형식 429 + Retry-After.",
         "narration_en": "Effective RPM is 1, so the second call gets an OpenAI-style 429 + Retry-After.",
         "call": http("POST", "/v1/chat/completions",
                      {"model": MODEL, "max_tokens": 8, "service_tier": "batch",
                       "messages": [{"role": "user", "content": "hello again"}]},
                      as_="none", headers=lambda c: {"Authorization": f"Bearer {c['api_key']}",
                                                     "x-request-id": _rid(c, 2)}),
         "expect": _chat2_429},
        {"id": "07-clear", "feature": "F02", "title_ko": "override 해제 (복원)",
         "title_en": "Clear override (restore)",
         "call": http("DELETE", f"/api/v1/token/quotas/{TID}/override", params={"tier": "batch"}),
         "expect": _clear_ok},
        {"id": "08-events", "feature": "F04", "title_ko": "게이트웨이 usage 이벤트 (토큰 유형별)",
         "title_en": "Gateway usage events (per token type)",
         "narration_ko": "멱등 키 gw:<x-request-id>:<token_type>. input은 cached 제외, output은 reasoning 제외.",
         "narration_en": "Idempotency key gw:<x-request-id>:<token_type>; input excludes cached, "
                         "output excludes reasoning.",
         "call": http("POST", "/api/v1/metering/events/batch", {"events": _gw_events()}),
         "expect": _batch_ok, "links": _link("dsx-metering", "운영 · 미터링", "Ops · metering")},
        {"id": "09-duplicate", "feature": "F04", "title_ko": "같은 키 재전송 → 중복",
         "title_en": "Same key again → duplicate",
         "call": http("POST", "/api/v1/metering/events", _gw_events()[0]), "expect": _dup_ok},
        {"id": "10-engine", "feature": "F04", "title_ko": "엔진 usage 수집 (AI-Infra /dynamo/v1/usage 형식)",
         "title_en": "Engine usage ingest (AI-Infra /dynamo/v1/usage shape)",
         "call": http("POST", "/api/v1/metering/engine/sync", {"records": ENGINE}),
         "expect": _engine_ok},
        {"id": "11-reconcile", "feature": "F04", "title_ko": "게이트웨이 ↔ 엔진 대조",
         "title_en": "Gateway ↔ engine reconciliation",
         "call": http("GET", "/api/v1/metering/reconcile",
                      params={"period": PERIOD, "tenant_id": TID}),
         "expect": _recon_ok},
        {"id": "12-nico-open", "feature": "F04", "title_ko": "NICo 상태 이벤트: 할당 시작",
         "title_en": "NICo state event: allocation start",
         "call": http("POST", "/api/v1/metering/nico/state",
                      {"machine_id": "demo-f04-tray-1", "timestamp": "2026-11-02T00:00:00Z",
                       "managed_host_state": {"state": "assigned",
                                              "instance_state": {"state": "ready"}},
                       "x-neocloud": {"tenant_id": TID, "instance_id": "inst-demo-f04",
                                      "gpus": 4, "rack_id": "rk-demo"}}),
         "expect": _nico_open},
        {"id": "13-nico-close", "feature": "F04", "title_ko": "NICo 상태 이벤트: 정리 → 시간 이벤트",
         "title_en": "NICo state event: cleanup → hour events",
         "call": http("POST", "/api/v1/metering/nico/state",
                      {"machine_id": "demo-f04-tray-1", "timestamp": "2026-11-02T02:30:00Z",
                       "managed_host_state": {"state": "waitingforcleanup",
                                              "cleanup_state": {"state": "hostcleanup"}}}),
         "expect": _nico_close},
        {"id": "14-usage", "feature": "F04", "title_ko": "/metering/usage 토큰 라인",
         "title_en": "/metering/usage token lines",
         "call": http("GET", "/api/v1/metering/usage", params={"tenant_id": TID}),
         "expect": _usage_ok},
        {"id": "15-cloudevents", "feature": "F04", "title_ko": "CloudEvents 1.0 봉투 보기",
         "title_en": "CloudEvents 1.0 envelopes",
         "call": http("GET", "/api/v1/metering/events",
                      params={"tenant_id": TID, "format": "cloudevents", "limit": 3}),
         "expect": _ce_ok},
    ],
}


# ---------------------------------------------------------------------------
# runner-less execution (scripts/demo_quota_meter.py, tests)
# ---------------------------------------------------------------------------
class _Res:
    def __init__(self, status, body, headers=None):
        self.status, self.body = status, body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


def _resolve(v, ctx):
    if callable(v):
        return _resolve(v(ctx), ctx)
    if isinstance(v, dict):
        return {k: _resolve(x, ctx) for k, x in v.items()}
    return v


def run_local(send, out=print, lang="ko") -> bool:
    """send(method, path, json, params, headers) -> (status, headers, body).
    Prints a curl line, the response and the verdict per step. A step whose
    route is missing on this build (404 Not Found) is reported pending."""
    ctx: dict = {}
    ok_all = True
    steps = SCENARIO["steps"]
    for i, st in enumerate(steps, 1):
        out(f"\n[{i:02d}/{len(steps)}] {st['title_ko' if lang == 'ko' else 'title_en']}")
        try:
            spec = _resolve(st["call"], ctx)
        except KeyError as e:
            out(f"  … pending (needs {e} from an earlier step)")
            continue
        m, path = spec["method"], spec["path"]
        body, params, hdr = spec.get("json"), spec.get("params"), spec.get("headers") or {}
        q = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
        hs = "".join(f" -H '{k}: {'Bearer …' if k.lower() == 'authorization' else v}'"
                     for k, v in hdr.items())
        out(f"  $ curl -si -X {m} '$NOCP{path}{q}'{hs}"
            + (f" -H 'Content-Type: application/json' -d '{json.dumps(body, ensure_ascii=False)}'"
               if body is not None else ""))
        status, rh, rbody = send(m, path, body, params, hdr)
        res = _Res(status, rbody, rh)
        shown = {k: v for k, v in res.headers.items()
                 if k.startswith("x-ratelimit") or k == "retry-after"}
        text = json.dumps(rbody, ensure_ascii=False)
        out(f"  ← HTTP {status} {json.dumps(shown) if shown else ''} {text[:300]}"
            f"{'…' if len(text) > 300 else ''}")
        if status == 404 and rbody == {"detail": "Not Found"} and st.get("requires"):
            out(f"  … pending ({st.get('feature')} not in this build)")
            continue
        try:
            ok, ko, en = st["expect"](res, ctx)
        except (KeyError, TypeError, IndexError, AttributeError) as e:
            ok, ko, en = False, f"검증 오류 {e!r}", f"check error {e!r}"
        out(f"  {'✔' if ok else '✘'} {ko if lang == 'ko' else en}")
        if ok and st.get("save"):
            st["save"](res, ctx)
        ok_all &= bool(ok)
    return ok_all
