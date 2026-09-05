"""Single-process, explicitly started, checkpointed analysis jobs.

Pause is cooperative: finish the current call and save its result, then stop.
Shutdown cancels the worker and waits before closing SQLite. No automatic network
retry on restart. Job progress is derived from durable per-chunk run records.
"""
from __future__ import annotations
import asyncio
import json
import logging
from uuid import uuid4

from .analysis import SCHEMAS, compact
from .domain import utc_now
from .llm import LLMError
from .manuscript import ManuscriptError, RevisionConflictError
from .structured_llm import StructuredError

log = logging.getLogger(__name__)


def model_signature(cfg) -> tuple:
    return tuple(getattr(cfg, k) for k in ("api_base_url", "api_key_env", "analysis_model",
                  "analysis_temperature", "context_window", "context_safety_ratio"))


class AnalysisJobs:
    def __init__(self, session):
        self.session = session
        self.active_id: str | None = None
        self.task: asyncio.Task | None = None
        self.pause_requested = False
        session.store.connection.execute(
            "UPDATE analysis_jobs SET status='interrupted',error=?,updated_at=? WHERE status IN ('running','pausing')",
            ("程序退出时任务未完成；继续分析会复用已完成分块", utc_now()))

    @property
    def busy(self) -> bool:
        return self.active_id is not None

    def progress_locked(self, plan_id: str, passes: list[str], total: int) -> dict:
        latest, success = {}, set()
        for row in self.session.store.connection.execute(
                "SELECT ordinal,pass_type,schema_key,status FROM analysis_runs WHERE plan_id=? ORDER BY rowid", (plan_id,)):
            kind = row["pass_type"]
            if kind not in passes or kind not in SCHEMAS or row["schema_key"] != SCHEMAS[kind][1]:
                continue
            key = (row["ordinal"], kind)
            latest[key] = row["status"]
            if row["status"] == "done":
                success.add(key)
        failed = {key for key, status in latest.items() if status in {"failed", "interrupted"} and key not in success}
        return {"total": total*len(passes), "completed": len(success), "failed": len(failed),
                "remaining": total*len(passes)-len(success),
                "_success": success, "_latest": latest}

    async def start(self, plan_id: str, expected: int, passes: list[str], retry_failed: bool, *, transport=None) -> dict:
        s = self.session
        async with s.lock:
            if self.busy or s.model_gate.locked() or s.generation.busy:
                raise LLMError("busy", "已有分析任务或模型请求，请先完成或暂停")
            if not passes or len(passes) != len(set(passes)) or any(p not in SCHEMAS for p in passes):
                raise ManuscriptError("请选择不重复的 facts / links / narrative 分析类型")
            for p in passes:
                package = s.analysis._input_locked(plan_id, 0, expected, p)
            config = package.config
            if not config.analysis_model:
                raise LLMError("configuration", "请先配置分析模型")
            jid, now = uuid4().hex, utc_now()
            def persist(conn):
                conn.execute("INSERT INTO analysis_jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (jid, plan_id, expected, compact(passes), compact(config.model_dump()), int(retry_failed),
                     "running", None, now, now))
            s._commit_snapshot_locked(extra=persist)
            self.active_id = jid
            self.pause_requested = False
            self.task = asyncio.create_task(self._worker(jid, plan_id, expected, passes, retry_failed,
                                                        config, transport), name=f"analysis-{jid}")
        return await self.view(jid)

    async def _status(self, jid: str, status: str, error: str | None = None):
        async with self.session.lock:
            self.session._commit_snapshot_locked(extra=lambda c: c.execute(
                "UPDATE analysis_jobs SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error[:2000] if error else None, utc_now(), jid)))

    async def _worker(self, jid, plan_id, expected, passes, retry_failed, config, transport):
        s = self.session
        try:
            async with s.lock:
                plan = s.imports.last_plan
                total = len(plan.chunks)
                progress = self.progress_locked(plan_id, passes, total)
            for ordinal in range(total):
                for kind in passes:
                    if self.pause_requested:
                        await self._status(jid, "paused", "已保存当前分块；可继续未完成部分")
                        return
                    async with s.lock:
                        if s.manuscript.revision_no != expected or s.imports.last_plan.id != plan_id:
                            raise RevisionConflictError("正文或计划已变化，请为当前正文重建分析计划")
                        if model_signature(s.project.data.config) != model_signature(config):
                            await_error = True
                        else:
                            await_error = False
                    if await_error:
                        await self._status(jid, "paused", "模型配置已变化；停止自动发送后续请求，请重新开始")
                        return
                    key = (ordinal, kind)
                    if key in progress["_success"]:
                        continue
                    if not retry_failed and progress["_latest"].get(key) in {"failed", "interrupted"}:
                        continue
                    try:
                        result = await s.analysis.run_chunk(plan_id, ordinal, expected, transport=transport,
                                                            pass_type=kind, job_id=jid)
                        if result["stale"]:
                            raise RevisionConflictError("等待模型期间正文已变化，本次结果已隔离")
                    except StructuredError:
                        # Bad content in one chunk does not prevent other chunks.
                        continue
                    except LLMError as exc:
                        if exc.code in {"configuration", "context_budget", "busy", "connection", "http_error", "timeout"}:
                            await self._status(jid, "paused", str(exc))
                            return
                        # E.g. truncated output; inspect and explicitly retry later.
                        continue
            async with s.lock:
                final = self.progress_locked(plan_id, passes, total)
            await self._status(jid, "done" if final["completed"] == final["total"] else "partial")
        except asyncio.CancelledError:
            await self._status(jid, "interrupted", "任务因程序关闭而中断；重开后可继续")
            raise
        except (ManuscriptError, RevisionConflictError) as exc:
            await self._status(jid, "stale", str(exc))
        except Exception:
            log.exception("全书分析失败")
            try:
                await self._status(jid, "failed", "任务失败；请检查磁盘和日志。已提交的分块不会丢弃")
            except Exception:
                log.exception("无法保存全书分析失败状态")
        finally:
            self.active_id = None

    async def pause(self, jid: str) -> dict:
        async with self.session.lock:
            if jid != self.active_id:
                raise ManuscriptError("任务未在运行")
            self.pause_requested = True
            self.session._commit_snapshot_locked(extra=lambda c: c.execute(
                "UPDATE analysis_jobs SET status='pausing',error=?,updated_at=? WHERE id=? AND status='running'",
                ("当前请求结束并保存后暂停；不强制中断服务端推理", utc_now(), jid)))
        return await self.view(jid)

    async def view(self, jid: str | None = None) -> dict:
        s = self.session
        async with s.lock:
            if jid:
                row = s.store.connection.execute("SELECT * FROM analysis_jobs WHERE id=?", (jid,)).fetchone()
            else:
                row = s.store.connection.execute("SELECT * FROM analysis_jobs ORDER BY rowid DESC LIMIT 1").fetchone()
            if row is None:
                if jid:
                    raise ManuscriptError("全书分析任务不存在")
                return {"job": None}
            job = dict(row)
            job["passes"] = json.loads(job.pop("passes_json"))
            job.pop("config_json")
            plan = s.store.connection.execute("SELECT plan_json FROM chunk_plans WHERE id=?", (job["plan_id"],)).fetchone()
            total = len(json.loads(plan["plan_json"])["chunks"])
            progress = self.progress_locked(job["plan_id"], job["passes"], total)
            job["progress"] = {k: v for k, v in progress.items() if not k.startswith("_")}
            job["stale"] = job["base_revision_no"] != s.manuscript.revision_no
            job["active"] = self.active_id == job["id"]
            return {"job": job}

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
        if self.task:
            try:
                await self.task
            except asyncio.CancelledError:
                pass
