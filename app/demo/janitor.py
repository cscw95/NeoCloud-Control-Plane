"""Demo isolation — every scenario cleans up what its own earlier runs left behind.

Each run gets a short unique id (`ctx["uid"]`) that scenarios put into every name
they create (tenants, signups, business numbers, DCIM serials), so two runs never
collide on a name. What a run held (demo tenants with NVKE clusters or BMaaS
allocations) is remembered per scenario; the next run of the *same* scenario starts
with a `00-cleanup` step that returns those clusters and allocations to the pool
through the real code paths (NVKE delete, terminate order). Other scenarios' state
is never touched, so the run order does not matter. Tenant records stay (history,
invoices); only capacity is reclaimed. `POST /api/v1/demo/reset` forgets all of it.
"""

from __future__ import annotations

import secrets
import threading
from typing import Iterable

_LOCK = threading.Lock()
_THREADS: list[threading.Thread] = []
_OWNED: dict[str, set[str]] = {}          # scenario id -> demo tenant ids its runs used
_PER_RUN: set[str] = set()                 # tenants named with their run's uid (never reused)
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def new_uid() -> str:
    """5 lowercase base36 chars — fits tenant names, slugs and serials."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(5))


def brn_for(uid: str, head: str = "3") -> str:
    """A well-formed business registration number (000-00-00000, valid check digit)
    derived from the run id, so each run signs up with its own number."""
    n = int(uid, 36) % 10**8
    d = [int(head)] + [int(c) for c in f"{n:08d}"]
    w = (1, 3, 7, 1, 3, 7, 1, 3, 5)
    s = sum(a * b for a, b in zip(d, w)) + (d[8] * 5) // 10
    d.append((10 - s % 10) % 10)
    t = "".join(map(str, d))
    return f"{t[:3]}-{t[3:5]}-{t[5:]}"


def is_demo_tenant(tid) -> bool:
    return isinstance(tid, str) and tid.startswith("tnt-") and "demo" in tid


def note(scenario_id: str, tids: Iterable[str], uid: str = "") -> None:
    with _LOCK:
        mine = {t for t in tids if is_demo_tenant(t)}
        _OWNED.setdefault(scenario_id, set()).update(mine)
        if uid:
            _PER_RUN.update(t for t in mine if uid in t)


def tenants_in_ctx(ctx: dict) -> set[str]:
    return {v for v in ctx.values() if is_demo_tenant(v)}


def _holdings(tid: str) -> dict:
    from .. import nvke
    from ..store import STORE
    clusters = [c.id for c in nvke.NVKE.clusters.values()
                if c.tenant_id == tid and c.state != "deleted"]
    allocs = [a.id for a in STORE.allocations.values() if a.tenant_id == tid]
    return {"clusters": clusters, "allocations": allocs}


def leftovers(scenario_id: str) -> dict[str, dict]:
    """{tenant: holdings} for this scenario's earlier runs that still hold capacity."""
    with _LOCK:
        tids = sorted(_OWNED.get(scenario_id, ()))
    out = {}
    for t in tids:
        h = _holdings(t)
        if h["clusters"] or h["allocations"]:
            out[t] = h
    return out


def reclaim(scenario_id: str, which: str = "all") -> dict:
    """Return this scenario's leftover NVKE clusters and BMaaS allocations.
    which: "all" | "per_run" (tenants named with an old uid) | "fixed" (reused names)."""
    from .. import lifecycle, nvke
    from ..models import OrderCreate, OrderKind
    done = {"tenants": [], "clusters": 0, "allocations": 0, "errors": []}
    for tid, h in leftovers(scenario_id).items():
        if which != "all" and (tid in _PER_RUN) != (which == "per_run"):
            continue
        done["tenants"].append(tid)
        for cid in h["clusters"]:
            try:
                nvke.retire_cluster(cid)
                done["clusters"] += 1
            except Exception as exc:              # noqa: BLE001 — report, keep going
                done["errors"].append(f"{cid}: {exc}")
        for aid in h["allocations"]:
            try:
                o = lifecycle.create_order(OrderCreate(tenant_id=tid, kind=OrderKind.terminate,
                                                       allocation_id=aid))
                done["allocations"] += 1
                if o.error:
                    done["errors"].append(f"{aid}: {o.error}")
            except Exception as exc:              # noqa: BLE001
                done["errors"].append(f"{aid}: {exc}")
    return done


_LAST: dict[str, dict] = {}                # scenario id -> last background reclaim result


def start_reclaim(scenario_id: str) -> dict:
    """Reclaim in the background (a live NICo sanitizes for ~37 s per allocation) so a
    rerun starts at once; the new run uses its own names, so it never waits on it."""
    plan = leftovers(scenario_id)
    # a scenario that reuses a fixed tenant name gets that tenant back now (the new run
    # will order on it); only tenants named with an old run's uid go to the background
    fixed = reclaim(scenario_id, "fixed")

    def work():
        _LAST[scenario_id] = reclaim(scenario_id, "per_run")

    name = f"demo-janitor-{scenario_id}"
    t = threading.Thread(target=work, name=name, daemon=True)
    with _LOCK:
        _THREADS[:] = [x for x in _THREADS if x.is_alive()]
        busy = any(x.name == name for x in _THREADS)    # one reclaim per scenario at a time
        if not busy:
            _THREADS.append(t)
    if not busy:
        t.start()
    return {"tenants": sorted(plan), "background": True,
            "clusters": sum(len(h["clusters"]) for h in plan.values()),
            "allocations": sum(len(h["allocations"]) for h in plan.values()),
            "now": fixed, "previous": _LAST.get(scenario_id)}


def wait(timeout: float = 600) -> None:
    """Block until background reclaims finish (demo reset, tests)."""
    with _LOCK:
        ts = list(_THREADS)
    for t in ts:
        t.join(timeout)


def cleanup_step(scenario_id: str) -> dict:
    """Synthetic first step of a rerun (only added when there is something to return)."""

    def call(ctx):
        return start_reclaim(scenario_id)

    def expect(res, ctx):
        b = res.body or {}
        prev = ((b.get("previous") or {}).get("errors") or []) + \
            ((b.get("now") or {}).get("errors") or [])
        ok = not prev
        return (ok,
                f"이전 실행 자원 회수 시작(백그라운드) · 테넌트 {len(b.get('tenants', []))} · NVKE 클러스터 "
                f"{b.get('clusters')} · 할당 {b.get('allocations')}"
                + (f" · 지난 회수 오류 {prev}" if prev else ""),
                f"returning the previous run's capacity (background) · {len(b.get('tenants', []))} "
                f"tenant(s) · {b.get('clusters')} NVKE cluster(s) · {b.get('allocations')} allocation(s)"
                + (f" · last reclaim errors {prev}" if prev else ""))

    return {"id": "00-cleanup", "title_ko": "이전 실행 정리", "title_en": "Clean up the previous run",
            "narration_ko": "같은 시나리오의 이전 실행이 잡고 있던 클러스터·랙을 풀로 돌려놓는다. 다른 시나리오 상태는 건드리지 않는다.",
            "narration_en": "Returns the clusters and racks this scenario's previous run held. Other scenarios are untouched.",
            "call": call, "expect": expect}


def reset() -> None:
    with _LOCK:
        _OWNED.clear()
    _LAST.clear()
    _PER_RUN.clear()
