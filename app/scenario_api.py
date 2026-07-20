"""NeoCloud 서비스 시나리오 북바운드 API — 고객 라이프사이클.

고객 포털(neocloud-consoles)이 소비하는 계약 표면:

  CP-004          Acceptance — 검수 리포트 제공·고객 승인/반려·간주(deemed) 승인
                  (승인 시 청구 개시 = billing_start_date 확정)
  CP-012/BP-016/  종료 워크플로우 — 백업 확인 게이트(시스템 차단) → 회수 saga
  OP-011          (drain→release→7단계 소거) → Secure Wipe 증명서 → 정산 마감
  CP-005/006/009  티켓 유형·라우팅 — app/business.py (tech/change→ops,
                  billing_dispute→biz)
  CP-011          Status Page + RCA — 컴포넌트 상태·인시던트는 /emu/faults
                  (트레이 에뮬레이터 + NICo 에뮬레이터 피드)에서 도출
  BP-006          SLA 리포트 + Service Credit — 월별 가용률·표준 크레딧 테이블
  CP-016          공개 상품 문의 (비인증)

페이싱: 종료 소거 saga는 NOCP_TERMINATION_STAGE_DELAY(초/단계)로 진행 속도를
제어한다 — 실체인(NOCP_NICO_URL) 기동 시 기본 1.5s(콘솔 라이브 폴링용),
인프로세스(pytest)는 0(완전 동기).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import lifecycle
from .business import HOURS_PER_MONTH, RACK_HOUR_RATE_USD
from .models import (
    LifecycleEvent,
    NodeLifecycleState as NS,
    OrderCreate,
    OrderKind,
    OrderState as OS,
    ServiceOrder,
)
from .nico_fake import SANITIZE_STEPS
from .store import STORE
from .trace import emit
from .tray_emu import EMULATOR, _emulator_reprov_faults

router = APIRouter(prefix="/api/v1", tags=["scenario"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_plus(ts: str, **delta) -> str:
    return (datetime.fromisoformat(ts) + timedelta(**delta)).isoformat()


# ===========================================================================
# 1) Acceptance (CP-004) — 고객 승인/반려/간주(deemed)
# ===========================================================================
ACCEPTANCE_DEEMED_DAYS = 7      # 리포트 제공 후 무응답 시 간주 승인 기한


def _deemed_days() -> float:
    """간주 승인 기한(일) — NOCP_ACCEPTANCE_DEEMED_DAYS로 재정의 (데모 페이싱).

    계약 기본값 7일. pytest/실서버 모두 env 미설정 시 7일 유지."""
    raw = os.environ.get("NOCP_ACCEPTANCE_DEEMED_DAYS")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return float(ACCEPTANCE_DEEMED_DAYS)


class AcceptanceDecision(BaseModel):
    decision: str               # approve | reject
    reason: Optional[str] = None


def _build_acceptance_report(order: ServiceOrder, report_ts: str) -> dict:
    """검수 리포트 — 기존 acceptance 게이트(격리 검증)·burn-in 데이터 합성."""
    n = len(order.node_ids)
    gbps = sum(sa.qos_gbps for sa in STORE.storage_allocs.values()
               if sa.order_id == order.id)
    return {
        "checks": [
            {"name": "nccl-allreduce", "status": "pass",
             "detail": f"NCCL all-reduce busbw — {order.racks}랙 {n}노드 "
                       "스모크 잡 기준치 통과",
             "value": "742 GB/s"},
            {"name": "fio-storage", "status": "pass",
             "detail": "VAST NFSoRDMA fio 4M seq-read — 계약 QoS 성능 충족",
             "value": f"{gbps or 40} GB/s"},
            {"name": "burn-in", "status": "pass",
             "detail": "dcgm diag -r 3 + 24h burn-in — 전 노드 PASS",
             "value": f"{n}/{n}"},
            {"name": "node-ready", "status": "pass",
             "detail": "전 노드 프로비저닝 완료 · 3-plane 격리 검증"
                       "(cross-vrf/ib-pkey/nvlink) 통과",
             "value": f"{n}/{n}"},
        ],
        "nodes_tested": n,
        "report_ts": report_ts,
    }


def _ensure_acceptance_locked(order: ServiceOrder) -> Optional[dict]:
    """acceptance 게이트 도달 시 검수 레코드 생성(1회). 전제: STORE.lock 보유.

    반환 None = 아직 acceptance 단계 이전(리포트 미제공)."""
    if order.kind != OrderKind.new:
        raise HTTPException(409, "acceptance는 신규 개통 주문 전용")
    rec = STORE.acceptances.get(order.id)
    if rec:
        return rec
    acc_ev = next((e for e in order.history
                   if e.state == OS.acceptance.value), None)
    if acc_ev is None:
        return None
    report_ts = acc_ev.at
    rec = {
        "order_id": order.id,
        "tenant_id": order.tenant_id,
        "status": "pending",            # pending | approved | rejected | deemed
        "report": _build_acceptance_report(order, report_ts),
        "deemed_deadline": _iso_plus(report_ts, days=_deemed_days()),
        "billing_start_date": None,     # 승인(또는 deemed) 후 확정
        "reject_reason": None,
        "decided_at": None,
        "decided_by": None,
    }
    STORE.acceptances[order.id] = rec
    return rec


def _acceptance_view(order_id: str, rec: Optional[dict]) -> dict:
    if rec is None:
        return {"order_id": order_id, "status": "pending", "report": None,
                "deemed_deadline": None, "billing_start_date": None}
    return {"order_id": order_id, "status": rec["status"],
            "report": rec["report"],
            "deemed_deadline": rec["deemed_deadline"],
            "billing_start_date": rec["billing_start_date"],
            "reject_reason": rec["reject_reason"]}


def _finalize_delivery(order: ServiceOrder, adapter) -> None:
    """acceptance 게이트에서 대기/반려 중인 주문을 delivered로 진행.

    전제: STORE.lock 미보유 (하위 stage 함수가 자체적으로 락을 잡는다).
    approval_mode 주문은 ops 승인 큐의 남은 게이트를 연속 실행한다 —
    고객/운영자 중 먼저 승인한 쪽이 유효."""
    if order.state == OS.delivered:
        return
    if order.approval_mode and order.pending_stage:
        while (order.pending_stage
               and order.state not in (OS.delivered, OS.failed, OS.rejected)):
            lifecycle.approve_next_stage(order.id, adapter)
        return
    if order.state == OS.acceptance:
        # 재테스트 루프(고객 반려 후) 재승인 — 노드는 이미 in_service일 수
        # 있으므로 상태 보존 전이로 delivered 복귀
        with STORE.lock:
            for nid in order.node_ids:
                node = STORE.node_instances.get(nid)
                if node and node.state != NS.in_service:
                    lifecycle.advance_node(node, NS.in_service,
                                           "customer acceptance passed")
            if not order.access_package:
                lifecycle._build_access_package(order)
            lifecycle.advance_order(order, OS.delivered,
                                    "고객 검수 승인 — delivered")


def _maybe_deem(order: ServiceOrder, adapter) -> None:
    """리포트 제공 후 +7일 무응답 시 간주(deemed) 승인 — GET 호출 시 판정."""
    with STORE.lock:
        rec = STORE.acceptances.get(order.id)
        if not rec or rec["status"] not in ("pending", "rejected"):
            return
        if order.state not in (OS.acceptance, OS.k8s_installing, OS.delivered):
            return                     # failed/reclaimed 주문은 간주 승인 제외
        deadline = rec["deemed_deadline"]
        if datetime.now(timezone.utc) < datetime.fromisoformat(deadline):
            return
    _finalize_delivery(order, adapter)
    with STORE.lock:
        now = _now()
        rec.update(status="deemed", billing_start_date=deadline,
                   decided_at=now, decided_by="deemed")
        order.history.append(LifecycleEvent(
            state=order.state.value,
            detail=f"AcceptanceDeemed — 간주 승인(기한 {deadline} 경과) · "
                   "청구 개시", at=now))
        emit("NeoCloudOS.M1", "BizPortal", "internal",
             f"AcceptanceDeemed → {order.id}",
             "검수 기한(+7일) 경과 — 간주 승인 처리, billing_start_date="
             "deadline 기준 청구 개시",
             payload={"billing_start_date": deadline}, order_id=order.id)


@router.get("/orders/{order_id}/acceptance-report")
def acceptance_report(order_id: str) -> dict:
    adapter = lifecycle.get_adapter()
    with STORE.lock:
        order = STORE.orders.get(order_id)
        if not order:
            raise HTTPException(404, f"order '{order_id}' not found")
        rec = _ensure_acceptance_locked(order)
    if rec is not None:
        _maybe_deem(order, adapter)
    with STORE.lock:
        return _acceptance_view(order_id, STORE.acceptances.get(order_id))


@router.post("/orders/{order_id}/acceptance")
def decide_acceptance(order_id: str, body: AcceptanceDecision) -> dict:
    adapter = lifecycle.get_adapter()
    with STORE.lock:
        order = STORE.orders.get(order_id)
        if not order:
            raise HTTPException(404, f"order '{order_id}' not found")
        if body.decision not in ("approve", "reject"):
            raise HTTPException(422, "decision must be 'approve' or 'reject'")
        if body.decision == "reject" and not (body.reason or "").strip():
            raise HTTPException(422, "reject 시 reason 필수")
        if order.state in (OS.failed, OS.rejected, OS.compensating,
                           OS.reclaiming, OS.closed):
            raise HTTPException(409, f"주문 상태 {order.state.value} — "
                                     "검수 결정 불가")
        rec = _ensure_acceptance_locked(order)
        if rec is None:
            raise HTTPException(409, "acceptance 단계 이전 — 검수 리포트가 "
                                     "아직 제공되지 않은 주문")
        if rec["status"] in ("approved", "deemed"):
            raise HTTPException(409, f"이미 {rec['status']} 처리된 주문 — "
                                     "검수 재실행 불가")

    if body.decision == "approve":
        _finalize_delivery(order, adapter)     # ops 큐와 공존 — 선승인 유효
        with STORE.lock:
            now = _now()
            rec.update(status="approved", billing_start_date=now,
                       reject_reason=None, decided_at=now,
                       decided_by="customer")
            order.history.append(LifecycleEvent(
                state=order.state.value,
                detail="AcceptanceApproved — 고객 검수 승인 · 청구 개시",
                at=now))
            emit("CustomerPortal", "NeoCloudOS.M1", "REST",
                 f"POST /orders/{order_id}/acceptance → approve",
                 "AcceptanceApproved=청구 개시 — billing_start_date 확정",
                 payload={"decision": "approve", "billing_start_date": now},
                 order_id=order_id)
            return _acceptance_view(order_id, rec)

    # -- reject: 재테스트 루프 (acceptance 재실행 상태, 청구 미개시) ----------
    with STORE.lock:
        now = _now()
        if order.state == OS.delivered:
            order.state = OS.acceptance        # 고객 반려 전용 역전이(재테스트)
        order.history.append(LifecycleEvent(
            state=OS.acceptance.value,
            detail=f"AcceptanceRejected — 고객 반려, acceptance 재실행: "
                   f"{body.reason}", at=now))
        report_ts = _now()                     # 재테스트 리포트 재생성
        rec.update(status="rejected", reject_reason=body.reason,
                   billing_start_date=None, decided_at=now,
                   decided_by="customer",
                   report=_build_acceptance_report(order, report_ts),
                   deemed_deadline=_iso_plus(report_ts,
                                             days=_deemed_days()))
        emit("CustomerPortal", "NeoCloudOS.M1", "REST",
             f"POST /orders/{order_id}/acceptance → reject",
             f"고객 반려 — 재테스트 루프 진입 (사유: {body.reason}) · "
             "청구 미개시",
             payload={"decision": "reject", "reason": body.reason},
             order_id=order_id)
        return _acceptance_view(order_id, rec)


# ===========================================================================
# 2) 종료 워크플로우 (CP-012/BP-016/OP-011)
# ===========================================================================
TERMINATION_CHECKLIST = [
    "테넌트 데이터 추출(export) 완료",
    "외부 스토리지 이관(migration) 완료",
    "최종 백업본 확인 완료",
]
ERASE_METHOD = "NVMe crypto-erase + GPU/DDR wipe + TPM reset"


class BackupConfirmBody(BaseModel):
    checklist: list[dict]           # [{item, confirmed}] — 전부 true 필요


def _termination_stage_delay() -> float:
    """종료 소거 saga 페이싱(단계당 초) — NOCP_TERMINATION_STAGE_DELAY."""
    raw = os.environ.get("NOCP_TERMINATION_STAGE_DELAY")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 0.0
    return 1.5 if os.environ.get("NOCP_NICO_URL") else 0.0


def _latest_termination_locked(tenant_id: str) -> Optional[dict]:
    recs = [t for t in STORE.terminations.values()
            if t["tenant_id"] == tenant_id]
    return max(recs, key=lambda t: t["requested_at"]) if recs else None


@router.post("/tenants/{tenant_id}/termination", status_code=201)
def request_termination(tenant_id: str) -> dict:
    with STORE.lock:
        if tenant_id not in STORE.tenants:
            raise HTTPException(404, f"tenant '{tenant_id}' not found")
        active = _latest_termination_locked(tenant_id)
        if active and active["state"] in ("awaiting_backup", "erasing"):
            raise HTTPException(409, f"이미 진행 중인 종료 워크플로우 "
                                     f"({active['termination_id']} · "
                                     f"{active['state']})")
        term_id = f"term-{STORE.next_seq('termination')}"
        now = _now()
        rec = {
            "termination_id": term_id,
            "tenant_id": tenant_id,
            "state": "awaiting_backup",   # awaiting_backup|erasing|wiped|closed
            "requested_at": now,
            "checklist": [{"item": i, "confirmed": False}
                          for i in TERMINATION_CHECKLIST],
            "backup_confirmed_at": None,
            "progress": {"stage": "awaiting_backup", "pct": 0},
            "allocations": [a.id for a in STORE.allocations.values()
                            if a.tenant_id == tenant_id],
            "orders": [],
            "wipe_certificate": None,
            "closed_at": None,
        }
        STORE.terminations[term_id] = rec
        emit("CustomerPortal", "NeoCloudOS.M1", "REST",
             f"POST /tenants/{tenant_id}/termination → {term_id}",
             "서비스 종료 요청 접수 — 데이터 백업 확인 대기 (백업 확인 전 "
             "종료 진행 시스템 차단)",
             payload={"termination_id": term_id,
                      "allocations": rec["allocations"],
                      "checklist": TERMINATION_CHECKLIST})
        return {"termination_id": term_id, "state": rec["state"],
                "requested_at": now, "checklist": rec["checklist"]}


def _term_progress(rec: dict, stage: str, done_units: int,
                   total_units: int) -> None:
    rec["progress"] = {"stage": stage,
                       "pct": (round(done_units / total_units * 100)
                               if total_units else 100)}


def _run_termination_saga(rec: dict, adapter, delay: float) -> None:
    """회수 saga — 할당별 drain→release→7단계 소거→풀 복귀. 완료 시 wiped +
    Secure Wipe 증명서 발급. delay>0이면 단계별 sleep(콘솔 폴링용)."""
    tenant_id = rec["tenant_id"]
    with STORE.lock:
        alloc_ids = [a.id for a in STORE.allocations.values()
                     if a.tenant_id == tenant_id]
        rec["allocations"] = alloc_ids
    stage_names = (["drain", "release"]
                   + [f"erase-{i}/7 {s}"
                      for i, s in enumerate(SANITIZE_STEPS, start=1)]
                   + ["pool-return"])
    total = max(1, len(alloc_ids)) * len(stage_names)
    done = 0
    cert_allocs: list[dict] = []

    for aid in alloc_ids:
        with STORE.lock:
            alloc = STORE.allocations.get(aid)
            rack_ids = list(alloc.rack_ids) if alloc else []
            node_cnt = sum(len(STORE.racks[r].tray_ids)
                           for r in rack_ids if r in STORE.racks)
        # 단계별 진행 표시(페이싱) — 실제 상태 전이는 기존 회수 saga
        # (run_terminate_order)가 수행하며 노드 이력·NICo sanitizer 트레이스에
        # 동일 7단계가 기록된다.
        for st in stage_names[:-1]:
            _term_progress(rec, f"{aid}: {st}", done, total)
            emit("NeoCloudOS.M1", "NICo.APIService", "internal",
                 f"termination:{st}",
                 f"{rec['termination_id']} — allocation {aid} {st}",
                 payload={"termination_id": rec["termination_id"],
                          "allocation_id": aid, "stage": st})
            if delay > 0:
                time.sleep(delay)
            done += 1
        term_order = lifecycle.run_terminate_order(OrderCreate(
            tenant_id=tenant_id, kind=OrderKind.terminate,
            allocation_id=aid), adapter)
        rec["orders"].append(term_order.id)
        _term_progress(rec, f"{aid}: pool-return", done, total)
        done += 1
        cert_allocs.append({
            "allocation_id": aid, "racks": rack_ids, "nodes": node_cnt,
            "terminate_order_id": term_order.id,
            "reclaim_state": term_order.state.value,
            "erase_steps": [{"step": s, "ok": True} for s in SANITIZE_STEPS],
        })

    with STORE.lock:
        now = _now()
        cert_body = {
            "tenant_id": tenant_id,
            "termination_id": rec["termination_id"],
            "erase_method": ERASE_METHOD,
            "erase_steps": list(SANITIZE_STEPS),
            "allocations": cert_allocs,
            "issued_at": now,
        }
        sha = hashlib.sha256(
            json.dumps(cert_body, sort_keys=True).encode()).hexdigest()
        rec["wipe_certificate"] = {
            "cert_id": f"wipe-{STORE.next_seq('wipe_cert')}",
            "issued_at": now, "sha256": sha, **cert_body}
        rec["state"] = "wiped"
        _term_progress(rec, "wiped", total, total)
        emit("NeoCloudOS.M1", "CustomerPortal", "internal",
             f"termination:wiped → {rec['termination_id']}",
             f"전체 자원 소거 완료 — Secure Wipe 증명서 "
             f"{rec['wipe_certificate']['cert_id']} 발급 "
             f"({len(cert_allocs)} allocation · {ERASE_METHOD})",
             payload={"cert_id": rec["wipe_certificate"]["cert_id"],
                      "sha256": sha})


@router.post("/tenants/{tenant_id}/termination/backup-confirm")
def confirm_backup(tenant_id: str, body: BackupConfirmBody) -> dict:
    adapter = lifecycle.get_adapter()
    with STORE.lock:
        rec = _latest_termination_locked(tenant_id)
        if not rec:
            raise HTTPException(404, f"tenant '{tenant_id}'의 종료 워크플로우 "
                                     "없음")
        if rec["state"] != "awaiting_backup":
            raise HTTPException(409, f"백업 확인 단계가 아님 (state="
                                     f"{rec['state']})")
        confirmed = {c.get("item"): bool(c.get("confirmed"))
                     for c in body.checklist}
        missing = [c["item"] for c in rec["checklist"]
                   if not confirmed.get(c["item"])]
        if missing:
            raise HTTPException(409, "백업 확인 전 종료 진행 불가 — 미확인 "
                                     f"항목: {missing}")
        for c in rec["checklist"]:
            c["confirmed"] = True
        rec["backup_confirmed_at"] = _now()
        rec["state"] = "erasing"
        _term_progress(rec, "erasing", 0, 1)
        emit("CustomerPortal", "NeoCloudOS.M1", "REST",
             f"POST /tenants/{tenant_id}/termination/backup-confirm",
             f"{rec['termination_id']} — 백업 3항목 확인 완료, 회수 saga "
             "개시 (drain→release→7단계 소거→풀 복귀)",
             payload={"termination_id": rec["termination_id"]})

    delay = _termination_stage_delay()
    if delay <= 0:                       # pytest/인프로세스 — 완전 동기
        _run_termination_saga(rec, adapter, 0.0)
    else:
        threading.Thread(target=_run_termination_saga,
                         args=(rec, adapter, delay),
                         name=f"termination-{rec['termination_id']}",
                         daemon=True).start()
    return {"termination_id": rec["termination_id"], "state": rec["state"],
            "progress": rec["progress"]}


@router.get("/tenants/{tenant_id}/termination")
def get_termination(tenant_id: str) -> dict:
    with STORE.lock:
        rec = _latest_termination_locked(tenant_id)
        if not rec:
            raise HTTPException(404, f"tenant '{tenant_id}'의 종료 워크플로우 "
                                     "없음")
        out = {"termination_id": rec["termination_id"],
               "tenant_id": tenant_id,
               "state": rec["state"], "requested_at": rec["requested_at"],
               "checklist": rec["checklist"], "progress": rec["progress"]}
        if rec["wipe_certificate"]:
            out["wipe_certificate"] = rec["wipe_certificate"]
        return out


@router.get("/tenants/{tenant_id}/termination/wipe-certificate")
def get_wipe_certificate(tenant_id: str) -> dict:
    with STORE.lock:
        rec = _latest_termination_locked(tenant_id)
        if not rec:
            raise HTTPException(404, f"tenant '{tenant_id}'의 종료 워크플로우 "
                                     "없음")
        if rec["state"] not in ("wiped", "closed"):
            raise HTTPException(409, f"소거 미완료 (state={rec['state']}) — "
                                     "증명서는 wiped 이후 수령 가능")
        cert = rec["wipe_certificate"]
        if rec["state"] == "wiped":      # 수령 → 정산 마감(closed) 전환
            rec["state"] = "closed"
            rec["closed_at"] = _now()
            _term_progress(rec, "closed", 1, 1)
            emit("NeoCloudOS.M1", "BizPortal", "internal",
                 f"termination:closed → {rec['termination_id']}",
                 "Secure Wipe 증명서 수령 확인 — 계약 종료·정산 마감 처리",
                 payload={"cert_id": cert["cert_id"]})
        return {"termination_id": rec["termination_id"],
                "state": rec["state"], "wipe_certificate": cert}


# ===========================================================================
# 4) Status Page + RCA (CP-011)
# ===========================================================================
STATUS_COMPONENTS = ["Compute", "GPU Fabric", "Storage", "Network", "Portal"]
_KIND_COMPONENT = {"gpu": "Compute", "reprovision": "Compute",
                   "cooling": "Compute", "fabric": "GPU Fabric",
                   "storage": "Storage", "network": "Network"}


def _fault_severity(f: dict) -> str:
    if f.get("severity") in ("critical", "P0"):
        return "P0"
    xid = f.get("xid")
    if (isinstance(xid, int) and xid in (79, 48)) \
            or f.get("severity") in ("major", "P1"):
        return "P1"
    return "P2"


def _fault_component(f: dict) -> str:
    return _KIND_COMPONENT.get(f.get("kind") or "gpu", "Compute")


def _sync_incidents_locked() -> None:
    """/emu/faults(트레이 XID) + NICo 에뮬레이터 장애 피드 → 인시던트
    read-model 동기화. resolved P0/P1 인시던트는 RCA 1건 자동 생성."""
    feed = EMULATOR.faults(limit=500)["recent"]
    feed = feed + _emulator_reprov_faults(100)
    by_key = {i["key"]: i for i in STORE.incidents.values() if "key" in i}
    for f in feed:
        started = f.get("started_at") or f.get("at") or _now()
        key = f"{f.get('tray_id')}|{f.get('gpu')}|{started}"
        inc = by_key.get(key)
        if inc is None:
            iid = f"inc-{STORE.next_seq('incident')}"
            xid = f.get("xid")
            title = (f"XID {xid} — GPU 장애 ({f.get('tray_id')})"
                     if isinstance(xid, int)
                     else (f.get("detail")
                           or f"{f.get('kind', 'infra')} 장애 "
                              f"({f.get('tray_id')})"))
            inc = {"id": iid, "key": key,
                   "severity": _fault_severity(f),
                   "component": _fault_component(f),
                   "title": title, "state": "investigating",
                   "started_at": started, "resolved_at": None,
                   "tenant_id": f.get("tenant_id"),
                   "mttr_min": None,
                   "updates": [{"ts": started,
                                "msg": "장애 감지 — NVSentinel 조사 개시 "
                                       "(cordon/drain 후보 지정)"}]}
            STORE.incidents[iid] = inc
            by_key[key] = inc
        resolved = bool(f.get("resolved")) or f.get("resolved_at")
        if resolved and inc["state"] != "resolved":
            resolved_at = f.get("resolved_at") or _now()
            ttr_s = f.get("ttr_s")
            mttr_min = (round(ttr_s / 60, 2) if ttr_s is not None else None)
            if mttr_min is None:
                try:
                    mttr_min = round((datetime.fromisoformat(resolved_at)
                                      - datetime.fromisoformat(started))
                                     .total_seconds() / 60, 2)
                except ValueError:
                    mttr_min = 0.0
            inc.update(state="resolved", resolved_at=resolved_at,
                       mttr_min=mttr_min)
            inc["updates"].append({"ts": resolved_at,
                                   "msg": "복구 완료 — 서비스 정상화 확인"
                                          " (resolved)"})
            if inc["severity"] in ("P0", "P1"):
                _publish_rca_locked(inc)


def _publish_rca_locked(inc: dict) -> None:
    """Major(P0/P1) resolved 인시던트 → RCA 자동 발행 (incident당 1건)."""
    if any(r["incident_id"] == inc["id"] for r in STORE.rcas.values()):
        return
    rca_id = f"rca-{STORE.next_seq('rca')}"
    STORE.rcas[rca_id] = {
        "rca_id": rca_id, "incident_id": inc["id"],
        "tenant_id": inc.get("tenant_id"),
        "severity": inc["severity"],
        "title": f"RCA — {inc['title']}",
        "published_at": _now(),
        "summary": f"{inc['component']} 컴포넌트 {inc['severity']} 인시던트 "
                   f"— 감지 후 {inc['mttr_min']}분 내 복구. NVSentinel "
                   "자동 격리(cordon/drain) 경로가 정상 작동함.",
        "root_cause": inc["title"] + " — GPU/트레이 하드웨어 이상 "
                      "(DCGM 헬스 이벤트 기준)",
        "corrective_actions": [
            "해당 트레이 burn-in 재검증 후 풀 복귀 (quarantine 게이트)",
            "동일 XID 재발 시 hot-spare 전환 자동화 적용",
            "NVSentinel 감지 임계값·cordon 정책 리뷰",
        ],
        "mttr_min": inc["mttr_min"],
    }


@router.get("/status")
def status_page() -> dict:
    """공개 Status Page (인증 무관) — 컴포넌트 상태 + 인시던트 + 90일 가용률."""
    with STORE.lock:
        _sync_incidents_locked()
        open_sev: dict[str, list[str]] = {}
        for inc in STORE.incidents.values():
            if inc["state"] != "resolved":
                open_sev.setdefault(inc["component"], []).append(
                    inc["severity"])
        components = []
        for name in STATUS_COMPONENTS:
            sevs = open_sev.get(name, [])
            state = ("outage" if "P0" in sevs
                     else "degraded" if sevs else "operational")
            components.append({"name": name, "state": state})
        incidents = sorted(STORE.incidents.values(),
                           key=lambda i: i["started_at"], reverse=True)[:20]
        downtime_min = sum(
            (i["mttr_min"] or 0) if i["state"] == "resolved"
            else max(0.0, (datetime.now(timezone.utc)
                           - datetime.fromisoformat(i["started_at"]))
                     .total_seconds() / 60)
            for i in STORE.incidents.values())
        window_min = 90 * 24 * 60
        return {
            "generated_at": _now(),
            "components": components,
            "incidents": [{"id": i["id"], "severity": i["severity"],
                           "title": i["title"], "state": i["state"],
                           "started_at": i["started_at"],
                           "updates": i["updates"]} for i in incidents],
            "history_90d": {"uptime_pct": round(
                max(0.0, 100 - downtime_min / window_min * 100), 3)},
        }


@router.get("/tenants/{tenant_id}/rca-reports")
def rca_reports(tenant_id: str) -> list:
    """Major(P0/P1) resolved 인시던트의 RCA — 테넌트 귀속 + 사이트 공통."""
    with STORE.lock:
        if tenant_id not in STORE.tenants:
            raise HTTPException(404, f"tenant '{tenant_id}' not found")
        _sync_incidents_locked()
        out = [{"rca_id": r["rca_id"], "incident_id": r["incident_id"],
                "title": r["title"], "published_at": r["published_at"],
                "summary": r["summary"], "root_cause": r["root_cause"],
                "corrective_actions": r["corrective_actions"],
                "mttr_min": r["mttr_min"]}
               for r in STORE.rcas.values()
               if r.get("tenant_id") in (tenant_id, None)]
        return sorted(out, key=lambda r: r["published_at"], reverse=True)


# ===========================================================================
# 5) SLA 리포트 + Service Credit (BP-006)
# ===========================================================================
SLA_TARGET_PCT = 99.9
# 표준 Service Credit 테이블 — 월 가용률 기준 (심한 위반 우선 매칭)
SLA_CREDIT_TABLE = [
    {"below_pct": 95.0, "credit_pct": 50},
    {"below_pct": 99.0, "credit_pct": 25},
    {"below_pct": SLA_TARGET_PCT, "credit_pct": 10},
]


def _credit_pct_for(avail: float) -> int:
    for row in SLA_CREDIT_TABLE:
        if avail < row["below_pct"]:
            return row["credit_pct"]
    return 0


def _tenant_monthly_usd_locked(tenant_id: str) -> float:
    total = 0.0
    for o in STORE.orders.values():
        if (o.tenant_id == tenant_id and o.kind == OrderKind.new
                and o.state == OS.delivered):
            rate = RACK_HOUR_RATE_USD.get(o.blueprint_key or "", 600.0)
            total += o.racks * rate * HOURS_PER_MONTH
    return round(total, 2)


@router.get("/tenants/{tenant_id}/sla-report")
def sla_report(tenant_id: str, month: Optional[str] = None,
               synthesize_violation: bool = False) -> dict:
    """월별 SLA 리포트 — fault 이력(인시던트) 연동 가용률·크레딧 산정.

    synthesize_violation=1 이면 데모용 합성 P1 장애(52분 다운타임)를 해당
    월에 1건 추가해 위반→크레딧 산정 경로를 실증한다."""
    with STORE.lock:
        if tenant_id not in STORE.tenants:
            raise HTTPException(404, f"tenant '{tenant_id}' not found")
        month = month or _now()[:7]
        try:
            month_dt = datetime.strptime(month, "%Y-%m")
        except ValueError:
            raise HTTPException(422, "month must be 'YYYY-MM'")
        _sync_incidents_locked()

        if synthesize_violation:
            syn_key = f"synthetic|{tenant_id}|{month}"
            if not any(i.get("key") == syn_key
                       for i in STORE.incidents.values()):
                iid = f"inc-{STORE.next_seq('incident')}"
                started = month_dt.replace(
                    day=15, hour=3, tzinfo=timezone.utc).isoformat()
                STORE.incidents[iid] = {
                    "id": iid, "key": syn_key, "severity": "P1",
                    "component": "Compute",
                    "title": "GPU Fabric 부분 장애 — 합성 데모 인시던트"
                             " (synthetic)",
                    "state": "resolved", "started_at": started,
                    "resolved_at": _iso_plus(started, minutes=52.6),
                    "tenant_id": tenant_id, "mttr_min": 52.6,
                    "updates": [
                        {"ts": started, "msg": "장애 감지 (synthetic)"},
                        {"ts": _iso_plus(started, minutes=52.6),
                         "msg": "복구 완료 (synthetic)"}]}
                _publish_rca_locked(STORE.incidents[iid])

        month_incs = [i for i in STORE.incidents.values()
                      if i.get("tenant_id") == tenant_id
                      and i["started_at"][:7] == month]
        inc_lines = []
        downtime_total = 0.0
        for i in month_incs:
            dt_min = (i["mttr_min"] or 0.0) if i["state"] == "resolved" else \
                max(0.0, (datetime.now(timezone.utc)
                          - datetime.fromisoformat(i["started_at"]))
                    .total_seconds() / 60)
            downtime_total += dt_min
            inc_lines.append({"id": i["id"], "severity": i["severity"],
                              "downtime_min": round(dt_min, 2),
                              "mttr_min": i["mttr_min"]})
        # 해당 월의 총 분 — 데모 단순화: 월 전체를 서비스 구간으로 본다
        next_month = (month_dt.replace(year=month_dt.year + 1, month=1)
                      if month_dt.month == 12
                      else month_dt.replace(month=month_dt.month + 1))
        window_min = (next_month - month_dt).total_seconds() / 60
        avail = round(max(0.0, 100 - downtime_total / window_min * 100), 4)
        violated = avail < SLA_TARGET_PCT

        credits: list[dict] = []
        if violated:
            ckey = f"{tenant_id}|{month}"
            credit = next((c for c in STORE.sla_credits.values()
                           if c["key"] == ckey), None)
            if credit is None:
                pct = _credit_pct_for(avail)
                monthly = _tenant_monthly_usd_locked(tenant_id)
                worst = max(month_incs,
                            key=lambda i: i["mttr_min"] or 0, default=None)
                credit = {
                    "credit_id": f"crd-{STORE.next_seq('sla_credit')}",
                    "key": ckey, "tenant_id": tenant_id, "month": month,
                    "incident_id": worst["id"] if worst else None,
                    "amount_usd": round(monthly * pct / 100, 2),
                    "pct_of_monthly": pct,
                    "status": "calculated",   # calculated|approved|applied
                    "applied_invoice": None,
                }
                STORE.sla_credits[credit["credit_id"]] = credit
                emit("NeoCloudOS.M7", "BizPortal", "internal",
                     f"service-credit:calculated → {credit['credit_id']}",
                     f"{tenant_id} {month} 가용률 {avail}% < "
                     f"{SLA_TARGET_PCT}% — 크레딧 {pct}% "
                     f"(${credit['amount_usd']}) 산정",
                     payload={k: v for k, v in credit.items() if k != "key"})
            credits.append({k: v for k, v in credit.items() if k != "key"})

        return {
            "tenant_id": tenant_id, "month": month,
            "availability_pct": avail, "target_pct": SLA_TARGET_PCT,
            "violated": violated,
            "incidents": inc_lines,
            "credits": credits,
            "credit_table": SLA_CREDIT_TABLE,
            "monthly_amount_usd": _tenant_monthly_usd_locked(tenant_id),
        }


# ===========================================================================
# 6) 공개 상품 문의 (CP-016) — 비인증
# ===========================================================================
class InquiryBody(BaseModel):
    company: str
    name: str
    email: str
    gpu_scale: Union[str, int] = ""
    message: str = ""


@router.post("/public/inquiries", status_code=201)
def create_inquiry(body: InquiryBody) -> dict:
    if "@" not in body.email:
        raise HTTPException(422, "invalid email")
    if not body.company.strip() or not body.name.strip():
        raise HTTPException(422, "company/name 필수")
    with STORE.lock:
        iid = f"inq-{STORE.next_seq('inquiry')}"
        rec = {"inquiry_id": iid, "company": body.company,
               "name": body.name, "email": body.email,
               "gpu_scale": str(body.gpu_scale), "message": body.message,
               "status": "received",
               "assigned": "영업 담당 배정 예정(자동 메일 발송)",
               "received_at": _now()}
        STORE.inquiries[iid] = rec
        emit("PublicWeb", "NeoCloudOS.M1", "REST",
             f"POST /public/inquiries → {iid}",
             f"공개 상품 문의 접수 — {body.company} ({body.gpu_scale} GPU "
             "규모) · 영업 배정 큐 등록, 자동 회신 메일 발송",
             payload={"inquiry_id": iid, "company": body.company,
                      "gpu_scale": str(body.gpu_scale)})
        return {"inquiry_id": iid, "status": "received",
                "assigned": rec["assigned"]}


@router.get("/public/inquiries")
def list_inquiries() -> list:
    """사업 콘솔용 문의 목록 (데모 — 인증 게이트는 콘솔 계층에서)."""
    with STORE.lock:
        return sorted(STORE.inquiries.values(),
                      key=lambda i: i["received_at"], reverse=True)
