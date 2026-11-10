import os

import pytest
from fastapi.testclient import TestClient

# DSX routers: never reach real emulators from the unit suite; existing tests
# run as the operator plane without a token (PLAN C security, dev switches).
os.environ.setdefault("NOCP_DSX_OFFLINE", "1")
os.environ.setdefault("NOCP_DEV_OPERATOR_DEFAULT", "1")
os.environ.setdefault("NOCP_DEV_INSECURE_HEADERS", "1")   # X-Tenant-Id in tests

from app.main import app
from app.seed import seed_default
from app.store import STORE


@pytest.fixture()
def client():
    # deterministic fresh state per test: SU-1 = GB200, SU-2 = Vera Rubin
    seed_default(STORE, blueprints=["gb200-nvl72", "vr-nvl72"])
    with TestClient(app) as c:
        yield c
