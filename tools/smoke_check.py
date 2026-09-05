"""Real-process smoke check; uses only the standard library and temporary data.

Run from any working directory: python tools/smoke_check.py
It launches a local server, checks real timed autosave (10-second setting),
normal shutdown, exclusive project ownership, and the explicit crash-loss window.
It never opens an existing user project.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_json(base: str, path: str, *, method: str = "GET", body=None, token: str = ""):
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Noveltool-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    with urlopen(Request(base + path, data=data, method=method, headers=headers), timeout=5) as response:
        return json.load(response)


@contextmanager
def running_server(path: Path, *, create: bool = False):
    port = free_port()
    args = [sys.executable, "-m", "noveltool", "--project", str(path), "--port", str(port)]
    if create:
        args += ["--create", "--title", "真实进程验收"]
    base = f"http://127.0.0.1:{port}"
    log_path = path.with_suffix(".server.log")
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 12
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(log_path.read_text(encoding="utf-8"))
                try:
                    assert http_json(base, "/health")["status"] == "ok"
                    break
                except (URLError, OSError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("本地服务未及时启动")
                    time.sleep(0.1)
            yield proc, base
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                    raise RuntimeError("服务未完成正常关闭")
    if proc.returncode == 0:
        assert "Application shutdown complete." in log_path.read_text(encoding="utf-8")
    elif proc.returncode != -signal.SIGKILL:
        raise RuntimeError(f"异常退出码 {proc.returncode}: {log_path.read_text(encoding='utf-8')}")


def main() -> int:
    results: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="noveltool-smoke-") as temporary:
        path = Path(temporary) / "小说.sqlite3"
        with running_server(path, create=True) as (_, base):
            results["real_http_health"] = http_json(base, "/health")
            token = http_json(base, "/api/session")["csrf_token"]
            config = http_json(base, "/api/config")["config"]
            config.update(min_chars=800, max_chars=1400, autosave_seconds=10)
            updated = http_json(base, "/api/config", method="PUT", token=token, body={
                "config": config, "expected_memory_version": 0,
            })
            assert updated["dirty"] is True
            with closing(sqlite3.connect(path, isolation_level=None)) as conn:
                assert conn.execute("SELECT min_chars FROM project_config").fetchone()[0] == 600
            deadline = time.monotonic() + 14
            while http_json(base, "/api/status")["dirty"]:
                if time.monotonic() > deadline:
                    raise AssertionError("真实定时自动保存没有生效")
                time.sleep(0.2)
            with closing(sqlite3.connect(path, isolation_level=None)) as conn:
                assert conn.execute("SELECT min_chars FROM project_config").fetchone()[0] == 800
            results["timed_autosave_10_seconds"] = True
            second = subprocess.run(
                [sys.executable, "-m", "noveltool", "--project", str(path), "--port", str(free_port())],
                cwd=ROOT, capture_output=True, text=True, timeout=10,
            )
            assert second.returncode != 0
            assert "项目已被另一个" in second.stdout + second.stderr
            results["second_process_refused"] = True
            status = http_json(base, "/api/status")
            http_json(base, "/api/project", method="PUT", token=token, body={
                "title": "正常退出已保存", "expected_memory_version": status["memory_version"],
            })
            assert http_json(base, "/api/status")["dirty"] is True
        with running_server(path) as (proc, base):
            status = http_json(base, "/api/status")
            assert status["title"] == "正常退出已保存"
            assert status["dirty"] is False
            results["sigint_flush_and_reopen"] = True
            token = http_json(base, "/api/session")["csrf_token"]
            http_json(base, "/api/project", method="PUT", token=token, body={
                "title": "强制终止前尚未写盘", "expected_memory_version": status["memory_version"],
            })
            assert http_json(base, "/api/status")["dirty"] is True
            proc.kill()  # Deliberately demonstrate that unflushed RAM is not durable.
            proc.wait(timeout=5)
        with running_server(path) as (_, base):
            status = http_json(base, "/api/status")
            assert status["title"] == "正常退出已保存"
            assert status["dirty"] is False
            results["crash_reopens_last_committed_state"] = True
            results["unflushed_change_lost_as_documented"] = True
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
