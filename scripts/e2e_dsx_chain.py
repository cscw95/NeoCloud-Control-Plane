"""DSX live-chain E2E: Console(:8090) -> NOCP(:8000) -> NICo(:9000) -> AI-Infra(:9100).

Drives every DSX flow over HTTP against the running services (see each repo's
run.sh) and checks that the upstream emulators really saw the change.

    .venv/bin/python scripts/e2e_dsx_chain.py              # dev mode (run.sh switches)
    E2E_SECURE=1 E2E_OPERATOR_TOKEN=op E2E_TENANT_TOKEN=ta E2E_TENANT=tnt-e2e-a \\
    E2E_OTHER_TOKEN=tb E2E_OTHER_TENANT=tnt-e2e-b E2E_EMU_API_KEY=k \\
        .venv/bin/python scripts/e2e_dsx_chain.py          # secure mode

Secure mode expects NOCP started without the dev switches and with NOCP_TOKENS
mapping the tokens above (operator / tenant:<id>), and both emulators started
with EMU_API_KEY (NOCP with NOCP_EMU_API_KEY). Exit code 0 when all checks pass.
"""
import os
import sys
import time

import httpx

NOCP = os.environ.get("E2E_NOCP", "http://127.0.0.1:8000")
NICO = os.environ.get("E2E_NICO", "http://127.0.0.1:9000")
AI = os.environ.get("E2E_AI_INFRA", "http://127.0.0.1:9100")
CONSOLE = os.environ.get("E2E_CONSOLE", "http://127.0.0.1:8090")
SECURE = os.environ.get("E2E_SECURE", "0") == "1"
OP_TOK = os.environ.get("E2E_OPERATOR_TOKEN")
TA_TOK = os.environ.get("E2E_TENANT_TOKEN")
TB_TOK = os.environ.get("E2E_OTHER_TOKEN")
TA = os.environ.get("E2E_TENANT", "tnt-e2e-a")
TB = os.environ.get("E2E_OTHER_TENANT", "tnt-e2e-b")
EMU_KEY = os.environ.get("E2E_EMU_API_KEY")
ONLY = set(filter(None, os.environ.get("E2E_ONLY", "").split(",")))

results: list = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name + (f"  — {detail}" if detail else ""))
    return bool(cond)


def section(name):
    print(f"\n[{name}]")
    return not ONLY or name in ONLY


def _hdr(tok=None, tenant=None):
    if SECURE:
        return {"Authorization": f"Bearer {tok}"} if tok else {}
    return {"X-Tenant-Id": tenant} if tenant else {}


op = httpx.Client(base_url=NOCP, timeout=60, headers=_hdr(OP_TOK))
ta = httpx.Client(base_url=NOCP, timeout=60, headers=_hdr(TA_TOK, TA))
tb = httpx.Client(base_url=NOCP, timeout=60, headers=_hdr(TB_TOK, TB))
emu_h = {"X-API-Key": EMU_KEY} if EMU_KEY else {}
ai = httpx.Client(base_url=AI, timeout=30, headers=emu_h)
nico = httpx.Client(base_url=NICO, timeout=30, headers=emu_h)


NREST = "/v2/org/neocloud/nico"           # F08: e2e reads NICo REST too (no /nico-bridge)
_LC = {"assigned": "allocated", "ready": "pool_ready"}


def _as_host(m):
    return {"host_id": m.get("id"), "state": _LC.get(m.get("lifecycleState"), m.get("lifecycleState")),
            "tenant_ref": m.get("tenantId"), "instance_id": m.get("instanceId"),
            "cordoned": bool(m.get("maintenanceMessage"))}


def host(hid):
    return _as_host(nico.get(f"{NREST}/machine/{hid}").json())


def bridge_calls():
    return nico.get("/emulator/v1/bridge-calls").json().get("count", -1)


def wait(fn, pred, tries=90, delay=1.0):
    v = fn()
    for _ in range(tries):
        if pred(v):
            return v
        time.sleep(delay)
        v = fn()
    return v


print(f"DSX chain E2E — {'SECURE' if SECURE else 'dev'} mode, NOCP {NOCP}")
BRIDGE0 = bridge_calls()

# ---------------------------------------------------------------------------
if section("reach"):
    check("AI-Infra up", httpx.get(AI + "/versions", timeout=5).status_code == 200)
    check("NICo up", httpx.get(NICO + "/versions", timeout=5).status_code == 200)
    check("Console up", httpx.get(CONSOLE + "/", timeout=5).status_code == 200)
    v = op.get("/api/v1/platform/versions").json()
    check("NOCP versions + live emulators", v.get("dsx_os") == "2026.05"
          and all((v.get("live") or {}).values()), f"nico_api={v.get('nico_api')}")
    cat = op.get("/api/v1/catalog").json()
    check("catalog bmaas + managed-k8s",
          {p["id"] for p in cat["products"]} >= {"bmaas", "managed-k8s"})

for tid in (TA, TB):
    op.post("/api/v1/tenants", json={"name": tid.removeprefix("tnt-"),
                                     "isolation_tier": "bare_metal_dedicated"})

# ---------------------------------------------------------------------------
if section("security") and SECURE:
    r = httpx.get(NOCP + "/api/v1/tenants", timeout=10)
    check("no token → 401", r.status_code == 401, str(r.status_code))
    r = httpx.get(NOCP + "/api/v1/tenants", timeout=10, headers={"X-Operator": "true"})
    check("X-Operator header alone → 401", r.status_code == 401, str(r.status_code))
    r = ta.get(f"/api/v1/tenants/{TB}")
    check("tenant A reads tenant B → 403", r.status_code == 403, str(r.status_code))
    r = ta.get("/api/v1/oob/summary")
    check("tenant on operator surface → 403", r.status_code == 403, str(r.status_code))
    r = ta.get(f"/api/v1/isolation/{TB}")
    check("cross-tenant isolation view → 403", r.status_code == 403, str(r.status_code))
    r = httpx.post(AI + "/emulator/v1/oob/recover", json={}, timeout=10)
    check("AI-Infra mutation without key → 401", r.status_code == 401, str(r.status_code))
    r = httpx.patch(NICO + NREST + "/machine/nh-x", json={"maintenanceMessage": "x"}, timeout=10)
    check("NICo mutation without key → 401", r.status_code == 401, str(r.status_code))

# ---------------------------------------------------------------------------
if section("bmaas"):
    o = ta.post("/api/v1/orders", json={"tenant_id": TA, "kind": "new",
                                        "blueprint_key": "vr-nvl72", "racks": 1})
    check("order intake (tenant)", o.status_code in (200, 201), o.text[:160])
    oid = o.json().get("id")
    fin = wait(lambda: ta.get(f"/api/v1/orders/{oid}").json(),
               lambda x: x.get("state") in ("delivered", "failed", "rolled_back"))
    check("order delivered", fin.get("state") == "delivered", fin.get("state"))
    alloc = next((a["id"] for a in ta.get(f"/api/v1/tenants/{TA}").json()
                  .get("allocations", [])), None)
    hosts = [_as_host(m) for m in nico.get(f"{NREST}/machine", params={
        "tenantId": TA, "pageSize": 100}).json()]
    check("NICo hosts allocated to tenant", hosts and all(h["state"] == "allocated"
                                                          for h in hosts), f"{len(hosts)} hosts")
    m = ta.get(f"/api/v1/metering/usage?tenant={TA}").json()
    check("metering has the bmaas order line",
          any(ln.get("product") == "bmaas" and ln.get("active") for ln in m.get("lines", [])),
          f"instance_hours={m.get('instance_hours')}")
    iso = ta.get(f"/api/v1/isolation/{TA}").json()
    check("isolation matrix ok", iso.get("ok") is True,
          ",".join(f"{x['layer']}={x['status']}" for x in iso.get("layers", [])))
    if SECURE:
        r = tb.get(f"/api/v1/orders/{oid}")
        check("tenant B reads tenant A order → 403/404", r.status_code in (403, 404),
              str(r.status_code))
    t = ta.post("/api/v1/orders", json={"tenant_id": TA, "kind": "terminate",
                                        "allocation_id": alloc})
    tid = t.json().get("id")
    tfin = wait(lambda: ta.get(f"/api/v1/orders/{tid}").json(),
                lambda x: x.get("state") in ("closed", "failed"))
    check("release/sanitize order closed", tfin.get("state") == "closed", tfin.get("state"))
    back = [host(h["host_id"])["state"] for h in hosts]
    check("hosts sanitized back to pool_ready", back and all(s == "pool_ready" for s in back),
          ",".join(sorted(set(back))))

# ---------------------------------------------------------------------------
if section("nvke"):
    rel = ta.get("/api/v1/nvke/releases").json()
    check("NVKE releases", {"1.33", "1.34"} <= set(rel.get("k8s_supported", [])))
    r = ta.post("/api/v1/nvke/clusters", json={
        "name": "e2e", "tenant_id": TA, "k8s_version": "1.33",
        "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 1}]})
    check("NVKE cluster create", r.status_code == 201, r.text[:160])
    cl = r.json() if r.status_code == 201 else {}
    cid = cl.get("id")
    if cid:
        np = cl["nodepools"][0]
        n0 = np["nodes"][0]["nico_host_id"]
        check("node host allocated in NICo", host(n0).get("state") == "allocated"
              and host(n0).get("tenant_ref") == TA, n0)
        r = ta.patch(f"/api/v1/nvke/clusters/{cid}/nodepools/{np['id']}", json={"count": 2})
        nodes = r.json().get("nodes", []) if r.status_code == 200 else []
        check("nodepool scale 1→2", r.status_code == 200 and len(nodes) == 2
              and host(nodes[1]["nico_host_id"]).get("state") == "allocated", str(r.status_code))
        k = ta.get(f"/api/v1/nvke/clusters/{cid}/kubeconfig")
        check("kubeconfig as owner", k.status_code == 200 and "kubeconfig" in k.json())
        k = tb.get(f"/api/v1/nvke/clusters/{cid}/kubeconfig")
        check("kubeconfig as other tenant → 403", k.status_code == 403, str(k.status_code))
        k = op.get(f"/api/v1/nvke/clusters/{cid}/kubeconfig")
        check("kubeconfig as operator → 403 (owner only)", k.status_code == 403,
              str(k.status_code))
        u = ta.post(f"/api/v1/nvke/clusters/{cid}/upgrade", json={"k8s_version": "1.34"})
        cur = wait(lambda: ta.get(f"/api/v1/nvke/clusters/{cid}").json(),
                   lambda x: str(x.get("k8s_version", "")).startswith("v1.34")
                   and x.get("state") == "running", tries=30)
        check("upgrade 1.33→1.34", u.status_code == 200
              and str(cur.get("k8s_version")).startswith("v1.34"), cur.get("k8s_version"))
        rp = op.post(f"/api/v1/nvke/clusters/{cid}/nodes/gpu-0/replace", json={})
        rj = rp.json() if rp.status_code == 200 else {}
        check("node replace (NVSentinel)", rp.status_code == 200
              and host(rj.get("new_host", "x")).get("state") == "allocated"
              and host(n0).get("cordoned") is True, rj.get("new_host"))
        m = ta.get(f"/api/v1/metering/usage?tenant={TA}").json()
        check("metering has NVKE node lines",
              any(ln.get("product") in ("managed-k8s", "nvke") for ln in m.get("lines", [])),
              f"k8s_node_hours={m.get('k8s_node_hours')}")
        pool_hosts = [n["nico_host_id"] for n in
                      ta.get(f"/api/v1/nvke/clusters/{cid}/nodepools").json()[0]["nodes"]
                      if n.get("state") != "removed"]
        d = ta.delete(f"/api/v1/nvke/clusters/{cid}")
        check("cluster delete returns hosts to pool", d.status_code == 200 and all(
            host(h).get("state") == "pool_ready" for h in pool_hosts), str(pool_hosts))
        # RMA on the faulty (replaced) host: cordon → … → pool_return (sanitized)
        rc = op.post("/api/v1/rma", json={"machine": n0, "reason": "e2e replace"})
        rid = rc.json().get("id")
        for _ in range(5):
            op.post(f"/api/v1/rma/{rid}/advance")
        case = op.get(f"/api/v1/rma/{rid}").json()
        h0 = host(n0)
        check("RMA advance to pool_return", case.get("stage") == "pool_return"
              and h0.get("state") == "pool_ready" and not h0.get("cordoned"),
              f"{case.get('stage')} / NICo {h0.get('state')} cordoned={h0.get('cordoned')}")

# ---------------------------------------------------------------------------
if section("power"):
    d = op.get("/api/v1/power/domains").json()
    doms = d.get("domains", [])
    check("power domains from AI-Infra", d.get("source") == "ai-infra" and doms
          and all(x.get("rack_count") for x in doms),
          ",".join(f"{x['id']}:{x.get('rack_count')}" for x in doms))
    dom = doms[0]["id"] if doms else "pd-aif-stt-gasan-f1"
    want = {"pd-aif-stt-gasan-f1": 6000, "pd-aif-stt-gasan-f2": 3000,
            "pd-aif-igis-ansan-f1": 12900, "pd-aif-igis-ansan-f2": 10400}
    got = {x["id"]: x.get("operating_limit_kw") for x in doms}
    check("one DPS domain per NOCP ComputeBlock, limit = block budget",
          got == want and all(x.get("site_budget_kw") == x.get("operating_limit_kw")
                              and x.get("budget_ok") for x in doms), str(got))
    bd = "pd-aif-stt-gasan-f2"
    r = op.patch(f"/api/v1/power/domains/{bd}", json={"operating_limit_kw": 3001})
    check("domain PATCH above block budget → 422, DPS unchanged", r.status_code == 422
          and ai.get(f"/dps/v1/power-domains/{bd}").json().get("operating_limit_kw") == 3000,
          str(r.status_code))
    g = op.get(f"/api/v1/power/domains/{dom}").json()
    check("power domain detail", g.get("source") == "ai-infra" and g["domain"]["rack_count"])
    p = op.get("/api/v1/power/profiles").json()
    names = {x.get("id") or x.get("name") for x in p.get("profiles", [])}
    check("WPPS profiles", {"max-p", "max-q", "training", "inference", "balanced"} <= names)
    s = op.post("/api/v1/power/simulate", json={"domain": dom, "operating_limit_kw": 3000,
                                                 "smoothing": True}).json()
    check("simulate static vs dynamic", s.get("source") == "ai-infra"
          and "racks_supported" in s.get("static", {}) and "racks_supported" in s.get("dynamic", {}),
          f"static={s.get('static', {}).get('racks_supported')} "
          f"dynamic={s.get('dynamic', {}).get('racks_supported')}")
    r = op.patch(f"/api/v1/power/domains/{dom}", json={"mode": "dynamic"})
    check("domain mode → dynamic", r.status_code == 200 and
          ai.get(f"/dps/v1/power-domains/{dom}").json().get("mode") == "dynamic")
    op.patch(f"/api/v1/power/domains/{dom}", json={"mode": "static"})
    rack = "su-1-rack-04"
    r = op.post(f"/api/v1/racks/{rack}/power-policy",
                json={"policy": "dps-dynamic", "profile": "inference"})
    rj = r.json()
    pl = ai.get(f"/redfish/v1/Chassis/{rack}/Controls/PowerLimit").json()
    check("policy dps-dynamic + WPPS → AI-Infra Redfish PowerLimit",
          r.status_code == 200 and rj.get("cap_source") == "ai-infra"
          and float(pl.get("SetPoint") or 0) == rj["power_cap_kw"] * 1000,
          f"cap={rj.get('power_cap_kw')} kW SetPoint={pl.get('SetPoint')} "
          f"effective={pl.get('Oem', {}).get('Nvidia', {}).get('EffectiveLimitWatts')}")
    nv = pl.get("Oem", {}).get("Nvidia", {})
    check("Redfish rack range = MaxP 227 kW, 170 kW SetPoint not clamped",
          pl.get("AllowableMax") == 227000 and rj.get("power_cap_kw") == 170
          and abs(float(nv.get("EffectiveLimitWatts") or 0) - 170000) <= 100,
          f"AllowableMax={pl.get('AllowableMax')} effective={nv.get('EffectiveLimitWatts')}")
    over = ai.patch(f"/redfish/v1/Chassis/{rack}/Controls/PowerLimit", json={"SetPoint": 230000})
    check("Redfish SetPoint above AllowableMax → 400", over.status_code == 400, str(over.status_code))
    ps = ai.get(f"/redfish/v1/Chassis/{rack}/PowerSubsystem").json()
    check("power shelf capacity 4 × 110 kW", ps.get("CapacityWatts") == 440000,
          str(ps.get("CapacityWatts")))
    # ansan-f2: 50 racks × 187 kW (maxq) = 9350 of 10400 kW; 26 racks fit maxp, the 27th is refused
    f2 = [f"su-{su}-rack-{i:02d}" for su, n in ((8, 16), (9, 16), (10, 16), (11, 2))
          for i in range(n)]
    codes = [op.post(f"/api/v1/racks/{rid}/power-policy", json={"policy": "maxp"}).status_code
             for rid in f2[:27]]
    check("rack policy over the ComputeBlock budget → 422", codes[:26] == [200] * 26
          and codes[26] == 422, f"{codes.count(200)} accepted, last={codes[26]}")
    for rid in f2[:26]:
        op.post(f"/api/v1/racks/{rid}/power-policy", json={"policy": "maxq"})
    tel = ai.get(f"/dps/v1/telemetry?rack={rack}").json()
    prof = next((x.get("profile") for dd in tel.get("domains", []) for gg in dd.get("groups", [])
                 for x in gg.get("racks", []) if x.get("rack_id") == rack), None)
    check("WPPS profile applied on AI-Infra rack", prof == "inference", str(prof))
    f = op.post("/api/v1/power/flex-events", json={"domain": dom, "reduce_kw": 100,
                                                    "duration_s": 30})
    fj = f.json()
    check("DSX Flex event", f.status_code == 201 and fj.get("source") == "ai-infra",
          fj.get("id"))
    t = op.get(f"/api/v1/power/telemetry?domain={dom}").json()
    check("power telemetry", t.get("source") == "ai-infra" and t.get("domains"))

# ---------------------------------------------------------------------------
if section("oob"):
    s = op.get("/api/v1/oob/summary").json()
    check("OOB summary from AI-Infra", s.get("source") == "ai-infra" and s["racks"]
          and all(r.get("rack") for r in s["racks"]), f"{len(s['racks'])} racks")
    sub = op.post("/api/v1/oob/subscribe").json()
    check("EventService subscription (idempotent)", sub.get("subscribed"), sub.get("id"))
    rack, tray = "su-1-rack-06", "su-1-rack-06-tray-03"
    ai.post("/emulator/v1/oob/recover", json={})
    time.sleep(1.0)                          # let the recover events land first
    last = max([e["id"] for e in op.get("/api/v1/events/recent?limit=1000").json()] or [0])
    ai.post("/emulator/v1/oob/inject", json={"kind": "leak", "target": tray,
                                             "severity": "Critical"})
    ai.post("/emulator/v1/oob/inject", json={"kind": "psu_fail", "target": rack})
    seen = {x["id"] for x in op.get(f"/api/v1/oob/events?rack={rack}&limit=500").json()["events"]}
    ev = wait(lambda: [x for x in op.get(f"/api/v1/oob/events?rack={rack}&limit=500").json()["events"]
                       if x["id"] not in seen and x["severity"] == "Critical"],
              lambda e: {"leak", "power"} <= {x["topic"] for x in e}, tries=15)
    leak = next((x for x in ev if x["topic"] == "leak"), {})
    check("leak + psu events reach /api/v1/oob/events", {"leak", "power"} <= {x["topic"] for x in ev},
          f"{len(ev)} events, leak machine={leak.get('machine')}")
    check("leak event carries the tray machine", leak.get("machine") == tray, leak.get("machine"))
    rec = op.get(f"/api/v1/events/recent?after={last}&topic=leak").json()
    check("leak republished on /api/v1/events bus", any(
        (x.get("data") or {}).get("rack") == rack for x in rec), f"{len(rec)} leak events")
    q = f"topic=leak&after={last}&once=true"
    if SECURE:
        q += "&token=" + op.post("/api/v1/events/token").json().get("token", "")
    sse = httpx.get(f"{NOCP}/api/v1/events?{q}", timeout=10)
    check("SSE /api/v1/events streams the leak", sse.status_code == 200
          and "event: leak" in sse.text and rack in sse.text,
          f"{sse.status_code} {sse.headers.get('content-type')} {len(sse.text)} bytes")
    dup = [x for x in ev if x["topic"] == "leak"]
    check("no duplicate deliveries (one subscription)", len(dup) == 1, f"{len(dup)} leak events")
    rd = op.get(f"/api/v1/oob/racks/{rack}").json()
    check("rack detail reflects AI-Infra (leak, PSU)", rd.get("leak") == "Critical"
          and rd.get("psu_ok", 0) < rd.get("psu_total", 0) and rd.get("trays"),
          f"leak={rd.get('leak')} psu={rd.get('psu_ok')}/{rd.get('psu_total')}")
    sel_before = len(op.get(f"/api/v1/oob/sel/{tray}").json().get("entries", []))
    b = op.post(f"/api/v1/oob/machines/nh-{tray}/actions/gpu-reset")
    a = op.post(f"/api/v1/oob/machines/nh-{tray}/actions/ac-power-cycle")
    c2 = op.post(f"/api/v1/oob/machines/nh-{tray}/actions/gpu-reset")
    check("gpu-reset while tray reboots → 409 (BMC refusal surfaced)",
          c2.status_code == 409, str(c2.status_code))
    sel = op.get(f"/api/v1/oob/sel/{tray}").json().get("entries", [])
    msgs = " ".join(str(e.get("MessageId", "")) for e in sel)
    check("ac-power-cycle + gpu-reset via NICo → AI-Infra",
          a.status_code == 202 and b.status_code == 202 and a.json().get("via") == "nico"
          and len(sel) >= sel_before + 2 and "AC_POWER_CYCLE" in msgs and "GPU_RESET" in msgs,
          f"via={a.json().get('via')} SEL {sel_before}→{len(sel)}")
    ai.post("/emulator/v1/oob/recover", json={})

# ---------------------------------------------------------------------------
if section("ncp"):
    fb = op.get("/api/v1/firmware/baseline").json()
    check("firmware baseline from NICo", fb.get("source") == "nico" and fb.get("components"))
    fd = op.get("/api/v1/firmware/drift").json()
    check("firmware drift from NICo", fd.get("source") == "nico" and fd.get("drift")
          and all(x.get("machine") and x.get("current") for x in fd["drift"]),
          f"{len(fd.get('drift', []))} rows")
    m = op.get("/api/v1/machines").json()
    check("machines view (NICo REST)", m.get("source") == "nico" and m.get("machines"))

# ---------------------------------------------------------------------------
if section("infra"):
    r = op.get("/api/v1/infra/emulator/v1/twin")
    check("infra proxy GET twin", r.status_code == 200 and r.json().get("racks") == 140)
    r = op.get("/api/v1/infra/dps/v1/profiles")
    check("infra proxy GET dps", r.status_code == 200)
    r = op.get("/api/v1/infra/emulator/v1/reset")
    check("infra proxy blocks reset", r.status_code == 403)
    r = op.get("/api/v1/infra/..%2fversions")
    check("infra proxy blocks traversal", r.status_code in (400, 403, 404))
    r = op.post("/api/v1/infra/emulator/v1/oob/recover", json={})
    check("infra proxy POST (emulator key forwarded)", r.status_code == 200, str(r.status_code))

# ---------------------------------------------------------------------------
if section("nico-rest"):
    n = bridge_calls()
    check("F08: zero /nico-bridge calls during the run (NICo call log)",
          n == BRIDGE0 == 0, f"before={BRIDGE0} after={n}")
    evs = nico.get("/emulator/v1/events?limit=2000").json()
    ids = {e["message_id"] for e in evs}
    check("F08: NICo saw REST instance create + delete",
          {"NICo.2.3.InstanceCreated", "NICo.2.3.InstanceDeleted"} <= ids)
    check("F08: no DeprecatedBridgeCall event", "NICo.2.3.DeprecatedBridgeCall" not in ids)
    mev = wait(lambda: op.get("/api/v1/events/recent?topic=machine&limit=1000").json(),
               lambda e: any(x["data"].get("instance_state") == "ready" for x in e), tries=10)
    check("F12: NICo machine state (assigned/ready) on NOCP bus topic machine",
          any(x["data"].get("instance_state") == "ready" for x in mev), f"{len(mev)} events")
    check("F12: upstream channel NICO/v1/machine/{id}/state",
          all(x["data"]["channel"].startswith("NICO/v1/machine/") for x in mev) and mev)

if section("token"):
    # F01 gateway: key → /v1/chat/completions (Dynamo) → usage once → billing line (F05)
    op.post("/api/v1/tenants", json={"name": "e2e-tok", "isolation_tier": "k8s_namespace"})
    kr = op.post("/api/v1/token/keys", json={"tenant_id": "tnt-e2e-tok", "name": "e2e"})
    check("token key issued", kr.status_code == 201, str(kr.status_code))
    if kr.status_code == 201:
        kh = {"Authorization": f"Bearer {kr.json()['key']}", "x-request-id": "e2e-tok-1"}
        body = {"model": "llama-3.3-70b-instruct", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hello"}]}
        gw = httpx.Client(base_url=NOCP, timeout=60)
        r1 = gw.post("/v1/chat/completions", json=body, headers=kh)
        check("token chat via Dynamo", r1.status_code == 200
              and r1.headers.get("x-neocloud-engine") == "dynamo",
              f"{r1.status_code} engine={r1.headers.get('x-neocloud-engine')}")
        r2 = gw.post("/v1/chat/completions", json=body, headers=kh)
        u = op.get("/api/v1/token/usage", params={"tenant_id": "tnt-e2e-tok"}).json()
        n = sum(1 for x in u.get("records", []) if x["request_id"] == "e2e-tok-1")
        check("token retry billed once", r2.headers.get("x-idempotent-replay") == "true" and n == 1,
              f"records={n}")
        st = gw.post("/v1/chat/completions", json={**body, "stream": True},
                     headers={**kh, "x-request-id": "e2e-tok-sse"})
        check("token SSE usage in last chunk", '"choices": []' in st.text
              and '"usage"' in st.text and st.text.rstrip().endswith("data: [DONE]"))
        ir = op.post("/api/v1/billing/invoices:run", json={"tenant_id": "tnt-e2e-tok",
                                                           "period": time.strftime("%Y-%m", time.gmtime())})
        if ir.status_code == 404:
            print("  SKIP token invoice line (F05 billing not in this build)")
        else:
            # invoices:run returns summaries; the lines are on GET /billing/invoices/{id}
            lines = [ln for inv in (ir.json().get("invoices") or [])
                     for ln in op.get(f"/api/v1/billing/invoices/{inv['id']}").json().get("lines", [])]
            check("token line on invoice", any(ln.get("metric") == "token" for ln in lines),
                  str(ir.status_code))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} PASS")
for n in failed:
    print("  FAILED:", n)
sys.exit(1 if failed else 0)
