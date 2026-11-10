#!/usr/bin/env python3
"""Terminal demo for F28 (Health Validation rules · readiness gate) and F31 (Slurm/Slinky).

Runs app/demo/scenarios/f28_health_validation.py and f31_slurm.py against a live NOCP and
prints, per step, the equivalent curl command, the HTTP status, the raw response (clipped)
and the KO/EN verdict.

  python scripts/demo_f28_f31.py                          # both, NOCP :8000
  python scripts/demo_f28_f31.py f31 --nocp http://127.0.0.1:20122 --lang en
  python scripts/demo_f28_f31.py --raw                    # full response bodies
NOCP_TOKEN=<bearer> is sent when set (otherwise the dev operator default applies).
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
NAMES = {"f28": "f28_health_validation", "f31": "f31_slurm"}


def load(name: str) -> dict:
    path = ROOT / "app" / "demo" / "scenarios" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SCENARIO


class Res:
    def __init__(self, status: int, body, headers=None):
        self.status, self.body, self.headers = status, body, headers or {}


def resolve(v, ctx):
    if callable(v):
        return v(ctx)
    if isinstance(v, dict):
        return {k: resolve(x, ctx) for k, x in v.items()}
    if isinstance(v, list):
        return [resolve(x, ctx) for x in v]
    return v


def run_steps(scenario: dict, send) -> list[tuple]:
    """Run steps with ``send(method, path, json) -> Res``; returns (step, res, ok, ko, en)."""
    ctx: dict = {}
    out = []
    for st in scenario["steps"]:
        call = resolve(st["call"], ctx)
        res = send(call["method"], call["path"], call.get("json"))
        verdict = st["expect"](res, ctx) if st.get("expect") else (res.status < 400, "", "")
        ok, ko, en = verdict if isinstance(verdict, tuple) else (verdict, "", "")
        if ok and st.get("save"):
            st["save"](res, ctx)
        out.append((st, call, res, ok, ko, en))
        if not ok:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="*", help="f28 and/or f31 (default: both)")
    ap.add_argument("--nocp", default=os.environ.get("NOCP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--lang", default="ko", choices=["ko", "en"])
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()
    a.which = a.which or list(NAMES)
    if set(a.which) - set(NAMES):
        ap.error(f"choose from {list(NAMES)}")
    headers = {"Authorization": f"Bearer {os.environ['NOCP_TOKEN']}"} if os.environ.get("NOCP_TOKEN") else {}
    client = httpx.Client(base_url=a.nocp.rstrip("/"), headers=headers, timeout=30)

    def send(method, path, body):
        r = client.request(method, path, json=body)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return Res(r.status_code, data, dict(r.headers))

    failed = 0
    for key in a.which:
        sc = load(NAMES[key])
        print(f"\n=== {sc['feature']} · {sc['title_' + a.lang]} ===")
        for st, call, res, ok, ko, en in run_steps(sc, send):
            print(f"\n[{st['id']}] {st['title_' + a.lang]}")
            c = f"curl -s -X {call['method']} '{a.nocp}{call['path']}'"
            if call.get("json") is not None:
                c += f" -H 'Content-Type: application/json' -d '{json.dumps(call['json'], ensure_ascii=False)}'"
            print("  $ " + c)
            body = res.body if isinstance(res.body, str) else json.dumps(res.body, ensure_ascii=False)
            print(f"  ← HTTP {res.status} {body if a.raw else body[:400]}")
            print(f"  {'✔' if ok else '✘'} {ko if a.lang == 'ko' else en}")
            failed += 0 if ok else 1
    print(f"\n{'PASS' if not failed else f'FAIL ({failed})'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
