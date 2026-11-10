# NeoCloud 개발자 도구 (F29): Python SDK · CLI · Terraform provider(에뮬레이션)

세 도구 모두 NOCP OpenAPI(`openapi/nocp-openapi.json`)에서 나온다. 표준 라이브러리만 쓰고 외부 의존성이 없다.

| 디렉터리 | 내용 |
|---|---|
| `openapi/nocp-openapi.json` | NOCP OpenAPI 3.1 스냅숏 (F01·F02·F05·F06·F07·F08·F11·데모 러너 병합 트리에서 내보냄, 337 operation) |
| `sdk/` | `neocloud_sdk` — `gen.py`가 만든 `_generated.py`(네임스페이스 66개) + 수기 코어(`_client.py`, `_transport.py`), OpenAI SDK 형태 `openai_compat.py` |
| `cli/` | `neocloud` CLI — 생성 표(`OPERATIONS`)로 명령을 만든다. `bin/neocloud`로 설치 없이 실행 |
| `terraform/` | `provider.py`(리소스 6종 스키마·CRUD), `tfemu.py`(Terraform CLI 대역), `provider-schema.json`, `examples/` |
| `examples/stories.py` | US-02~05를 SDK만으로 재현하고 인수 기준을 검사 |

## 재생성
```bash
python packaging/sdk/gen.py --from-app   # app.main에서 OpenAPI 스냅숏을 다시 내보내고 SDK 생성
python packaging/sdk/gen.py --check      # 생성물이 스냅숏과 다르면 exit 1 (테스트가 확인)
python packaging/terraform/tfemu.py providers-schema > packaging/terraform/provider-schema.json
python packaging/terraform/tfemu.py hcl -chdir=packaging/terraform/examples/<ex> > .../main.tf
```
이름 규칙: `/api/v1/<seg>/…` → `client.<seg>`, `/v1/*` → `client.openai`, `/realms/*` → `client.oidc`; 메서드 이름은 FastAPI 핸들러 이름(operationId에서 `_<path>_<method>` 접미사를 뗀 것). 기능 브랜치가 더 병합되면 `--from-app`으로 다시 만든다.

## SDK
```python
from neocloud_sdk import Client
nc = Client("http://127.0.0.1:8000", operator=True)        # 또는 token=..., NEOCLOUD_TOKEN
ctr = nc.contracts.create_contract({"tenant_id": "tnt-a", "product": "bmaas", "racks": 4,
        "blueprint_key": "vr-nvl72", "term_months": 12, "start_date": "2026-11-01",
        "unit_price": 95_000_000, "placement": {"same_ib_zone": True}})
nc.contracts.activate_contract(ctr["id"])
order = nc.orders.create_order({"tenant_id": "tnt-a", "kind": "new", "contract_id": ctr["id"]})

oa = nc.openai_client(api_key)        # == OpenAI(base_url="http://…/v1", api_key=…)
for chunk in oa.chat.completions.create(model=m, messages=msgs, stream=True): ...
```
인증 우선순위: `token`(Bearer: HMAC `nocp1.`, NOCP_TOKENS, OIDC access token) → `/v1/*`에는 `api_key` → 개발 스택 헤더 `X-Operator`/`X-Tenant-Id`. `Client.from_oidc(base, realm, client_id, secret)`는 Keycloak 형식 토큰 엔드포인트(RFC 6749 client_credentials)를 쓴다. 오류 클래스와 이름은 openai-python과 같다(`RateLimitError.retry_after`, `err.response.headers["retry-after"]`). SSE는 EventSource 규칙(`data:` 줄, 빈 줄로 이벤트 끝, `data: [DONE]`)으로 파싱한다.

## CLI
```bash
export NEOCLOUD_BASE_URL=http://127.0.0.1:8000
packaging/bin/neocloud commands --ns contracts
packaging/bin/neocloud --operator contracts create-contract -f tenant_id=tnt-a -f product=bmaas \
   -F racks=4 -f blueprint_key=vr-nvl72 -F term_months=12 -f start_date=2026-11-01 \
   -F unit_price=95000000 -F placement.same_ib_zone=true -q id
packaging/bin/neocloud --api-key "$KEY" chat --stream "hello"
```
`-f`는 문자열, `-F`는 JSON 값(`gh api` 규칙), 점 표기로 중첩. `-q a.b.0`은 필드 하나만 출력(`length` 지원). 종료 코드: 0 성공, 1 API 오류, 2 사용법, 3 429(Retry-After를 stderr에), 4 연결 오류.

## Terraform (에뮬레이션)
실제 provider는 gRPC 플러그인(tfplugin6)이다. 여기서는 사용자가 보는 면을 그대로 둔다: `terraform providers schema -json` 문서(format_version 1.0), 리소스 CRUD 의미(ForceNew 속성 변경은 교체), state v4(`terraform.tfstate`), `plan -json` 표현(format_version 1.2, `actions`). 설정은 Terraform JSON 문법(`main.tf.json`)이고 `main.tf`는 그 HCL 쌍둥이다(`tfemu.py hcl`로 생성, 테스트가 일치를 확인).

| 리소스 | NOCP 작업 | destroy |
|---|---|---|
| `neocloud_tenant` | `POST /api/v1/tenants` | 상태에서만 제거(오프보딩은 termination 워크플로) |
| `neocloud_contract` | `POST /api/v1/contracts` + `/activate`, 변경 `PATCH` | `PATCH status=terminated` |
| `neocloud_bmaas_order` | `POST /api/v1/orders kind=new` | `kind=terminate` (할당별) |
| `neocloud_nvke_cluster` | `POST /api/v1/nvke/clusters`, kubeconfig는 소유 테넌트로 조회 | `DELETE` |
| `neocloud_token_key` | `POST /api/v1/token/keys` (비밀값 sensitive) | `DELETE /api/v1/token/keys/{id}` |
| `neocloud_token_quota` | `PUT /api/v1/token/quotas/{tenant}` | 상태에서만 제거 |

```bash
T=packaging/terraform/tfemu.py; D=packaging/terraform/examples/us02-reserved-contract
python $T -chdir=$D init && python $T -chdir=$D plan && python $T -chdir=$D apply
python $T -chdir=$D output node_count && python $T -chdir=$D destroy
```

## 시연 (US-02~05)
- 터미널: `NEOCLOUD_BASE_URL=… NICO_URL=… scripts/demo_f29.sh [cli|tf|sdk|all]` — 명령과 결과, PASS/FAIL 표.
- SDK만: `python packaging/examples/stories.py --base-url … --nico-url …`
- 콘솔 데모 센터: 시나리오 `f29-packaging` (`POST /api/v1/demo/scenarios/f29-packaging/run`) — SDK를 앱 안(TestClient)에서 돌려 단계별 SDK 요청 목록을 보여 주고 마지막에 Terraform 예제를 적용·재계획·삭제한다.

제약: 보안 모드(`NOCP_DEV_INSECURE_HEADERS=0`)에서 kubeconfig는 테넌트 토큰으로만 나온다. Terraform provider에 테넌트 토큰을 주거나 SDK `as_tenant`를 쓴다.
