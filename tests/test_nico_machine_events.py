"""F12 — NICo machine state events pushed by AI-Infra EventService arrive on
the NOCP bus topic `machine` with derived from/to; payload checked against the
upstream AsyncAPI (ref/upstream/dsx-exchange/.../nico.yaml when present)."""
from pathlib import Path

import pytest
import yaml

from app import events, nico_events
from tests.schema_check import validate

MID = "nh-su-2-rack-00-tray-01"
CH = f"NICO/v1/machine/{MID}/state"


def _redfish(state: dict, eid="1"):
    ce = {"specversion": "1.0", "id": f"ce-{eid}", "source": "nico://neocloud/site/s",
          "type": "com.nvidia.nico.v1.machine.state", "subject": CH,
          "time": "2026-11-10T00:00:00Z", "datacontenttype": "application/json",
          "data": {"machine_id": MID, "timestamp": "2026-11-10T00:00:00Z",
                   "managed_host_state": state}}
    return {"@odata.type": "#Event.v1_7_0.Event", "Id": eid, "Events": [{
        "EventType": "Alert", "EventId": eid, "Severity": "OK",
        "Message": f"{CH} on {MID}", "MessageId": "NeoCloudEmulator.1.0.x",
        "OriginOfCondition": {"@odata.id": f"/redfish/v1/Chassis/{MID}"},
        "Oem": {"Nvidia": {"Category": "machine", "Topic": "machine", "Data": {**ce, "subject": CH}}}}]}


def test_machine_topic_listed(client):
    assert "machine" in client.get("/api/v1/events/topics").json()["topics"]


def test_instance_ready_event_reaches_bus_with_from_to(client):
    after = events.BUS.seq
    for i, st in enumerate(({"state": "ready"},
                            {"state": "assigned", "instance_state": {"state": "init"}},
                            {"state": "assigned", "instance_state": {"state": "ready"}})):
        r = client.post("/api/v1/oob/events", json=_redfish(st, str(i)))
        assert r.status_code == 202, r.text
    evs = client.get(f"/api/v1/events/recent?after={after}&topic=machine").json()
    assert [(e["data"]["from"], e["data"]["to"]) for e in evs][-2:] == [
        ("ready", "assigned/init"), ("assigned/init", "assigned/ready")]
    ready = [e for e in evs if e["data"]["instance_state"] == "ready"]
    assert len(ready) == 1 and ready[0]["data"]["channel"] == CH
    assert ready[0]["data"]["cloudevent"]["specversion"] == "1.0"
    # not mistaken for an OOB health event
    assert not [e for e in client.get("/api/v1/oob/events").json().get("events", [])
                if e.get("machine") == MID]
    sse = client.get(f"/api/v1/events?topic=machine&once=true&after={after}")
    assert sse.status_code == 200 and "event: machine" in sse.text


def test_malformed_machine_event_ignored(client):
    bad = _redfish({"state": "ready"})
    bad["Events"][0]["Oem"]["Nvidia"]["Data"] = {"data": {"nope": 1}}
    assert client.post("/api/v1/oob/events", json=bad).status_code == 202
    assert nico_events.handle({"Oem": {"Nvidia": {"Topic": "machine", "Data": {}}}}) is None


def test_payload_matches_upstream_asyncapi():
    for d in Path(__file__).resolve().parents:
        f = d / "ref/upstream/dsx-exchange/schemas/asyncapi/nico/nico.yaml"
        if f.exists():
            break
    else:
        pytest.skip("ref/upstream not present")
    doc = yaml.safe_load(open(f))
    msg = doc["components"]["messages"]["ManagedHostStateChangeMessage"]["payload"]
    ev = _redfish({"state": "waitingforcleanup", "cleanup_state": {"state": "init"}})
    data = ev["Events"][0]["Oem"]["Nvidia"]["Data"]["data"]
    assert validate(data, msg, doc) == []
    rec = nico_events.handle(ev["Events"][0])
    assert rec["data"]["payload"] == data and rec["data"]["to"] == "waitingforcleanup/init"
