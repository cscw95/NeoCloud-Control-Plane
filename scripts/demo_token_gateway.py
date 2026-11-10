#!/usr/bin/env python3
"""Terminal demo for F01 (token gateway) + F03 (safety guard).

  NOCP_URL=http://127.0.0.1:8000 python scripts/demo_token_gateway.py

Prints each step as the curl command plus a one-line summary of the real
response (headers and body excerpt). Needs a running NOCP; operator calls use
NOCP_TOKEN when set (else the dev operator default of run.sh). Deterministic
ids, safe to re-run (same request ids replay).
"""
import json
import os
import sys
import zlib

import httpx

BASE = os.environ.get("NOCP_URL", "http://127.0.0.1:8000").rstrip("/")
OPS = {"Authorization": f"Bearer {os.environ['NOCP_TOKEN']}"} if os.environ.get("NOCP_TOKEN") else {}
MODEL = "llama-3.3-70b-instruct"
WORDS = ("the", "rack", "power", "token", "model", "cluster", "GPU", "fast", "serving",
         "NeoCloud", "inference", "stream", "data", "is", "ready", "and", "decode",
         "prefill", "cache", "efficient")
c = httpx.Client(base_url=BASE, timeout=30)
n = 0


def step(title, method, path, body=None, headers=None, params=None):
    global n
    n += 1
    h = {**(headers or {})}
    shown = {k: ("Bearer sk-…" if k == "Authorization" and "sk-" in v else v) for k, v in h.items()}
    curl = f"curl -sS -X {method} '{BASE}{path}" + (f"?{httpx.QueryParams(params)}" if params else "") + "'"
    for k, v in shown.items():
        curl += f" -H '{k}: {v}'"
    if body is not None:
        curl += f" -H 'Content-Type: application/json' -d '{json.dumps(body, ensure_ascii=False)}'"
    print(f"\n[{n:02d}] {title}\n  $ {curl}")
    r = c.request(method, path, json=body, headers=h, params=params)
    keep = {k: r.headers[k] for k in ("content-type", "x-request-id", "x-neocloud-engine",
                                      "x-ratelimit-remaining-tokens", "x-idempotent-replay",
                                      "retry-after") if k in r.headers}
    print(f"  → HTTP {r.status_code} {keep}")
    return r


def chat(text="NVL72 랙 하나에 GPU가 몇 개인가요?", stream=False, max_tokens=16):
    return {"model": MODEL, "stream": stream, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": "NeoCloud 안내 봇"},
                         {"role": "user", "content": text}]}


def sse(text):
    return [json.loads(l[6:]) if l[6:] != "[DONE]" else "[DONE]"
            for l in text.splitlines() if l.startswith("data: ")]


def say(msg):
    print(f"  ✔ {msg}")


r = step("테넌트 생성 / create tenant", "POST", "/api/v1/tenants",
         {"name": "demo-f01-gw", "isolation_tier": "k8s_namespace"}, OPS)
tid = "tnt-demo-f01-gw"
r = step("게이트웨이 API 키 / gateway API key", "POST", "/api/v1/token/keys",
         {"tenant_id": tid, "name": "cli-demo"}, OPS)
if r.status_code != 201:
    sys.exit(f"key issue failed: {r.text}")
key = r.json()["key"]
say(f"{r.json()['id']} {r.json()['prefix']} (secret shown once)")
K = {"Authorization": f"Bearer {key}"}

r = step("모델 목록 / list models", "GET", "/v1/models", headers=K)
say(", ".join(m["id"] for m in r.json()["data"]))

r = step("채팅 완성 / chat completion", "POST", "/v1/chat/completions", chat(),
         {**K, "x-request-id": "cli-f01-chat"})
b = r.json()
say(f"engine={b['neocloud']['engine']} deployment={b['neocloud']['deployment']} "
    f"TTFT={b['neocloud']['ttft_ms']}ms TPOT={b['neocloud']['tpot_ms']}ms")
say(f"usage={ {k: b['usage'][k] for k in ('prompt_tokens', 'cached_tokens', 'completion_tokens', 'total_tokens')} }")
say(f"text: {b['choices'][0]['message']['content']!r}")

r = step("SSE 스트리밍 / streaming", "POST", "/v1/chat/completions", chat(stream=True),
         {**K, "x-request-id": "cli-f01-sse"})
ch = sse(r.text)
print("  first: " + json.dumps(ch[0], ensure_ascii=False)[:160])
print("  last : " + json.dumps(ch[-2], ensure_ascii=False)[:200])
say(f"{len(ch) - 1} chunks, usage in last chunk, terminator {ch[-1]}")

for i in (1, 2):
    r = step(f"멱등 재시도 {i}/2 / idempotent retry", "POST", "/v1/chat/completions", chat(),
             {**K, "x-request-id": "cli-f01-retry"})
u = c.get("/api/v1/token/usage", params={"tenant_id": tid}, headers=OPS).json()
say(f"usage records for cli-f01-retry: "
    f"{sum(1 for x in u['records'] if x['request_id'] == 'cli-f01-retry')} (2 calls, billed once)")

rid = "cli-f03-cut"
words = [WORDS[zlib.crc32(f"{rid}:{i}".encode()) % len(WORDS)] for i in range(40)]
term = next(w for i, w in enumerate(words) if i >= 3 and w not in words[:i])
step("가드 enforce + 차단어 / guard enforce", "PUT", f"/api/v1/token/guard-policy/{tid}",
     {"mode": "enforce", "blocked_terms": [term]}, OPS)
r = step("PII 입력 차단 / PII input blocked", "POST", "/v1/chat/completions",
         chat(text="제 주민번호 900101-1234567 확인해줘"), K)
say(json.dumps(r.json(), ensure_ascii=False))
r = step("출력 가드 스트림 차단 / output guard cut", "POST", "/v1/chat/completions",
         chat(stream=True, max_tokens=40), {**K, "x-request-id": rid})
ch = [x for x in sse(r.text) if isinstance(x, dict)]
text = "".join(x["choices"][0]["delta"].get("content", "") for x in ch if x["choices"])
say(f"blocked term {term!r}; delivered {ch[-1]['usage']['completion_tokens']}/40 tokens, "
    f"finish_reason={ch[-2]['choices'][0]['finish_reason']}: {text!r}")

step("공공 테넌트 / public-sector tenant", "POST", "/api/v1/tenants",
     {"name": "gov-demo-agency", "isolation_tier": "k8s_namespace"}, OPS)
r = step("공공 정책 완화 시도 / relax public policy", "PUT",
         "/api/v1/token/guard-policy/tnt-gov-demo-agency", {"mode": "monitor"}, OPS)
say(f"refused: {r.json()}")
r = step("추론 SLO / inference SLO", "GET", "/api/v1/token/slo", headers=OPS, params={"model": MODEL})
s = r.json()
say(f"TTFT p99 {s['ttft_p99_ms']} ms, TPOT p99 {s['tpot_p99_ms']} ms vs {s['target']} → "
    f"breach={s['breach']} (source {s['source']})")
