#!/usr/bin/env python3
"""F05/F06 billing demo in the terminal — contract → Lago events → invoice →
Stripe payment, with each curl command and a response summary.

    ./run.sh                                   # NOCP (dev: NOCP_DEV_OPERATOR_DEFAULT=1)
    .venv/bin/python scripts/demo_billing.py   # default http://127.0.0.1:8000
    .venv/bin/python scripts/demo_billing.py --base http://127.0.0.1:20182 --lang en
    NOCP_TOKEN=<operator bearer> .venv/bin/python scripts/demo_billing.py

Steps are app/demo/scenarios/f05_billing.py SCENARIO (the same steps the
Demo Center runs). Exit code 0 = every check passed.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.demo.scenarios.f05_billing import run_local  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=os.environ.get("NOCP", "http://127.0.0.1:8000"))
    ap.add_argument("--token", default=os.environ.get("NOCP_TOKEN"))
    ap.add_argument("--lang", choices=("ko", "en"), default="ko")
    a = ap.parse_args()
    headers = {"Authorization": f"Bearer {a.token}"} if a.token else {}
    cli = httpx.Client(base_url=a.base, headers=headers, timeout=30)

    def send(method, path, body, params):
        r = cli.request(method, path, json=body, params=params)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, dict(r.headers), data

    print(f"NOCP = {a.base}  (export NOCP={a.base} to paste the curl lines)")
    ok = run_local(send, lang=a.lang)
    print("\n" + ("✔ 모든 검증 통과" if a.lang == "ko" else "✔ all checks passed")
          if ok else "\n✘ FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
