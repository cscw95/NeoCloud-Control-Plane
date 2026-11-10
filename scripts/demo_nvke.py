#!/usr/bin/env python3
"""Terminal demo for F11 (NVKE depth: CAPI/CAPNICo, IMEX ComputeDomain, Topograph).

Runs app/demo/scenarios/f11_nvke.py against a live NOCP and prints, per step,
the equivalent curl command, HTTP status, a response summary and the KO/EN
verdict. `--raw` also prints the CAPNICo → NICo requests of one NicoMachine and
one ComputeDomain object as the upstream CRD shape.

  python scripts/demo_nvke.py                          # NOCP :8000, Korean
  python scripts/demo_nvke.py --nocp http://127.0.0.1:20062 --lang en --raw
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


def load() -> dict:
    path = ROOT / "app" / "demo" / "scenarios" / "f11_nvke.py"
    spec = importlib.util.spec_from_file_location("f11_nvke", path)
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
        keys = [k for k in ("id", "state", "status", "node_count", "failure_domain",
                            "old_host", "new_host") if k in body]
        return ", ".join(f"{k}={body[k]}" for k in keys) or ", ".join(list(body)[:6])
    if isinstance(body, list):
        return f"[{len(body)} items]"
    return str(body)[:120]


def raw_dump(c: httpx.Client, cid: str) -> None:
    capi = c.get(f"/api/v1/nvke/clusters/{cid}/capi", params={"objects": "true"}).json()
    nm = next(o for o in capi["objects"] if o["kind"] == "NicoMachine")
    print("\n  --- CAPNICo → NICo (one NicoMachine) ---")
    for r in nm["x-neocloud"]["nico_requests"]:
        print(f"  → {r['method']} {r['path']}  ← {r['status']}")
        if r.get("body"):
            print(f"    req  {json.dumps(r['body'], ensure_ascii=False)}")
        print(f"    resp {json.dumps(r['result'], ensure_ascii=False)}")
    cd = c.get(f"/api/v1/nvke/clusters/{cid}/compute-domains").json()[0]["computeDomain"]
    cd = {**cd, "status": {**cd["status"], "nodes": cd["status"]["nodes"][:2]}}
    print("\n  --- ComputeDomain (resource.nvidia.com/v1beta1, 2 of the nodes shown) ---")
    print("  " + json.dumps(cd, ensure_ascii=False, indent=1).replace("\n", "\n  "))


def run(sc: dict, base: str, lang: str, raw: bool) -> bool:
    ctx, ok_all = {}, True
    print(f"\n=== {sc['title_' + lang]} ({sc['feature']}) ===")
    with httpx.Client(base_url=base, timeout=120.0,
                      headers={"X-Operator": os.environ.get("DEMO_OPERATOR", "ops")}) as c:
        for st in sc["steps"]:
            call = st["call"]
            v = lambda x: x(ctx) if callable(x) else x  # noqa: E731
            path, body, params = v(call["path"]), v(call.get("json")), v(call.get("params"))
            url = base + path + ("?" + "&".join(f"{k}={x}" for k, x in params.items())
                                 if params else "")
            print(f"\n[{st['id']}] {st['title_' + lang]}\n  $ {curl(call['method'], url, body)}")
            res = Res(c.request(call["method"], path, json=body, params=params))
            ok, ko, en = st["expect"](res, ctx)
            if st.get("save") and ok:
                st["save"](res, ctx)
            ok_all &= bool(ok)
            print(f"  ← HTTP {res.status}  {summary(res.body)}")
            print(f"  {'PASS' if ok else 'FAIL'}  {ko if lang == 'ko' else en}")
            if raw and st["id"] == "04-imex" and ok:
                raw_dump(c, ctx["cluster_id"])
    return ok_all


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nocp", default=os.environ.get("NOCP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--lang", choices=("ko", "en"), default="ko")
    ap.add_argument("--raw", action="store_true", help="print CAPNICo requests and a CRD object")
    a = ap.parse_args()
    ok = run(load(), a.nocp.rstrip("/"), a.lang, a.raw)
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
