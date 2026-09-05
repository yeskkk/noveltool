from contextlib import closing
import sqlite3

from noveltool import __version__, MILESTONE
from noveltool.db import ProjectStore


def test_home_and_static_assets_are_local(client):
    response = client.get("/")
    assert response.status_code == 200
    assert __version__ in response.text
    assert "设定编辑与时间线已接通" in response.text
    assert "https://" not in response.text
    for path in ["/static/app.js", "/static/app.css"]:
        assert client.get(path).status_code == 200
    assert client.get("/docs").status_code == 404


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "version": __version__, "milestone": MILESTONE}


def test_status(client):
    status = client.get("/api/status").json()
    assert status["dirty"] is False
    assert status["memory_version"] == status["saved_version"] == 0
    assert status["title"] == "测试小说"


def test_request_requires_local_token(client):
    assert client.post("/api/save").status_code == 403
    assert client.post("/api/save", headers={"X-Noveltool-Token": "wrong"}).status_code == 403


def test_host_and_origin_guards(client, write_headers):
    assert client.get("/api/config", headers={"Host": "evil.example"}).status_code == 400
    assert client.post("/api/save", headers={**write_headers, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/save", headers={**write_headers, "Origin": "http://127.0.0.1"}).status_code == 200
    assert client.get("/health", headers={"Host": "[::1]:8765"}).status_code == 200


def test_security_headers(client):
    response = client.get("/")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]


def test_config_update_then_save(client, write_headers, project_path):
    data = client.get("/api/config").json()
    data["config"]["min_chars"] = 800
    data["config"]["max_chars"] = 1400
    response = client.put("/api/config", json={
        "config": data["config"], "expected_memory_version": data["memory_version"]
    }, headers=write_headers)
    assert response.status_code == 200
    assert response.json()["dirty"]
    with closing(sqlite3.connect(project_path, isolation_level=None)) as disk:
        assert disk.execute("SELECT min_chars FROM project_config").fetchone()[0] == 600
    saved = client.post("/api/save", headers=write_headers)
    assert saved.status_code == 200
    assert saved.json()["written"] is True
    assert saved.json()["dirty"] is False
    with closing(sqlite3.connect(project_path, isolation_level=None)) as disk:
        assert disk.execute("SELECT min_chars FROM project_config").fetchone()[0] == 800


def test_invalid_config_is_not_applied(client, write_headers):
    data = client.get("/api/config").json()
    config = {**data["config"], "min_chars": 2000, "max_chars": 1000}
    response = client.put("/api/config", json={"config": config, "expected_memory_version": 0}, headers=write_headers)
    assert response.status_code == 422
    assert client.get("/api/config").json() == data
    assert client.get("/api/status").json()["dirty"] is False


def test_conflict_does_not_overwrite_title(client, write_headers):
    response = client.put("/api/project", json={"title": "第一个页面", "expected_memory_version": 0}, headers=write_headers)
    assert response.status_code == 200
    response = client.put("/api/project", json={"title": "旧页面", "expected_memory_version": 0}, headers=write_headers)
    assert response.status_code == 409
    assert client.get("/api/status").json()["title"] == "第一个页面"


def test_config_cannot_accept_a_literal_api_key(client, write_headers, monkeypatch, project_path):
    secret = "not-a-real-key-and-must-not-be-stored"
    monkeypatch.setenv("NOVELTOOL_API_KEY", secret)
    data = client.get("/api/config").json()
    assert secret not in str(data)
    response = client.put("/api/config", json={
        "config": {**data["config"], "api_key": secret}, "expected_memory_version": 0
    }, headers=write_headers)
    assert response.status_code == 422
    assert secret.encode() not in project_path.read_bytes()


def test_noop_save(client, write_headers):
    result = client.post("/api/save", headers=write_headers).json()
    assert result["written"] is False
    assert result["memory_version"] == 0


def test_validation_does_not_leak_into_db(client, write_headers):
    response = client.put("/api/project", json={"title": "   ", "expected_memory_version": 0}, headers=write_headers)
    assert response.status_code == 422
    assert client.get("/api/status").json()["memory_version"] == 0


def test_app_shutdown_reopens_with_saved_title(project_path):
    from fastapi.testclient import TestClient
    from noveltool.app import create_app
    with TestClient(create_app(project_path), base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["csrf_token"]
        client.put("/api/project", json={"title": "关闭后重开", "expected_memory_version": 0}, headers={"X-Noveltool-Token": token})
        assert client.get("/api/status").json()["dirty"]
    with ProjectStore.open(project_path) as store:
        assert store.load().meta.title == "关闭后重开"


def test_save_failure_returns_503_without_losing_memory(client, write_headers, monkeypatch):
    from noveltool.db import SaveFailedError
    original = ProjectStore.flush
    client.put("/api/project", json={"title": "保存失败仍在内存", "expected_memory_version": 0}, headers=write_headers)
    def fail(*args, **kwargs):
        raise SaveFailedError("模拟磁盘故障")
    try:
        monkeypatch.setattr(ProjectStore, "flush", fail)
        result = client.post("/api/save", headers=write_headers)
        assert result.status_code == 503
        status = client.get("/api/status").json()
        assert status["dirty"] is True
        assert status["saved_version"] == 0
        assert status["title"] == "保存失败仍在内存"
        assert status["last_save_error"] is not None
    finally:
        monkeypatch.setattr(ProjectStore, "flush", original)
    assert client.post("/api/save", headers=write_headers).json()["written"] is True
