"""ComputeBlock power budget: domain PATCH and rack policy changes (Phase-1 seed,
VR NVL72 MaxP 227 / MaxQ 187 kW reference design)."""
import pytest

from app import power
from app.seed import seed_default
from app.store import STORE


@pytest.fixture()
def phase1(client):
    seed_default(STORE)              # 4 ComputeBlocks, 140 VR racks at maxq
    power.POWER.reset()
    yield client
    power.POWER.reset()


def _block_racks(block):
    return [r.id for r in power._block_racks(block)]


def test_blocks_and_local_domains_match_topology(phase1):
    want = {"aif-stt-gasan-f1": (6000.0, 24), "aif-stt-gasan-f2": (3000.0, 12),
            "aif-igis-ansan-f1": (12900.0, 54), "aif-igis-ansan-f2": (10400.0, 50)}
    doms = phase1.get("/api/v1/power/domains").json()["domains"]
    got = {d["compute_block"]: (d["operating_limit_kw"], d["rack_count"]) for d in doms}
    assert got == want
    assert all(d["id"] == f"pd-{d['compute_block']}" and d["budget_ok"] for d in doms)


def test_wpps_max_q_is_reference_maxq(phase1):
    rack = _block_racks("aif-stt-gasan-f1")[0]
    r = phase1.post(f"/api/v1/racks/{rack}/power-policy",
                    json={"policy": "maxq", "profile": "max-q"}).json()
    assert r["power_cap_kw"] == 187
    r = phase1.post(f"/api/v1/racks/{rack}/power-policy",
                    json={"policy": "dps-dynamic", "profile": "inference"}).json()
    assert r["power_cap_kw"] == 170


def test_domain_patch_above_block_budget_rejected(phase1):
    did = "pd-aif-igis-ansan-f2"
    assert phase1.patch(f"/api/v1/power/domains/{did}",
                        json={"operating_limit_kw": 10400}).status_code == 200
    r = phase1.patch(f"/api/v1/power/domains/{did}", json={"operating_limit_kw": 10401})
    assert r.status_code == 422 and "budget" in r.json()["detail"]


def test_rack_policy_static_caps_must_fit_block_budget(phase1):
    # ansan-f2: 50 racks × 187 kW (maxq) = 9350 of 10400 kW → 1050 kW spare;
    # each maxq→maxp step costs 40 kW, so 26 racks fit and the 27th is refused.
    racks = _block_racks("aif-igis-ansan-f2")
    for rid in racks[:26]:
        assert phase1.post(f"/api/v1/racks/{rid}/power-policy",
                           json={"policy": "maxp"}).status_code == 200
    r = phase1.post(f"/api/v1/racks/{racks[26]}/power-policy", json={"policy": "maxp"})
    assert r.status_code == 422 and "aif-igis-ansan-f2" in r.json()["detail"]
    assert STORE.racks[racks[26]].power_cap_kw == 187          # unchanged on refusal
    # dps-dynamic reserves nothing (DPS steers inside the domain limit) → allowed,
    # and it frees that rack's static reservation for another maxp rack
    assert phase1.post(f"/api/v1/racks/{racks[26]}/power-policy",
                       json={"policy": "dps-dynamic"}).status_code == 200
    assert phase1.post(f"/api/v1/racks/{racks[27]}/power-policy",
                       json={"policy": "maxp"}).status_code == 200


def test_gasan_f1_fits_all_racks_at_maxp(phase1):
    # 24 × 227 = 5448 ≤ 6000 kW: the whole floor can run MaxP statically
    for rid in _block_racks("aif-stt-gasan-f1"):
        assert phase1.post(f"/api/v1/racks/{rid}/power-policy",
                           json={"policy": "maxp"}).status_code == 200
