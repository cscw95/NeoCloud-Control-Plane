// NeoCloud Control-Plane 모듈별 세부 역할 말풍선.
// /arch의 [data-b] 박스와 /flow 모듈 스트립의 [data-m]에 호버(클릭 고정)로 표시.
// URL 해시 #mod:<id> 로 특정 모듈 말풍선을 자동 고정한다(시연용).
const MODULE_GUIDE = {
  "p-op": ["① Operator Portal — 내부 운영",
    "Fulfillment 승인 게이트(단계별 approve/reject) · break-fix 큐·reconcile · 장비 헬스 취합/조치(faulted·maintenance) · 테넌트 Observability(NICo 직접 연동) · 티켓 처리 · Shared Services(IAM·PAM·감사) 운영."],
  "p-cust": ["② Customer Portal · Public APIs — 테넌트 셀프서비스",
    "클러스터 LIVE 상태·SLA 가시성 · 셀프서비스 주문(개통·확장·회수, 스토리지 자동/수동) · 딜리버리 접속·인증 패키지(1회 노출 secret) · CPU 노드·스토리지 현황 · 티켓 접수."],
  "p-biz": ["③ Business Portal — 고객·계약",
    "계약(테넌트) 생성 + 개통 요청(운영 승인 워크플로) · 전체 GPU 사용 현황·테넌트 드릴다운 · 사용량·과금 프리뷰(rack-hour 단가·월 환산) · 서비스 요청 현황."],
  "cp": ["NeoCloud OS Control Plane — SKT Core Engine",
    "포털/외부 요청을 받아 NVIDIA·파트너 인프라 도메인(④~⑦)을 오케스트레이션하는 SKT 자체 계층. 멀티테넌시·과금·격리 정책·승인 워크플로 등 상품화 기능을 보유하고, 하부 플랫폼의 네이티브 관리 기능은 중복 구현하지 않는다."],
  "cp-intake": ["Service-Order Intake — 주문 접수",
    "주문 접수·정규화(OrderCreate 검증) · ServiceOrder 생성과 상태머신 시작(received→validated) · 승인 모드(approval_mode) 라우팅 — 거절 시 rejected 종결."],
  "cp-fulfill": ["Tenant Fulfillment — M1 saga 오케스트레이터",
    "7단계 파이프라인(validated→reserved→provisioning→isolating→storage_binding→acceptance→delivered) 실행 · 실패 시 saga 보상(compensating)으로 원복 · 승인 게이트(pending_stage) · 단계별 하부 API 버킷팅(/orders/{id}/flow)."],
  "cp-provision": ["BM Provisioning — 베어메탈 프로비저닝",
    "D1 ComputeAdapter 경유 노드 프로비저닝 — reserve(gRPC)→provision: BMC(Redfish) 전원·부트오더 → DPU-DHCP IP 할당 → PXE 테넌트 이미지 → cloud-init. job 폴링 수렴, 실패 노드는 quarantine 격리."],
  "cp-delivery": ["Delivery · Expansion — 인도·확장",
    "인수 검증 통과 후 in_service 전환·인도 완료 · 접속·보안 인증 패키지 발급(SSH bastion·OIDC secret 1회 노출·스토리지 마운트·P_Key) · 확장 주문 시 기존 P_Key/VRF 재사용."],
  "cp-reclaim": ["Reclamation · Sanitization — 회수·소거",
    "회수 saga — drain→release→7단계 소거(NVMe crypto-erase·GPU/메모리 wipe·TPM reset·re-attest)→풀 복귀(실패 시 RMA) · 자격증명 폐기(IAM SA revoke·Vault purge) · NVLink/P_Key/VPC/스토리지 역순 해체."],
  "cp-model": ["Resource & Service Model — 단일 리소스 모델",
    "Factory▸Block(층)▸DU▸SU▸Rack▸Tray▸GPU 토폴로지 SoT · NodeInstance 상태머신 미러(M3) · reconcile로 외부 SoT(NICo)와 정합성 감사(GHOST/ORPHAN/STATE_MISMATCH) · 장비 헬스 집계."],
  "cp-policy": ["Policy Orchestration — 정책 결정 (M4 배치)",
    "M4 배치 = 주문한 랙을 \"어느 사이트·어느 SU·어느 랙에 둘지\" 정하는 규칙 엔진. ① 랙 단위 판매 — NVLink는 랙(NVL72) 안에서만 닫히므로 랙을 쪼개지 않는다 ② best-fit — 한 SU에 들어가면 남는 공간이 가장 작은 SU를 골라 파편화 최소화 ③ 넘치면 여러 SU로 분할(spill)하되 같은 사이트 안에서만 — 가산·안산 간 IB가 없어 클러스터가 사이트를 못 넘는다 ④ dedicated 테넌트가 점유한 SU에는 타 테넌트 배치 금지(양방향). 그 외 격리 티어→DPU 모드 도출, 4계층 격리 검증 리포트(acceptance 게이트)도 이 모듈 소관."],
  "cp-obs": ["Observability — 관측 취합",
    "시스템 세부 트레이스 버스(전 채널 메시지 페이로드) · NICo 벌크 헬스·DCGM 시계열(240틱) · XID/장애 이벤트 · 물리+논리 헬스 단일 SoT(CAP05) — 실배치는 OTel/NATS로 전환(M8)."],
  "cp-sla": ["SLA Management — SLA/SLO",
    "프로비저닝 리드타임 자동 산출(주문 이력) · 가용성(in_service 비율) · NVIDIA SLA 타깃(관리면 99.95% financially-backed, 스토리지 QoS) 측정 체계 — M6 로드맵."],
  "cp-api": ["Northbound / Public APIs — 공식 연동 표면",
    "/api/v1/* 98 엔드포인트(docs/API_REFERENCE.md) — 포털 3종·외부 시스템이 소비. 실배치에서 OIDC Bearer(SEC01)+RBAC(SEC04) 보호. 주문/토폴로지/테넌시/헬스/과금/트레이스."],
  "d-compute": ["④ Compute Services — NVIDIA DSX · NICo",
    "호스트 외부 SoT — Day0(디스커버리·TPM attestation)/Day1(프로비저닝)/Day2(회수·소거) · BMC(Redfish over TLS)·DPU-DHCP·PXE·cloud-init · 비동기 job 모델. 실연동: LocalNicoAdapter→NicoHttpAdapter 교체."],
  "d-sdn": ["⑤ SDN / Network Fabric — BlueField",
    "DPU isolation(실 NICo infra-controller 기준): 각 BlueField의 dpu-agent가 API(carbide)에서 desired config를 gRPC 폴링 → FNN(L3 EVPN) NVUE 템플릿 렌더(vrf vpc_<vni>·pf0hpf ACL 체인·NSG) → HBN 컨테이너 적용(nv config→ifreload) → BGP(ToR·routeserver) 수렴 검증. + UFM IB P_Key · NMX NVLink 파티션 — 3-plane 분리(SDN04)."],
  "d-storage": ["⑥ Storage Services — VAST VMS",
    "View/Quota/QoS 프로비저닝(주문당 자동 또는 수동 지정) · export를 테넌트 VRF 서브넷으로 제한 · 용량·성능·헬스 SLA 데이터 · 회수 시 스냅샷 파기·뷰 삭제."],
  "d-shared": ["⑦ Shared Services — Common Platform",
    "IAM: 테넌트 realm·롤 3종(RBAC)·포털 MFA·주문별 서비스 계정 · Vault: 자격증명 저장·회전(s3/redfish) · PAM: 권한상승 세션(TTL·녹화) · 감사 로그(SEC08) — 실배치: Keycloak·Vault·PAM 게이트웨이."],
};

// 주문 파이프라인 단계별 기능 설명 (말풍선) — flow 트랙·arch 단계·ops 셰브런 공용
const STAGE_GUIDE = {
  received: ["received — 주문 접수 (Service-Order Intake)",
    "포털/API의 OrderCreate를 ServiceOrder로 기록하고 상태머신을 시작. 승인 모드(approval_mode)면 운영 포털 승인 큐에서 단계별 게이트로 진행."],
  validated: ["validated — 정책 검증 + M4 배치",
    "테넌트/블루프린트/규격 검사 후 M4 배치 실행 — 단일 사이트 내 best-fit(랙 단위·NVL 무결성), dedicated SU 격리 반영. 용량 부족 시 rejected(사이트별 가용 내역 포함)."],
  reserved: ["reserved — 자원 예약",
    "선택된 랙의 전 노드를 NICo에 ReserveHost(gRPC)로 예약 — NodeInstance 미러가 reserved로 전이. 이후 실패 시 saga가 예약을 원복."],
  provisioning: ["provisioning — BM 프로비저닝",
    "노드별: DPU provisioning(BFB·rshim → DHCP/PXE 서비스 구성) → BMC(Redfish) PXE 원스부트+Reset → DPU-DHCP /30 임대 → iPXE/이미지 스트리밍 → cloud-init. job 폴링 수렴, 실패 노드는 quarantine 후 보상."],
  isolating: ["isolating — 격리 구성 (3평면)",
    "테넌트 VPC 생성(FNN L3 EVPN — vrf vpc_&lt;vni&gt;) 후 호스트별 HBN 적용 · UFM IB P_Key 파티션 · NMX NVLink 파티션 · CPU 노드 5대 VPC 연결."],
  storage_binding: ["storage_binding — 스토리지 바인딩",
    "VAST VMS view/quota/QoS 생성 — 자동(랙당 500TB·40GB/s) 또는 수동 지정. export는 테넌트 VRF 서브넷으로 제한."],
  acceptance: ["acceptance — 인수 검증",
    "4계층 격리 부정 테스트(교차 VRF·IB P_Key·NVLink) + 격리 리포트 PASS 필수. 통과 시 IAM 서비스 계정·Vault 자격증명(s3/redfish) 발급."],
  k8s_installing: ["k8s_installing — Managed K8s 설치 (옵션)",
    "BMaaS 인수 후 K8s 설치: CP CPU 노드 3대를 NICo(DPU isolation) 경유 Day1 프로비저닝 → Converged Network attach → NKD 부트스트랩(HA CP·kube-vip VIP) → GPU 워커 join → 관리형 애드온(CNI·GPU/Network Operator·DCGM exporter) → burn-in 검증(NCCL·dcgm diag) · DCGM 수집을 in-band로 전환."],
  delivered: ["delivered — 인도 완료",
    "노드 in_service 전환 · 접속·보안 인증 패키지 발급(SSH bastion·OIDC secret 1회 노출·스토리지 마운트·P_Key) · 과금 시작."],
  reclaiming: ["reclaiming — 회수·소거",
    "역순 해체: NVLink 파티션 → UFM(부분 회수면 포트만 언바인드·P_Key 유지) → VPC/HBN·DHCP 회수 → 스토리지 파기 → drain→release→7단계 sanitize → 풀 복귀(실패 시 RMA) · 자격증명 폐기."],
  closed: ["closed — 종결", "회수 완료 — 과금 라인 마감, 자원은 판매 가능 풀로 복귀."],
  compensating: ["compensating — 보상 (saga)",
    "실패 지점까지의 진행분을 역순으로 자동 원복 — 예약 해제·격리 해체. 피해 노드 1대만 quarantine(break-fix 트랙), 나머지는 풀 복귀."],
  failed: ["failed — 실패", "보상 완료 후 종결 상태 — 원인은 order.error에 기록. 재시도 주문은 비정상 랙을 자동 스킵."],
  rejected: ["rejected — 거절",
    "정책/용량 단계에서 거절 — 예: 단일 사이트 용량 부족(스팬 불가), dedicated SU 격리 잠김. 사유에 테넌트 기준 사이트별 가용 내역 포함."],
};

// /flow 모듈 스트립(data-m) → 사전 키 매핑
const FLOW_MOD_ALIAS = {
  portal: "cp-api", intake: "cp-intake", fulfill: "cp-fulfill",
  policy: "cp-policy", model: "cp-model", provision: "cp-provision",
  nico: "d-compute", sdn: "d-sdn", storage: "d-storage",
  shared: "d-shared", delivery: "cp-delivery",
};

(function () {
  const tip = document.createElement("div");
  tip.id = "modtip";
  (document.body ? Promise.resolve() : new Promise(r =>
    document.addEventListener("DOMContentLoaded", r))).then(() =>
    document.body.appendChild(tip));
  let pinned = null;

  function guideKey(el) {
    if (el.dataset.b) return el.dataset.b;
    if (el.dataset.m) return FLOW_MOD_ALIAS[el.dataset.m] || el.dataset.m;
    return null;
  }
  function entryOf(el) {
    if (el.dataset.stage) return [STAGE_GUIDE[el.dataset.stage], "주문 파이프라인 단계"];
    return [MODULE_GUIDE[guideKey(el)], "NeoCloud Control-Plane 모듈"];
  }
  function show(el) {
    if (document.documentElement.dataset.tips === "0") return;   // 말풍선 OFF
    const [entry, head] = entryOf(el);
    if (!entry) return;
    const [t, d] = entry;
    tip.innerHTML = `<div class="modtip-h">${head}</div>
      <div class="modtip-t">${t}</div><div class="modtip-d">${d}</div>`;
    tip.style.display = "block";
    const r = el.getBoundingClientRect();
    tip.style.maxWidth = Math.min(430, innerWidth - 24) + "px";
    const x = Math.min(Math.max(8, r.left), innerWidth - tip.offsetWidth - 8);
    let y = r.bottom + 9;
    tip.classList.remove("up");
    if (y + tip.offsetHeight > innerHeight - 8) {
      y = r.top - tip.offsetHeight - 9;
      tip.classList.add("up");
    }
    tip.style.left = x + "px";
    tip.style.top = Math.max(4, y) + "px";
    tip.style.setProperty("--ax", Math.max(12, Math.min(
      r.left + r.width / 2 - x - 5, tip.offsetWidth - 22)) + "px");
  }
  const SEL = "[data-b],[data-m],[data-stage]";
  document.addEventListener("mouseover", e => {
    const b = e.target.closest && e.target.closest(SEL);
    if (b && !pinned && entryOf(b)[0]) show(b);
  });
  document.addEventListener("mouseout", e => {
    if (e.target.closest && e.target.closest(SEL) && !pinned)
      tip.style.display = "none";
  });
  document.addEventListener("click", e => {
    const b = e.target.closest && e.target.closest(SEL);
    if (b && entryOf(b)[0]) {
      pinned = (pinned === b) ? null : b;
      pinned ? show(b) : tip.style.display = "none";
    } else if (pinned) { pinned = null; tip.style.display = "none"; }
  });
  addEventListener("scroll", () => {
    if (!pinned) tip.style.display = "none";
    else show(pinned);                       // 고정 상태면 위치 추적
  }, true);

  document.addEventListener("nc-tips-off", () => {  // 스위치 OFF — 즉시 닫기
    pinned = null; tip.style.display = "none";
  });

  addEventListener("load", () => {           // #mod:<id>·#stage:<state> — 시연용 자동 고정
    const m = location.hash.match(/^#(mod|stage):(.+)$/);
    if (!m) return;
    const id = decodeURIComponent(m[2]);
    setTimeout(() => {                       // 동적 렌더(fetch) 완료 후 탐색
      const el = m[1] === "stage"
        ? document.querySelector(`[data-stage="${id}"]`)
        : document.querySelector(`[data-b="${id}"]`) ||
          document.querySelector(`[data-m="${id}"]`);
      if (el) {
        pinned = el;
        el.scrollIntoView({ block: "center" });
        setTimeout(() => show(el), 250);
      }
    }, 600);
  });
})();
