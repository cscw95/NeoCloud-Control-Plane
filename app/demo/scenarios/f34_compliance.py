"""F34 demo — ISMS-P control mapping with live evidence, and an audit log
export whose SHA-256 hash chain an auditor can recompute.

Deterministic: the steps write their own audit records over HTTP (IAM token
request, PAM session) before reading, and the export filters on today's date, so the
verdicts do not depend on what else ran on the stack. ISMS-P ids/names are
the public KISA criteria (공개 문서 기준); NeoCloud extension schemas live in
ref/upstream/neocloud-ext/{compliance-controls,audit-export}.schema.json.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ._kcc_local import body, check, http


def _today(ctx=None):
    d = datetime.now(timezone.utc).date().isoformat()
    return f"{d}..{d}"


def _controls(res, ctx):
    b = body(res)
    s = b["summary"]
    ids = {c["id"]: c for c in b["controls"]}
    log = ids["2.9.4"]
    return check(log["status"] == "implemented" and s["total"] >= 20,
                 f"ISMS-P 통제 {s['total']}개 매핑: 이행 {s['implemented']} · 부분 "
                 f"{s['partial']} · 계획 {s['planned']} (이행률 {s['coverage_pct']}%) · "
                 f"2.9.4 로그 및 접속기록 관리 = {log['status']} (감사 기록 "
                 f"{log['evidence'][0].get('count')}건)",
                 f"{s['total']} ISMS-P controls: {s['implemented']} implemented, "
                 f"{s['partial']} partial, {s['planned']} planned ({s['coverage_pct']}%)")


def _verify(ctx):
    from ...compliance import verify_chain
    ok = verify_chain(ctx["entries"])
    bad = [dict(e) for e in ctx["entries"]]
    bad[0]["target"] = "tampered"
    return {"chain_valid": ok, "tampered_detected": not verify_chain(bad),
            "entries": len(ctx["entries"])}


def _verified(res, ctx):
    b = body(res)
    return check(b["chain_valid"] and b["tampered_detected"],
                 f"해시 체인 재계산 일치 ({b['entries']}건), 1건 변조 시 불일치 검출",
                 f"chain recomputes ({b['entries']} records); a tampered record is detected")


def _csv(res, ctx):
    text = res.body if isinstance(res.body, str) else ""
    head = text.splitlines()[0] if text else ""
    return check(res.status == 200 and head.startswith("seq,at,actor,action"),
                 f"CSV 내보내기 헤더: {head}", f"CSV export header: {head}")


SCENARIO = {
    "id": "f34-compliance-evidence",
    "order": 34,
    "feature": "F34",
    "title_ko": "F34 ISMS-P 통제 매핑 → 감사 로그 내보내기(해시 체인 검증)",
    "title_en": "F34 ISMS-P control mapping → audit log export (hash-chain check)",
    "summary_ko": "NeoCloud 기능을 ISMS-P 통제에 매핑해 실시간 증적으로 이행 상태를 보이고, "
                  "감사 로그를 JSON/CSV로 내보내 SHA-256 해시 체인으로 변조 여부를 확인한다.",
    "summary_en": "Map NeoCloud to ISMS-P controls with live evidence, export the audit "
                  "log as JSON/CSV and check it with a SHA-256 hash chain.",
    "steps": [
        {"id": "seed-iam", "title_ko": "증적 생성 ①: IAM 토큰 발급 시도(감사 기록됨)",
         "title_en": "Evidence 1: IAM token request (audited)",
         "call": http("POST", "/fake-shared/iam/token", json={"client_id": "f34-demo-client"}),
         "expect": {"status": [200, 401, 403, 404]}},
        {"id": "seed-pam", "title_ko": "증적 생성 ②: PAM 특수권한 세션",
         "title_en": "Evidence 2: PAM privileged session",
         "call": http("POST", "/fake-shared/pam/sessions", json={
             "operator": "f34-demo", "target": "console:f34-demo-tray",
             "reason": "ISMS-P 2.5.5 evidence demo", "ttl_s": 600}),
         "expect": {"status": 201}},
        {"id": "controls", "title_ko": "ISMS-P 통제 매핑 조회", "title_en": "ISMS-P control mapping",
         "call": http("GET", "/api/v1/compliance/controls"), "expect": _controls},
        {"id": "controls-2.5", "title_ko": "2.5 인증 및 권한관리 영역만",
         "title_en": "Domain 2.5 only",
         "call": http("GET", "/api/v1/compliance/controls", params={"domain": "2.5"})},
        {"id": "export", "title_ko": "오늘 감사 로그 JSON 내보내기",
         "title_en": "Export today's audit log (JSON)",
         "call": http("GET", "/api/v1/audit/export", params={"period": _today,
                                                              "action": "iam."}),
         "expect": lambda res, ctx: _export_any(res, ctx)},
        {"id": "verify", "title_ko": "해시 체인 재계산·변조 검출", "title_en": "Recompute the chain",
         "call": _verify, "expect": _verified},
        {"id": "csv", "title_ko": "CSV 내보내기", "title_en": "CSV export",
         "call": http("GET", "/api/v1/audit/export",
                      params={"period": _today, "format": "csv"}), "expect": _csv},
        {"id": "tenant-denied", "title_ko": "테넌트 토큰은 거부(운영자 전용)",
         "title_en": "Tenant token is refused (operator only)",
         "call": http("GET", "/api/v1/compliance/controls", as_="tenant:tnt-f34-demo"),
         "expect": {"status": [401, 403]}},
    ],
}


def _export_any(res, ctx):
    """Export of today's iam.* records: at least the seeded token issue."""
    b = body(res)
    if b.get("count", 0) >= 1:
        ctx["entries"] = b["entries"]
        return check(b["digest"] == b["entries"][-1]["hash"],
                     f"오늘 iam.* 감사 기록 {b['count']}건 · 해시 체인 머리 "
                     f"{b['digest'][:16]}… (X-Audit-Digest)",
                     f"{b['count']} iam.* records today · chain head {b['digest'][:16]}…")
    return check(False, "내보낸 기록 없음", "no records exported")
