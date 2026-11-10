"""Demo runner engine — executes registered demo scenarios step by step.

Scenario format (WAVE-v2.md "시연(데모) 요건"): `app/demo/scenarios/<feature>.py`
defines

    SCENARIO = {"id", "title_ko", "title_en", "feature": "F0x",
                "summary_ko"?, "summary_en"?, "order"?,
                "steps": [{"id", "title_ko", "title_en", "call", "expect", ...}]}

Step keys
  call        dict HTTP spec {"method", "path", "json"?, "params"?, "as"?,
              "service"?: "nocp"|"ai"|"nico", "headers"?} — any value may be a
              callable(ctx); or a callable(ctx) for an internal function call.
              `as`: "operator" (default) | "tenant:<tenant_id>" | "none".
  expect      callable(res, ctx) -> bool | None | (bool|None, detail_ko, detail_en)
              (None = skipped, e.g. an upstream emulator is offline), or a dict
              {"status": 201} / {"status": [200, 201]}. Default: status < 400.
  save        callable(res, ctx) — copy values into the run context.
  feature     "F05" — the v2 feature that owns the endpoint.
  requires    ["POST /api/v1/billing/invoices:run", ...] — when a route is not
              registered in this NOCP build the step reports `pending` ("pending
              feature F05") instead of failing.
  fallback    call spec run when the step is pending (pre-v2 equivalent), so later
              steps still have data; `save` is applied to its result.
  narration_ko / narration_en, expect_ko / expect_en, links [{href, label_ko, label_en}]

A step whose call or expectation needs a context key an earlier pending/skipped step
never produced (KeyError) is reported as `pending` too, never as `failed`.

Calls to NOCP itself run in-process through Starlette's TestClient (no lifespan, so
the live store is shared) with a freshly minted bearer token, so they go through the
real middleware (tenancy guard, CORS-less) exactly like an external client.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .. import auth

STEP_STATES = ("waiting", "running", "passed", "failed", "pending", "skipped")
_BODY_LIMIT = 6000
_SHOWN_HEADERS = ("content-type", "x-request-id", "retry-after", "location",
                  "x-ratelimit-remaining-tokens", "x-ratelimit-remaining-requests",
                  "x-ratelimit-limit-tokens", "x-ratelimit-limit-requests")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Pending(Exception):
    """Raised by a scenario helper when a dependency is not there yet."""

    def __init__(self, feature: str = "", why_ko: str = "", why_en: str = ""):
        super().__init__(feature)
        self.feature, self.why_ko, self.why_en = feature, why_ko, why_en


class Result:
    """Normalised outcome of one call — what `expect` and `save` see."""

    def __init__(self, status: int, body: Any = None, headers: Optional[dict] = None,
                 text: str = ""):
        self.status = status
        self.body = body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.text = text

    @property
    def json(self) -> Any:
        return self.body

    def __getitem__(self, k):
        return self.body[k]

    def get(self, k, default=None):
        return self.body.get(k, default) if isinstance(self.body, dict) else default


# ── route registry (feature detection) ─────────────────────────────────────

def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "demo-x", path.split("?")[0])


def has_route(method: str, path: str) -> bool:
    """True when this NOCP build serves `METHOD path` (template params allowed).
    Uses the router's own matcher, so it works whatever FastAPI's route tree is."""
    from starlette.routing import Match
    from ..main import app
    scope = {"type": "http", "method": method.upper(), "path": _concrete(path),
             "root_path": "", "query_string": b"", "headers": []}
    for r in app.router.routes:
        m, _ = r.matches(scope)
        if m == Match.FULL:
            return True
    return False


def missing_routes(requires: list[str]) -> list[str]:
    miss = []
    for req in requires or ():
        method, _, path = req.partition(" ")
        if not has_route(method, path):
            miss.append(req)
    return miss


# ── call execution ─────────────────────────────────────────────────────────

def _resolve(v: Any, ctx: dict) -> Any:
    if callable(v):
        return v(ctx)
    if isinstance(v, dict):
        return {k: _resolve(x, ctx) for k, x in v.items()}
    if isinstance(v, list):
        return [_resolve(x, ctx) for x in v]
    return v


def _token_for(as_: str) -> Optional[str]:
    if as_ in (None, "", "operator"):
        return auth.mint("operator", ttl_s=600)
    if as_ == "none":
        return None
    if as_.startswith("tenant:"):
        _, _, rest = as_.partition(":")
        tid, _, role = rest.partition(":")
        return auth.mint("tenant", tenant_id=tid, tenant_role=role or "admin", ttl_s=600)
    raise ValueError(f"unknown identity '{as_}'")


def _redact(h: dict) -> dict:
    out = {}
    for k, v in h.items():
        if k.lower() in ("authorization", "x-api-key"):
            v = v.split(" ")[0] + " …(redacted)" if " " in v else "…(redacted)"
        out[k] = v
    return out


def _clip(body: Any) -> Any:
    try:
        text = json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(body)
    if len(text) <= _BODY_LIMIT:
        return body
    return text[:_BODY_LIMIT] + f" …(+{len(text) - _BODY_LIMIT} chars)"


class Client:
    """One per run: in-process NOCP client + upstream emulator calls."""

    def __init__(self):
        from fastapi.testclient import TestClient
        from ..main import app
        self.tc = TestClient(app, base_url="http://nocp.demo", raise_server_exceptions=False)

    def http(self, spec: dict) -> tuple[Result, dict]:
        method = spec.get("method", "GET").upper()
        path = spec["path"]
        service = spec.get("service", "nocp")
        body = spec.get("json")
        params = spec.get("params")
        if service != "nocp":
            return self._upstream(service, method, path, body, params)
        headers = dict(spec.get("headers") or {})
        tok = _token_for(spec.get("as", "operator"))
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        r = self.tc.request(method, path, json=body, params=params, headers=headers)
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            try:
                parsed = r.json()
            except ValueError:
                parsed = r.text
        else:
            parsed = r.text
        res = Result(r.status_code, parsed, dict(r.headers), r.text)
        req = {"service": "nocp", "method": method, "url": str(r.request.url),
               "headers": _redact(headers), "body": body}
        return res, req

    def _upstream(self, service: str, method: str, path: str, body, params):
        from .. import dsx
        base = dsx.ai_infra_url() if service == "ai" else dsx.nico_url()
        st, resp = dsx.call(method, base + path, json=body, params=params)
        req = {"service": service, "method": method, "url": base + path,
               "headers": _redact(dsx.emu_headers()), "body": body}
        if st is None:
            return Result(0, {"error": str(resp)}), req
        return Result(st, resp), req


def _summary(res: Result) -> str:
    b = res.body
    if isinstance(b, dict):
        keys = list(b.keys())[:8]
        return f"HTTP {res.status} · {{{', '.join(keys)}{', …' if len(b) > 8 else ''}}}"
    if isinstance(b, list):
        return f"HTTP {res.status} · [{len(b)} items]"
    if isinstance(b, str):
        first = b.strip().splitlines()[0] if b.strip() else ""
        return f"HTTP {res.status} · {first[:80]}"
    return f"HTTP {res.status}"


# ── runs ───────────────────────────────────────────────────────────────────

class Run:
    def __init__(self, run_id: str, scenario: dict, mode: str):
        self.id = run_id
        self.scenario = scenario
        self.mode = mode
        self.state = "ready"                     # ready | running | paused | done
        self.created_at = _now()
        self.finished_at: Optional[str] = None
        self.ctx: dict = {"run_id": run_id,
                          "period": datetime.now(timezone.utc).strftime("%Y-%m")}
        self.cursor = 0
        self.steps = [self._blank(s) for s in scenario["steps"]]
        self.events: list[dict] = []
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.client: Optional[Client] = None
        self._emit("run", {"state": self.state})

    @staticmethod
    def _blank(s: dict) -> dict:
        return {"id": s["id"], "title_ko": s["title_ko"], "title_en": s["title_en"],
                "narration_ko": s.get("narration_ko") or s["title_ko"],
                "narration_en": s.get("narration_en") or s["title_en"],
                "expect_ko": s.get("expect_ko", ""), "expect_en": s.get("expect_en", ""),
                "feature": s.get("feature"), "links": s.get("links", []),
                "state": "waiting", "request": None, "response": None,
                "assertion": None, "started_at": None, "duration_ms": None,
                "fallback": False}

    def _emit(self, kind: str, data: dict) -> None:
        with self.cond:
            self.events.append({"seq": len(self.events) + 1, "kind": kind,
                                "at": _now(), "data": data})
            self.cond.notify_all()

    # ── one step ────────────────────────────────────────────────────────────
    def step(self) -> Optional[dict]:
        with self.lock:
            if self.cursor >= len(self.steps):
                return None
            i = self.cursor
            self.cursor += 1
            self.state = "running"
            if self.client is None:
                self.client = Client()
        spec = self.scenario["steps"][i]
        view = self.steps[i]
        view["state"] = "running"
        view["started_at"] = _now()
        self._emit("step", {"index": i, "step": dict(view)})
        t0 = time.perf_counter()
        try:
            self._execute(spec, view)
        except Exception as exc:                 # noqa: BLE001 — report, never crash
            view["state"] = "failed"
            view["assertion"] = {"ok": False, "detail_ko": f"실행 오류: {exc}",
                                 "detail_en": f"execution error: {exc}"}
        view["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._emit("step", {"index": i, "step": dict(view)})
        with self.lock:
            if self.cursor >= len(self.steps):
                self.state = "done"
                self.finished_at = _now()
                self._emit("done", self.summary())
            else:
                self.state = "paused" if self.mode == "step" else "running"
        return view

    def _pending(self, view: dict, feature: str, ko: str, en: str) -> None:
        view["state"] = "pending"
        f = feature or "?"
        view["assertion"] = {"ok": None,
                             "detail_ko": f"pending feature {f} — {ko}",
                             "detail_en": f"pending feature {f} — {en}"}

    def _call(self, call, ctx: dict) -> tuple[Result, Optional[dict]]:
        if callable(call):
            out = call(ctx)
            if isinstance(out, Result):
                return out, {"service": "internal", "method": "CALL",
                             "url": getattr(call, "__qualname__", "internal"), "body": None}
            return Result(200, out), {"service": "internal", "method": "CALL",
                                      "url": getattr(call, "__qualname__", "internal"),
                                      "body": None}
        spec = _resolve(call, ctx)
        return self.client.http(spec)

    def _execute(self, spec: dict, view: dict) -> None:
        ctx = self.ctx
        feature = spec.get("feature", "")
        miss = missing_routes(spec.get("requires", []))
        if miss:
            self._pending(view, feature, "이 NOCP 빌드에 아직 없는 API: " + ", ".join(miss),
                          "not in this NOCP build yet: " + ", ".join(miss))
            fb = spec.get("fallback")
            if fb is not None:
                try:
                    res, req = self._call(fb, ctx)
                    view["request"], view["response"] = req, self._resp(res)
                    view["fallback"] = True
                    if res.status < 400 and spec.get("save"):
                        spec["save"](res, ctx)
                        view["assertion"]["detail_ko"] += " · 기존 API로 대체 실행"
                        view["assertion"]["detail_en"] += " · ran the pre-v2 equivalent"
                except (KeyError, Pending):
                    pass
            return
        try:
            res, req = self._call(spec["call"], ctx)
        except KeyError as exc:
            self._pending(view, feature, f"앞 단계 결과({exc})가 없음",
                          f"needs {exc} from an earlier pending step")
            return
        except Pending as p:
            self._pending(view, p.feature or feature, p.why_ko, p.why_en)
            return
        view["request"], view["response"] = req, self._resp(res)
        if res.status == 0:
            view["state"] = "skipped"
            svc = (req or {}).get("service", "upstream")
            view["assertion"] = {"ok": None,
                                 "detail_ko": f"{svc} 에뮬레이터 미연결 — 건너뜀",
                                 "detail_en": f"{svc} emulator unreachable — skipped"}
            return
        if (res.status == 404 and feature and res.body == {"detail": "Not Found"}):
            self._pending(view, feature, "라우트 없음(404)", "route not found (404)")
            return
        try:
            verdict = self._judge(spec.get("expect"), res, ctx)
        except KeyError as exc:
            self._pending(view, feature, f"앞 단계 결과({exc})가 없음",
                          f"needs {exc} from an earlier pending step")
            return
        except Pending as p:
            self._pending(view, p.feature or feature, p.why_ko, p.why_en)
            return
        ok, ko, en = verdict
        if ok is None:
            view["state"] = "skipped"
        else:
            view["state"] = "passed" if ok else "failed"
        view["assertion"] = {"ok": ok, "detail_ko": ko, "detail_en": en}
        if ok and spec.get("save"):
            try:
                spec["save"](res, ctx)
            except (KeyError, TypeError, IndexError) as exc:
                view["state"] = "failed"
                view["assertion"] = {"ok": False,
                                     "detail_ko": f"응답에서 값을 못 읽음: {exc}",
                                     "detail_en": f"could not read the response: {exc}"}

    @staticmethod
    def _resp(res: Result) -> dict:
        h = {k: v for k, v in res.headers.items() if k in _SHOWN_HEADERS}
        return {"status": res.status, "headers": h, "body": _clip(res.body),
                "summary": _summary(res)}

    @staticmethod
    def _judge(expect, res: Result, ctx: dict) -> tuple[Optional[bool], str, str]:
        if expect is None:
            ok = res.status < 400
            return ok, f"HTTP {res.status}", f"HTTP {res.status}"
        if isinstance(expect, dict):
            want = expect.get("status")
            want = want if isinstance(want, (list, tuple)) else [want]
            ok = res.status in want
            return (ok, f"HTTP {res.status} (기대 {', '.join(map(str, want))})",
                    f"HTTP {res.status} (expected {', '.join(map(str, want))})")
        out = expect(res, ctx)
        if isinstance(out, tuple):
            ok, ko, en = (list(out) + ["", ""])[:3]
            return ok, ko or "", en or ko or ""
        ok = None if out is None else bool(out)
        word = {True: ("통과", "passed"), False: ("실패", "failed"),
                None: ("건너뜀", "skipped")}[ok]
        return ok, f"{word[0]} · HTTP {res.status}", f"{word[1]} · HTTP {res.status}"

    def run_all(self, pace_ms: int = 0) -> None:
        with self.lock:
            self.mode = "all" if self.mode != "step" else self.mode
        while True:
            v = self.step()
            if v is None:
                break
            if pace_ms:
                time.sleep(pace_ms / 1000)

    # ── views ───────────────────────────────────────────────────────────────
    def counts(self) -> dict:
        c = {s: 0 for s in STEP_STATES}
        for v in self.steps:
            c[v["state"]] += 1
        return c

    def verdict(self) -> str:
        c = self.counts()
        if c["failed"]:
            return "fail"
        if self.state != "done":
            return "running"
        if c["pending"] or c["skipped"]:
            return "pass_with_pending"
        return "pass"

    def summary(self) -> dict:
        return {"run_id": self.id, "scenario_id": self.scenario["id"],
                "state": self.state, "verdict": self.verdict(), "counts": self.counts(),
                "total_ms": round(sum(v["duration_ms"] or 0 for v in self.steps), 1)}

    def view(self) -> dict:
        s = self.scenario
        with self.lock:
            return {"id": self.id, "scenario_id": s["id"], "feature": s.get("feature"),
                    "title_ko": s["title_ko"], "title_en": s["title_en"],
                    "mode": self.mode, "state": self.state, "verdict": self.verdict(),
                    "created_at": self.created_at, "finished_at": self.finished_at,
                    "cursor": self.cursor, "counts": self.counts(),
                    "total_ms": round(sum(v["duration_ms"] or 0 for v in self.steps), 1),
                    "context": {k: v for k, v in self.ctx.items()
                                if isinstance(v, (str, int, float, bool))},
                    "steps": [dict(v) for v in self.steps]}


class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.runs: dict[str, Run] = {}
        self.seq = 0

    def reset(self) -> None:
        with self.lock:
            self.runs.clear()
            self.seq = 0

    def create(self, scenario: dict, mode: str) -> Run:
        with self.lock:
            self.seq += 1
            rid = f"demo-run-{self.seq:04d}"
            run = Run(rid, scenario, mode)
            self.runs[rid] = run
            while len(self.runs) > 50:            # keep the newest 50
                self.runs.pop(next(iter(self.runs)))
            return run

    def get(self, run_id: str) -> Optional[Run]:
        return self.runs.get(run_id)


RUNNER = Runner()


# ── helpers for scenario authors ──────────────────────────────────────────

def http(method: str, path, json=None, params=None, as_: str = "operator",
         service: str = "nocp", headers=None) -> dict:
    spec = {"method": method, "path": path, "as": as_, "service": service}
    if headers is not None:
        spec["headers"] = headers
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def tenant_as(key: str = "tenant_id") -> Callable[[dict], str]:
    return lambda ctx: f"tenant:{ctx[key]}"


def check(cond: bool, ko: str, en: str) -> tuple[bool, str, str]:
    return bool(cond), ko, en


def won(v) -> str:
    """₩ amount for narration (tolerates a missing field)."""
    try:
        return f"₩{int(v):,}"
    except (TypeError, ValueError):
        return "₩—"


def sse_chunks(text: str) -> list:
    """`data:` payloads of an OpenAI-style SSE body (JSON-decoded, [DONE] kept as str)."""
    import json as _json
    out = []
    for line in (text or "").splitlines():
        if line.startswith("data:"):
            d = line[5:].strip()
            try:
                out.append(_json.loads(d))
            except ValueError:
                out.append(d)
    return out
