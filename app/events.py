"""DSX Exchange-style event bus (NOCP side).

Topics follow DSX Exchange: power, thermal, leak, grid, health (+ rma,
firmware for NOCP workflows). Producers call `publish()`; the OOB webhook
re-publishes AI-Infra EventService pushes. `/api/v1/events` streams the bus as
Server-Sent Events (re-publication of Exchange), `/api/v1/events/recent` is the
polling twin.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

TOPICS = ("power", "thermal", "leak", "grid", "health", "rma", "firmware",
          "machine")                       # machine: F12 NICo host state (app/nico_events.py)

router = APIRouter(prefix="/api/v1", tags=["dsx-events"])


class _Bus:
    def __init__(self, size: int = 1000) -> None:
        self.lock = threading.Lock()
        self.buf: deque = deque(maxlen=size)
        self.seq = 0

    def publish(self, topic: str, data: dict, source: str = "nocp") -> dict:
        with self.lock:
            self.seq += 1
            ev = {"id": self.seq, "topic": topic if topic in TOPICS else "health",
                  "source": source, "data": data,
                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self.buf.append(ev)
        for fn in list(LISTENERS):       # F21/F22 subscribers (never break producers)
            try:
                fn(ev)
            except Exception:            # noqa: BLE001
                pass
        return ev

    def since(self, after: int = 0, topic: Optional[str] = None,
              limit: int = 200) -> list:
        with self.lock:
            out = [e for e in self.buf if e["id"] > after
                   and (not topic or e["topic"] == topic)]
        return out[-limit:]

    def reset(self) -> None:
        with self.lock:
            self.buf.clear()
            self.seq = 0


BUS = _Bus()
publish = BUS.publish
LISTENERS: list = []


def add_listener(fn) -> None:
    """Call fn(event) after every publish (in-process Exchange subscription)."""
    if fn not in LISTENERS:
        LISTENERS.append(fn)


@router.get("/events/topics")
def topics() -> dict:
    return {"topics": list(TOPICS), "transport": "sse",
            "schema": "DSX Exchange-compatible {id, topic, source, data, at}"}


@router.get("/events/recent")
def recent(after: int = 0, topic: Optional[str] = None,
           limit: int = Query(200, le=1000)) -> list:
    return BUS.since(after, topic, limit)


@router.get("/events")
async def stream(request: Request, topic: Optional[str] = None, after: int = 0,
                 once: bool = False):
    """SSE stream. `once=true` flushes the backlog and closes (tests, curl)."""
    async def gen():
        last = after
        while True:
            for ev in BUS.since(last, topic):
                last = ev["id"]
                yield f"id: {ev['id']}\nevent: {ev['topic']}\ndata: {json.dumps(ev)}\n\n"
            if once or await request.is_disconnected():
                return
            await asyncio.sleep(1.0)
            yield ": keepalive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
