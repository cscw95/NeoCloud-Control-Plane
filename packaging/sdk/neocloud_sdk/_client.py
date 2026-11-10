"""Hand-written core the generated namespaces call into (`BaseClient.call`).

Auth (one of, highest first), matching NOCP app/auth.py:
  - `token`    → `Authorization: Bearer <token>` (HMAC `nocp1.` token, NOCP_TOKENS
                 static token, or an OIDC access token from `/realms/{realm}/...`).
  - `api_key`  → `Authorization: Bearer <key>` on the OpenAI-compatible `/v1/*` paths.
  - `operator` / `tenant_id` → legacy `X-Operator: true` / `X-Tenant-Id` headers
                 (honoured only when NOCP runs with NOCP_DEV_INSECURE_HEADERS=1).
Environment defaults: NEOCLOUD_BASE_URL, NEOCLOUD_TOKEN, NEOCLOUD_API_KEY,
NEOCLOUD_OPERATOR=1, NEOCLOUD_TENANT_ID.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import Any, Optional

from ._transport import (RateLimitError, Response, UrllibTransport, iter_sse,
                         raise_for_status)

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class BaseClient:
    def __init__(self, base_url: Optional[str] = None, *, token: Optional[str] = None,
                 api_key: Optional[str] = None, operator: Optional[bool] = None,
                 tenant_id: Optional[str] = None, transport=None, max_retries: int = 0,
                 default_headers: Optional[dict] = None, user_agent: str = "neocloud-sdk/0.1.0"):
        env = os.environ
        self.base_url = (base_url or env.get("NEOCLOUD_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.auth_token = token if token is not None else env.get("NEOCLOUD_TOKEN")
        self.api_key = api_key if api_key is not None else env.get("NEOCLOUD_API_KEY")
        self.operator = operator if operator is not None else env.get("NEOCLOUD_OPERATOR") == "1"
        self.tenant_id = tenant_id if tenant_id is not None else env.get("NEOCLOUD_TENANT_ID")
        self.transport = transport or UrllibTransport()
        self.max_retries = max_retries
        self.default_headers = dict(default_headers or {})
        self.user_agent = user_agent
        self.last_request: Optional[dict] = None   # for demos: the wire request (auth redacted)
        self.last_response: Optional[Response] = None
        self.history: list = []                      # last 50 (request, status) pairs

    # ------------------------------------------------------------------ helpers
    def with_options(self, **kw) -> "BaseClient":
        """Copy of this client with some options replaced (like openai-python)."""
        cur = dict(base_url=self.base_url, token=self.auth_token, api_key=self.api_key,
                   operator=self.operator, tenant_id=self.tenant_id, transport=self.transport,
                   max_retries=self.max_retries, default_headers=self.default_headers)
        cur.update(kw)
        return type(self)(**cur)

    def _headers(self, path: str, extra: Optional[dict]) -> dict:
        h = {"Accept": "application/json", "User-Agent": self.user_agent}
        h.update(self.default_headers)
        bearer = self.auth_token
        if path.startswith("/v1/") and self.api_key:
            bearer = self.api_key
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        else:
            if self.operator:
                h["X-Operator"] = "true"
            if self.tenant_id:
                h["X-Tenant-Id"] = self.tenant_id
        h.update(extra or {})
        return h

    def url(self, path: str, query: Optional[dict] = None) -> str:
        q = {k: (str(v).lower() if isinstance(v, bool) else v)
             for k, v in (query or {}).items() if v is not None}
        qs = urllib.parse.urlencode(q, doseq=True)
        return self.base_url + path + (f"?{qs}" if qs else "")

    # ------------------------------------------------------------------ core
    def request(self, method: str, path: str, *, query: Optional[dict] = None,
                json_body: Any = None, form: Optional[dict] = None,
                headers: Optional[dict] = None, stream: bool = False) -> Response:
        h = self._headers(path, headers)
        data = None
        if form is not None:
            data = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None}).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        if stream:
            h["Accept"] = "text/event-stream"
        url = self.url(path, query)
        self.last_request = {"method": method, "url": url, "body": json_body if form is None else form,
                             "headers": {k: ("Bearer ***" if k == "Authorization" else v)
                                         for k, v in h.items()}}
        attempt = 0
        while True:
            resp = self.transport.send(method, url, h, data, stream)
            self.last_response = resp
            self.history = (self.history + [{**self.last_request, "status": resp.status}])[-50:]
            try:
                raise_for_status(resp)
                return resp
            except RateLimitError as e:
                if attempt >= self.max_retries:
                    raise
                attempt += 1
                time.sleep(min(60.0, e.retry_after or 1.0))

    def call(self, op: dict, path_params: dict, query: dict, body: Any,
             headers: Optional[dict] = None, stream: bool = False) -> Any:
        """Entry point for generated methods: `op` is one OPERATIONS row."""
        path = op["path"]
        for name, val in path_params.items():
            if val is None:
                raise TypeError(f"{op['id']}: path parameter '{name}' is required")
            path = path.replace("{" + name + "}", urllib.parse.quote(str(val), safe=""))
        form = body if op.get("body") == "form" else None
        json_body = body if op.get("body") == "json" else None
        if body is not None and not op.get("body"):
            json_body = body
        if isinstance(json_body, dict) and json_body.get("stream") is True:
            stream = True
        resp = self.request(op["method"], path, query=query, json_body=json_body, form=form,
                            headers=headers, stream=stream)
        if stream and resp.lines is not None:
            return self._events(resp)
        return resp.body

    @staticmethod
    def _events(resp: Response):
        for ev in iter_sse(resp.lines):
            try:
                yield json.loads(ev["data"])
            except ValueError:
                yield ev
