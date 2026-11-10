#!/usr/bin/env python3
"""Terminal demo for F30 (KAI scheduling) and F32 (marketplace deploy).

  scripts/demo_f30_f32.py f30 [--base http://127.0.0.1:20142] [--lang en] [--raw]
  scripts/demo_f30_f32.py f32 ...
  scripts/demo_f30_f32.py all ...

Without --base NOCP runs in-process (TestClient, offline stub engines). Each step
prints the curl command, the HTTP status and a one-line verdict (KO or EN);
--raw also prints the response body.
"""
import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {"f30": "f30_scheduling.py", "f32": "f32_marketplace.py"}


def _load(fid):
    spec = importlib.util.spec_from_file_location(fid, ROOT / "app/demo/scenarios" / FILES[fid])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SCENARIO


class Res:
    def __init__(self, status, body):
        self.status, self.body = status, body


def _client(base):
    if base:
        import httpx
        tok = os.environ.get("NOCP_TOKEN")
        h = {"Authorization": f"Bearer {tok}"} if tok else {}
        return httpx.Client(base_url=base, headers=h, timeout=30), contextlib_null()
    os.environ.setdefault("NOCP_DSX_OFFLINE", "1")
    os.environ.setdefault("NOCP_DEV_OPERATOR_DEFAULT", "1")
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient
    from app.main import app
    tc = TestClient(app)
    return tc, tc


class contextlib_null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run(fid, client, base, lang, raw):
    sc = _load(fid)
    print(f"\n=== {sc['feature']} · {sc['title_' + lang]}\n    {sc.get('summary_' + lang, '')}\n")
    ctx, ok_all = {}, True
    for st in sc["steps"]:
        call = st["call"]
        v = lambda x: x(ctx) if callable(x) else x  # noqa: E731
        path, body = v(call["path"]), v(call.get("json"))
        cmd = f"curl -s -X {call['method']} {base or '$NOCP'}{path}"
        if body is not None:
            cmd += f" -H 'Content-Type: application/json' -d '{json.dumps(body, ensure_ascii=False)}'"
        print(f"[{st['id']}] {st['title_' + lang]}\n  $ {cmd}")
        if st.get("narration_" + lang):
            print(f"  · {st['narration_' + lang]}")
        r = client.request(call["method"], path, json=body)
        try:
            b = r.json()
        except ValueError:
            b = r.text
        ok, ko, en = st["expect"](Res(r.status_code, b), ctx)
        ok_all &= bool(ok)
        print(f"  → HTTP {r.status_code} {'PASS' if ok else 'FAIL'} — {ko if lang == 'ko' else en}")
        if raw:
            print("  " + json.dumps(b, ensure_ascii=False)[:1500])
        if st.get("save"):
            st["save"](Res(r.status_code, b), ctx)
    return ok_all


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("feature", choices=["f30", "f32", "all"])
    ap.add_argument("--base")
    ap.add_argument("--lang", choices=["ko", "en"], default="ko")
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()
    client, cm = _client(a.base)
    with cm:
        ok = all([run(f, client, a.base, a.lang, a.raw)
                  for f in (["f30", "f32"] if a.feature == "all" else [a.feature])])
    print("\nRESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
