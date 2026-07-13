"""VRCM application entrypoint.

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
    business,
    fabric,
    integration,
    lifecycle,
    nico_fake,
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
    lifecycle.bootstrap_nodes()
    # 컴퓨트 트레이 에뮬레이션 — 백그라운드 틱 (2s)
    tray_emu.EMULATOR.reset()
    tray_emu.EMULATOR.sync_from_store()
    if fresh:                      # 데모 시드에만 샘플 장애/티켓 포함
        seed_demo_samples(STORE)
    ticker = asyncio.create_task(tray_emu.EMULATOR.run_loop())
    yield
    ticker.cancel()


app = FastAPI(
    title="VRCM — Vera Rubin Cluster Manager",
    version=__version__,
    description="NeoCloud control-plane MVP for NVIDIA Vera Rubin NVL72 "
                "GPU clusters (DSX AI Factory). Domains: Inventory & Topology, "
                "Multi-tenancy & Isolation.",
    lifespan=lifespan,
)

app.include_router(topology.router)
app.include_router(tenancy.router)
app.include_router(lifecycle.router)
app.include_router(trace.router)
app.include_router(business.router)
app.include_router(fabric.router)
app.include_router(tray_emu.router)
app.include_router(nico_fake.router)
app.include_router(vast_fake.router)
app.include_router(integration.router)
app.include_router(shared_services.router)


# NeoCloud 3대 콘솔(별도 오리진 :8090)에서의 API 연동 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8090", "http://localhost:8090"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_pages(request, call_next):
    """HTML·정적 자원 캐시 방지 — 데모 중 편집이 새로고침 즉시 반영되게.

    (열어둔 탭은 JS만 주기 실행하므로 화면 개편 후엔 브라우저 새로고침 필요)"""
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
    lifecycle.bootstrap_nodes()
    tray_emu.EMULATOR.reset()
    tray_emu.EMULATOR.sync_from_store()
    if bps is None:                # 기본(데모) 리시드에만 샘플 장애/티켓 포함
        seed_demo_samples(STORE)
    return {"reseeded": True, "scalable_units": len(STORE.sus),
            "gpus": len(STORE.gpus), "nodes": len(STORE.node_instances)}


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/flow")
def flow_console() -> FileResponse:
    """NeoCloud OS × Fake NICo 동작 검증 콘솔 (모듈별 API 흐름 시각화)."""
    return FileResponse(STATIC_DIR / "flow.html")


@app.get("/nico")
def nico_dashboard() -> FileResponse:
    """NICo 운영 대시보드 — REST API 전체 탐색 + 클러스터 에뮬레이션 라이브."""
    return FileResponse(STATIC_DIR / "nico.html")


@app.get("/arch")
def architecture_flow() -> FileResponse:
    """플랫폼 아키텍처 플로우 — 주문 단계별 점등 + 하부 API 전체 리스트."""
    return FileResponse(STATIC_DIR / "arch.html")


@app.get("/ops")
def operator_portal() -> FileResponse:
    """운영 포털 — 인프라·인시던트·break-fix·티켓 처리."""
    return FileResponse(STATIC_DIR / "ops.html")


@app.get("/customer")
def customer_portal() -> FileResponse:
    """고객 포털 — 테넌트 스코프 클러스터·SLA·셀프서비스."""
    return FileResponse(STATIC_DIR / "customer.html")


@app.get("/biz")
def business_portal() -> FileResponse:
    """비즈 포털 — 고객·계약·서비스요청·사용량/과금."""
    return FileResponse(STATIC_DIR / "biz.html")


# static assets (after routes so "/" resolves to the dashboard)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
