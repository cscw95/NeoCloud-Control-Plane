"""Demo playlist — the Day 0 → Day 1 → Day 2 presentation order (DEMO-ORDER.md, 18 steps).

Single source for the `day`, `order` and `showcase_order` fields of every scenario
(GET /api/v1/demo/scenarios) and for GET /api/v1/demo/playlist.

  day             0 = 구축 build (install, validate, sellable stock)
                  1 = 개통 turn-up (signup, order, service start)
                  2 = 운영 operate (use, billing, faults, grid)
  order           DEMO-ORDER.md step number 1..18 (several scenarios may share a step)
  showcase_order  1..8 for the eight flagship scenarios, None otherwise

A step without a runnable scenario is a `screen` item: the presenter opens its
console route instead.
"""

from __future__ import annotations

from typing import Optional

DAYS = [
    {"day": 0, "title_ko": "Day 0 — 구축", "title_en": "Day 0 — Build",
     "summary_ko": "설치·검증, 판매 가능 재고가 되기까지",
     "summary_en": "Install and validate until the racks are sellable stock"},
    {"day": 1, "title_ko": "Day 1 — 개통", "title_en": "Day 1 — Turn-up",
     "summary_ko": "고객 가입·주문·서비스 시작", "summary_en": "Customer signup, order, service start"},
    {"day": 2, "title_ko": "Day 2 — 운영", "title_en": "Day 2 — Operate",
     "summary_ko": "사용·과금·장애·전력망", "summary_en": "Usage, billing, faults, power grid"},
]

# step order, day, titles, features, scenario ids (first = lead), console screen
STEPS = [
    (1, 0, "랙 bring-up 15단계 (04 케이블 불일치 → 수정·재시도 → 판매 가능)",
     "Rack bring-up, 15 steps (step-04 miswire → fix → retry → sellable)", "F23",
     ["f-rack-bringup", "f23-rack-bringup"], ("ops", "bringup")),
    (2, 0, "펌웨어 번들 적용과 기준 일치", "Firmware bundle and baseline compliance", "F24",
     ["f24-firmware-bundle"], ("ops", "p2-firmware")),
    (3, 0, "부팅 무결성 검증 (measured boot)", "Boot integrity (measured boot)", "F25",
     ["f25-attestation"], ("ops", "p2-attest")),
    (4, 0, "헬스 검증 규칙 · 클러스터 준비 게이트", "Health validation rules · cluster readiness gate",
     "F28", ["f28-health-validation"], ("ops", "p2-pipeline")),
    (5, 0, "MaxLPS 전력 도메인 예산 확인 (227/187 kW, 도메인 32.3 MW)",
     "MaxLPS power-domain budget (227/187 kW, 32.3 MW domain)", "",
     ["f35-capacity-scenarios"], ("ops", "dsx-power")),
    (6, 1, "고객 셀프 가입 → 조직 · OIDC 로그인", "Customer self signup → organization · OIDC login",
     "F07", ["f07-identity"], ("ops", "v2-accounts")),
    (7, 1, "약정 계약 (예약 NVL72 랙) → 청구서", "Reserved contract (NVL72 racks) → invoice", "F06",
     ["a-signup-to-invoice"], ("biz", "contracts")),
    (8, 1, "베어메탈(BMaaS) 주문 → 테넌트 격리(VPC·IB·NVLink) → 인수",
     "Bare-metal (BMaaS) order → tenant isolation (VPC·IB·NVLink) → acceptance", "F08",
     ["h-bmaas-baseline", "f08-nico-rest"], ("ops", "provisioning")),
    (9, 1, "NVKE 관리형 K8s (클리크 인식 노드풀, IMEX, kubeconfig)",
     "NVKE managed K8s (clique-aware node pool, IMEX, kubeconfig)", "F11",
     ["g-nvke-clique", "f11-nvke-capi"], ("ops", "nvke-capi")),
    (10, 1, "마켓플레이스 원클릭 모델 배포 → 토큰 API 키 발급",
     "Marketplace one-click model deploy → token API key", "F32",
     ["f32-marketplace-deploy", "f29-packaging"], ("customer", "dashboard")),
    (11, 2, "토큰 API 호출(스트리밍) → 한도 429 → 안전 필터 → 사용량 대조",
     "Token API (streaming) → 429 quota → safety filter → usage reconciliation", "F01–F04",
     ["b-token-api", "f01-token-gateway", "f02-f04-quota-metering"], ("ops", "v2-token")),
    (12, 2, "KV 캐시 계층 (G4 = VAST) 적중 → cached tokens 요율",
     "KV cache tiers (G4 = VAST) hit → cached-token rate", "F33",
     ["f33-kv-tiers"], ("ops", "v2-token")),
    (13, 2, "작업 배치: KAI 큐 · 갱 스케줄링, Slurm", "Placement: KAI queues · gang scheduling, Slurm",
     "F30/F31", ["f30-kai-scheduling", "f31-slurm"], ("ops", "nvke-sched")),
    (14, 2, "GPU 장애(XID) → 격리·드레인·리셋·교체 → SLA 크레딧",
     "GPU fault (XID) → isolate · drain · reset · replace → SLA credit", "F10/F09",
     ["c-xid-remediation", "f10-f09-recovery-sla", "f12-machine-events"], ("ops", "v2-recovery")),
    (15, 2, "누수 감지 → 격리 → 워크로드 이전", "Leak detected → isolate → move the workload", "F22",
     ["e-leak-workload-move", "f22-leak-response"], ("ops", "obs-dlc")),
    (16, 2, "전력망 감축(DSX Flex) → 예약 보호 · 배치 축소 → 원복 → 정산 · 공지",
     "Grid curtailment (DSX Flex) → protect reserved · shrink batch → restore → settle · notice",
     "F20/F21/F13", ["d-grid-curtailment", "f20-dsx-flex", "f21-curtailment", "f13-status-notices"],
     ("ops", "p2-grid")),
    (17, 2, "월말 청구서 (원화·부가세·세금계산서, SLA 크레딧 차감) → 결제, FinOps 원가",
     "Month-end invoice (KRW · VAT · tax invoice, SLA credit) → payment, FinOps cost", "F05/F27",
     ["f05-billing-lago-stripe", "f27-finops"], ("biz", "revenue")),
    (18, 2, "관측 지표(/metrics) · 감사 증적 내보내기", "Metrics (/metrics) · audit evidence export",
     "F26/F34", ["f26-metrics", "f34-compliance-evidence"], ("ops", "p2-metrics")),
]

# steps whose lead is a console screen (existing feature, DEMO-ORDER "[기존]"); their
# scenarios follow the screen as a supplement
SCREEN_STEPS = {5}

# the eight flagship scenarios, Day 0 → 2 (Demo Center showcase)
SHOWCASE = ["f-rack-bringup", "a-signup-to-invoice", "h-bmaas-baseline", "g-nvke-clique",
            "b-token-api", "c-xid-remediation", "e-leak-workload-move", "d-grid-curtailment"]

_META: dict[str, dict] = {}
_pos = 0
for _o, _d, _ko, _en, _f, _ids, _scr in STEPS:
    for _i in _ids:
        _pos += 1
        _META[_i] = {"day": _d, "order": _o, "seq": _pos}
del _o, _d, _ko, _en, _f, _ids, _scr


def meta(scenario_id: str) -> dict:
    """{day, order, seq, showcase_order} for a scenario; unknown ids sort last (day 2)."""
    m = dict(_META.get(scenario_id) or {"day": 2, "order": 99, "seq": 999})
    m["showcase_order"] = (SHOWCASE.index(scenario_id) + 1) if scenario_id in SHOWCASE else None
    return m


def parse_day(v) -> Optional[int]:
    """0 / "0" / "day0" / "Day 1" → int; None when absent; ValueError when not 0..2."""
    if v is None or v == "":
        return None
    s = str(v).strip().lower().replace("day", "").strip()
    d = int(s)
    if d not in (0, 1, 2):
        raise ValueError(v)
    return d


def build(scenarios: dict[str, dict], day: Optional[int] = None) -> dict:
    """Playlist in Day 0 → 2 order (shape agreed with the Console Demo Center):
    {days:[{day,title_*,summary_*}], items:[{order, step, day, kind:"scenario"|"screen",
    scenario_id?, title_*, caption_*, console, route, showcase_order?}]}.
    Each step lists its scenarios (those loaded in this build) with the console route to
    show next to them; a screen step (or a step with no loaded scenario) is a `screen`
    item carrying only its console route."""
    items: list[dict] = []
    for o, d, ko, en, feat, ids, (console, route) in STEPS:
        if day is not None and d != day:
            continue
        loaded = [i for i in ids if i in scenarios]
        screen = {"kind": "screen", "step": o, "day": d, "scenario_id": None,
                  "title_ko": ko, "title_en": en, "caption_ko": ko, "caption_en": en,
                  "feature": feat, "console": console, "route": route,
                  "screen_only": True, "showcase_order": None}
        if o in SCREEN_STEPS or not loaded:
            items.append(screen)
        for sid in loaded:
            sc = scenarios[sid]
            items.append({"kind": "scenario", "step": o, "day": d, "scenario_id": sid,
                          "title_ko": sc["title_ko"], "title_en": sc["title_en"],
                          "caption_ko": ko, "caption_en": en, "feature": sc.get("feature") or feat,
                          "console": console, "route": route,
                          "showcase_order": meta(sid)["showcase_order"]})
    for n, it in enumerate(items, 1):
        it["order"] = n
    days = [d for d in DAYS if day is None or d["day"] == day]
    return {"source": "DEMO-ORDER.md", "steps": len({i["step"] for i in items}),
            "days": days, "items": items, "showcase": [s for s in SHOWCASE if s in scenarios]}
