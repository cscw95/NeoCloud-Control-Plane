"""UI consistency — shared header/navigation convention across the 4 pages.

Every page uses theme.css's nc-header, exposes the four page links in the
top-right navigation in the same order, and marks only the current page's
single button as active (.act).
"""

import re

PAGES = ["/", "/flow", "/nico", "/arch", "/ops", "/customer", "/biz"]
# Real-dev priority order: Customer → Biz → Ops → Control-Plane → NICo → Verify → NOCP
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
        # All 4 nav links exist and in the same order
        nav = re.search(r'<nav class="nc-nav">(.*?)</nav>', html, re.S)
        assert nav, f"{page}: nc-nav 없음"
        pos = [nav.group(1).find(h) for h in NAV_ORDER]
        assert all(p >= 0 for p in pos), f"{page}: 네비 링크 누락 {pos}"
        assert pos == sorted(pos), f"{page}: 네비 순서 불일치"
        # Only the current page's button is active
        assert nav.group(1).count('class="act"') == 1, f"{page}: act 버튼 수"


def test_nvreq_tooltip_dictionary_covers_all_badges(client):
    """NVIDIA Req tooltips — every Req ID used in a badge must be defined in the dictionary."""
    js = client.get("/static/nvreq.js")
    assert js.status_code == 200 and "NVREQ_GUIDE" in js.text
    defined = set(re.findall(r"^  ([A-Z]+\d*): \[", js.text, re.M))
    assert {"CNP01", "CAP05", "TEL", "SLA"} <= defined

    css = client.get("/static/theme.css").text
    assert "#nvtip" in css                       # tooltip style exists in the shared theme

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
    """Nav icons + nav.js (ticket/approval badges) — included on every page."""
    assert client.get("/static/nav.js").status_code == 200
    for page in PAGES:
        html = client.get(page).text
        assert "/static/nav.js" in html, f"{page}: nav.js 미포함"
        # Redesign 1b: emoji icons removed — flat tab text only
        assert ">고객 포털</a>" in html or ">고객</a>" in html, f"{page}: 고객 탭"


def test_flow_console_matches_control_plane_module_names(client):
    """Verify console (/flow) module architecture = Control-Plane (/arch) module naming."""
    flow = client.get("/flow").text
    arch = client.get("/arch").text
    for name in ["Service-Order Intake", "Tenant Fulfillment", "BM Provisioning",
                 "Resource &amp; Service Model", "Policy Orchestration",
                 "Compute Services", "SDN / Network Fabric",
                 "Storage Services", "Shared Services"]:
        assert name in arch, f"/arch에 '{name}' 없음"
        assert name in flow, f"/flow에 '{name}' 없음 — Control-Plane 명명 불일치"
    # Guard against regression to stale labels
    assert "미구현" not in flow


def test_control_plane_module_tooltips_cover_all_boxes(client):
    """Module-role tooltips (modinfo.js) — coverage of all /arch boxes and the /flow strip."""
    js = client.get("/static/modinfo.js")
    assert js.status_code == 200 and "MODULE_GUIDE" in js.text
    keys = set(re.findall(r'^  "([^"]+)": \[', js.text, re.M))
    alias_block = js.text.split("FLOW_MOD_ALIAS = {")[1].split("};")[0]
    alias = dict(re.findall(r'(\w+): "([\w-]+)"', alias_block))

    arch = client.get("/arch").text
    assert "/static/modinfo.js" in arch
    arch_ids = {i for i in re.findall(r'data-b="([^"]+)"', arch)
                if re.fullmatch(r"[a-z][a-z-]+", i)}   # exclude JS template literals
    missing = arch_ids - keys
    assert not missing, f"/arch 모듈 말풍선 누락: {sorted(missing)}"

    flow = client.get("/flow").text
    assert "/static/modinfo.js" in flow
    flow_ids = {i for i in re.findall(r'data-m="([^"]+)"', flow)
                if re.fullmatch(r"[a-z][a-z-]*", i)}
    for m in flow_ids:
        assert alias.get(m, m) in keys, f"/flow 모듈 '{m}' 말풍선 매핑 누락"


def test_pipeline_stage_tooltips_cover_all_states(client):
    """Order-pipeline stage tooltips (STAGE_GUIDE) — cover all state-machine states + attach points."""
    from app.models import OrderState
    js = client.get("/static/modinfo.js").text
    assert "STAGE_GUIDE" in js and '"주문 파이프라인 단계"' in js
    guide_block = js.split("STAGE_GUIDE = {")[1].split("\n};")[0]
    keys = set(re.findall(r'^  (\w+): \[', guide_block, re.M))
    states = {s.value for s in OrderState}
    missing = states - keys
    assert not missing, f"STAGE_GUIDE 누락 상태: {sorted(missing)}"
    # Hash auto-pinning (#stage:) support
    assert "#(mod|stage):" in js

    # Attach points — flow track / arch stage cards / ops Fulfillment chevron
    assert 'data-stage="${s}"' in client.get("/flow").text
    assert 'data-stage="${s.state}"' in client.get("/arch").text
    ops = client.get("/ops").text
    assert 'data-stage="${s}"' in ops
    assert "/static/modinfo.js" in ops   # tooltips work on ops too


def test_tooltip_onoff_switch_in_header(client):
    """Tooltip On/Off switch (nav.js) — gates all tooltip systems at once."""
    nav = client.get("/static/nav.js").text
    assert "nc-tipsw" in nav and 'localStorage.getItem("nc-tips")' in nav
    assert "dataset.tips" in nav
    assert "말풍선 <input" in nav and "기능 설명 말풍선 On/Off" in nav   # label / description
    # Each tooltip system respects the switch state
    for path in ["/static/nvreq.js", "/static/modinfo.js"]:
        assert 'dataset.tips === "0"' in client.get(path).text, f"{path} 게이트 누락"
    assert 'dataset.tips==="0"' in client.get("/nico").text   # prov tooltip gate
    assert ".nc-tipsw" in client.get("/static/theme.css").text


def test_no_stale_fabric_schema_references(client):
    """Guard against leftover regressions from the fabric API schema change (sus/spines → sites[]).

    Prevents a recurrence of a real bug: the biz portal 'active racks' card
    referenced fab.sus and raised
    'Cannot read properties of undefined (reading reduce)'."""
    fab = client.get("/api/v1/fabric/ib").json()
    assert "sites" in fab and "sus" not in fab and "spines" not in fab
    for page in PAGES:
        html = client.get(page).text
        assert "fab.sus" not in html, f"{page}: 제거된 fab.sus 참조"
        assert "f.spines" not in html, f"{page}: 제거된 f.spines 참조"


def test_nico_provisioning_detail_and_api_list_panels(client):
    """NICo menu — ⑧ Provisioning detail (Node Anatomy) / ⑨ REST API list."""
    html = client.get("/nico").text
    for marker in ("Provisioning 세부", "DPU 기반 IP 할당·OS 설치",
                   "DPU-DHCP 서버", "iPXE", "loadProvDetail",
                   "NICo에 호출되는 REST API 리스트", "loadNicoApis"):
        assert marker in html, f"/nico: '{marker}' 없음"


def test_flow_console_tenant_equipment_fault_staging(client):
    """Verify console — per-tenant faulty-equipment creation (demo) control."""
    html = client.get("/flow").text
    for marker in ("테넌트 장비 장애 스테이징", "stageEquipFault",
                   "clearEquipFault", "tray:maintenance", "rack:faulted"):
        assert marker in html, f"/flow: '{marker}' 없음"
