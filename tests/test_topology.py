# seed: SU-1 = GB200 NVL72, SU-2 = Vera Rubin NVL72 (mixed generation)

def test_summary_counts_mixed_generation(client):
    s = client.get("/api/v1/inventory/summary").json()
    assert s["scalable_units"] == 2
    assert s["racks"] == 30               # gb200 SU 14랙 + vr SU 16랙(NCP RD)
    assert s["gpus"] == 2160              # 72/rack × 30랙
    assert s["cpus"] == 1080              # 30랙 × 36
    assert s["dpus"] == 540               # 30 x 18 trays
    # power differs by generation: GB200=120kW, VR(maxq)=200kW
    assert s["design_power_mw"] == 5.31   # 14×120 + 16×227 kW
    assert s["capped_power_mw"] == 4.67   # 14×120 + 16×187 kW (MaxQ 확정)
    # multi-generation aggregations
    assert s["gpus_by_arch"]["Blackwell (B200)"] == 1008
    assert s["gpus_by_arch"]["Rubin"] == 1152    # vr SU 16랙
    assert s["racks_by_generation"]["GB200 NVL72"] == 14
    assert s["racks_by_generation"]["Vera Rubin NVL72"] == 16


def test_blueprints_catalog(client):
    bps = {b["key"]: b for b in client.get("/api/v1/blueprints").json()}
    assert set(bps) == {"gb200-nvl72", "gb300-nvl72", "vr-nvl72"}
    assert bps["gb200-nvl72"]["gpu_hbm_gb"] == 192
    assert bps["gb200-nvl72"]["nvlink_gen"] == "NVLink5"
    assert bps["vr-nvl72"]["gpu_hbm_gb"] == 288
    assert bps["vr-nvl72"]["nvlink_gen"] == "NVLink6"
    assert bps["gb300-nvl72"]["preliminary"] is True


def test_tree_shows_generation(client):
    t = client.get("/api/v1/topology/tree").json()
    sus = t["factories"][0]["compute_blocks"][0]["deployment_units"][0]["scalable_units"]
    su1 = next(s for s in sus if s["id"] == "su-1")
    su2 = next(s for s in sus if s["id"] == "su-2")
    assert su1["model"] == "GB200 NVL72"
    assert su2["model"] == "Vera Rubin NVL72"
    assert su1["racks"][0]["gpu_arch"] == "Blackwell (B200)"
    assert su1["gpu_count"] == 1008


def test_rack_gpu_specs_per_generation(client):
    gb = client.get("/api/v1/racks/su-1-rack-00/gpus").json()
    vr = client.get("/api/v1/racks/su-2-rack-00/gpus").json()
    assert len(gb) == 72 and len(vr) == 72
    assert gb[0]["hbm_gb"] == 192 and gb[0]["hbm_type"] == "HBM3e"
    assert vr[0]["hbm_gb"] == 288 and vr[0]["hbm_type"] == "HBM4"
    assert gb[0]["arch"] == "Blackwell (B200)"


def test_provision_gb300_su(client):
    r = client.post("/api/v1/scalable-units?blueprint_key=gb300-nvl72").json()
    assert r["model"] == "GB300 NVL72"
    assert r["gpu_count"] == 1008
    s = client.get("/api/v1/inventory/summary").json()
    assert s["scalable_units"] == 3
    assert s["racks"] == 44   # 30 + gb300 SU 14랙
    assert s["gpus_by_arch"]["Blackwell Ultra (B300)"] == 1008


def test_provision_unknown_blueprint_rejected(client):
    r = client.post("/api/v1/scalable-units?blueprint_key=h100")
    assert r.status_code == 422


def test_power_policy_per_generation(client):
    # Vera Rubin: maxp = 227
    r = client.post("/api/v1/racks/su-2-rack-00/power-policy",
                    json={"policy": "maxp"}).json()
    assert r["power_cap_kw"] == 227
    # GB200: maxq == maxp == 120 (single power point)
    r = client.post("/api/v1/racks/su-1-rack-00/power-policy",
                    json={"policy": "maxp"}).json()
    assert r["power_cap_kw"] == 120


def test_su_composition_hac_view(client):
    """SU(HAC) 시스템 구성 — 컴퓨트·CMX·IB·Converged·CDU(DLC)·CPU 풀 (NCP RA)."""
    c = client.get("/api/v1/topology/su-composition?su_id=su-2").json()
    assert c["blueprint"] == "vr-nvl72" and c["hac_id"] == "su-2-hac"
    assert c["compute"]["racks"] == 16 and c["compute"]["gpus"] == 1152
    assert c["cmx"]["racks"] == 2 and c["cmx"]["cms_chassis"] == 32
    assert c["ib"]["leaves"] == 32 and c["ib"]["spine_racks"] == 4
    assert c["ib"]["node_links_800g"] == 2304
    assert c["converged"]["spines"] == 4 and c["converged"]["leaves"] == 12
    assert c["cooling"]["cdu"] == 2 and c["cooling"]["hac_tdp_mw"] == 3.9
    assert c["cooling"]["cooling_class"] == "liquid"
    assert c["cpu_pool"]["total"] == 60 and c["cpu_pool"]["per_tenant"] == 5
    assert client.get(
        "/api/v1/topology/su-composition?su_id=no-such").status_code == 404
