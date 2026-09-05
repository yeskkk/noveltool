"""Import/plan workflows; commits reuse the same atomic session transaction."""
from __future__ import annotations
import time
from typing import TYPE_CHECKING
from .import_text import ImportPreview, PREVIEW_TTL_SECONDS
from .chunker import ChunkPlan, PlanSettings, make_plan, plan_view, verify_plan
from .manuscript import ManuscriptError
from .revision import RevisionEngine
from .domain import utc_now
from hashlib import sha256
if TYPE_CHECKING:
    from .runtime import ProjectSession

class ImportService:
    def __init__(self,session:ProjectSession):
        self.session=session
        self.previews:dict[str,tuple[float,ImportPreview,int]]={}
        row=session.store.connection.execute('SELECT plan_json FROM chunk_plans ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
        self.plan_load_error: str | None = None
        try:
            self.last_plan=ChunkPlan.model_validate_json(row['plan_json']) if row else None
            if self.last_plan and self.last_plan.base_revision_no==session.manuscript.revision_no:
                verify_plan(self.last_plan,session.manuscript)
        except (ValueError,TypeError):
            # A derived plan is disposable; never block access to confirmed text.
            self.last_plan=None
            self.plan_load_error='保存的分块计划校验失败，已停止使用；正文不受影响，请重建计划。'

    async def remember(self,preview:ImportPreview)->dict:
        async with self.session.lock:
            now=time.monotonic()
            self.previews={k:v for k,v in self.previews.items() if now-v[0]<PREVIEW_TTL_SECONDS}
            if len(self.previews)>=2:self.previews.pop(next(iter(self.previews)))
            revision=self.session.manuscript.revision_no
            self.previews[preview.id]=(now,preview,revision)
            return {**preview.view(),'expected_revision_no':revision,
                    'can_commit':not bool(self.session.manuscript.blocks)}

    async def commit(self,preview_id:str,expected:int)->dict:
        s=self.session
        async with s.lock:
            item=self.previews.get(preview_id)
            if not item or time.monotonic()-item[0]>=PREVIEW_TTL_SECONDS:
                self.previews.pop(preview_id,None)
                raise ManuscriptError('导入预览已过期或服务已重启，请重新选择文件预览')
            _,p,base=item
            s.manuscript.check_revision(expected)
            if expected!=base:raise ManuscriptError('导入预览基于旧正文，请重新预览')
            plan=RevisionEngine.import_text(s.manuscript,p.text,expected,p.paragraph_mode)
            if plan is None:raise ManuscriptError('没有可导入的正文')
            def persist_source(conn):
                conn.execute('INSERT INTO source_imports VALUES(?,?,?,?,?,?,?,?,?)',
                             (p.id,p.filename,p.encoding,p.paragraph_mode,p.raw,p.raw_hash,
                              p.normalized_hash,plan.revision.id,utc_now()))
            s._commit_snapshot_locked(plan=plan,extra=persist_source)
            del self.previews[preview_id]
        return await s.manuscript_view()

    async def create_plan(self,settings:PlanSettings,expected:int)->dict:
        s=self.session
        async with s.lock:
            s.manuscript.check_revision(expected)
            config=s.project.data.config
            plan=make_plan(s.manuscript,settings,config.context_window,config.context_safety_ratio)
            def persist(conn):
                conn.execute('INSERT INTO chunk_plans VALUES(?,?,?,?,?)',
                             (plan.id,plan.base_revision_no,plan.manuscript_hash,plan.model_dump_json(),plan.created_at))
            s._commit_snapshot_locked(extra=persist)
            self.last_plan=plan
            self.plan_load_error=None
            return plan_view(plan,s.manuscript)

    async def current_plan(self)->dict:
        async with self.session.lock:
            cfg=self.session.project.data.config
            return {'plan':plan_view(self.last_plan,self.session.manuscript,context_window=cfg.context_window,safety_ratio=cfg.context_safety_ratio) if self.last_plan else None,
                    'load_error':self.plan_load_error}

    async def sources(self)->list[dict]:
        async with self.session.lock:
            return [dict(row) for row in self.session.store.connection.execute(
                'SELECT id,filename,encoding,paragraph_mode,length(raw_bytes) AS raw_bytes,raw_hash,created_at '
                'FROM source_imports ORDER BY created_at DESC LIMIT 20')]

    async def original(self,source_id:str)->bytes:
        async with self.session.lock:
            row=self.session.store.connection.execute('SELECT raw_bytes,raw_hash FROM source_imports WHERE id=?',(source_id,)).fetchone()
            if row is None:raise ManuscriptError('原始导入文件不存在')
            raw=bytes(row['raw_bytes'])
            if sha256(raw).hexdigest()!=row['raw_hash']:
                raise ManuscriptError('原始文件哈希校验失败，请检查备份；已确认正文不受影响')
            return raw
