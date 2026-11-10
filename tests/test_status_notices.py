"""F13 status-page notice types (incident · maintenance · grid_curtailment)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import auth, status_notices

from _schema import check

SCHEMA = json.loads((Path(__file__).parent / "contracts" / "status-notice.schema.json").read_text())


@pytest.fixture(autouse=True)
def _reset():
    status_notices.reset()
    yield
    status_notices.reset()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _body(kind="incident", **kw):
    return {"kind": kind, "components": ["Compute"], "message_ko": "점검 공지",
            "message_en": "Maintenance notice", **kw}


def test_create_each_kind_matches_schema_and_shows_on_status(client):
    for kind in status_notices.KINDS:
        r = client.post("/api/v1/status/notices", json=_body(kind))
        assert r.status_code == 201, r.text
        assert check(r.json(), SCHEMA) == []
    page = client.get("/api/v1/status").json()
    assert {n["kind"] for n in page["notices"]} == set(status_notices.KINDS)
    assert all(check(n, SCHEMA) == [] for n in page["notices"])
    assert {"components", "incidents", "history_90d"} <= set(page)        # unchanged fields


def test_state_follows_clock_and_resolve(client):
    now = datetime.now(timezone.utc)
    fut = client.post("/api/v1/status/notices", json=_body(
        "maintenance", start=_iso(now + timedelta(hours=2)), end=_iso(now + timedelta(hours=4)))).json()
    assert fut["state"] == "scheduled"
    past = client.post("/api/v1/status/notices", json=_body(
        "maintenance", start=_iso(now - timedelta(hours=3)), end=_iso(now - timedelta(hours=1)))).json()
    assert past["state"] == "resolved"
    act = client.post("/api/v1/status/notices", json=_body("grid_curtailment",
                                                           components=["bmaas", "token-api"])).json()
    assert act["state"] == "active" and act["impact"] == "degraded"
    assert [n["id"] for n in client.get("/api/v1/status/notices",
                                        params={"state": "active"}).json()["items"]] == [act["id"]]
    r = client.post(f"/api/v1/status/notices/{act['id']}/resolve", json={})
    assert r.status_code == 200 and r.json()["state"] == "resolved" and r.json()["resolved_at"]
    assert client.get(f"/api/v1/status/notices/{act['id']}").json()["state"] == "resolved"


def test_validation(client):
    assert client.post("/api/v1/status/notices", json=_body("outage")).status_code == 422
    assert client.post("/api/v1/status/notices", json=_body(components=[])).status_code == 422
    assert client.post("/api/v1/status/notices", json=_body(components=["Moon"])).status_code == 422
    now = datetime.now(timezone.utc)
    assert client.post("/api/v1/status/notices", json=_body(
        start=_iso(now), end=_iso(now - timedelta(minutes=5)))).status_code == 422
    assert client.get("/api/v1/status/notices/ntc-9999").status_code == 404
    assert client.get("/api/v1/status/notices", params={"kind": "x"}).status_code == 422


def test_tenant_cannot_post_but_page_is_public(client, monkeypatch):
    monkeypatch.setenv("NOCP_DEV_INSECURE_HEADERS", "0")
    tok = auth.mint("tenant", tenant_id="tnt-notice-test")
    r = client.post("/api/v1/status/notices", json=_body(),
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
    op = auth.mint("operator")
    assert client.post("/api/v1/status/notices", json=_body(),
                       headers={"Authorization": f"Bearer {op}"}).status_code == 201
    assert client.get("/api/v1/status/notices").status_code == 200


def test_python_seams_for_curtailment_and_leak():
    n = status_notices.create_notice({"kind": "grid_curtailment", "components": ["bmaas"],
                                      "start": None, "end": None, "message_ko": "감축",
                                      "message_en": "curtailment", "extra": "ignored"})
    assert n["state"] == "active"
    out = status_notices.close_notice(n["id"], None)
    assert out["state"] == "resolved"
    assert status_notices.close_notice("ntc-0404") is None
    assert status_notices.component_impacts() == {}
