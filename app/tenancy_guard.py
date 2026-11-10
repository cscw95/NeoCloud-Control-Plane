"""Customer-plane tenant isolation · RBAC guard.

Contract (console ↔ NOCP):
  - If a request carries the header `X-Tenant-Id: tnt-xxx`, it is treated as a
    customer-plane request. `X-Tenant-Role: admin|member|viewer` (default member)
    conveys the role.
  - Without the header, behavior is unchanged — the ops/biz consoles·existing
    tests·ops approval queue (management plane) are entirely unaffected.
  - Customer-plane rules:
      1) endpoints with a tenant in the path (`/api/v1/tenants/{tid}/...`) where
         tid ≠ header tenant → 403 {"detail": "tenant scope violation"}.
      2) single order (`/api/v1/orders/{id}` and sub flow/acceptance* /approve
         etc.) with a mismatched order tenant_id → 403.
      3) single/sub k8s cluster (`/api/v1/k8s/clusters/{id}/*`) with a mismatched
         cluster tenant_id → 403.
      4) list endpoints (consumed by the customer console) return only that
         tenant's items:
         - lists that already support a tenant_id query filter have the query
           string force-rewritten to the header tenant (reuses the existing
           handler filter logic, zero intrusion).
         - `/fake-vast/views`·`/fake-nico/hosts`·`/api/v1/tenants` post-filter the
           response JSON array by tenant_ref/id.
      5) `X-Tenant-Role: viewer` is read-only — methods other than
         GET/HEAD/OPTIONS → 403 {"detail": "read-only role"}. member/admin are
         allowed mutating actions.
  - Unauthenticated public surfaces (`/api/v1/status`, `/api/v1/public/*`,
    `/api/v1/spec`) and non-API paths (HTML·/health·/docs·/static) are unchanged
    regardless of the header.

Authentication (DSX baseline security, see auth.py):
  - Identity comes from a bearer token (role operator|tenant + tenant_id). The
    `X-Tenant-Id`/`X-Tenant-Role`/`X-Operator` headers described above are
    honoured only with `NOCP_DEV_INSECURE_HEADERS=1`; otherwise they are
    stripped and replaced by the token's identity before routing.
  - No identity on /api/* → 401, unless `NOCP_DEV_OPERATOR_DEFAULT=1` (dev:
    implicit operator; the test suite and run.sh set it).
  - Exempt: public surfaces above and the Redfish EventService webhook
    (`POST /api/v1/oob/events`, pushed by the AI-Infra emulator).

Implementation: instead of per-router dependency injection, path-pattern matching
in a single middleware — not a single line of existing router code changes
(minimizing regression risk).
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
OPERATOR_HEADER = "x-operator"
_WEBHOOKS = frozenset({("POST", "/api/v1/oob/events")})
# EventSource cannot send headers — the SSE stream accepts a signed ?token=
# (scope "events", POST /api/v1/events/token); see auth.py
_SSE_PATHS = frozenset({"/api/v1/events"})
# operator-only DSX surfaces (fleet health, power, raw infra) — closed to tenants
_OPERATOR_ONLY = ("/api/v1/oob", "/api/v1/infra", "/api/v1/power", "/api/v1/rma",
                  "/api/v1/firmware", "/api/v1/machines", "/api/v1/events",
                  "/api/v1/auth/tokens", "/api/v1/dcim", "/api/v1/bringup")
_ISOLATION_PATH_RE = re.compile(r"^/api/v1/isolation/(?P<tid>[^/]+)")
ROLE_HEADER = "x-tenant-role"

# public (unauthenticated) surfaces never intervened in, even with the header
_PUBLIC_PREFIXES = (
    "/api/v1/status",
    "/api/v1/public/",
    "/api/v1/spec",
    "/api/v1/signup",      # F07 self signup (handlers check operator/tenant themselves)
)
# F07 OIDC endpoints authenticate clients themselves (Basic client auth, RFC 6750 errors)
_OIDC_PREFIXES = ("/realms/", "/oauth2/")

# single/sub surfaces with a resource id in the path — owner tenant check
_TENANT_PATH_RE = re.compile(r"^/api/v1/tenants/(?P<tid>[^/]+)")
_ORDER_PATH_RE = re.compile(r"^/api/v1/orders/(?P<oid>[^/]+)")
_CLUSTER_PATH_RE = re.compile(r"^/api/v1/k8s/clusters/(?P<cid>[^/]+)")
_NVKE_PATH_RE = re.compile(r"^/api/v1/nvke/clusters/(?P<cid>[^/]+)")
_TICKET_PATH_RE = re.compile(r"^/api/v1/tickets/(?P<id>[^/]+)")
_NODE_PATH_RE = re.compile(r"^/api/v1/nodes/(?P<id>[^/]+)$")

# list endpoints already supporting a tenant_id query filter (exact-match paths)
# → force-rewrite the query string to reuse the existing filter logic
_QUERY_SCOPED_LISTS = frozenset({
    "/api/v1/orders",
    "/api/v1/k8s/clusters",
    "/api/v1/k8s/installs",
    "/api/v1/tickets",
    "/api/v1/billing/usage",
    "/api/v1/nodes",
    "/api/v1/cpu-nodes",
    "/api/v1/nvlink-partitions",
    "/api/v1/nvke/clusters",
    "/api/v1/metering/usage",
    "/api/v1/metering/events",
    "/api/v1/token/quotas",
})

# lists without a tenant filter parameter — post-filter the response JSON array
# {path: key that identifies the tenant in each item}
_RESPONSE_FILTERED_LISTS = {
    "/api/v1/tenants": "id",          # customer console tenant info — only its own
    "/fake-vast/views": "tenant_ref",
    "/fake-nico/hosts": "tenant_ref",
}

_READ_METHODS = ("GET", "HEAD", "OPTIONS")


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=403)


def _scope_violation(request: Request, tid: str) -> bool:
    """Path-based ownership check — True on violation (subject to 403).

    Nonexistent resources pass the check to preserve the existing 404 path
    (a hardcoded other-tenant path is 403 regardless of existence — to avoid
    leaking existence)."""
    path = request.url.path

    m = _TENANT_PATH_RE.match(path) or _ISOLATION_PATH_RE.match(path)
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

    m = _TICKET_PATH_RE.match(path)
    if m:
        t = STORE.tickets.get(m.group("id"))
        if t is not None and t.tenant_id != tid:
            return True

    m = _NODE_PATH_RE.match(path)
    if m:
        n = STORE.node_instances.get(m.group("id"))
        if n is not None and n.tenant_id not in (None, tid):
            return True

    m = _NVKE_PATH_RE.match(path)
    if m:
        from .nvke import cluster_tenant
        owner = cluster_tenant(m.group("cid"))
        if owner is not None and owner != tid:
            return True

    return False


def _force_tenant_query(request: Request, tid: str) -> None:
    """Force (overwrite) a list request's tenant_id query to the header tenant."""
    params = dict(parse_qsl(request.scope.get("query_string", b"").decode()))
    params["tenant_id"] = tid
    request.scope["query_string"] = urlencode(params).encode()


async def _filter_json_list(response: Response, key: str, tid: str) -> Response:
    """If the response is a JSON array, keep only items whose `key` value matches the tenant."""
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
    """Check a mutating request body for a forged tenant_id — True if it differs
    from the header tenant.

    Consuming the body in BaseHTTPMiddleware prevents downstream from reading it
    again, so replace the receive with one that replays the read body, passing it
    to the handler unchanged."""
    if request.method in _READ_METHODS:
        return False
    body = await request.body()

    async def _replay():
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = _replay  # re-supply the cached body
    if not body:
        return False
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return (isinstance(data, dict)
            and "tenant_id" in data
            and data["tenant_id"] != tid)


def _guarded(path: str) -> bool:
    return ((path.startswith("/api/") or path.startswith("/fake-"))
            and not path.startswith(_PUBLIC_PREFIXES))


_IDENTITY_HEADERS = (TENANT_HEADER.encode(), ROLE_HEADER.encode(),
                     OPERATOR_HEADER.encode())


def _bind_identity_headers(request: Request, ident) -> None:
    """Make the authenticated identity the only source of tenant headers.

    Handlers that read `X-Tenant-Id` (customer_ops) then see the token's tenant;
    client-supplied identity headers are dropped unless they produced `ident`."""
    hdrs = [(k, v) for k, v in request.scope["headers"] if k not in _IDENTITY_HEADERS]
    if ident is not None and ident.role == "tenant":
        hdrs += [(TENANT_HEADER.encode(), ident.tenant_id.encode()),
                 (ROLE_HEADER.encode(), ident.tenant_role.encode())]
    elif ident is not None:
        hdrs.append((OPERATOR_HEADER.encode(), b"true"))
    request.scope["headers"] = hdrs


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=401,
                        headers={"WWW-Authenticate": "Bearer"})


class TenancyGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        from . import auth
        path = request.url.path
        # OpenAI-compatible token API authenticates its own API keys (token_api.py)
        if path.startswith("/v1/"):
            return await call_next(request)
        try:
            ident = auth.resolve(request, _SSE_PATHS)
        except auth.AuthError as e:
            if not path.startswith(_OIDC_PREFIXES):
                return _unauthorized(str(e))
            ident = None
        if (ident is None and _guarded(path)
                and (request.method, path) not in _WEBHOOKS
                and request.method != "OPTIONS"):
            if not auth.dev_operator_default():
                return _unauthorized("authentication required")
            ident = auth.Identity(role="operator", via="dev-default")
        _bind_identity_headers(request, ident)
        request.state.identity = ident
        tid = ident.tenant_id if ident is not None and ident.role == "tenant" else None
        # management plane·non-API paths·public surfaces → no tenant rules
        if (not tid
                or not (path.startswith("/api/") or path.startswith("/fake-"))
                or path.startswith(_PUBLIC_PREFIXES)):
            return await call_next(request)

        # 0) block admin surfaces from the customer plane
        if path.startswith("/api/v1/admin"):
            return _forbidden("admin surface not allowed for tenant scope")
        if path.startswith(_OPERATOR_ONLY):
            return _forbidden("operator surface not allowed for tenant scope")

        # 1) tenant scope (path-based ownership check)
        if _scope_violation(request, tid):
            return _forbidden("tenant scope violation")

        # 2) RBAC — viewer is read-only
        role = ident.tenant_role.strip().lower()
        if role == "viewer" and request.method not in _READ_METHODS:
            return _forbidden("read-only role")

        # 2.5) block a forged tenant_id in the mutating body (order/install/ticket creation, etc.)
        if await _body_tenant_mismatch(request, tid):
            return _forbidden("tenant scope violation")

        # 3) list scope — query rewrite (reuse the existing handler filter)
        if request.method == "GET" and path in _QUERY_SCOPED_LISTS:
            _force_tenant_query(request, tid)

        response = await call_next(request)

        # 4) list scope — response post-filter (surfaces without a tenant parameter)
        if request.method == "GET" and path in _RESPONSE_FILTERED_LISTS:
            response = await _filter_json_list(
                response, _RESPONSE_FILTERED_LISTS[path], tid)
        return response
