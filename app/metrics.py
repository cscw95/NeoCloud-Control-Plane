"""Lightweight in-process API-call metrics for the /flow verification console.

A request-counter middleware maps every HTTP request path to a NeoCloud
Control-Plane module (cp-*/d-*) and keeps per-module cumulative totals plus a
60-second sliding window for EPS (calls/sec). Purely in-memory and thread-safe;
the per-request cost is O(1) — one counter increment and a timestamp append.

Exposed via ``GET /api/v1/integration/module-stats`` so the module-architecture
strip on /flow can render live call badges + activity lighting.

In production this maps onto the observability pipeline (cp-obs / M8): the
per-module counters become request-rate metrics scraped from the API gateway.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

# The 14 Control-Plane modules — ids match /arch `data-b` and /flow `data-m`.
# Order follows the request flow rendered on the module-architecture strip.
MODULES = [
    "cp-api", "cp-intake", "cp-fulfill", "cp-policy", "cp-model",
    "cp-provision", "cp-obs", "cp-sla", "d-compute", "d-sdn",
    "d-storage", "d-shared", "cp-delivery", "cp-reclaim",
]

WINDOW_S = 60.0


def modules_for_path(path: str) -> list[str]:
    """Map a request path to the Control-Plane module(s) it exercises.

    Prefix-based; a request may touch more than one module (e.g. an order goes
    through both Service-Order Intake and Tenant Fulfillment). Unmapped paths
    fall through to ``["other"]`` so nothing is silently dropped.
    """
    p = (path or "/").rstrip("/") or "/"

    # ── Northbound API routes ────────────────────────────────────────────
    if p.startswith("/api/v1/orders") or p.startswith("/api/v1/k8s"):
        return ["cp-intake", "cp-fulfill"]
    if p.startswith("/api/v1/tenants"):
        return ["cp-intake"]
    if (p.startswith("/api/v1/fabric") or p.startswith("/api/v1/network")
            or p.startswith("/api/v1/nvlink")):
        return ["d-sdn"]
    if p.startswith("/api/v1/reconcile"):
        return ["cp-policy"]
    if p.startswith("/api/v1/billing"):
        return ["cp-sla"]
    if p.startswith("/api/v1/tickets"):
        return ["cp-delivery"]
    if (p.startswith("/api/v1/racks") or p.startswith("/api/v1/nodes")
            or p.startswith("/api/v1/cpu-nodes")
            or p.startswith("/api/v1/scalable-units")
            or p.startswith("/api/v1/allocations")
            or p.startswith("/api/v1/nvlink-partitions")
            or p.startswith("/api/v1/factories")
            or p.startswith("/api/v1/trays")
            or p.startswith("/api/v1/inventory")
            or p.startswith("/api/v1/blueprints")
            or p.startswith("/api/v1/equipment")):
        return ["cp-provision"]
    if (p.startswith("/api/v1/integration") or p.startswith("/api/v1/trace")
            or p.startswith("/api/v1/emu") or p.startswith("/api/v1/topology")):
        return ["cp-obs"]

    # ── Southbound domain fakes (Compute/Storage/Shared) ─────────────────
    if p.startswith("/fake-nico"):
        return ["d-compute"]
    if p.startswith("/fake-vast"):
        return ["d-storage"]
    if p.startswith("/fake-shared"):
        return ["d-shared"]

    # ── Portals / public API surface (static consoles + admin) ───────────
    if (p == "/" or p == "/health" or p.startswith("/static")
            or p.startswith("/api/v1/admin")
            or p in ("/customer", "/ops", "/biz", "/arch", "/flow", "/nico",
                     "/docs", "/openapi.json")):
        return ["cp-api"]

    return ["other"]


# Trace bus src → module, for supplementary lighting of purely-internal modules
# (Tenant Fulfillment saga, Resource & Service Model mirror, etc.) that emit
# pipeline events but receive little/no direct HTTP traffic.
def module_for_trace_src(src: str) -> str | None:
    if not src:
        return None
    if src.startswith("NeoCloudOS.M1") or src.startswith("NeoCloudOS.M6"):
        return "cp-fulfill"
    if src.startswith("NeoCloudOS.M3"):
        return "cp-model"
    if src.startswith("NeoCloudOS.M4"):
        return "cp-policy"
    if src.startswith("NeoCloudOS.M5"):
        return "cp-obs"
    if src.startswith("NeoCloudOS.D1"):
        return "cp-provision"
    if src.startswith("NeoCloudOS.D2"):
        return "d-sdn"
    if src.startswith("NeoCloudOS.D4"):
        return "d-storage"
    if src.startswith("Portal/API") or src.startswith("Operator"):
        return "cp-api"
    if src.startswith("NICo"):
        return "d-compute"
    return None


class MetricsCollector:
    """Thread-safe per-module HTTP request counters + 60s sliding EPS windows."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total: dict[str, int] = defaultdict(int)   # module -> cumulative
        self._win: dict[str, deque] = defaultdict(deque)  # module -> timestamps
        self._last: dict[str, float] = {}                # module -> last epoch
        self._grand_total = 0
        self._grand_win: deque = deque()

    def record(self, path: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        mods = modules_for_path(path)
        with self._lock:
            self._grand_total += 1
            self._grand_win.append(now)
            for m in mods:
                self._total[m] += 1
                self._win[m].append(now)
                self._last[m] = now

    @staticmethod
    def _eps(dq: deque, now: float) -> float:
        cut = now - WINDOW_S
        while dq and dq[0] < cut:
            dq.popleft()
        return round(len(dq) / WINDOW_S, 3)

    def snapshot(self, now: float | None = None):
        """Return (per-module dict, grand_total_calls, grand_eps)."""
        now = time.time() if now is None else now
        with self._lock:
            mods = {}
            for m in MODULES:
                last = self._last.get(m)
                mods[m] = {
                    "id": m,
                    "calls": self._total.get(m, 0),
                    "eps": self._eps(self._win[m], now),
                    "last_active_s": (round(now - last, 1)
                                      if last is not None else None),
                }
            grand_eps = self._eps(self._grand_win, now)
            return mods, self._grand_total, grand_eps

    def reset(self) -> None:
        with self._lock:
            self._total.clear()
            self._win.clear()
            self._last.clear()
            self._grand_total = 0
            self._grand_win.clear()


# process-wide singleton
METRICS = MetricsCollector()
