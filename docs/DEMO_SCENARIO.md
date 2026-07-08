# BMaaS 서비스 E2E 시연 시나리오 (13막 · 포털 여정)

역할 기반의 BMaaS 서비스 여정으로 전체 동작을 시연·검증한다:
**Biz 포털**(계약·과금) → **고객 포털**(셀프서비스·SLA·티켓) → **NeoCloud OS**
(파이프라인·격리·스토리지) → **NICo/설비**(에뮬레이션) → **운영 포털**(break-fix·티켓·정합성).

```bash
cd ~/vrcm && ./run.sh                                  # ① 서버 기동 (별도 터미널)
.venv/bin/python scripts/demo_scenario.py              # ② 자동 실행 (33개 검증, ~10초)
.venv/bin/python scripts/demo_scenario.py --pause      # ③ 발표 모드 — 막마다 Enter, 화면 병행
pytest tests/test_e2e_scenario.py -q                   # CI 검증 (동일 여정, 2-SU 스케일)
```

## 서비스 화면 구성 (7종 — 우상단 네비로 상호 이동)

| 화면 | 역할 |
|---|---|
| `/ops` 운영 포털 | **⚡Fulfillment 보드(승인 대기/인도 완료/실패·거절 3분류, 셰브런 진행 트랙+펄스 강조)** · **테넌트 Observability(NICo `/health` 벌크 센서 직접 연동 — GPU 노드 테이블·BMC 전력/온도/냉각수 + DCGM 추이 차트 + XID 이벤트)** · Break-fix 큐 · Reconcile · 티켓 처리 |
| `/customer` 고객 포털 | 테넌트 스코프: 클러스터 LIVE·**서비스 현황 라인그래프 4종(util/전력+cap/온도/처리량·NVLink)**·**랙별 상세**·SLA 가시성·셀프서비스·티켓·스토리지 |
| `/biz` 비즈 포털 | 고객·계약 관리 · **전체 GPU 사용 현황(할당률·util·전력 추이 그래프)** · **테넌트별 세부 현황(행 클릭 → 고객 추이 그래프)** · 사용량/과금 프리뷰 · 티켓 큐 |
| `/` VRCM | 인벤토리·토폴로지·할당 (운영 상세) |
| `/flow` 검증 콘솔 | 파이프라인 리플레이·장애 주입·reconcile 스테이징 |
| `/nico` NICo 대시보드 | NICo REST 전체 탐색·트레이 에뮬레이션 LIVE·IB fabric |
| `/arch` 아키텍처 | 구성도 위 주문 리플레이 + 단계별 하부 API 전체 리스트 |

## 승인 게이트 fulfillment (시연 하이라이트)

비즈 포털 "계약 + 개통 요청" → 주문이 `approval_mode`로 생성되어 **운영 포털
Fulfillment 큐**에 대기. 운영자가 파이프라인 단계(정책·배치→예약→프로비저닝→격리→
스토리지→인수검증→인도)를 **하나씩 승인**하며, 게이트 사이마다 고객/비즈 포털의
"운영 승인 대기: {단계}" 배지, `/nico` 호스트 상태(pool_ready→reserved→allocated),
`/arch` 플로우 단계·Operator 승인 이벤트, `/flow` 그리드가 실시간 싱크된다.
거절 시: 시작 전이면 rejected, 진행분이 있으면 saga 보상으로 완전 원복.
API: `POST /orders` `{approval_mode:true}` → `POST /orders/{id}/approve|reject`.

## 막 구성

| 막 | 여정(역할) | 핵심 검증 | 화면 |
|---|---|---|---|
| 0 | 초기화 | 30MW 재구성(2,772노드), 티켓/VAST/VPC 공백, 화면 7종 서빙 | `/flow` ⟲ |
| 1 | 인프라 검증 | 29.68MW·NICo 서비스 9종·스파인4×SU11 | `/nico` |
| 2 | **Biz — 계약 체결** | 테넌트 생성→VRF/VNI 자동 바인딩, 요율표 | `/biz` |
| 3 | **고객 — 셀프서비스 개통** | VR 16랙 delivered(0.1s)·VAST 8,000TB·P_Key | `/customer` |
| 4 | 플로우 감사 | 하부 호출 3,196건 (Redfish 576·HBN 288·NMX 16…) | `/arch` |
| 5 | 운영(에뮬) | training 91%/2.6MW → inference 55%/1.7MW | `/nico` LIVE |
| 6 | **고객 — SLA 가시성** | 리드타임 0.1s·가용성 100%·격리 PASS | `/customer` SLA |
| 7 | 장애/saga | 주입 실패→failed·1대 격리·재시도 delivered | `/flow` |
| 8 | **티켓 (고객→운영)** | 접수(high)→진행(코멘트)→해결, 고객 확인 | `/customer`+`/ops` |
| 9 | 정합성 | GHOST/ORPHAN/MISMATCH 검출→복구 | `/ops` Reconcile |
| 10 | **고객 — 확장** | +2랙, P_Key 재사용 | `/customer` |
| 11 | **회수 + Biz 과금** | sanitize 7단계·역순 해체, beta 라인 마감·acme 월 환산 산출 | `/biz` 과금 |
| 12 | 복원 | 시작 상태 완전 복원 (멱등) | `/flow` ⟲ |

## 실제 NICo 연동에 대하여

실제 NICo(github.com/NVIDIA/infra-controller)는 Rust+Go 워크스페이스로, 실행에
Kubernetes(HA 3노드)·PostgreSQL·Temporal·BMC/DPU 엔드포인트가 전제된다(웹 UI는
API 서비스에 내장·OIDC). 로컬 데모 환경에서는 기동이 불가하므로 본 워크스페이스는
Fake NICo가 그 REST 표면을 대역한다. **실 연동 시 교체 지점**: `adapters.py`의
`LocalNicoAdapter` → `NicoHttpAdapter(base_url=실제 site controller)`, NICo 자체
포털은 해당 컨트롤러 URL로 접속(운영 포털에서 링크 연결).
