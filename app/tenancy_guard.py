"""고객면(Customer-plane) 테넌트 격리 · RBAC 가드.

계약 (콘솔 ↔ NOCP):
  - 요청 헤더 `X-Tenant-Id: tnt-xxx` 가 있으면 그 요청은 고객면으로 간주한다.
    `X-Tenant-Role: admin|member|viewer` (기본 member)로 역할을 전달한다.
  - 헤더가 없으면 기존 동작 그대로 — 운영/사업 콘솔·기존 테스트·ops 승인
    큐(관리면)는 아무 영향도 받지 않는다.
  - 고객면 규칙:
      1) 경로에 테넌트가 박힌 엔드포인트(`/api/v1/tenants/{tid}/...`)는
         tid ≠ 헤더 테넌트 → 403 {"detail": "tenant scope violation"}.
      2) 주문 단건(`/api/v1/orders/{id}` 및 하위 flow/acceptance* /approve
         등)은 주문의 tenant_id 불일치 → 403.
      3) k8s 클러스터 단건·하위(`/api/v1/k8s/clusters/{id}/*`)는 클러스터의
         tenant_id 불일치 → 403.
      4) 목록 엔드포인트(고객 콘솔 소비분)는 해당 테넌트 것만 반환:
         - tenant_id 쿼리 필터를 이미 지원하는 목록은 쿼리스트링을 헤더
           테넌트로 강제 재작성(기존 핸들러 필터 로직 재사용, 침습 0).
         - `/fake-vast/views`·`/fake-nico/hosts`·`/api/v1/tenants` 는 응답
           JSON 배열을 tenant_ref/id 기준 후처리 필터.
      5) `X-Tenant-Role: viewer` 는 읽기 전용 — GET/HEAD/OPTIONS 외 메서드는
         403 {"detail": "read-only role"}. member/admin은 변경 액션 허용.
  - 비인증 공개 표면(`/api/v1/status`, `/api/v1/public/*`, `/api/v1/spec`)과
    API 외 경로(HTML·/health·/docs·/static)는 헤더와 무관하게 무변경.

구현: 라우터별 의존성 주입 대신 미들웨어 1곳에서 경로 패턴 매칭 — 기존
라우터 코드는 한 줄도 바꾸지 않는다(회귀 위험 최소화).
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .store import STORE

TENANT_HEADER = "x-tenant-id"
ROLE_HEADER = "x-tenant-role"

# 헤더가 있어도 절대 개입하지 않는 공개(비인증) 표면
_PUBLIC_PREFIXES = (
    "/api/v1/status",
    "/api/v1/public/",
    "/api/v1/spec",
)

# 경로에 리소스 id가 박힌 단건/하위 표면 — 소유 테넌트 검사
_TENANT_PATH_RE = re.compile(r"^/api/v1/tenants/(?P<tid>[^/]+)")
_ORDER_PATH_RE = re.compile(r"^/api/v1/orders/(?P<oid>[^/]+)")
_CLUSTER_PATH_RE = re.compile(r"^/api/v1/k8s/clusters/(?P<cid>[^/]+)")

# tenant_id 쿼리 필터를 이미 지원하는 목록 엔드포인트(정확히 일치하는 경로)
# → 쿼리스트링 강제 재작성으로 기존 필터 로직 재사용
_QUERY_SCOPED_LISTS = frozenset({
    "/api/v1/orders",
    "/api/v1/k8s/clusters",
    "/api/v1/k8s/installs",
    "/api/v1/tickets",
    "/api/v1/billing/usage",
    "/api/v1/nodes",
    "/api/v1/cpu-nodes",
    "/api/v1/nvlink-partitions",
})

# tenant 필터 파라미터가 없는 목록 — 응답 JSON 배열 후처리 필터
# {경로: 항목에서 테넌트를 식별하는 키}
_RESPONSE_FILTERED_LISTS = {
    "/api/v1/tenants": "id",          # 고객 콘솔 테넌트 정보 — 자기 것만
    "/fake-vast/views": "tenant_ref",
    "/fake-nico/hosts": "tenant_ref",
}

_READ_METHODS = ("GET", "HEAD", "OPTIONS")


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=403)


def _scope_violation(request: Request, tid: str) -> bool:
    """경로 기반 소유 검사 — 위반이면 True (403 대상).

    존재하지 않는 리소스는 검사를 통과시켜 기존 404 경로를 보존한다
    (타 테넌트 경로 하드코딩은 존재 여부와 무관하게 403 — 존재 노출 방지)."""
    path = request.url.path

    m = _TENANT_PATH_RE.match(path)
    if m and m.group("tid") != tid:
        return True

    m = _ORDER_PATH_RE.match(path)
    if m:
        order = STORE.orders.get(m.group("oid"))
        if order is not None and order.tenant_id != tid:
            return True

    m = _CLUSTER_PATH_RE.match(path)
    if m:
        cluster = STORE.k8s_clusters.get(m.group("cid"))
        if cluster is not None and cluster.tenant_id != tid:
            return True

    return False


def _force_tenant_query(request: Request, tid: str) -> None:
    """목록 요청의 tenant_id 쿼리를 헤더 테넌트로 강제(덮어쓰기)."""
    params = dict(parse_qsl(request.scope.get("query_string", b"").decode()))
    params["tenant_id"] = tid
    request.scope["query_string"] = urlencode(params).encode()


async def _filter_json_list(response: Response, key: str, tid: str) -> Response:
    """응답이 JSON 배열이면 항목의 `key` 값이 테넌트와 일치하는 것만 남긴다."""
    if response.status_code != 200:
        return response
    chunks = [chunk async for chunk in response.body_iterator]
    body = b"".join(chunks)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        data = None
    if isinstance(data, list):
        body = json.dumps(
            [x for x in data if isinstance(x, dict) and x.get(key) == tid]
        ).encode()
    headers = dict(response.headers)
    headers.pop("content-length", None)
    return Response(content=body, status_code=response.status_code,
                    headers=headers, media_type="application/json")


async def _body_tenant_mismatch(request: Request, tid: str) -> bool:
    """변경 요청 바디의 tenant_id 위조 검사 — 헤더 테넌트와 다르면 True.

    BaseHTTPMiddleware에서 body를 소비하면 다운스트림이 다시 읽지 못하므로,
    읽은 바디를 되돌려주는 receive로 교체해 핸들러에 그대로 전달한다."""
    if request.method in _READ_METHODS:
        return False
    body = await request.body()

    async def _replay():
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = _replay  # 캐시된 바디 재공급
    if not body:
        return False
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return (isinstance(data, dict)
            and "tenant_id" in data
            and data["tenant_id"] != tid)


class TenancyGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        tid = request.headers.get(TENANT_HEADER)
        path = request.url.path
        # 관리면(헤더 없음)·API 외 경로·공개 표면 → 완전 무개입
        if (not tid
                or not (path.startswith("/api/") or path.startswith("/fake-"))
                or path.startswith(_PUBLIC_PREFIXES)):
            return await call_next(request)

        # 0) 고객면에서 관리자 표면 차단
        if path.startswith("/api/v1/admin"):
            return _forbidden("admin surface not allowed for tenant scope")

        # 1) 테넌트 스코프 (경로 기반 소유 검사)
        if _scope_violation(request, tid):
            return _forbidden("tenant scope violation")

        # 2) RBAC — viewer는 읽기 전용
        role = (request.headers.get(ROLE_HEADER) or "member").strip().lower()
        if role == "viewer" and request.method not in _READ_METHODS:
            return _forbidden("read-only role")

        # 2.5) 변경 바디의 tenant_id 위조 차단 (주문/설치/티켓 생성 등)
        if await _body_tenant_mismatch(request, tid):
            return _forbidden("tenant scope violation")

        # 3) 목록 스코프 — 쿼리 재작성(기존 핸들러 필터 재사용)
        if request.method == "GET" and path in _QUERY_SCOPED_LISTS:
            _force_tenant_query(request, tid)

        response = await call_next(request)

        # 4) 목록 스코프 — 응답 후처리 필터 (tenant 파라미터 없는 표면)
        if request.method == "GET" and path in _RESPONSE_FILTERED_LISTS:
            response = await _filter_json_list(
                response, _RESPONSE_FILTERED_LISTS[path], tid)
        return response
