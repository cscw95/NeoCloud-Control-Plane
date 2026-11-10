#!/usr/bin/env python3
"""Terminal demo — F13 status notices · F26 Prometheus /metrics · F27 FinOps.

Prints each request as a curl command and a one-line summary of the response.

  python3 scripts/demo_obs_finops.py                      # NOCP :8000, AI :9100, NICo :9000
  NOCP=http://127.0.0.1:20112 AI=http://127.0.0.1:20111 NICO=http://127.0.0.1:20110 \
  NOCP_TOKEN=<operator bearer> python3 scripts/demo_obs_finops.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

NOCP = os.environ.get("NOCP", "http://127.0.0.1:8000").rstrip("/")
AI = os.environ.get("AI", "http://127.0.0.1:9100").rstrip("/")
NICO = os.environ.get("NICO", "http://127.0.0.1:9000").rstrip("/")
TOKEN = os.environ.get("NOCP_TOKEN", "")
FAIL = []


def call(method, url, body=None, auth=True):
    h = {"Accept": "application/json, text/plain"}
    if body is not None:
        h["Content-Type"] = "application/json"
    if auth and TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    curl = f"curl -s -X {method} '{url}'" + (" -H 'Authorization: Bearer $NOCP_TOKEN'" if auth and TOKEN else "")
    if body is not None:
        curl += f" -H 'Content-Type: application/json' -d '{json.dumps(body, ensure_ascii=False)}'"
    print(f"\n$ {curl}")
    req = urllib.request.Request(url, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers.get("content-type", ""), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read().decode()
    except Exception as e:                        # noqa: BLE001
        return None, "", str(e)


def show(step, ok, ko, en=""):
    mark = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
    if ok is False:
        FAIL.append(step)
    print(f"  [{mark}] {step}: {ko}" + (f"  / {en}" if en else ""))


def scrape(step, url, must):
    st, ct, text = call("GET", url, auth=False)
    if st is None:
        return show(step, None, f"미기동 ({text[:60]})", "offline")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from app import prom
    probs = prom.lint(text)
    fams = sum(1 for l in text.splitlines() if l.startswith("# TYPE "))
    missing = [m for m in must if m not in text]
    show(step, st == 200 and not probs and not missing,
         f"HTTP {st} · {ct} · 지표 묶음 {fams}개 · 형식 문제 {len(probs)}건" + (f" · 누락 {missing}" if missing else ""))
    for l in [l for l in text.splitlines() if any(l.startswith(m) for m in must)][:3]:
        print("      " + l[:160])


def main():
    print("== F13 상태 페이지 공지 ==")
    call("POST", f"{NOCP}/api/v1/status/notices/demo/reset")
    t0 = (datetime.now(timezone.utc) + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    st, _, b = call("POST", f"{NOCP}/api/v1/status/notices", {
        "kind": "maintenance", "components": ["Storage"], "start": t0.isoformat(),
        "end": (t0 + timedelta(hours=2)).isoformat(), "message_ko": "VAST Data 점검",
        "message_en": "VAST Data maintenance", "source": "demo"})
    m = json.loads(b) if st and st < 300 else {}
    show("maintenance", m.get("state") == "scheduled", f"HTTP {st} · {m.get('id')} {m.get('state')}")
    st, _, b = call("POST", f"{NOCP}/api/v1/status/notices", {
        "kind": "grid_curtailment", "components": ["bmaas", "token-api"],
        "message_ko": "전력망 감축 대응 중", "message_en": "Grid curtailment in progress", "source": "demo"})
    g = json.loads(b) if st and st < 300 else {}
    show("grid_curtailment", g.get("state") == "active", f"HTTP {st} · {g.get('id')} {g.get('state')}")
    st, _, b = call("GET", f"{NOCP}/api/v1/status", auth=False)
    ns = json.loads(b).get("notices", []) if st == 200 else []
    show("status page", len(ns) >= 2, f"공지 {len(ns)}건: " + ", ".join(f"{n['kind']}={n['state']}" for n in ns[:4]))
    if g.get("id"):
        st, _, b = call("POST", f"{NOCP}/api/v1/status/notices/{g['id']}/resolve", {})
        show("resolve", st == 200 and json.loads(b).get("state") == "resolved", f"HTTP {st}")

    print("\n== F26 Prometheus /metrics ==")
    scrape("NOCP", f"{NOCP}/metrics", ["nocp_sla_availability_ratio", "nocp_status_notices", "nocp_curtailments"])
    scrape("AI-Infra", f"{AI}/metrics", ["neocloud_rack_power_watts", "neocloud_cdu_heat_watts"])
    scrape("AI-Infra DCGM", f"{AI}/metrics?rack=su-1-rack-00", ["DCGM_FI_DEV_BOARD_POWER_WATTS", "DCGM_FI_DEV_XID_ERROR"])
    scrape("NICo", f"{NICO}/metrics", ["nico_machines"])

    print("\n== F27 FinOps ==")
    now = datetime.now(timezone.utc)
    period = f"{now.year - (now.month == 1):04d}-{(now.month - 2) % 12 + 1:02d}"
    for by in ("rack", "tenant", "model"):
        st, _, b = call("GET", f"{NOCP}/api/v1/finops/cost?period={period}&by={by}")
        j = json.loads(b) if st == 200 else {}
        t = j.get("totals", {})
        show(f"cost by {by}", st == 200, f"{period} · {len(j.get('items', []))}건 · 합계 {t.get('total_cost_krw', 0):,}원 "
             f"(전력 {t.get('energy_cost_krw', 0):,} · 감가 {t.get('depreciation_krw', 0):,}) · 전력 출처 {j.get('power_source')}"
             + (f" · {j['meta'].get('note')}" if j.get("meta", {}).get("note") else ""))
    st, _, b = call("GET", f"{NOCP}/api/v1/power/domains")
    doms = json.loads(b).get("domains", []) if st == 200 else []
    if doms:
        st, _, b = call("POST", f"{NOCP}/api/v1/power/flex-events",
                        {"domain": doms[0]["id"], "reduce_kw": 400, "duration_s": 1800, "source": "demo"})
        show("flex inject", st is not None and st < 300, f"HTTP {st} · {doms[0]['id']} −400 kW · 30분")
    st, _, b = call("GET", f"{NOCP}/api/v1/finops/dr-settlements")
    j = json.loads(b) if st == 200 else {}
    it = (j.get("items") or [{}])[-1]
    show("dr settlement", st == 200 and j.get("totals", {}).get("count", 0) > 0,
         f"{j.get('totals', {}).get('count')}건 · 최근 {it.get('achieved_kw')} kW × {it.get('hours')} h = {it.get('kwh')} kWh "
         f"→ {it.get('incentive_krw', 0):,}원 (출처 {j.get('source')})")
    print("\n결과: " + ("모두 통과" if not FAIL else f"실패 {FAIL}"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
