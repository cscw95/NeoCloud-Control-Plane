"""F01 token gateway — OpenAI-compatible public API in front of the AI-Infra
Dynamo emulator (Token Factory).

Request flow (Envoy AI Gateway → Dynamo frontend, as in the DSX deck):
  API key auth → quota (F02 hook) → input guard (F03) → engine routing
  (AI-Infra POST /dynamo/v1/generate, stub engine when AI-Infra is down)
  → SSE streaming with output guard (F03) → usage events (F04).

Wire format follows the OpenAI API spec copy in
ref/upstream/openai/openapi.yaml (openai/openai-openapi 2.3.0, MIT):
CreateChatCompletionResponse / CreateChatCompletionStreamResponse
("chat.completion.chunk", `data: [DONE]` terminator), CreateCompletionResponse,
CreateEmbeddingResponse, ListModelsResponse, ErrorResponse
{error:{message,type,param,code}}. Streaming and usage follow vLLM's
OpenAI server (vllm/entrypoints/openai/serving_chat.py: role delta first,
content deltas, finish chunk, then a usage chunk with choices: [] — sent
always here, as the contract asks for usage in the last chunk). Usage also
carries the flat fields of the approved contract (cached_tokens,
reasoning_tokens) next to the OpenAI *_details objects.

Service tier: request service_tier "interactive"|"batch" (contract) or the
OpenAI names "default"|"auto"|"flex"; the response echoes the OpenAI enum
(interactive → "default", batch → "flex") and the NeoCloud tier sits in
`neocloud.service_tier` and the `x-neocloud-service-tier` header.

Idempotency: `x-request-id` (client supplied or generated) is the idempotency
key; a retry with the same id replays the stored answer and emits no new
usage. The id is passed to the engine as request_id (the engine replays too).

  GET  /v1/models
  POST /v1/chat/completions      POST /v1/completions      POST /v1/embeddings
  POST /api/v1/token/keys {tenant_id, name}   GET /api/v1/token/keys?tenant_id=
  DELETE /api/v1/token/keys/{key_id}
  GET  /api/v1/token/deployments
  GET  /api/v1/token/slo?model=&window=
  GET  /api/v1/token/usage?tenant_id=&limit=
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import secrets
import threading
import time
import uuid
import zlib
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Any, Iterator, Optional, Union

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import dsx, token_guard
from .store import STORE

router = APIRouter(tags=["token-api"])
ops = APIRouter(prefix="/api/v1/token", tags=["token-api"])

KEY_PREFIX = "sk-neo-"
DEFAULT_MAX_TOKENS = 256
MAX_OUTPUT_TOKENS = 32768
CACHE_MAX = 5000
USAGE_MAX = 10000
SAMPLES_MAX = 5000
MODELS_TTL_S = 5.0
SLO_TARGET = {"ttft_p99_ms": 2000.0, "tpot_p99_ms": 50.0}
TIERS = ("interactive", "batch")
_TIER_IN = {"interactive": "interactive", "default": "interactive", "auto": "interactive",
            "priority": "interactive", "batch": "batch", "flex": "batch", "scale": "batch"}
_TIER_OUT = {"interactive": "default", "batch": "flex"}

# Stub engine catalog — same ids/numbers as AI-Infra app/dynamo.py MODELS, used
# only when AI-Infra is unreachable (NOCP_DSX_OFFLINE=1 or connection error).
STUB_MODELS = {
    "llama-3.3-70b-instruct": {"context_length": 131072, "tpot_ms": 12.0,
                               "prefill_tps": 60000.0, "reasoning": 0.0},
    "qwen3-235b-a22b": {"context_length": 131072, "tpot_ms": 18.0,
                        "prefill_tps": 45000.0, "reasoning": 0.0},
    "deepseek-r1": {"context_length": 131072, "tpot_ms": 22.0,
                    "prefill_tps": 30000.0, "reasoning": 0.6},
    "kanana-1.5-8b-instruct": {"context_length": 32768, "tpot_ms": 6.0,
                               "prefill_tps": 40000.0, "reasoning": 0.0},
}
# embeddings are served by the gateway itself (Dynamo serves generation only)
EMBED_MODELS = {"bge-m3": {"context_length": 8192, "dims": 32}}
_WORDS = ("the", "rack", "power", "token", "model", "cluster", "GPU", "fast",
          "serving", "NeoCloud", "inference", "stream", "data", "is", "ready",
          "and", "decode", "prefill", "cache", "efficient")
_CREATED = 1767225600            # 2026-01-01T00:00:00Z — fixed model "created"


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _h(s: str) -> int:
    return zlib.crc32(s.encode())


# ---------------------------------------------------------------------------
# quota hook (F02 replaces it with set_quota_hook)
# ---------------------------------------------------------------------------
class _WindowQuota:
    """Stub until F02: fixed 60 s window per key+tier, generous defaults.
    Interface F02 must keep: check(...) -> (ok, retry_after_s, headers),
    commit(...) after the request with the real token count."""
    RPM, TPM = 600, 2_000_000

    def __init__(self):
        self.lock = threading.Lock()
        self.win: dict[tuple, list] = {}

    def _w(self, key: tuple, now: float) -> list:
        w = self.win.get(key)
        if w is None or now - w[0] >= 60:
            w = self.win[key] = [now, 0, 0]
        return w

    def check(self, tenant_id: str, key_id: str, tier: str, est_tokens: int):
        now = time.time()
        with self.lock:
            w = self._w((key_id, tier), now)
            rem_r, rem_t = self.RPM - w[1], self.TPM - w[2]
            hdr = {"x-ratelimit-limit-requests": str(self.RPM),
                   "x-ratelimit-limit-tokens": str(self.TPM),
                   "x-ratelimit-remaining-requests": str(max(0, rem_r - 1)),
                   "x-ratelimit-remaining-tokens": str(max(0, rem_t - est_tokens))}
            if rem_r <= 0 or rem_t < est_tokens:
                return False, max(1, math.ceil(60 - (now - w[0]))), hdr
            w[1] += 1
            return True, 0, hdr

    def commit(self, tenant_id: str, key_id: str, tier: str, tokens: int):
        with self.lock:
            self._w((key_id, tier), time.time())[2] += tokens

    def reset(self):
        with self.lock:
            self.win.clear()


QUOTA: Any = _WindowQuota()


def set_quota_hook(q: Any) -> None:
    global QUOTA
    QUOTA = q


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
class _Gateway:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        self.keys: dict[str, dict] = {}            # key_id → record (hash only)
        self.by_hash: dict[str, str] = {}          # sha256(secret) → key_id
        self.cache: OrderedDict = OrderedDict()    # (tenant, request_id) → replay
        self.usage: deque = deque(maxlen=USAGE_MAX)
        self.samples: deque = deque(maxlen=SAMPLES_MAX)
        self.models: tuple[float, Optional[dict]] = (0.0, None)
        self._kseq = itertools.count(1)
        self._useq = itertools.count(1)


GW = _Gateway()


def reset() -> None:
    with GW.lock:
        GW.reset()
    if hasattr(QUOTA, "reset"):
        QUOTA.reset()
    token_guard.reset()


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def issue_key(tenant_id: str, name: str = "default") -> dict:
    """Create an API key; the secret is returned once and stored hashed."""
    secret = KEY_PREFIX + secrets.token_urlsafe(24)
    with GW.lock:
        kid = f"key-{next(GW._kseq):04d}"
        rec = {"id": kid, "object": "api_key", "tenant_id": tenant_id, "name": name,
               "prefix": secret[:11] + "…", "created_at": _iso(), "revoked": False,
               "last_used_at": None}
        GW.keys[kid] = rec
        GW.by_hash[_sha(secret)] = kid
    return {**rec, "key": secret}


# ---------------------------------------------------------------------------
# OpenAI error envelope
# ---------------------------------------------------------------------------
class APIError(Exception):
    def __init__(self, status: int, message: str, type_: str = "invalid_request_error",
                 code: Optional[str] = None, param: Optional[str] = None,
                 headers: Optional[dict] = None, **extra):
        self.status, self.headers = status, headers or {}
        self.body = {"error": {"message": message, "type": type_, "param": param,
                               "code": code, **extra}}


def _err(e: APIError) -> JSONResponse:
    return JSONResponse(e.body, status_code=e.status, headers=e.headers)


def _auth(request: Request) -> dict:
    authz = request.headers.get("authorization", "")
    scheme, _, tok = authz.partition(" ")
    tok = tok.strip()
    if not authz or scheme.lower() != "bearer" or not tok:
        raise APIError(401, "You didn't provide an API key. You need to provide your "
                            "API key in an Authorization header using Bearer auth "
                            "(i.e. Authorization: Bearer YOUR_KEY).")
    with GW.lock:
        kid = GW.by_hash.get(_sha(tok))
        rec = GW.keys.get(kid) if kid else None
        if rec is not None and not rec["revoked"]:
            rec["last_used_at"] = _iso()
            return dict(rec)
    # customer-console keys (POST /api/v1/api-keys, customer_ops.py: nc_sk_…)
    with STORE.lock:
        a = next((a for a in STORE.api_keys.values() if a.get("secret") == tok), None)
        if (a is not None and a.get("state", "active") == "active"
                and a.get("scope") not in ("read", "read-only")):
            a["last_used"] = _iso()
            return {"id": a["key_id"], "tenant_id": a["tenant_id"], "name": a.get("name"),
                    "source": "customer-console"}
    raise APIError(401, f"Incorrect API key provided: {tok[:7]}…", code="invalid_api_key")


# ---------------------------------------------------------------------------
# models / engine
# ---------------------------------------------------------------------------
def _engine_models() -> tuple[dict, str]:
    """{model_id: {context_length, engine}} and the source (ai-infra|stub)."""
    now = time.time()
    with GW.lock:
        at, cached = GW.models
        if cached is not None and now - at < MODELS_TTL_S:
            return cached["m"], cached["src"]
    body = None if dsx.offline() else dsx.ai_get("/dynamo/v1/models", timeout=2.0)
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        m = {x["id"]: {"context_length": x.get("context_length"), "engine": "dynamo",
                       "deployments": x.get("deployments", [])}
             for x in body["data"] if isinstance(x, dict) and x.get("id")}
        src = "ai-infra"
    else:
        m = {k: {"context_length": v["context_length"], "engine": "stub",
                 "deployments": []} for k, v in STUB_MODELS.items()}
        src = "stub"
    with GW.lock:
        GW.models = (now, {"m": m, "src": src})
    return m, src


def _stub_run(model: str, prompt_tokens: int, max_tokens: int, rid: str,
              prefix_hash: Optional[str]) -> dict:
    m = STUB_MODELS.get(model) or STUB_MODELS["llama-3.3-70b-instruct"]
    ttft = 15.0 + prompt_tokens / m["prefill_tps"] * 1000.0 + m["tpot_ms"]
    return {"id": rid, "deployment": f"stub-{model}", "model": model,
            "prompt_tokens": prompt_tokens, "cached_tokens": 0, "kv_hit_tokens": 0,
            "completion_tokens": max_tokens,
            "reasoning_tokens": int(max_tokens * m["reasoning"]),
            "total_tokens": prompt_tokens + max_tokens,
            "ttft_ms": round(ttft, 2), "tpot_ms": m["tpot_ms"],
            "worker": {"prefill": "stub", "decode": "stub"}, "racks": [],
            "finish_reason": "length", "engine": "stub"}


def _stub_events(res: dict) -> Iterator[tuple[str, Any]]:
    yield "start", res
    for i in range(res["completion_tokens"]):
        w = _WORDS[_h(f"{res['id']}:{i}") % len(_WORDS)]
        yield "token", w if i == 0 else " " + w
    yield "done", res


def _engine_events(payload: dict) -> Iterator[tuple[str, Any]]:
    """Engine stream as ("start"|"token"|"done", data). Falls back to the stub
    engine when AI-Infra is unreachable before the first byte. Engine HTTP
    errors become APIError (raised before the first event)."""
    stub = lambda: _stub_events(_stub_run(payload["model"], payload["prompt_tokens"],  # noqa: E731
                                          payload["max_tokens"], payload["request_id"],
                                          payload.get("prefix_hash")))
    if dsx.offline():
        yield from stub()
        return
    url = dsx.ai_infra_url() + "/dynamo/v1/generate"
    try:
        client = httpx.Client(transport=dsx._transport, timeout=httpx.Timeout(30.0, connect=2.0))
        cm = client.stream("POST", url, json={**payload, "stream": True},
                           headers=dsx.emu_headers())
        r = cm.__enter__()
    except Exception:                               # noqa: BLE001 — engine down
        client.close()
        yield from stub()
        return
    try:
        if r.status_code >= 400:
            r.read()
            try:
                detail = r.json().get("detail")
            except Exception:                       # noqa: BLE001
                detail = r.text
            if r.status_code == 404:
                raise APIError(404, f"The model `{payload['model']}` does not exist or "
                                    f"has no deployment ({detail})", code="model_not_found",
                               param="model")
            if r.status_code == 503:
                raise APIError(503, f"engine unavailable: {detail}", type_="server_error",
                               code="engine_unavailable")
            if r.status_code == 422:
                raise APIError(400, f"engine rejected the request: {detail}",
                               param="max_tokens")
            raise APIError(502, f"engine error {r.status_code}: {detail}",
                           type_="server_error", code="bad_gateway")
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            ev = json.loads(data)
            t = ev.get("type")
            if t == "start":
                yield "start", ev
            elif t == "token":
                yield "token", ev.get("text", "")
            elif t == "done":
                ev.setdefault("engine", "dynamo")
                yield "done", ev
    finally:
        cm.__exit__(None, None, None)
        client.close()


# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------
def _count(text: str) -> int:
    """Deterministic token estimate (~4 chars per token, ≥1 for non-empty)."""
    return max(1, math.ceil(len(text) / 4)) if text else 0


def _content_text(c: Any) -> str:
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):                         # content parts
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c)


class ChatBody(BaseModel):
    model: str
    messages: list[dict] = Field(..., min_length=1)
    stream: bool = False
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    user: Optional[str] = None
    service_tier: Optional[str] = None
    n: Optional[int] = 1
    stream_options: Optional[dict] = None
    model_config = {"extra": "allow"}


class CompletionBody(BaseModel):
    model: str
    prompt: Union[str, list[str]]
    stream: bool = False
    max_tokens: Optional[int] = None
    user: Optional[str] = None
    service_tier: Optional[str] = None
    n: Optional[int] = 1
    model_config = {"extra": "allow"}


class EmbeddingBody(BaseModel):
    model: str
    input: Union[str, list[str]]
    user: Optional[str] = None
    encoding_format: Optional[str] = "float"
    model_config = {"extra": "allow"}


def _tier(v: Optional[str]) -> str:
    if v is None:
        return "interactive"
    t = _TIER_IN.get(v)
    if t is None:
        raise APIError(400, f"service_tier must be one of {list(TIERS)}", param="service_tier")
    return t


def _request_id(request: Request) -> str:
    rid = (request.headers.get("x-request-id") or "").strip()
    if rid and (len(rid) > 128 or not all(c.isalnum() or c in "-_.:" for c in rid)):
        raise APIError(400, "x-request-id must be ≤128 chars of [A-Za-z0-9-_.:]")
    return rid or f"req-{uuid.uuid4().hex[:20]}"


def _usage(res: dict, completion: int, reasoning: int) -> dict:
    prompt, cached = res["prompt_tokens"], res.get("cached_tokens", 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "cached_tokens": cached, "reasoning_tokens": reasoning,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": reasoning}}


def _emit_usage(key: dict, model: str, tier: str, rid: str, usage: dict,
                res: dict, finish: str, kind: str) -> None:
    """Gateway usage → F04 metering events, one per token type
    (input excludes cached; output excludes reasoning), idempotency key
    gw:<request_id>:<token_type>."""
    ts = _iso()
    rec = {"seq": next(GW._useq), "request_id": rid, "tenant_id": key["tenant_id"],
           "key_id": key["id"], "model": model, "service_tier": tier, "kind": kind,
           "finish_reason": finish, "engine": res.get("engine", "dynamo"),
           "deployment": res.get("deployment"), "ttft_ms": res.get("ttft_ms"),
           "tpot_ms": res.get("tpot_ms"), "usage": {k: v for k, v in usage.items()
                                                    if not k.endswith("_details")},
           "at": ts}
    with GW.lock:
        GW.usage.append(rec)
        if res.get("ttft_ms") is not None:
            GW.samples.append({"ts": time.time(), "model": model,
                               "ttft": res["ttft_ms"], "tpot": res.get("tpot_ms")})
    parts = {"input": usage["prompt_tokens"] - usage["cached_tokens"],
             "cached": usage["cached_tokens"],
             "output": usage["completion_tokens"] - usage["reasoning_tokens"],
             "reasoning": usage["reasoning_tokens"]}
    events = [{"idempotency_key": f"gw:{rid}:{tt}", "source": "gateway",
               "tenant_id": key["tenant_id"], "product": "token-api", "metric": "token",
               "quantity": q, "unit": "tokens", "ts": ts,
               "attrs": {"model": model, "token_type": tt, "tier": tier,
                         "request_id": rid, "finish_reason": finish}}
              for tt, q in parts.items() if q > 0]
    _publish_metering(events)
    if hasattr(QUOTA, "commit"):
        QUOTA.commit(key["tenant_id"], key["id"], tier, usage["total_tokens"])


def _publish_metering(events: list[dict]) -> None:
    """F04 boundary: app.metering.ingest(event) when F04 has landed, else
    append to STORE.metering_events (list or dict keyed by idempotency key —
    the shape app/billing.py reads)."""
    try:
        from . import metering                       # type: ignore[attr-defined]
        ingest = getattr(metering, "ingest", None)
    except ImportError:
        ingest = None
    for e in events:
        if ingest is not None:
            ingest(e)
            continue
        sink = getattr(STORE, "metering_events", None)
        if sink is None:
            sink = []
            setattr(STORE, "metering_events", sink)
        if isinstance(sink, dict):
            sink.setdefault(e["idempotency_key"], e)
        elif not any(x.get("idempotency_key") == e["idempotency_key"] for x in sink[-50:]):
            sink.append(e)


def _headers(rid: str, tier: str, qh: dict, engine: str) -> dict:
    return {"x-request-id": rid, "x-neocloud-service-tier": tier,
            "x-neocloud-engine": engine, **qh}


# ---------------------------------------------------------------------------
# core generation (chat + completions share it)
# ---------------------------------------------------------------------------
def _generate(request: Request, *, kind: str, model: str, text: str, prompt_tokens: int,
              prefix_hash: Optional[str], max_tokens: Optional[int], stream: bool,
              tier_in: Optional[str], user: Optional[str], n: Optional[int]):
    try:
        key = _auth(request)
        rid = _request_id(request)
        tier = _tier(tier_in)
        if n not in (None, 1):
            raise APIError(400, "only n=1 is supported", param="n")
        models, _src = _engine_models()
        if model not in models:
            raise APIError(404, f"The model `{model}` does not exist or you do not have "
                                f"access to it.", code="model_not_found", param="model")
        mt = DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        ctx = models[model].get("context_length") or 131072
        if not 1 <= mt <= min(MAX_OUTPUT_TOKENS, ctx - prompt_tokens):
            raise APIError(400, f"max_tokens must be 1..{min(MAX_OUTPUT_TOKENS, ctx - prompt_tokens)} "
                                f"for this prompt", param="max_tokens")
        ck = (key["tenant_id"], rid)
        with GW.lock:
            prev = GW.cache.get(ck)
        if prev is not None:
            if prev["kind"] != kind or prev["model"] != model:
                raise APIError(409, "x-request-id was already used for a different request",
                               code="idempotency_conflict")
            hdr = _headers(rid, prev["tier"], {}, prev["res"].get("engine", "dynamo"))
            hdr["x-idempotent-replay"] = "true"
            return _respond(prev, stream, hdr)
        ok, retry, qh = QUOTA.check(key["tenant_id"], key["id"], tier, prompt_tokens + mt)
        if not ok:
            raise APIError(429, "Rate limit reached for requests or tokens; retry after "
                                f"{retry}s", type_="rate_limit_exceeded",
                           code="rate_limit_exceeded",
                           headers={"retry-after": str(retry), "x-request-id": rid, **qh})
        gin = token_guard.check(key["tenant_id"], text, "input", request_id=rid)
        if gin["action"] == "block":
            raise APIError(400, f"input blocked by safety guard: {gin['categories']}",
                           type_="content_filter", code="content_filter", stage="input",
                           categories=gin["categories"], headers={"x-request-id": rid, **qh})
        payload = {"model": model, "prompt_tokens": prompt_tokens, "max_tokens": mt,
                   "prefix_hash": prefix_hash, "request_id": rid,
                   "tenant_id": key["tenant_id"]}
        events = _engine_events(payload)
        first = next(events)                       # raises APIError on engine errors
    except APIError as e:
        return _err(e)
    start = first[1]
    state = {"key": key, "rid": rid, "tier": tier, "model": model, "kind": kind,
             "user": user, "created": int(time.time()), "guard_in": gin}
    hdr = _headers(rid, tier, qh, start.get("engine") or "dynamo")
    if stream:
        return StreamingResponse(_stream_live(state, events), media_type="text/event-stream",
                                 headers={**hdr, "cache-control": "no-cache"})
    rec = _collect(state, events)
    if rec["finish_reason"] == "content_filter" and rec["guard_out"]["action"] == "block":
        return _err(APIError(400, "output blocked by safety guard: "
                                  f"{rec['guard_out']['categories']}", type_="content_filter",
                             code="content_filter", stage="output",
                             categories=rec["guard_out"]["categories"], headers=hdr))
    return JSONResponse(_body(rec), headers=hdr)


def _finish(state: dict, tokens: list[str], res: dict, guard_out: Optional[dict]) -> dict:
    blocked = guard_out is not None and guard_out["action"] == "block"
    completion = len(tokens)
    m = STUB_MODELS.get(state["model"], {})
    reasoning = (min(completion, res.get("reasoning_tokens", 0)) if not blocked
                 else int(completion * m.get("reasoning", 0.0)))
    finish = "content_filter" if blocked else res.get("finish_reason", "length")
    usage = _usage(res, completion, reasoning)
    rec = {**state, "tokens": tokens, "res": res, "usage": usage, "finish_reason": finish,
           "guard_out": guard_out}
    rec.pop("key")
    with GW.lock:
        GW.cache[(state["key"]["tenant_id"], state["rid"])] = rec
        while len(GW.cache) > CACHE_MAX:
            GW.cache.popitem(last=False)
    _emit_usage(state["key"], state["model"], state["tier"], state["rid"], usage, res,
                finish, state["kind"])
    return rec


def _collect(state: dict, events) -> dict:
    sg = token_guard.StreamGuard(state["key"]["tenant_id"], state["rid"])
    tokens, res, verdict = [], None, None
    for t, data in events:
        if t == "token":
            v = sg.feed(data)
            if v is not None and v["action"] == "block":
                verdict = v
                break
            tokens.append(data)
        elif t == "done":
            res = data
    if res is None:                                  # cut by the guard: drain for res
        for t, data in events:
            if t == "done":
                res = data
    verdict = verdict or sg.flagged
    return _finish(state, tokens, res or {}, verdict)


def _neocloud(rec: dict) -> dict:
    res = rec["res"]
    return {"service_tier": rec["tier"], "engine": res.get("engine", "dynamo"),
            "deployment": res.get("deployment"), "worker": res.get("worker"),
            "racks": res.get("racks"), "ttft_ms": res.get("ttft_ms"),
            "tpot_ms": res.get("tpot_ms"), "kv_hit_tokens": res.get("kv_hit_tokens", 0),
            "guard": {"input": rec["guard_in"],
                      "output": rec["guard_out"] or {"stage": "output", "action": "allow",
                                                     "label": "Safe", "categories": []}}}


def _cmpl_id(rec: dict) -> str:
    return ("chatcmpl-" if rec["kind"] == "chat" else "cmpl-") + rec["rid"]


def _body(rec: dict) -> dict:
    text = "".join(rec["tokens"])
    base = {"id": _cmpl_id(rec), "created": rec["created"], "model": rec["model"],
            "service_tier": _TIER_OUT[rec["tier"]], "system_fingerprint": "fp_neocloud_dynamo",
            "usage": rec["usage"], "neocloud": _neocloud(rec)}
    if rec["kind"] == "chat":
        return {**base, "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text,
                                                      "refusal": None},
                             "logprobs": None, "finish_reason": rec["finish_reason"]}]}
    return {**base, "object": "text_completion",
            "choices": [{"index": 0, "text": text, "logprobs": None,
                         "finish_reason": rec["finish_reason"]}]}


def _sse(obj: Any) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _chunk(rec: dict, delta: Optional[dict] = None, finish: Optional[str] = None,
           text: Optional[str] = None, usage: Optional[dict] = None) -> dict:
    base = {"id": _cmpl_id(rec), "created": rec["created"], "model": rec["model"],
            "service_tier": _TIER_OUT[rec["tier"]], "system_fingerprint": "fp_neocloud_dynamo"}
    if usage is not None:
        choices: list = []
    elif rec["kind"] == "chat":
        choices = [{"index": 0, "delta": delta or {}, "logprobs": None, "finish_reason": finish}]
    else:
        choices = [{"index": 0, "text": text or "", "logprobs": None, "finish_reason": finish}]
    out = {**base, "object": "chat.completion.chunk" if rec["kind"] == "chat"
           else "text_completion", "choices": choices}
    if usage is not None:
        out["usage"] = usage
        out["neocloud"] = _neocloud(rec)
    return out


def _token_chunk(rec: dict, tok: str) -> dict:
    return (_chunk(rec, delta={"content": tok}) if rec["kind"] == "chat"
            else _chunk(rec, text=tok))


def _stream_live(state: dict, events) -> Iterator[str]:
    view = {**state}
    if state["kind"] == "chat":
        yield _sse(_chunk(view, delta={"role": "assistant", "content": ""}))
    sg = token_guard.StreamGuard(state["key"]["tenant_id"], state["rid"])
    tokens, res, verdict = [], None, None
    for t, data in events:
        if t == "token":
            v = sg.feed(data)
            if v is not None and v["action"] == "block":
                verdict = v                          # Qwen3Guard-Stream: stop here
                break
            tokens.append(data)
            yield _sse(_token_chunk(view, data))
        elif t == "done":
            res = data
    if res is None:
        for t, data in events:
            if t == "done":
                res = data
    rec = _finish(state, tokens, res or {}, verdict or sg.flagged)
    yield from _tail(rec)


def _tail(rec: dict) -> Iterator[str]:
    yield _sse(_chunk(rec, delta={}, finish=rec["finish_reason"]) if rec["kind"] == "chat"
               else _chunk(rec, text="", finish=rec["finish_reason"]))
    yield _sse(_chunk(rec, usage=rec["usage"]))
    yield "data: [DONE]\n\n"


def _replay_stream(rec: dict) -> Iterator[str]:
    if rec["kind"] == "chat":
        yield _sse(_chunk(rec, delta={"role": "assistant", "content": ""}))
    for tok in rec["tokens"]:
        yield _sse(_token_chunk(rec, tok))
    yield from _tail(rec)


def _respond(rec: dict, stream: bool, hdr: dict):
    if stream:
        return StreamingResponse(_replay_stream(rec), media_type="text/event-stream",
                                 headers={**hdr, "cache-control": "no-cache"})
    if rec["finish_reason"] == "content_filter" and (rec.get("guard_out") or {}).get("action") == "block":
        return _err(APIError(400, "output blocked by safety guard", type_="content_filter",
                             code="content_filter", stage="output",
                             categories=rec["guard_out"]["categories"], headers=hdr))
    return JSONResponse(_body(rec), headers=hdr)


# ---------------------------------------------------------------------------
# public OpenAI-compatible routes
# ---------------------------------------------------------------------------
@router.get("/v1/models")
def list_models(request: Request):
    try:
        _auth(request)
    except APIError as e:
        return _err(e)
    models, src = _engine_models()
    data = [{"id": mid, "object": "model", "created": _CREATED, "owned_by": "neocloud",
             "context_length": m.get("context_length"), "tiers": list(TIERS),
             "engine": m.get("engine")} for mid, m in models.items()]
    data += [{"id": mid, "object": "model", "created": _CREATED, "owned_by": "neocloud",
              "context_length": m["context_length"], "tiers": list(TIERS),
              "engine": "gateway-embedding"} for mid, m in EMBED_MODELS.items()]
    return JSONResponse({"object": "list", "data": data}, headers={"x-neocloud-engine": src})


@router.post("/v1/chat/completions")
def chat_completions(body: ChatBody, request: Request):
    text = "\n".join(_content_text(m.get("content")) for m in body.messages)
    prompt_tokens = sum(4 + _count(_content_text(m.get("content"))) for m in body.messages) + 2
    prefix = (format(_h(json.dumps(body.messages[:-1], sort_keys=True, ensure_ascii=False)), "08x")
              if len(body.messages) > 1 else None)
    return _generate(request, kind="chat", model=body.model, text=text,
                     prompt_tokens=prompt_tokens, prefix_hash=prefix,
                     max_tokens=body.max_completion_tokens or body.max_tokens,
                     stream=body.stream, tier_in=body.service_tier, user=body.user, n=body.n)


@router.post("/v1/completions")
def completions(body: CompletionBody, request: Request):
    if isinstance(body.prompt, list) and len(body.prompt) != 1:
        return _err(APIError(400, "only a single prompt is supported", param="prompt"))
    text = body.prompt if isinstance(body.prompt, str) else body.prompt[0]
    return _generate(request, kind="text", model=body.model, text=text,
                     prompt_tokens=max(1, _count(text)), prefix_hash=None,
                     max_tokens=body.max_tokens, stream=body.stream,
                     tier_in=body.service_tier, user=body.user, n=body.n)


def _embed(text: str, dims: int) -> list[float]:
    raw = hashlib.sha256(text.encode()).digest() * (dims // 32 + 1)
    v = [(b - 127.5) / 127.5 for b in raw[:dims]]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / norm, 6) for x in v]


@router.post("/v1/embeddings")
def embeddings(body: EmbeddingBody, request: Request):
    try:
        key = _auth(request)
        rid = _request_id(request)
        if body.model not in EMBED_MODELS:
            raise APIError(404, f"The model `{body.model}` does not exist or you do not "
                                f"have access to it.", code="model_not_found", param="model")
        if body.encoding_format not in (None, "float"):
            raise APIError(400, "only encoding_format=float is supported",
                           param="encoding_format")
        inputs = [body.input] if isinstance(body.input, str) else body.input
        gin = token_guard.check(key["tenant_id"], "\n".join(inputs), "input", request_id=rid)
        if gin["action"] == "block":
            raise APIError(400, f"input blocked by safety guard: {gin['categories']}",
                           type_="content_filter", code="content_filter", stage="input",
                           categories=gin["categories"])
    except APIError as e:
        return _err(e)
    dims = EMBED_MODELS[body.model]["dims"]
    tokens = sum(max(1, _count(t)) for t in inputs)
    res = {"prompt_tokens": tokens, "cached_tokens": 0, "engine": "gateway-embedding",
           "deployment": None}
    with GW.lock:
        dup = (key["tenant_id"], rid) in GW.cache
        GW.cache[(key["tenant_id"], rid)] = {"kind": "embedding", "model": body.model}
    if not dup:
        _emit_usage(key, body.model, "interactive", rid, _usage(res, 0, 0), res, "stop",
                    "embedding")
    return JSONResponse({"object": "list", "model": body.model,
                         "data": [{"object": "embedding", "index": i,
                                   "embedding": _embed(t, dims)} for i, t in enumerate(inputs)],
                         "usage": {"prompt_tokens": tokens, "total_tokens": tokens}},
                        headers={"x-request-id": rid})


# ---------------------------------------------------------------------------
# operator / customer management surfaces (/api/v1/token/*)
# ---------------------------------------------------------------------------
class KeyCreate(BaseModel):
    tenant_id: str
    name: str = Field("default", max_length=64)


def _tenant_scope(request: Request, tenant_id: Optional[str]) -> Optional[str]:
    ident = getattr(request.state, "identity", None)
    if ident is not None and ident.role == "tenant":
        if tenant_id and tenant_id != ident.tenant_id:
            raise HTTPException(403, "tenant scope violation")
        return ident.tenant_id
    return tenant_id


@ops.post("/keys", status_code=201)
def create_key(body: KeyCreate, request: Request):
    _tenant_scope(request, body.tenant_id)
    if body.tenant_id not in STORE.tenants:
        raise HTTPException(404, f"tenant {body.tenant_id} not found")
    return issue_key(body.tenant_id, body.name)


@ops.get("/keys")
def list_keys(request: Request, tenant_id: Optional[str] = None):
    tenant_id = _tenant_scope(request, tenant_id)
    with GW.lock:
        rows = [dict(k) for k in GW.keys.values()
                if not tenant_id or k["tenant_id"] == tenant_id]
    return {"count": len(rows), "keys": rows}


@ops.delete("/keys/{key_id}")
def revoke_key(key_id: str, request: Request):
    with GW.lock:
        rec = GW.keys.get(key_id)
        if rec is None:
            raise HTTPException(404, f"key {key_id} not found")
        _tenant_scope(request, rec["tenant_id"])
        rec["revoked"] = True
        return dict(rec)


@ops.get("/deployments")
def deployments():
    """Model → engine deployment (DGD), racks, GPUs (AI-Infra Dynamo)."""
    body = None if dsx.offline() else dsx.ai_get("/dynamo/v1/deployments")
    if isinstance(body, dict) and isinstance(body.get("deployments"), list):
        items = [{"model": d.get("model"), "deployment": d.get("id"), "kind": d.get("kind"),
                  "mode": d.get("mode"), "state": d.get("state"), "racks": d.get("racks", []),
                  "gpus": d.get("gpus"), "tp": d.get("tp"),
                  "prefill_workers": d.get("prefill_workers"),
                  "decode_workers": d.get("decode_workers"),
                  "capacity_tokens_per_s": d.get("capacity_tokens_per_s"),
                  "power_kw": d.get("power_kw")} for d in body["deployments"]]
        return {"source": "ai-infra", "count": len(items), "deployments": items}
    items = [{"model": m, "deployment": f"stub-{m}", "kind": "Stub", "mode": "stub",
              "state": "Stub", "racks": [], "gpus": 0} for m in STUB_MODELS]
    return {"source": "stub", "count": len(items), "deployments": items}


def _p99(vals: list[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s) - 1, math.ceil(0.99 * len(s)) - 1)], 3)


@ops.get("/slo")
def slo(model: Optional[str] = None, window: float = 300.0):
    """TTFT/TPOT p99 vs the deck target (TTFT p99 ≤ 2 s, TPOT p99 ≤ 50 ms).
    Source: AI-Infra /dynamo/v1/stats; gateway samples when it is down."""
    window = max(1.0, min(window, 30 * 86400.0))
    params = {"window": window, **({"model": model} if model else {})}
    body = None if dsx.offline() else dsx.ai_get("/dynamo/v1/stats", params=params)
    if isinstance(body, dict) and isinstance(body.get("deployments"), list):
        items = [{"model": d.get("model"), "deployment": d.get("deployment"),
                  "state": d.get("state"), "source": d.get("source"),
                  "requests": d.get("requests"), "ttft_p99_ms": d.get("ttft_p99_ms"),
                  "tpot_p99_ms": d.get("tpot_p99_ms"), "breach": d.get("breach"),
                  "served_tokens_per_s": d.get("served_tokens_per_s"),
                  "power_kw": d.get("power_kw"),
                  "tokens_per_s_per_mw": d.get("tokens_per_s_per_mw")}
                 for d in body["deployments"]]
        src = "ai-infra"
    else:
        since = time.time() - window
        with GW.lock:
            ss = [s for s in GW.samples if s["ts"] >= since and (not model or s["model"] == model)]
        by: dict[str, list] = {}
        for s in ss:
            by.setdefault(s["model"], []).append(s)
        items = []
        for m, rows in sorted(by.items()):
            t99 = _p99([r["ttft"] for r in rows])
            p99 = _p99([r["tpot"] for r in rows if r["tpot"] is not None])
            items.append({"model": m, "deployment": f"stub-{m}", "state": "Stub",
                          "source": "gateway", "requests": len(rows), "ttft_p99_ms": t99,
                          "tpot_p99_ms": p99,
                          "breach": bool((t99 or 0) > SLO_TARGET["ttft_p99_ms"]
                                         or (p99 or 0) > SLO_TARGET["tpot_p99_ms"])})
        src = "gateway"
    t99s = [i["ttft_p99_ms"] for i in items if i["ttft_p99_ms"] is not None]
    p99s = [i["tpot_p99_ms"] for i in items if i["tpot_p99_ms"] is not None]
    return {"model": model, "window_s": window, "source": src,
            "ttft_p99_ms": max(t99s) if t99s else None,
            "tpot_p99_ms": max(p99s) if p99s else None,
            "target": dict(SLO_TARGET), "breach": any(bool(i["breach"]) for i in items),
            "deployments": items}


@ops.get("/usage")
def usage(request: Request, tenant_id: Optional[str] = None, limit: int = 200):
    """Gateway-side usage records (one per request; F04 reads the events)."""
    tenant_id = _tenant_scope(request, tenant_id)
    with GW.lock:
        rows = [u for u in GW.usage if not tenant_id or u["tenant_id"] == tenant_id]
    rows = rows[-max(1, min(limit, USAGE_MAX)):]
    tot: dict[str, int] = {}
    for r in rows:
        for k, v in r["usage"].items():
            tot[k] = tot.get(k, 0) + int(v)
    return {"count": len(rows), "totals": tot, "records": rows}
