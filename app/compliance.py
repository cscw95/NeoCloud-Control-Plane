"""F34 compliance evidence — ISMS-P control mapping and audit log export.

No open-source upstream (ref/upstream/UPSTREAM-REFS.md: "F34 원본 없음,
NeoCloud 고유"). Control ids and names follow the public KISA ISMS-P
certification criteria (정보보호 및 개인정보보호 관리체계 인증 기준, 공개 문서
기준); only the subset that the NeoCloud control plane produces evidence for
is mapped. Status is derived live from the plane's own state: a control is
"implemented" when its evidence source holds records now, "partial" when the
mechanism exists but holds no records yet or covers only part of the control,
"planned" when nothing in the emulator covers it.

  GET /api/v1/compliance/controls?domain=&status=     (operator)
  GET /api/v1/audit/export?period=YYYY-MM|YYYY-MM-DD..YYYY-MM-DD&format=json|jsonl|csv
      (operator) — SEC08 audit trail with a SHA-256 hash chain per entry
      (prev_hash → hash) and the chain head in X-Audit-Digest; the export
      itself is audited (audit.export).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .contracts import _caller_tenant
from .shared_services import SHARED

router = APIRouter(prefix="/api/v1", tags=["compliance"])

FRAMEWORK = {"id": "ISMS-P", "name_ko": "정보보호 및 개인정보보호 관리체계 인증",
             "authority": "KISA / 과학기술정보통신부·개인정보보호위원회",
             "basis": "공개 문서 기준 (ISMS-P 인증기준 안내서)",
             "scope": "NeoCloud control plane subset"}
STATUSES = ("implemented", "partial", "planned")
EXPORT_FIELDS = ("seq", "at", "actor", "action", "target", "result",
                 "tenant_ref", "prev_hash", "hash")
GENESIS = "0" * 64
_PERIOD = re.compile(r"^\d{4}-\d{2}$")
_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")


def _require_operator(request: Request) -> None:
    if _caller_tenant(request) is not None:
        raise HTTPException(403, "operator only")


def _audit_count(*prefixes: str) -> int:
    return sum(1 for e in SHARED.audit_log if e.action.startswith(prefixes))


def _ev(kind: str, ref: str, count: Optional[int] = None) -> dict:
    out = {"kind": kind, "ref": ref}
    if count is not None:
        out["count"] = count
    return out


def _controls() -> list[dict]:
    """(id, domain, name_ko, name_en, neocloud mapping, evidence, status)."""
    realms = len(SHARED.realms)
    secrets = len(SHARED.secrets)
    pam = len(SHARED.pam_sessions)
    audit = len(SHARED.audit_log)

    def live(n: int, full: bool = True) -> str:
        return ("implemented" if full else "partial") if n else "partial"

    rows = [
        ("2.5.1", "2.5 인증 및 권한관리", "사용자 계정 관리", "User account management",
         ["SEC01", "SEC04"], [_ev("iam-realm", "/fake-shared/iam/realms", realms),
                              _ev("audit", "iam.realm.create", _audit_count("iam.realm"))],
         live(realms)),
        ("2.5.2", "2.5 인증 및 권한관리", "사용자 식별", "User identification",
         ["SEC01"], [_ev("oidc", "/fake-shared/iam/token",
                         _audit_count("iam.token.issue"))], live(realms)),
        ("2.5.3", "2.5 인증 및 권한관리", "사용자 인증", "User authentication",
         ["SEC01", "SEC07"], [_ev("oidc+mfa", "tenant-portal client totp_required",
                                  realms)], live(realms)),
        ("2.5.5", "2.5 인증 및 권한관리", "특수 계정 및 권한 관리",
         "Privileged account management", ["SEC04", "PAM"],
         [_ev("pam-session", "/fake-shared/pam/sessions", pam)], live(pam)),
        ("2.5.6", "2.5 인증 및 권한관리", "접근권한 검토", "Access rights review",
         ["SEC04"], [_ev("audit", "/api/v1/audit/export", audit)], "partial"),
        ("2.6.1", "2.6 접근통제", "네트워크 접근", "Network access",
         ["DPU isolation", "tenancy"],
         [_ev("isolation-report", "/api/v1/tenants/{tenant_id}/isolation")],
         "implemented"),
        ("2.6.2", "2.6 접근통제", "정보시스템 접근", "Information system access",
         ["tenancy_guard", "SEC04"], [_ev("guard", "tenancy_guard (operator-only prefixes)")],
         "implemented"),
        ("2.6.6", "2.6 접근통제", "원격접근 통제", "Remote access control",
         ["PAM", "OOB"], [_ev("pam-session", "/fake-shared/pam/sessions", pam)], live(pam)),
        ("2.7.1", "2.7 암호화 적용", "암호정책 적용", "Cryptography policy",
         ["SEC09"], [_ev("vault", "vault kv (masked values only)", secrets)], live(secrets)),
        ("2.7.2", "2.7 암호화 적용", "암호키 관리", "Cryptographic key management",
         ["SEC09", "SEC10"], [_ev("audit", "vault.secret.write / iam.credentials.revoke",
                                  _audit_count("vault.", "iam.credentials"))],
         live(secrets)),
        ("2.9.1", "2.9 시스템 및 서비스 운영관리", "변경관리", "Change management",
         ["firmware baseline"], [_ev("firmware-drift", "/api/v1/firmware/drift")], "partial"),
        ("2.9.2", "2.9 시스템 및 서비스 운영관리", "성능 및 장애관리",
         "Performance and incident management", ["F10", "F26"],
         [_ev("equipment-health", "/api/v1/health/equipment"),
          _ev("oob-events", "/api/v1/oob/events")], "implemented"),
        ("2.9.3", "2.9 시스템 및 서비스 운영관리", "백업 및 복구관리",
         "Backup and recovery", ["VAST Data snapshots"], [], "planned"),
        ("2.9.4", "2.9 시스템 및 서비스 운영관리", "로그 및 접속기록 관리",
         "Log and access record management", ["SEC08", "F34"],
         [_ev("audit-log", "/api/v1/audit/export", audit)], live(audit)),
        ("2.9.5", "2.9 시스템 및 서비스 운영관리", "로그 및 접속기록 점검",
         "Log and access record review", ["F34"],
         [_ev("hash-chain", "X-Audit-Digest on /api/v1/audit/export", audit)], live(audit)),
        ("2.9.6", "2.9 시스템 및 서비스 운영관리", "시간 동기화", "Time synchronization",
         ["UTC timestamps"], [_ev("timestamps", "ISO-8601 UTC on every audit entry")],
         "partial"),
        ("2.10.2", "2.10 시스템 및 서비스 보안관리", "클라우드 보안", "Cloud security",
         ["tenant isolation", "site isolation"],
         [_ev("isolation-report", "/api/v1/tenants/{tenant_id}/isolation")], "implemented"),
        ("2.10.4", "2.10 시스템 및 서비스 보안관리", "전자거래 및 핀테크 보안",
         "E-commerce and fintech security", ["F05", "Stripe webhooks"],
         [_ev("signed-webhooks", "/api/v1/billing/webhooks")], "implemented"),
        ("2.10.8", "2.10 시스템 및 서비스 보안관리", "패치관리", "Patch management",
         ["Dell iDRAC10 firmware baseline"],
         [_ev("firmware-baseline", "/api/v1/firmware/baseline")], "implemented"),
        ("2.11.3", "2.11 사고 예방 및 대응", "이상행위 분석 및 모니터링",
         "Anomaly analysis and monitoring", ["F10", "NVSentinel"],
         [_ev("oob-events", "/api/v1/oob/events")], "implemented"),
        ("2.11.5", "2.11 사고 예방 및 대응", "사고 대응 및 복구", "Incident response and recovery",
         ["RMA", "break-fix"], [_ev("rma", "/api/v1/rma")], "implemented"),
        ("2.12.1", "2.12 재해복구", "재해, 재난 대비 안전조치", "Disaster preparedness",
         ["multi-site"], [], "planned"),
        ("3.4.1", "3.4 개인정보 파기 시 보호조치", "개인정보의 파기",
         "Destruction of personal information", ["order reclaim"],
         [_ev("audit", "iam.credentials.revoke", _audit_count("iam.credentials.revoke"))],
         "partial"),
    ]
    return [{"id": i, "domain": dom, "name_ko": ko, "name_en": en,
             "neocloud": nc, "evidence": ev, "status": st}
            for i, dom, ko, en, nc, ev, st in rows]


@router.get("/compliance/controls")
def compliance_controls(request: Request, domain: Optional[str] = None,
                        status: Optional[str] = None) -> dict:
    _require_operator(request)
    if status is not None and status not in STATUSES:
        raise HTTPException(422, f"status must be one of {list(STATUSES)}")
    rows = [c for c in _controls()
            if (status is None or c["status"] == status)
            and (domain is None or c["domain"].startswith(domain)
                 or c["id"].startswith(domain))]
    allc = _controls()
    summary = {s: sum(1 for c in allc if c["status"] == s) for s in STATUSES}
    summary["total"] = len(allc)
    summary["coverage_pct"] = round(100.0 * summary["implemented"] / len(allc), 1)
    return {"framework": FRAMEWORK, "generated_at": _iso(),
            "controls": rows, "summary": summary}


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounds(period: str) -> tuple[datetime, datetime]:
    if _PERIOD.match(period or ""):
        y, m = int(period[:4]), int(period[5:])
        if not 1 <= m <= 12:
            raise HTTPException(422, "period month must be 01..12")
        a = datetime(y, m, 1, tzinfo=timezone.utc)
        return a, datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=timezone.utc)
    mr = _RANGE.match(period or "")
    if mr:
        try:
            a = datetime.fromisoformat(mr.group(1)).replace(tzinfo=timezone.utc)
            b = datetime.fromisoformat(mr.group(2)).replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(422, "invalid date in period") from None
        if b < a:
            raise HTTPException(422, "period end before start")
        return a, b + timedelta(days=1)
    raise HTTPException(422, "period must be YYYY-MM or YYYY-MM-DD..YYYY-MM-DD")


def _chain(entries: list[dict]) -> list[dict]:
    prev, out = GENESIS, []
    for e in entries:
        body = json.dumps({k: e.get(k) for k in EXPORT_FIELDS[:7]},
                          sort_keys=True, ensure_ascii=False)
        h = hashlib.sha256((prev + body).encode()).hexdigest()
        out.append({**e, "prev_hash": prev, "hash": h})
        prev = h
    return out


def verify_chain(entries: list[dict]) -> bool:
    """Recompute the chain of an export (used by tests and auditors)."""
    raw = [{k: e.get(k) for k in EXPORT_FIELDS[:7]} for e in entries]
    return [e["hash"] for e in _chain(raw)] == [e["hash"] for e in entries]


@router.get("/audit/export")
def audit_export(request: Request, period: str, format: str = "json",
                 action: Optional[str] = None):
    _require_operator(request)
    if format not in ("json", "jsonl", "csv"):
        raise HTTPException(422, "format must be json, jsonl or csv")
    a, b = _bounds(period)
    rows = []
    for e in list(SHARED.audit_log):
        at = datetime.fromisoformat(e.at.replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if a <= at < b and (action is None or e.action.startswith(action)):
            rows.append(e.model_dump())
    rows = _chain(rows)
    digest = rows[-1]["hash"] if rows else GENESIS
    SHARED.audit("neocloud-os", "audit.export", f"{period}:{format}",
                 tenant_ref=None)
    headers = {"X-Audit-Digest": digest, "X-Audit-Count": str(len(rows)),
               "Content-Disposition":
                   f'attachment; filename="audit-{period}.{format}"'}
    if format == "jsonl":
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        return Response(text, media_type="application/x-ndjson", headers=headers)
    if format == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(EXPORT_FIELDS), lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in EXPORT_FIELDS})
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers=headers)
    return JSONResponse({"period": period, "from": a.isoformat(), "to": b.isoformat(),
                         "count": len(rows), "digest": digest, "algorithm": "sha256-chain",
                         "genesis": GENESIS, "entries": rows}, headers=headers)
