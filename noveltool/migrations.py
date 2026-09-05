"""Bundled SQL only; backups use SQLite backup(), including committed WAL data."""
from __future__ import annotations
from importlib.resources import files
from pathlib import Path
import os
import sqlite3
from uuid import uuid4

from .domain import SCHEMA_VERSION


def scripts_after(version: int) -> list[str]:
    root = files("noveltool").joinpath("sql")
    return [p.read_text(encoding="utf-8") for p in sorted(root.iterdir(), key=lambda p: p.name)
            if p.name.endswith(".sql") and version < int(p.name[:3]) <= SCHEMA_VERSION]


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    # execute(), unlike executescript(), never implicitly commits this transaction.
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            conn.execute(buffer)
            buffer = ""
    if buffer.strip() and any(not line.strip().startswith("--") for line in buffer.splitlines()):
        raise ValueError("不完整的内置 SQL migration")


def migrate(conn: sqlite3.Connection, path: Path, old_version: int) -> Path:
    backup = path.with_name(path.name + f".before-schema-{SCHEMA_VERSION}-{uuid4().hex[:8]}.bak")
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(backup) as destination:
            conn.backup(destination)
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    finally:
        if 'destination' in locals():
            destination.close()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for script in scripts_after(old_version):
            _execute_script(conn, script)
        conn.execute("UPDATE project_meta SET schema_version=?", (SCHEMA_VERSION,))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("migration 外键检查失败")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return backup
