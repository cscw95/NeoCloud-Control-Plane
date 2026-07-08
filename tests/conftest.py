import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed_default
from app.store import STORE


@pytest.fixture()
def client():
    # deterministic fresh state per test: SU-1 = GB200, SU-2 = Vera Rubin
    seed_default(STORE, blueprints=["gb200-nvl72", "vr-nvl72"])
    with TestClient(app) as c:
        yield c
