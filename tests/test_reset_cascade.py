"""전체/시스템별 초기화 — 캐스케이드 계약 회귀.

외부 계층(NICo Emulator :9000 / AI Infra :9100)은 best-effort:
미기동이면 'unreachable'로 보고하되 로컬(NOCP) 리셋은 성공해야 한다.
테스트는 결정성을 위해 닫힌 포트로 강제한다.
"""
import os

import pytest


@pytest.fixture()
def dead_emulators(monkeypatch):
    monkeypatch.setitem(os.environ, "NICO_EMULATOR_URL", "http://127.0.0.1:1")
    monkeypatch.setitem(os.environ, "AI_INFRA_URL", "http://127.0.0.1:1")


def test_reset_all_local_ok_even_when_emulators_down(client, dead_emulators):
    r = client.post("/api/v1/admin/reset-all")
    assert r.status_code == 200
    body = r.json()
    assert body["nocp"]["reseeded"] is True
    assert body["nico_emulator"]["status"] == "unreachable"
    assert body["ai_infra"]["status"] == "unreachable"
    # 로컬 스토어는 실제로 초기화됨 — 테넌트/주문 없음
    assert client.get("/api/v1/tenants").json() == []
    assert client.get("/api/v1/orders").json() == []


def test_per_system_reset_proxies(client, dead_emulators):
    r = client.post("/api/v1/admin/reset/nico").json()
    assert r["nico_emulator"]["status"] == "unreachable"
    r = client.post("/api/v1/admin/reset/ai-infra").json()
    assert r["ai_infra"]["status"] == "unreachable"


def test_consistency_report_shape(client, dead_emulators):
    r = client.get("/api/v1/integration/consistency")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ok", "adapter", "tenants", "findings"}
    assert body["adapter"] in ("local", "http")
    # 외부 미응답 → UNREACHABLE 경고 2건 (fail 아님 — ok 유지)
    kinds = [f["kind"] for f in body["findings"]]
    assert kinds.count("UNREACHABLE") == 2
    assert body["ok"] is True
    assert body["tenants"]["nico_emulator"] is None
    assert body["tenants"]["ai_infra"] is None
