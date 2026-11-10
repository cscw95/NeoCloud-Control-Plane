"""PLAN C security — operator plane needs X-Operator unless NOCP_DEV_OPERATOR_DEFAULT=1."""


def test_operator_header_required_when_dev_default_off(client, monkeypatch):
    monkeypatch.setenv("NOCP_DEV_OPERATOR_DEFAULT", "0")
    r = client.get("/api/v1/racks")
    assert r.status_code == 403 and r.json()["detail"] == "operator header required"
    assert client.get("/api/v1/racks", headers={"X-Operator": "true"}).status_code == 200
    # public surfaces / non-API untouched
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/status").status_code != 403
    # tenant plane keeps its own rules
    assert client.get("/api/v1/orders", headers={"X-Tenant-Id": "tnt-x"}).status_code == 200
    # EventService webhook is exempt
    r = client.post("/api/v1/oob/events", json={"Events": []})
    assert r.status_code != 403


def test_dev_default_keeps_implicit_operator(client, monkeypatch):
    monkeypatch.setenv("NOCP_DEV_OPERATOR_DEFAULT", "1")
    assert client.get("/api/v1/racks").status_code == 200
