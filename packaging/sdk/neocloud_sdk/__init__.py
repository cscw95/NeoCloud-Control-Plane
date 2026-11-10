"""NeoCloud Python SDK (F29) — generated from the NOCP OpenAPI document.

    from neocloud_sdk import Client
    nc = Client("http://127.0.0.1:8000", operator=True)
    ctr = nc.contracts.create_contract({"tenant_id": "tnt-a", "racks": 4, ...})
    nc.contracts.activate_contract(ctr["id"])
    order = nc.orders.create_order({"tenant_id": "tnt-a", "kind": "new", "contract_id": ctr["id"]})

Every NOCP operation is a method `client.<namespace>.<handler>()`; see
`neocloud_sdk.OPERATIONS` or `neocloud commands` for the full list.
"""

from __future__ import annotations

from typing import Optional

from ._client import BaseClient
from ._generated import API_TITLE, API_VERSION, OPERATIONS, GeneratedClient
from ._transport import (APIConnectionError, APIStatusError, AuthenticationError,
                         BadRequestError, ConflictError, InternalServerError, NeoCloudError,
                         NotFoundError, PermissionDeniedError, RateLimitError, Response,
                         TestClientTransport, UnprocessableEntityError, UrllibTransport)

__version__ = "0.1.0"


class Client(GeneratedClient):
    def openai_client(self, api_key: Optional[str] = None):
        """OpenAI-SDK-shaped client on this NOCP's `/v1` (US-05)."""
        from .openai_compat import OpenAI
        return OpenAI(base_url=self.base_url + "/v1", api_key=api_key or self.api_key or "",
                      transport=self.transport, max_retries=self.max_retries)

    @classmethod
    def from_oidc(cls, base_url: str, realm: str, client_id: str, client_secret: str,
                  **kw) -> "Client":
        """client_credentials grant on the Keycloak-shaped issuer (F07), then Bearer."""
        anon = cls(base_url, token="", operator=False, **kw)
        # the token endpoint reads a form body (RFC 6749 §4.4), not in the OpenAPI schema
        tok = anon.request("POST", f"/realms/{realm}/protocol/openid-connect/token",
                           form={"grant_type": "client_credentials", "client_id": client_id,
                                 "client_secret": client_secret}).body
        return cls(base_url, token=tok["access_token"], **kw)


__all__ = ["Client", "BaseClient", "OPERATIONS", "API_TITLE", "API_VERSION", "Response",
           "UrllibTransport", "TestClientTransport", "NeoCloudError", "APIStatusError",
           "APIConnectionError", "AuthenticationError", "BadRequestError", "ConflictError",
           "InternalServerError", "NotFoundError", "PermissionDeniedError", "RateLimitError",
           "UnprocessableEntityError"]
