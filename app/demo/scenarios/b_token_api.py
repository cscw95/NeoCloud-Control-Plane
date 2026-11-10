"""(b) Phase-1 DoD chain b / US-05·06 — OpenAI-compatible token API: streaming
chat completion, 429 on the TPM/RPM quota, content-guard block, usage
reconciliation (gateway vs engine), and the token line on the invoice.

Contracts: F01 (gateway `/v1/*`), F02 (quotas), F03 (guard policy), F04 (metering
events + reconcile), F05 (invoice). The API key is the tenant key from
POST /api/v1/api-keys (exists on main).
"""

from ..engine import check, http, sse_chunks
from ._common import link, tenant_step

MODEL_FALLBACK = "llama-3.3-70b-instruct"


def _key_saved(res, ctx):
    ctx["api_key"] = res.body["secret"]


def _key_ok(res, ctx):
    return check(res.status == 201 and str(res.body.get("secret", "")).startswith("nc_sk_"),
                 f"API 키 {res.body.get('key_id')} 발급 (비밀값은 1회만 노출)",
                 f"API key {res.body.get('key_id')} issued (secret shown once)")


def _bearer(ctx):
    return {"Authorization": f"Bearer {ctx['api_key']}"}


def _models_ok(res, ctx):
    data = res.body.get("data") if isinstance(res.body, dict) else None
    return check(data, f"모델 {len(data or [])}개: " + ", ".join(m.get("id", "?") for m in (data or [])[:3]),
                 f"{len(data or [])} models: " + ", ".join(m.get("id", "?") for m in (data or [])[:3]))


def _save_model(res, ctx):
    ctx["model"] = res.body["data"][0]["id"]


def _stream_ok(res, ctx):
    chunks = sse_chunks(res.text)
    objs = [c for c in chunks if isinstance(c, dict)]
    usage = next((c.get("usage") for c in reversed(objs) if c.get("usage")), None)
    done = "[DONE]" in chunks
    ok = res.status == 200 and "text/event-stream" in res.headers.get("content-type", "") \
        and usage and done
    return check(ok, f"SSE 청크 {len(objs)}개 · 마지막 usage {usage} · [DONE]={done}",
                 f"{len(objs)} SSE chunks · final usage {usage} · [DONE]={done}")


def _save_stream(res, ctx):
    ctx["request_id"] = res.headers.get("x-request-id", "")


def _chat(stream=False, text="NVL72 랙 하나에 GPU가 몇 개인가요?", max_tokens=64):
    return lambda c: {"model": c.get("model", MODEL_FALLBACK), "stream": stream,
                      "max_tokens": max_tokens, "service_tier": "interactive",
                      "messages": [{"role": "user", "content": text}]}


def _quota_ok(res, ctx):
    return check(res.status in (200, 201), "interactive 등급 TPM 200 · RPM 1로 축소",
                 "interactive tier set to TPM 200 · RPM 1")


def _burst(ctx):
    """Two quick calls — the second must be throttled (internal call, two requests)."""
    from ..engine import Client, Result
    cl = Client()
    spec = {"method": "POST", "path": "/v1/chat/completions", "as": "none",
            "headers": _bearer(ctx), "json": _chat(max_tokens=16)(ctx)}
    first, _ = cl.http(spec)
    second, _ = cl.http(spec)
    body = second.body if isinstance(second.body, dict) else {"raw": second.text}
    return Result(second.status, {"first_status": first.status, **body}, second.headers)


def _limited(res, ctx):
    err = (res.body.get("error") or {}) if isinstance(res.body, dict) else {}
    ok = res.status == 429 and err.get("type") == "rate_limit_exceeded" \
        and res.headers.get("retry-after")
    return check(ok, f"2번째 호출 429 · {err.get('type')} · Retry-After {res.headers.get('retry-after')}s",
                 f"2nd call 429 · {err.get('type')} · Retry-After {res.headers.get('retry-after')}s")


def _guard_ok(res, ctx):
    return check(res.status in (200, 201) and res.body.get("mode") == "enforce",
                 "가드 정책 enforce (PII·차단어)", "guard policy enforce (PII, blocked terms)")


def _quota_restored(res, ctx):
    return check(res.status in (200, 201),
                 f"쿼터 원복 {res.status}", f"quota restored {res.status}")


def _blocked(res, ctx):
    err = (res.body.get("error") or {}) if isinstance(res.body, dict) else {}
    return check(res.status == 400 and err.get("type") == "content_filter",
                 f"입력 가드 차단 400 · stage={err.get('stage')}",
                 f"input guard blocked 400 · stage={err.get('stage')}")


def _events_ok(res, ctx):
    rows = res.body if isinstance(res.body, list) else res.body.get("events", [])
    srcs = sorted({r.get("source") for r in rows})
    return check(rows, f"usage 이벤트 {len(rows)}건 · 원천 {', '.join(srcs)}",
                 f"{len(rows)} usage events · sources {', '.join(srcs)}")


def _recon_ok(res, ctx):
    b = res.body
    return check("diff_pct" in b,
                 f"게이트웨이 {b.get('gateway_tokens')} vs 엔진 {b.get('engine_tokens')} 토큰 · 차이 {b.get('diff_pct')}%",
                 f"gateway {b.get('gateway_tokens')} vs engine {b.get('engine_tokens')} tokens · diff {b.get('diff_pct')}%")


def _invoice_has_tokens(res, ctx):
    inv = [i for i in res.body.get("invoices", []) if i.get("tenant_id") == ctx["tenant_id"]]
    if not inv:
        return check(False, "청구 초안 없음", "no draft invoice")
    ctx["invoice_id"] = inv[0]["id"]
    return check(True, f"청구 초안 {inv[0]['id']} 생성", f"draft {inv[0]['id']} created")


def _token_line(res, ctx):
    lines = [x for x in res.body.get("lines", []) if x.get("metric") == "token"]
    return check(lines, "토큰 라인 " + ", ".join(f"{x.get('token_type', '')} {x.get('quantity')}" for x in lines),
                 "token lines " + ", ".join(f"{x.get('token_type', '')} {x.get('quantity')}" for x in lines))


SCENARIO = {
    "id": "b-token-api",
    "order": 2,
    "feature": "F01/F02/F03/F04/F05",
    "story": "US-05 · US-06",
    "title_ko": "(b) 토큰 API: 스트리밍 · 429 · 가드 → 사용량 대조 → 청구 라인",
    "title_en": "(b) Token API: streaming · 429 · guard → usage reconciliation → invoice line",
    "summary_ko": "OpenAI SDK 그대로 base_url만 바꿔 호출하고, 같은 요청 ID는 한 번만 청구된다 (DoD 체인 b).",
    "summary_en": "Call with the OpenAI SDK by changing base_url; one request id is billed once (DoD chain b).",
    "steps": [
        tenant_step("01-tenant", "demo-b-tokens", tier="k8s_namespace"),
        {"id": "02-api-key", "title_ko": "API 키 발급", "title_en": "Issue an API key",
         "call": http("POST", "/api/v1/api-keys", {"name": "demo-token-key", "scope": "inference"},
                      as_=lambda c: f"tenant:{c['tenant_id']}"),
         "expect": _key_ok, "save": _key_saved,
         "links": [link("customer", "api", "고객 · 토큰 API", "Customer · token API")]},
        {"id": "03-models", "feature": "F01", "requires": ["GET /v1/models"],
         "title_ko": "모델 목록 (GET /v1/models)", "title_en": "List models (GET /v1/models)",
         "call": http("GET", "/v1/models", as_="none", headers=_bearer),
         "expect": _models_ok, "save": _save_model},
        {"id": "04-stream", "feature": "F01", "requires": ["POST /v1/chat/completions"],
         "title_ko": "스트리밍 채팅 완성", "title_en": "Streaming chat completion",
         "narration_ko": "stream=true면 text/event-stream으로 토큰이 오고 마지막 청크에 usage가 온다.",
         "narration_en": "With stream=true tokens arrive as text/event-stream and the last chunk carries usage.",
         "expect_ko": "SSE · 마지막 청크 usage · [DONE]", "expect_en": "SSE · usage in last chunk · [DONE]",
         "call": http("POST", "/v1/chat/completions", _chat(stream=True), as_="none", headers=_bearer),
         "expect": _stream_ok, "save": _save_stream,
         "links": [link("ops", "performance", "운영 · 추론 SLO", "Ops · inference SLO")]},
        {"id": "05-quota", "feature": "F02", "requires": ["PUT /api/v1/token/quotas/{tid}"],
         "title_ko": "쿼터 축소 (TPM/RPM)", "title_en": "Tighten the quota (TPM/RPM)",
         "call": http("PUT", lambda c: f"/api/v1/token/quotas/{c['tenant_id']}",
                      {"tier": "interactive", "tpm": 200, "rpm": 1}),
         "expect": _quota_ok},
        {"id": "06-429", "feature": "F02", "requires": ["POST /v1/chat/completions"],
         "title_ko": "한도 초과 → 429", "title_en": "Over the limit → 429",
         "narration_ko": "같은 분 안의 두 번째 호출은 OpenAI 형식 오류와 Retry-After로 거절된다.",
         "narration_en": "A second call in the same minute is refused with an OpenAI-style error and Retry-After.",
         "call": _burst, "expect": _limited},
        {"id": "06b-quota-restore", "feature": "F02", "requires": ["PUT /api/v1/token/quotas/{tid}"],
         "title_ko": "쿼터 원복", "title_en": "Restore the quota",
         "narration_ko": "가드 시연 전에 한도를 되돌린다 (F02 버킷이 실제로 적용되므로 필요).",
         "narration_en": "Put the limit back before the guard step (the F02 buckets are real).",
         "call": http("PUT", lambda c: f"/api/v1/token/quotas/{c['tenant_id']}",
                      {"tier": "interactive", "tpm": 2000000, "rpm": 600}),
         "expect": _quota_restored},
        {"id": "07-guard-policy", "feature": "F03", "requires": ["PUT /api/v1/token/guard-policy/{tid}"],
         "title_ko": "가드 정책 enforce", "title_en": "Guard policy enforce",
         "call": http("PUT", lambda c: f"/api/v1/token/guard-policy/{c['tenant_id']}",
                      {"mode": "enforce", "categories": ["pii", "blocked_terms"]}),
         "expect": _guard_ok},
        {"id": "08-guard-block", "feature": "F03", "requires": ["POST /v1/chat/completions"],
         "title_ko": "PII 입력 차단", "title_en": "PII input blocked",
         "narration_ko": "주민등록번호 패턴이 든 프롬프트는 엔진에 가기 전에 content_filter로 막힌다.",
         "narration_en": "A prompt with a resident-registration-number pattern is stopped before the engine.",
         "call": http("POST", "/v1/chat/completions",
                      _chat(text="내 주민번호는 900101-1234567 입니다"), as_="none", headers=_bearer),
         "expect": _blocked},
        {"id": "09-usage-events", "feature": "F04", "requires": ["GET /api/v1/metering/events"],
         "title_ko": "usage 이벤트 (멱등)", "title_en": "Usage events (idempotent)",
         "call": http("GET", "/api/v1/metering/events",
                      params=lambda c: {"tenant_id": c["tenant_id"]}),
         "expect": _events_ok,
         "links": [link("ops", "dsx-metering", "운영 · 미터링", "Ops · metering")]},
        {"id": "10-reconcile", "feature": "F04", "requires": ["GET /api/v1/metering/reconcile"],
         "title_ko": "게이트웨이 대 엔진 대조", "title_en": "Gateway vs engine reconciliation",
         "call": http("GET", "/api/v1/metering/reconcile", params=lambda c: {"period": c["period"]}),
         "expect": _recon_ok},
        {"id": "11-invoice-run", "feature": "F05",
         "requires": ["POST /api/v1/billing/invoices:run", "POST /v1/chat/completions"],
         "title_ko": "월 청구 실행", "title_en": "Run monthly billing",
         "call": http("POST", "/api/v1/billing/invoices:run",
                      lambda c: {"period": c["period"], "tenant_id": c["tenant_id"]}),
         "expect": _invoice_has_tokens},
        {"id": "12-token-line", "feature": "F05",
         "requires": ["GET /api/v1/billing/invoices/{id}", "POST /v1/chat/completions"],
         "title_ko": "청구서 토큰 라인", "title_en": "Token line on the invoice",
         "expect_ko": "metric=token 라인 존재 (input/output 별도)",
         "expect_en": "metric=token lines (input/output separate)",
         "call": http("GET", lambda c: f"/api/v1/billing/invoices/{c['invoice_id']}"),
         "expect": _token_line,
         "links": [link("customer", "billing", "고객 · 청구", "Customer · billing")]},
    ],
}

