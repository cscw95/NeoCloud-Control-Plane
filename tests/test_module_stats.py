"""실시간 API 호출 집계 — 미들웨어 카운터 + module-stats 엔드포인트 + 모듈 매핑.

/flow 검증 콘솔의 '모듈 아키텍처' 라이브 배지가 소비하는 백엔드 계약을 검증한다.
"""

from app.metrics import MODULES, modules_for_path


def test_module_mapping_covers_key_prefixes():
    assert modules_for_path("/api/v1/orders") == ["cp-intake", "cp-fulfill"]
    assert modules_for_path("/api/v1/orders/o-1/flow") == ["cp-intake", "cp-fulfill"]
    assert modules_for_path("/api/v1/tenants") == ["cp-intake"]
    assert modules_for_path("/api/v1/scalable-units") == ["cp-provision"]
    assert modules_for_path("/api/v1/racks/r-1/gpus") == ["cp-provision"]
    assert modules_for_path("/api/v1/cpu-nodes") == ["cp-provision"]
    assert modules_for_path("/api/v1/allocations") == ["cp-provision"]
    assert modules_for_path("/api/v1/fabric/ib") == ["d-sdn"]
    assert modules_for_path("/api/v1/reconcile/run") == ["cp-policy"]
    assert modules_for_path("/api/v1/billing/usage") == ["cp-sla"]
    assert modules_for_path("/api/v1/tickets") == ["cp-delivery"]
    assert modules_for_path("/api/v1/integration/topology") == ["cp-obs"]
    assert modules_for_path("/api/v1/trace") == ["cp-obs"]
    assert modules_for_path("/fake-nico/hosts") == ["d-compute"]
    assert modules_for_path("/fake-vast/v1/clusters") == ["d-storage"]
    assert modules_for_path("/fake-shared/iam") == ["d-shared"]
    assert modules_for_path("/flow") == ["cp-api"]
    assert modules_for_path("/") == ["cp-api"]
    assert modules_for_path("/static/theme.css") == ["cp-api"]
    assert modules_for_path("/some/unknown/thing") == ["other"]


def test_module_stats_shape(client):
    s = client.get("/api/v1/integration/module-stats").json()
    ids = [m["id"] for m in s["modules"]]
    assert ids == MODULES                       # 14개 · 고정 순서
    assert len(ids) == 14
    for m in s["modules"]:
        assert set(m) == {"id", "calls", "eps", "last_active_s"}
        assert isinstance(m["calls"], int)
        assert isinstance(m["eps"], (int, float))
    tot = s["totals"]
    assert {"calls", "eps", "active_orders", "active_jobs",
            "tenants", "pipeline_events"} <= set(tot)
    assert "at" in s and "twin" in s


def test_middleware_counts_increment_per_module(client):
    def calls_of(mid):
        s = client.get("/api/v1/integration/module-stats").json()
        return {m["id"]: m["calls"] for m in s["modules"]}[mid]

    before_intake = calls_of("cp-intake")
    before_provision = calls_of("cp-provision")
    before_sla = calls_of("cp-sla")

    for _ in range(3):
        client.get("/api/v1/tenants")             # → cp-intake
    client.get("/api/v1/scalable-units")          # → cp-provision
    client.get("/api/v1/billing/rates")           # → cp-sla

    assert calls_of("cp-intake") >= before_intake + 3
    assert calls_of("cp-provision") >= before_provision + 1
    assert calls_of("cp-sla") >= before_sla + 1


def test_totals_reflect_control_plane_state(client):
    # 개통 실행 → 활성 주문·테넌트가 totals에 반영
    client.post("/api/v1/tenants", json={"name": "metrics-co"})
    s = client.get("/api/v1/integration/module-stats").json()
    assert s["totals"]["calls"] > 0
    assert s["totals"]["eps"] >= 0
    assert isinstance(s["totals"]["active_orders"], int)
    assert isinstance(s["totals"]["pipeline_events"], int)


def test_module_stats_endpoint_is_observability(client):
    # module-stats 호출 자체가 cp-obs(integration) 카운트를 올린다
    def obs_calls():
        s = client.get("/api/v1/integration/module-stats").json()
        return {m["id"]: m["calls"] for m in s["modules"]}["cp-obs"]

    a = obs_calls()
    b = obs_calls()
    assert b > a
