#!/usr/bin/env python3
"""F33/F34/F35 demo in the terminal — each step prints the curl command, a
response summary and the KO/EN verdict.

    .venv/bin/python scripts/demo_kv_comp_cap.py all --ai http://127.0.0.1:20151
    .venv/bin/python scripts/demo_kv_comp_cap.py f35 --base http://127.0.0.1:20152 --lang en

Steps are app/demo/scenarios/f33_kvbm.py, f34_compliance.py, f35_capacity.py
(the same steps the Demo Center runs). Without --base, NOCP runs in this
process (TestClient, dev operator) against the live AI-Infra at --ai, so the
internal steps (F33 metering events, F34 chain check) share its store. With
--base the NOCP HTTP steps go to that server; F33's metering step then only
reaches it through the Demo Center (/api/v1/demo/*).
Exit code 0 = every check passed.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_AI = next((sys.argv[i + 1] for i, x in enumerate(sys.argv[:-1]) if x == "--ai"),
           os.environ.get("AI_INFRA", "http://127.0.0.1:9100"))
os.environ.setdefault("AI_INFRA_URL", _AI)
os.environ.setdefault("NOCP_DEV_OPERATOR_DEFAULT", "1")
os.environ.setdefault("NOCP_DEV_INSECURE_HEADERS", "1")    # X-Tenant-Id (in-process)
from app.demo.scenarios import f33_kvbm, f34_compliance, f35_capacity  # noqa: E402
from app.demo.scenarios._kcc_local import run_local  # noqa: E402

SCEN = {"f33": f33_kvbm.SCENARIO, "f34": f34_compliance.SCENARIO,
        "f35": f35_capacity.SCENARIO}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("which", choices=[*SCEN, "all"])
    ap.add_argument("--base", default=os.environ.get("NOCP"),
                    help="live NOCP; default: NOCP in this process")
    ap.add_argument("--ai", default=_AI)
    ap.add_argument("--token", default=os.environ.get("NOCP_TOKEN"))
    ap.add_argument("--lang", choices=("ko", "en"), default="ko")
    a = ap.parse_args()
    headers = {"Authorization": f"Bearer {a.token}"} if a.token else {}
    if a.base:
        nocp = httpx.Client(base_url=a.base, headers=headers, timeout=30)
    else:
        from fastapi.testclient import TestClient
        from app.main import app
        nocp = TestClient(app, base_url="http://nocp.inproc")
    cli = {"nocp": nocp, "ai": httpx.Client(base_url=a.ai, timeout=30)}

    def send(method, path, body, params, service="nocp", hdrs=None):
        r = cli[service].request(method, path, json=body, params=params, headers=hdrs)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, dict(r.headers), data

    print(f"NOCP = {a.base or 'in-process'}  AI_INFRA = {a.ai}")
    ok = True
    for k in (SCEN if a.which == "all" else [a.which]):
        sc = SCEN[k]
        print(f"\n═══ {sc['title_ko' if a.lang == 'ko' else 'title_en']}")
        ok &= run_local(sc, send, lang=a.lang)
    print("\n" + ("✔ 모든 검증 통과" if a.lang == "ko" else "✔ all checks passed")
          if ok else "\n✘ FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
