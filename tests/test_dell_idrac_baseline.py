"""Dell iDRAC10 firmware baseline must equal what AI-Infra-Emulator's Redfish reports.

AI-Infra serves Managers/{tray}.FirmwareVersion = app/dell.py IDRAC_FW, and NICo's
Compute BMC baseline is the same number, so drift is computed against real values.
"""
import re
from pathlib import Path

import pytest

from app import ncp, nico_fake

ROOT = Path(__file__).resolve().parents[2]


def _const(path: Path, name: str) -> str:
    if not path.exists():
        pytest.skip(f"sibling repo not checked out: {path}")
    m = re.search(rf'^{name}\s*=\s*"([^"]+)"', path.read_text(), re.M)
    assert m, f"{name} not found in {path}"
    return m.group(1)


def test_idrac10_baseline_equals_ai_infra_reported_version():
    reported = _const(ROOT / "AI-Infra-Emulator" / "app" / "dell.py", "IDRAC_FW")
    assert ncp.IDRAC10_FW == reported
    assert ncp.FIRMWARE_BASELINE[0] == {"component": "BMC (Dell iDRAC10)", "baseline": reported}
    assert _const(ROOT / "NICo-Emulator" / "app" / "rest" / "inventory.py", "IDRAC10_FW") == reported


def test_idrac10_baseline_and_drift_served(client):
    b = client.get("/api/v1/firmware/baseline").json()
    assert {"component": "BMC (Dell iDRAC10)", "baseline": ncp.IDRAC10_FW} in b["components"]
    assert not any("OpenBMC" in c["component"] for c in b["components"])
    nico_fake.FAKE_NICO.get_host("nh-su-1-rack-00-tray-01").firmware_ok = False
    row = client.get("/api/v1/firmware/drift").json()["drift"][0]
    assert row["component"] == "BMC (Dell iDRAC10)" and row["baseline"] == ncp.IDRAC10_FW
