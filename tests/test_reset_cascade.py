"""Full / per-system reset — cascade contract regression.

External layers (NICo Emulator :9000 / AI Infra :9100) are best-effort:
if down, report 'unreachable' but the local (NOCP) reset must still succeed.
The tests force a closed port for determinism.
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
    # The local store is actually reset — only the demo-seed tenants, no orders
    from app.seed import DEMO_TENANTS
    assert {t["id"] for t in client.get("/api/v1/tenants").json()} == \
        {f"tnt-{n}" for n, _, _ in DEMO_TENANTS}
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
    # External non-response → 2 UNREACHABLE warnings (not a fail — ok stays True)
    kinds = [f["kind"] for f in body["findings"]]
    assert kinds.count("UNREACHABLE") == 2
    assert body["ok"] is True
    assert body["tenants"]["nico_emulator"] is None
    assert body["tenants"]["ai_infra"] is None
