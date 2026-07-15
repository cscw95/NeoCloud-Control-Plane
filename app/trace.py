"""System-wide detailed operation trace bus.

Every internal action across the stack — NeoCloud OS pipeline steps, NICo
southbound control (Redfish/BMC, DPU-DHCP, PXE, cloud-init), DPU HBN/NVUE
config, UFM/NMX partitioning, VAST VMS storage calls — is emitted here as a
structured TraceEvent with the actual message payload. This is what the /flow
console renders as the "system detail trace", complementing the HTTP API log.

In production this maps onto the observability pipeline (M8): the emit() call
sites become OTel spans / NATS `neocloud.telemetry.*` events.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel


class TraceEvent(BaseModel):
    seq: int
    at: str                            # ISO-8601 UTC
    src: str                           # e.g. "NeoCloudOS.M1", "NICo.APIService"
    dst: str                           # e.g. "BMC(nh-su-1-rack-00-tray-00)"
    channel: str                       # REST|gRPC|Redfish|DHCP|PXE|cloud-init|
                                       # NVUE/HBN|UFM|NMX|VAST-API|internal
    op: str                            # verb/endpoint, e.g. "POST …/Reset"
    detail: str = ""
    order_id: Optional[str] = None
    host_id: Optional[str] = None
    payload: Optional[dict] = None     # actual message body (simulated)


class Tracer:
    def __init__(self, capacity: int = 25000) -> None:
        self._lock = threading.Lock()
        self._events: deque = deque(maxlen=capacity)
        self._seq = 0

    def emit(self, src: str, dst: str, channel: str, op: str, detail: str = "",
             payload: Optional[dict] = None, order_id: Optional[str] = None,
             host_id: Optional[str] = None) -> TraceEvent:
        with self._lock:
            self._seq += 1
            ev = TraceEvent(
                seq=self._seq, at=datetime.now(timezone.utc).isoformat(),
                src=src, dst=dst, channel=channel, op=op, detail=detail,
                order_id=order_id, host_id=host_id, payload=payload)
            self._events.append(ev)
            return ev

    def query(self, order_id: Optional[str] = None, channel: Optional[str] = None,
              q: Optional[str] = None, since: int = 0,
              limit: int = 500) -> list:
        with self._lock:
            out = []
            for ev in self._events:
                if ev.seq <= since:
                    continue
                if order_id and ev.order_id != order_id:
                    continue
                if channel and ev.channel != channel:
                    continue
                if q:
                    hay = f"{ev.src} {ev.dst} {ev.op} {ev.detail} {ev.host_id}"
                    if q.lower() not in hay.lower():
                        continue
                out.append(ev)
            return out[-limit:]

    def count(self) -> int:
        with self._lock:
            return len(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._seq = 0


TRACER = Tracer()
emit = TRACER.emit                      # convenience alias for call sites


router = APIRouter(prefix="/api/v1", tags=["trace"])


@router.get("/trace")
def get_trace(order_id: Optional[str] = None, channel: Optional[str] = None,
              q: Optional[str] = None, since: int = 0,
              limit: int = 500) -> list:
    return TRACER.query(order_id=order_id, channel=channel, q=q,
                        since=since, limit=min(limit, 2000))


@router.delete("/trace")
def clear_trace() -> dict:
    TRACER.clear()
    return {"cleared": True}
