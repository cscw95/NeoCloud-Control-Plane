"""F05 webhook emitter — Lago and Stripe envelopes, signed, with a delivery log.

Upstream shapes (URL only, see ref/upstream/UPSTREAM-REFS.md):
  - Lago  getlago/lago-openapi@7d237882f6ef openapi.yaml `webhooks:` — body
    `{webhook_type, object_type, organization_id, <object_type>: {...}}`,
    headers `X-Lago-Signature`, `X-Lago-Signature-Algorithm` (hmac|jwt),
    `X-Lago-Unique-Key`. HMAC = base64(HMAC-SHA256(hmac_key, raw body)).
    Only the hmac algorithm is emulated (jwt needs Lago's RSA key pair).
  - Stripe stripe/openapi@f1ec696a9d04 latest/openapi.spec3.yaml `event` —
    `{id, object:"event", api_version, created, data:{object}, livemode,
    pending_webhooks, request:{id, idempotency_key}, type}`, header
    `Stripe-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>`.

Every webhook is recorded in STORE.billing_webhooks (newest last) with the raw
headers and body, so the demo shows what went on the wire. Each registered
endpoint gets a delivery: `internal://nocp/...` URLs (the default receivers,
NOCP itself) are delivered in-process; http(s) URLs are POSTed on a daemon
thread (2 s timeout, no retry — Lago/Stripe retry is out of scope).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from .store import STORE

LAGO_ORG_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "neocloud/lago/organization"))
LAGO_HMAC_KEY = "lago_hmac_neocloud_test"          # emulated org hmac key
STRIPE_API_VERSION = "2026-10-28.endive"           # spec v2599
STRIPE_WEBHOOK_SECRET = "whsec_neocloud_test"      # default endpoint secret
INTERNAL_LAGO = "internal://nocp/api/v1/billing/webhooks/lago"
INTERNAL_STRIPE = "internal://nocp/api/v1/billing/webhooks/stripe"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _body(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def lago_signature(raw: str, key: str = LAGO_HMAC_KEY) -> str:
    return base64.b64encode(hmac.new(key.encode(), raw.encode(),
                                     hashlib.sha256).digest()).decode()


def verify_lago(raw: str, signature: str, key: str = LAGO_HMAC_KEY) -> bool:
    return hmac.compare_digest(lago_signature(raw, key), signature or "")


def stripe_signature(raw: str, t: int, secret: str = STRIPE_WEBHOOK_SECRET) -> str:
    v1 = hmac.new(secret.encode(), f"{t}.{raw}".encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


def verify_stripe(raw: str, header: str, secret: str = STRIPE_WEBHOOK_SECRET) -> bool:
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    if "t" not in parts or "v1" not in parts:
        return False
    return hmac.compare_digest(stripe_signature(raw, int(parts["t"]), secret),
                               f"t={parts['t']},v1={parts['v1']}")


# ---------------------------------------------------------------------------
# endpoints (Lago POST /webhook_endpoints, Stripe POST /v1/webhook_endpoints)
# ---------------------------------------------------------------------------
def endpoints(provider: str) -> dict:
    eps = STORE.billing_webhook_endpoints.setdefault(provider, {})
    if not eps:                                    # NOCP is the default receiver
        if provider == "lago":
            eid = str(uuid.uuid5(uuid.NAMESPACE_URL, INTERNAL_LAGO))
            eps[eid] = {"lago_id": eid, "lago_organization_id": LAGO_ORG_ID,
                        "webhook_url": INTERNAL_LAGO, "signature_algo": "hmac",
                        "name": "NeoCloud NOCP", "event_types": None,
                        "created_at": _now().isoformat()}
        else:
            eps["we_nocp_default"] = {
                "id": "we_nocp_default", "object": "webhook_endpoint",
                "api_version": STRIPE_API_VERSION, "application": None,
                "created": int(_now().timestamp()), "description": "NeoCloud NOCP",
                "enabled_events": ["*"], "livemode": False, "metadata": {},
                "secret": STRIPE_WEBHOOK_SECRET, "status": "enabled",
                "url": INTERNAL_STRIPE}
    return eps


def _wants(ep: dict, provider: str, kind: str) -> bool:
    if provider == "lago":
        types = ep.get("event_types")
        return not types or kind in types or "*" in types
    if ep.get("status") != "enabled":
        return False
    ev = ep.get("enabled_events") or []
    return "*" in ev or kind in ev


def _deliver(rec: dict, url: str, headers: dict, raw: str) -> None:
    d = {"url": url, "status": None, "error": None}
    rec["deliveries"].append(d)
    if url.startswith("internal://"):
        d["status"] = 200                          # NOCP receiver, verified below
        d["verified"] = (verify_lago(raw, headers.get("X-Lago-Signature", ""))
                         if rec["provider"] == "lago" else
                         verify_stripe(raw, headers.get("Stripe-Signature", "")))
        return

    def _post():
        try:
            import httpx
            r = httpx.post(url, content=raw.encode(), timeout=2.0,
                           headers={**headers, "Content-Type": "application/json"})
            d["status"] = r.status_code
        except Exception as e:                     # noqa: BLE001 — best effort
            d["error"] = str(e)[:200]

    threading.Thread(target=_post, daemon=True).start()


def _record(provider: str, kind: str, headers: dict, body: dict, raw: str,
            refs: dict) -> dict:
    rec = {"seq": STORE.next_seq("billing_webhook"), "provider": provider,
           "type": kind, "at": _now().isoformat(), "headers": headers,
           "body": body, "raw": raw, "refs": refs, "deliveries": []}
    STORE.billing_webhooks.append(rec)
    return rec


def emit_lago(webhook_type: str, object_type: str, obj: dict, **refs) -> dict:
    body = {"webhook_type": webhook_type, "object_type": object_type,
            "organization_id": LAGO_ORG_ID, object_type: obj}
    raw = _body(body)
    headers = {"X-Lago-Signature": lago_signature(raw),
               "X-Lago-Signature-Algorithm": "hmac",
               "X-Lago-Unique-Key": str(uuid.uuid4())}
    rec = _record("lago", webhook_type, headers, body, raw, refs)
    for ep in list(endpoints("lago").values()):
        if _wants(ep, "lago", webhook_type):
            _deliver(rec, ep["webhook_url"], headers, raw)
    return rec


def emit_stripe(event_type: str, obj: dict, idempotency_key: Optional[str] = None,
                **refs) -> dict:
    eps = [ep for ep in endpoints("stripe").values()
           if _wants(ep, "stripe", event_type)]
    t = int(_now().timestamp())
    event = {"id": f"evt_{STORE.next_seq('stripe_event'):014d}", "object": "event",
             "api_version": STRIPE_API_VERSION, "created": t,
             "data": {"object": obj}, "livemode": False,
             "pending_webhooks": len(eps),
             "request": {"id": None, "idempotency_key": idempotency_key},
             "type": event_type}
    STORE.stripe.setdefault("events", {})[event["id"]] = event
    raw = _body(event)
    rec = _record("stripe", event_type, {}, event, raw, refs)
    for ep in eps:
        headers = {"Stripe-Signature": stripe_signature(raw, t, ep["secret"])}
        rec["headers"] = headers
        _deliver(rec, ep["url"], headers, raw)
    return rec


def deliveries(provider: Optional[str] = None, **refs) -> list[dict]:
    out = []
    for r in STORE.billing_webhooks:
        if provider and r["provider"] != provider:
            continue
        if any(v is not None and r["refs"].get(k) != v for k, v in refs.items()):
            continue
        out.append(r)
    return out
