"""Helpers shared by the F33/F34/F35 scenarios: the demo runner's http/check
when it is merged (feat/v2-demo app/demo/engine.py), the same shapes when it
is not, and run_local() for the terminal scripts (no SCENARIO here, so the
runner's loader skips this module)."""

from __future__ import annotations

import json

try:                                      # demo runner (v2-demo)
    from ..engine import check, http      # noqa: F401
except ImportError:
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


class Res:
    def __init__(self, status, body, headers=None):
        self.status, self.body, self.headers = status, body, headers or {}


def body(res) -> dict:
    return res.body if isinstance(res.body, dict) else {}


def _resolve(v, ctx):
    if callable(v):
        return v(ctx)
    if isinstance(v, dict):
        return {k: _resolve(x, ctx) for k, x in v.items()}
    return v


def _judge(expect, res, ctx):
    if expect is None:
        return res.status < 400, f"HTTP {res.status}", f"HTTP {res.status}"
    if isinstance(expect, dict):
        want = expect["status"]
        want = want if isinstance(want, list) else [want]
        return res.status in want, f"HTTP {res.status}", f"HTTP {res.status}"
    out = expect(res, ctx)
    return out if isinstance(out, tuple) else (bool(out), "", "")


def run_local(scenario, send, out=print, lang="ko") -> bool:
    """Run a SCENARIO with send(method, path, json, params, service, headers) ->
    (status, headers, body); prints curl line, response summary, verdict."""
    ctx: dict = {}
    ok_all = True
    steps = scenario["steps"]
    for i, st in enumerate(steps, 1):
        out(f"\n[{i:02d}/{len(steps)}] {st['title_ko' if lang == 'ko' else 'title_en']}")
        call = st["call"]
        if callable(call):
            res = Res(200, call(ctx))
            out(f"  (local) {json.dumps(res.body, ensure_ascii=False)[:300]}")
        else:
            spec = _resolve(call, ctx)
            m, path, svc = spec["method"], spec["path"], spec.get("service", "nocp")
            b, params = spec.get("json"), spec.get("params")
            q = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
            var = {"ai": "$AI_INFRA", "nico": "$NICO"}.get(svc, "$NOCP")
            out(f"  $ curl -s -X {m} '{var}{path}{q}'"
                + (f" -H 'Content-Type: application/json' -d '"
                   f"{json.dumps(b, ensure_ascii=False)}'" if b is not None else ""))
            who = spec.get("as") or "operator"
            hdrs = {"X-Tenant-Id": who[7:]} if who.startswith("tenant:") else None
            status, headers, rbody = send(m, path, b, params, svc, hdrs)
            res = Res(status, rbody, headers)
            text = json.dumps(rbody, ensure_ascii=False)
            out(f"  ← HTTP {status} {text[:300]}{'…' if len(text) > 300 else ''}")
        try:
            ok, ko, en = _judge(st.get("expect"), res, ctx)
        except (KeyError, TypeError, IndexError, StopIteration) as e:
            ok, ko, en = False, f"검증 오류 {e!r}", f"check error {e!r}"
        out(f"  {'✔' if ok else '✘'} {ko if lang == 'ko' else en}")
        if ok and st.get("save"):
            st["save"](res, ctx)
        ok_all &= bool(ok)
        if not ok:
            break
    return ok_all
