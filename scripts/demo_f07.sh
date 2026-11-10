#!/usr/bin/env bash
# F07 terminal demo — signup → pending 403 → verify → OIDC JWT → NOCP + NICo accept it.
# Usage: NOCP=http://127.0.0.1:8000 NICO=http://127.0.0.1:9000 OP_TOKEN=<operator token> scripts/demo_f07.sh
# OP_TOKEN: operator bearer (POST /api/v1/auth/tokens, or nothing when NOCP_DEV_OPERATOR_DEFAULT=1).
# NICo must run with EMU_JWT_KEY = NOCP_JWT_SECRET and EMU_JWT_ISSUER = $NOCP for step 9.
set -euo pipefail
NOCP=${NOCP:-http://127.0.0.1:8000}
NICO=${NICO:-http://127.0.0.1:9000}
TID=tnt-demo-f07-sh
OPH=()
[ -n "${OP_TOKEN:-}" ] && OPH=(-H "Authorization: Bearer $OP_TOKEN")
j() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1"; }
step() { printf '\n\033[1m[%s] %s\033[0m\n' "$1" "$2"; }
show() { printf '  $ %s\n' "$*"; }

step 0 "초기화 / reset (DELETE /api/v1/signup/$TID)"
show "curl -X DELETE $NOCP/api/v1/signup/$TID"
curl -s -o /dev/null -w '  → HTTP %{http_code}\n' -X DELETE "${OPH[@]}" "$NOCP/api/v1/signup/$TID"

step 1 "셀프 가입 / self signup"
BODY='{"org_name":"demo-f07-sh","business_reg_no":"214-86-00001","admin_email":"admin@f07-sh.example","admin_name":"Demo Admin","public_sector":false}'
show "curl -X POST $NOCP/api/v1/signup -d '$BODY'"
S=$(curl -s -X POST "$NOCP/api/v1/signup" -H 'Content-Type: application/json' -d "$BODY")
echo "$S" | j '"  → " + d["tenant_id"] + " · " + d["status"] + " · issuer " + d["issuer"]'
PW=$(echo "$S" | j 'd["admin"]["initial_password"]')

step 2 "OIDC discovery (Keycloak path)"
show "curl $NOCP/realms/$TID/.well-known/openid-configuration"
curl -s "$NOCP/realms/$TID/.well-known/openid-configuration" | j '"  → token_endpoint " + d["token_endpoint"] + "\n  → jwks_uri " + d["jwks_uri"]'

step 3 "password grant → JWT"
show "curl -X POST $NOCP/realms/$TID/protocol/openid-connect/token -d grant_type=password&client_id=$TID-portal&username=admin@f07-sh.example&password=…"
T=$(curl -s -X POST "$NOCP/realms/$TID/protocol/openid-connect/token" \
  --data-urlencode grant_type=password --data-urlencode client_id=$TID-portal \
  --data-urlencode username=admin@f07-sh.example --data-urlencode "password=$PW")
JWT=$(echo "$T" | j 'd["access_token"]')
echo "$JWT" | cut -d. -f2 | python3 -c "import base64,json,sys; s=sys.stdin.read().strip(); c=json.loads(base64.urlsafe_b64decode(s+'='*(-len(s)%4))); print('  → claims', json.dumps({k:c[k] for k in ('iss','sub','tid','trole','scope','exp')}))"

step 4 "확인 전 주문 → 403 / order before verification"
show "curl -X POST $NOCP/api/v1/api-keys -H 'Authorization: Bearer <JWT>'"
curl -s -w '  → HTTP %{http_code}\n' -X POST "$NOCP/api/v1/api-keys" -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{"name":"demo"}'; echo

step 5 "운영자 확인 / operator verifies"
show "curl -X POST $NOCP/api/v1/signup/$TID/verify"
curl -s -X POST "${OPH[@]}" "$NOCP/api/v1/signup/$TID/verify" | j '"  → status " + d["status"]'

step 6 "확인 후 주문 허용 / order allowed"
curl -s -o /dev/null -w '  → HTTP %{http_code}\n' -X POST "$NOCP/api/v1/api-keys" -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{"name":"demo"}'

step 7 "JWT 와 HMAC 동시 허용 / JWT and HMAC side by side"
curl -s "$NOCP/api/v1/auth/whoami" -H "Authorization: Bearer $JWT" | j '"  JWT  → " + d["role"] + " " + str(d.get("tenant_id")) + " via " + d["via"]'
[ -n "${OP_TOKEN:-}" ] && curl -s "$NOCP/api/v1/auth/whoami" "${OPH[@]}" | j '"  HMAC → " + d["role"] + " via " + d["via"]'

step 8 "변조·만료 JWT → 401 / tampered JWT → 401"
curl -s -o /dev/null -w '  → HTTP %{http_code}\n' "$NOCP/api/v1/tenants/$TID/users" -H "Authorization: Bearer ${JWT%????}AAAA"

step 9 "NICo 가 같은 JWT 수용 / NICo accepts the JWT"
show "curl $NICO/v2/org/neocloud/nico/vpc -H 'Authorization: Bearer <JWT>'"
curl -s -o /dev/null -w '  → own tenant list HTTP %{http_code}\n' "$NICO/v2/org/neocloud/nico/vpc" -H "Authorization: Bearer $JWT"
curl -s -o /dev/null -w '  → provider surface /machine HTTP %{http_code} (403 = tenant scope)\n' "$NICO/v2/org/neocloud/nico/machine" -H "Authorization: Bearer $JWT"
curl -s -o /dev/null -w '  → tampered JWT HTTP %{http_code}\n' "$NICO/v2/org/neocloud/nico/vpc" -H "Authorization: Bearer ${JWT%????}AAAA"
echo
