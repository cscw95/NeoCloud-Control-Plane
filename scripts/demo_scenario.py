#!/usr/bin/env python3
"""NeoCloud OS — BMaaS 서비스 E2E 시연 러너 (13막).

역할 기반 여정으로 전체 동작을 시연·검증한다:
  Biz 포털(계약) → 고객 포털(셀프서비스 주문·SLA·티켓) → NeoCloud OS
  (파이프라인·격리·스토리지) → NICo/설비(에뮬레이션) → 운영 포털
  (break-fix·티켓 처리·reconcile) → Biz 포털(사용량·과금) → 회수·복원.

    cd ~/nocp && ./run.sh            # 서버 기동 (별도 터미널)
    .venv/bin/python scripts/demo_scenario.py            # 자동 진행
    .venv/bin/python scripts/demo_scenario.py --pause    # 발표 모드(막마다 Enter)

종료 코드 0 = 전 검증 통과.
"""

import argparse
import sys
import time
from datetime import datetime

import httpx

G, R, Y, B, X = "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m"
PASSED = FAILED = 0
PAUSE = False


def act(n: int, title: str, screen: str = "") -> None:
    print(f"\n{B}{'━' * 74}\n ACT {n}. {title}"
          + (f"   {Y}▶ 화면: {screen}{X}" if screen else f"{X}"))
    print(f"{B}{'━' * 74}{X}")


def check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  {G}✓{X} {msg}")
    else:
        FAILED += 1
        print(f"  {R}✗ FAIL{X} {msg}")


def note(msg: str) -> None:
    print(f"  {Y}·{X} {msg}")


def pause() -> None:
    if PAUSE:
        input(f"\n  {Y}[Enter]를 누르면 다음 막으로 진행합니다…{X}")


def lead_time_s(order: dict) -> float:
    ev = {e["state"]: e["at"] for e in order["history"]}
    if "received" in ev and "delivered" in ev:
        return (datetime.fromisoformat(ev["delivered"])
                - datetime.fromisoformat(ev["received"])).total_seconds()
    return -1.0


def main() -> int:
    global PAUSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--pause", action="store_true", help="막마다 Enter 대기(발표 모드)")
    args = ap.parse_args()
    PAUSE = args.pause
    c = httpx.Client(base_url=args.base, timeout=120)

    print(f"{B}NeoCloud OS — BMaaS 서비스 E2E 시연 (STT 가산+IGIS 안산 · 13막 · 포털 여정){X}")
    print(f"대상: {args.base}")

    # ═══ ACT 0. 초기화 ═══════════════════════════════════════════════════
    act(0, "초기화 — 전체 리셋(reseed): Phase 1 2사이트 재구성", "/flow ⟲")
    rs = c.post("/api/v1/admin/reseed").json()
    check(rs["scalable_units"] == 11 and rs["gpus"] == 10080,
          f"Phase 1 시드: SU {rs['scalable_units']} · GPU {rs['gpus']:,} · 노드 {rs['nodes']:,} (전량 VR)")
    s = c.get("/api/v1/nodes/summary").json()["by_state"]
    check(s == {"pool_ready": 2520}, f"전 노드 pool_ready: {s}")
    check(c.get("/fake-vast/views").json() == [] and
          c.get("/fake-nico/segments").json() == []
          and c.get("/api/v1/tickets").json() == [], "VAST·VPC·티켓 초기화")
    pages = ("/", "/flow", "/nico", "/arch", "/ops", "/customer", "/biz")
    check(all(c.get(p).status_code == 200 for p in pages),
          f"웹 화면 {len(pages)}종 서빙 (포털 3 + 콘솔 4)")
    pause()

    # ═══ ACT 1. 인프라 검증 ══════════════════════════════════════════════
    act(1, "인프라 검증 — 2사이트(가산·안산) 4층·NICo·IB 패브릭", "/nico ①②⑥")
    inv = c.get("/api/v1/inventory/summary").json()
    check(inv["capped_power_mw"] == 26.18 and inv["racks"] == 140,
          f"전력 캡 {inv['capped_power_mw']}MW · 랙 {inv['racks']} · GPU {inv['gpus']:,}")
    tree = c.get("/api/v1/topology/tree").json()
    floors = [b["name"] for f in tree["factories"] for b in f["compute_blocks"]]
    check(len(tree["factories"]) == 2 and len(floors) == 4,
          f"사이트 2 ({' / '.join(f['name'] for f in tree['factories'])}) · 층 4: {floors}")
    site = c.get("/fake-nico/site").json()
    check(site["counts"]["hosts_by_state"]["pool_ready"] == 2520,
          f"NICo 호스트 2,520 · 서비스 {len(site['services'])}종 정상")
    check(len(site["sites"]) == 2,
          "NICo 인스턴스 2식 — " + " / ".join(x["name"] for x in site["sites"]))
    fab = c.get("/api/v1/fabric/ib").json()
    check(len(fab["sites"]) == 2
          and sum(len(s["sus"]) for s in fab["sites"]) == 11,
          "IB 패브릭: 사이트별 독립 (spine 4×2식 · SU leaf 11) — 크로스 연결 없음")
    pause()

    # ═══ ACT 2. 비즈 포털 — 계약 체결 ════════════════════════════════════
    act(2, "비즈 포털 — 고객 계약 체결(테넌트 온보딩)", "/biz 고객·계약")
    acme = c.post("/api/v1/tenants", json={
        "name": "acme-ai", "isolation_tier": "bare_metal_dedicated"}).json()
    ni = c.get(f"/api/v1/tenants/{acme['id']}").json()["network_isolation"]
    check(ni["vrf"] == "VRF-acme-ai",
          f"계약 생성 → 자동 바인딩: {ni['vrf']} · L3VNI {ni['compute_l3vni']}")
    rates = c.get("/api/v1/billing/rates").json()
    check("vr-nvl72" in rates["rates"],
          f"요율표 준비: {' · '.join(f'{k} ${v}/rack-h' for k, v in rates['rates'].items())}")
    pause()

    # ═══ ACT 3. 고객 포털 — 셀프서비스 주문 ══════════════════════════════
    act(3, "고객 포털 — 셀프서비스 개통: VR 16랙(SU 스팬)", "/customer 셀프서비스")
    t0 = time.time()
    o1 = c.post("/api/v1/orders", json={
        "tenant_id": acme["id"], "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 16}).json()
    check(o1["state"] == "delivered",
          f"{o1['id']} delivered ({time.time()-t0:.1f}s) · 노드 {len(o1['node_ids'])} · allocation {len(o1['allocation_ids'])}")
    v = c.get("/fake-vast/views").json()[0]
    check(v["capacity_tb"] == 8000 and v["qos_gbps"] == 640,
          f"VAST: {v['path']} · {v['capacity_tb']}TB · QoS {v['qos_gbps']}GB/s")
    sel = c.get("/api/v1/fabric/ib", params={"tenant_id": acme["id"]}).json()["selected"]
    check(sel["pkey"] is not None and sel["gpus"] == 1152,
          f"IB fabric: P_Key {sel['pkey']} · {sel['racks']}랙/{sel['gpus']:,}GPU")
    pause()

    # ═══ ACT 4. 플로우 감사 ══════════════════════════════════════════════
    act(4, "플로우 감사 — 단계별 하부 API 전체 리스트", "/arch 리플레이")
    flow = c.get(f"/api/v1/orders/{o1['id']}/flow").json()
    by = {st["state"]: st for st in flow["stages"]}
    total = sum(st["api_total"] for st in flow["stages"])
    note(f"전체 하부 호출 {total:,}건 — " + " · ".join(
        f"{st['state']} {st['api_total']}" for st in flow["stages"]))
    check(by["provisioning"]["by_channel"]["Redfish"] == 576,
          "BMC(Redfish) 576 · DHCP/PXE/cloud-init 각 288")
    check(by["isolating"]["by_channel"]["NVUE/HBN"] == 293
          and by["isolating"]["by_channel"]["NMX"] == 16,
          "격리: DPU HBN 293 (트레이 288 + CPU노드 5) · UFM 1 · NMX 16")
    pause()

    # ═══ ACT 5. 운영 — 에뮬레이션 ════════════════════════════════════════
    act(5, "운영 — 트레이 에뮬레이션 (training → inference)", "/nico ⑤⑦ LIVE")
    c.post("/api/v1/emu/tick?n=12")
    cl = c.get("/api/v1/emu/clusters").json()[0]
    check(cl["gpus"] == 1152 and cl["avg_util_pct"] > 50,
          f"training: util {cl['avg_util_pct']}% · {cl['power_kw']}/{cl['power_cap_kw']}kW · ≈{cl['tokens_ks']}K tok/s")
    c.post(f"/api/v1/emu/clusters/{acme['id']}/workload", json={"profile": "inference"})
    c.post("/api/v1/emu/tick?n=12")
    cl2 = c.get("/api/v1/emu/clusters").json()[0]
    check(cl2["profile"] == "inference" and cl2["power_kw"] < cl["power_kw"],
          f"inference 전환: 전력 {cl['power_kw']}→{cl2['power_kw']}kW")
    pause()

    # ═══ ACT 6. 고객 포털 — SLA 가시성 ═══════════════════════════════════
    act(6, "고객 포털 — SLA 가시성 (리드타임·가용성·격리)", "/customer SLA")
    lt = lead_time_s(o1)
    check(0 <= lt < 30, f"프로비저닝 리드타임 {lt:.1f}s (주문 이력에서 자동 산출)")
    nodes = c.get("/api/v1/nodes", params={"tenant_id": acme["id"]}).json()
    in_svc = sum(1 for n in nodes if n["state"] == "in_service")
    check(in_svc == len(nodes) == 288, f"가용성: in_service {in_svc}/{len(nodes)} (100%)")
    check(c.get(f"/api/v1/tenants/{acme['id']}/isolation").json()["ok"],
          "격리 검증 리포트 PASS — 고객 포털 SLA 패널 노출")
    pause()

    # ═══ ACT 7. 장애 주입 → saga 보상 ════════════════════════════════════
    act(7, "장애 — provision 실패 주입 → saga 자동 보상 → 재시도", "/flow ②③")
    beta = c.post("/api/v1/tenants", json={
        "name": "beta-lab", "isolation_tier": "bare_metal_dedicated"}).json()
    # M4 best-fit이 고를 랙을 미리 계산해 장애 주입 (완전-free SU 중 최소 → 첫 랙)
    su_sizes = {su["id"]: len(su["rack_ids"])
                for su in c.get("/api/v1/scalable-units").json()}
    free_by_su: dict = {}
    for n in c.get("/api/v1/nodes", params={"state": "pool_ready"}).json():
        free_by_su.setdefault("-".join(n["rack_id"].split("-")[:2]), {}) \
                  .setdefault(n["rack_id"], 0)
        free_by_su["-".join(n["rack_id"].split("-")[:2])][n["rack_id"]] += 1
    fully_free = {sid: racks for sid, racks in free_by_su.items()
                  if sum(1 for c_ in racks.values() if c_ == 18) == su_sizes[sid]}
    target_su = min(fully_free, key=lambda sid: su_sizes[sid])
    target_rack = sorted(fully_free[target_su])[0]
    c.post(f"/fake-nico/hosts/nh-{target_rack}-tray-00/inject",
           json={"op": "provision"})
    fail = c.post("/api/v1/orders", json={
        "tenant_id": beta["id"], "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 1}).json()
    check(fail["state"] == "failed", f"{fail['id']} → failed (saga 보상 완료)")
    s = c.get("/api/v1/nodes/summary").json()["by_state"]
    check(s.get("quarantined") == 1, "피해 노드 1대만 quarantine — 나머지 풀 복귀")
    retry = c.post("/api/v1/orders", json={
        "tenant_id": beta["id"], "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 1}).json()
    check(retry["state"] == "delivered", f"재시도 {retry['id']} → delivered (비정상 랙 스킵)")
    beta_alloc = retry["allocation_ids"][0]
    pause()

    # ═══ ACT 8. 티켓 — 고객 접수 → 운영 처리 ════════════════════════════
    act(8, "티켓 — 고객 포털 접수 → 운영 포털 처리(진행→해결)", "/customer + /ops 티켓 큐")
    tck = c.post("/api/v1/tickets", json={
        "tenant_id": beta["id"], "subject": "rack 프로비저닝 실패 문의",
        "severity": "high", "ref": fail["id"],
        "body": "주문이 failed로 종료됨 — 원인 확인 요청"}).json()
    check(tck["status"] == "open", f"{tck['id']} 접수 (severity=high, ref={fail['id']})")
    c.patch(f"/api/v1/tickets/{tck['id']}", json={
        "status": "in_progress", "comment": "펌웨어 불일치 노드 격리 확인 — break-fix 트랙 이관",
        "author": "operator"})
    done = c.patch(f"/api/v1/tickets/{tck['id']}", json={
        "status": "resolved",
        "comment": "재시도 주문 delivered — 격리 노드는 수리 후 재검증 예정",
        "author": "operator"}).json()
    check(done["status"] == "resolved" and len(done["comments"]) == 2,
          "운영 처리 완료: open → in_progress → resolved (코멘트 2건)")
    check(c.get("/api/v1/tickets",
                params={"tenant_id": beta["id"], "status": "resolved"}
                ).json()[0]["id"] == tck["id"],
          "고객 포털에서 해결 이력·답변 확인 가능")
    pause()

    # ═══ ACT 9. 정합성 reconcile ═════════════════════════════════════════
    act(9, "정합성 — GHOST/ORPHAN/MISMATCH 검출 → 복구", "/ops Reconcile")
    c.post("/fake-nico/hosts/ghost", json={"host_id": "nh-ghost-demo"})
    orphan = next(n["nico_host_id"] for n in
                  c.get("/api/v1/nodes", params={"state": "pool_ready"}).json()
                  if c.get(f"/fake-nico/hosts/{n['nico_host_id']}").status_code == 200)
    c.delete(f"/fake-nico/hosts/{orphan}")
    victim = c.get("/api/v1/nodes", params={"tenant_id": acme["id"]}).json()[0]
    c.patch(f"/fake-nico/hosts/{victim['nico_host_id']}/state",
            json={"state": "pool_ready"})
    rec = c.post("/api/v1/reconcile/run").json()
    check(rec["ghosts_registered"] == 1 and rec["orphans_cordoned"] == 1
          and rec["mismatches"] == 1,
          f"검출: GHOST {rec['ghosts_registered']} · ORPHAN {rec['orphans_cordoned']} · MISMATCH {rec['mismatches']}")
    c.patch(f"/fake-nico/hosts/{victim['nico_host_id']}/state",
            json={"state": "allocated"})
    rec2 = c.post("/api/v1/reconcile/run").json()
    check(rec2["mismatches"] == 0, "운영자 복구 후 MISMATCH 해소")
    pause()

    # ═══ ACT 10. 확장 ════════════════════════════════════════════════════
    act(10, "확장 — 고객 포털에서 acme +2랙 (P_Key 재사용)", "/customer + /nico ⑥")
    exp = c.post("/api/v1/orders", json={
        "tenant_id": acme["id"], "kind": "new",
        "blueprint_key": "vr-nvl72", "racks": 2}).json()
    sel2 = c.get("/api/v1/fabric/ib", params={"tenant_id": acme["id"]}).json()["selected"]
    check(exp["state"] == "delivered" and sel2["racks"] == 18
          and sel2["pkey"] == sel["pkey"],
          f"확장: {sel2['racks']}랙/{sel2['gpus']:,}GPU · P_Key {sel2['pkey']} 재사용")
    pause()

    # ═══ ACT 11. 회수 + 과금 프리뷰 ══════════════════════════════════════
    act(11, "회수(F4) + 비즈 포털 과금 — beta 해지·사용량 마감", "/biz 사용량·과금")
    term = c.post("/api/v1/orders", json={
        "tenant_id": beta["id"], "kind": "terminate",
        "allocation_id": beta_alloc}).json()
    check(term["state"] == "closed" and term["error"] is None,
          f"{term['id']} closed — sanitize 7단계·VPC/VAST 해체")
    usage = c.get("/api/v1/billing/usage").json()
    beta_line = next(l for l in usage["lines"] if l["tenant_id"] == beta["id"])
    acme_lines = [l for l in usage["lines"] if l["tenant_id"] == acme["id"]]
    check(beta_line["active"] is False and beta_line["end"] is not None,
          f"beta 과금 마감: {beta_line['rack_hours']:.3f} rack-h · ${beta_line['amount_usd']}")
    check(len(acme_lines) == 2 and all(l["active"] for l in acme_lines),
          f"acme 활성 과금 2건 · 월 환산 ${usage['totals']['projected_monthly_usd']:,.0f}")
    pause()

    # ═══ ACT 12. 최종 초기화 ═════════════════════════════════════════════
    act(12, "최종 초기화 — 시연 종료, 시작 상태 완전 복원", "/flow ⟲")
    c.post("/api/v1/admin/reseed")
    s = c.get("/api/v1/nodes/summary").json()["by_state"]
    check(s == {"pool_ready": 2520}, "노드 2,520 전량 pool_ready")
    check(c.get("/api/v1/orders").json() == []
          and c.get("/api/v1/tickets").json() == []
          and c.get("/fake-vast/views").json() == []
          and c.get("/fake-nico/segments").json() == [],
          "주문·티켓·VAST·VPC 완전 초기화 — 재시연 가능 상태")

    # ═══ 결과 ════════════════════════════════════════════════════════════
    print(f"\n{B}{'═' * 74}{X}")
    color = G if FAILED == 0 else R
    print(f" 시연 결과: {color}{PASSED} PASS · {FAILED} FAIL{X}"
          f"  (13막 BMaaS 여정: 계약→개통→운영→장애→티켓→정합성→확장→과금→복원)")
    print(f"{B}{'═' * 74}{X}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.ConnectError:
        print(f"{R}서버에 연결할 수 없습니다 — 먼저 실행: cd ~/nocp && ./run.sh{X}")
        sys.exit(2)
