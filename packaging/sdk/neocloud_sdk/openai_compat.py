"""OpenAI-SDK-shaped facade over the NOCP `/v1/*` token API (US-05).

Mirrors the openai-python surface a developer already uses, so moving to NeoCloud
is a base_url change:

    from neocloud_sdk.openai_compat import OpenAI     # or: from openai import OpenAI
    client = OpenAI(base_url="http://nocp:8000/v1", api_key="sk-nc-...")
    for chunk in client.chat.completions.create(model=..., messages=..., stream=True,
                                                stream_options={"include_usage": True}):
        ...

Response objects allow attribute access (`chunk.choices[0].delta.content`,
`resp.usage.prompt_tokens_details.cached_tokens`) like openai-python's pydantic
models; the wire format is the OpenAI spec (ref/upstream/openai/openapi.yaml,
CreateChatCompletionResponse / CreateChatCompletionStreamResponse / CompletionUsage).
Errors use the openai-python names; `RateLimitError.response.headers["retry-after"]`.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

from . import _transport as t
from ._client import BaseClient


class Obj(dict):
    """dict with attribute access, recursively (openai-python model stand-in)."""

    def __getattr__(self, k):
        try:
            return _wrap(self[k])
        except KeyError:
            raise AttributeError(k) from None

    def model_dump(self) -> dict:
        return dict(self)


def _wrap(v):
    if isinstance(v, dict) and not isinstance(v, Obj):
        return Obj(v)
    if isinstance(v, list):
        return [_wrap(x) for x in v]
    return v


class _Resp:
    def __init__(self, status: int, headers: dict):
        self.status_code = status
        self.headers = {k.lower(): v for k, v in headers.items()}


def _reraise(e: t.APIStatusError):
    e.response = _Resp(e.status or 0, e.headers)   # openai-python: err.response.headers
    raise e


APIError = t.NeoCloudError
APIStatusError = t.APIStatusError
APIConnectionError = t.APIConnectionError
AuthenticationError = t.AuthenticationError
BadRequestError = t.BadRequestError
PermissionDeniedError = t.PermissionDeniedError
NotFoundError = t.NotFoundError
RateLimitError = t.RateLimitError
InternalServerError = t.InternalServerError


class Stream:
    """Iterator of ChatCompletionChunk objects; `.response` holds the HTTP headers."""

    def __init__(self, events: Iterator[Any], response: _Resp):
        self._events = events
        self.response = response

    def __iter__(self):
        for ev in self._events:
            yield _wrap(ev)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Completions:
    def __init__(self, c: BaseClient, path: str):
        self._c, self._path = c, path

    def create(self, *, extra_headers: Optional[dict] = None, **params) -> Any:
        stream = bool(params.get("stream"))
        try:
            resp = self._c.request("POST", self._path, json_body=params,
                                   headers=extra_headers, stream=stream)
        except t.APIStatusError as e:
            _reraise(e)
        if stream:
            return Stream(BaseClient._events(resp), _Resp(resp.status, resp.headers))
        return _wrap(resp.body)


class _Chat:
    def __init__(self, c: BaseClient):
        self.completions = _Completions(c, "/v1/chat/completions")


class _Models:
    def __init__(self, c: BaseClient):
        self._c = c

    def list(self) -> Any:
        try:
            return _wrap(self._c.request("GET", "/v1/models").body)
        except t.APIStatusError as e:
            _reraise(e)


class _Embeddings:
    def __init__(self, c: BaseClient):
        self._c = c

    def create(self, **params) -> Any:
        try:
            return _wrap(self._c.request("POST", "/v1/embeddings", json_body=params).body)
        except t.APIStatusError as e:
            _reraise(e)


class OpenAI:
    def __init__(self, *, base_url: str, api_key: str, transport=None, max_retries: int = 0,
                 default_headers: Optional[dict] = None):
        root = base_url.rstrip("/")
        root = root[: -len("/v1")] if root.endswith("/v1") else root
        self._c = BaseClient(root, api_key=api_key, token="", operator=False, tenant_id="",
                             transport=transport, max_retries=max_retries,
                             default_headers=default_headers)
        self.base_url = root + "/v1"
        self.chat = _Chat(self._c)
        self.completions = _Completions(self._c, "/v1/completions")
        self.models = _Models(self._c)
        self.embeddings = _Embeddings(self._c)
