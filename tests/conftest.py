import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import create_app  # noqa: E402
from app.service import WeightingService  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def service(tmp_path):
    return WeightingService(db_path=str(tmp_path / "test.db"))


@pytest.fixture()
def client(service):
    app = create_app(service=service)
    return TestClient(app)
