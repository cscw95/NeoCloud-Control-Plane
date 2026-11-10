"""NOCP demo runner — `/api/v1/demo/*` (operator only).

  GET  /api/v1/demo/scenarios                    registered scenarios (+ pending features)
  GET  /api/v1/demo/scenarios/{id}               one scenario with its step list
  POST /api/v1/demo/scenarios/{id}/run           {mode:"all"|"step", pace_ms?, reset?}
                                                 ?wait=true runs to the end before answering
  GET  /api/v1/demo/runs                         recent runs (summary)
  GET  /api/v1/demo/runs/{run_id}                steps: state, narration KO/EN, request /
                                                 response summary, assertion, timings
  POST /api/v1/demo/runs/{run_id}/step           execute the next step (step mode)
  POST /api/v1/demo/runs/{run_id}/continue       run the remaining steps (background)
  GET  /api/v1/demo/runs/{run_id}/events         SSE: `step` / `done` events (Last-Event-ID)
  POST /api/v1/demo/reset                        deterministic reseed {scope:"nocp"|"all"}

Scenarios are auto-loaded from `app/demo/scenarios/*.py` (format in engine.py and
WAVE-v2.md). A module that fails to import is listed under `load_errors`, never
breaks the app.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .engine import RUNNER, missing_routes

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])

_SCENARIOS: dict[str, dict] = {}
_LOAD_ERRORS: dict[str, str] = {}
_RESET_HOOKS: list = []


def load_scenarios() -> dict[str, dict]:
    """(Re)import every module in app/demo/scenarios that defines SCENARIO."""
    from . import scenarios as pkg
    _SCENARIOS.clear()
    _LOAD_ERRORS.clear()
    _RESET_HOOKS.clear()
    for m in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{pkg.__name__}.{m.name}")
            sc = getattr(mod, "SCENARIO", None)
            if not isinstance(sc, dict):
                continue
            for k in ("id", "title_ko", "title_en", "steps"):
                if k not in sc:
                    raise ValueError(f"SCENARIO missing '{k}'")
            for st in sc["steps"]:
                for k in ("id", "title_ko", "title_en"):
                    if k not in st:
                        raise ValueError(f"step missing '{k}'")
                if "call" not in st:
                    raise ValueError(f"step {st['id']} missing 'call'")
            if sc["id"] in _SCENARIOS:
                raise ValueError(f"duplicate scenario id '{sc['id']}'")
            sc.setdefault("module", m.name)
            _SCENARIOS[sc["id"]] = sc
            if callable(getattr(mod, "reset", None)):
                _RESET_HOOKS.append(mod.reset)
        except Exception as exc:                  # noqa: BLE001 — surface, don't crash
            _LOAD_ERRORS[m.name] = f"{type(exc).__name__}: {exc}"
    return _SCENARIOS


def _scenarios() -> dict[str, dict]:
    if not _SCENARIOS and not _LOAD_ERRORS:
        load_scenarios()
    return _SCENARIOS


def _operator(request: Request) -> None:
    ident = getattr(request.state, "identity", None)
    if ident is None or not ident.is_operator:
        raise HTTPException(403, "demo runner is operator only")


def _features(sc: dict) -> dict:
    """Which v2 features this scenario needs and which are not in this build."""
    need, pending = [], []
    for st in sc["steps"]:
        f = st.get("feature")
        if f and f not in need:
            need.append(f)
        if f and missing_routes(st.get("requires", [])) and f not in pending:
            pending.append(f)
    return {"features": need, "pending_features": pending}


def _brief(sc: dict) -> dict:
    return {"id": sc["id"], "title_ko": sc["title_ko"], "title_en": sc["title_en"],
            "feature": sc.get("feature"), "order": sc.get("order", 99),
            "summary_ko": sc.get("summary_ko", ""), "summary_en": sc.get("summary_en", ""),
            "story": sc.get("story", ""), "steps": len(sc["steps"]),
            "links": sc.get("links", []), **_features(sc)}


def _step_brief(st: dict) -> dict:
    return {"id": st["id"], "title_ko": st["title_ko"], "title_en": st["title_en"],
            "narration_ko": st.get("narration_ko") or st["title_ko"],
            "narration_en": st.get("narration_en") or st["title_en"],
            "expect_ko": st.get("expect_ko", ""), "expect_en": st.get("expect_en", ""),
            "feature": st.get("feature"), "requires": st.get("requires", []),
            "pending": bool(missing_routes(st.get("requires", []))),
            "links": st.get("links", [])}


@router.get("/scenarios")
def list_scenarios(request: Request, reload: bool = False) -> dict:
    _operator(request)
    if reload:
        load_scenarios()
    items = sorted((_brief(s) for s in _scenarios().values()),
                   key=lambda b: (b["order"], b["id"]))
    return {"scenarios": items, "load_errors": dict(_LOAD_ERRORS)}


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: str, request: Request) -> dict:
    _operator(request)
    sc = _scenarios().get(scenario_id)
    if sc is None:
        raise HTTPException(404, f"scenario '{scenario_id}' not found")
    return {**_brief(sc), "steps": [_step_brief(s) for s in sc["steps"]]}


class RunBody(BaseModel):
    mode: str = Field("all", pattern="^(all|step)$")
    pace_ms: int = Field(0, ge=0, le=10_000)
    reset: bool = False


@router.post("/scenarios/{scenario_id}/run", status_code=201)
def run_scenario(scenario_id: str, request: Request, body: Optional[RunBody] = None,
                 wait: bool = Query(False)) -> dict:
    _operator(request)
    body = body or RunBody()
    sc = _scenarios().get(scenario_id)
    if sc is None:
        raise HTTPException(404, f"scenario '{scenario_id}' not found")
    if body.reset:
        _do_reset("nocp")
    run = RUNNER.create(sc, body.mode)
    if body.mode == "all":
        if wait:
            run.run_all(body.pace_ms)
        else:
            threading.Thread(target=run.run_all, args=(body.pace_ms,), daemon=True).start()
    return run.view()


@router.get("/runs")
def list_runs(request: Request) -> list:
    _operator(request)
    return [r.summary() for r in reversed(list(RUNNER.runs.values()))]


def _run(run_id: str):
    run = RUNNER.get(run_id)
    if run is None:
        raise HTTPException(404, f"run '{run_id}' not found")
    return run


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    _operator(request)
    return _run(run_id).view()


@router.post("/runs/{run_id}/step")
def next_step(run_id: str, request: Request) -> dict:
    _operator(request)
    run = _run(run_id)
    if run.state == "running" and run.mode == "all":
        raise HTTPException(409, "run is executing all steps")
    if run.step() is None:
        raise HTTPException(409, "run is already done")
    return run.view()


@router.post("/runs/{run_id}/continue")
def continue_run(run_id: str, request: Request, pace_ms: int = Query(0, ge=0, le=10_000),
                 wait: bool = Query(False)) -> dict:
    _operator(request)
    run = _run(run_id)
    if run.state == "done":
        raise HTTPException(409, "run is already done")
    run.mode = "all"
    if wait:
        run.run_all(pace_ms)
    else:
        threading.Thread(target=run.run_all, args=(pace_ms,), daemon=True).start()
    return run.view()


@router.get("/runs/{run_id}/events")
def run_events(run_id: str, request: Request,
               last_event_id: Optional[int] = Query(None, alias="since")):
    """SSE stream of a run: `step` events (full step view) then one `done`."""
    _operator(request)
    run = _run(run_id)
    hdr = request.headers.get("last-event-id")
    start = int(hdr) if hdr and hdr.isdigit() else (last_event_id or 0)

    def gen():
        sent = start
        deadline = time.monotonic() + 600
        yield "retry: 2000\n\n"
        while time.monotonic() < deadline:
            with run.cond:
                if len(run.events) <= sent:
                    run.cond.wait(timeout=10)
                batch = run.events[sent:]
            if not batch:
                yield ": keep-alive\n\n"
                continue
            for ev in batch:
                sent = ev["seq"]
                yield (f"id: {ev['seq']}\nevent: {ev['kind']}\n"
                       f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
                if ev["kind"] == "done":
                    return

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


class ResetBody(BaseModel):
    scope: str = Field("nocp", pattern="^(nocp|all)$")
    blueprints: Optional[str] = None


def _do_reset(scope: str, blueprints: Optional[str] = None) -> dict:
    from .. import main
    out = (main.reset_all(blueprints) if scope == "all"
           else {"nocp": main.reseed(blueprints)})
    for hook in list(_RESET_HOOKS):
        try:
            hook()
        except Exception:                          # noqa: BLE001
            pass
    RUNNER.reset()
    return out


@router.post("/reset")
def demo_reset(request: Request, body: Optional[ResetBody] = None) -> dict:
    """Deterministic reseed (fixed Phase-1 layout + fixed demo samples) and clear runs.
    scope=all also resets NICo (cascade) and AI-Infra."""
    _operator(request)
    body = body or ResetBody()
    out = _do_reset(body.scope, body.blueprints)
    return {"reset": True, "scope": body.scope, "seed": body.blueprints or "phase1-default",
            **out}
