# NeoCloud v2 시연 런북 (데모 러너 · 데모 센터)

v2 기능이 실제로 동작하는 모습을 사람이 처음부터 끝까지 볼 수 있게 하는 시연 절차다. 시연은 **실제 API 호출**만 보여준다. 아직 머지되지 않은 기능의 단계는 실패가 아니라 `pending feature Fxx`(기능 대기)로 표시되고, 기능이 머지되면 코드 수정 없이 자동으로 실행된다.

## 1. 구성 요소

| 구성 | 위치 | 하는 일 |
|---|---|---|
| 데모 러너 | NOCP `app/demo/` · `/api/v1/demo/*` (운영자 전용) | 시나리오 자동 로드, 전체 실행·단계 실행, 단계별 나레이션(KO/EN)·요청/응답·검증·소요 시간, SSE 이벤트, 결정적 초기화 |
| 시나리오 | NOCP `app/demo/scenarios/*.py` | 파일 하나에 `SCENARIO = {...}` 하나. 형식은 `app/demo/engine.py` 머리 주석과 `WAVE-v2.md` "시연(데모) 요건" |
| 데모 센터 | Console 운영 콘솔 `#/demo-center` (`ops/demo.js`) | 시나리오 목록, 실행 버튼, 실시간 단계 타임라인, 통과/실패 배지, 바뀐 화면 딥링크, 발표 모드 |
| 터미널 실행 | NOCP `scripts/demo_all.sh` | 아무것도 띄우지 않고, 주어진 NOCP에 모든 시나리오를 돌려 표로 출력 |
| 화면 점검 | Console `scripts/demo-center-check.mjs` | 헤드리스 Chrome으로 데모 센터를 열어 실행·단계·EN·발표 모드를 확인하고 스크린샷 저장 |

### API 요약

| 메서드 · 경로 | 설명 |
|---|---|
| `GET /api/v1/demo/scenarios` | 시나리오 목록(Day 0 → 2 시연 순서) · 시나리오마다 `day`(0 구축·1 개통·2 운영) · `order`(DEMO-ORDER.md 단계 1–18) · `showcase_order`(대표 8개 1–8, 나머지 null) · 기능과 대기 기능(`pending_features`) · `load_errors`. `?day=0\|1\|2`(또는 `day1`)는 그날만, `?showcase=1`은 대표 8개를 시연 순서로 |
| `GET /api/v1/demo/playlist` | Day 0 → Day 1 → Day 2 시연 목록(18단계). `{days:[{day,title_*,summary_*}], items:[{order, step, day, kind:"scenario"\|"screen", scenario_id, title_*, caption_*, console, route, showcase_order}], showcase:[…]}`. 화면만 보여주는 단계(예: 5 MaxLPS 전력 예산)는 `kind:"screen"` + 콘솔 경로. `?day=` 지원 |
| `GET /api/v1/demo/scenarios/{id}` | 단계 목록(나레이션·기대값·딥링크·대기 여부) |
| `POST /api/v1/demo/scenarios/{id}/run` | 본문 `{mode:"all"\|"step", pace_ms?, reset?}`. `?wait=true`면 끝날 때까지 기다린 뒤 응답 |
| `POST /api/v1/demo/runs/{run_id}/step` | 단계 모드에서 다음 단계 1개 실행 |
| `POST /api/v1/demo/runs/{run_id}/continue` | 남은 단계 실행(`?pace_ms=`, `?wait=`) |
| `GET /api/v1/demo/runs/{run_id}` | 단계 상태·나레이션 KO/EN·요청/응답 요약·원문·검증·소요 시간 |
| `GET /api/v1/demo/runs/{run_id}/events` | SSE: `run` → 단계마다 `step` 2번(시작·끝) → `done`. `Last-Event-ID`/`?since=` 재개 |
| `POST /api/v1/demo/reset` | `{scope:"nocp"\|"all"}` 결정적 재시드(Phase 1 기본 배치 + 고정 샘플). `all`은 NICo(cascade)·AI-Infra도 초기화. 실행 기록과 시나리오별 회수 목록도 지운다 |

단계 상태: `passed` 통과 · `failed` 실패 · `pending` 기능 대기(라우트가 이 빌드에 없음, 또는 앞 대기 단계의 결과가 필요) · `skipped` 건너뜀(에뮬레이터 미연결 등) · `waiting` 대기 · `running` 실행 중. 판정(`verdict`): `pass` · `pass_with_pending` · `fail`.

CORS: 콘솔이 다른 출처에서 부를 수 있게 `X-Request-Id` 요청 헤더를 허용하고, 응답의 `X-Request-Id`·`Retry-After`·`X-RateLimit-*`를 노출(`Access-Control-Expose-Headers`)한다.

**시연 격리**: 시나리오는 어떤 순서로 돌려도 통과한다. 실행마다 짧은 고유 id(`ctx["uid"]`, 5자)를 받아 만드는 모든 이름에 붙인다(테넌트 `demo-g-nvke-<uid>`, 가입 조직·사업자번호, DCIM 시리얼·위치). 같은 시나리오를 다시 돌리면 첫 단계로 `00-cleanup`(이전 실행 정리)이 붙어, 이전 실행이 잡고 있던 NVKE 클러스터와 BMaaS 할당만 실제 경로(NVKE 삭제, terminate 주문)로 풀에 돌려놓는다. 다른 시나리오 상태는 건드리지 않는다. 라이브 NICo에서는 할당 하나 회수에 약 37초(소거 대기)가 걸린다. NVKE는 같은 순위 안에서 빈 트레이가 많은 랙부터 채워, 2랙 노드풀이 늘 랙 2개(클리크 2개)에 놓인다.

요청 원문에서 `Authorization`·`X-API-Key` 값은 가려진다. NOCP 내부 호출은 같은 프로세스 안에서 실제 미들웨어(테넌트 가드 포함)를 거치며, 호출마다 새로 발급한 운영자/테넌트 토큰을 쓴다.

## 2. 스택 띄우기

개발 스택(기본 포트):

```bash
cd AI-Infra-Emulator && ./run.sh            # :9100
cd NICo-Emulator && ./run.sh                # :9000 (AI_INFRA_URL=http://127.0.0.1:9100)
cd NeoCloud-Control-Plane && ./run.sh       # :8000 (개발 스위치: 운영자 기본 신원)
cd NeoCloud-Console && ./run.sh             # :8090
```

다른 포트 대역(예: PORT_BASE=20160 → NICo 20160 · AI-Infra 20161 · NOCP 20162 · Console 20163)으로 띄울 때 NOCP 환경 변수:

```bash
NOCP_DEV_OPERATOR_DEFAULT=1 NOCP_DEV_INSECURE_HEADERS=1 NOCP_OOB_AUTOSUBSCRIBE=0 \
NOCP_NICO_URL=http://127.0.0.1:20160 NICO_EMULATOR_URL=http://127.0.0.1:20160 \
AI_INFRA_URL=http://127.0.0.1:20161 NOCP_CORS_ORIGINS=http://127.0.0.1:20163 \
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 20162
```

콘솔이 다른 NOCP를 보게 하려면 브라우저 콘솔에서 `localStorage.setItem("nc-nocp","http://127.0.0.1:20162")` 후 새로고침. 보안 모드(토큰 필수) NOCP라면 `localStorage.setItem("nocp_token","<운영자 토큰>")`; 이때 SSE는 헤더를 못 붙이므로 데모 센터가 자동으로 폴링으로 바꾼다.

## 3. 시연 방법

### 화면 (데모 센터)

1. 운영 콘솔 → 사이드바 **DSX OS › 데모 센터** (`/ops/#/demo-center`).
2. 상단 **초기화**(NOCP만) 또는 **전체 초기화**(NICo·AI-Infra 포함)로 시작 상태를 맞춘다.
3. 왼쪽에서 시나리오 선택 → **전체 실행**(속도: 빠르게/보통/천천히) 또는 **단계 실행** → **다음 단계**로 한 단계씩.
4. 단계 카드: 나레이션 → 기대 → 결과(검증 문장) → 요청 줄·응답 요약 → **바뀐 화면** 딥링크(운영 화면은 같은 탭, 고객·사업 콘솔은 새 탭) → **요청·응답 원문** 펼침.
5. **발표 모드**: 사이드바·검색을 숨기고 글자를 1.45배로. `Esc` 해제, 단계 모드에서 `→`/`PageDown` = 다음 단계.
6. KO/EN은 상단 언어 토글. 러너가 두 언어 문장을 모두 주므로 바로 다시 그린다.

### 터미널

```bash
scripts/demo_all.sh http://127.0.0.1:8000 --reset-all              # 표만
scripts/demo_all.sh http://127.0.0.1:8000 --reset --steps          # 단계별 요청·응답·검증 출력 (KO)
scripts/demo_all.sh http://127.0.0.1:8000 --only c-xid-remediation --steps --lang en
scripts/demo_all.sh http://127.0.0.1:8000 --day 1                   # Day 1(개통)만
scripts/demo_all.sh http://127.0.0.1:8000 --reverse                 # 역순 (격리 점검)
NOCP_TOKEN=<운영자 토큰> scripts/demo_all.sh https://nocp.example     # 보안 모드
```

종료 코드: 실패 단계가 하나라도 있으면 1, 기능 대기·건너뜀만 있으면 0.

출력 예 (2026-11-10, 포트 20160–20163, NOCP = feat/v2-demo + F05/F06 + F23/F24/F25 브랜치 병합본, AI-Infra·NICo = bring-up 브랜치):

```
scenario                   verdict             pass  fail  pend  skip       ms
a-signup-to-invoice        pass_with_pending     10     0     2     0   1313.6
b-token-api                pass_with_pending      2     0    10     0      6.7
c-xid-remediation          pass_with_pending      5     0     5     0    330.0
d-grid-curtailment         pass_with_pending      4     0     7     0    326.1
e-leak-workload-move       pass_with_pending      5     0     1     0    537.5
f-rack-bringup             pass                   7     0     0     0    319.6
g-nvke-clique              pass_with_pending      3     0     3     0    681.2
h-bmaas-baseline           pass                   8     0     0     0    520.9
f23-rack-bringup           pass                   8     0     0     0    313.9
f24-firmware-bundle        pass                   6     0     0     0     18.8
f25-attestation            pass                   5     0     0     0     80.2
total: 63 passed · 0 failed · 28 pending · 0 skipped
```

### 화면 점검 (자동)

```bash
cd NeoCloud-Console
CONSOLE_URL=http://127.0.0.1:8090 NOCP_BASE=http://127.0.0.1:8000 node scripts/demo-center-check.mjs /tmp
```

## 4. 시나리오 한눈에 (Day 0 → Day 1 → Day 2)

시연 순서는 `DEMO-ORDER.md`(18단계)를 따른다. Day 0 = 구축(설치·검증, 판매 가능 재고까지) · Day 1 = 개통(가입·주문·서비스 시작) · Day 2 = 운영(사용·과금·장애·전력망). 대표 8개 시나리오(`?showcase=1`)는 아래 순서다. 나머지 시나리오도 모두 `day`·`order`가 있어 목록과 `/playlist`에서 같은 날·단계에 붙는다.

| # | Day | 시나리오 | 보여주는 것 | 기능 | 기준 |
|---|---|---|---|---|---|
| 1 | Day 0 구축 | `f-rack-bringup` | DCIM 등록 → 오결선 주입 → 04단계 실패 → 재결선 → 15단계 통과 → 판매 재고 | F23 | Phase 2 DoD ③ |
| 2 | Day 1 개통 | `a-signup-to-invoice` | 가입 → 계약 → 4랙 예약 주문 → 인수 → 원화 청구서·세금계산서·결제 | F07 · F06 · F05 | Phase 1 DoD 체인 a, US-01·02·06 |
| 3 | Day 1 개통 | `h-bmaas-baseline` | 기존 BMaaS: 테넌트 → 1랙 주문 → 흐름 감사 → 격리 → 인수 → 계측 → 반납 | 기존(main) | 기준선 |
| 4 | Day 1 개통 | `g-nvke-clique` | 2랙 노드풀 → CAPI 객체 → gpu.clique 라벨 2종 → IMEX ComputeDomain 2개 → kubeconfig | F11 | US-04 |
| 5 | Day 2 운영 | `b-token-api` | OpenAI 호환 스트리밍 → 429 → 가드 차단 → usage 대조 → 청구 토큰 라인 | F01 · F02 · F03 · F04 · F05 | 체인 b, US-05·06 |
| 6 | Day 2 운영 | `c-xid-remediation` | XID 79 주입 → cordon/drain/reset/교체/RMA → 다운타임 → 크레딧 → 청구서 차감 | F10 · F09 · F05 | 체인 c, US-07 |
| 7 | Day 2 운영 | `e-leak-workload-move` | 누수 Critical → 랙 상태 → 자동 교체 run → 노드 이전 확인 | F22 · F10 | Phase 2 DoD ② |
| 8 | Day 2 운영 | `d-grid-curtailment` | Load Target Set → 예약 보호 · Batch 축소 → SLA 예외 · 공지 → 원복 → 정산 | F20 · F21 · F02 · F09 · F13 · F27 | Phase 2 DoD ① |

기능 브랜치가 추가한 시나리오(예: `f23-rack-bringup`, `f24-firmware-bundle`, `f25-attestation`)도 같은 폴더에 있으면 자동으로 목록에 나온다.

기능이 없을 때 대신 쓰는 기존 API(대체 실행): 가입(a) → `POST /api/v1/tenants`, (c) 자동 복구 → 수동 `POST /api/v1/rma`, (e) 자동 이전 → 수동 `POST /api/v1/nvke/clusters/{id}/nodes/{node}/replace`. 이 단계는 `pending`으로 표시되고 결과 문장 끝에 "기존 API로 대체 실행"이 붙는다. (d)의 Load Target Set은 F20 전까지 기존 축약형 `POST /api/v1/power/flex-events`로 들어온다. DSX Flex 1.7.0은 공개 메시지 형식이 없어 이름만 공개 문서 기준으로 따른다.

에뮬레이터 의존: (f)의 오결선 주입은 AI-Infra Config Manager가 있어야 한다. AI-Infra가 없으면 주입·04단계 실패·불일치 확인이 `skipped`로 표시되고 나머지는 로컬 폴백으로 진행한다.

## 5. 단계별 토크 트랙

아래 표는 `app/demo/scenarios/*.py`의 단계 정의(나레이션·기대값·딥링크)에서 만든 것이다. 시나리오를 고치면 같은 방법으로 다시 만든다.

### Day 0 구축

#### 1. 신규 랙 bring-up 15단계 (04 케이블 불일치 실패 포함)

- 시나리오 id: `f-rack-bringup` · 기능: F23
- 무엇을 보여주나: DCIM 등록부터 판매 가능 재고까지 자동 진행. 케이블이 계획과 다르면 04 LLDP 검증에서 멈춘다 (Phase 2 DoD ③).

| 단계 | 화면에 보이는 것 | 말할 것 (토크 트랙) | 확인 포인트 | 필요 기능 |
|---|---|---|---|---|
| `01-dcim` DCIM 랙 등록 | 운영 · bring-up, 운영 · 프로비저닝 | NetBox 대체 DCIM에 시리얼·위치·청사진·케이블 계획을 넣는다. | HTTP 성공·검증 문장 | F23 |
| `02-miswire` 케이블 오결선 주입 (AI-Infra) | — | 케이블 오결선 주입 (AI-Infra) | HTTP 성공·검증 문장 | F23 |
| `03-bringup-fail` bring-up 시작 → 04 실패 | — | 01 DCIM → 02 전원 → 03 ZTP → 04 LLDP 검증에서 계획과 다른 이웃을 찾아 멈춘다. | state=failed · failed_step=4 | F23 |
| `04-cables` 케이블 불일치 확인 (NetBox 형식) | — | 케이블 불일치 확인 (NetBox 형식) | HTTP 성공·검증 문장 | F23 |
| `05-recable` 현장 재결선 | — | 현장 재결선 | HTTP 성공·검증 문장 | F23 |
| `06-bringup-pass` bring-up 재시작 → 15단계 통과 | — | bring-up 재시작 → 15단계 통과 | HTTP 성공·검증 문장 | F23 |
| `07-sellable` 판매 가능 재고 | 운영 · 용량 | 판매 가능 재고 | HTTP 성공·검증 문장 | F23 |

### Day 1 개통

#### 2. 가입 → 계약 → 4랙 예약 → 인수 → 청구서(KRW+VAT)

- 시나리오 id: `a-signup-to-invoice` · 기능: F07/F06/F05 · 스토리: US-01 · US-02 · US-06
- 무엇을 보여주나: 기업 고객이 웹 가입부터 첫 원화 청구서·세금계산서를 받기까지 (Phase 1 DoD 체인 a).

| 단계 | 화면에 보이는 것 | 말할 것 (토크 트랙) | 확인 포인트 | 필요 기능 |
|---|---|---|---|---|
| `01-signup` 셀프 가입 | 고객 콘솔 | 사업자번호·관리자 이메일로 가입하면 테넌트가 '확인 대기'로 생긴다. | status = pending_verification | F07 |
| `02-verify` 운영자 사업자 확인 | — | 운영자가 확인하면 realm·관리자가 활성화되고 주문이 허용된다(미확인 주문은 403). | HTTP 성공·검증 문장 | F07 |
| `03-contract` 4랙 12개월 예약 계약 등록 | 사업 · 계약 | 영업이 같은 IB 구역 4랙, 12개월, 랙당 단가로 약정 계약을 만든다. | HTTP 성공·검증 문장 | F06 |
| `04-activate` 계약 활성화 | — | 계약 활성화 | HTTP 성공·검증 문장 | F06 |
| `05-order` 계약으로 4랙 주문 | 운영 · 프로비저닝 | 주문에 contract_id만 넣으면 랙 수·배치 제약(같은 IB 구역)을 계약에서 가져온다. | delivered · 4랙 72노드 · 한 SU | 기존(main) |
| `06-acceptance` 고객 인수 승인 | — | 인수 보고서(NCCL·fio·burn-in)를 보고 고객 관리자가 승인한다. | HTTP 성공·검증 문장 | 기존(main) |
| `07-invoice-run` 월 청구 실행 | 고객 · 청구 | 요율표로 랙-시간·약정 true-up·SLA 크레딧을 계산해 테넌트별 초안을 만든다(멱등). | HTTP 성공·검증 문장 | F05 |
| `08-invoice` 원화 청구서 확인 | — | 원화 청구서 확인 | KRW · VAT = floor(공급가×10%) · 합계 = 공급가+VAT | F05 |
| `09-finalize` 청구서 확정 | — | 확정하면 수정이 막히고 SLA 크레딧이 이 청구서에 적용되며 세금계산서가 발행된다. | HTTP 성공·검증 문장 | F05 |
| `10-tax-invoice` 전자세금계산서(모사) | 사업 · 매출 | 전자세금계산서(모사) | HTTP 성공·검증 문장 | F05 |
| `11-payment-method` 결제수단 등록 (Stripe 테스트 모드 모사) | — | 결제수단 등록 (Stripe 테스트 모드 모사) | HTTP 성공·검증 문장 | F05 |
| `12-pay` 결제 | — | 결제 | HTTP 성공·검증 문장 | F05 |

#### 3. BMaaS 주문 기준선

- 시나리오 id: `h-bmaas-baseline` · 기능: baseline
- 무엇을 보여주나: v2 이전부터 있던 흐름: 테넌트 → 1랙 주문 → 격리 → 인수 → 계측 → 반납.

| 단계 | 화면에 보이는 것 | 말할 것 (토크 트랙) | 확인 포인트 | 필요 기능 |
|---|---|---|---|---|
| `01-tenant` 테넌트 demo-h-baseline 생성 | 운영 · 테넌트 | 고객 조직을 만들면 VRF·L3VNI·IAM realm이 자동으로 묶인다. | HTTP 성공·검증 문장 | 기존(main) |
| `02-order` 1랙 BMaaS 주문 | 운영 · 프로비저닝 | 주문은 NICo 할당 → PXE·OS 설치 → 3-plane 격리 → VAST 뷰 연결까지 한 번에 간다. | HTTP 성공·검증 문장 | 기존(main) |
| `03-flow` 프로비저닝 흐름 감사 | 운영 · 프로비저닝 | 주문 단계별로 어떤 하위 API(Redfish·NVUE·VAST)가 몇 번 불렸는지 본다. | HTTP 성공·검증 문장 | 기존(main) |
| `04-isolation` 3-plane 격리 검증 | 운영 · 미터링·격리 | 3-plane 격리 검증 | HTTP 성공·검증 문장 | 기존(main) |
| `05-acceptance-report` 인수 보고서 | — | 인수 보고서 | HTTP 성공·검증 문장 | 기존(main) |
| `06-approve` 고객 인수 승인 | 고객 · 클러스터 | 고객 관리자 토큰으로 승인한다(테넌트 범위 검사 통과). | HTTP 성공·검증 문장 | 기존(main) |
| `07-metering` 사용량 계측 | 운영 · 미터링 | 사용량 계측 | HTTP 성공·검증 문장 | 기존(main) |
| `08-reclaim` 반납(소거 후 풀 복귀) | — | 반납(소거 후 풀 복귀) | HTTP 성공·검증 문장 | 기존(main) |

#### 4. NVKE 클러스터: 클리크 인식 노드풀 + kubeconfig

- 시나리오 id: `g-nvke-clique` · 기능: F11 · 스토리: US-04
- 무엇을 보여주나: 2랙 노드풀을 만들면 랙 단위 NVLink 클리크·IMEX 도메인이 생기고 kubeconfig가 나온다 (US-04).

| 단계 | 화면에 보이는 것 | 말할 것 (토크 트랙) | 확인 포인트 | 필요 기능 |
|---|---|---|---|---|
| `01-tenant` 테넌트 demo-g-nvke 생성 | 운영 · 테넌트 | 고객 조직을 만들면 VRF·L3VNI·IAM realm이 자동으로 묶인다. | HTTP 성공·검증 문장 | 기존(main) |
| `02-create` 2랙 노드풀 클러스터 생성 | 고객 · NVKE | NVL72 2랙 = 36노드 · 144 GPU. 노드는 NICo 인스턴스(NicoMachine)로 붙는다. | HTTP 성공·검증 문장 | 기존(main) |
| `03-capi` CAPI 객체 | 운영 · Managed K8s | CAPI 객체 | HTTP 성공·검증 문장 | F11 |
| `04-cliques` 노드 · 클리크 라벨 | — | 노드 · 클리크 라벨 | nvidia.com/gpu.clique 2종 (<domainId>.<cliqueId>) | F11 |
| `05-compute-domains` IMEX ComputeDomain | — | IMEX ComputeDomain | HTTP 성공·검증 문장 | F11 |
| `06-kubeconfig` kubeconfig 발급 | — | kubeconfig 발급 | HTTP 성공·검증 문장 | 기존(main) |

### Day 2 운영

#### 5. 토큰 API: 스트리밍 · 429 · 가드 → 사용량 대조 → 청구 라인

- 시나리오 id: `b-token-api` · 기능: F01/F02/F03/F04/F05 · 스토리: US-05 · US-06
- 무엇을 보여주나: OpenAI SDK 그대로 base_url만 바꿔 호출하고, 같은 요청 ID는 한 번만 청구된다 (DoD 체인 b).

| 단계 | 화면에 보이는 것 | 말할 것 (토크 트랙) | 확인 포인트 | 필요 기능 |
|---|---|---|---|---|
| `01-tenant` 테넌트 demo-b-tokens 생성 | 운영 · 테넌트 | 고객 조직을 만들면 VRF·L3VNI·IAM realm이 자동으로 묶인다. | HTTP 성공·검증 문장 | 기존(main) |
| `02-api-key` API 키 발급 | 고객 · 토큰 API | API 키 발급 | HTTP 성공·검증 문장 | 기존(main) |
| `03-models` 모델 목록 (GET /v1/models) | — | 모델 목록 (GET /v1/models) | HTTP 성공·검증 문장 | F01 |
| `04-stream` 스트리밍 채팅 완성 | 운영 · 추론 SLO | stream=true면 text/event-stream으로 토큰이 오고 마지막 청크에 usage가 온다. | SSE · 마지막 청크 usage · [DONE] | F01 |
| `05-quota` 쿼터 축소 (TPM/RPM) | — | 쿼터 축소 (TPM/RPM) | HTTP 성공·검증 문장 | F02 |
| `06-429` 한도 초과 → 429 | — | 같은 분 안의 두 번째 호출은 OpenAI 형식 오류와 Retry-After로 거절된다. | HTTP 성공·검증 문장 | F02 |
| `07-guard-policy` 가드 정책 enforce | — | 가드 정책 enforce | HTTP 성공·검증 문장 | F03 |
| `08-guard-block` PII 입력 차단 | — | 주민등록번호 패턴이 든 프롬프트는 엔진에 가기 전에 content_filter로 막힌다. | HTTP 성공·검증 문장 | F03 |
| `09-usage-events` usage 이벤트 (멱등) | 운영 · 미터링 | usage 이벤트 (멱등) | HTTP 성공·검증 문장 | F04 |
| `10-reconcile` 게이트웨이 대 엔진 대조 | — | 게이트웨이 대 엔진 대조 | HTTP 성공·검증 문장 | F04 |
| `11-invoice-run` 월 청구 실행 | — | 월 청구 실행 | HTTP 성공·검증 문장 | F05 |
| `12-token-line` 청구서 토큰 라인 | 고객 · 청구 | 청구서 토큰 라인 | metric=token 라인 존재 (input/output 별도) | F05 |

#### 6. XID 장애 주입 → 자동 cordon/drain/교체 → SLA 크레딧 → 청구서 차감

- 시나리오 id: `c-xid-remediation` · 기능: F10/F09/F05 · 스토리: US-07
- 무엇을 보여주나: GPU가 버스에서 떨어지면 NVSentinel 흐름대로 자동 교체되고, 가용성 미달분이 다음 청구서에서 빠진다 (DoD 체인 c).

| 단계 | 화면에 보이는 것 | 말할 것 (토크 트랙) | 확인 포인트 | 필요 기능 |
|---|---|---|---|---|
| `01-tenant` 테넌트 demo-c-xid 생성 | 운영 · 테넌트 | 고객 조직을 만들면 VRF·L3VNI·IAM realm이 자동으로 묶인다. | HTTP 성공·검증 문장 | 기존(main) |
| `02-order` 1랙 BMaaS 주문 | 운영 · 프로비저닝 | 주문은 NICo 할당 → PXE·OS 설치 → 3-plane 격리 → VAST 뷰 연결까지 한 번에 간다. | HTTP 성공·검증 문장 | 기존(main) |
| `03-policy` 자동 복구 정책 켜기 | 운영 · RMA·복구 | 기본은 '모니터링만'. 덱 NOTE대로 운영자가 auto로 켠다(opt-in). | HTTP 성공·검증 문장 | F10 |
| `04-inject-xid` XID 79 장애 주입 | 운영 · OOB 이벤트 | AI-Infra가 보내는 것과 같은 Redfish EventService 봉투로 XID 79(GPU fallen off the bus)를 넣는다. | HTTP 성공·검증 문장 | 기존(main) |
| `05-remediation` 자동 복구 실행 확인 | — | cordon → drain → GPU reset → 재검증 실패 → 예비 노드 교체 → RMA 순으로 진행, MTTR을 잰다. | outcome recovered|replaced · MTTR ≤ 60분 | F10 |
| `06-rma` RMA 케이스 연결 | 운영 · RMA | RMA 케이스 연결 | HTTP 성공·검증 문장 | 기존(main) |
| `07-downtime` SLA 다운타임 귀속 | — | SLA 다운타임 귀속 | HTTP 성공·검증 문장 | F09 |
| `08-sla-report` 월 SLA 보고서 · 크레딧 | 고객 · 상태·SLA | 월 SLA 보고서 · 크레딧 | HTTP 성공·검증 문장 | F09 |
| `09-invoice-run` 월 청구 실행 | — | 월 청구 실행 | HTTP 성공·검증 문장 | F05 |
| `10-credit-deducted` 청구서에서 크레딧 차감 | 고객 · 청구 | 청구서에서 크레딧 차감 | HTTP 성공·검증 문장 | F05 |

#### 7. 누수 → cordon → 워크로드 이전

- 시나리오 id: `e-leak-workload-move` · 기능: F22/F10
- 무엇을 보여주나: 트레이 냉각수 누수가 감지되면 해당 노드를 격리하고 NVKE 워크로드를 예비 노드로 옮긴다 (Phase 2 DoD ②).

| 단계 | 화면에 보이는 것 | 말할 것 (토크 트랙) | 확인 포인트 | 필요 기능 |
|---|---|---|---|---|
| `01-tenant` 테넌트 demo-e-leak 생성 | 운영 · 테넌트 | 고객 조직을 만들면 VRF·L3VNI·IAM realm이 자동으로 묶인다. | HTTP 성공·검증 문장 | 기존(main) |
| `02-nvke` NVKE 클러스터 (노드 4대) | 고객 · NVKE | NVKE 클러스터 (노드 4대) | HTTP 성공·검증 문장 | 기존(main) |
| `03-inject-leak` 누수 Critical 주입 | 운영 · OOB (Leak 히트맵) | Redfish LeakDetection 이벤트로 트레이 누수를 알린다(운영자 주입). | HTTP 성공·검증 문장 | 기존(main) |
| `04-rack-state` 랙 누수 상태 | — | 랙 누수 상태 | HTTP 성공·검증 문장 | 기존(main) |
| `05-auto-move` 자동 cordon · 교체 run | 운영 · RMA·복구 | Critical 누수는 F10 교체 run을 만든다. Warning이면 공지만 한다. | HTTP 성공·검증 문장 | F22 |
| `06-nodes` 노드 목록: 이전 확인 | — | 노드 목록: 이전 확인 | HTTP 성공·검증 문장 | 기존(main) |

#### 8. 그리드 감축(DSX Flex) → 예약 보호 · Batch 축소 · 원복 · 정산

- 시나리오 id: `d-grid-curtailment` · 기능: F20/F21/F02/F09/F13/F27
- 무엇을 보여주나: 전력망 감축 요청이 오면 예약 고객 랙은 그대로 두고 토큰 Batch·스팟부터 줄인 뒤 원복·정산한다 (Phase 2 DoD ①).

| 단계 | 화면에 보이는 것 | 말할 것 (토크 트랙) | 확인 포인트 | 필요 기능 |
|---|---|---|---|---|
| `01-reserved-tenant` 테넌트 demo-d-reserved 생성 | 운영 · 테넌트 | 고객 조직을 만들면 VRF·L3VNI·IAM realm이 자동으로 묶인다. | HTTP 성공·검증 문장 | 기존(main) |
| `02-reserved-order` 1랙 BMaaS 주문 | 운영 · 프로비저닝 | 주문은 NICo 할당 → PXE·OS 설치 → 3-plane 격리 → VAST 뷰 연결까지 한 번에 간다. | HTTP 성공·검증 문장 | 기존(main) |
| `03-domains` DPS 전력 도메인 | 운영 · MaxLPS 전력 | DPS 전력 도메인 | HTTP 성공·검증 문장 | 기존(main) |
| `04-policy` 감축 정책 (예약 보호) | — | 감축 정책 (예약 보호) | HTTP 성공·검증 문장 | F21 |
| `05-load-target` Load Target Set 수신 (−2 MW) | 운영 · MaxLPS 전력 | 전력망 운영자가 15분간 2 MW 감축을 요청한다. F20 전에는 축약형 flex-event로 들어온다. | HTTP 성공·검증 문장 | 기존(main) |
| `06-curtailment` 감축 실행: 누구를 줄였나 | — | 감축 실행: 누구를 줄였나 | 예약 고객 랙 상한 변화 0 · 달성 kW > 0 | F21 |
| `07-batch-quota` 토큰 Batch 쿼터 축소 | — | 토큰 Batch 쿼터 축소 | HTTP 성공·검증 문장 | F02 |
| `08-sla-exception` SLA 예외 구간 기록 | — | SLA 예외 구간 기록 | HTTP 성공·검증 문장 | F09 |
| `09-status-notice` 상태 페이지 공지 | 고객 · 상태 | 상태 페이지 공지 | HTTP 성공·검증 문장 | F13 |
| `10-restore` 감축 종료 · 원복 | — | 감축 종료 · 원복 | HTTP 성공·검증 문장 | F21 |
| `11-settlement` 수요반응 정산 | 사업 · 매출 | 수요반응 정산 | HTTP 성공·검증 문장 | F27 |

## 6. 시나리오 추가하기 (기능 담당자용)

1. `app/demo/scenarios/<feature>.py`에 `SCENARIO = {"id", "title_ko", "title_en", "feature", "summary_ko", "summary_en", "order", "steps": [...]}`.
2. 단계: `{"id", "title_ko", "title_en", "narration_ko", "narration_en", "expect_ko", "expect_en", "feature", "requires": ["POST /api/v1/..."], "call", "expect", "save", "fallback", "links"}`.
   - `call`: `engine.http(method, path, json, params, as_="operator"|"tenant:<id>"|"none", service="nocp"|"ai"|"nico", headers)` — 값에 `lambda ctx: ...` 사용 가능. 또는 `callable(ctx)`(내부 함수).
   - `expect`: `(ok, 문장_ko, 문장_en)`을 돌려주는 함수(`ok=None`이면 건너뜀), 또는 `{"status": 201}`.
   - `requires`의 라우트가 빌드에 없으면 `pending feature <feature>`로 표시된다.
3. 격리: 만드는 이름(테넌트·가입 조직·사업자번호·시리얼·위치)에는 `ctx["uid"]`를 붙인다(`engine.uniq("demo-x")`, `janitor.brn_for(uid)`, `_common.tenant_step`). 실행이 쓴 데모 테넌트(`tnt-…demo…`)는 컨텍스트에서 자동으로 기록되고, 다음 실행의 `00-cleanup`이 그 NVKE 클러스터·BMaaS 할당을 돌려놓는다. 다른 시나리오가 남긴 상태를 가정하지 않는다. 모듈에 `reset()`이 있으면 `/api/v1/demo/reset` 때 호출된다.
4. 시연 순서: `app/demo/playlist.py`의 `STEPS`에 시나리오 id를 해당 DEMO-ORDER 단계에 넣는다(넣지 않으면 Day 2 · order 99로 맨 뒤). `day`·`order`·`showcase_order`는 로드할 때 여기서 붙는다.
5. 테스트: `tests/test_demo_isolation.py`가 모든 시나리오를 두 번 순서대로, 두 번 역순으로 한 NOCP에서 돌려 단독 실행보다 실패가 늘지 않는지, `day`·`order`·`/playlist`·CORS를 확인한다. `tests/test_demo_runner.py`가 모든 시나리오를 오프라인 빌드에서 돌려 `failed`가 없는지 확인한다.
