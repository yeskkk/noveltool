"""In-memory state, version guards, dirty tracking, and batched persistence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
import sqlite3
from collections.abc import Callable

from .db import ProjectStore
from .domain import ProjectConfig, ProjectData, ProjectMeta, utc_now
from .revision import RevisionEngine, RevisionPlan, load_manuscript, next_undo, persist_plan, revision_history
from .manuscript import count_chars, text_hash, ManuscriptError, validate_range
from .llm import LLMRun, persist_runs
from .structured_llm import ValidationRecord
from .import_service import ImportService
from .analysis import AnalysisService
from .analysis_jobs import AnalysisJobs
from .knowledge import KnowledgeService
from .settings import SettingsService
from .ideas import IdeaService
from .state import StateReducer
from .context import ContextBuilder
from .generation import GenerationService
from .semantic import SemanticSync
from .consistency import ConsistencyService
from .maintenance import MaintenanceService, startup_recovery_counts

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

    Network waits never hold this lock. SQLite writes are synchronous and bounded.
    If larger writes later move to a worker, that worker must OWN its connection.
    """

    def __init__(self, store: ProjectStore):
        self.store = store
        self.project = RuntimeProject.from_data(store.load())
        self.manuscript = load_manuscript(store.connection)
        self.lock = asyncio.Lock()
        self.model_gate = asyncio.Lock()
        self._pending_llm_runs: list[LLMRun] = []
        self._pending_validations: dict[str, ValidationRecord] = {}
        self._stop = asyncio.Event()
        self._autosave_task: asyncio.Task[None] | None = None
        self._last_attempt = time.monotonic()
        self._closed = False
        self.startup_recovery = startup_recovery_counts(store.connection)
        self.imports = ImportService(self)
        self.analysis = AnalysisService(self)
        self.jobs = AnalysisJobs(self)
        self.settings = SettingsService(self)
        self.knowledge = KnowledgeService(self)
        self.ideas = IdeaService(self)
        self.state_reducer = StateReducer(self)
        self.context = ContextBuilder(self)
        self.generation = GenerationService(self)
        self.semantic = SemanticSync(self)
        self.consistency = ConsistencyService(self)
        self.maintenance = MaintenanceService(self)

    async def update_config(self, config: ProjectConfig, expected: int) -> bool:
        async with self.lock:
            return self.project.update_config(config, expected)

    async def update_title(self, title: str, expected: int) -> bool:
        async with self.lock:
            return self.project.update_title(title, expected)

    async def record_llm_run(self, run: LLMRun) -> None:
        from dataclasses import replace
        async with self.lock:
            # Turning logging off also applies to an already-running request.
            if not self.project.data.config.retain_llm_logs:
                run = replace(run, request_json="", raw_response="", retained=0)
            self._pending_llm_runs.append(run)
            self.project.data = ProjectData(self.project._changed_meta(), self.project.data.config)
            self.project.dirty.update({"meta", "llm_runs"})

    async def record_validation(self, record: ValidationRecord) -> None:
        from dataclasses import replace
        async with self.lock:
            pending = next((r for r in self._pending_llm_runs if r.id == record.run_id), None)
            row = None if pending else self.store.connection.execute(
                "SELECT retained FROM llm_runs WHERE id=?", (record.run_id,)).fetchone()
            retained = pending.retained if pending else bool(row and row["retained"])
            if not retained or not self.project.data.config.retain_llm_logs:
                record = replace(record, parsed_json=None)
            self._pending_validations[record.run_id] = record
            self.project.data = ProjectData(self.project._changed_meta(), self.project.data.config)
            self.project.dirty.update({"meta", "llm_runs"})

    async def llm_run_views(self) -> list[dict]:
        from dataclasses import asdict
        async with self.lock:
            rows = [dict(row) for row in self.store.connection.execute(
                "SELECT * FROM llm_runs ORDER BY finished_at DESC LIMIT 20")]
            rows = [asdict(run) for run in reversed(self._pending_llm_runs)] + rows
            for row in rows:
                record = self._pending_validations.get(row["id"])
                if record:
                    row.update(validation_status=record.status, validation_error=record.error)
            return [{k: row.get(k) for k in ("id","purpose","model","status","error_code",
                     "error_message","finish_reason","retained","elapsed_ms","finished_at",
                     "validation_status","validation_error")}
                    for row in rows[:20]]

    def _persist_extras(self, conn, plan: RevisionPlan | None = None) -> None:
        persist_runs(conn, self._pending_llm_runs)
        for record in self._pending_validations.values():
            cur = conn.execute("UPDATE llm_runs SET validation_status=?,parsed_json=?,validation_error=? WHERE id=?",
                (record.status, record.parsed_json, record.error, record.run_id))
            if cur.rowcount != 1:
                from .db import SaveFailedError
                raise SaveFailedError("结构化校验记录没有对应的模型调用")
        self.generation.drafts.persist_pending(conn)
        if plan is not None:
            persist_plan(conn, plan)

    async def manuscript_view(self) -> dict[str, object]:
        async with self.lock:
            rendered = self.manuscript.render()
            return {"revision_no": self.manuscript.revision_no, "text": rendered.text,
                    "text_hash": text_hash(rendered.text), "char_count": count_chars(rendered.text),
                    "block_count": len(self.manuscript.blocks),
                    "line_count": rendered.text.count("\n") + 1}

    def _commit_plan_locked(self, plan: RevisionPlan | None) -> bool:
        if plan is None:
            return False
        return self._commit_snapshot_locked(plan=plan)

    def _commit_snapshot_locked(self, *, plan: RevisionPlan | None = None,
                                extra: Callable[[sqlite3.Connection], None] | None = None) -> bool:
        """Caller owns lock. Persist pending edits/logs + optional text + auxiliary rows atomically."""
        changed = ProjectData(self.project._changed_meta(), self.project.data.config)
        def apply(conn):
            self._persist_extras(conn, plan)
            if extra is not None:
                extra(conn)
        try:
            saved = self.store.flush(changed, expected_version=self.project.saved_version,
                                     dirty=set(self.project.dirty) | {"meta"}, apply=apply)
        except Exception as exc:
            self.project.last_save_error = str(exc)
            raise
        # Never publish a changed manuscript before COMMIT succeeds.
        if plan is not None:
            self.manuscript = plan.manuscript
        self.project.data = saved
        self.project.saved_version = saved.meta.data_version
        self.project.dirty.clear()
        self._pending_llm_runs.clear()
        self._pending_validations.clear()
        self.generation.drafts.mark_persisted()
        self.project.last_save_error = None
        self._last_attempt = time.monotonic()
        return True

    async def import_manuscript(self, text: str, expected: int, mode: str = "auto") -> bool:
        async with self.lock:
            return self._commit_plan_locked(RevisionEngine.import_text(self.manuscript, text, expected, mode))

    async def append_manuscript(self, text: str, expected: int) -> bool:
        async with self.lock:
            return self._commit_plan_locked(RevisionEngine.append(self.manuscript, text, expected))

    async def replace_manuscript(self, start: int, end: int, text: str, expected: int,
                                 instruction: str = "", selected_text: str | None = None) -> bool:
        async with self.lock:
            self.manuscript.check_revision(expected)
            validate_range(self.manuscript.text, start, end)
            if selected_text is not None and self.manuscript.text[start:end] != selected_text:
                raise ManuscriptError("选区原文与当前正文不匹配；请重新选择，修改未提交")
            return self._commit_plan_locked(RevisionEngine.replace(self.manuscript, start, end, text, expected, instruction))

    async def undo_manuscript(self, expected: int) -> bool:
        async with self.lock:
            self.manuscript.check_revision(expected)
            target, blocks = next_undo(self.store.connection)
            return self._commit_plan_locked(RevisionEngine.undo(self.manuscript, target, blocks, expected))

    async def history(self) -> list[dict]:
        async with self.lock:
            return revision_history(self.store.connection)

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
                "manuscript_revision_no": self.manuscript.revision_no,
                "migration_backup": str(self.store.migration_backup) if self.store.migration_backup else None,
            }

    def _flush_locked(self) -> bool:
        if not self.project.dirty:
            return False
        try:
            data = self.store.flush(
                self.project.data,
                expected_version=self.project.saved_version,
                dirty=set(self.project.dirty),
                apply=self._persist_extras,
            )
        except Exception as exc:
            self.project.last_save_error = str(exc)
            raise
        self.project.data = data
        self.project.saved_version = data.meta.data_version
        self.project.dirty.clear()
        self._pending_llm_runs.clear()
        self._pending_validations.clear()
        self.generation.drafts.mark_persisted()
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
            if current < self._last_attempt + self.project.data.config.autosave_seconds:
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
            await self.consistency.close()
            await self.generation.close()
            await self.jobs.close()
            if self._autosave_task is not None:
                await self._autosave_task
            await self.save()
        except Exception:
            logger.exception("退出保存失败：不要将本次退出视为已成功保存")
            raise
        finally:
            self.store.close()
            self._closed = True
