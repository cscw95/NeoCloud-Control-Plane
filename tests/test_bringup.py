"""F23 DCIM + 15-step bring-up orchestrator, F24 firmware bundles/plans, F25
attestation via NICo REST. Upstreams (AI-Infra, NICo) are a stateful
httpx.MockTransport fake; the live chain is covered by the temp-stack check."""
import json
import re

import httpx
import pytest

from app import dsx

N = "/v2/org/neocloud/nico"
SERIAL = "SN-VR-0001"
RID = "bu-sn-vr-0001"
PLAN = [{"a": f"tray-{i:02d}:p0", "b": f"leaf-0:swp{i + 1}"} for i in range(18)]


class Upstream:
    def __init__(self):
        self.lldp_faults = {}            # a -> actual_b
        self.power = "Off"
        self.racks = {}
        self.machines = {}
        self.attest_fail = set()
        self.validate_fail = set()
        self.trays = 18
        self.calls = []
        self.last = {}

    def __call__(self, req):
        p, m = req.url.path, req.method
        body = json.loads(req.content) if req.content else None
        self.calls.append((m, p))
        self.last[p] = body
        if p == "/config-manager/v1/switches":
            rack = req.url.params.get("rack")
            return 200, {"switches": [{"id": f"{rack}-mgmt-sw-0", "source": "bringup"},
                                      {"id": f"{rack}-leaf-sw-0", "source": "bringup"}]}
        if re.fullmatch(r"/config-manager/v1/switches/[^/]+/ztp", p):
            return 200, {"state": "configured"}
        if p == "/config-manager/v1/lldp-faults":
            if m == "POST":
                self.lldp_faults[body["a"]] = body.get("actual_b")
            else:
                self.lldp_faults.clear()
            return 200, {"faults": self.lldp_faults}
        mh = re.fullmatch(r"/emulator/v1/rest/machine/([^/]+)/attestation", p)
        if mh:
            (self.attest_fail.add if body.get("measuredBoot") == "Failed"
             else self.attest_fail.discard)(mh.group(1))
            return 200, {"machineId": mh.group(1)}
        if p == "/config-manager/v1/lldp-validate":
            mm = [{"a": c["a"], "expected_b": c["b"], "actual_b": self.lldp_faults[c["a"]],
                   "kind": "miswired"} for c in body["cable_plan"] if c["a"] in self.lldp_faults]
            n = len(body["cable_plan"])
            return 200, {"checked": n, "matched": n - len(mm), "mismatches": mm}
        mr = re.fullmatch(r"/rms/v1/racks/([^/]+)/(inventory|power|firmware)", p)
        if mr:
            rid, what = mr.groups()
            if what == "power":
                self.power = {"POWER_OPERATION_ON": "On"}.get(body.get("operation"), "Off")
                return 200, {"status": "RETURN_CODE_SUCCESS", "state": self.power}
            if what == "firmware":
                return 200, {"order": ["bmc", "bf", "gpu", "cx", "nvswitch"]}
            return 200, {"rack_id": rid, "power": self.power,
                         "compute_trays": [{"id": f"{rid}-tray-{i:02d}", "gpus": 4,
                                            "power": self.power} for i in range(self.trays)],
                         "nvlink_switch_trays": 9, "power_shelves": 4}
        if p == "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate":
            return 200, {"Id": "fwbundle-1", "TaskState": "Completed",
                         "Oem": {"Nvidia": {"UpdateOrder": ["bmc", "bf", "gpu", "cx",
                                                            "nvswitch"]}}}
        if p == f"{N}/expected-rack" and m == "POST":
            rid = body["rackId"]
            assert set(body) <= {"siteId", "rackId", "rackProfileId", "name", "description",
                                 "labels"}
            if rid in self.racks:
                return 409, {"source": "nico", "message": "exists", "data": None}
            self.racks[rid] = body
            for i in range(18):
                self.machines[f"nh-{rid}-tray-{i:02d}"] = rid
            return 201, {"id": "00000000-0000-0000-0000-000000000001", "rackId": rid}
        if re.fullmatch(rf"{N}/rack/[^/]+/bringup", p):
            return 200, {"taskIds": ["00000000-0000-0000-0000-0000000000aa"]}
        if p == f"{N}/machine":
            rid = req.url.params.get("rackId")
            return 200, [{"id": k} for k, v in self.machines.items() if v == rid]
        if p.startswith(f"{N}/domain/nvlink/"):
            return 200, {"id": p.rsplit("/", 1)[1], "topology": "NVL72"}
        ma = re.fullmatch(rf"{N}/machine/([^/]+)/attestation", p)
        if ma:
            bad = ma.group(1) in self.attest_fail
            return 200, {"machineId": ma.group(1), "measuredBoot": "Failed" if bad else "Passed",
                         "attested": not bad, "tpm": {"pcrs": {"0": "a" * 64, "7": "b" * 64}},
                         "dpu": {"attested": True}, "verifiedAt": "2026-11-10T00:00:00Z"}
        mv = re.fullmatch(rf"{N}/machine/([^/]+)/validation/run", p)
        if mv:
            ok = mv.group(1) not in self.validate_fail
            return 202, {"status": {"state": "Success" if ok else "Failed"}}
        return 404, {"detail": "nope"}


@pytest.fixture
def up():
    u = Upstream()

    def handler(req):
        st, b = u(req)
        return httpx.Response(st, json=b)
    dsx.set_transport(httpx.MockTransport(handler))
    yield u
    dsx.set_transport(None)


def _register(client, **kw):
    body = {"serial": SERIAL, "site": "gasan", "row": "R3", "position": "07",
            "blueprint_key": "vr-nvl72", "cable_plan": PLAN, **kw}
    return client.post("/api/v1/dcim/racks", json=body)


def test_dcim_register_list_and_conflicts(client):
    r = _register(client)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["id"] == RID and v["state"] == "planned" and v["gpus"] == 72
    assert len(v["cable_plan"]) == 18
    assert _register(client).status_code == 409                          # serial
    assert _register(client, serial="other").status_code == 409          # position
    assert _register(client, serial="x2", position="08",
                     blueprint_key="nope").status_code == 400
    lst = client.get("/api/v1/dcim/racks", params={"state": "planned"}).json()
    assert lst["count"] == 1
    assert client.get("/api/v1/dcim/racks", params={"state": "weird"}).status_code == 400
    assert client.get(f"/api/v1/dcim/racks/{RID}").json()["serial"] == SERIAL
    assert client.get("/api/v1/dcim/racks/none").status_code == 404
    topics = [e["topic"] for e in client.get("/api/v1/events/recent").json()]
    assert "dcim" in topics


def test_bringup_happy_path_to_sellable(client, up):
    _register(client)
    s = client.post(f"/api/v1/bringup/racks/{RID}:start").json()
    assert s["state"] == "sellable", s
    assert len(s["steps"]) == 15 and s["progress"] == {"passed": 15, "total": 15}
    assert [x["no"] for x in s["steps"]] == list(range(1, 16))
    assert s["source"] == "upstream"
    hist = [h["state"] for h in client.get(f"/api/v1/dcim/racks/{RID}").json()["history"]]
    assert hist == ["planned", "installed", "bringup", "validating", "sellable"]
    sell = client.get("/api/v1/dcim/sellable").json()
    assert sell["rack_count"] == 1 and sell["gpu_count"] == 72 and sell["tray_count"] == 18
    assert ("POST", f"{N}/expected-rack") in up.calls
    assert ("POST", f"{N}/rack/{RID}/bringup") in up.calls
    assert up.power == "On"
    step5 = s["steps"][4]
    assert [c["url"].split("/nico")[1] for c in step5["calls"]] == ["/expected-rack",
                                                                    f"/rack/{RID}/bringup"]
    assert step5["calls"][0]["status"] == 201 and step5["calls"][0]["request"]["rackId"] == RID
    cab = client.get("/api/v1/dcim/cables", params={"rack": RID}).json()
    assert cab["count"] == 18 and cab["results"][0]["status"]["value"] == "connected"
    rk = client.get(f"/api/v1/dcim/racks/{RID}").json()
    assert rk["status"] == {"value": "active", "label": "Active"} and rk["name"] == RID
    assert client.post(f"/api/v1/bringup/racks/{RID}:start").status_code == 409
    assert client.get(f"/api/v1/bringup/racks/{RID}").json()["state"] == "sellable"


def test_cable_mismatch_fails_step_04_then_retry(client, up):
    _register(client)
    up.lldp_faults[PLAN[4]["a"]] = "leaf-0:swp30"
    s = client.post(f"/api/v1/bringup/racks/{RID}:start").json()
    assert s["state"] == "failed" and s["failed_step"] == 4
    st = {x["no"]: x["status"] for x in s["steps"]}
    assert st[1] == st[2] == st[3] == "passed" and st[4] == "failed"
    assert all(st[n] == "blocked" for n in range(5, 16))
    assert s["cable_mismatches"][0]["actual_b"] == "leaf-0:swp30"
    assert "mismatch" in s["steps"][3]["detail"]
    assert ("POST", f"{N}/expected-rack") not in up.calls  # nothing past step 04
    cab = client.get("/api/v1/dcim/cables", params={"rack": RID}).json()
    assert cab["results"][4]["status"]["value"] == "planned"
    assert cab["results"][4]["lldp_mismatch"]["actual_b"] == "leaf-0:swp30"
    assert client.get("/api/v1/dcim/sellable").json()["rack_count"] == 0
    # field crew fixes the cable → retry passes
    up.lldp_faults.clear()
    s = client.post(f"/api/v1/bringup/racks/{RID}:start").json()
    assert s["state"] == "sellable"


def test_sku_attestation_and_validation_failures(client, up):
    _register(client)
    up.trays = 17
    s = client.post(f"/api/v1/bringup/racks/{RID}:start").json()
    assert s["failed_step"] == 6 and "compute_trays expected 18 got 17" in s["steps"][5]["detail"]
    up.trays = 18
    up.attest_fail.add(f"nh-{RID}-tray-03")
    s = client.post(f"/api/v1/bringup/racks/{RID}:start").json()
    assert s["failed_step"] == 11 and s["state"] == "failed"
    up.attest_fail.clear()
    up.validate_fail.add(f"nh-{RID}-tray-05")
    s = client.post(f"/api/v1/bringup/racks/{RID}:start").json()
    assert s["failed_step"] == 13
    assert s["steps"][4]["detail"].startswith("already registered")   # 409 on retry is ok
    up.validate_fail.clear()
    assert client.post(f"/api/v1/bringup/racks/{RID}:start").json()["state"] == "sellable"


def test_patch_cable_plan_and_offline_fallback(client):
    _register(client)
    r = client.patch(f"/api/v1/dcim/racks/{RID}", json={"cable_plan": PLAN[:2]})
    assert len(r.json()["cable_plan"]) == 2
    s = client.post(f"/api/v1/bringup/racks/{RID}:start").json()   # NOCP_DSX_OFFLINE
    assert s["state"] == "sellable" and s["source"] == "local-fallback"
    assert client.patch(f"/api/v1/dcim/racks/{RID}",
                        json={"cable_plan": PLAN}).status_code == 409
    _register(client, serial="SN-EMPTY", position="09", cable_plan=[])
    s = client.post("/api/v1/bringup/racks/bu-sn-empty:start").json()
    assert s["failed_step"] == 1


def test_firmware_bundles_and_plans(client, up):
    b = client.get("/api/v1/firmware/bundles").json()
    assert b["bundles"][0]["components"]["bmc"] == "1.4.2"
    assert b["apply_order"][:3] == ["bmc", "bf", "gpu"]
    comps = {"bmc": "1.4.2", "gpu": "97.11.0", "nvswitch": "2.6.0", "cx": "40.45.0", "bf": "3.2.0"}
    r = client.post("/api/v1/firmware/bundles", json={"version": "vr-2026.08",
                                                       "components": comps})
    assert r.status_code == 201
    assert client.post("/api/v1/firmware/bundles", json={"version": "vr-2026.08",
                                                          "components": comps}).status_code == 409
    assert client.post("/api/v1/firmware/bundles",
                       json={"version": "bad-bmc", "components": {**comps, "bmc": "1.5.0"}}
                       ).status_code == 400
    assert client.post("/api/v1/firmware/bundles",
                       json={"version": "short", "components": {"bmc": "1.4.2"}}
                       ).status_code == 400
    from app.store import STORE
    from app.models import HwState
    rid = next(iter(STORE.racks))
    tray = STORE.racks[rid].tray_ids[0]
    STORE.trays[tray].state = HwState.allocated
    p = client.post("/api/v1/firmware/plans", json={"rack": rid, "bundle": "vr-2026.08",
                                                     "window": "2026-11-12T02:00Z"}).json()
    assert p["status"] == "scheduled" and p["order"][:3] == ["bmc", "bf", "gpu"]
    assert p["skipped"] >= 1 and p["targets"] == len(p["hosts"]) - p["skipped"]
    assert next(h for h in p["hosts"] if h["host_id"] == tray)["action"] == "skip"
    e = client.post(f"/api/v1/firmware/plans/{p['id']}:execute").json()
    assert e["status"] == "completed" and e["result"]["source"] == "ai-infra"
    assert client.get(f"/api/v1/firmware/plans/{p['id']}").json()["status"] == "completed"
    assert client.post("/api/v1/firmware/plans", json={"rack": "nope"}).status_code == 404
    assert client.post("/api/v1/firmware/plans", json={"rack": rid,
                                                       "bundle": "nope"}).status_code == 404


def test_attestation_via_nico_rest(client, up):
    a = client.get("/fake-nico/hosts/nh-su-1-rack-00-tray-00/attestation").json()
    assert a["source"] == "nico" and a["attested"] is True
    assert a["measured_boot"] == "passed" and set(a["tpm"]) == {"PCR0", "PCR7"}
    assert ("GET", f"{N}/machine/nh-su-1-rack-00-tray-00/attestation") in up.calls


def test_attestation_fallback_to_fake(client):
    from app.nico_fake import FAKE_NICO
    hid = next(iter(FAKE_NICO.hosts))
    a = client.get(f"/fake-nico/hosts/{hid}/attestation").json()
    assert a["source"] == "fake-nico" and "PCR0" in a["tpm"]


def test_dcim_operator_only(client):
    r = client.get("/api/v1/dcim/racks", headers={"X-Tenant-Id": "t-acme"})
    assert r.status_code == 403


# ── upstream message shapes, injection, demo ────────────────────────────────
from pathlib import Path  # noqa: E402

FIX = json.loads((Path(__file__).parent / "fixtures" / "bringup_upstream.json").read_text())


def _schema_check(obj, fields):
    return [f for f in fields if f not in obj]


def test_netbox_shapes(client, up):
    _register(client)
    client.post(f"/api/v1/bringup/racks/{RID}:start")
    lst = client.get("/api/v1/dcim/racks").json()
    assert _schema_check(lst, FIX["netbox"]["list"]) == []
    assert _schema_check(lst["results"][0], FIX["netbox"]["rack"]) == []
    assert lst["results"][0]["status"]["value"] in FIX["netbox"]["rack_status"]
    cab = client.get("/api/v1/dcim/cables").json()["results"][0]
    assert _schema_check(cab, FIX["netbox"]["cable"]) == []
    assert cab["status"]["value"] in FIX["netbox"]["cable_status"]
    assert cab["a_terminations"][0]["object_type"] == "dcim.interface"


def test_firmware_execute_sends_dmtf_simple_update(client, up):
    from app.store import STORE
    p = client.post("/api/v1/firmware/plans", json={"rack": next(iter(STORE.racks))}).json()
    client.post(f"/api/v1/firmware/plans/{p['id']}:execute")
    sent = up.last["/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate"]
    assert set(sent) - {"Oem"} <= set(FIX["simple_update_parameters"])
    assert sent["ImageURI"].endswith(".fwpkg")
    assert sent["Oem"]["Nvidia"]["Bundle"]["components"]["bmc"] == "1.4.2"


def test_inject_and_demo_reset(client, up):
    _register(client)
    r = client.post("/api/v1/bringup/inject", json={"kind": "miswire", "rack": RID}).json()
    assert r["calls"][0]["url"].endswith("/config-manager/v1/lldp-faults")
    assert r["calls"][0]["request"]["a"] == PLAN[0]["a"]
    assert client.post("/api/v1/bringup/inject", json={"kind": "bogus"}).status_code == 422
    assert client.post("/api/v1/bringup/inject",
                       json={"kind": "attestation_fail"}).status_code == 400
    _register(client, serial="SN-DEMO-0001", position="10")
    d = client.post("/api/v1/bringup/demo/reset").json()
    assert d["removed"] is True and d["rack"] == "bu-sn-demo-0001"


def _load_scenario(name):
    import importlib.util
    path = Path(__file__).parent.parent / "app" / "demo" / "scenarios" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"demo_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SCENARIO


def _run_scenario(client, sc):
    """Minimal stand-in for v2-demo's runner: NOCP calls in-process, checks expect."""
    from app.demo.engine import _resolve
    from app.demo.janitor import new_uid
    ctx, out = {"uid": new_uid()}, []     # per-run names (demo isolation)
    for st in sc["steps"]:
        call = st["call"]
        r = lambda v: _resolve(v, ctx)  # noqa: E731
        res = client.request(call["method"], r(call["path"]), json=r(call.get("json")),
                             params=r(call.get("params")))
        body = res.json() if res.content else None

        class Res:
            status, headers = res.status_code, dict(res.headers)
        Res.body = body
        exp = st["expect"]
        ok = exp(Res, ctx) if callable(exp) else res.status_code == exp.get("status", 200)
        ok = ok[0] if isinstance(ok, tuple) else ok
        if st.get("save"):
            st["save"](Res, ctx)
        out.append((st["id"], ok))
    return out


@pytest.mark.parametrize("name,feature", [("f23_bringup", "F23"), ("f24_firmware", "F24"),
                                          ("f25_attestation", "F25")])
def test_demo_scenarios_pass(client, up, name, feature):
    sc = _load_scenario(name)
    assert sc["feature"] == feature and sc["title_ko"] and sc["title_en"]
    for st in sc["steps"]:
        assert {"id", "title_ko", "title_en", "call", "expect"} <= set(st)
    first = _run_scenario(client, sc)
    assert all(ok for _, ok in first), first
    again = _run_scenario(client, sc)                 # deterministic re-run after reset
    assert all(ok for _, ok in again), again
