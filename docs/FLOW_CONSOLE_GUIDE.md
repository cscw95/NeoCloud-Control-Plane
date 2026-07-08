# 동작 검증 콘솔 사용 가이드 (`/flow` · `/nico` · `/arch`)

> **`/arch` — 플랫폼 아키텍처 플로우**: NeoCloud Platform Architecture 구성도(포털 3종 →
> Control Plane 기능 블록 10종 → 인프라 도메인 4종) 위에서 주문 플로우를 확인하는 화면.
> 주문 선택 → `▶ 리플레이`로 단계별 리플레이: 각 단계에서 관여하는 구성도 블록·도메인·
> 화살표가 점등되고, 좌측 상태머신에 단계별 **하부 호출 API 건수·채널 분포**, 우측에
> **API 전체 리스트**(NICo REST/gRPC + BMC Redfish + DHCP + PXE + cloud-init + DPU
> HBN + UFM + NMX + VAST 전부)가 표시된다. 단계 클릭으로 개별 탐색도 가능.
> 데이터: `GET /api/v1/orders/{id}/flow` — 트레이스를 단계 시간창으로 버킷팅
> (order_id / 노드 host_id / 무태그 시스템 이벤트 귀속).

> **`/nico` — NICo 운영 대시보드**: NICo REST API 전체 표면을 탐색하며 동작을 이해하는 페이지.
> ① Site Controller 서비스(9종)·집계 ② 호스트 인벤토리(상태 필터·검색) ③ 호스트 드릴다운
> (hardware/health BMC 센서/attestation TPM PCR/sanitize) ④ 인스턴스 타입 카탈로그·인스턴스·
> Segments(VPC)·Jobs·DHCP Leases ⑤ **테넌트 클러스터 컴퓨트 트레이 에뮬레이션(LIVE, 2s 틱)**
> — 트레이 히트맵(색=GPU util, 빨간 테두리=fault), 클러스터 카드(평균 util·전력/캡·온도·NVLink·
> tok/s·ECC·fault), 워크로드 프로파일 전환(training/inference/idle) ⑥ 트레이 상세(GPU 4개별
> util/온도/전력/HBM/ECC/XID). XID 폴트는 트레이스 버스(DCGM→NVSentinel)에도 기록된다.
> ⑥ **IB 패브릭 토폴로지** — rail-optimized 2-tier(core 스파인 4 × SU별 rail leaf) SVG.
> 전체 물리 자원 뷰에서 테넌트를 선택하면 해당 P_Key 파티션의 GPU fabric(랙·leaf·스파인
> 경로)만 하이라이트. 딥링크: `/nico#tenant=tnt-x`. 데이터: `GET /api/v1/fabric/ib`.
> 사용 순서: `/flow`에서 개통 → `/nico`에서 라이브 운영 상태 확인.
>
> **기본 시드 = Phase 1 30MW**: VR SU×10 + GB200 SU×1 = 154랙 / 2,772트레이 /
> 11,088 GPU ≈ 29.7MW(MaxQ). 주문은 SU 초과 규모 가능(랙 수 프리셋 1 SU=14) — 단일 SU
> best-fit 후 부족분은 SU 스팬(spill) 배치되며 SU별 allocation으로 분리된다.
> 호스트 인벤토리는 50대/페이지 페이지네이션, 인스턴스·Jobs·Leases는 전체 스크롤 리스트.

NeoCloud OS(주문 파이프라인·M3 미러·reconcile)와 Fake NICo(Day 0/1/2)의 상호작용을
브라우저에서 단계별로 검증하는 콘솔. 서버 기동 후 http://127.0.0.1:8000/flow 접속.

```bash
cd ~/vrcm && ./run.sh          # 또는: .venv/bin/python -m uvicorn app.main:app --port 8000
```

## 화면 구성

| 섹션 | 내용 |
|---|---|
| ① 모듈 아키텍처 | 요청이 지나가는 모듈 체인. 실행 중인 단계가 초록(실패 시 빨강)으로 점등 |
| ② 시나리오 컨트롤 | 리셋 / 테넌트 / 개통 / 장애 주입 / 회수 / reconcile / job 지연 |
| ③ 파이프라인 상태머신 | 주문 상태 전이를 타임스탬프 순으로 애니메이션 리플레이 |
| ④⑤ 미러 뷰 | NodeInstance(M3) vs NICo 호스트 — 랙×트레이 그리드, 상태별 색상 |
| D2 패널 | 테넌트 격리 구성 — VPC(VRF/VNI 3종) · NICo segment · NVLink 파티션 |
| D4 패널 | VAST 스토리지 — view 경로/용량/QoS/export 제한 (VMS 제어 결과) |
| ⑥ 수동 모드 | 호스트 1대에 Day 0/1/2 API를 버튼 하나씩 실행 (host_ip 표시) |
| ⑦ Reconcile 결과 | GHOST/ORPHAN/MISMATCH 발견 항목 (심각도 색상) |
| ⑧ 시스템 세부 동작 트레이스 | **API 로그와 별개의 내부 동작 전수 기록** — BMC(Redfish)·DHCP·PXE·cloud-init·DPU(NVUE/HBN)·UFM·NMX·VAST-API 채널별 메시지. 행 클릭 → 실제 페이로드 JSON. 필터(호스트/키워드)·채널 셀렉트 지원 |
| ⑨ API 콜 로그 | 페이지의 모든 HTTP 호출 기록. 행 클릭 → 요청/응답 JSON |

### ⑧ 트레이스에서 볼 수 있는 개통 1건의 세부 흐름 (랙 1개 기준 ~211건)
1. `Portal/API → M1` 주문 접수 → `M4 → M1` NVL 배치 결정 (랙 목록 페이로드)
2. 트레이별: `D1 → NICo` ReserveHost(gRPC) → ProvisionHost(REST)
3. 트레이별 NICo 남향 제어: `NICo → BMC` Redfish PXE 원스부트 + ForceRestart →
   `DPU-DHCP → Host` IP 임대(/30, yiaddr 페이로드) → `Host → NICo.PXE` 이미지 스트리밍 →
   `NICo.PXE → Host` cloud-init(고정 IP netplan·UEFI 잠금·BMC 로테이션)
4. `D1 → NICo` AllocateInstance(gRPC) → `NICo → FMDS` 메타데이터 등록
5. 격리: `D2 → NICo` VPC(segment) 생성 → 트레이별 `DPU-Agent → HBN` VRF/VXLAN/EVPN 적용
   → `D2 → UFM` P_Key 바인딩 → `D2 → NMX` NVLink 파티션
6. 스토리지: `D4 → VAST.VMS` view 생성 → CNode export 활성화 → quota → QoS
7. 검증: IsolationVerifier 3종 PASS (cross-vrf / ib-pkey / nvlink)
8. 회수 시 역순: NMX·UFM 해체 → segment 삭제 → VAST 뷰 파기 → release → sanitize 7단계

## 권장 시나리오 순서

### 0. 초기화
`⟲ 전체 리셋` — 테넌트·주문·노드·NICo를 재시드. 언제든 상태가 꼬이면 이걸 누른다.

### 1. 정상 개통 (F1 해피패스)
1. 테넌트 이름 입력 → `+ 생성`
2. 블루프린트 `vr-nvl72`, 랙 수 `1` → `▸ 개통 실행`
3. 관찰: ③에서 `received→…→delivered` 8단계 리플레이, ①에서 모듈 점등,
   ④⑤에서 랙 하나(18칸)가 `in_service`(밝은 초록)로 변함, ⑧에 API 기록

### 2. saga 보상 (장애 주입)
1. 장애 주입: 대상 호스트 기본값(`nh-su-2-rack-00-tray-00`) 그대로, `provision` 선택 → `⚡ 주입`
2. `▸ 개통 실행` — 이번 주문이 그 랙을 배치받으면 provision에서 실패
3. 관찰: ③에서 `compensating→failed` 빨간 칩, 피해 노드 1개만 `quarantined`(빨강),
   나머지는 전부 `pool_ready` 복귀, 테넌트 allocation 잔재 없음
4. 재실행 → 배치가 비정상 랙을 자동 스킵하고 `delivered`

### 3. 회수·Sanitization (F4)
1. `대상 allocation` 선택 → `▸ 회수 실행`
2. 관찰: drain→release→sanitize 후 풀 복귀. ⑥에서 해당 호스트 선택 후
   `5-1. 소거 보고서` → 7단계(erase/wipe/TPM/재attestation) PASS 확인
3. RMA 경로: 회수 전에 `sanitize` 장애 주입 → 해당 노드만 `rma`, 주문 error에 물리 폐기 명시

### 4. Reconcile (정합성 감사)
- `GHOST 생성` → NICo에만 있는 호스트 등록 → `Reconcile 실행` → info, discovered로 자동 등록
- `ORPHAN 생성` → NICo에서 호스트 삭제 → critical, 노드 cordoned(판매 풀 제외)
- `MISMATCH 생성` (in_service 노드 필요 — 개통 먼저) → NICo 상태를 몰래 변조
  → critical, **노드 상태는 자동 변경하지 않음** (감지·에스컬레이션 원칙)

### 5. 수동 모드 (Day 0/1/2 API 단계별)
1. `NICo job 지연`을 `2`로 설정(폴링 동작 확인용)
2. 호스트 입력(기본 `nh-su-1-rack-13-tray-17`) → `상태 조회`
3. `1. reserve` → `2. provision` (job running) → `2-1. job 폴링` ×2 (succeeded)
   → `3. allocate` → `4. release` → `5. sanitize` → `5-1. 소거 보고서`
4. 상태머신 칩이 `pool_ready→reserved→provisioning→provisioned→allocated→released→sanitizing→pool_ready`로 이동

> 주의: 수동 모드는 NICo만 조작하므로 M3 노드와 어긋난다 → `Reconcile 실행`으로
> MISMATCH가 잡히는 것까지가 시나리오. 끝나면 `⟲ 전체 리셋`.

## 트러블슈팅

| 증상 | 원인/조치 |
|---|---|
| 주문이 `rejected` (insufficient capacity) | 해당 세대 랙 부족 또는 비정상 노드 랙 제외됨 — 랙 수 축소 또는 리셋 |
| 주문이 `failed` (reserve failed) | NICo와 미러 불일치(고아 호스트 등) — 정상 saga. 재시도하면 우회 배치 |
| 수동 모드 409 | 호스트 상태 전제조건 위반(예: reserve 없이 provision) — 의도된 가드 |
| 페이지가 이상함 | 새로고침(⌘R) 후 `⟲ 전체 리셋` |
| 서버 종료/재시작 | `kill $(lsof -ti :8000)` / `cd ~/vrcm && ./run.sh` |
