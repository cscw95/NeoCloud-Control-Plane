#!/usr/bin/env python3
"""F02 quotas + F04 metering demo in the terminal — quota → override → 429 →
usage events → duplicate → engine sync → reconcile → NICo hours → usage, with
each curl command and a response summary.

    ./run.sh                                         # NOCP (dev: NOCP_DEV_OPERATOR_DEFAULT=1)
    .venv/bin/python scripts/demo_quota_meter.py     # default http://127.0.0.1:8000
    .venv/bin/python scripts/demo_quota_meter.py --base http://127.0.0.1:20022 --lang en
    .venv/bin/python scripts/demo_quota_meter.py --local   # in-process, no server
    NOCP_TOKEN=<operator bearer> .venv/bin/python scripts/demo_quota_meter.py

Steps are app/demo/scenarios/f02_f04_quota_metering.py SCENARIO (the same
steps the Demo Center runs). Exit code 0 = every check passed.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.demo.scenarios.f02_f04_quota_metering import run_local  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=os.environ.get("NOCP", "http://127.0.0.1:8000"))
    ap.add_argument("--token", default=os.environ.get("NOCP_TOKEN"))
    ap.add_argument("--lang", choices=("ko", "en"), default="ko")
    ap.add_argument("--local", action="store_true", help="run in-process (TestClient)")
    a = ap.parse_args()
    headers = {"Authorization": f"Bearer {a.token}"} if a.token else {}
    if a.local:
        os.environ.setdefault("NOCP_DSX_OFFLINE", "1")
        os.environ.setdefault("NOCP_DEV_OPERATOR_DEFAULT", "1")
        from fastapi.testclient import TestClient
        from app.main import app
        cli = TestClient(app, headers=headers)
        cli.__enter__()
    else:
        import httpx
        cli = httpx.Client(base_url=a.base, headers=headers, timeout=30)

    def send(method, path, body, params, hdr):
        r = cli.request(method, path, json=body, params=params, headers=hdr or None)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, dict(r.headers), data

    print(f"NOCP = {'in-process' if a.local else a.base}  (export NOCP=... to paste the curl lines)")
    ok = run_local(send, lang=a.lang)
    print("\n" + ("✔ 모든 검증 통과" if a.lang == "ko" else "✔ all checks passed")
          if ok else "\n✘ FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
