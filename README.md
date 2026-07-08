# VRCM — Vera Rubin Cluster Manager

NeoCloud(GPUaaS) **컨트롤 플레인 MVP**. NVIDIA **NVL72** 시스템(**GB200 / GB300 / Vera
Rubin**)으로 구성된 멀티 세대 GPU 클러스터(DSX AI Factory)를 관리한다. 두 NVIDIA 레퍼런스
문서를 데이터 모델·API로 코드화했다.

### 지원 세대 (MGX)

| 블루프린트 | 모델 | GPU/CPU | HBM | NVLink | 랙 TDP | 냉각 |
|---|---|---|---|---|---|---|
| `gb200-nvl72` | GB200 NVL72 (Gen 1.1) | Blackwell / Grace | 192 GB HBM3e | NVLink5 (1.8 TB/s) | 120 kW (공칭 · MaxQ 미공개) | hybrid |
| `gb300-nvl72` | GB300 NVL72 (Gen 1.1)¹ | Blackwell Ultra / Grace | 288 GB HBM3e | NVLink5 | 135 kW (피크 ~155 · MaxQ 미공개) | liquid |
| `vr-nvl72` | Vera Rubin NVL72 (Gen 1.2) | Rubin / Vera | 288 GB HBM4 | NVLink6 (3.6 TB/s) | 227 kW (MaxP 227 · MaxQ 187) | liquid |

¹ GB300 전력은 공개 추정치(`preliminary=true`). 한 AI Factory에 세대 혼재 가능.

> 근거 문서
> - *NVIDIA DSX Facilities Infrastructure Design Guide* v1.0 (2026-03-12)
> - *NVIDIA Cloud Partner: Vera Rubin NVL72 Systems Reference Design* (PRD12771-001 v3.0)

## 이번 MVP 범위

| 도메인 | 상태 | 내용 |
|---|---|---|
| **인벤토리 & 토폴로지** | ✅ 구현 | AI Factory ▸ Block ▸ DU ▸ SU ▸ Rack(NVL72) ▸ Tray ▸ GPU/CPU/DPU 전개, 용량·전력 집계, 블루프린트 기반 SU 프로비저닝, MaxQ/MaxP 전력 정책 |
| **멀티테넌시 & 격리** | ✅ 구현 | 테넌트 생명주기, 용량 할당(SU/rack-set/HAC), NVLink 파티션(GFM 모델), 자동 VNI/VRF 바인딩, 4계층 격리 검증 리포트 |
| **서비스 라이프사이클 (M1/M3/M4-lite)** | ✅ 구현 | 주문 파이프라인(saga 보상 포함), NodeInstance·ServiceOrder 상태머신, NVL 도메인 무결성 배치, 회수·sanitization, NICo reconcile(GHOST/ORPHAN/MISMATCH) |
| **D1 ComputeAdapter + Fake NICo** | ✅ 구현 | ComputeAdapter 계약(Local/HTTP 구현체), NICo Day 0/1/2 시뮬레이터(job 폴링·장애 주입) — 실 NICo 연동 시 어댑터만 교체 |
| 헬스 & 텔레메트리 | 🔜 로드맵 | GPU/DPU/네트워크/시설 통합 모니터링 (NVSentinel 연동) |
| 전력 & 냉각 연동 | 🔜 로드맵 | DSX Exchange(MQTT) IT-OT, CDU/TCS, Coordinated Leak Response |
| 과금 & 미터링 | 🔜 로드맵 | 테넌트별 사용량 계측·빌링 (NeoCloud 매출 계층) |

상세 설계와 로드맵은 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 참조.

## 빠른 시작

```bash
cd ~/vrcm
./run.sh                      # venv 생성 + 의존성 설치 + 서버 기동
```

- **포털 3종**: 운영 http://127.0.0.1:8000/ops · 고객 /customer · 비즈 /biz (티켓·SLA·과금 프리뷰)
- 대시보드: http://127.0.0.1:8000/
- 동작 검증 콘솔: http://127.0.0.1:8000/flow — 사용법: [docs/FLOW_CONSOLE_GUIDE.md](docs/FLOW_CONSOLE_GUIDE.md)
- NICo 운영 대시보드: http://127.0.0.1:8000/nico — REST 전체 탐색 + 트레이 에뮬레이션 LIVE
- 아키텍처 플로우: http://127.0.0.1:8000/arch — 구성도 위 주문 리플레이 + 단계별 하부 API 전체 리스트
- API 문서(OpenAPI): http://127.0.0.1:8000/docs
- **연동 API 레퍼런스** (NICo 에뮬레이션 포함 98 엔드포인트, 실연동 교체 지점): [docs/API_REFERENCE.md](docs/API_REFERENCE.md)
- **DPU Isolation 동작 상세** (실 NICo infra-controller 코드 분석 + vrcm 매핑): [docs/DPU_ISOLATION.md](docs/DPU_ISOLATION.md)
- 헬스체크: http://127.0.0.1:8000/health

기본 시드는 **Phase 1 실배치 구성 (전량 Vera Rubin)** — 2개 사이트 × 각 2개 층:

| 사이트 | 층 | 가동 | GPU (랙) |
|---|---|---|---|
| STT 가산 | 1층 6MW | '27.3월 | 1,728 (24랙) |
| STT 가산 | 2층 3MW | '27.9월 | 864 (12랙) |
| IGIS 안산 | 1층 12.9MW | '27.11월 | 3,888 (54랙) |
| IGIS 안산 | 2층 10.4MW | '28.1월 | 3,600 (50랙) |

합계 **10,080 GPU / 140랙 / IT MaxQ 26.2MW (MaxP 31.8MW)** + CPU 노드 풀 60대. 테스트는 2-SU 축소 시드 사용.
포털 기능에는 **NVIDIA BMaaS Requirements Guide v2.2** 매핑 배지(`NVIDIA Req · <ID>`)가
표시되며, 운영 포털에 전체 준수 현황 패널이 있다.

세대 추가 프로비저닝:
```bash
# GB300 SU 추가
curl -XPOST "localhost:8000/api/v1/scalable-units?blueprint_key=gb300-nvl72"
# 지원 세대 카탈로그
curl localhost:8000/api/v1/blueprints
# 시드 재구성 (원하는 세대 조합)
curl -XPOST "localhost:8000/api/v1/admin/reseed?blueprints=gb200-nvl72,gb300-nvl72,vr-nvl72"
```

## 테스트 · 시연

```bash
. .venv/bin/activate
pytest -q                                        # 45 tests (E2E 시나리오 포함)
python scripts/demo_scenario.py                  # 전체 동작 시연 러너 (11막·37검증)
python scripts/demo_scenario.py --pause          # 발표 모드
```

시연 시나리오 상세: [docs/DEMO_SCENARIO.md](docs/DEMO_SCENARIO.md)

## 핵심 API 예시

```bash
# 인벤토리 요약
curl localhost:8000/api/v1/inventory/summary

# 테넌트 생성 (자동 VNI/VRF 바인딩)
curl -XPOST localhost:8000/api/v1/tenants \
  -H 'content-type: application/json' \
  -d '{"name":"fin-corp","isolation_tier":"vm_multitenant"}'

# SU 통째 할당 (티어에서 DPU 모드 자동 도출)
curl -XPOST localhost:8000/api/v1/allocations \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"tnt-fin-corp","su_id":"su-1","scope":"scalable_unit"}'

# NVLink 파티션 (compute isolation)
curl -XPOST localhost:8000/api/v1/nvlink-partitions \
  -H 'content-type: application/json' \
  -d '{"rack_id":"su-1-rack-00","tenant_id":"tnt-fin-corp","tray_ids":["su-1-rack-00-tray-00"]}'

# 격리 검증 리포트
curl localhost:8000/api/v1/tenants/tnt-fin-corp/isolation

# --- 서비스 라이프사이클 (M1-lite) ---
# 신규 개통 주문: 배치→예약→NICo 프로비저닝→격리→인수까지 한 번에 (실패 시 saga 롤백)
curl -XPOST localhost:8000/api/v1/orders \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"tnt-fin-corp","kind":"new","blueprint_key":"vr-nvl72","racks":2}'

# 회수: drain→release→sanitization(7단계)→풀 복귀 (실패 시 RMA 에스컬레이션)
curl -XPOST localhost:8000/api/v1/orders \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"tnt-fin-corp","kind":"terminate","allocation_id":"alloc-1"}'

# 노드 풀 현황 / NICo 정합성 감사
curl localhost:8000/api/v1/nodes/summary
curl -XPOST localhost:8000/api/v1/reconcile/run

# Fake NICo 직접 조작 (장애 주입 등)
curl -XPOST localhost:8000/fake-nico/hosts/nh-su-2-rack-00-tray-00/inject \
  -H 'content-type: application/json' -d '{"op":"provision"}'
```

## 구조

```
vrcm/
├─ app/
│  ├─ spec.py       # NVL72/DSX 하드웨어·토폴로지 상수 (문서 single source of truth)
│  ├─ models.py     # Pydantic 데이터 모델 (토폴로지 + 테넌시)
│  ├─ store.py      # 인메모리 저장소 (교체 가능 인터페이스)
│  ├─ seed.py       # 블루프린트 기반 SU 프로비저닝 + 기본 시드
│  ├─ topology.py   # 인벤토리 & 토폴로지 API
│  ├─ tenancy.py    # 멀티테넌시 & 격리 API + 검증 로직
│  ├─ lifecycle.py  # 주문 파이프라인(saga)·상태머신·배치·reconcile (M1/M3/M4-lite)
│  ├─ adapters.py   # D1 ComputeAdapter 계약 + Local/HTTP 구현체
│  ├─ nico_fake.py  # NICo Day 0/1/2 시뮬레이터 (+ /fake-nico REST)
│  ├─ main.py       # FastAPI 조립 + lifespan 시드
│  └─ static/index.html   # 경량 대시보드
├─ tests/           # pytest (topology + tenancy + lifecycle)
├─ docs/ARCHITECTURE.md   # 상세 설계서
├─ requirements.txt
└─ run.sh
```
