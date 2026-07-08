# NeoCloud Control-Plane — 연동 가능 API 레퍼런스

> 실행 중인 서버의 OpenAPI 스키마(`GET /openapi.json`)에서 추출한 **전체 98개 엔드포인트**를
> Control-Plane 모듈 구조(①~⑦, cp-*)로 분류한 연동 레퍼런스.
> 대화형 문서는 http://127.0.0.1:8000/docs (Swagger UI).
>
> 표기 — **[실연동]**: 실 배치에서 그대로 노출되는 Northbound API ·
> **[에뮬]**: 시뮬레이터 대역 API(실 시스템의 API 형상에 대응, 어댑터 교체 지점) ·
> **[데모]**: 검증 콘솔 전용(장애 주입 등, 실 배치에서 제거)

---

## A. Northbound / Public APIs (cp-api) — `/api/v1/*`

포털 3종·외부 시스템이 소비하는 공식 연동 표면. 실 배치에서는 OIDC Bearer(SEC01) +
RBAC(SEC04)로 보호된다.

### A-1. Service-Order Intake · Tenant Fulfillment (cp-intake / cp-fulfill)

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/v1/orders` | **[실연동]** 주문 생성 — `kind: new`(개통·확장) / `terminate`(회수). body: `tenant_id, blueprint_key, racks, approval_mode, storage_mode(auto\|manual), storage_tb, storage_gbps, allocation_id` |
| GET | `/api/v1/orders` | **[실연동]** 주문 목록 (`?tenant_id=` 필터) |
| GET | `/api/v1/orders/{id}` | **[실연동]** 주문 상세 — 상태머신 history, `access_package`(딜리버리 접속·인증 정보, secret 1회 노출) 포함 |
| POST | `/api/v1/orders/{id}/approve` | **[실연동]** 승인 게이트 — 다음 파이프라인 단계 1개 실행 (운영 포털) |
| POST | `/api/v1/orders/{id}/reject` | **[실연동]** 거절 — 진행분 saga 보상 원복 |
| GET | `/api/v1/orders/{id}/flow` | **[실연동]** 단계별 하부 호출 API 전체 버킷팅 (감사·검증 콘솔용) |

### A-2. Resource & Service Model (cp-model · M3 인벤토리/미러)

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/inventory/summary` | **[실연동]** 용량·전력·세대별 집계 (CAP01) |
| GET | `/api/v1/topology/tree` | **[실연동]** Factory▸Block(층)▸DU▸SU▸Rack 트리 (+`power_mw`, `ready`) |
| GET | `/api/v1/factories` · `/scalable-units` · `/scalable-units/{id}` | **[실연동]** 사이트/SU 조회 |
| POST | `/api/v1/scalable-units` | **[실연동]** SU 추가 프로비저닝 (`?blueprint_key=`) |
| GET | `/api/v1/racks` · `/racks/{id}` · `/racks/{id}/gpus` · `/trays/{id}` | **[실연동]** 랙/트레이/GPU 상세 |
| POST | `/api/v1/racks/{id}/power-policy` | **[실연동]** MaxQ/MaxP 전력 정책 |
| GET | `/api/v1/nodes` · `/nodes/{id}` · `/nodes/summary` | **[실연동]** NodeInstance 미러 (`?state=&tenant_id=`) — CAP02 |
| GET | `/api/v1/cpu-nodes` | **[실연동]** CPU 노드 풀(테넌트당 5대, DPU·VPC) — DMS01/02 |
| GET | `/api/v1/blueprints` · `/spec` | **[실연동]** 세대 카탈로그(GB200/GB300/VR)·하드웨어 상수 |
| POST | `/api/v1/reconcile/run` | **[실연동]** SoT 정합성 감사 — GHOST/ORPHAN/STATE_MISMATCH 검출·복구 |

### A-3. Policy Orchestration · 테넌시/격리 (cp-policy)

| Method | Path | 설명 |
|---|---|---|
| POST / GET | `/api/v1/tenants` · `/tenants/{id}` | **[실연동]** 테넌트 생성(IAM realm·VNI/VRF 자동 바인딩)·조회 |
| GET | `/api/v1/tenants/{id}/isolation` | **[실연동]** 4계층 격리 검증 리포트 (acceptance 게이트) |
| POST / GET / DELETE | `/api/v1/allocations` | **[실연동]** SU/rack-set 수동 할당·해제 |
| POST / GET | `/api/v1/nvlink-partitions` | **[실연동]** NVLink 파티션 (NMX/GFM 모델) — NET02 |
| GET | `/api/v1/network/vni-map` | **[실연동]** 테넌트 VNI/VRF 맵 (SDN01) |
| GET | `/api/v1/fabric/ib` | **[실연동]** IB spine-leaf 토폴로지 (`?tenant_id=` → P_Key 스코프) — NET01 |

### A-4. Observability · 장비 헬스 (cp-obs — CAP05·TEL)

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/health/equipment` | **[실연동]** 사이트/층별 장비 HwState 집계 + 비정상 리스트 (단일 SoT) |
| PATCH | `/api/v1/equipment/state` | **[실연동]** 운영자 조치 — rack/tray/gpu × faulted/maintenance/ready |
| GET | `/api/v1/emu/clusters` · `/emu/trays` · `/emu/trays/{id}` · `/emu/status` | **[에뮬]** 트레이 텔레메트리(DCGM 대응) — 실 배치: NVSentinel/DCGM 스트림 |
| GET | `/api/v1/emu/history` | **[에뮬]** 시계열(전역/테넌트, 240틱) — 실 배치: OTLP 시계열 저장소 |
| POST | `/api/v1/emu/tick` · `/emu/clusters/{tid}/workload` | **[데모]** 에뮬 틱 강제·워크로드 프로파일 전환 |
| GET / DELETE | `/api/v1/trace` | **[에뮬]** 시스템 세부 트레이스 버스 — 실 배치: OTel span/NATS `neocloud.telemetry.*` |

### A-5. Business (비즈 포털 백엔드 — 티켓·과금)

| Method | Path | 설명 |
|---|---|---|
| POST / GET | `/api/v1/tickets` | **[실연동]** 티켓 생성·목록 (`?tenant_id=&status=`) |
| PATCH | `/api/v1/tickets/{id}` | **[실연동]** 상태 전이 open→in_progress→resolved + 코멘트 |
| GET | `/api/v1/billing/usage` · `/billing/rates` | **[실연동]** rack-hour 사용량·월 환산, 데모 단가표 |

### A-6. 시스템·관리

| Method | Path | 설명 |
|---|---|---|
| GET | `/health` | **[실연동]** 헬스체크 (SLA 관리면 측정 대상) |
| POST | `/api/v1/admin/reseed` | **[데모]** 전체 재시드 (`?blueprints=` 조합) |
| GET | `/` `/ops` `/customer` `/biz` `/flow` `/nico` `/arch` | 웹 화면 7종 |

---

## B. ④ Compute Services — NICo 에뮬레이션 `/fake-nico/*`

실 **NICo(NVIDIA/infra-controller)** API Service의 형상에 대응하는 시뮬레이터.
Control-Plane은 이 API를 직접 호출하지 않고 **D1 ComputeAdapter**를 경유하므로,
실 연동 시 `LocalNicoAdapter` → `NicoHttpAdapter`(REST/gRPC) 교체만으로 전환된다.
(실 NICo의 정확한 리소스 경로·스키마는 NVIDIA 배포본 기준 재확인 필요)

### B-1. 읽기 API — 실 NICo 리소스 대응

| Method | Path | 실 NICo 대응 개념 |
|---|---|---|
| GET | `/fake-nico/site` | 사이트/설비 정보 |
| GET | `/fake-nico/hosts` · `/hosts/{id}` | 호스트 인벤토리·상태 (외부 SoT) |
| GET | `/fake-nico/instance-types` | 인스턴스 타입 카탈로그 |
| GET | `/fake-nico/instances` | 테넌트 인스턴스(할당) 목록 |
| GET | `/fake-nico/jobs` · `/jobs/{id}` | 비동기 job 폴링 (provision/sanitize 수렴) |
| GET | `/fake-nico/dhcp/leases` | DPU-DHCP 리스 테이블 |
| GET | `/fake-nico/hosts/{id}/hardware` | HW 시리얼·펌웨어 (BFX03) |
| GET | `/fake-nico/hosts/{id}/health` | 호스트 BMC 센서 (전력·온도·냉각수) |
| GET | `/fake-nico/health?tenant_ref=` | 벌크 헬스 — 운영 포털 Observability가 직접 소비 (CAP05) |
| GET | `/fake-nico/hosts/{id}/attestation` | TPM attestation 결과 (CNP09) |
| GET | `/fake-nico/hosts/{id}/sanitize-report` · `/sanitize-reports` | 7단계 소거 보고서 (SEC21) |

### B-2. Day 0/1/2 액션 API — D1 어댑터가 호출

| Method | Path | 파이프라인 사용 지점 |
|---|---|---|
| POST | `/fake-nico/hosts/{id}/reserve` · `/unreserve` | reserved 단계 (gRPC ReserveHost 대응) |
| POST | `/fake-nico/hosts/{id}/provision` | provisioning — BMC(Redfish)→DHCP→PXE→cloud-init 서브스텝 트레이스 |
| POST | `/fake-nico/hosts/{id}/abort-provision` | saga 보상 경로 |
| POST / DELETE | `/fake-nico/instances` · `/instances/{id}` | 테넌트 할당(allocate)/회수(release) |
| POST | `/fake-nico/hosts/{id}/sanitize` | 회수 시 7단계 소거 (NVMe·GPU·TPM) |
| POST | `/fake-nico/hosts/{id}/cordon` | break-fix cordon (BFX01) |
| POST / GET / DELETE | `/fake-nico/segments` | 테넌트 VPC 세그먼트 — DPU HBN(VXLAN/EVPN) 적용 |

### B-3. 데모 전용 (실 배치 제거 대상)

| Method | Path | 용도 |
|---|---|---|
| POST | `/fake-nico/hosts/{id}/inject` | 다음 1회 실패 주입 (provision/sanitize) — saga 검증 |
| POST | `/fake-nico/hosts/ghost` | GHOST 스테이징 (reconcile 데모) |
| DELETE | `/fake-nico/hosts/{id}` | ORPHAN 스테이징 |
| PATCH | `/fake-nico/hosts/{id}/state` | STATE_MISMATCH 스테이징 |
| PATCH | `/fake-nico/config` | job 지연 조정 (폴링-수렴 데모) |

---

## C. ⑥ Storage Services — VAST VMS 에뮬레이션 `/fake-vast/*`

실 **VAST VMS REST v3**(`/api/v3/views·quotas·qospolicies`) 대응. Control-Plane은
D4 StorageAdapter 경유 — 실 연동 시 VMS REST 어댑터로 교체.

| Method | Path | 설명 |
|---|---|---|
| GET | `/fake-vast/views` | **[에뮬]** 테넌트 뷰 목록 — 경로·용량(Quota)·QoS(대역폭/IOPS)·export 서브넷(테넌트 VRF 제한) |

(생성/삭제는 주문 파이프라인 storage_binding/reclaiming 단계에서 어댑터가 내부 수행 —
트레이스 채널 `VAST-API`로 관측)

---

## D. ⑦ Shared Services — IAM·Vault·PAM 에뮬레이션 `/fake-shared/*`

실 배치 대응: **Keycloak**(OIDC admin/token) · **Vault**(KV v2) · PAM 게이트웨이.
SEC01·SEC04·SEC07·SEC08·SEC09 매핑.

| Method | Path | 설명 |
|---|---|---|
| GET | `/fake-shared/iam/realms` · `/iam/realms/{tenant_id}` | **[에뮬]** 테넌트 realm — 롤 3종(RBAC)·클라이언트(MFA)·SA(주문별, secret 마스킹) |
| POST | `/fake-shared/iam/token` | **[에뮬]** OIDC client-credentials 토큰 발급 (비활성 클라이언트 403 + 감사 denied) |
| GET | `/fake-shared/secrets?tenant_ref=` | **[에뮬]** Vault KV 목록 (값 마스킹 — s3-access-key·redfish-cred) |
| POST / GET | `/fake-shared/pam/sessions` | **[에뮬]** 권한상승 세션 개시(operator/target/reason/TTL·녹화)·목록 |
| POST | `/fake-shared/pam/sessions/{id}/close` | **[에뮬]** 세션 종료 |
| GET | `/fake-shared/audit?tenant_ref=&limit=` | **[에뮬]** 보안 감사 트레일 (SEC08) — IAM/Vault/PAM/딜리버리 전 이벤트 |

---

## E. 실 연동 교체 지점 요약

| 에뮬레이션 | 실 시스템 | 교체 방법 |
|---|---|---|
| `/fake-nico/*` (FakeNico) | NICo — NVIDIA/infra-controller (REST/gRPC, K8s·PG·Temporal 전제) | `adapters.py`의 `LocalNicoAdapter` → `NicoHttpAdapter` (계약 동일) |
| `/fake-vast/*` (FakeVast) | VAST VMS REST v3 | D4 StorageAdapter 구현체 교체 |
| `/fake-shared/*` (FakeSharedServices) | Keycloak(OIDC) · Vault(KV) · PAM | `shared_services.SHARED` 호출 지점을 실 클라이언트로 교체 |
| `/api/v1/trace` (TRACER) | OTel collector · NATS `neocloud.telemetry.*` | emit() 호출부가 span 생성 지점 (M8 로드맵) |
| `/api/v1/emu/*` (EMULATOR) | NVSentinel · DCGM 텔레메트리 | 읽기 API 형상 유지, 소스만 교체 |
