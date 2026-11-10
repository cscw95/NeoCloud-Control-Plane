"""F02 — token API quotas and rate limiting (TPM / RPM token buckets).

  GET  /api/v1/token/quotas?tenant_id=              per tenant × tier rows
  PUT  /api/v1/token/quotas/{tenant_id}             {tier, tpm, rpm, monthly_token_cap?, key_id?}
  POST /api/v1/token/quotas/{tenant_id}/override    {tier, factor, until, reason}
  DELETE /api/v1/token/quotas/{tenant_id}/override?tier=

Buckets are kept per tenant × tier (interactive | batch) and, when a key-level
limit was PUT with `key_id`, also per key × tier. In memory; `BucketStore` is
the Redis adapter boundary (one get/put per bucket).

Upstream flow (WAVE-v2 "원본 충실도", UPSTREAM-REFS §3.2 steps 2 and 6):
  envoyproxy/ai-gateway @11e8b24b0d2d site/docs/capabilities/traffic/
  usage-based-ratelimiting.md and api/v1alpha1/quota_policy.go — the request
  is admitted while the bucket still has budget (hits_addend 0 on the request
  path), the real token cost (`llm_total_token`) is deducted when the response
  or stream ends, and the *next* request is answered 429. Requests are counted
  on admission. The bucket may go negative after a large response; refill pays
  the debt off before the next admission.
Wire format: openai/openai-openapi @c6522b4e0c48 — 429 body
  `{error:{message, type:"rate_limit_exceeded", param, code}}` (code
  `rate_limit_exceeded`, or `insufficient_quota` for the monthly cap) and the
  OpenAI rate-limit headers x-ratelimit-{limit,remaining,reset}-{requests,tokens}
  with reset durations like "1s", "6m0s", "120ms", plus Retry-After (seconds).

Gateway seam (F01, app/token_api.py): this module registers QUOTA through
token_api.set_quota_hook() — interface check(tenant_id, key_id, tier,
est_tokens) -> (ok, retry_after_s, headers), commit(tenant_id, key_id, tier,
tokens), reset(). Curtailment seam (F21, app/curtailment.py):
apply_override(tenant_id, tier, factor, until, reason) / clear_override().
"""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .store import STORE

TIERS = ("interactive", "batch")
# defaults for a tenant without a PUT (per tier)
DEFAULTS = {"interactive": {"tpm": 1_000_000, "rpm": 600, "monthly_token_cap": None},
            "batch": {"tpm": 2_000_000, "rpm": 300, "monthly_token_cap": None}}

router = APIRouter(prefix="/api/v1/token", tags=["token-quota"])


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _parse_ts(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _month(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


def _next_month(ts: float) -> float:
    d = datetime.fromtimestamp(ts, timezone.utc)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return datetime(y, m, 1, tzinfo=timezone.utc).timestamp()


def reset_duration(seconds: float) -> str:
    """OpenAI x-ratelimit-reset-* format: "120ms", "1s", "6m0s", "1h2m3s"."""
    if seconds <= 0:
        return "0s"
    if seconds < 1:
        return f"{max(1, int(round(seconds * 1000)))}ms"
    s = int(math.ceil(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m}m{sec}s"
    if m:
        return f"{m}m{sec}s"
    return f"{sec}s"


# ---------------------------------------------------------------------------
# bucket store (Redis adapter boundary)
# ---------------------------------------------------------------------------
class BucketStore:
    """In-memory bucket state: key -> {"tokens", "requests", "at"}.
    A Redis adapter would implement get/put with the same dict shape."""

    def __init__(self):
        self.d: dict[tuple, dict] = {}

    def get(self, key: tuple) -> Optional[dict]:
        return self.d.get(key)

    def put(self, key: tuple, val: dict) -> None:
        self.d[key] = val

    def clear(self) -> None:
        self.d.clear()


class QuotaEngine:
    def __init__(self, clock: Callable[[], float] = time.time,
                 buckets: Optional[BucketStore] = None):
        self.clock = clock                  # injectable for tests (epoch seconds)
        self.lock = threading.RLock()
        self.buckets = buckets or BucketStore()
        self.monthly: dict[tuple, int] = {}          # (tenant, tier, YYYY-MM) -> tokens
        self.denials: list[dict] = []                # recent 429s (ops view)
        self.stats: dict[tuple, dict] = {}           # (tenant, tier) -> counters

    # -- config ---------------------------------------------------------------
    @staticmethod
    def _cfg_table() -> dict:
        t = getattr(STORE, "token_quotas", None)
        if t is None:
            t = {}
            setattr(STORE, "token_quotas", t)
        return t

    @staticmethod
    def _ovr_table() -> dict:
        t = getattr(STORE, "token_quota_overrides", None)
        if t is None:
            t = {}
            setattr(STORE, "token_quota_overrides", t)
        return t

    def base(self, tenant_id: str, tier: str, key_id: Optional[str] = None) -> dict:
        c = self._cfg_table().get((tenant_id, tier, key_id))
        if c is None and key_id is None:
            return {**DEFAULTS[tier], "source": "default"}
        return c

    def override(self, tenant_id: str, tier: str) -> Optional[dict]:
        o = self._ovr_table().get((tenant_id, tier))
        if o is None:
            return None
        if self.clock() >= o["until_ts"]:          # expired → restore
            self._ovr_table().pop((tenant_id, tier), None)
            return None
        return o

    def effective(self, tenant_id: str, tier: str, key_id: Optional[str] = None) -> Optional[dict]:
        b = self.base(tenant_id, tier, key_id)
        if b is None:
            return None
        o = self.override(tenant_id, tier)
        f = o["factor"] if o else 1.0
        return {"tpm": max(0, int(b["tpm"] * f)), "rpm": max(0, int(b["rpm"] * f)),
                "monthly_token_cap": b.get("monthly_token_cap"), "factor": f}

    # -- buckets ----------------------------------------------------------------
    def _bucket(self, key: tuple, lim: dict, now: float) -> dict:
        """Refill a bucket to `now` (continuous refill, capacity = per-minute limit)."""
        b = self.buckets.get(key)
        if b is None:
            b = {"tokens": float(lim["tpm"]), "requests": float(lim["rpm"]), "at": now}
        dt = max(0.0, now - b["at"])
        b = {"tokens": min(float(lim["tpm"]), b["tokens"] + dt * lim["tpm"] / 60.0),
             "requests": min(float(lim["rpm"]), b["requests"] + dt * lim["rpm"] / 60.0),
             "at": now}
        self.buckets.put(key, b)
        return b

    @staticmethod
    def _wait(level: float, need: float, per_min: int) -> float:
        """Seconds until `level` reaches `need` at per_min/60 refill."""
        if level >= need:
            return 0.0
        if per_min <= 0:
            return 60.0
        return (need - level) * 60.0 / per_min

    def _scopes(self, tenant_id: str, key_id: Optional[str], tier: str) -> list[tuple]:
        out = [((tenant_id, tier, None), self.effective(tenant_id, tier))]
        if key_id:
            kl = self.effective(tenant_id, tier, key_id)
            if kl is not None:
                out.append(((tenant_id, tier, key_id), kl))
        return out

    def check(self, tenant_id: str, key_id: str, tier: str, est_tokens: int = 0):
        """Admission (gateway F01 calls it before routing)."""
        tier = tier if tier in TIERS else "interactive"
        now = self.clock()
        with self.lock:
            scopes = self._scopes(tenant_id, key_id, tier)
            states = [(k, lim, self._bucket(k, lim, now)) for k, lim in scopes]
            # headers describe the tightest scope (lowest remaining tokens)
            k0, lim0, b0 = min(states, key=lambda s: (s[2]["tokens"], s[2]["requests"]))
            reason, retry = None, 0.0
            for _k, lim, b in states:
                rw = self._wait(b["requests"], 1.0, lim["rpm"])
                tw = self._wait(b["tokens"], 1.0, lim["tpm"])
                if rw > 0 and rw >= tw:
                    reason, retry = reason or "requests", max(retry, rw)
                elif tw > 0:
                    reason, retry = reason or "tokens", max(retry, tw)
            cap = lim0.get("monthly_token_cap")
            used = self.monthly.get((tenant_id, tier, _month(now)), 0)
            if reason is None and cap is not None and used >= cap:
                reason, retry = "monthly_cap", _next_month(now) - now
            ok = reason is None
            if ok:
                for k, _lim, b in states:
                    b["requests"] -= 1.0
                    self.buckets.put(k, b)
            hdr = self._headers(lim0, b0)
            if not ok:
                hdr["retry-after"] = str(max(1, int(math.ceil(retry))))
                hdr["x-neocloud-ratelimit-reason"] = reason
                hdr["x-neocloud-ratelimit-code"] = ("insufficient_quota" if reason == "monthly_cap"
                                                    else "rate_limit_exceeded")
                self.denials.append({"tenant_id": tenant_id, "key_id": key_id, "tier": tier,
                                     "reason": reason, "retry_after_s": int(hdr["retry-after"]),
                                     "at": _iso(now)})
                del self.denials[:-200]
            st = self.stats.setdefault((tenant_id, tier), {"admitted": 0, "rejected": 0})
            st["admitted" if ok else "rejected"] += 1
            return ok, (0 if ok else int(hdr["retry-after"])), hdr

    def commit(self, tenant_id: str, key_id: str, tier: str, tokens: int) -> None:
        """Actual cost at response / stream end (Envoy llm_total_token)."""
        tier = tier if tier in TIERS else "interactive"
        now = self.clock()
        with self.lock:
            for k, lim in self._scopes(tenant_id, key_id, tier):
                b = self._bucket(k, lim, now)
                b["tokens"] -= float(tokens)
                self.buckets.put(k, b)
            mk = (tenant_id, tier, _month(now))
            self.monthly[mk] = self.monthly.get(mk, 0) + int(tokens)

    @staticmethod
    def _headers(lim: dict, b: dict) -> dict:
        rr = max(0, int(math.floor(b["requests"])))
        rt = max(0, int(math.floor(b["tokens"])))
        return {"x-ratelimit-limit-requests": str(lim["rpm"]),
                "x-ratelimit-limit-tokens": str(lim["tpm"]),
                "x-ratelimit-remaining-requests": str(rr),
                "x-ratelimit-remaining-tokens": str(rt),
                "x-ratelimit-reset-requests": reset_duration(
                    QuotaEngine._wait(b["requests"], lim["rpm"], lim["rpm"])),
                "x-ratelimit-reset-tokens": reset_duration(
                    QuotaEngine._wait(b["tokens"], lim["tpm"], lim["tpm"]))}

    # -- views ------------------------------------------------------------------
    def row(self, tenant_id: str, tier: str, key_id: Optional[str] = None) -> dict:
        now = self.clock()
        with self.lock:
            base = self.base(tenant_id, tier, key_id)
            eff = self.effective(tenant_id, tier, key_id)
            b = self._bucket((tenant_id, tier, key_id), eff, now)
            used = self.monthly.get((tenant_id, tier, _month(now)), 0)
            o = self.override(tenant_id, tier) if key_id is None else None
            st = self.stats.get((tenant_id, tier), {"admitted": 0, "rejected": 0})
            rt = max(0, int(b["tokens"]))
            cap = base.get("monthly_token_cap")
            return {"tenant_id": tenant_id, "tier": tier, "key_id": key_id,
                    "tpm": base["tpm"], "rpm": base["rpm"], "monthly_token_cap": cap,
                    "source": base.get("source", "configured"),
                    "effective": {"tpm": eff["tpm"], "rpm": eff["rpm"], "factor": eff["factor"]},
                    "override": ({k: o[k] for k in ("factor", "until", "reason", "created_at")}
                                 if o else None),
                    "remaining": {"tokens": rt, "requests": max(0, int(b["requests"]))},
                    "utilization_pct": round(100.0 * (1 - rt / eff["tpm"]), 1) if eff["tpm"] else 100.0,
                    "month": {"period": _month(now), "tokens": used,
                              "cap_remaining": None if cap is None else max(0, cap - used)},
                    "admitted": st["admitted"], "rejected": st["rejected"]}

    def tenants(self) -> list[str]:
        ids = set(STORE.tenants.keys())
        ids |= {k[0] for k in self._cfg_table()}
        ids |= {k[0] for k in self._ovr_table()}
        ids |= {k[0] for k in self.stats}
        return sorted(ids)

    def reset(self) -> None:
        with self.lock:
            self.buckets.clear()
            self.monthly.clear()
            self.denials.clear()
            self.stats.clear()
            self._cfg_table().clear()
            self._ovr_table().clear()


QUOTA = QuotaEngine()


def error_body(headers: dict) -> dict:
    """OpenAI ErrorResponse for a 429 from check() headers."""
    code = headers.get("x-neocloud-ratelimit-code", "rate_limit_exceeded")
    reason = headers.get("x-neocloud-ratelimit-reason", "tokens")
    if reason == "monthly_cap":
        msg = "You exceeded your current monthly token quota."
    else:
        msg = (f"Rate limit reached for {reason} per min: limit "
               f"{headers.get('x-ratelimit-limit-' + reason, '?')}. "
               f"Please try again in {headers.get('retry-after', '1')}s.")
    return {"error": {"message": msg, "type": "rate_limit_exceeded" if code == "rate_limit_exceeded"
                      else "insufficient_quota", "param": None, "code": code}}


def reset() -> None:
    QUOTA.reset()


# ---------------------------------------------------------------------------
# config API (python seams used by F21 and the REST layer)
# ---------------------------------------------------------------------------
def set_quota(tenant_id: str, tier: str, tpm: int, rpm: int,
              monthly_token_cap: Optional[int] = None, key_id: Optional[str] = None) -> dict:
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}")
    with QUOTA.lock:
        QUOTA._cfg_table()[(tenant_id, tier, key_id)] = {
            "tpm": int(tpm), "rpm": int(rpm), "monthly_token_cap": monthly_token_cap,
            "source": "configured", "updated_at": _iso(QUOTA.clock())}
        QUOTA.buckets.d.pop((tenant_id, tier, key_id), None)   # new limit starts full
    return QUOTA.row(tenant_id, tier, key_id)


def apply_override(tenant_id: str, tier: str, factor: float, until, reason: str = "") -> dict:
    """Scale tier limits by `factor` until `until` (ISO 8601 or epoch); F21 seam."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}")
    if not 0 <= float(factor) <= 1:
        raise ValueError("factor must be within 0..1")
    until_ts = _parse_ts(until)
    with QUOTA.lock:
        QUOTA._ovr_table()[(tenant_id, tier)] = {
            "factor": float(factor), "until": _iso(until_ts), "until_ts": until_ts,
            "reason": reason, "created_at": _iso(QUOTA.clock())}
        eff = QUOTA.effective(tenant_id, tier)
        for k in [k for k in QUOTA.buckets.d if k[0] == tenant_id and k[1] == tier]:
            b = QUOTA.buckets.d[k]
            b["tokens"] = min(b["tokens"], eff["tpm"])
            b["requests"] = min(b["requests"], eff["rpm"])
    return QUOTA.row(tenant_id, tier)


def clear_override(tenant_id: str, tier: str) -> None:
    with QUOTA.lock:
        QUOTA._ovr_table().pop((tenant_id, tier), None)


# register with the F01 gateway when it is merged
try:                                                  # pragma: no cover - depends on F01
    from . import token_api as _token_api             # type: ignore[attr-defined]
    if hasattr(_token_api, "set_quota_hook"):
        _token_api.set_quota_hook(QUOTA)
except ImportError:
    _token_api = None


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------
def _operator_only(request: Request) -> None:
    who = getattr(request.state, "identity", None)
    if who is not None and getattr(who, "role", None) == "tenant":
        raise HTTPException(403, "operator identity required")


def _tenant_or_404(tenant_id: str) -> None:
    if tenant_id not in STORE.tenants and not any(
            k[0] == tenant_id for k in QUOTA._cfg_table()):
        raise HTTPException(404, f"tenant '{tenant_id}' not found")


class QuotaPut(BaseModel):
    tier: str = Field(..., pattern="^(interactive|batch)$")
    tpm: int = Field(..., ge=0)
    rpm: int = Field(..., ge=0)
    monthly_token_cap: Optional[int] = Field(None, ge=0)
    key_id: Optional[str] = None          # NeoCloud extension: key-level sub-limit


class OverrideIn(BaseModel):
    tier: str = Field(..., pattern="^(interactive|batch)$")
    factor: float = Field(..., ge=0, le=1)
    until: str
    reason: str = ""


@router.get("/quotas")
def list_quotas(tenant_id: Optional[str] = None, tier: Optional[str] = None) -> dict:
    tids = [tenant_id] if tenant_id else QUOTA.tenants()
    rows = []
    for t in tids:
        for tr in TIERS:
            if tier and tr != tier:
                continue
            rows.append(QUOTA.row(t, tr))
            rows += [QUOTA.row(t, tr, k[2]) for k in sorted(QUOTA._cfg_table(), key=str)
                     if k[0] == t and k[1] == tr and k[2]]
    return {"object": "list", "data": rows, "defaults": DEFAULTS,
            "denials": [d for d in QUOTA.denials if not tenant_id or d["tenant_id"] == tenant_id][-20:]}


@router.put("/quotas/{tenant_id}")
def put_quota(tenant_id: str, body: QuotaPut, request: Request) -> dict:
    _operator_only(request)
    _tenant_or_404(tenant_id)
    return set_quota(tenant_id, body.tier, body.tpm, body.rpm, body.monthly_token_cap,
                     body.key_id)


@router.post("/quotas/{tenant_id}/override", status_code=201)
def post_override(tenant_id: str, body: OverrideIn, request: Request) -> dict:
    _operator_only(request)
    _tenant_or_404(tenant_id)
    try:
        until_ts = _parse_ts(body.until)
    except ValueError:
        raise HTTPException(422, "until must be an ISO 8601 date-time")
    if until_ts <= QUOTA.clock():
        raise HTTPException(422, "until must be in the future")
    return apply_override(tenant_id, body.tier, body.factor, until_ts, body.reason)


@router.delete("/quotas/{tenant_id}/override")
def delete_override(tenant_id: str, request: Request,
                    tier: str = Query(..., pattern="^(interactive|batch)$")) -> dict:
    _operator_only(request)
    clear_override(tenant_id, tier)
    return QUOTA.row(tenant_id, tier)
