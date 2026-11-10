"""NOCP application entrypoint.

Run:  uvicorn app.main:app --reload
Dashboard:  http://127.0.0.1:8000/
API docs:   http://127.0.0.1:8000/docs
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import asyncio

from . import (
    __version__,
    auth,
    business,
    fabric,
    integration,
    k8s_api,
    lifecycle,
    metrics,
    nico_fake,
    scenario_api,
    customer_ops,
    dsx,
    events,
    ncp,
    nvke,
    oob,
    power,
    shared_services,
    tenancy,
    topology,
    trace,
    tray_emu,
    vast_fake,
)
from .seed import seed_default, seed_demo_samples
from .store import STORE

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    fresh = not STORE.factories
    if fresh:                      # seed once on startup (Phase-1 demo)
        seed_default(STORE)
    # fake NICo/VAST mirror the topology (Day-0 done) + node bootstrap; all
    # idempotent so tests that pre-seed STORE get a matching fresh mirror.
    nico_fake.FAKE_NICO.seed_from_store(STORE)
    vast_fake.FAKE_VAST.reset()
    shared_services.SHARED.reset()
    trace.TRACER.clear()
    nvke.NVKE.reset()
    power.POWER.reset()
    events.BUS.reset()
    oob.OOB.reset()
    ncp.RMA.reset()
    metrics.METRICS.reset()        # reset real-time API call counters
    lifecycle.bootstrap_nodes()
    # compute-tray emulation — background tick (2s)
    tray_emu.EMULATOR.reset()
    tray_emu.EMULATOR.sync_from_store()
    if fresh:                      # sample faults/tickets only in the demo seed
        seed_demo_samples(STORE)
    ticker = asyncio.create_task(tray_emu.EMULATOR.run_loop())
    # DSX: register for AI-Infra Redfish EventService pushes (best effort, off-thread)
    import os
    import threading
    if os.environ.get("NOCP_OOB_AUTOSUBSCRIBE", "1") == "1" and not dsx.offline():
        threading.Thread(target=oob.subscribe, daemon=True).start()
    yield
    ticker.cancel()


app = FastAPI(
    title="NOCP — NeoCloud OS Control Plane",
    version=__version__,
    description="NeoCloud control-plane MVP for NVIDIA Vera Rubin NVL72 "
                "GPU clusters (DSX AI Factory). Domains: Inventory & Topology, "
                "Multi-tenancy & Isolation.",
    lifespan=lifespan,
)

app.include_router(topology.router)
app.include_router(tenancy.router)
app.include_router(lifecycle.router)
app.include_router(k8s_api.router)
app.include_router(trace.router)
app.include_router(business.router)
app.include_router(scenario_api.router)
app.include_router(customer_ops.router)
app.include_router(fabric.router)
app.include_router(tray_emu.router)
from . import readiness as _readiness, slurm as _slurm  # F28 · F31
app.include_router(_readiness.router)
app.include_router(_slurm.router)
app.include_router(nico_fake.router)
app.include_router(vast_fake.router)
app.include_router(integration.router)
app.include_router(shared_services.router)
app.include_router(dsx.router)
app.include_router(auth.router)
app.include_router(nvke.router)
app.include_router(power.router)
app.include_router(events.router)
app.include_router(oob.router)
app.include_router(ncp.router)


# Customer-facing tenant isolation / RBAC — guard based on the X-Tenant-Id/X-Tenant-Role headers.
# Requests without those headers (ops/business consoles, existing tests) are left completely untouched.
# Added before CORS → CORS becomes the outer layer, so CORS headers are attached even to the guard's 403
# (the customer console :8090 needs to read the 403 body).
from .tenancy_guard import TenancyGuardMiddleware  # noqa: E402

app.add_middleware(TenancyGuardMiddleware)

# Allow API integration from the three NeoCloud consoles (separate origin :8090)
import os as _os  # noqa: E402

# CORS locked to configured console origins (NOCP_CORS_ORIGINS, comma list).
CORS_ORIGINS = [o.strip() for o in _os.environ.get(
    "NOCP_CORS_ORIGINS", "http://localhost:8090,http://127.0.0.1:8090").split(",")
    if o.strip() and o.strip() != "*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Tenant-Id", "X-Tenant-Role",
                   "X-Operator", "X-Webhook-Token"],
    allow_credentials=False,
)


@app.middleware("http")
async def count_api_calls(request, call_next):
    """Lightweight request counter — maps the path to a Control-Plane module and tallies it.

    O(1) per request (counter increment + timestamp). Consumed by the /flow module-stats badge."""
    metrics.METRICS.record(request.url.path)
    return await call_next(request)


@app.middleware("http")
async def no_cache_pages(request, call_next):
    """Prevent caching of HTML and static assets — so edits during a demo show up immediately on refresh.

    (An already-open tab only runs its JS periodically, so a browser refresh is needed after a UI overhaul)"""
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ("text/html" in ctype or "javascript" in ctype or "text/css" in ctype):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "seeded_sus": len(STORE.sus)}


@app.post("/api/v1/admin/reseed")
def reseed(blueprints: str | None = None) -> dict:
    """Reseed the factory. `blueprints` = comma list (e.g. 'gb200-nvl72,vr-nvl72')."""
    bps = [b.strip() for b in blueprints.split(",")] if blueprints else None
    seed_default(STORE, blueprints=bps)
    nico_fake.FAKE_NICO.seed_from_store(STORE)
    vast_fake.FAKE_VAST.reset()
    shared_services.SHARED.reset()
    trace.TRACER.clear()
    nvke.NVKE.reset()
    power.POWER.reset()
    events.BUS.reset()
    oob.OOB.reset()
    ncp.RMA.reset()
    lifecycle.bootstrap_nodes()
    tray_emu.EMULATOR.reset()
    _readiness.reset()
    _slurm.reset()
    tray_emu.EMULATOR.sync_from_store()
    if bps is None:                # sample faults/tickets only on the default (demo) reseed
        seed_demo_samples(STORE)
    return {"reseeded": True, "scalable_units": len(STORE.sus),
            "gpus": len(STORE.gpus), "nodes": len(STORE.node_instances)}


# ── Per-system / full reset — used by the verification console (/flow) ──────────
# Each layer holds its in-memory state independently, so if a reset does not
# propagate, cross-layer data (tenants, etc.) drifts out of sync. reset-all resets
# NOCP → NICo Emulator (:9000, cascade=true) → AI Infra (:9100) all at once. The
# external layers are best-effort (reported as unreachable if not running; the
# local reset is kept even on failure).
def _post_json(url: str, timeout: float = 6.0) -> dict:
    import httpx
    try:
        from .dsx import emu_headers
        r = httpx.post(url, timeout=timeout, headers=emu_headers())
        if r.status_code >= 400:
            return {"status": "error", "detail": f"HTTP {r.status_code}"}
        return {"status": "reset", **(r.json() if r.content else {})}
    except Exception as exc:                       # noqa: BLE001 — surface only
        return {"status": "unreachable", "detail": str(exc)[:160]}


def _emu_urls() -> tuple[str, str]:
    import os
    return (os.environ.get("NICO_EMULATOR_URL", "http://127.0.0.1:9000"),
            os.environ.get("AI_INFRA_URL", "http://127.0.0.1:9100"))


@app.post("/api/v1/admin/reset/nico")
def reset_nico() -> dict:
    """Reset only the NICo Emulator (:9000) (proxy — avoids console CORS)."""
    nico, _ = _emu_urls()
    return {"nico_emulator": _post_json(nico + "/emulator/v1/reset")}


@app.post("/api/v1/admin/reset/ai-infra")
def reset_ai_infra() -> dict:
    """Reset only the AI Infra Emulator (:9100) (proxy)."""
    _, ai = _emu_urls()
    return {"ai_infra": _post_json(ai + "/emulator/v1/reset")}


@app.post("/api/v1/admin/reset-all")
def reset_all(blueprints: str | None = None) -> dict:
    """Reset the entire stack — NOCP reseed + NICo Emulator (cascade) + AI Infra.

    If NICo responds, its cascade also resets AI Infra. If NICo is down,
    AI Infra is reset directly so at least the remaining layers stay consistent."""
    nocp = reseed(blueprints)
    nico_url, ai_url = _emu_urls()
    nico = _post_json(nico_url + "/emulator/v1/reset?cascade=true")
    ai = nico.get("ai_infra")
    if not ai or ai.get("status") != "reset":      # NICo path failed → reset directly
        ai = _post_json(ai_url + "/emulator/v1/reset")
    nico.pop("ai_infra", None)
    return {"nocp": nocp, "nico_emulator": nico, "ai_infra": ai}


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/flow")
def flow_console() -> FileResponse:
    """NeoCloud OS × Fake NICo behavior verification console (per-module API flow visualization)."""
    return FileResponse(STATIC_DIR / "flow.html")


@app.get("/nico")
def nico_dashboard() -> FileResponse:
    """NICo operations dashboard — full REST API explorer + live cluster emulation."""
    return FileResponse(STATIC_DIR / "nico.html")


@app.get("/arch")
def architecture_flow() -> FileResponse:
    """Platform architecture flow — per-order-stage highlighting + full list of underlying APIs."""
    return FileResponse(STATIC_DIR / "arch.html")


@app.get("/ops")
def operator_portal() -> FileResponse:
    """Operations portal — infrastructure, incidents, break-fix, ticket handling."""
    return FileResponse(STATIC_DIR / "ops.html")


@app.get("/customer")
def customer_portal() -> FileResponse:
    """Customer portal — tenant-scoped clusters, SLA, self-service."""
    return FileResponse(STATIC_DIR / "customer.html")


@app.get("/biz")
def business_portal() -> FileResponse:
    """Business portal — customers, contracts, service requests, usage/billing."""
    return FileResponse(STATIC_DIR / "biz.html")


# static assets (after routes so "/" resolves to the dashboard)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
