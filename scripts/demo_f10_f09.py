#!/usr/bin/env python
"""F10·F09 demo in the terminal: prints each step's curl line, the HTTP status
and the KO/EN result sentence of app/demo/scenarios/f10_f09_recovery_sla.py.

  python scripts/demo_f10_f09.py                    # in-process NOCP (TestClient)
  python scripts/demo_f10_f09.py --base http://127.0.0.1:20072 [--token T]
  --raw   also print the response body (JSON)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class Res:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base")
    ap.add_argument("--token", default=os.environ.get("NOCP_TOKEN"))
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()
    from app.demo.scenarios.f10_f09_recovery_sla import SCENARIO
    if a.base:
        import httpx
        cli = httpx.Client(base_url=a.base, timeout=30)
    else:
        os.environ.setdefault("NOCP_DSX_OFFLINE", "1")
        os.environ.setdefault("NOCP_DEV_OPERATOR_DEFAULT", "1")
        from fastapi.testclient import TestClient
        from app.main import app
        cli = TestClient(app)
        cli.__enter__()
    hdr = {"Authorization": f"Bearer {a.token}"} if a.token else {}
    base = a.base or "http://nocp"
    print(f"== {SCENARIO['title_ko']}\n   {SCENARIO['title_en']}\n")
    ctx, failed = {}, 0
    for st in SCENARIO["steps"]:
        spec = st["call"](ctx) if callable(st["call"]) else st["call"]
        q = "&".join(f"{k}={v}" for k, v in (spec.get("params") or {}).items())
        url = base + spec["path"] + (f"?{q}" if q else "")
        data = f" -H 'Content-Type: application/json' -d '{json.dumps(spec['json'])}'" \
            if spec.get("json") is not None else ""
        print(f"[{st['id']}] {st['title_ko']} / {st['title_en']}", file=sys.stderr)
        print(f"  $ curl -s -X {spec['method']} '{url}'{data}", file=sys.stderr)
        r = cli.request(spec["method"], spec["path"], json=spec.get("json"),
                        params=spec.get("params"), headers=hdr)
        body = r.json() if "json" in r.headers.get("content-type", "") else r.text
        res = Res(r.status_code, dict(r.headers), body)
        exp = st["expect"]
        if callable(exp):
            ok, ko, en = exp(res, ctx)
        else:
            ok, ko, en = r.status_code == exp["status"], "", ""
        if ok and st.get("save"):
            st["save"](res, ctx)
        failed += not ok
        print(f"  HTTP {r.status_code} {'PASS' if ok else 'FAIL'}\n  KO: {ko}\n  EN: {en}")
        if a.raw:
            print("  " + json.dumps(body, ensure_ascii=False)[:2000])
        print()
        if not ok:
            break
    print(f"== {'PASS' if not failed else 'FAIL'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
