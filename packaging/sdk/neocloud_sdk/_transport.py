"""HTTP transport for the NeoCloud SDK (stdlib only, no third-party deps).

Two transports share one small interface, `send(method, url, headers, body, stream)`:
  - UrllibTransport: real network calls (urllib.request).
  - ASGITransport-free `TestClientTransport`: wraps a starlette/fastapi TestClient so
    tests and the NOCP demo runner can drive the SDK in-process.

Error mapping follows the OpenAI Python SDK (openai-python `_exceptions.py`): one
APIStatusError per HTTP status class, RateLimitError for 429 carrying Retry-After.
SSE parsing follows the WHATWG EventSource rules used by the OpenAI stream format
(`data: {...}` lines, blank line ends an event, `data: [DONE]` ends the stream).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


@dataclass
class Response:
    status: int
    headers: dict
    body: Any = None            # parsed JSON, text, or None for streams
    raw: bytes = b""
    lines: Optional[Iterator[str]] = field(default=None, repr=False)

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        low = name.lower()
        for k, v in self.headers.items():
            if k.lower() == low:
                return v
        return default


class NeoCloudError(Exception):
    """Base error. `status`/`body`/`headers` are set for HTTP errors."""

    def __init__(self, message: str, status: Optional[int] = None, body: Any = None,
                 headers: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.headers = headers or {}

    @property
    def request_id(self) -> Optional[str]:
        for k, v in self.headers.items():
            if k.lower() in ("x-request-id", "request-id"):
                return v
        return None


class APIConnectionError(NeoCloudError):
    pass


class APIStatusError(NeoCloudError):
    pass


class BadRequestError(APIStatusError):           # 400
    pass


class AuthenticationError(APIStatusError):       # 401
    pass


class PermissionDeniedError(APIStatusError):     # 403
    pass


class NotFoundError(APIStatusError):             # 404
    pass


class ConflictError(APIStatusError):             # 409
    pass


class UnprocessableEntityError(APIStatusError):  # 422
    pass


class RateLimitError(APIStatusError):            # 429
    @property
    def retry_after(self) -> Optional[float]:
        for k, v in self.headers.items():
            if k.lower() == "retry-after":
                try:
                    return float(v)
                except ValueError:
                    return None
        return None


class InternalServerError(APIStatusError):       # >= 500
    pass


_BY_STATUS = {400: BadRequestError, 401: AuthenticationError, 403: PermissionDeniedError,
              404: NotFoundError, 409: ConflictError, 422: UnprocessableEntityError,
              429: RateLimitError}


def _message(body: Any, status: int) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return f"{status}: {err['message']}"          # OpenAI error envelope
        if isinstance(err, str):
            return f"{status}: {err}: {body.get('error_description', '')}".rstrip(": ")  # RFC 6749
        if body.get("detail") is not None:
            return f"{status}: {body['detail']}"           # FastAPI
    return f"HTTP {status}"


def raise_for_status(resp: Response) -> None:
    if resp.status < 400:
        return
    cls = _BY_STATUS.get(resp.status, InternalServerError if resp.status >= 500 else APIStatusError)
    raise cls(_message(resp.body, resp.status), resp.status, resp.body, resp.headers)


def _ctype(headers: dict) -> str:
    for k, v in headers.items():
        if k.lower() == "content-type":
            return v
    return ""


def _parse(raw: bytes, ctype: str) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    if "json" in ctype:
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


class UrllibTransport:
    def __init__(self, timeout: float = 900.0):   # a 4-rack release waits for NICo cleanup
        self.timeout = timeout

    def send(self, method: str, url: str, headers: dict, body: Optional[bytes],
             stream: bool = False) -> Response:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=self.timeout)  # noqa: S310 (caller-chosen URL)
        except urllib.error.HTTPError as e:
            raw = e.read() or b""
            hdr = dict(e.headers.items())
            return Response(e.code, hdr, _parse(raw, _ctype(hdr)), raw)
        except (urllib.error.URLError, OSError) as e:
            raise APIConnectionError(f"connection error: {e}") from e
        hdr = dict(r.headers.items())
        if stream and r.status < 400:
            def lines(resp=r):
                with resp:
                    for b in resp:
                        yield b.decode("utf-8", "replace").rstrip("\r\n")
            return Response(r.status, hdr, None, b"", lines())
        with r:
            raw = r.read()
        return Response(r.status, hdr, _parse(raw, _ctype(hdr)), raw)


class TestClientTransport:
    """In-process transport over a starlette TestClient (tests, demo runner)."""

    __test__ = False  # not a pytest class

    def __init__(self, test_client):
        self.tc = test_client

    def send(self, method: str, url: str, headers: dict, body: Optional[bytes],
             stream: bool = False) -> Response:
        parts = urllib.parse.urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        r = self.tc.request(method, path, content=body, headers=headers)
        hdr = dict(r.headers.items())
        if stream and r.status_code < 400:
            return Response(r.status_code, hdr, None, r.content, iter(r.text.splitlines()))
        return Response(r.status_code, hdr, _parse(r.content, _ctype(hdr)), r.content)


def iter_sse(lines: Iterator[str]) -> Iterator[dict]:
    """Yield `{"event", "data"}` per SSE event; stops at `data: [DONE]`."""
    event, data = None, []
    for line in lines:
        if line == "":
            if data:
                payload = "\n".join(data)
                if payload.strip() == "[DONE]":
                    return
                yield {"event": event or "message", "data": payload}
            event, data = None, []
            continue
        if line.startswith(":"):
            continue
        name, _, val = line.partition(":")
        val = val[1:] if val.startswith(" ") else val
        if name == "data":
            data.append(val)
        elif name == "event":
            event = val
    if data and "\n".join(data).strip() != "[DONE]":
        yield {"event": event or "message", "data": "\n".join(data)}
