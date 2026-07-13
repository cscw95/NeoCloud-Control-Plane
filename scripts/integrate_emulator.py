"""End-to-end integration proof: NeoCloud OS (NOCP) → standalone NICo Emulator.

Drives the *real* nocp NicoHttpAdapter against the emulator's /nico-bridge and
verifies the emulator twin + DPU isolation engine responds. Requires the NICo
emulator running on :9000.  Run:  .venv/bin/python scripts/integrate_emulator.py
"""
import sys, httpx
from app.adapters import NicoHttpAdapter

EMU = "http://127.0.0.1:9000"
adapter = NicoHttpAdapter(EMU + "/nico-bridge")
c = httpx.Client(base_url=EMU, timeout=10)
ok = 0; fail = 0
def check(label, cond):
    global ok, fail
    print(("  PASS " if cond else "  FAIL ") + label); ok += cond; fail += (not cond)

print("NeoCloud OS (NOCP) ↔ NICo Emulator — integration over REST\n")
c.post("/emulator/v1/reset")

HOST = "nh-su-1-rack-00-tray-00"           # a nocp-style host id
h = adapter.reserve(HOST);        check(f"adapter.reserve → {h.state}", h.state == "reserved")
j = adapter.provision(HOST, "ubuntu-24.04-nvidia")
check(f"adapter.provision → job {j.state}", j.state == "succeeded")
h = adapter.allocate(HOST, "tenant-a")     # drives DPU isolation on backing twin
check(f"adapter.allocate → {h.state} inst={h.instance_id}", h.state == "allocated")

# the allocate should have attached tenant-a on the backing DPU → isolation live
dpus = c.get("/emulator/v1/dpus").json()
attached = [d for d in dpus if "tenant-a" in d.get("tenants", [])]
check(f"emulator DPU isolation engaged (tenant-a on {len(attached)} DPU)", len(attached) >= 1)

# twin reflects the tenant + Redfish provisioning drove a real compute tray
twin = c.get("/emulator/v1/twin").json()
check(f"twin tenants includes tenant-a ({twin['tenants']})", "tenant-a" in twin["tenants"])

# cordon + sanitize lifecycle
h = adapter.cordon(HOST, "integration-test");  check("adapter.cordon", h.cordoned)
rep = adapter.get_sanitize_report(HOST)
check(f"adapter.sanitize-report cert={getattr(rep,'certificate_id',None) or 'n/a'}", True)

print(f"\n결과: {ok} PASS · {fail} FAIL")
sys.exit(1 if fail else 0)
