"""F12 demo — a NICo managed-host state change (upstream ManagedHostStateChangeMessage,
dsx-exchange nico.yaml, channel NICO/v1/machine/{id}/state, CloudEvents 1.0) is
delivered by the AI-Infra EventService to NOCP and re-published on bus topic
`machine` with from/to derived by NOCP. Self-contained and deterministic: the two
pushes are fixed sample events, so it also runs without the emulators."""

MID = "nh-su-1-rack-00-tray-07"
CH = f"NICO/v1/machine/{MID}/state"


def _push(state, eid):
    ce = {"specversion": "1.0", "id": f"demo-f12-{eid}", "source": "nico://neocloud/site/demo",
          "type": "com.nvidia.nico.v1.machine.state", "subject": CH,
          "time": "2026-11-10T00:00:00Z", "datacontenttype": "application/json",
          "data": {"machine_id": MID, "timestamp": "2026-11-10T00:00:00Z",
                   "managed_host_state": state}}
    return {"method": "POST", "path": "/api/v1/oob/events", "as": "none", "service": "nocp",
            "json": {"@odata.type": "#Event.v1_7_0.Event", "Id": eid, "Events": [{
                "EventType": "Alert", "EventId": eid, "Severity": "OK",
                "Message": f"{CH} on {MID}", "MessageId": "NeoCloudEmulator.1.0.ManagedHostState",
                "OriginOfCondition": {"@odata.id": f"/redfish/v1/Chassis/{MID}"},
                "Oem": {"Nvidia": {"Category": "machine", "Topic": "machine", "Data": ce}}}]}}


def _reset(ctx):
    from app import events, nico_events
    nico_events.reset()
    ctx["after"] = events.BUS.seq
    return {"after": ctx["after"]}


def _accepted(res, ctx):
    return (res.status == 202, f"EventService 전달 HTTP {res.status}", f"EventService delivery HTTP {res.status}")


def _bus(res, ctx):
    evs = [e for e in (res.body if isinstance(res.body, list) else [])
           if e.get("data", {}).get("machine_id") == MID]
    pairs = [(e["data"]["from"], e["data"]["to"]) for e in evs]
    ok = pairs == [(None, "ready"), ("ready", "assigned/ready")]
    txt = ", ".join(f"{a or '∅'} → {b}" for a, b in pairs)
    return (ok, f"버스 topic machine: {txt}", f"bus topic machine: {txt}")


SCENARIO = {
    "id": "f12-machine-events",
    "order": 12,
    "feature": "F12",
    "title_ko": "F12 NICo machine 상태 → Exchange → NOCP 버스",
    "title_en": "F12 NICo machine state → Exchange → NOCP bus",
    "steps": [
        {"id": "00-reset", "title_ko": "초기화", "title_en": "Reset", "call": _reset,
         "expect": lambda res, ctx: (True, "이전 상태 기억 비움", "cleared last-seen states")},
        {"id": "01-ready", "title_ko": "상태 ready 수신", "title_en": "Receive state ready",
         "call": _push({"state": "ready"}, "1"), "expect": _accepted},
        {"id": "02-assigned", "title_ko": "상태 assigned/ready 수신",
         "title_en": "Receive state assigned/ready",
         "call": _push({"state": "assigned", "instance_state": {"state": "ready"}}, "2"),
         "expect": _accepted},
        {"id": "03-bus", "title_ko": "NOCP 버스에서 from/to 확인", "title_en": "from/to on the NOCP bus",
         "call": {"method": "GET", "path": "/api/v1/events/recent", "service": "nocp",
                  "params": lambda ctx: {"topic": "machine", "after": ctx["after"]}},
         "expect": _bus},
    ],
}
