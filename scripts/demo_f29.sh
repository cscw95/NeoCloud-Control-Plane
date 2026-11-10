#!/usr/bin/env bash
# F29 demo: US-02..US-05 reproduced with the NeoCloud CLI, the Terraform provider
# (emulated, tfemu.py) and the generated Python SDK. Read-only on code; writes only
# terraform.tfstate files under packaging/terraform/examples/*.
#
#   scripts/demo_f29.sh [cli|tf|sdk|all]      (default all)
#   NEOCLOUD_BASE_URL=http://127.0.0.1:8000 NICO_URL=http://127.0.0.1:9000 scripts/demo_f29.sh
#
# Needs a dev stack (NOCP_DEV_OPERATOR_DEFAULT=1 / NOCP_DEV_INSECURE_HEADERS=1) with
# F01/F02/F06/F08/F11 merged. Each run uses fresh tenant names (suffix = epoch seconds).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export NEOCLOUD_BASE_URL="${NEOCLOUD_BASE_URL:-http://127.0.0.1:8000}"
NICO_URL="${NICO_URL:-http://127.0.0.1:9000}"
PY="${PYTHON:-python3}"
NC="$ROOT/packaging/bin/neocloud --operator"
TF="$PY $ROOT/packaging/terraform/tfemu.py"
S="$(date +%s)"
PASS=0; FAIL=0
ok()   { echo "  [PASS] $*"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
chk()  { if [ "$1" = "$2" ]; then ok "$3 ($1)"; else bad "$3 (got '$1', want '$2')"; fi; }
run()  { echo "  \$ $*" | sed -e "s#$ROOT/##g" -e 's/sk-neo-[A-Za-z0-9_-]*/sk-neo-***/g' >&2; "$@"; }
step() { echo; echo "── $*"; }

cli() {
  step "CLI · US-02 4랙 12개월 예약 계약 → 계약 주문 / 4-rack 12-month reserved contract → order"
  T=$(run $NC tenants create-tenant -f name=cli-us02-$S -f isolation_tier=bare_metal_dedicated -q id)
  C=$(run $NC contracts create-contract -f tenant_id=$T -f product=bmaas -f commitment=reserved \
        -F racks=4 -f blueprint_key=vr-nvl72 -F term_months=12 -f start_date=2026-11-01 \
        -F unit_price=95000000 -F placement.same_ib_zone=true -q id)
  chk "$(run $NC contracts activate-contract $C -q status)" active "contract $C activate"
  O=$(run $NC orders create-order -f tenant_id=$T -f kind=new -f contract_id=$C -q id)
  chk "$(run $NC orders get-order $O -q state)" delivered "order $O"
  chk "$(run $NC orders get-order $O -q node_ids.length)" 72 "nodes (4 racks x 18 trays)"
  chk "$(run $NC tenants get-tenant $T -q allocations.0.rack_ids.length)" 4 "racks in one allocation (one SU / IB zone)"
  echo "  billing_start: $(run $NC contracts get-contract $C -q orders.0.billing_start)"

  step "CLI · US-03 할당·반납이 NICo 공식 REST로 / allocate and release over NICo REST"
  B0=$(curl -s "$NICO_URL/emulator/v1/bridge-calls" | $PY -c 'import json,sys;print(json.load(sys.stdin)["count"])')
  T=$(run $NC tenants create-tenant -f name=cli-us03-$S -f isolation_tier=bare_metal_dedicated -q id)
  O=$(run $NC orders create-order -f tenant_id=$T -f kind=new -f blueprint_key=vr-nvl72 -F racks=1 -q id)
  A=$(run $NC orders get-order $O -q allocation_ids.0)
  P1=$(curl -s "$NICO_URL/v2/org/neocloud/nico/infiniband-partition?tenantId=$T" | $PY -c 'import json,sys;print(len(json.load(sys.stdin)))')
  chk "$P1" 1 "NICo IB partition after delivery"
  chk "$(run $NC orders create-order -f tenant_id=$T -f kind=terminate -f allocation_id=$A -q state)" closed "release $A"
  P2=$(curl -s "$NICO_URL/v2/org/neocloud/nico/infiniband-partition?tenantId=$T" | $PY -c 'import json,sys;print(len(json.load(sys.stdin)))')
  chk "$P2" 0 "IB partition DELETEd after release"
  B1=$(curl -s "$NICO_URL/emulator/v1/bridge-calls" | $PY -c 'import json,sys;print(json.load(sys.stdin)["count"])')
  chk "$((B1-B0))" 0 "/nico-bridge calls during US-03"

  step "CLI · US-04 2랙 NVKE 노드풀 / 2-rack NVKE node pool"
  T=$(run $NC tenants create-tenant -f name=cli-us04-$S -f isolation_tier=bare_metal_dedicated -q id)
  K=$(run $NC nvke create-cluster -f name=cli-us04 -f tenant_id=$T -f k8s_version=1.33 \
        --body '{"nodepools":[{"name":"gpu","instance_type":"vr-nvl72","count":36}]}' -q id)
  chk "$(run $NC nvke list-nodes $K -q length)" 36 "nodes (x4 GPU = 144)"
  CL=$($NC nvke list-nodes $K | $PY -c 'import json,sys;print(len({n["labels"].get("nvidia.com/gpu.clique") for n in json.load(sys.stdin)}))')
  chk "$CL" 2 "nvidia.com/gpu.clique values"
  chk "$(run $NC nvke list-compute-domains $K -q length)" 2 "ComputeDomains"
  KC=$(run $ROOT/packaging/bin/neocloud --tenant $T nvke kubeconfig $K -q kubeconfig | head -2 | tail -1)
  chk "$KC" "kind: Config" "kubeconfig (owner tenant)"

  step "CLI · US-05 OpenAI 호환 스트림 + 429 / OpenAI-compatible stream + 429"
  T=$(run $NC tenants create-tenant -f name=cli-us05-$S -f isolation_tier=k8s_namespace -q id)
  KEY=$($NC token create-key -f tenant_id=$T -f name=cli -q key)
  echo "  \$ neocloud token create-key -f tenant_id=$T -f name=cli -q key   → ${KEY:0:10}…"
  run $ROOT/packaging/bin/neocloud --api-key "$KEY" chat --stream --max-tokens 16 "NVL72 랙 하나에 GPU가 몇 개인가요?" \
    2> >(sed 's/^/  stderr: /' >&2) && ok "stream with usage in the last chunk (stderr line)" || bad "stream"
  run $NC token put-quota $T -f tier=interactive -F tpm=40 -F rpm=1 -q effective >/dev/null
  RC=0
  for i in 1 2 3; do $ROOT/packaging/bin/neocloud --api-key "$KEY" chat --max-tokens 16 "hi" >/dev/null 2>"$TMPDIR/f29.err" || { RC=$?; break; }; done
  chk "$RC" 3 "exit code 3 = HTTP 429"
  grep -o 'retry-after: [0-9.]*' "$TMPDIR/f29.err" | sed 's/^/  /'
}

tf() {
  for E in us02-reserved-contract us04-nvke-2rack us05-token-api; do
    D="$ROOT/packaging/terraform/examples/$E"
    step "Terraform · $E"
    rm -f "$D/terraform.tfstate"
    V="-var base_url=$NEOCLOUD_BASE_URL -var tenant_name=tf-${E%%-*}-$S"
    run $TF -chdir=$D init >/dev/null
    run $TF -chdir=$D plan $V | tail -1
    run $TF -chdir=$D apply $V | sed 's/^/  /'
    chk "$($TF -chdir=$D plan $V | tail -1)" "Plan: 0 to add, 0 to change, 0 to destroy." "re-plan is a no-op"
  done
  D="$ROOT/packaging/terraform/examples/us05-token-api"
  KEY=$($TF -chdir=$D output api_key)
  run $ROOT/packaging/bin/neocloud --api-key "$KEY" chat --stream --max-tokens 8 "hello" 2>/dev/null >/dev/null \
    && ok "Terraform-issued key streams on /v1" || bad "Terraform-issued key"
  D="$ROOT/packaging/terraform/examples/us02-reserved-contract"
  chk "$($TF -chdir=$D output node_count)" 72 "us02 output node_count"
  step "Terraform · destroy us02 (release 4 racks → partitions DELETE)"
  run $TF -chdir=$D destroy -var base_url=$NEOCLOUD_BASE_URL -var tenant_name=tf-us02-$S | tail -1
}

sdk() {
  step "SDK · packaging/examples/stories.py (US-02..05)"
  run $PY "$ROOT/packaging/examples/stories.py" --base-url "$NEOCLOUD_BASE_URL" --nico-url "$NICO_URL" --suffix "-$S" \
    && ok "SDK stories" || bad "SDK stories"
}

case "${1:-all}" in
  cli) cli ;; tf) tf ;; sdk) sdk ;;
  all) cli; tf; sdk ;;
  *) echo "usage: $0 [cli|tf|sdk|all]"; exit 2 ;;
esac
echo; echo "F29 demo: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
