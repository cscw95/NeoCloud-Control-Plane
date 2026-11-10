"""F08 — NicoRestAdapter writes: REST call order for allocate / isolate / release
against a small stateful fake of the NICo REST surface; zero /nico-bridge calls."""
import json
import re
import uuid

import httpx
import pytest

from app.adapters import wait_job
from app.nico_rest_adapter import NicoRestAdapter

P = "/v2/org/neocloud/nico"
SITE = "site-1"
IT = "it-vr"


class FakeNico:
    """Just enough of /v2/org/{org}/nico for the adapter (spec field names)."""

    def __init__(self, machines=("nh-a", "nh-b")):
        self.m = {mid: {"id": mid, "siteId": SITE, "instanceTypeId": IT, "status": "Ready",
                        "lifecycleState": "ready", "labels": {}, "instanceId": None,
                        "controllerMachineType": "vr-nvl72", "maintenanceMessage": None,
                        "statusHistory": [{"status": "Ready", "message": "seed"}]}
                  for mid in machines}
        self.objs = {k: {} for k in ("vpc", "allocation", "instance", "infiniband-partition",
                                     "nvlink-logical-partition", "spectrumx-partition")}
        self.dead = {}
        self.calls = []
        self.cleanup_polls = 1          # GET /machine polls a machine stays in Reset

    def _list(self, kind, q):
        items = [o for o in self.objs[kind].values()
                 if all(o.get(k) == v for k, v in q.items() if k in ("tenantId", "siteId", "vpcId"))]
        return items

    def handle(self, req: httpx.Request):
        path, m = req.url.path, req.method
        self.calls.append((m, path))
        q = dict(req.url.params)
        body = json.loads(req.content) if req.content else {}
        rel = path[len(P):]
        if rel.startswith("/machine"):
            return self._machine(m, rel, body)
        if rel.startswith("/instance"):
            return self._instance(m, rel, body, q)
        hit = re.fullmatch(r"/allocation/([^/]+)/constraint/([^/]+)", rel)
        if hit:
            a = self.objs["allocation"][hit[1]]
            c = next(c for c in a["allocationConstraints"] if c["id"] == hit[2])
            c["constraintValue"] = body["constraintValue"]
            return 200, c
        kind, _, oid = rel.strip("/").partition("/")
        if kind in self.objs:
            if m == "GET" and not oid:
                return 200, self._list(kind, q)
            if m == "POST":
                o = {"id": str(uuid.uuid4()), **body, "status": "Ready"}
                if kind == "infiniband-partition":
                    o["partitionKey"] = (body.get("labels") or {}).get(
                        "neocloud.io/partition-key") or "0x8001"
                if kind == "allocation":
                    for c in o["allocationConstraints"]:
                        c["id"] = str(uuid.uuid4())
                if kind == "vpc":
                    o["vni"] = 20001
                self.objs[kind][o["id"]] = o
                return 201, o
            if oid not in self.objs[kind]:
                return 404, {"source": "nico", "message": "not found", "data": None}
            if m == "GET":
                return 200, self.objs[kind][oid]
            if m == "PATCH":
                self.objs[kind][oid].update(body)
                return 200, self.objs[kind][oid]
            if m == "DELETE":
                if kind == "vpc" and self._list("instance", {"vpcId": oid}):
                    return 400, {"message": "VPC has instances"}
                self.objs[kind].pop(oid)
                return 202, {"message": "accepted"}
        return 404, {"message": f"no route {m} {path}"}

    def _machine(self, m, rel, body):
        parts = rel.strip("/").split("/")
        if len(parts) == 1:
            return 200, list(self.m.values())
        mach = self.m.get(parts[1])
        if mach is None:
            return 404, {"message": "machine not found"}
        if len(parts) == 3 and parts[2] == "status-history":
            return 200, mach["statusHistory"]
        if m == "PATCH":
            for k in ("labels", "maintenanceMessage"):
                if k in body:
                    mach[k] = body[k]
        elif mach["lifecycleState"] == "sanitizing":
            self.cleanup_left -= 1
            if self.cleanup_left < 0:
                mach.update(lifecycleState="ready", status="Ready")
                mach["statusHistory"].append({"status": "Ready", "message": "sanitize complete"})
        return 200, dict(mach)

    def _instance(self, m, rel, body, q):
        parts = rel.strip("/").split("/")
        if len(parts) == 1:
            if m == "GET":
                return 200, self._list("instance", q)
            mach = self.m[body["machineId"]]
            if mach["lifecycleState"] != "ready":
                return 400, {"message": "machine not Ready"}
            i = {"id": f"inst-{len(self.objs['instance']) + len(self.dead) + 1}", **body,
                 "siteId": mach["siteId"], "status": "Ready", "infinibandInterfaces":
                     body.get("infinibandInterfaces") or [],
                 "statusHistory": [{"status": "Provisioning"}, {"status": "Ready"}]}
            self.objs["instance"][i["id"]] = i
            mach.update(lifecycleState="assigned", status="InUse", instanceId=i["id"])
            return 201, i
        iid = parts[1]
        i = self.objs["instance"].get(iid) or self.dead.get(iid)
        if i is None:
            return 404, {"message": "instance not found"}
        if len(parts) == 3:
            return 200, i["statusHistory"]
        if m == "GET":
            return (404, {"message": "gone"}) if iid in self.dead else (200, i)
        if m == "PATCH":
            i.update(body)
            return 200, i
        if m == "DELETE":
            self.dead[iid] = self.objs["instance"].pop(iid)
            i["statusHistory"].append({"status": "Terminating"})
            mach = self.m[i["machineId"]]
            mach.update(lifecycleState="sanitizing", status="Reset", instanceId=None)
            mach["statusHistory"].append({"status": "Reset", "message": "sanitizing"})
            self.cleanup_left = self.cleanup_polls
            return 202, {"message": "accepted"}


@pytest.fixture
def nico(monkeypatch):
    monkeypatch.setenv("NOCP_NICO_POLL_S", "0")
    f = FakeNico()
    c = httpx.Client(base_url="http://nico",
                     transport=httpx.MockTransport(lambda r: _respond(f, r)))
    return f, NicoRestAdapter("http://nico", org="neocloud", client=c)


def _respond(f, req):
    status, body = f.handle(req)
    return httpx.Response(status, json=body)


def _writes(f):
    return [(m, re.sub(r"[0-9a-f-]{36}|inst-\d+", "{id}", p)) for m, p in f.calls if m != "GET"]


def _order(a, hosts=("nh-a", "nh-b"), tenant="tnt-a"):
    for h in hosts:
        a.reserve(h)
    out = []
    for h in hosts:
        assert wait_job(a, a.provision(h, "ubuntu")).state == "succeeded"
        out.append(a.allocate(h, tenant))
    seg = a.create_segment(tenant, "VRF-a", 30001, 40001, list(hosts),
                           allocation_id="alloc-1", ib_pkey=0x8042)
    return out, seg


def test_allocate_and_isolate_call_order(nico):
    f, a = nico
    hosts, seg = _order(a)
    assert all(h.state.value == "allocated" and h.instance_id for h in hosts)
    w = _writes(f)
    assert w[:4] == [("PATCH", f"{P}/machine/nh-a"), ("PATCH", f"{P}/machine/nh-b"),
                     ("PATCH", f"{P}/machine/nh-a"), ("POST", f"{P}/vpc")]
    first_alloc = w.index(("POST", f"{P}/allocation"))
    first_inst = w.index(("POST", f"{P}/instance"))
    assert w.index(("POST", f"{P}/vpc")) < first_alloc < first_inst
    assert w.count(("POST", f"{P}/vpc")) == 1                  # one tenant VPC
    assert ("PATCH", f"{P}/allocation/{{id}}/constraint/{{id}}") in w   # 2nd host grows quota
    for kind in ("infiniband-partition", "nvlink-logical-partition", "spectrumx-partition"):
        assert w.count(("POST", f"{P}/{kind}")) == 1
    assert w.count(("PATCH", f"{P}/instance/{{id}}")) == 2      # DPU ↔ IB partition per host
    ib = next(iter(f.objs["infiniband-partition"].values()))
    assert ib["partitionKey"] == "0x8042" and seg.ib_pkey == 0x8042
    assert seg.segment_id in f.objs["vpc"] and seg.host_ids == ["nh-a", "nh-b"]
    assert not any(p.startswith("/nico-bridge") for _, p in f.calls)
    assert a.list_segments()[0].segment_id == seg.segment_id


def test_release_then_partition_delete(nico):
    f, a = nico
    hosts, seg = _order(a)
    a.delete_segment(seg.segment_id)                 # lifecycle order: segment first …
    assert f.objs["infiniband-partition"]           # … but instances live → kept (B2-13)
    for h in hosts:
        assert wait_job(a, a.release(h.instance_id)).state == "succeeded"
        f.calls.append(("MARK", h.host_id))
        job = wait_job(a, a.sanitize(h.host_id))
        assert job.state == "succeeded"
        assert a.get_host(h.host_id).state.value == "pool_ready"
        assert a.get_sanitize_report(h.host_id).passed
    seq = [(m, p) for m, p in f.calls]
    i_del = max(i for i, c in enumerate(seq) if c[0] == "DELETE" and "/instance/" in c[1])
    i_hist = max(i for i, c in enumerate(seq) if c[1].endswith("/status-history")
                 and "/instance/" in c[1])
    i_part = min(i for i, c in enumerate(seq) if c[0] == "DELETE" and "partition" in c[1])
    assert i_del < i_hist < i_part                 # DELETE → status-history → partition DELETE
    assert not f.objs["infiniband-partition"] and not f.objs["vpc"] \
        and not f.objs["spectrumx-partition"] and not f.objs["nvlink-logical-partition"]
    assert not f.objs["allocation"]
    assert not any(p.startswith("/nico-bridge") for _, p in f.calls)


def test_sanitize_waits_for_cleanup(nico):
    f, a = nico
    f.cleanup_polls = 3
    hosts, _ = _order(a, hosts=("nh-a",))
    wait_job(a, a.release(hosts[0].instance_id))
    job = a.sanitize("nh-a")
    assert job.state == "running"
    assert wait_job(a, job).state == "succeeded"


def test_reserve_refuses_busy_machine_and_cordon(nico):
    f, a = nico
    a.reserve("nh-a")
    with pytest.raises(Exception) as e:
        a.reserve("nh-a")                           # held
    assert e.value.status_code == 409
    a.unreserve("nh-a")
    assert a.get_host("nh-a").state.value == "pool_ready"
    assert a.cordon("nh-b", "rma-1").cordoned
    assert f.m["nh-b"]["maintenanceMessage"] == "rma-1"


def test_expansion_reuses_segment_and_binds_new_host(nico):
    f, a = nico
    f.m["nh-c"] = {**f.m["nh-a"], "id": "nh-c", "labels": {}, "statusHistory": []}
    _order(a, hosts=("nh-a", "nh-b"))
    a.reserve("nh-c")
    h = a.allocate("nh-c", "tnt-a")                 # VPC already carries the IB partition
    inst = f.objs["instance"][h.instance_id]
    ib = next(iter(f.objs["infiniband-partition"]))
    assert inst["infinibandInterfaces"] == [{"partitionId": ib, "isPhysical": True}]
    seg = a.create_segment("tnt-a", "VRF-a", 30001, 40001, ["nh-c"], ib_pkey=0x8042)
    assert len(f.objs["infiniband-partition"]) == 1 and "nh-c" in seg.host_ids
    seg = a.attach_hosts(seg.segment_id, ["nh-c"], purpose="converged")
    assert seg.host_ids.count("nh-c") == 1


def test_build_http_adapter_rest_has_no_bridge_client(monkeypatch):
    from app.adapters import build_http_adapter
    monkeypatch.delenv("NOCP_NICO_API", raising=False)
    a = build_http_adapter("http://x:9000/nico-bridge")
    assert isinstance(a, NicoRestAdapter)
    assert str(a._c.base_url).rstrip("/") == "http://x:9000"
