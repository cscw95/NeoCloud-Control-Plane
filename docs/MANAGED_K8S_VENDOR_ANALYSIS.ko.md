# NeoCloud 업체 Managed Kubernetes 상품 조사·분석

> 작성일: 2026-07-14 · 조사 방법: 각 사 공식 문서/제품 페이지 웹 조사
> 목적: NOCP(NeoCloud OS Control-Plane) 목업 UI 및 fulfillment 파이프라인에 반영할 보완 항목 도출
>
> **우리 시스템 전제**: BMaaS 개통 후 K8s 설치, CPU 노드 3대 = K8s control plane, Converged Ethernet(RoCE) 네트워크, DCGM in-band agent 수집. 목업 UI 10메뉴 = Overview / 요청·작업 / 클러스터 / 네트워킹 / 접근관리 / 업그레이드 / 장애관리 / 스토리지 / 모니터링 / 설정.

---

## 1. 업체별 요약

### 1.1 CoreWeave — CKS (CoreWeave Kubernetes Service)

- **Bare-metal K8s**: 하이퍼바이저 없이 GPU 노드에 K8s를 직접 구동. VPC 오케스트레이션·네트워크 처리는 BlueField-3 **DPU로 오프로드**. CNI는 기본 **Cilium**(최소 기능 세트의 관리형 구성).
- **Control plane**: CoreWeave가 완전 관리(고객 노드 밖 out-of-band에서 관리 컴포넌트 실행). 고객에게 **control plane audit log/로그 접근** 제공.
- **노드 라이프사이클**: Fleet/Node Lifecycle Controller + "Mission Control"(자사 내부 시스템)로 프로비저닝~RMA까지 관리. **유휴 노드에서 매시간 HPC Verification(약 20분, 전 GPU 사용, 낮은 우선순위·선점 가능)** 능동 헬스체크, 워크로드 중에는 in-band + out-of-band 텔레메트리 수동 감시. 장애 시 자동 remediation→실패하면 프로덕션 제외 후 **대체 노드 자동 투입**, 복귀 노드는 최대 48시간 onboarding 테스트 재통과 필요. InfiniBand 패브릭도 1일 수회 자동 검증 + 주간 수동 점검.
- **버전 정책**: minor 버전 **순차 업그레이드만 허용**(1.32→1.33), 다운그레이드 불가, `isUpgradable` 필드로 가능 여부 노출.
- **접근·보안**: CKS Managed Auth — 콘솔에서 **API Access Token을 내장한 kubeconfig 발급**, 토큰 권한 = RBAC 매핑, 외부 IdP 연동(OIDC Workload Identity Federation, 2025-11 출시: 저장 credential 제거·자동 토큰 로테이션).
- **GPU 스택**: GPU Operator·DCGM은 CoreWeave가 사전 설치·관리(고객이 GPU Operator를 직접 설치하면 안 됨). DCGM 칩 레벨 메트릭을 메트릭 쿼리 서비스/대시보드로 노출.
- **관측성**: **관리형 Grafana**(노드 로그·GPU 사용률·Kueue 메트릭 등 큐레이션된 대시보드) + 고객 자체 exporter 병행 허용.
- **스케줄링**: **SUNK(Slurm on Kubernetes)** — slurmd를 파드로 실행, Slurm↔K8s 상태 양방향 동기화, 동일 클러스터에서 학습(Slurm)과 추론(K8s) 공존. Kueue 대시보드 제공.
- 출처: [CKS 소개](https://docs.coreweave.com/docs/products/cks) · [Node Lifecycle Day2+](https://docs.coreweave.com/docs/platform/fleet-management/node-lifecycle/day2) · [SUNK](https://docs.coreweave.com/docs/products/sunk) · [Managed Grafana](https://docs.coreweave.com/docs/observability/managed-grafana) · [API Access Token/Kubeconfig](https://docs.coreweave.com/docs/products/cks/auth-access/manage-api-access-tokens) · [Upgrade Kubernetes](https://docs.coreweave.com/docs/products/cks/clusters/upgrade-kubernetes) · [제품 페이지](https://www.coreweave.com/products/coreweave-kubernetes-service)

### 1.2 Nebius — Managed Service for Kubernetes

- **Control plane**: 완전 관리·**무료**. HA 기본 활성(etcd 3중화, HA 여부가 요금에 영향 없음). 퍼블릭 엔드포인트 기본 + IP allowlist 또는 프라이빗 전용 구성 가능.
- **SLA**: **99.90% 가용성**(control plane 접근 5분 이상 불가 시 다운타임 산정). 미달 시 10%/25% 크레딧. **단, HA(etcd 3~5) 구성에만 SLA 적용** — 비HA는 SLA 제외.
- **노드 관리(핵심 차별점)**: 노드그룹 단위 **auto-repair를 노드 컨디션 기반으로 선언적 설정** — 컨디션(Ready, MemoryPressure 등)·상태(TRUE/FALSE/UNKNOWN)·**타임아웃(예: 2h45m) 조합으로 자동 복구 트리거**. 업데이트 전략도 **drain timeout / max surge / max unavailable** 파라미터로 제어(2026 Q3부터 drain timeout 기본 10m).
- **GPU**: GPU 노드그룹 프리셋(H100/H200/L40S 등), **NVIDIA 드라이버·InfiniBand 드라이버 사전 설치**, `drivers_preset`(cuda12/cuda12.8)으로 CUDA 버전 선택, 기존 Compute **GPUCluster(InfiniBand 패브릭)에 노드그룹 연결**하는 모델. NVLink 인스턴스 그룹 연결 필드도 존재.
- **버전**: 기본/권장 1.33, 노드그룹 버전은 `<major>.<minor>`로 지정(기본은 control plane 버전 추종).
- **스케줄링**: **Managed Soperator**(Slurm-on-K8s) 별도 상품 + K8s-native 잡 스케줄러 이미지 카탈로그 제공.
- 출처: [제품 페이지](https://nebius.com/services/managed-kubernetes) · [SLA](https://docs.nebius.com/legal/sla-levels/managed-kubernetes) · [Terraform node_group 레퍼런스(auto-repair)](https://docs.nebius.com/terraform-provider/reference/resources/mk8s_v1_node_group) · [클러스터 관리](https://docs.nebius.com/kubernetes/clusters/manage) · [Managed Soperator](https://nebius.com/services/soperator)

### 1.3 Crusoe — CMK (Crusoe Managed Kubernetes)

- **Control plane**: 완전 관리, **최소 3노드를 서로 다른 물리 호스트에 분산**(HA). 클러스터당 $0.10/h. UI·CLI·API·Terraform 모두 지원.
- **노드 풀**: 동일 인스턴스 타입의 인스턴스를 노드풀로 그룹화. 노드풀 템플릿 변경은 **in-place 업그레이드가 아니라 롤링 교체**(노드 VM 삭제 → 템플릿 반영된 새 VM으로 self-heal). `crusoe kubernetes versions list`로 지원 버전 조회.
- **애드온 카탈로그**: 클러스터 생성 시 **NVIDIA GPU Operator, NVIDIA Network Operator, Crusoe CSI, cluster_autoscaler를 선택형 애드온**으로 설치.
- **장애 대응**: **AutoClusters** — 일반적 하드웨어 장애를 자동 감지·해결해 수동 개입 최소화(옵트인).
- **네트워킹**: GPU 노드에 RDMA/InfiniBand 표준 제공(Quantum-2), NVIDIA·AMD(MI300X/MI355X) 모두 지원. **NCCL 테스트 실행 가이드/절차를 고객에게 공식 제공**(수용성 검증 셀프서비스).
- **스케줄링**: **Crusoe Managed Slurm on CMK**(Slurm을 CMK 위에서 관리형으로 제공), Run:ai 배포 가이드 제공.
- **관측성**: Command Center(GPU 관측성+오케스트레이션) 상품 별도 운영.
- 출처: [CMK Overview](https://docs.crusoecloud.com/orchestration/cmk/overview/) · [Node Pool 관리](https://docs.crusoecloud.com/orchestration/cmk/managing-nodepools) · [Slurm on CMK 블로그](https://www.crusoe.ai/resources/blog/slurm-on-crusoe-managed-kubernetes-how-we-built-managed-gpu-training-infrastructure) · [NCCL 테스트 가이드](https://support.crusoecloud.com/hc/en-us/articles/36499606523291-Run-NCCL-Tests-On-Crusoe-Managed-Kubernetes-CMK-Cluster) · [Command Center](https://www.crusoe.ai/cloud/management-observability)

### 1.4 Lambda — Managed Kubernetes (MK8s on 1-Click Clusters)

- **포지셔닝**: 1-Click Cluster(16~2,000+ GPU, 고객 단독 점유) 위에 MK8s를 얹는 모델. Lambda가 **K8s 설치·업그레이드, control plane HA·유지보수, 노드 장애 감지·수리, 메트릭 수집을 24/7 대행**.
- **배포판**: RKE2 기반(예: `v1.32.3+rke2r1`).
- **GPU/네트워크**: GPU Operator + Network Operator 사전 구성. RDMA는 파드에서 `rdma/rdma_shared_device_a` 리소스 요청 + `IPC_LOCK` capability로 사용(InfiniBand, SHARP 지원).
- **자동 복구**: 하드웨어 이슈 감지·복구 **auto-remediation 시스템** + GPU·네트워크·스토리지 기능의 **지속 검증(continuous validation)**.
- **접근**: `kubelogin`(OIDC) 브라우저 로그인 — Lambda Cloud 계정으로 인증하고 **계정 역할→ClusterRole 자동 매핑**(Member=edit, Admin=cluster-admin), 비대화형은 ServiceAccount kubeconfig.
- **관측성**: 클러스터별 Grafana 엔드포인트(`grafana.<zone>.k8s.lambda.ai`)에 **NVIDIA DCGM Exporter Dashboard** 기본 제공.
- **스토리지**: StorageClass 6종 — `lambda-shared`/`lambda-nfs`(공유), `lambda-local`(로컬 NVMe), 각각 `-retain` 변형.
- 출처: [MK8s Docs](https://docs.lambda.ai/managed-kubernetes/) · [제품 페이지](https://lambda.ai/kubernetes) · [1-Click Clusters](https://lambda.ai/1-click-clusters)

### 1.5 Together AI — Instant Clusters (K8s flavor)

- **셀프서비스 개통**: 8 GPU(1노드)~수백 GPU 클러스터를 **분 단위로 셀프서비스 생성**. 클러스터 flavor로 **Kubernetes 또는 Slurm 선택**, 필요 시 SSH.
- **자동화 계약면**: 생성·스케일·삭제·스토리지 등 모든 관리 동작을 **REST API + Terraform**으로 제공 — "클러스터 as API"가 핵심 차별점.
- **사전 구성**: GPU Operator, Network Operator, InfiniBand, 버전 고정(driver/CUDA pinning)으로 재현성 보장. DC-local 고대역 공유 스토리지(내구성·리사이즈 가능, 온디맨드 과금).
- 출처: [Instant Clusters Docs](https://docs.together.ai/docs/instant-clusters) · [GA 블로그](https://www.together.ai/blog/together-instant-clusters-ga)

### 1.6 Voltage Park — Managed Kubernetes

- Bare-metal GPU 클러스터 위 **관리형 control plane**(설치·보안 패치·모니터링 오프로드). 2025-06 출시.
- 사전 구성: **NVIDIA GPU Operator + Prometheus/Grafana + SentinelOne**(보안 관측·위협 탐지 — 보안 스택을 기본 포함하는 점이 특징).
- 현재 bare-metal 대상, VM 환경 지원은 로드맵.
- 출처: [제품 페이지](https://www.voltagepark.com/managed-kubernetes) · [출시 보도자료](https://www.businesswire.com/news/home/20250604080012/en/Voltage-Park-Addresses-Kubernetes-Complexity-for-AI-Developers-with-New-Managed-Offering)

### 1.7 (참고) NVIDIA — DGX Cloud / Mission Control(BCM) / K8s 스택

- **Mission Control의 K8s 배포**: BCM 11의 `cm-kubernetes-setup`으로 설치. **k8s-admin(관리용) + 워크로드용 2개 클러스터** 구성, control plane **최소 3노드 = etcd 3노드 겸용**, K8s v1.32, CNI **Calico**, Prometheus Operator 스택/Loki/Promtail/ingress-nginx 사전 설치. NMC 2.3부터는 "향후 자동화 도구" 대비 클러스터 명명 규칙 고정 — **"NKD"라는 이름의 공개 문서는 아직 없음**(BCM 경로가 공식 문서화된 경로). 2.3에서 가상화 control plane 옵션·통합 인증 추가.
- **DGX Cloud Create(Run:ai on DGX Cloud)**: CSP 위 NVIDIA 최적화 K8s 클러스터 + **Run:ai SaaS control plane**. 클러스터 내 **NVIDIA 네임스페이스(NVIDIA 운영) / 고객 네임스페이스(고객 운영)로 책임 분리**, 고객별 Realm(테넌트) 배정. GPU 클러스터 자체는 NVIDIA가 프로비저닝·운영.
- **GPU Operator**: 드라이버, device plugin, Container Toolkit, GFD(자동 노드 레이블), **DCGM exporter(in-band 메트릭)**, MIG manager를 오퍼레이터로 일괄 관리 — 사실상 모든 조사 대상 업체가 이를 기본 채택.
- **Network Operator**: RDMA shared device plugin, SR-IOV, GPUDirect RDMA 등 K8s 네트워킹을 CRD로 관리 — RoCE/IB 공통 적용 가능.
- 출처: [NMC Kubernetes Installation](https://docs.nvidia.com/mission-control/docs/nmc-software-installation-guide/2.0.0/nmc-kubernetes.html) · [NMC 2.3 Release Notes](https://docs.nvidia.com/mission-control/docs/systems-quick-start-guide/2.3.0/nmc-release-notes.html) · [Run:ai on DGX Cloud Overview](https://docs.nvidia.com/dgx-cloud/run-ai/latest/overview.html) · [GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/index.html) · [Network Operator](https://docs.nvidia.com/networking/display/kubernetes2570/deployment-guide-kubernetes.html)

---

## 2. 기능 비교 매트릭스

| 항목 | CoreWeave CKS | Nebius MK8s | Crusoe CMK | Lambda MK8s | Together Instant | Voltage Park | NVIDIA NMC/DGX Cloud (참고) |
|---|---|---|---|---|---|---|---|
| 노드 형태 | **Bare-metal**(DPU 격리) | VM(GPUCluster 연결) | VM 노드풀 | Bare-metal 1CC 전용 | BM/VM 혼재 | Bare-metal | 온프레미스 BM |
| Control plane HA | 관리형(상세 비공개), out-of-band | **etcd 3중화 기본, 무료** | **3노드/물리 분산**, $0.10/h | 관리형 HA 포함 | 관리형 | 관리형 | **CPU 3노드=etcd 겸용** |
| Control plane SLA | 별도 계약 | **99.90% + 크레딧(HA만 적용)** | 미공시 | 미공시 | 미공시 | 미공시 | (해당 없음) |
| 버전 정책 | **순차 minor 업그레이드, `isUpgradable` 노출** | 기본 1.33, 노드그룹은 CP 버전 추종 | 버전 목록 API, 노드풀 롤링 교체 | RKE2 고정 배포판, Lambda가 대행 | 버전 pinning(재현성) | 대행 | v1.32 고정 |
| Auto-repair | **유휴노드 능동 HPC 검증 + 자동 교체 + 복귀 48h 재검증** | **컨디션+타임아웃 선언적 auto-repair, drain/surge 파라미터** | AutoClusters(옵트인) | auto-remediation + 지속 검증 | 미공시 | 헬스 모니터링 | NMC autonomous resiliency |
| CNI | **Cilium(eBPF)** | 미공시 | 미공시 | RKE2 기본(Canal/Cilium) | 미공시 | 미공시 | **Calico** |
| RDMA | IB fat-tree + SHARP, DPU | **IB GPUCluster 연결 모델** | IB 표준 + **NCCL 테스트 가이드** | IB, `rdma/rdma_shared_device_a` | IB 사전 구성 | IB | Network Operator(SR-IOV/RDMA) |
| GPU 스택 | GPU Operator/DCGM **운영자 관리(고객 설치 금지)** | 드라이버 사전 설치, `drivers_preset` | GPU/Network Operator **선택형 애드온** | GPU/Network Operator 사전 구성 | GPU/Network Operator 사전 구성 | GPU Operator 사전 구성 | GPU Operator |
| 관측성 | **관리형 Grafana + DCGM + control plane 로그/audit** | 시스템 모니터링 | Command Center | **클러스터별 Grafana + DCGM 대시보드** | 미공시 | Prometheus/Grafana + SentinelOne | Grafana 대시보드(NMC) |
| kubeconfig/인증 | **토큰 내장 kubeconfig + OIDC 페더레이션** | 표준 kubectl 연결 | CLI/API 발급 | **kubelogin(OIDC) + 역할→ClusterRole 매핑** | REST API | 미공시 | NMC 2.3 통합 인증 |
| 스토리지 | 관리형 CSI | CSI | Crusoe CSI 애드온 | **StorageClass 6종(local NVMe/shared/retain)** | DC-local 공유 스토리지 | 미공시 | (별도) |
| Slurm 연동 | **SUNK** | **Managed Soperator** | **Managed Slurm on CMK** | (1CC 별도 Slurm) | Slurm flavor | 없음 | Run:ai + Slurm |
| API/IaC | CKS API | **Terraform 공식 provider** | UI/CLI/API/TF | Docs 중심 | **REST API + TF** | 미공시 | BCM CLI/API |

**시사점 요약**: (1) GPU 클라우드 Managed K8s의 사실상 표준 구성은 "관리형 HA control plane + GPU Operator/Network Operator 사전 설치 + DCGM 대시보드 + Slurm-on-K8s 옵션". (2) 차별화 축은 **노드 라이프사이클 자동화의 깊이**(CoreWeave가 최고 수준)와 **auto-repair/업그레이드의 선언적 파라미터화**(Nebius), **셀프서비스 API화**(Together). (3) 접근관리는 "정적 kubeconfig 다운로드"에서 "**OIDC/단기 토큰 + 콘솔 역할→RBAC 자동 매핑**"으로 이동 중.

---

## 3. 보완 권고 Top 8

표기: **[UI]** = 목업 UI 10메뉴에 추가, **[BE]** = 컨트롤플레인 백엔드(fulfillment 파이프라인)에 추가.

### 1. 노드 라이프사이클 상태머신 + 유휴 노드 능동 검증(Active Health Check) — [BE] 중심, [UI] 장애관리
- **왜**: CoreWeave의 핵심 차별화. 유휴 노드에서 매시간 HPC Verification(선점 가능·낮은 우선순위)을 돌리고, 장애 노드는 자동 교체 후 복귀 시 최대 48h 재검증을 요구한다. Lambda도 "continuous validation"을 명시. 단순 "장애 발생 → 티켓"이 아니라 **장애를 선제 발견하는 파이프라인**이 상품 신뢰도의 근간.
- **무엇을**:
  - [BE] NodeInstance 상태머신 확장: `provisioning → verifying(burn-in) → in_service → suspect → triage → repairing/RMA → re_verifying(48h) → in_service`. 기존 M1/M3 saga의 acceptance 단계에 **burn-in 검증 잡**(DCGM diag, NCCL loopback)을 삽입하고, in_service 중 유휴 슬롯에 선점 가능한 검증 잡을 스케줄.
  - [UI] 장애관리 메뉴에 노드별 라이프사이클 타임라인(상태 전이 이력 + 검증 결과 + RMA 티켓 링크) 뷰 추가.

### 2. 선언적 auto-repair·drain 정책 파라미터 — [BE] + [UI] 클러스터
- **왜**: Nebius는 노드 컨디션(Ready 등)×상태×타임아웃(예: `2h45m`)의 선언적 auto-repair와 **drain timeout / max surge / max unavailable**을 노드그룹 스펙으로 노출한다. Crusoe AutoClusters도 옵트인 방식. 고객마다 "자동 교체 허용 범위"가 달라 **정책을 파라미터로 노출**하는 것이 관리형 상품의 표준이 되고 있다.
- **무엇을**:
  - [BE] 노드풀 스펙에 `auto_repair {condition, status, timeout, enabled}` 및 `update_strategy {drain_timeout, max_surge, max_unavailable}` 필드 추가. 자동 드레인→BMaaS 노드 교체→풀 복귀를 잇는 reconcile 루프(기존 NICo reconcile 패턴 재사용).
  - [UI] 클러스터 메뉴의 노드풀 상세에 auto-repair 토글/타임아웃, 롤링 정책 슬라이더 + "최근 자동 복구 이력" 카드.

### 3. Control plane SLA 명세·상태 노출 — [UI] Overview + [BE]
- **왜**: Nebius는 99.90% SLA를 문서화하고 **HA(etcd 3~5) 구성에만 SLA를 적용**하며, control plane 접근 5분 불가를 다운타임으로 정의한다. Crusoe는 "3노드 물리 분산"을 명시. 우리는 이미 CPU 노드 3대를 control plane으로 쓰므로 **SLA를 상품 스펙으로 승격**할 근거가 충분하다.
- **무엇을**:
  - [BE] control plane 컴포넌트(API server/etcd) 헬스 프로브 + 가용성 집계(월별 uptime %) API. etcd 3중화 상태(quorum, 물리 분산 여부)를 노출.
  - [UI] Overview에 "Control Plane 상태/이번 달 가용성/SLA 99.9%" 위젯, 설정 메뉴에 SLA 등급·크레딧 정책 표기.

### 4. OIDC/단기 토큰 kubeconfig 발급 + 역할→RBAC 자동 매핑 + audit log — [UI] 접근관리 + [BE]
- **왜**: CoreWeave는 토큰 내장 kubeconfig 콘솔 발급→OIDC Workload Identity Federation(정적 credential 제거·자동 로테이션)으로 진화했고, Lambda는 kubelogin 브라우저 로그인에 **포털 역할(Member=edit, Admin=cluster-admin)을 ClusterRole로 자동 매핑**한다. CoreWeave는 control plane audit log도 고객에 개방.
- **무엇을**:
  - [BE] 테넌트 IAM과 연동된 kubeconfig 발급 API(단기 토큰, TTL·스코프 지정), 테넌트 역할→K8s RBAC 매핑 테이블, API server audit log 수집·테넌트별 필터링 엔드포인트.
  - [UI] 접근관리 메뉴에 "kubeconfig 발급(TTL 선택)", 사용자별 역할·ClusterRole 매핑 표, audit log 조회 탭.

### 5. 관리형 애드온 카탈로그(GPU Operator / Network Operator / CSI / autoscaler / Slurm) — [UI] 클러스터·업그레이드 + [BE]
- **왜**: Crusoe는 클러스터 생성 시 GPU Operator·Network Operator·CSI·cluster_autoscaler를 **선택형 애드온**으로 제공하고, Together는 driver/CUDA 버전 pinning으로 재현성을 보장한다. CoreWeave는 반대로 GPU Operator를 운영자 전유물로 잠근다(고객 설치 금지). 어느 쪽이든 "**애드온을 누가·어떤 버전으로 관리하는가**"가 상품 정의의 일부다.
- **무엇을**:
  - [BE] fulfillment 파이프라인의 K8s 설치 단계 뒤에 **애드온 설치 스테이지** 추가: `gpu-operator(버전 pin, DCGM exporter 포함) → network-operator(RoCE/SR-IOV 프로파일) → CSI → (선택) autoscaler/Slurm operator`. 애드온별 버전·상태를 ServiceOrder 산출물로 기록.
  - [UI] 클러스터 메뉴에 애드온 카드(설치됨/버전/관리 주체=운영자), 업그레이드 메뉴에 "K8s 버전"과 별도로 "애드온 버전 채널" 섹션.

### 6. K8s 버전 수명주기·순차 업그레이드 UX — [UI] 업그레이드 + [BE]
- **왜**: CoreWeave는 "minor 1단계씩 순차 업그레이드, 다운그레이드 불가, `isUpgradable` 필드 노출"을 명문화했고, Crusoe는 노드풀을 in-place가 아닌 **롤링 교체(노드 재생성)**로 업그레이드한다. Nebius는 노드그룹 버전이 control plane 버전을 추종. 업그레이드 정책의 명문화는 고객 신뢰 요소.
- **무엇을**:
  - [BE] 클러스터 리소스에 `version / is_upgradable / supported_versions[]` 필드, control plane 선행→노드풀 롤링(surge 기반, #2의 파라미터 사용) 2단계 업그레이드 오케스트레이션(saga로 실패 시 보상).
  - [UI] 업그레이드 메뉴에 버전 타임라인(현재/지원 종료 예정/권장), "control plane 먼저 → 노드풀" 2단계 진행률, 순차 업그레이드 제약 안내 배너.

### 7. RDMA(Converged Ethernet) 검증 셀프서비스 + 네트워크 리소스 가시화 — [UI] 네트워킹 + [BE]
- **왜**: Crusoe는 NCCL 테스트 실행 절차를 공식 가이드로 제공하고 CoreWeave는 패브릭을 1일 수회 자동 검증한다. Lambda는 파드에서 쓸 RDMA 리소스명(`rdma/rdma_shared_device_a`)까지 문서화. 우리는 IB가 아닌 **Converged Ethernet(RoCE)**이므로 "RoCE에서도 검증된 NCCL 성능"을 증명하는 것이 오히려 더 중요한 세일즈 포인트다.
- **무엇을**:
  - [BE] Network Operator 기반 RoCE 프로파일(SR-IOV/호스트 네트워크, rdma shared device) 설치를 애드온 스테이지(#5)에 포함 + **고객 트리거형 NCCL all-reduce 검증 잡 API**(결과: busbw, 예상 대비 %). 개통 acceptance에 NCCL 기준치 통과를 포함.
  - [UI] 네트워킹 메뉴에 노드별 RDMA 리소스 현황(할당 가능/사용 중), rail/leaf 토폴로지 뷰, "NCCL 테스트 실행" 버튼과 이력 그래프.

### 8. 고객향 관측성 패키지: 관리형 Grafana + DCGM 대시보드 + control plane 로그 — [UI] 모니터링 + [BE]
- **왜**: CoreWeave(관리형 Grafana + DCGM 칩 레벨 메트릭 + Kueue 대시보드 + control plane 로그), Lambda(클러스터별 Grafana에 DCGM Exporter Dashboard 기본 탑재), Voltage Park(Prometheus/Grafana 기본)까지 — **DCGM 기반 GPU 대시보드의 고객 노출**은 이미 업계 최저선이다. 우리는 DCGM in-band agent 수집을 이미 하므로 "고객 노출 계층"만 얹으면 된다.
- **무엇을**:
  - [BE] 수집 중인 DCGM 메트릭을 테넌트 스코프로 분리하는 메트릭 쿼리 API(테넌트별 label 필터), K8s 이벤트·control plane 로그의 테넌트향 스트림. (로드맵의 NVSentinel 연동과 통합 설계)
  - [UI] 모니터링 메뉴에 GPU 사용률/온도/Xid 에러/ECC 카운트 표준 대시보드(임베드), "내 Prometheus로 remote_write" 연동 설정, 장애관리 메뉴와 크로스링크(메트릭 이상→노드 라이프사이클 이력).

### 우선순위 제언

| 순위 | 항목 | 이유 |
|---|---|---|
| 1 | #5 애드온 카탈로그 | fulfillment 파이프라인의 뼈대 — 다른 항목(#7, #8)의 전제 |
| 2 | #1 노드 라이프사이클 | GPUaaS 신뢰도의 본질, 기존 NodeInstance 상태머신 확장으로 구현 비용 낮음 |
| 3 | #8 관측성 패키지 | DCGM 수집이 이미 있어 ROI 최고, 데모 임팩트 큼 |
| 4 | #4 접근관리 | 보안 요구가 있는 국내 엔터프라이즈/공공 대상 필수 |
| 5~8 | #2, #6, #3, #7 | 상품 성숙도 단계에 맞춰 순차 반영 |
