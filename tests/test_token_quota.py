"""F02 — TPM/RPM token buckets, tier separation, override expiry, 429 wire format."""

import json
from pathlib import Path

import pytest

from app import token_quota as tq
from tests.contracts.mini_schema import errors

CONTRACTS = Path(__file__).parent / "contracts"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def clock(client):
    c = Clock()
    old = tq.QUOTA.clock
    tq.QUOTA.clock = c
    tq.reset()
    yield c
    tq.QUOTA.clock = old
    tq.reset()


def _tenant(client):
    r = client.post("/api/v1/tenants", json={"name": "quota-a", "isolation_tier": "bare_metal_dedicated"})
    return r.json()["id"] if r.status_code == 201 else "tnt-quota-a"


def test_defaults_and_put(client, clock):
    tid = _tenant(client)
    rows = client.get("/api/v1/token/quotas", params={"tenant_id": tid}).json()["data"]
    assert {r["tier"] for r in rows} == {"interactive", "batch"}
    assert all(r["source"] == "default" for r in rows)
    r = client.put(f"/api/v1/token/quotas/{tid}",
                   json={"tier": "batch", "tpm": 6000, "rpm": 6, "monthly_token_cap": 50000})
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["tpm"], b["rpm"], b["monthly_token_cap"], b["source"]) == (6000, 6, 50000, "configured")
    assert client.put("/api/v1/token/quotas/tnt-nope",
                      json={"tier": "batch", "tpm": 1, "rpm": 1}).status_code == 404
    assert client.put(f"/api/v1/token/quotas/{tid}",
                      json={"tier": "gold", "tpm": 1, "rpm": 1}).status_code == 422


def test_token_bucket_refill_with_clock(client, clock):
    tid = _tenant(client)
    tq.set_quota(tid, "interactive", tpm=600, rpm=100)
    ok, _, h = tq.QUOTA.check(tid, "k1", "interactive", 100)
    assert ok and h["x-ratelimit-limit-tokens"] == "600"
    tq.QUOTA.commit(tid, "k1", "interactive", 600)          # bucket drained to 0
    ok, retry, h = tq.QUOTA.check(tid, "k1", "interactive", 10)
    assert not ok and h["x-neocloud-ratelimit-reason"] == "tokens"
    assert retry == 1 and h["retry-after"] == "1"             # 1 token at 10 tok/s
    assert h["x-ratelimit-remaining-tokens"] == "0"
    clock.t += 30                                            # +300 tokens
    ok, _, h = tq.QUOTA.check(tid, "k1", "interactive", 10)
    assert ok and h["x-ratelimit-remaining-tokens"] == "300"
    assert h["x-ratelimit-reset-tokens"] == "30s"
    clock.t += 3600                                          # capped at capacity
    ok, _, h = tq.QUOTA.check(tid, "k1", "interactive", 10)
    assert h["x-ratelimit-remaining-tokens"] == "600"


def test_rpm_limit_and_debt(client, clock):
    tid = _tenant(client)
    tq.set_quota(tid, "interactive", tpm=1000, rpm=2)
    assert tq.QUOTA.check(tid, "k", "interactive")[0]
    assert tq.QUOTA.check(tid, "k", "interactive")[0]
    ok, retry, h = tq.QUOTA.check(tid, "k", "interactive")
    assert not ok and h["x-neocloud-ratelimit-reason"] == "requests" and retry == 30
    # a response larger than the bucket leaves a debt (Envoy: deduct at stream end)
    clock.t += 60
    tq.QUOTA.commit(tid, "k", "interactive", 1500)
    ok, retry, _ = tq.QUOTA.check(tid, "k", "interactive")
    assert not ok and retry == 31                            # 501 tokens at 1000/min


def test_tiers_are_separate(client, clock):
    tid = _tenant(client)
    tq.set_quota(tid, "batch", tpm=100, rpm=10)
    tq.QUOTA.commit(tid, "k", "batch", 100)
    assert not tq.QUOTA.check(tid, "k", "batch")[0]
    assert tq.QUOTA.check(tid, "k", "interactive")[0]


def test_key_level_limit(client, clock):
    tid = _tenant(client)
    tq.set_quota(tid, "interactive", tpm=100_000, rpm=100)
    tq.set_quota(tid, "interactive", tpm=50, rpm=100, key_id="key-a")
    tq.QUOTA.commit(tid, "key-a", "interactive", 50)
    assert not tq.QUOTA.check(tid, "key-a", "interactive")[0]
    assert tq.QUOTA.check(tid, "key-b", "interactive")[0]
    rows = client.get("/api/v1/token/quotas", params={"tenant_id": tid}).json()["data"]
    assert any(r["key_id"] == "key-a" for r in rows)


def test_override_and_expiry(client, clock):
    tid = _tenant(client)
    tq.set_quota(tid, "batch", tpm=10_000, rpm=100)
    until = tq._iso(clock.t + 600)
    r = client.post(f"/api/v1/token/quotas/{tid}/override",
                    json={"tier": "batch", "factor": 0.3, "until": until, "reason": "grid DR"})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["effective"] == {"tpm": 3000, "rpm": 30, "factor": 0.3}
    assert b["override"]["reason"] == "grid DR" and b["remaining"]["tokens"] == 3000
    tq.QUOTA.commit(tid, "k", "batch", 3000)
    assert not tq.QUOTA.check(tid, "k", "batch")[0]
    inter = client.get("/api/v1/token/quotas", params={"tenant_id": tid, "tier": "interactive"})
    assert inter.json()["data"][0]["effective"]["factor"] == 1.0
    clock.t += 601                                           # expired → restored
    row = client.get("/api/v1/token/quotas", params={"tenant_id": tid, "tier": "batch"}).json()["data"][0]
    assert row["override"] is None and row["effective"]["tpm"] == 10_000
    past = client.post(f"/api/v1/token/quotas/{tid}/override",
                       json={"tier": "batch", "factor": 0.5, "until": tq._iso(clock.t - 1)})
    assert past.status_code == 422


def test_curtailment_seam(client, clock):
    tid = _tenant(client)
    res = tq.apply_override(tid, "batch", 0.3, tq._iso(clock.t + 60), "F21")
    assert res["effective"]["factor"] == 0.3
    tq.clear_override(tid, "batch")
    assert tq.QUOTA.effective(tid, "batch")["factor"] == 1.0


def test_monthly_cap_insufficient_quota(client, clock):
    tid = _tenant(client)
    tq.set_quota(tid, "interactive", tpm=10**9, rpm=10**6, monthly_token_cap=1000)
    tq.QUOTA.commit(tid, "k", "interactive", 1000)
    ok, retry, h = tq.QUOTA.check(tid, "k", "interactive")
    assert not ok and h["x-neocloud-ratelimit-code"] == "insufficient_quota" and retry > 0
    body = tq.error_body(h)
    assert body["error"]["code"] == "insufficient_quota"


def test_429_body_matches_openai_error_schema(client, clock):
    root = json.loads((CONTRACTS / "openai_error.json").read_text())
    tid = _tenant(client)
    tq.set_quota(tid, "interactive", tpm=10, rpm=10)
    tq.QUOTA.commit(tid, "k", "interactive", 10)
    ok, _, h = tq.QUOTA.check(tid, "k", "interactive")
    body = tq.error_body(h)
    assert not ok and errors(body, root["ErrorResponse"], root) == []
    assert body["error"]["type"] == "rate_limit_exceeded"
    for k in ("limit", "remaining", "reset"):
        for u in ("requests", "tokens"):
            assert f"x-ratelimit-{k}-{u}" in h


def test_reset_duration_format():
    assert tq.reset_duration(0.12) == "120ms"
    assert tq.reset_duration(1) == "1s"
    assert tq.reset_duration(360) == "6m0s"
    assert tq.reset_duration(3723) == "1h2m3s"


def test_tenant_cannot_write(client, clock):
    tid = _tenant(client)
    hdr = {"X-Tenant-Id": tid, "X-Tenant-Role": "admin"}
    r = client.put(f"/api/v1/token/quotas/{tid}", headers=hdr,
                   json={"tier": "batch", "tpm": 1, "rpm": 1})
    assert r.status_code == 403
    rows = client.get("/api/v1/token/quotas", headers=hdr).json()["data"]
    assert rows and {r["tenant_id"] for r in rows} == {tid}


def test_gateway_hook_registered_when_f01_present():
    try:
        from app import token_api
    except ImportError:
        pytest.skip("F01 gateway not merged on this branch")
    assert token_api.QUOTA is tq.QUOTA
