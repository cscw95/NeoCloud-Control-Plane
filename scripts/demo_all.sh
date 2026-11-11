#!/usr/bin/env bash
# Run every registered demo scenario against a running NOCP and print a pass/fail table.
# Starts nothing: point it at a stack you already run.
#
#   scripts/demo_all.sh [NOCP_URL] [--reset] [--reset-all] [--only <id>[,<id>]] [--day 0|1|2]
#                       [--reverse] [--steps] [--lang ko|en]
#
#   NOCP_URL     default $NOCP_URL or http://127.0.0.1:8000
#   --reset      POST /api/v1/demo/reset before each scenario (deterministic NOCP seed)
#   --reset-all  same, scope=all (also resets NICo with cascade and AI-Infra)
#   --only       comma list of scenario ids
#   --day        only one demo day (Day 0 build · 1 turn-up · 2 operate)
#   --reverse    run in reverse presentation order (isolation check: any order passes)
#   --steps      print every step: request line, response summary, assertion
#   --lang       narration language for --steps (default ko)
#   NOCP_TOKEN   operator bearer token (omit on a dev stack with NOCP_DEV_OPERATOR_DEFAULT=1)
#
# Exit code: 0 when no step failed (pending/skipped allowed), 1 otherwise.
set -euo pipefail

URL="${NOCP_URL:-http://127.0.0.1:8000}"
RESET=""; ONLY=""; STEPS=0; LANG_="ko"; DAY=""; REV=0
while [ $# -gt 0 ]; do
  case "$1" in
    --reset) RESET="nocp" ;;
    --reset-all) RESET="all" ;;
    --only) ONLY="$2"; shift ;;
    --day) DAY="$2"; shift ;;
    --reverse) REV=1 ;;
    --steps) STEPS=1 ;;
    --lang) LANG_="$2"; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    http*) URL="$1" ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
URL="${URL%/}"
AUTH=()
[ -n "${NOCP_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer ${NOCP_TOKEN}")

api() {  # api METHOD PATH [JSON]
  local m="$1" p="$2" d="${3:-}"
  if [ -n "$d" ]; then
    curl -sS --max-time 300 -X "$m" ${AUTH[@]+"${AUTH[@]}"} -H 'Content-Type: application/json' -d "$d" "$URL$p"
  else
    curl -sS --max-time 300 -X "$m" ${AUTH[@]+"${AUTH[@]}"} "$URL$p"
  fi
}

if ! curl -sS --max-time 5 "$URL/health" >/dev/null 2>&1; then
  echo "NOCP not reachable at $URL (this script starts nothing)" >&2
  exit 2
fi

LIST="$(api GET "/api/v1/demo/scenarios${DAY:+?day=$DAY}")"
IDS="$(printf '%s' "$LIST" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if "scenarios" not in d:
    sys.exit("demo runner answered: %s" % d)
for k, v in d.get("load_errors", {}).items():
    print("load error %s: %s" % (k, v), file=sys.stderr)
only = [x for x in sys.argv[1].split(",") if x]
for s in d["scenarios"]:
    if not only or s["id"] in only:
        print(s["id"])
' "$ONLY")"
if [ "$REV" = 1 ]; then
  IDS="$(printf '%s\n' "$IDS" | awk '{a[NR]=$0} END {for (i = NR; i > 0; i--) print a[i]}')"
fi

RESULTS="$(mktemp)"
trap 'rm -f "$RESULTS"' EXIT
echo "NOCP $URL · $(printf '%s\n' "$IDS" | grep -c .) scenario(s)"
for id in $IDS; do
  if [ -n "$RESET" ]; then
    api POST /api/v1/demo/reset "{\"scope\":\"$RESET\"}" >/dev/null
  fi
  RUN="$(api POST "/api/v1/demo/scenarios/$id/run?wait=true" '{"mode":"all"}')"
  printf '%s\n' "$RUN" | python3 -c '
import json, sys
steps, lang = int(sys.argv[1]), sys.argv[2]
r = json.load(sys.stdin)
if "steps" not in r:
    print(json.dumps({"id": sys.argv[3], "verdict": "error", "counts": {}, "ms": 0, "err": str(r)[:200]}))
    sys.exit(0)
icon = {"passed": "✔", "failed": "✘", "pending": "…", "skipped": "↷", "waiting": " ", "running": "▶"}
if steps:
    print("\n── %s · %s" % (r["scenario_id"], r["title_" + lang]), file=sys.stderr)
    for s in r["steps"]:
        req, res, a = s.get("request") or {}, s.get("response") or {}, s.get("assertion") or {}
        print("  %s %-22s %s" % (icon.get(s["state"], "?"), s["id"], s["title_" + lang]), file=sys.stderr)
        print("      %s" % s["narration_" + lang], file=sys.stderr)
        if req:
            print("      $ %s %s" % (req.get("method"), req.get("url")), file=sys.stderr)
        if res:
            print("      → %s" % res.get("summary"), file=sys.stderr)
        print("      = %s  (%s ms)" % (a.get("detail_" + lang, ""), s.get("duration_ms")), file=sys.stderr)
print(json.dumps({"id": r["scenario_id"], "verdict": r["verdict"], "counts": r["counts"], "ms": r["total_ms"]}))
' "$STEPS" "$LANG_" "$id" >>"$RESULTS"
done

python3 - "$RESULTS" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
hdr = ("scenario", "verdict", "pass", "fail", "pend", "skip", "ms")
print()
print("%-26s %-18s %5s %5s %5s %5s %8s" % hdr)
print("-" * 78)
bad = 0
for r in rows:
    c = r.get("counts", {})
    bad += c.get("failed", 0) + (r["verdict"] == "error")
    print("%-26s %-18s %5s %5s %5s %5s %8s" % (r["id"], r["verdict"], c.get("passed", "-"),
          c.get("failed", "-"), c.get("pending", "-"), c.get("skipped", "-"), r.get("ms", "-")))
    if r.get("err"):
        print("   ", r["err"])
print("-" * 78)
tot = lambda k: sum(r.get("counts", {}).get(k, 0) for r in rows)
print("total: %d passed · %d failed · %d pending · %d skipped" % (tot("passed"), tot("failed"),
      tot("pending"), tot("skipped")))
sys.exit(1 if bad else 0)
PY
