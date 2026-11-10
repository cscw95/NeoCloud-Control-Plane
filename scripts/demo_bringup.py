#!/usr/bin/env python3
"""Terminal demo for F23 (rack bring-up), F24 (firmware bundles), F25 (attestation).

Runs the scenario files in app/demo/scenarios/ against a live NOCP and prints, per
step, the equivalent curl command, the HTTP status, a response summary and the
KO/EN verdict. For bring-up runs it also prints the 15 steps with the raw
upstream calls (AI-Infra / NICo) each step made.

  python scripts/demo_bringup.py                       # all three, NOCP :8000
  python scripts/demo_bringup.py f23 --nocp http://127.0.0.1:20102 --lang en
  python scripts/demo_bringup.py f23 --raw              # full upstream request/response
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
NAMES = {"f23": "f23_bringup", "f24": "f24_firmware", "f25": "f25_attestation"}


def load(name: str) -> dict:
    path = ROOT / "app" / "demo" / "scenarios" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SCENARIO


class Res:
    def __init__(self, r: httpx.Response):
        self.status, self.headers = r.status_code, dict(r.headers)
        try:
            self.body = r.json()
        except ValueError:
            self.body = r.text


def curl(method: str, url: str, body) -> str:
    c = f"curl -s -X {method} '{url}' -H 'X-Operator: ops'"
    if body is not None:
        c += f" -H 'Content-Type: application/json' -d '{json.dumps(body, ensure_ascii=False)[:160]}'"
    return c


def summary(body) -> str:
    if isinstance(body, dict):
        keys = [k for k in ("id", "state", "status", "failed_step", "progress", "count",
                            "rack_count", "gpu_count", "attested", "source") if k in body]
        return ", ".join(f"{k}={body[k]}" for k in keys) or ", ".join(list(body)[:6])
    if isinstance(body, list):
        return f"[{len(body)} items]"
    return str(body)[:120]


def run(sc: dict, base: str, lang: str, raw: bool) -> bool:
    ctx, ok_all = {}, True
    print(f"\n=== {sc['title_' + lang]} ({sc['feature']}) ===")
    with httpx.Client(base_url=base, timeout=60.0,
                      headers={"X-Operator": os.environ.get("DEMO_OPERATOR", "ops")}) as c:
        for st in sc["steps"]:
            call = st["call"]
            v = lambda x: x(ctx) if callable(x) else x  # noqa: E731
            path, body, params = v(call["path"]), v(call.get("json")), v(call.get("params"))
            url = base + path + ("?" + "&".join(f"{k}={x}" for k, x in params.items())
                                 if params else "")
            print(f"\n[{st['id']}] {st['title_' + lang]}\n  $ {curl(call['method'], url, body)}")
            res = Res(c.request(call["method"], path, json=body, params=params))
            exp = st["expect"]
            verdict = exp(res, ctx) if callable(exp) else (res.status == exp.get("status"), "", "")
            ok, ko, en = verdict if isinstance(verdict, tuple) else (verdict, "", "")
            if st.get("save") and ok:
                st["save"](res, ctx)
            ok_all &= bool(ok)
            print(f"  ← HTTP {res.status}  {summary(res.body)}")
            print(f"  {'PASS' if ok else 'FAIL'}  {ko if lang == 'ko' else en}")
            if isinstance(res.body, dict) and len(res.body.get("steps") or []) == 15:
                for s in res.body["steps"]:
                    print(f"     {s['no']:02d} {s['status']:<8} {s['key']:<21} {s.get('detail') or ''}")
                    for k in s.get("calls") or []:
                        print(f"          → {k['method']} {k['url']}  ← {k['status']}")
                        if raw:
                            print(f"            req  {json.dumps(k.get('request'), ensure_ascii=False)}")
                            print(f"            resp {json.dumps(k.get('response'), ensure_ascii=False)}")
    return ok_all


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="*", default=["f23", "f24", "f25"])
    ap.add_argument("--nocp", default=os.environ.get("NOCP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--lang", choices=("ko", "en"), default="ko")
    ap.add_argument("--raw", action="store_true", help="print raw upstream request/response")
    a = ap.parse_args()
    ok = all([run(load(NAMES[w]), a.nocp.rstrip("/"), a.lang, a.raw) for w in a.which])
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
