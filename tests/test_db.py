from contextlib import closing
from dataclasses import replace
import sqlite3
import stat

import pytest

from noveltool.db import (
    InvalidProjectError, ProjectExistsError, ProjectLockedError, ProjectNotFoundError,
    ProjectStore, SaveConflictError, SaveFailedError, UnsupportedSchemaError,
)
from noveltool.domain import SCHEMA_VERSION, APPLICATION_ID, ProjectConfig, ProjectData, ProjectMeta


def changed_data(data, version=1):
    return ProjectData(
        ProjectMeta.model_validate({**data.meta.model_dump(), "title": "改名", "data_version": version}),
        ProjectConfig(min_chars=800, max_chars=1400, writer_model="本地模型", retain_llm_logs=False),
    )


def test_create_and_round_trip(tmp_path):
    path = tmp_path / "子目录" / "小说.sqlite3"
    config = ProjectConfig(writer_model="小说模型", analysis_model="分析模型", min_chars=800, max_chars=1400)
    with ProjectStore.create(path, "中文标题 😀", config) as store:
        first = store.load()
        assert first.meta.title == "中文标题 😀"
        assert store.connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with ProjectStore.open(path) as store:
        assert store.load() == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_file_is_never_overwritten(project_path):
    original = project_path.read_bytes()
    with pytest.raises(ProjectExistsError):
        ProjectStore.create(project_path, "不应覆盖")
    assert project_path.read_bytes() == original
    with ProjectStore.open(project_path) as store:
        assert store.load().meta.title == "测试小说"


def test_open_missing_does_not_create(tmp_path):
    path = tmp_path / "missing" / "project.sqlite3"
    with pytest.raises(ProjectNotFoundError):
        ProjectStore.open(path)
    assert not path.exists()
    assert not path.parent.exists()


def test_one_process_owns_the_project(project_path):
    with ProjectStore.open(project_path):
        with pytest.raises(ProjectLockedError):
            ProjectStore.open(project_path)
    # The lock file remains but the OS lock has been released.
    assert project_path.with_name(project_path.name + ".lock").exists()
    with ProjectStore.open(project_path):
        pass


def test_external_database_is_rejected_without_rewriting(tmp_path):
    path = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("CREATE TABLE other(value TEXT)")
        conn.execute("INSERT INTO other VALUES ('保留这个内容')")
    original = path.read_bytes()
    with pytest.raises(InvalidProjectError):
        ProjectStore.open(path)
    assert path.read_bytes() == original


def test_non_sqlite_file_is_rejected(tmp_path):
    path = tmp_path / "text.sqlite3"
    path.write_text("不是数据库", encoding="utf-8")
    with pytest.raises(InvalidProjectError):
        ProjectStore.open(path)
    assert path.read_text(encoding="utf-8") == "不是数据库"


def test_future_schema_rejected(project_path):
    with closing(sqlite3.connect(project_path, isolation_level=None)) as conn:
        conn.execute("PRAGMA user_version = 999")
    with pytest.raises(UnsupportedSchemaError):
        ProjectStore.open(project_path)
    with closing(sqlite3.connect(project_path, isolation_level=None)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 999


def test_only_implemented_tables(project_path):
    with ProjectStore.open(project_path) as store:
        names = {row[0] for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names == {"project_meta", "project_config", "revisions", "manuscript_blocks", "llm_runs", "source_imports", "chunk_plans", "analysis_runs", "observations", "analysis_jobs", "setting_entities", "setting_entries", "setting_changes", "idea_proposals", "generation_tasks", "generation_attempts"}


def test_flush_and_noop(project_path):
    with ProjectStore.open(project_path) as store:
        data = changed_data(store.load())
        saved = store.flush(data, expected_version=0, dirty={"meta", "config"})
        assert saved.meta.data_version == 1
        assert saved.config.retain_llm_logs is False
        trace = []
        store.connection.set_trace_callback(trace.append)
        assert store.flush(saved, expected_version=1, dirty=set()) is saved
        assert trace == []
    with ProjectStore.open(project_path) as store:
        assert store.load() == saved


def test_batch_is_atomic_on_sql_failure(project_path):
    with ProjectStore.open(project_path) as store:
        before = store.load()
        store.connection.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON project_config "
                                 "BEGIN SELECT RAISE(FAIL, 'injected write failure'); END")
        with pytest.raises(SaveFailedError):
            store.flush(changed_data(before), expected_version=0, dirty={"meta", "config"})
        assert not store.connection.in_transaction
        assert store.load() == before  # The preceding metadata UPDATE was rolled back too.
        store.connection.execute("DROP TRIGGER fail_update")
        store.flush(changed_data(before), expected_version=0, dirty={"meta", "config"})
        assert store.load().meta.title == "改名"


def test_external_version_conflict_is_not_overwritten(project_path):
    with ProjectStore.open(project_path) as store:
        before = store.load()
        with closing(sqlite3.connect(project_path, isolation_level=None)) as outsider:
            outsider.execute("UPDATE project_meta SET title='外部内容', data_version=1")
        with pytest.raises(SaveConflictError):
            store.flush(changed_data(before), expected_version=0, dirty={"meta", "config"})
        assert store.load().meta.title == "外部内容"
        assert store.load().config == before.config


def test_creation_failure_cleans_up_and_unlocks(tmp_path, monkeypatch):
    path = tmp_path / "new.sqlite3"
    original = ProjectStore._configure_owned_database
    def fail(conn):
        raise OSError("模拟初始化失败")
    monkeypatch.setattr(ProjectStore, "_configure_owned_database", staticmethod(fail))
    with pytest.raises(OSError):
        ProjectStore.create(path, "test")
    assert not path.exists()
    monkeypatch.setattr(ProjectStore, "_configure_owned_database", staticmethod(original))
    with ProjectStore.create(path, "test"):
        pass


def test_sql_constraints_and_loader_validation(project_path):
    with ProjectStore.open(project_path) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute("UPDATE project_config SET max_chars=1")
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute("UPDATE project_config SET project_id='missing'")
        # SQL cannot express every domain rule: strict load validation catches this.
        store.connection.execute("UPDATE project_config SET api_base_url='not-a-url'")
        with pytest.raises(InvalidProjectError):
            store.load()


def test_wrong_project_and_unknown_dirty_category_rejected(project_path):
    with ProjectStore.open(project_path) as store:
        data = changed_data(store.load())
        with pytest.raises(SaveFailedError):
            store.flush(data, expected_version=0, dirty={"future_module"})
        wrong_meta = ProjectMeta.model_validate({**data.meta.model_dump(), "id": "f" * 32})
        with pytest.raises(SaveFailedError):
            store.flush(replace(data, meta=wrong_meta), expected_version=0, dirty={"meta"})


def test_invalid_load_releases_ownership_lock(project_path):
    with closing(sqlite3.connect(project_path, isolation_level=None)) as conn:
        conn.execute("UPDATE project_config SET api_base_url='invalid'")
    with pytest.raises(InvalidProjectError):
        ProjectStore.open(project_path)
    with closing(sqlite3.connect(project_path, isolation_level=None)) as conn:
        conn.execute("UPDATE project_config SET api_base_url='http://localhost:8000/v1'")
    with ProjectStore.open(project_path):
        pass


def test_sql_creation_error_is_actionable_and_releases_lock(tmp_path, monkeypatch):
    from noveltool.db import ProjectError
    path = tmp_path / "failure.sqlite3"
    original = ProjectStore._configure_owned_database
    def fail(conn):
        raise sqlite3.OperationalError("simulated disk full")
    monkeypatch.setattr(ProjectStore, "_configure_owned_database", staticmethod(fail))
    with pytest.raises(ProjectError, match="项目初始化失败"):
        ProjectStore.create(path, "test")
    assert not path.exists()
    monkeypatch.setattr(ProjectStore, "_configure_owned_database", staticmethod(original))
    with ProjectStore.create(path, "test"):
        pass


def test_metadata_schema_mismatch_is_rejected(project_path):
    with closing(sqlite3.connect(project_path, isolation_level=None)) as conn:
        conn.execute("UPDATE project_meta SET schema_version=999")
    with pytest.raises(UnsupportedSchemaError):
        ProjectStore.open(project_path)
