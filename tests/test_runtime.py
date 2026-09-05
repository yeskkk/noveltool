from contextlib import closing
import asyncio
import sqlite3

import pytest

from noveltool.db import ProjectStore, SaveFailedError
from noveltool.domain import ProjectConfig
from noveltool.runtime import EditConflictError, ProjectSession


def test_edits_are_in_memory_until_save(project_path):
    async def scenario():
        session = ProjectSession(ProjectStore.open(project_path))
        try:
            await session.update_config(ProjectConfig(min_chars=800, max_chars=1400), 0)
            status = await session.status()
            assert status["dirty"] is True
            assert status["memory_version"] == 1
            assert status["saved_version"] == 0
            with closing(sqlite3.connect(project_path, isolation_level=None)) as disk:
                assert disk.execute("SELECT min_chars FROM project_config").fetchone()[0] == 600
            assert await session.save() is True
            assert await session.save() is False
            status = await session.status()
            assert status["memory_version"] == status["saved_version"] == 1
            assert not status["dirty"]
        finally:
            await session.close()
    asyncio.run(scenario())


def test_unchanged_values_do_not_dirty_or_increment(project_path):
    async def scenario():
        session = ProjectSession(ProjectStore.open(project_path))
        try:
            assert not await session.update_config(ProjectConfig(), 0)
            assert not await session.update_title(" 测试小说 ", 0)
            assert (await session.status())["memory_version"] == 0
            assert (await session.status())["dirty"] is False
        finally:
            await session.close()
    asyncio.run(scenario())


def test_stale_browser_does_not_overwrite_memory(project_path):
    async def scenario():
        session = ProjectSession(ProjectStore.open(project_path))
        try:
            await session.update_title("新标题", 0)
            with pytest.raises(EditConflictError):
                await session.update_config(ProjectConfig(min_chars=100), 0)
            assert session.project.data.config.min_chars == 600
            assert session.project.data.meta.title == "新标题"
        finally:
            await session.close()
    asyncio.run(scenario())


def test_autosave_due_time_and_dynamic_interval(project_path):
    async def scenario():
        session = ProjectSession(ProjectStore.open(project_path))
        try:
            base = session._last_attempt
            await session.update_title("自动保存", 0)
            assert not await session.autosave_tick(base + 59.99)
            assert await session.autosave_tick(base + 60)
            assert not (await session.status())["dirty"]
            await session.update_config(ProjectConfig(autosave_seconds=10), 1)
            assert not await session.autosave_tick(base + 69.99)
            assert await session.autosave_tick(base + 70)
        finally:
            await session.close()
    asyncio.run(scenario())


def test_clean_autosave_does_not_write(project_path):
    async def scenario():
        session = ProjectSession(ProjectStore.open(project_path))
        try:
            trace = []
            session.store.connection.set_trace_callback(trace.append)
            assert not await session.autosave_tick(session._last_attempt + 61)
            assert trace == []
        finally:
            await session.close()
    asyncio.run(scenario())


def test_failed_save_preserves_dirty_data_and_can_retry(project_path, monkeypatch):
    async def scenario():
        session = ProjectSession(ProjectStore.open(project_path))
        original = session.store.flush
        try:
            await session.update_title("不能丢失", 0)
            def fail(*args, **kwargs):
                raise SaveFailedError("模拟磁盘写入失败")
            monkeypatch.setattr(session.store, "flush", fail)
            with pytest.raises(SaveFailedError):
                await session.save()
            status = await session.status()
            assert status["dirty"] is True
            assert status["saved_version"] == 0
            assert status["title"] == "不能丢失"
            assert status["last_save_error"] is not None
            monkeypatch.setattr(session.store, "flush", original)
            assert await session.save()
            assert (await session.status())["last_save_error"] is None
        finally:
            monkeypatch.setattr(session.store, "flush", original)
            await session.close()
    asyncio.run(scenario())


def test_failed_autosave_keeps_timer_retryable(project_path, monkeypatch):
    async def scenario():
        session = ProjectSession(ProjectStore.open(project_path))
        original = session.store.flush
        try:
            await session.update_title("稍后重试", 0)
            def fail(*args, **kwargs):
                raise SaveFailedError("injected")
            monkeypatch.setattr(session.store, "flush", fail)
            base = session._last_attempt
            assert not await session.autosave_tick(base + 60)
            assert (await session.status())["dirty"]
            monkeypatch.setattr(session.store, "flush", original)
            assert not await session.autosave_tick(base + 61)
            assert await session.autosave_tick(base + 120)
        finally:
            monkeypatch.setattr(session.store, "flush", original)
            await session.close()
    asyncio.run(scenario())


def test_close_flushes_and_stops_background_task(project_path):
    async def scenario():
        session = ProjectSession(ProjectStore.open(project_path))
        session.start_autosave()
        with pytest.raises(RuntimeError):
            session.start_autosave()
        await session.update_title("退出时保存", 0)
        await session.close()
        assert session._autosave_task.done()
        assert session.store._closed
        await session.close()  # Idempotent.
    asyncio.run(scenario())
    with ProjectStore.open(project_path) as store:
        assert store.load().meta.title == "退出时保存"


def test_read_operations_do_not_query_sqlite(project_path):
    async def scenario():
        session = ProjectSession(ProjectStore.open(project_path))
        try:
            trace = []
            session.store.connection.set_trace_callback(trace.append)
            for _ in range(5):
                await session.status()
                await session.config_view()
            assert trace == []
        finally:
            await session.close()
    asyncio.run(scenario())
