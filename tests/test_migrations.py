from contextlib import closing
from importlib.resources import files
import sqlite3

import pytest

from noveltool.db import ProjectStore
from noveltool.domain import APPLICATION_ID, ProjectConfig, ProjectMeta, utc_now, SCHEMA_VERSION
from noveltool.migrations import migrate


def create_v1(path):
    with closing(sqlite3.connect(path, isolation_level=None)) as c:
        c.executescript(files('noveltool').joinpath('sql/001_initial.sql').read_text())
        c.execute(f'PRAGMA application_id={APPLICATION_ID}')
        c.execute('PRAGMA user_version=1')
        now = utc_now()
        meta = ProjectMeta(id='a'*32, title='旧项目', schema_version=1, created_at=now, updated_at=now, saved_at=now)
        d = meta.model_dump()
        c.execute(f"INSERT INTO project_meta(singleton,{','.join(d)}) VALUES(1,{','.join('?' for _ in d)})", list(d.values()))
        cfg = ProjectConfig(min_chars=123, max_chars=456).model_dump()
        c.execute(f"INSERT INTO project_config(project_id,{','.join(cfg)}) VALUES (?,{','.join('?' for _ in cfg)})", [meta.id, *cfg.values()])


def test_v1_migrates_and_keeps_real_backup(tmp_path):
    path = tmp_path / 'v1.sqlite3'
    create_v1(path)
    with ProjectStore.open(path) as s:
        backup = s.migration_backup
        assert backup and backup.is_file()
        assert s.load().config.min_chars == 123
        assert s.load().meta.schema_version == SCHEMA_VERSION
    with closing(sqlite3.connect(backup)) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == 1
        assert c.execute('SELECT title FROM project_meta').fetchone()[0] == '旧项目'
    with ProjectStore.open(path) as s:
        assert s.migration_backup is None


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / 'v1.sqlite3'
    create_v1(path)
    monkeypatch.setattr('noveltool.migrations.scripts_after', lambda _: ['CREATE TABLE partial(x);\nINVALID SQL;\n'])
    with closing(sqlite3.connect(path, isolation_level=None)) as c:
        with pytest.raises(sqlite3.Error):
            migrate(c, path, 1)
        assert c.execute('PRAGMA user_version').fetchone()[0] == 1
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone()
