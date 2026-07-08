"""Fake Shared Services — 구성도 ⑦ Common Platform (IAM·Secrets·PAM·Audit).

DSX 아키텍처의 Shared Services 플레인 대역(fake). 실제 배치에서는
Keycloak(OIDC)·Vault·PAM 게이트웨이로 교체되며, 어댑터 경계는 이 모듈의
SHARED 싱글턴 호출 지점이다.

NVIDIA BMaaS Requirements 매핑:
  - SEC01  사용자 OIDC 인증 — 테넌트별 IAM realm + OIDC 클라이언트
  - SEC04  최소권한 RBAC — realm 기본 롤 3종(tenant-admin/ops-operator/viewer)
  - SEC07  관리자 인터페이스 MFA — tenant-portal 클라이언트 TOTP 강제
  - SEC08  감사 로그 — 모든 IAM/Vault/PAM 동작을 audit 트레일로 보존
  - SEC09/SEC10 자격증명 발급·회전·폐기 — 주문 saga와 동기화

파이프라인 연동:
  - 테넌트 생성          → IAM realm + 기본 롤 + 포털 클라이언트
  - 주문 acceptance      → 서비스 계정(OIDC client-credentials) + Vault 시크릿
                           (스토리지 S3 키·OOB Redfish 자격증명) 발급
  - 주문 terminate(회수) → 해당 주문의 서비스 계정 폐기 + 시크릿 파기
"""

from __future__ import annotations

import secrets as _pysecrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .trace import emit

DEFAULT_ROLES = ["tenant-admin", "ops-operator", "viewer"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OidcClient(BaseModel):
    client_id: str
    kind: str                              # portal | service-account
    scope: str = "openid profile"
    order_id: Optional[str] = None
    mfa: bool = False                      # SEC07 — 관리자 인터페이스 TOTP
    tokens_issued: int = 0
    secret_masked: str = ""                # 원본 secret은 딜리버리 패키지 1회 노출
    state: str = "active"                  # active | revoked
    created_at: str = Field(default_factory=_now)


class IamRealm(BaseModel):
    realm: str                             # == tenant_id
    display: str
    roles: list = Field(default_factory=lambda: list(DEFAULT_ROLES))
    clients: list[OidcClient] = Field(default_factory=list)
    state: str = "active"
    created_at: str = Field(default_factory=_now)


class SecretEntry(BaseModel):
    path: str                              # vault kv 경로
    kind: str                              # s3-access-key | redfish-cred | …
    tenant_ref: str
    order_id: Optional[str] = None
    version: int = 1
    value_masked: str                      # 값은 마스킹만 노출 (SEC 준수)
    created_at: str = Field(default_factory=_now)


class PamSession(BaseModel):
    id: str
    operator: str
    target: str                            # e.g. "console:nh-su-1-rack-00-tray-00"
    reason: str
    ttl_s: int
    state: str = "active"                  # active | closed
    opened_at: str = Field(default_factory=_now)
    closed_at: Optional[str] = None


class AuditEntry(BaseModel):
    seq: int
    at: str
    actor: str
    action: str
    target: str
    result: str = "success"
    tenant_ref: Optional[str] = None


class FakeSharedServices:
    """IAM(OIDC)·Vault·PAM 시뮬레이터 + SEC08 감사 트레일."""

    def __init__(self) -> None:
        self.realms: dict[str, IamRealm] = {}
        self.secrets: dict[str, SecretEntry] = {}
        self.pam_sessions: dict[str, PamSession] = {}
        self.audit_log: list[AuditEntry] = []
        self._sa_secrets: dict[str, str] = {}   # order_id → 원본 secret (1회 인출)
        self._seq = 0

    def reset(self) -> None:
        self.realms.clear()
        self.secrets.clear()
        self.pam_sessions.clear()
        self.audit_log.clear()
        self._sa_secrets.clear()
        self._seq = 0

    # -- SEC08: 감사 트레일 -------------------------------------------------
    def audit(self, actor: str, action: str, target: str,
              result: str = "success", tenant_ref: Optional[str] = None) -> None:
        self._seq += 1
        self.audit_log.append(AuditEntry(
            seq=self._seq, at=_now(), actor=actor, action=action,
            target=target, result=result, tenant_ref=tenant_ref))
        if len(self.audit_log) > 2000:
            del self.audit_log[:200]

    # -- IAM (SEC01·SEC04·SEC07) ---------------------------------------------
    def create_realm(self, tenant_id: str, display: str) -> IamRealm:
        if tenant_id in self.realms:
            return self.realms[tenant_id]
        realm = IamRealm(realm=tenant_id, display=display)
        realm.clients.append(OidcClient(
            client_id=f"{tenant_id}-portal", kind="portal", mfa=True,
            scope="openid profile tenant:self"))
        self.realms[tenant_id] = realm
        emit("NeoCloudOS.Tenancy", "IAM(Keycloak)", "OIDC/IAM",
             f"POST /admin/realms → {tenant_id}",
             f"테넌트 IAM realm 생성 — 기본 롤 {DEFAULT_ROLES} (RBAC), "
             "포털 클라이언트 MFA(TOTP) 강제",
             payload={"realm": tenant_id, "roles": realm.roles,
                      "clients": [c.client_id for c in realm.clients],
                      "mfa": "TOTP required (SEC07)"})
        self.audit("neocloud-os", "iam.realm.create", tenant_id,
                   tenant_ref=tenant_id)
        return realm

    def issue_service_account(self, tenant_id: str, order_id: str) -> OidcClient:
        realm = self.realms.get(tenant_id) or self.create_realm(tenant_id,
                                                                tenant_id)
        secret = f"nc_{_pysecrets.token_hex(16)}"
        client = OidcClient(
            client_id=f"sa-{order_id}", kind="service-account",
            order_id=order_id, secret_masked=f"nc_****{secret[-4:]}",
            scope="nodes:read storage:mount telemetry:write")
        realm.clients.append(client)
        self._sa_secrets[order_id] = secret     # 딜리버리 패키지에서 1회 인출
        emit("NeoCloudOS.M1", "IAM(Keycloak)", "OIDC/IAM",
             f"POST /admin/realms/{tenant_id}/clients → sa-{order_id}",
             "인수(acceptance) 단계 — 클러스터 서비스 계정 발급 "
             f"(scope: {client.scope})", order_id=order_id,
             payload={"client_id": client.client_id,
                      "grant": "client_credentials", "roles": ["viewer"]})
        self.issue_token(client.client_id, actor="neocloud-os",
                         order_id=order_id)
        return client

    def issue_token(self, client_id: str, actor: str = "api",
                    order_id: Optional[str] = None) -> dict:
        realm, client = self._find_client(client_id)
        if client is None or client.state != "active":
            self.audit(actor, "iam.token.issue", client_id, result="denied")
            raise HTTPException(403, f"iam: client '{client_id}' not active")
        client.tokens_issued += 1
        token = f"eyJ.fake.{_pysecrets.token_hex(12)}"
        emit("NeoCloudOS.SharedSvc", "IAM(Keycloak)", "OIDC/IAM",
             "POST /realms/{r}/protocol/openid-connect/token".format(
                 r=realm.realm),
             f"OIDC 토큰 발급 — {client_id} (client_credentials, exp 3600s)",
             order_id=order_id,
             payload={"client_id": client_id, "token_type": "Bearer",
                      "expires_in": 3600, "scope": client.scope})
        self.audit(actor, "iam.token.issue", client_id,
                   tenant_ref=realm.realm)
        return {"access_token": token, "token_type": "Bearer",
                "expires_in": 3600, "scope": client.scope}

    def pop_sa_secret(self, order_id: str) -> str:
        """서비스 계정 원본 secret 1회 인출 — 딜리버리 패키지 전달용 (SEC09)."""
        return self._sa_secrets.pop(order_id, f"nc_{_pysecrets.token_hex(16)}")

    def _find_client(self, client_id: str):
        for realm in self.realms.values():
            for c in realm.clients:
                if c.client_id == client_id:
                    return realm, c
        return None, None

    # -- Vault (SEC09·SEC10) ---------------------------------------------------
    def write_secret(self, path: str, kind: str, tenant_ref: str,
                     order_id: Optional[str] = None) -> SecretEntry:
        prev = self.secrets.get(path)
        entry = SecretEntry(
            path=path, kind=kind, tenant_ref=tenant_ref, order_id=order_id,
            version=(prev.version + 1) if prev else 1,
            value_masked=f"{kind[:3]}_****{_pysecrets.token_hex(2)}")
        self.secrets[path] = entry
        emit("NeoCloudOS.M1", "Vault", "Vault", f"PUT /v1/secret/{path}",
             f"자격증명 저장 — {kind} (v{entry.version}, 값은 KV 암호화 보관)",
             order_id=order_id,
             payload={"path": path, "kind": kind, "version": entry.version})
        self.audit("neocloud-os", "vault.secret.write", path,
                   tenant_ref=tenant_ref)
        return entry

    # -- 회수: 주문 단위 자격증명 폐기 ---------------------------------------
    def revoke_order_credentials(self, tenant_id: str, source_order_id: str,
                                 reclaim_order_id: str) -> dict:
        revoked, purged = [], []
        realm = self.realms.get(tenant_id)
        if realm:
            for c in realm.clients:
                if c.order_id == source_order_id and c.state == "active":
                    c.state = "revoked"
                    revoked.append(c.client_id)
        for path in [p for p, e in self.secrets.items()
                     if e.order_id == source_order_id]:
            purged.append(path)
            del self.secrets[path]
        if revoked or purged:
            emit("NeoCloudOS.M1", "IAM(Keycloak)", "OIDC/IAM",
                 f"DELETE /admin/realms/{tenant_id}/clients",
                 f"회수 — 서비스 계정 폐기 {revoked} + 발급 토큰 무효화",
                 order_id=reclaim_order_id, payload={"revoked": revoked})
            emit("NeoCloudOS.M1", "Vault", "Vault",
                 "DELETE /v1/secret/tenants/{t}/{o}".format(
                     t=tenant_id, o=source_order_id),
                 f"회수 — 주문 자격증명 파기 ({len(purged)}건, shred)",
                 order_id=reclaim_order_id, payload={"purged": purged})
            self.audit("neocloud-os", "iam.credentials.revoke",
                       f"{tenant_id}/{source_order_id}", tenant_ref=tenant_id)
        return {"revoked": revoked, "purged": purged}

    # -- PAM ---------------------------------------------------------------
    def pam_open(self, operator: str, target: str, reason: str,
                 ttl_s: int = 900) -> PamSession:
        self._seq += 1
        sess = PamSession(id=f"pam-{self._seq}", operator=operator,
                          target=target, reason=reason, ttl_s=ttl_s)
        self.pam_sessions[sess.id] = sess
        emit("Operator", "PAM", "PAM", f"POST /pam/sessions → {sess.id}",
             f"권한상승 세션 개시 — {operator} → {target} "
             f"(사유: {reason}, TTL {ttl_s}s, 세션 녹화)",
             payload={"operator": operator, "target": target,
                      "ttl_s": ttl_s, "recording": True})
        self.audit(operator, "pam.session.open", target)
        return sess

    def pam_close(self, session_id: str) -> PamSession:
        sess = self.pam_sessions.get(session_id)
        if not sess:
            raise HTTPException(404, f"pam: session '{session_id}' not found")
        sess.state, sess.closed_at = "closed", _now()
        emit("Operator", "PAM", "PAM",
             f"POST /pam/sessions/{session_id}/close",
             f"권한상승 세션 종료 — {sess.operator} → {sess.target}")
        self.audit(sess.operator, "pam.session.close", sess.target)
        return sess


SHARED = FakeSharedServices()

router = APIRouter(prefix="/fake-shared", tags=["fake-shared-services"])


class TokenRequest(BaseModel):
    client_id: str


class PamOpenRequest(BaseModel):
    operator: str
    target: str
    reason: str
    ttl_s: int = 900


@router.get("/iam/realms")
def list_realms() -> list:
    return list(SHARED.realms.values())


@router.get("/iam/realms/{tenant_id}")
def get_realm(tenant_id: str) -> IamRealm:
    realm = SHARED.realms.get(tenant_id)
    if not realm:
        raise HTTPException(404, f"iam: realm '{tenant_id}' not found")
    return realm


@router.post("/iam/token")
def issue_token(body: TokenRequest) -> dict:
    return SHARED.issue_token(body.client_id)


@router.get("/secrets")
def list_secrets(tenant_ref: Optional[str] = None) -> list:
    items = list(SHARED.secrets.values())
    if tenant_ref:
        items = [e for e in items if e.tenant_ref == tenant_ref]
    return items


@router.get("/pam/sessions")
def list_pam_sessions() -> list:
    return list(SHARED.pam_sessions.values())


@router.post("/pam/sessions", status_code=201)
def open_pam_session(body: PamOpenRequest) -> PamSession:
    return SHARED.pam_open(body.operator, body.target, body.reason, body.ttl_s)


@router.post("/pam/sessions/{session_id}/close")
def close_pam_session(session_id: str) -> PamSession:
    return SHARED.pam_close(session_id)


@router.get("/audit")
def audit_trail(tenant_ref: Optional[str] = None, limit: int = 50) -> list:
    items = SHARED.audit_log
    if tenant_ref:
        items = [a for a in items if a.tenant_ref == tenant_ref]
    return items[-limit:][::-1]
