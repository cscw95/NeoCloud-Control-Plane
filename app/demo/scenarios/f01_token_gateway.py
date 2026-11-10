"""F01 token gateway + F03 safety guard — focused demo (engine format: app/demo/engine.py).

OpenAI-compatible /v1 in front of the AI-Infra Dynamo emulator (stub engine when
AI-Infra is down): key → models → chat (engine, TTFT/TPOT, KV hit) → SSE with usage
in the last chunk → same x-request-id replays without new usage → input PII block →
output guard cuts a stream → public-sector tenant locked to enforce → SLO vs deck.
Deterministic: fixed request ids, the engine's token text is a crc32 of
request id + index, so every run shows the same output.
"""

import zlib

from ..engine import check, http, sse_chunks
from ._common import link, tenant_step

MODEL = "llama-3.3-70b-instruct"
_WORDS = ("the", "rack", "power", "token", "model", "cluster", "GPU", "fast",
          "serving", "NeoCloud", "inference", "stream", "data", "is", "ready",
          "and", "decode", "prefill", "cache", "efficient")
CUT_RID = "demo-f03-cut"


def _cut_term() -> str:
    """First word of the CUT_RID stream that appears at index ≥ 3 for the first time."""
    words = [_WORDS[zlib.crc32(f"{CUT_RID}:{i}".encode()) % len(_WORDS)] for i in range(40)]
    return next(w for i, w in enumerate(words) if i >= 3 and w not in words[:i])


def _bearer(ctx, rid=None):
    h = {"Authorization": f"Bearer {ctx['api_key']}"}
    if rid:
        h["x-request-id"] = rid
    return h


def _chat(text="NVL72 랙 하나에 GPU가 몇 개인가요?", stream=False, max_tokens=16):
    return {"model": MODEL, "stream": stream, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": "NeoCloud 안내 봇"},
                         {"role": "user", "content": text}]}


def _key_ok(res, ctx):
    k = res.body.get("key", "") if isinstance(res.body, dict) else ""
    return check(res.status == 201 and k.startswith("sk-neo-"),
                 f"API 키 {res.body.get('id')} 발급 · {res.body.get('prefix')} (비밀값 1회 노출, 해시 저장)",
                 f"API key {res.body.get('id')} issued · {res.body.get('prefix')} (secret once, stored hashed)")


def _models_ok(res, ctx):
    ids = [m["id"] for m in res.body.get("data", [])]
    return check(res.status == 200 and MODEL in ids,
                 f"모델 {len(ids)}개 ({res.headers.get('x-neocloud-engine')}) : {', '.join(ids)}",
                 f"{len(ids)} models ({res.headers.get('x-neocloud-engine')}): {', '.join(ids)}")


def _chat_ok(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    nc, u = b.get("neocloud", {}), b.get("usage", {})
    return check(res.status == 200 and b.get("object") == "chat.completion",
                 f"엔진 {nc.get('engine')} · {nc.get('deployment')} · TTFT {nc.get('ttft_ms')} ms · "
                 f"TPOT {nc.get('tpot_ms')} ms · usage 입력 {u.get('prompt_tokens')}(캐시 {u.get('cached_tokens')}) "
                 f"출력 {u.get('completion_tokens')}",
                 f"engine {nc.get('engine')} · {nc.get('deployment')} · TTFT {nc.get('ttft_ms')} ms · "
                 f"TPOT {nc.get('tpot_ms')} ms · usage in {u.get('prompt_tokens')} (cached "
                 f"{u.get('cached_tokens')}) out {u.get('completion_tokens')}")


def _stream_ok(res, ctx):
    ch = sse_chunks(res.text)
    objs = [c for c in ch if isinstance(c, dict)]
    last = objs[-1] if objs else {}
    ok = (res.status == 200 and ch[-1:] == ["[DONE]"] and last.get("choices") == []
          and last.get("usage"))
    return check(ok, f"SSE 청크 {len(objs)}개 → 마지막 청크 usage {last.get('usage', {}).get('total_tokens')} 토큰 · [DONE]",
                 f"{len(objs)} SSE chunks → last chunk usage {last.get('usage', {}).get('total_tokens')} tokens · [DONE]")


def _replay_ok(res, ctx):
    return check(res.status == 200 and res.headers.get("x-idempotent-replay") == "true",
                 "같은 x-request-id 재시도 → 저장된 응답 재생 (x-idempotent-replay: true)",
                 "same x-request-id retried → stored answer replayed (x-idempotent-replay: true)")


def _usage_once(res, ctx):
    n = sum(1 for r in res.body.get("records", []) if r["request_id"] == "demo-f01-retry")
    return check(n == 1, f"request demo-f01-retry 의 usage 기록 {n}건 (2회 호출, 1회 청구)",
                 f"usage records for demo-f01-retry: {n} (2 calls, billed once)")


def _policy_ok(res, ctx):
    return check(res.status == 200 and res.body.get("mode") == "enforce",
                 f"가드 enforce · 차단어 {res.body.get('blocked_terms')}",
                 f"guard enforce · blocked terms {res.body.get('blocked_terms')}")


def _in_blocked(res, ctx):
    e = (res.body or {}).get("error", {}) if isinstance(res.body, dict) else {}
    return check(res.status == 400 and e.get("type") == "content_filter" and e.get("stage") == "input",
                 f"입력 가드 400 content_filter · stage={e.get('stage')} · {e.get('categories')}",
                 f"input guard 400 content_filter · stage={e.get('stage')} · {e.get('categories')}")


def _out_cut(res, ctx):
    ch = [c for c in sse_chunks(res.text) if isinstance(c, dict)]
    fin = next((c["choices"][0].get("finish_reason") for c in reversed(ch)
                if c.get("choices") and c["choices"][0].get("finish_reason")), None)
    used = ch[-1].get("usage", {}).get("completion_tokens") if ch else None
    return check(res.status == 200 and fin == "content_filter",
                 f"출력 가드가 스트림 중간에서 끊음 · finish_reason={fin} · 전달 토큰 {used}/40",
                 f"output guard cut the stream midway · finish_reason={fin} · {used}/40 tokens delivered")


def _public_locked(res, ctx):
    return check(res.status == 409, f"공공 테넌트 정책 완화 거부 (HTTP {res.status})",
                 f"public-sector policy relax refused (HTTP {res.status})")


def _slo_ok(res, ctx):
    b = res.body
    return check(res.status == 200 and "breach" in b,
                 f"TTFT p99 {b.get('ttft_p99_ms')} ms / TPOT p99 {b.get('tpot_p99_ms')} ms · 목표 2000/50 · "
                 f"위반 {b.get('breach')} ({b.get('source')})",
                 f"TTFT p99 {b.get('ttft_p99_ms')} ms / TPOT p99 {b.get('tpot_p99_ms')} ms · target 2000/50 · "
                 f"breach {b.get('breach')} ({b.get('source')})")


SCENARIO = {
    "id": "f01-token-gateway",
    "order": 21,
    "feature": "F01/F03",
    "story": "US-05",
    "title_ko": "F01·F03 토큰 게이트웨이: OpenAI 호환 · Dynamo 라우팅 · 멱등 · 입력/출력 가드",
    "title_en": "F01·F03 token gateway: OpenAI-compatible · Dynamo routing · idempotency · I/O guard",
    "summary_ko": "base_url만 바꾼 OpenAI 호출이 Dynamo 엔진으로 가고, 같은 요청 ID는 1회 청구, 가드는 입력과 스트림 출력을 막는다.",
    "summary_en": "An OpenAI call with only base_url changed reaches the Dynamo engine; one request id bills once; the guard stops input and streamed output.",
    "steps": [
        tenant_step("01-tenant", "demo-f01-gw", tier="k8s_namespace"),
        {"id": "02-key", "feature": "F01", "requires": ["POST /api/v1/token/keys"],
         "title_ko": "게이트웨이 API 키 발급", "title_en": "Issue a gateway API key",
         "call": http("POST", "/api/v1/token/keys", lambda c: {"tenant_id": c["tenant_id"],
                                                               "name": "demo"}),
         "expect": _key_ok, "save": lambda r, c: c.__setitem__("api_key", r.body["key"])},
        {"id": "03-models", "feature": "F01", "requires": ["GET /v1/models"],
         "title_ko": "GET /v1/models", "title_en": "GET /v1/models",
         "call": http("GET", "/v1/models", as_="none", headers=_bearer), "expect": _models_ok},
        {"id": "04-chat", "feature": "F01", "requires": ["POST /v1/chat/completions"],
         "title_ko": "채팅 완성 → Dynamo 엔진", "title_en": "Chat completion → Dynamo engine",
         "narration_ko": "게이트웨이가 키 → 쿼터 → 입력 가드 → 엔진 순으로 처리. 지연은 랙 전력 상한으로 계산된다.",
         "narration_en": "Key → quota → input guard → engine. Latency comes from the rack power cap.",
         "call": http("POST", "/v1/chat/completions", lambda c: _chat(), as_="none",
                      headers=lambda c: _bearer(c, "demo-f01-chat")),
         "expect": _chat_ok,
         "links": [link("customer", "api", "고객 · 토큰 API", "Customer · token API")]},
        {"id": "05-stream", "feature": "F01", "requires": ["POST /v1/chat/completions"],
         "title_ko": "SSE 스트리밍 (마지막 청크 usage)", "title_en": "SSE streaming (usage in last chunk)",
         "call": http("POST", "/v1/chat/completions", lambda c: _chat(stream=True), as_="none",
                      headers=lambda c: _bearer(c, "demo-f01-sse")),
         "expect": _stream_ok},
        {"id": "06-retry-a", "feature": "F01", "title_ko": "요청 (x-request-id 고정)",
         "title_en": "Request (fixed x-request-id)",
         "call": http("POST", "/v1/chat/completions", lambda c: _chat(), as_="none",
                      headers=lambda c: _bearer(c, "demo-f01-retry")),
         "expect": {"status": 200}},
        {"id": "07-retry-b", "feature": "F01", "title_ko": "같은 ID로 재시도", "title_en": "Retry with the same id",
         "call": http("POST", "/v1/chat/completions", lambda c: _chat(), as_="none",
                      headers=lambda c: _bearer(c, "demo-f01-retry")),
         "expect": _replay_ok},
        {"id": "08-usage", "feature": "F01", "requires": ["GET /api/v1/token/usage"],
         "title_ko": "usage 1회만 기록", "title_en": "Usage recorded once",
         "call": http("GET", "/api/v1/token/usage", params=lambda c: {"tenant_id": c["tenant_id"]}),
         "expect": _usage_once},
        {"id": "09-guard", "feature": "F03", "requires": ["PUT /api/v1/token/guard-policy/{tid}"],
         "title_ko": "가드 enforce + 차단어", "title_en": "Guard enforce + blocked term",
         "call": http("PUT", lambda c: f"/api/v1/token/guard-policy/{c['tenant_id']}",
                      lambda c: {"mode": "enforce", "blocked_terms": [_cut_term()]}),
         "expect": _policy_ok},
        {"id": "10-input-block", "feature": "F03", "title_ko": "PII 입력 차단", "title_en": "PII input blocked",
         "narration_ko": "주민등록번호 패턴(Kanana Safeguard 자리)은 엔진에 가기 전에 막힌다.",
         "narration_en": "A resident-registration-number pattern (Kanana Safeguard slot) never reaches the engine.",
         "call": http("POST", "/v1/chat/completions",
                      lambda c: _chat(text="제 주민번호 900101-1234567 확인해줘"), as_="none",
                      headers=_bearer),
         "expect": _in_blocked},
        {"id": "11-output-cut", "feature": "F03", "title_ko": "출력 가드: 스트림 중간 차단",
         "title_en": "Output guard: stream cut midway",
         "narration_ko": "Qwen3Guard-Stream처럼 토큰마다 검사해 차단어가 나오기 직전에 finish_reason=content_filter로 끝낸다.",
         "narration_en": "Like Qwen3Guard-Stream each token is checked; the stream ends with finish_reason=content_filter before the term.",
         "call": http("POST", "/v1/chat/completions", lambda c: _chat(stream=True, max_tokens=40),
                      as_="none", headers=lambda c: _bearer(c, CUT_RID)),
         "expect": _out_cut},
        tenant_step("12-gov", "gov-demo-agency", key="gov_id", tier="k8s_namespace"),
        {"id": "13-gov-locked", "feature": "F03", "title_ko": "공공 테넌트 enforce 고정",
         "title_en": "Public-sector tenant pinned to enforce",
         "call": http("PUT", lambda c: f"/api/v1/token/guard-policy/{c['gov_id']}", {"mode": "monitor"}),
         "expect": _public_locked},
        {"id": "14-slo", "feature": "F01", "requires": ["GET /api/v1/token/slo"],
         "title_ko": "추론 SLO (덱 기준선)", "title_en": "Inference SLO (deck baseline)",
         "call": http("GET", "/api/v1/token/slo", params={"model": MODEL}), "expect": _slo_ok,
         "links": [link("ops", "performance", "운영 · 추론 SLO", "Ops · inference SLO")]},
    ],
}
