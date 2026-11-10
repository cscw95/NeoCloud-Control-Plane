"""F12 — NICo managed-host state events received from DSX Exchange.

NICo publishes the upstream ``ManagedHostStateChangeMessage``
(dsx-ai-factory/dsx-exchange@3358e108d9ad schemas/asyncapi/nico/nico.yaml) on
``NICO/v1/machine/{machineId}/state`` inside a CloudEvents 1.0 envelope;
AI-Infra's EventService pushes it to ``POST /api/v1/oob/events`` as a Redfish
Event with ``Oem.Nvidia.Topic == "machine"`` and the envelope in
``Oem.Nvidia.Data``. The upstream payload has no previous state, so NOCP derives
``from``/``to`` by diffing against the last state it saw per machine, then
re-publishes on the NOCP bus topic ``machine`` (SSE /api/v1/events?topic=machine)
for F04 metering and F10 remediation.
"""

from __future__ import annotations

import threading
from typing import Optional

from . import events

_LOCK = threading.Lock()
_LAST: dict[str, str] = {}


def reset() -> None:
    with _LOCK:
        _LAST.clear()


def is_machine_event(e: dict) -> bool:
    nv = ((e.get("Oem") or {}).get("Nvidia") or {}) if isinstance(e, dict) else {}
    return nv.get("Topic") == "machine" or str(e.get("topic", "")).startswith("NICO/v1/machine/")


def _label(state: dict) -> str:
    """Compact state name: ``assigned/ready``, ``waitingforcleanup/init``, ``ready``."""
    name = str(state.get("state", "unknown"))
    for k in ("instance_state", "cleanup_state", "machine_state", "validation_state"):
        if isinstance(state.get(k), dict) and state[k].get("state"):
            return f"{name}/{state[k]['state']}"
    return name


def handle(e: dict, source: str = "ai-infra.exchange") -> Optional[dict]:
    """Normalize one pushed event (Redfish-wrapped or bare CloudEvent) and
    re-publish it on the NOCP bus. Returns the bus record or None if malformed."""
    nv = ((e.get("Oem") or {}).get("Nvidia") or {})
    ce = nv.get("Data") if isinstance(nv.get("Data"), dict) else e
    data = ce.get("data") if isinstance(ce.get("data"), dict) else None
    if not data or "managed_host_state" not in data or "machine_id" not in data:
        return None
    mid = str(data["machine_id"])
    to = _label(data["managed_host_state"])
    with _LOCK:
        frm = _LAST.get(mid)
        _LAST[mid] = to
    st = data["managed_host_state"]
    rec = {"machine_id": mid, "from": frm, "to": to,
           "state": st.get("state"),
           "instance_state": (st.get("instance_state") or {}).get("state"),
           "timestamp": data.get("timestamp"),
           "channel": ce.get("subject") or f"NICO/v1/machine/{mid}/state",
           "cloudevent": {k: ce.get(k) for k in ("specversion", "id", "source", "type",
                                                 "subject", "time", "dataschema")},
           "payload": data}
    return events.publish("machine", rec, source=source)
