# VRCM 아키텍처 설계서

> Vera Rubin Cluster Manager — NeoCloud 컨트롤 플레인
> 대상: SKT AI 인프라/GPUaaS. 근거: NVIDIA DSX Facilities Design Guide v1.0 + NCP Vera Rubin NVL72 Reference Design v3.0

---

## 1. 문제 정의 — "무엇을 관리하는가"

두 NVIDIA 문서를 종합하면 관리 대상은 **계층적 빌딩 블록**과 그 위의 **멀티테넌트 운영**이다.
랙은 **멀티 세대**(MGX Gen 1.1 = GB200/GB300, Gen 1.2 = Vera Rubin)이며 한 팩토리에 혼재한다.

```
AI Factory (250 MW)
└─ Compute Block (×4)              # 6 DU
   └─ Deployment Unit (×6)         # 가용영역, 99.9%, 3~4 SU + Power Block
      └─ Scalable Unit (×3~4)      # 14 NVL72 랙 = 1,008 GPU, HAC 단위 (세대 동질)
         └─ Rack: NVL72            # = 1 NVLink 도메인(72 GPU)
            │                      #   GB200 120kW · GB300 140kW · VR 227kW
            ├─ Compute Tray (×18)  # 4 GPU + 2 CPU + 1 BlueField + N×ConnectX
            └─ NVLink Switch Tray (×9)
```

**세대별 블루프린트**(`spec.BLUEPRINTS`)가 GPU/CPU 아키텍처, HBM, NVLink 세대, 전력 캡,
냉각을 캡슐화한다. SU/DU/Block 계층은 세대 무관이며, SU 랙 수는 세대별로 다르다
(`blueprint.racks_per_su` — VR은 NCP RD RD-12835-001-01 v02 기준 **16랙/SU = 1,152 GPU**,
1 SU = 1 HAC(2열×8랙, HAC TDP ~3.9MW), CMX 랙 2/SU, IB는 듀얼 네트워크 Fabric-A/B.
GB200/GB300은 14랙/SU 유지).
`POST /scalable-units?blueprint_key=...`로 임의 세대 SU를 프로비저닝하고,
인벤토리 집계는 `gpus_by_arch`·`racks_by_generation`으로 세대별로 분해된다.

이 위에서 NeoCloud는 **멀티테넌트**를 4계층으로 격리한다(NCP RD):
Identity → Process → Compute Virtualization → **Compute Isolation(NVLink 파티션)**.
물리 경계는 **SU/HAC**, 네트워크 경계는 **VXLAN + BGP EVPN + VRF**,
하드웨어 앵커는 **BlueField-4 DPU**(NIC / DPU / DPU Zero-Trust).

VRCM은 이 전체 그래프를 **데이터 모델로 코드화**하고, 인벤토리 조회와
멀티테넌시 할당·격리 검증을 API/대시보드로 제공한다.

---

## 2. 시스템 컨텍스트

```
        ┌────────────────────────────────────────────────┐
        │            NeoCloud 상품 계층 (별도)            │  ← 과금·셀프서비스·SLA (로드맵)
        └───────────────────────┬────────────────────────┘
                                │ REST API
        ┌───────────────────────┴────────────────────────┐
        │                    VRCM (本 MVP)                │
        │   Inventory & Topology  │  Multi-tenancy & Iso  │
        └───┬───────────────┬───────────────┬─────────────┘
   (로드맵) │               │               │ (로드맵)
   ┌────────┴───┐   ┌───────┴──────┐  ┌─────┴───────────┐
   │ NICo/BCM   │   │ GFM (NVLink) │  │ DSX Exchange    │
   │ 베어메탈   │   │ 파티션 관리  │  │ MQTT IT-OT 버스 │
   │ 프로비저닝 │   │              │  │ (전력/열/누수)  │
   └────────────┘   └──────────────┘  └─────────────────┘
        │                                     │
   ┌────┴──────────────── VR NVL72 클러스터 ──┴──────────┐
   │ GPU/DPU(BlueField)  ·  Spectrum-X/Converged/OOB  ·  CDU/전력 │
   └─────────────────────────────────────────────────────────────┘
```

본 MVP는 점선(NICo/GFM/DSX Exchange) 연동을 **모델 레벨로 추상화**했다.
실 하드웨어 연동은 어댑터로 교체 가능하도록 `store.py` 인터페이스를 좁게 두었다.

---

## 3. 데이터 모델

### 3.1 토폴로지 (인벤토리)

| 엔티티 | 핵심 필드 | 블루프린트 근거 |
|---|---|---|
| `AIFactory` | site, design_power_mw, blocks | 250 MW = 4 블록 |
| `ComputeBlock` | du_ids | 6 DU/블록 |
| `DeploymentUnit` | su_ids | 3~4 SU, 가용영역 |
| `ScalableUnit` | rack_ids(14), hac_id, cmx_racks(2) | 1,008 GPU |
| `Rack` | model, tdp_kw(227), power_cap_kw, tray_ids(18), nvlink_switch_tray_ids(9), tenant_id | NVL72 = 1 NVLink 도메인 |
| `ComputeTray` | gpu_ids(4), cpu_ids(2), dpu_id, connectx9(8) | tray 스펙 |
| `RubinGPU` | hbm4_gb(288), dies(2), state, tenant_id | |
| `VeraCPU` | cores(88), mem_tb(1.44) | |
| `BlueFieldDPU` | sku(B4240V), bandwidth(800G), **mode** | 격리 앵커 |

ID는 사람이 읽는 슬러그(`su-1/rack-03/tray-07/gpu-2`)라서 토폴로지가 grep 가능하고
재시드 후에도 안정적이다. 모든 상수는 `app/spec.py`에 단일화(문서 출처).

### 3.2 멀티테넌시

| 엔티티 | 핵심 필드 | 의미 |
|---|---|---|
| `Tenant` | isolation_tier, sla_tier | bare_metal_dedicated / vm_multitenant / k8s_namespace |
| `NetworkIsolation` | compute_l3vni, converged_vni, oob_vni, vrf | 테넌트 생성 시 자동 바인딩 |
| `Allocation` | scope(SU/rack_set/HAC), su_id, rack_ids, dpu_mode | 용량 할당 |
| `NVLinkPartition` | rack_id(도메인), partition_id, tray_ids | GFM 파티션(compute isolation) |

---

## 4. 멀티테넌시 격리 모델 (핵심 로직)

### 4.1 티어 → 정책 자동 도출

| Tier | 물리 경계 | 기본 DPU 모드 | 비고 |
|---|---|---|---|
| `bare_metal_dedicated` | SU/HAC 단독 점유 강제 | `dpu` | 공유 시도 시 409 |
| `vm_multitenant` | SU 공유 허용 | `dpu_zero_trust` | DPU 에어갭으로 격리 |
| `k8s_namespace` | 소프트(fractional) | `dpu` | 네트워크 격리 의존 |

### 4.2 할당 시 강제되는 불변식 (invariants)

1. **랙 단일 점유** — 한 랙은 두 테넌트에 동시 할당 불가 (409).
2. **전용 테넌트 SU 독점** — `bare_metal_dedicated`는 같은 SU에 co-tenant 랙이 있으면 거부.
3. **VNI/VRF 유일성** — 테넌트마다 Compute/Converged/OOB 3 패브릭 VNI + VRF가 충돌 없이 배정.
4. **NVLink 파티션 무결성** — 파티션은 (a) 테넌트가 소유한 랙에서만, (b) tray가 그 랙 소속, (c) 동일 도메인 내 tray 중복 금지.

### 4.3 격리 검증 리포트

`GET /tenants/{id}/isolation` → 5개 레이어를 점검해 `pass/warn/fail` 산출:

- **identity** — 테넌트 스코프 접근제어 (항상 pass)
- **physical** — 전용 티어의 SU 단독 점유 여부
- **network** — VNI/VRF 유일성(타 테넌트와 충돌 검사)
- **process** — vm_multitenant인데 DPU Zero-Trust 아니면 warn
- **compute_isolation** — NVLink 파티션이 소유 랙 내에 정합

`fail`이 하나도 없으면 `ok=true`. 이 리포트가 SLA/컴플라이언스(금융·공공) 증빙의 기초.

---

## 5. API 표면

### 인벤토리 & 토폴로지
```
GET    /api/v1/inventory/summary          # GPU·CPU·DPU·HBM4·전력 집계
GET    /api/v1/topology/tree              # 중첩 트리(GPU는 카운트)
GET    /api/v1/factories | /scalable-units | /racks | /trays/{id}
GET    /api/v1/racks/{id} | /racks/{id}/gpus
POST   /api/v1/scalable-units             # 블루프린트로 신규 SU 프로비저닝
POST   /api/v1/racks/{id}/power-policy     # MaxQ ↔ MaxP 전력 캡
GET    /api/v1/spec                        # 블루프린트 상수
```

### 멀티테넌시 & 격리
```
POST   /api/v1/tenants                     # 생성(VNI/VRF 자동)
GET    /api/v1/tenants | /tenants/{id}
POST   /api/v1/allocations                 # SU/rack_set/HAC 할당
DELETE /api/v1/allocations/{id}            # 해제(파티션·바인딩 롤백)
POST   /api/v1/nvlink-partitions           # GFM 파티션
GET    /api/v1/nvlink-partitions
GET    /api/v1/network/vni-map             # 패브릭 VNI/VRF 맵
GET    /api/v1/tenants/{id}/isolation      # 격리 검증 리포트
```

---

## 6. 전력·냉각 모델링 (현재 반영 수준)

- **MaxQ/MaxP**: 랙 `power_cap_kw`로 표현. MaxQ=200kW(throughput), MaxP=227kW(time-to-train).
  `inventory/summary`의 `capped_power_mw`가 실제 프로비저닝 전력을 집계 →
  *동일 전력예산 하 캐파* 계산의 기초.
- **냉각 상수**(45°C TCS, 1.5 LPM/kW, CDU 2.3MW N+1)는 `spec.py`에 보유.
  실시간 텔레메트리·Leak Response는 로드맵(§7)에서 DSX Exchange 어댑터로 편입.

---

## 7. 로드맵 — 다음 도메인

| 우선 | 도메인 | 통합 포인트 |
|---|---|---|
| 1 | **헬스 & 텔레메트리** | NVSentinel(K8s 네이티브 GPU 장애·자동 cordon/drain), DCGM, Fleet Intelligence. `RubinGPU.state`에 faulted 전이 + 자동 remediation 훅 |
| 2 | **전력·냉각 IT-OT** | DSX Exchange(MQTT) 구독 → 랙/CDU 텔레메트리 수집, Coordinated Leak Response(BMS↔Cluster Manager), DSX MaxQ 동적 power cap |
| 3 | **스케줄링·할당** | Slurm/KAI Scheduler/Run:ai 연동, NVLink 파티션을 GFM API로 실제 집행 |
| 4 | **과금·미터링** | 테넌트별 GPU-시간/전력 계측 → 빌링 (NeoCloud 매출 계층) |
| 5 | **프로비저닝 백엔드** | NICo(베어메탈)·BCM·DPF(BlueField 대규모 관리) 어댑터로 `store.py` 교체 |

설계 원칙: **운영 코어(스케줄러·헬스)는 NVIDIA OSS 차용, 멀티테넌시·과금·격리 정책 계층은
VRCM이 자체 보유** — DSX OS가 비워둔 상품화 계층이 SKT의 차별화 지점이기 때문.

---

## 8. 기술 결정 기록 (요약)

- **FastAPI + Pydantic v2** — OpenAPI 자동화, 타입 안전 데이터 모델, SKT 기존 Python 환경 정합.
- **인메모리 store + 좁은 인터페이스** — MVP 단순성. 프로덕션은 RDB/CMDB로 교체(인터페이스 불변).
- **블루프린트 기반 프로비저닝** — 시드와 런타임 SU가 구조적으로 동일(`provision_scalable_unit` 단일 경로).
- **사람이 읽는 결정적 ID** — grep 가능·재현 가능한 토폴로지.
- **격리 검증을 일급 기능으로** — 할당 시 불변식 강제 + 사후 리포트. 규제 산업 증빙 대비.
