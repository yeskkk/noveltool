"""In-memory state, version guards, dirty tracking, and batched persistence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time

from .db import ProjectStore
from .domain import ProjectConfig, ProjectData, ProjectMeta, utc_now

logger = logging.getLogger(__name__)


class EditConflictError(RuntimeError):
    pass


@dataclass(slots=True)
class RuntimeProject:
    data: ProjectData
    saved_version: int
    dirty: set[str] = field(default_factory=set)
    last_save_error: str | None = None

    @classmethod
    def from_data(cls, data: ProjectData) -> RuntimeProject:
        return cls(data=data, saved_version=data.meta.data_version)

    def _check_version(self, expected: int) -> None:
        if expected != self.data.meta.data_version:
            raise EditConflictError("页面基于旧版本；请先重新载入，避免覆盖另一个页面的修改")

    def _changed_meta(self, **changes: object) -> ProjectMeta:
        return ProjectMeta.model_validate({
            **self.data.meta.model_dump(), **changes,
            "data_version": self.data.meta.data_version + 1,
            "updated_at": utc_now(),
        })

    def update_config(self, config: ProjectConfig, expected: int) -> bool:
        self._check_version(expected)
        if config == self.data.config:
            return False
        self.data = ProjectData(self._changed_meta(), config)
        self.dirty.update({"meta", "config"})
        return True

    def update_title(self, title: str, expected: int) -> bool:
        self._check_version(expected)
        validated = self._changed_meta(title=title)
        if validated.title == self.data.meta.title:
            return False
        self.data = ProjectData(validated, self.data.config)
        self.dirty.add("meta")
        return True


class ProjectSession:
    """All access occurs on one event loop; no lock is held during model calls.

    There are no model calls yet. Store writes are currently tiny and synchronous.
    If larger writes later move to a worker, that worker must OWN its connection.
    """

    def __init__(self, store: ProjectStore):
        self.store = store
        self.project = RuntimeProject.from_data(store.load())
        self.lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._autosave_task: asyncio.Task[None] | None = None
        self._last_attempt = time.monotonic()
        self._closed = False

    async def update_config(self, config: ProjectConfig, expected: int) -> bool:
        async with self.lock:
            return self.project.update_config(config, expected)

    async def update_title(self, title: str, expected: int) -> bool:
        async with self.lock:
            return self.project.update_title(title, expected)

    async def config_view(self) -> dict[str, object]:
        async with self.lock:
            return {
                "config": self.project.data.config.model_dump(),
                "title": self.project.data.meta.title,
                "memory_version": self.project.data.meta.data_version,
            }

    async def status(self) -> dict[str, object]:
        async with self.lock:
            meta = self.project.data.meta
            return {
                "project_id": meta.id, "title": meta.title,
                "database_path": str(self.store.path), "schema_version": meta.schema_version,
                "memory_version": meta.data_version, "saved_version": self.project.saved_version,
                "dirty": bool(self.project.dirty), "dirty_sections": sorted(self.project.dirty),
                "last_saved_at": meta.saved_at, "last_save_error": self.project.last_save_error,
                "autosave_seconds": self.project.data.config.autosave_seconds,
            }

    def _flush_locked(self) -> bool:
        if not self.project.dirty:
            return False
        try:
            data = self.store.flush(
                self.project.data,
                expected_version=self.project.saved_version,
                dirty=set(self.project.dirty),
            )
        except Exception as exc:
            self.project.last_save_error = str(exc)
            raise
        self.project.data = data
        self.project.saved_version = data.meta.data_version
        self.project.dirty.clear()
        self.project.last_save_error = None
        return True

    async def save(self) -> bool:
        async with self.lock:
            try:
                return self._flush_locked()
            finally:
                self._last_attempt = time.monotonic()

    async def autosave_tick(self, now: float | None = None) -> bool:
        """One timer check. Tests inject a monotonic time, not a 60-second sleep."""
        async with self.lock:
            current = time.monotonic() if now is None else now
            if current - self._last_attempt < self.project.data.config.autosave_seconds:
                return False
            self._last_attempt = current
            try:
                return self._flush_locked()
            except Exception:
                # Preserve dirty data and keep the timer alive for a later retry.
                logger.exception("自动保存失败；修改仍在内存，请检查磁盘状态并重试")
                return False

    async def _autosave_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                await self.autosave_tick()

    def start_autosave(self) -> None:
        if self._closed:
            raise RuntimeError("项目已关闭")
        if self._autosave_task is not None:
            raise RuntimeError("自动保存已启动")
        self._autosave_task = asyncio.create_task(self._autosave_loop(), name="noveltool-autosave")

    async def close(self) -> None:
        if self._closed:
            return
        self._stop.set()
        try:
            if self._autosave_task is not None:
                await self._autosave_task
            await self.save()
        except Exception:
            logger.exception("退出保存失败：不要将本次退出视为已成功保存")
            raise
        finally:
            self.store.close()
            self._closed = True
