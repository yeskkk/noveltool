from fastapi.testclient import TestClient
import pytest

from noveltool.app import create_app
from noveltool.db import ProjectStore


@pytest.fixture
def project_path(tmp_path):
    path = tmp_path / "项目.sqlite3"
    with ProjectStore.create(path, "测试小说"):
        pass
    return path


@pytest.fixture
def client(project_path):
    with TestClient(create_app(project_path), base_url="http://127.0.0.1") as result:
        yield result


@pytest.fixture
def write_headers(client):
    token = client.get("/api/session").json()["csrf_token"]
    return {"X-Noveltool-Token": token}
