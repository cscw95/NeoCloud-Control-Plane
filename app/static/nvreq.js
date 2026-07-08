// NVIDIA BMaaS Requirements Guide v2.2 (2026-04-17) — Req ID별 요구사항 말풍선.
// .nvreq 배지 호버 시 표시, 클릭하면 고정(다시 클릭/바깥 클릭으로 해제).
// URL 해시 #nvreq 로 첫 배지에 말풍선을 자동 고정한다(시연용).
const NVREQ_GUIDE = {
  CNP01: ["API / CLI Access",
    "프로비저닝 시스템에 대한 API/CLI 접근 — 노드 라이프사이클(생성·수정·삭제·리스트·전원 상태) 관리와 인벤토리 조회. 베어메탈 전 라이프사이클 연산 지원 필수."],
  CNP03: ["NVLink-Aware Allocation",
    "NVL72급 NVLink 패브릭 구성에서 스위칭 도메인 내 연속 배치를 보장하는 NVLink 도메인 인지 노드 할당을 API가 지원해야 한다."],
  CNP04: ["Resource States",
    "명확한 리소스 상태 모델 노출 필수 — provisioning, running, degraded, maintenance_required 등 노드·NIC 상태."],
  CNP06: ["Console Access",
    "노드당 시리얼 콘솔 접근(읽기 전용 최소, 인터랙티브 권장). 콘솔 출력은 로깅되어 과거 조회가 가능해야 한다."],
  CNP07: ["Node Group Management",
    "논리 노드 그룹(토폴로지·네트워크·격리 경계를 공유하는 컴퓨트 인스턴스 집합)에 대한 CRUD 연산 지원."],
  CNP09: ["Firmware Attestation",
    "테넌트 전환 간 모든 펌웨어를 known-good 상태로 복원. 모든 펌웨어는 암호학적으로 서명되고 부팅 시 attestation 되어야 한다."],
  CNP10: ["Remote Management",
    "BMC 등 플랫폼 관리 컨트롤러는 Redfish over TLS를 지원해야 하며, IPMI는 비활성화(금지)."],
  BOOT01: ["Image Deployment & Updates",
    "API 기반 워크플로우로 벤더 제공/커스텀 디스크 이미지를 베어메탈 프로비저닝을 통해 배포·갱신·관리."],
  BOOT02: ["Instance Metadata",
    "cloud-init 및 link-local 주소(169.254.x.x) 기반 인스턴스 메타데이터 디스커버리 지원."],
  SDN01: ["Virtual Networking",
    "SDN 사설 네트워크의 전체 API/CLI 라이프사이클(CRUD·List) 관리. 비충돌 BYOIP 지원 포함."],
  SDN04: ["Tenant Isolation (3-plane)",
    "OOB 관리(BMC)·사용자 트래픽·스토리지 3개 플레인의 하드 논리/물리 네트워크 분리 — 3-plane은 스위치를 공유해선 안 된다."],
  SEC01: ["Authentication — Users",
    "모든 플랫폼·테넌트 대상 서비스에서 OIDC 표준 사용자 인증 지원. OIDC 발급 토큰의 서명·발급자(issuer)·대상(audience) 검증 필수."],
  SEC04: ["Authorization (RBAC)",
    "전 관리 서비스·인프라에 최소권한 RBAC 강제 — 세분화된 API 액션(CRUD)과 스코프(dev/staging 등) 단위 권한."],
  SEC07: ["Admin Interface MFA",
    "모든 관리자 인터페이스(UI·CLI·API)는 다중 인증(MFA)으로 보호되어야 한다."],
  SEC08: ["Audit Logs",
    "보안 관련 전 이벤트의 감사 로그 생성·보존 — 관리/컨트롤 플레인 API 호출, 인증 이벤트, 인가 결정 포함."],
  SEC21: ["Data Sanitization",
    "테넌트 전환·하드웨어 교체 시 데이터 소거 필수 — 전 데이터 드라이브 cryptographic erase, 영속 메모리 소거 포함."],
  SEC22: ["HW Root of Trust + Secure Boot",
    "TPM 2.0 하드웨어 신뢰 루트 필수. 전 플랫폼에서 TPM 2.0 기반 UEFI OS Secure Boot 활성화."],
  BFX01: ["Breakfix Lifecycle",
    "Breakfix API 필수 액션 — 노드 파워사이클, GPU 리셋, 노드/랙 정비 반환, cordon(기존 워크로드 유지·신규 차단), 헬스 임계 초과 시 호스트 교체 요청."],
  BFX02: ["Breakfix Events",
    "노드/랙의 예정·진행 정비 이벤트, 폐기(retirement) 예고, 수리 이력·현황 조회 API. 이벤트에는 티켓 일자·HW 식별자·부품 유형·장애 설명·조치가 포함되어야 한다."],
  BFX03: ["Diagnostics",
    "설치된 HW 부품(섀시·베이스보드·NIC·CPU·GPU)의 시리얼 번호 노출(난독화된 안정 식별자 허용). 컴퓨트 노드·NV 스위치 트레이 펌웨어 버전 조회 가능."],
  HSS01: ["Storage Provisioning APIs",
    "스토리지 프로비저닝은 포털 또는 API로 제공 — 티켓 기반 절차 없이 전 연산 자동화 가능해야 한다."],
  HSS02: ["Storage Performance (QoS)",
    "요청된 대역폭·IOPS를 프로비저닝하고, 커밋된 성능 수준을 프로덕션 워크로드에서도 일관되게 충족."],
  HSS04: ["Quota Support",
    "볼륨 또는 사용자 워크로드 단위 쿼터 한도를 API로 설정 가능해야 한다."],
  DMS01: ["Data Mover Node Provisioning",
    "GPU 클러스터 인도 최소 2주 전에 데이터 이동 작업용 전용 CPU 컴퓨트(베어메탈/VM)를 제공해야 한다."],
  DMS02: ["Data Mover Nodes (CPU)",
    "데이터 무버 기능 실행용 전용 CPU 노드 — 공유 파일시스템과 동일 패브릭 또는 동등 대역폭 경로의 고성능 네트워킹 제공."],
  NET01: ["Backend Switch Fabric API",
    "각 컴퓨트 노드에 대해 노드-코어 간 백엔드 네트워크 스위치 가시성을 API로 제공. 각 스위치는 고유 식별자로 식별."],
  NET02: ["NVLink Domain API",
    "NVLink 지원 노드(GB200·GB300·Vera Rubin)별로 노드가 속한 NVLink 도메인의 고유 식별자를 API가 반환해야 한다."],
  CAP01: ["Governance Metrics",
    "테넌시별 핵심 플릿 지표 추적·노출 — Delivered(인도)·Healthy(정상)·Reserved(예약)·Active/In-Use(사용 중) 노드·GPU."],
  CAP02: ["Resource Governance API",
    "노드별 Node ID·Health State(정상/비정상)·Instance ID·생성 시각·Hardware Type을 반환하는 거버넌스 API."],
  CAP05: ["Unified Health & Lifecycle APIs",
    "물리 호스트와 논리 컴퓨트 리소스 헬스의 단일 SoT(Source of Truth) — 호스트별 실시간 헬스(GPU·열·메모리) + 노드그룹/토폴로지 블록 수준 집계(스파인 장애 등 광역 장애 식별)."],
  TEL: ["Telemetry Delivery",
    "DCGM 등 텔레메트리를 합의된 Delivery Method(OTLP)와 Scope로 전달 — 수집~전달 지연 120초 이내."],
  SLA: ["Management Plane Availability",
    "관리 플레인(프로비저닝 API 등) 가용성 — 프로덕션 기준 financially-backed 99.95%+ 업타임."],
  SLO: ["Service SLO Targets",
    "스토리지 QoS — 요청 대역폭·IOPS 충족. 고속 스토리지 서비스 가용성 99.99%(30일 롤링, 정비 제외), 파일시스템 가용성 99.5%+/PB · 내구성 연 99.999%/PB."],
};

(function () {
  const tip = document.createElement("div");
  tip.id = "nvtip";
  (document.body ? Promise.resolve() : new Promise(r =>
    document.addEventListener("DOMContentLoaded", r))).then(() =>
    document.body.appendChild(tip));
  let pinned = null;

  function idsOf(el) {
    return [...new Set(el.textContent.split("·")
      .map(s => s.trim().split(/\s+/)[0]))].filter(id => NVREQ_GUIDE[id]);
  }
  function show(el) {
    if (document.documentElement.dataset.tips === "0") return;   // 말풍선 OFF
    const ids = idsOf(el);
    if (!ids.length) return;
    tip.innerHTML =
      '<div class="nvtip-h">NVIDIA BMaaS Requirements Guide v2.2</div>' +
      ids.map(id => {
        const [t, d] = NVREQ_GUIDE[id];
        return `<div class="nvtip-row"><b>${id}</b><span class="nvtip-t">${t}</span>
                <div class="nvtip-d">${d}</div></div>`;
      }).join("");
    tip.style.display = "block";
    const r = el.getBoundingClientRect();
    tip.style.maxWidth = Math.min(420, innerWidth - 24) + "px";
    const x = Math.min(Math.max(8, r.left), innerWidth - tip.offsetWidth - 8);
    let y = r.bottom + 9;
    tip.classList.remove("up");
    if (y + tip.offsetHeight > innerHeight - 8) {   // 아래 공간 부족 → 위로
      y = r.top - tip.offsetHeight - 9;
      tip.classList.add("up");
    }
    tip.style.left = x + "px";
    tip.style.top = Math.max(4, y) + "px";
    tip.style.setProperty("--ax", Math.max(12, Math.min(
      r.left + r.width / 2 - x - 5, tip.offsetWidth - 22)) + "px");
  }
  const hide = () => { if (!pinned) tip.style.display = "none"; };

  // 배지는 동적으로 생성되므로 위임 리스너 사용
  document.addEventListener("mouseover", e => {
    const b = e.target.closest && e.target.closest(".nvreq");
    if (b && !pinned) show(b);
  });
  document.addEventListener("mouseout", e => {
    if (e.target.closest && e.target.closest(".nvreq")) hide();
  });
  document.addEventListener("click", e => {
    const b = e.target.closest && e.target.closest(".nvreq");
    if (b) { pinned = (pinned === b) ? null : b; pinned ? show(b) : tip.style.display = "none"; }
    else if (pinned) { pinned = null; tip.style.display = "none"; }
  });
  addEventListener("scroll", () => { pinned = null; tip.style.display = "none"; }, true);

  document.addEventListener("nc-tips-off", () => {  // 스위치 OFF — 즉시 닫기
    pinned = null; tip.style.display = "none";
  });

  addEventListener("load", () => {                  // #nvreq — 시연용 자동 고정
    if (location.hash !== "#nvreq") return;
    const b = document.querySelector(".nvreq");
    if (b) { pinned = b; b.scrollIntoView({ block: "center" }); setTimeout(() => show(b), 400); }
  });
})();
