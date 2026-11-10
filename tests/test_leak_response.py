"""F22 coolant leak → cordon → workload move (NVKE replace / BMaaS ticket)."""
import json
import re
from pathlib import Path

import pytest

from app import leak_response, nico_fake
from app.store import STORE

CONTRACTS = Path(__file__).parent / "contracts"
SCHEMA = json.loads((CONTRACTS / "leak-response.schema.json").read_text())
PROTO = (CONTRACTS / "health_event.proto").read_text()


@pytest.fixture(autouse=True)
def _reset():
    leak_response.reset()
    yield
    leak_response.reset()


def _check(inst, sch, path="$"):
    """Minimal JSON Schema check (type/required/enum/pattern/properties/items)."""
    errs = []
    t = sch.get("type")
    tm = {"object": dict, "array": list, "string": str, "integer": int,
          "number": (int, float), "boolean": bool, "null": type(None)}
    if t:
        ts = t if isinstance(t, list) else [t]
        if not any(isinstance(inst, tm[x]) for x in ts):
            return [f"{path}: expected {t}"]
    if "enum" in sch and inst not in sch["enum"]:
        errs.append(f"{path}: {inst!r} not in enum")
    if "pattern" in sch and isinstance(inst, str) and not re.search(sch["pattern"], inst):
        errs.append(f"{path}: pattern")
    if isinstance(inst, dict):
        errs += [f"{path}: missing {k}" for k in sch.get("required", []) if k not in inst]
        for k, s in sch.get("properties", {}).items():
            if k in inst:
                errs += _check(inst[k], s, f"{path}.{k}")
    if isinstance(inst, list) and "items" in sch:
        for i, v in enumerate(inst):
            errs += _check(v, sch["items"], f"{path}[{i}]")
    return errs


def _proto_fields(message):
    body = re.search(r"message %s \{(.*?)\n\}" % message, PROTO, re.S).group(1)
    return set(re.findall(r"^\s*(?:repeated\s+)?(?:map<[^>]+>|[\w.]+)\s+(\w+)\s*=\s*\d+;", body, re.M))


def _leak(client, tray, sev="Critical", mid=None):
    ev = {"Events": [{
        "EventType": "Alert", "EventId": f"ev-{tray}-{sev}",
        "MessageId": mid or f"Environmental.1.5.LeakDetected{sev}",
        "Message": f"Coolant leak {sev} on {tray}", "Severity": sev,
        "OriginOfCondition": {"@odata.id":
                              f"/redfish/v1/Chassis/{tray}/ThermalSubsystem/LeakDetection"}}]}
    r = client.post("/api/v1/oob/events", json=ev)
    assert r.status_code == 202, r.text


def _cluster(client):
    tid = client.post("/api/v1/tenants", json={
        "name": "leak-co", "isolation_tier": "bare_metal_dedicated"}).json()["id"]
    c = client.post("/api/v1/nvke/clusters", json={
        "name": "leak", "tenant_id": tid, "k8s_version": "1.33",
        "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 2}]}).json()
    return tid, c


def test_critical_leak_on_nvke_node_moves_workload(client):
    tid, c = _cluster(client)
    victim = c["nodepools"][0]["nodes"][0]
    tray = victim["tray_id"] or victim["nico_host_id"].removeprefix("nh-")
    _leak(client, tray)
    rows = client.get("/api/v1/oob/leak-responses").json()["items"]
    assert len(rows) == 1
    r = rows[0]
    assert not _check(r, SCHEMA), _check(r, SCHEMA)
    assert r["kind"] == "nvke" and r["state"] == "moved" and r["tenant_id"] == tid
    assert [s["name"] for s in r["steps"]][:2] == ["health-event", "cordon"]
    assert r["replacement"]["old_host"] == victim["nico_host_id"]
    assert r["within_kpi"] and r["elapsed_s"] <= 300
    # NICo host cordoned, cluster keeps 2 nodes without the leaking tray
    assert nico_fake.FAKE_NICO.get_host(victim["nico_host_id"]).cordoned
    hosts = [n["nico_host_id"] for p in client.get(
        f"/api/v1/nvke/clusters/{c['id']}").json()["nodepools"] for n in p["nodes"]]
    assert victim["nico_host_id"] not in hosts and len(hosts) == 2
    # duplicate push is ignored
    _leak(client, tray)
    assert len(client.get("/api/v1/oob/leak-responses").json()["items"]) == 1


def test_health_event_matches_nvsentinel_proto(client):
    tid, c = _cluster(client)
    tray = c["nodepools"][0]["nodes"][0]["nico_host_id"].removeprefix("nh-")
    _leak(client, tray)
    he = client.get("/api/v1/oob/leak-responses").json()["items"][0]["health_event"]
    fields = _proto_fields("HealthEvent")
    assert set(he) <= fields, set(he) - fields
    actions = set(re.findall(r"^\s*(\w+) = \d+;",
                             re.search(r"enum RecommendedAction \{(.*?)\}", PROTO, re.S).group(1),
                             re.M))
    assert he["recommendedAction"] in actions
    assert set(he["entitiesImpacted"][0]) == _proto_fields("Entity")
    assert he["isFatal"] is True and he["componentClass"] == "Tray"


def test_warning_posts_notice_only(client):
    tid, c = _cluster(client)
    node = c["nodepools"][0]["nodes"][0]
    tray = node["nico_host_id"].removeprefix("nh-")
    _leak(client, tray, "Warning")
    r = client.get("/api/v1/oob/leak-responses").json()["items"][0]
    assert r["kind"] == "notice-only" and r["notice"]["message_en"]
    assert not nico_fake.FAKE_NICO.get_host(node["nico_host_id"]).cordoned
    hosts = [n["nico_host_id"] for p in client.get(
        f"/api/v1/nvke/clusters/{c['id']}").json()["nodepools"] for n in p["nodes"]]
    assert node["nico_host_id"] in hosts


def test_bmaas_node_gets_ticket(client):
    tid = client.post("/api/v1/tenants", json={
        "name": "bm-co", "isolation_tier": "bare_metal_dedicated"}).json()["id"]
    ni = next(n for n in STORE.node_instances.values() if not n.tenant_id)
    ni.tenant_id = tid
    _leak(client, ni.tray_id)
    r = client.get(f"/api/v1/oob/leak-responses?tenant_id={tid}").json()["items"][0]
    assert r["kind"] == "bmaas" and r["state"] == "awaiting-customer"
    t = STORE.tickets[r["ticket_id"]]
    assert t.tenant_id == tid and "누수" in t.subject
    assert client.get(f"/api/v1/oob/leak-responses/{r['id']}").json()["id"] == r["id"]
    assert client.get("/api/v1/oob/leak-responses/leak-9999").status_code == 404
