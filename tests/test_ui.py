"""UI 일관성 — 4개 페이지 공용 헤더·네비게이션 규약.

모든 페이지는 theme.css의 nc-header를 사용하고, 우상단 네비게이션에
4개 페이지 링크를 동일한 순서로 노출하며, 현재 페이지 버튼 1개만
active(.act)로 표시한다.
"""

import re

PAGES = ["/", "/flow", "/nico", "/arch", "/ops", "/customer", "/biz"]
# 실개발 우선순위 순서: 고객 → 비즈 → 운영 → Control-Plane → NICo → 검증 → VRCM
NAV_ORDER = ['href="/customer"', 'href="/biz"', 'href="/ops"', 'href="/arch"',
             'href="/nico"', 'href="/flow"', 'href="/"']


def test_theme_css_served(client):
    r = client.get("/static/theme.css")
    assert r.status_code == 200
    assert ".nc-header" in r.text and ".nc-nav" in r.text


def test_unified_header_on_every_page(client):
    for page in PAGES:
        html = client.get(page).text
        assert 'class="nc-header"' in html, f"{page}: nc-header 없음"
        assert '/static/theme.css' in html, f"{page}: theme.css 미포함"
        # 네비 4개 링크가 모두 존재하고 순서가 동일
        nav = re.search(r'<nav class="nc-nav">(.*?)</nav>', html, re.S)
        assert nav, f"{page}: nc-nav 없음"
        pos = [nav.group(1).find(h) for h in NAV_ORDER]
        assert all(p >= 0 for p in pos), f"{page}: 네비 링크 누락 {pos}"
        assert pos == sorted(pos), f"{page}: 네비 순서 불일치"
        # 현재 페이지 버튼만 active
        assert nav.group(1).count('class="act"') == 1, f"{page}: act 버튼 수"


def test_nvreq_tooltip_dictionary_covers_all_badges(client):
    """NVIDIA Req 말풍선 — 배지에 쓰인 모든 Req ID가 사전에 정의되어야 한다."""
    js = client.get("/static/nvreq.js")
    assert js.status_code == 200 and "NVREQ_GUIDE" in js.text
    defined = set(re.findall(r"^  ([A-Z]+\d*): \[", js.text, re.M))
    assert {"CNP01", "CAP05", "TEL", "SLA"} <= defined

    css = client.get("/static/theme.css").text
    assert "#nvtip" in css                       # 말풍선 스타일 공용 테마에 존재

    used = set()
    for page in ("/ops", "/customer", "/biz"):
        html = client.get(page).text
        assert "/static/nvreq.js" in html, f"{page}: nvreq.js 미포함"
        for badge in re.findall(r'class="nvreq"[^>]*>([^<]+)', html):
            for token in badge.split("·"):
                tok = token.strip().split()[0]
                if re.fullmatch(r"[A-Z]{3,4}\d*", tok):
                    used.add(tok)
    missing = used - defined
    assert not missing, f"말풍선 사전에 없는 Req ID: {sorted(missing)}"


def test_nav_icons_and_navjs_on_every_page(client):
    """네비 아이콘 + nav.js(티켓·승인 배지) — 전 페이지 포함."""
    assert client.get("/static/nav.js").status_code == 200
    for page in PAGES:
        html = client.get(page).text
        assert "/static/nav.js" in html, f"{page}: nav.js 미포함"
        assert "👤 고객 포털" in html and "🛠 운영 포털" in html, f"{page}: 아이콘"


def test_flow_console_matches_control_plane_module_names(client):
    """검증 콘솔(/flow) 모듈 아키텍처 = Control-Plane(/arch) 모듈 명명."""
    flow = client.get("/flow").text
    arch = client.get("/arch").text
    for name in ["Service-Order Intake", "Tenant Fulfillment", "BM Provisioning",
                 "Resource &amp; Service Model", "Policy Orchestration",
                 "Compute Services", "SDN / Network Fabric",
                 "Storage Services", "Shared Services"]:
        assert name in arch, f"/arch에 '{name}' 없음"
        assert name in flow, f"/flow에 '{name}' 없음 — Control-Plane 명명 불일치"
    # 낡은 표기 회귀 방지
    assert "미구현" not in flow


def test_control_plane_module_tooltips_cover_all_boxes(client):
    """모듈 역할 말풍선(modinfo.js) — /arch 전 박스·/flow 스트립 커버리지."""
    js = client.get("/static/modinfo.js")
    assert js.status_code == 200 and "MODULE_GUIDE" in js.text
    keys = set(re.findall(r'^  "([^"]+)": \[', js.text, re.M))
    alias_block = js.text.split("FLOW_MOD_ALIAS = {")[1].split("};")[0]
    alias = dict(re.findall(r'(\w+): "([\w-]+)"', alias_block))

    arch = client.get("/arch").text
    assert "/static/modinfo.js" in arch
    arch_ids = {i for i in re.findall(r'data-b="([^"]+)"', arch)
                if re.fullmatch(r"[a-z][a-z-]+", i)}   # JS 템플릿 리터럴 제외
    missing = arch_ids - keys
    assert not missing, f"/arch 모듈 말풍선 누락: {sorted(missing)}"

    flow = client.get("/flow").text
    assert "/static/modinfo.js" in flow
    flow_ids = {i for i in re.findall(r'data-m="([^"]+)"', flow)
                if re.fullmatch(r"[a-z][a-z-]*", i)}
    for m in flow_ids:
        assert alias.get(m, m) in keys, f"/flow 모듈 '{m}' 말풍선 매핑 누락"


def test_pipeline_stage_tooltips_cover_all_states(client):
    """주문 파이프라인 단계 말풍선(STAGE_GUIDE) — 상태머신 전 상태 커버 + 부착 지점."""
    from app.models import OrderState
    js = client.get("/static/modinfo.js").text
    assert "STAGE_GUIDE" in js and '"주문 파이프라인 단계"' in js
    guide_block = js.split("STAGE_GUIDE = {")[1].split("\n};")[0]
    keys = set(re.findall(r'^  (\w+): \[', guide_block, re.M))
    states = {s.value for s in OrderState}
    missing = states - keys
    assert not missing, f"STAGE_GUIDE 누락 상태: {sorted(missing)}"
    # 해시 자동 고정(#stage:) 지원
    assert "#(mod|stage):" in js

    # 부착 지점 — flow 트랙·arch 단계 카드·ops Fulfillment 셰브런
    assert 'data-stage="${s}"' in client.get("/flow").text
    assert 'data-stage="${s.state}"' in client.get("/arch").text
    ops = client.get("/ops").text
    assert 'data-stage="${s}"' in ops
    assert "/static/modinfo.js" in ops   # ops에서도 말풍선 동작


def test_tooltip_onoff_switch_in_header(client):
    """말풍선 On/Off 스위치(nav.js) — 전 말풍선 시스템 일괄 게이트."""
    nav = client.get("/static/nav.js").text
    assert "nc-tipsw" in nav and 'localStorage.getItem("nc-tips")' in nav
    assert "dataset.tips" in nav
    assert "💬 말풍선" in nav and "기능 설명 말풍선 On/Off" in nav   # 라벨·설명
    # 각 말풍선 시스템이 스위치 상태를 존중
    for path in ["/static/nvreq.js", "/static/modinfo.js"]:
        assert 'dataset.tips === "0"' in client.get(path).text, f"{path} 게이트 누락"
    assert 'dataset.tips==="0"' in client.get("/nico").text   # prov 말풍선 게이트
    assert ".nc-tipsw" in client.get("/static/theme.css").text


def test_no_stale_fabric_schema_references(client):
    """fabric API 스키마 변경(sus/spines → sites[]) 잔재 회귀 방지.

    실사용 버그 재발 방지: biz 포털 '활성 랙' 카드가 fab.sus를 참조해
    'Cannot read properties of undefined (reading reduce)'가 발생했었다."""
    fab = client.get("/api/v1/fabric/ib").json()
    assert "sites" in fab and "sus" not in fab and "spines" not in fab
    for page in PAGES:
        html = client.get(page).text
        assert "fab.sus" not in html, f"{page}: 제거된 fab.sus 참조"
        assert "f.spines" not in html, f"{page}: 제거된 f.spines 참조"


def test_nico_provisioning_detail_and_api_list_panels(client):
    """NICo 메뉴 — ⑧ Provisioning 세부(Node Anatomy)·⑨ REST API 리스트."""
    html = client.get("/nico").text
    for marker in ("Provisioning 세부", "DPU 기반 IP 할당·OS 설치",
                   "DPU-DHCP 서버", "iPXE", "loadProvDetail",
                   "NICo에 호출되는 REST API 리스트", "loadNicoApis"):
        assert marker in html, f"/nico: '{marker}' 없음"


def test_flow_console_tenant_equipment_fault_staging(client):
    """검증 콘솔 — 테넌트별 비정상 장비 생성(시연) 컨트롤."""
    html = client.get("/flow").text
    for marker in ("테넌트 장비 장애 스테이징", "stageEquipFault",
                   "clearEquipFault", "tray:maintenance", "rack:faulted"):
        assert marker in html, f"/flow: '{marker}' 없음"
