"""Independent, sequential prose candidates based on an immutable context.

A whole candidate is a checkpoint; regenerating never overwrites an older
success. Worker state is durable, model work is explicitly started/resumed,
and task/candidate snapshots are hot-cached while old tasks load on demand.
"""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
import logging
from uuid import uuid4
from pydantic import Field
from .analysis import compact
from .context import ContextRequest
from .domain import ProjectConfig,utc_now
from .drafts import GenerationDrafts
from .rewrite import RewriteTarget, capture_target
from .llm import LLMClient,LLMError,request_token_estimate
from .manuscript import ManuscriptError,RevisionConflictError,check_text,count_chars
from .settings import Strict
from .structured_llm import strict_loads

log=logging.getLogger(__name__)
SUCCESS={'complete','length_mismatch'}


class GenerationRequest(Strict):
    context:ContextRequest
    candidate_count:int|None=Field(default=None,ge=1,le=32)


class GenerationService:
    def __init__(self,session):
        self.session=session
        self.active_id=None
        self.task:asyncio.Task|None=None
        self.pause_requested=False
        self._cache=OrderedDict()
        self.drafts=GenerationDrafts(self)
        conn=session.store.connection
        conn.execute("UPDATE generation_tasks SET status='interrupted',error=?,updated_at=? WHERE status IN ('generating','pausing')",
                     ('程序退出时生成未完成；成功候选已保存，继续需明确操作',utc_now()))
        conn.execute("UPDATE generation_attempts SET status='interrupted',error=?,updated_at=? WHERE status='running'",
                     ('请求未完成，不能当成可用候选',utc_now()))

    @property
    def busy(self):return self.active_id is not None

    def _busy_locked(self):
        if self.busy or self.session.jobs.busy or self.session.model_gate.locked() or self.session.consistency.busy:
            raise LLMError('busy','已有模型任务，请先完成或暂停它')

    def _invalidate(self,tid):self._cache.pop(tid,None)

    def _bundle_locked(self,tid):
        if tid not in self._cache:
            row=self.session.store.connection.execute('SELECT * FROM generation_tasks WHERE id=?',(tid,)).fetchone()
            if row is None:raise ManuscriptError('生成任务不存在')
            row=dict(row)
            try:
                config=ProjectConfig.model_validate_json(row.pop('config_json'))
                context=strict_loads(row.pop('context_json'))
                request=GenerationRequest.model_validate(strict_loads(row.pop('request_json')),strict=True)
                check_text(row['draft_text'])
                row['rewrite_target'] = None
                if row['task_type']=='rewrite':
                    stored=self.session.store.connection.execute('SELECT target_json FROM generation_rewrite_targets WHERE task_id=?',(tid,)).fetchone()
                    if stored is None:raise ValueError('返修任务缺少选区快照')
                    target=RewriteTarget.model_validate_json(stored['target_json'])
                    if (target.base_revision_no!=row['base_revision_no'] or
                        (target.start_cp,target.end_cp)!=(request.context.start_cp,request.context.end_cp) or
                        (target.start_cp,target.end_cp)!=(context['start_cp'],context['end_cp'])):
                        raise ValueError('返修选区与任务基线不一致')
                    section=next((sec for sec in context['sections'] if sec['key']=='target'),None)
                    if section is None or section['text']!=target.selected_text or not section['included']:
                        raise ValueError('返修目标与上下文快照不一致')
                    row['rewrite_target']=target.model_dump()
                if request.context.task_type!=row['task_type'] or context['task_type']!=row['task_type']:
                    raise ValueError('任务类型不一致')
                if row['status']=='committed' and (not row['committed_revision_id'] or row['committed_text_hash']!=sha256(row['draft_text'].encode()).hexdigest()):
                    raise ValueError('确认记录不完整')
                if not isinstance(context,dict) or context['base_revision_no']!=row['base_revision_no'] or context['setting_version']!=row['base_setting_version']:
                    raise ValueError('上下文基线不一致')
                fingerprint=sha256(compact([context['messages'],context['output_token_reserve'],row['base_revision_no'],row['base_setting_version']]).encode()).hexdigest()
                if context['fingerprint']!=fingerprint or context['input_token_estimate']!=request_token_estimate(context['messages']):
                    raise ValueError('上下文指纹无效')
                attempts=[dict(r) for r in self.session.store.connection.execute(
                    'SELECT * FROM generation_attempts WHERE task_id=? ORDER BY candidate_index,attempt_no',(tid,))]
                for a in attempts:
                    from .language import for_id,for_owner
                    a['language']=for_id(self.session,a.get('language_id')) or for_owner(self.session,'generation',a['id'])
                    check_text(a['text'])
                    if a['status'] in SUCCESS:
                        if not a['text'].strip() or count_chars(a['text'])!=a['char_count']:
                            raise ValueError('候选字数校验失败')
                        within=context['min_chars']<=a['char_count']<=context['max_chars']
                        if within!=(a['status']=='complete'):raise ValueError('候选范围状态错误')
                self._cache[tid]=(row,config,context,request,attempts)
            except (ValueError,TypeError,KeyError) as exc:
                raise ManuscriptError('生成任务存储数据无法通过校验，未改变正文') from exc
        self._cache.move_to_end(tid)
        while len(self._cache)>16:self._cache.popitem(last=False)
        return self._cache[tid]

    def _stale_locked(self,row):
        s=self.session;s.knowledge.refresh_locked()
        return row['base_revision_no']!=s.manuscript.revision_no or row['base_setting_version']!=s.knowledge.version

    def _slots_locked(self,row,attempts):
        slots=[];duplicates={}
        for index in range(row['candidate_count']):
            tries=[a for a in attempts if a['candidate_index']==index]
            usable=next((a for a in reversed(tries) if a['status'] in SUCCESS),None)
            latest=tries[-1] if tries else None
            slot={'index':index,'candidate':usable,'latest_attempt':latest,'attempt_count':len(tries),'duplicate_of':None}
            if usable:
                if usable['text'] in duplicates:slot['duplicate_of']=duplicates[usable['text']]
                else:duplicates[usable['text']]=index
            slots.append(slot)
        return slots

    def _view_locked(self,tid):
        row,config,context,request,attempts=self._bundle_locked(tid)
        slots=self._slots_locked(row,attempts)
        return deepcopy({**row,**self.drafts.overlay_locked(row),'model':config.writer_model,'temperature':config.writer_temperature,
            'min_chars':context['min_chars'],'max_chars':context['max_chars'],'auto_chinese':config.auto_chinese,
            'instruction':request.context.instruction,'context_fingerprint':context['fingerprint'],
            'context_warnings':context.get('warnings',[]),
            'slots':slots,'attempts':attempts,'usable_count':sum(bool(s['candidate']) for s in slots),
            'in_range_count':sum(bool(s['candidate']) and s['candidate']['status']=='complete' for s in slots),
            'live':self.active_id==tid,'stale':row['status']!='committed' and self._stale_locked(row)})

    async def view(self,tid):
        async with self.session.lock:return self._view_locked(tid)

    async def context_view(self,tid):
        async with self.session.lock:return deepcopy(self._bundle_locked(tid)[2])

    async def list(self):
        async with self.session.lock:
            return [dict(r) for r in self.session.store.connection.execute(
                'SELECT id,task_type,status,candidate_count,base_revision_no,error,created_at,updated_at FROM generation_tasks ORDER BY rowid DESC LIMIT 100')]

    def _launch_locked(self,tid,indices,transport):
        self.active_id=tid;self.pause_requested=False
        self.task=asyncio.create_task(self._worker(tid,indices,transport),name=f'writer-{tid}')

    async def start(self,body:GenerationRequest,*,transport=None):
        s=self.session
        async with s.lock:
            self._busy_locked()
            package=s.context.build_locked(body.context)
            config=s.project.data.config
            if not config.writer_model:raise LLMError('configuration','请先填写并应用写作模型名')
            n=config.candidate_count if body.candidate_count is None else body.candidate_count
            tid,now=uuid4().hex,utc_now()
            target=capture_target(s.manuscript,body.context.start_cp,body.context.end_cp) if body.context.task_type=='rewrite' else None
            def persist(c):
                c.execute('''INSERT INTO generation_tasks
                    (id,task_type,base_revision_no,base_setting_version,candidate_count,request_json,config_json,context_json,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,'generating',?,?)''',(tid,body.context.task_type,package.base_revision_no,package.setting_version,n,
                    body.model_dump_json(),config.model_dump_json(),compact(package.to_dict()),now,now))
                if target:c.execute('INSERT INTO generation_rewrite_targets VALUES(?,?)',(tid,target.model_dump_json()))
            s._commit_snapshot_locked(extra=persist)
            self._launch_locked(tid,list(range(n)),transport)
        return await self.view(tid)

    async def _status(self,tid,status,error=None):
        s=self.session
        async with s.lock:
            s._commit_snapshot_locked(extra=lambda c:c.execute(
                'UPDATE generation_tasks SET status=?,error=?,updated_at=? WHERE id=?',
                (status,error[:2000] if error else None,utc_now(),tid)))
            self._invalidate(tid)

    async def _finish_attempt(self,tid,aid,status,text='',error=None,run_id=None,language_id=None):
        s=self.session
        async with s.lock:
            chars=count_chars(text) if status in SUCCESS else 0
            def apply(c):
                c.execute('UPDATE generation_attempts SET status=?,text=?,char_count=?,error=?,llm_run_id=?,updated_at=?,language_id=? WHERE id=?',
                    (status,text,chars,error[:2000] if error else None,run_id,utc_now(),language_id,aid))
                c.execute('UPDATE generation_tasks SET updated_at=? WHERE id=?',(utc_now(),tid))
            s._commit_snapshot_locked(extra=apply);self._invalidate(tid)

    async def _worker(self,tid,indices,transport):
        s=self.session;active_attempt=None
        try:
            async with s.model_gate:
                async with s.lock:
                    row,config,context,_,_=self._bundle_locked(tid)
                    s.knowledge.refresh_locked()
                    protected_names=[n for e in s.knowledge.graph['entities'] for n in [e['name'],*e.get('names',[]),*e.get('aliases',[])]]
                async with LLMClient(config,on_run=s.record_llm_run,transport=transport) as client:
                    for index in indices:
                        if self.pause_requested:
                            await self._status(tid,'paused','已停止发送后续候选；可明确点击继续')
                            return
                        async with s.lock:
                            row,_,_,_,attempts=self._bundle_locked(tid)
                            if self._stale_locked(row):raise RevisionConflictError('正文或设定已变化，停止旧基线后续生成')
                            number=1+max((a['attempt_no'] for a in attempts if a['candidate_index']==index),default=0)
                            active_attempt=uuid4().hex;now=utc_now()
                            s._commit_snapshot_locked(extra=lambda c:c.execute('''INSERT INTO generation_attempts
                                (id,task_id,candidate_index,attempt_no,status,created_at,updated_at) VALUES(?,?,?,?,'running',?,?)''',
                                (active_attempt,tid,index,number,now,now)))
                            self._invalidate(tid)
                        try:
                            completion=await client.complete(messages=context['messages'],model=config.writer_model,
                                temperature=config.writer_temperature,max_tokens=context['output_token_reserve'],purpose='prose_candidate')
                            text=completion.text.strip();check_text(text)
                            if not text:raise ManuscriptError('模型返回了空白文本')
                            from .language import ChineseRenderer
                            converted=await ChineseRenderer(s,client,'generation',active_attempt).render(text,protected_names=protected_names)
                            text=converted.text;check_text(text)
                            chars=count_chars(text)
                            status='complete' if context['min_chars']<=chars<=context['max_chars'] else 'length_mismatch'
                            await self._finish_attempt(tid,active_attempt,status,text,run_id=completion.run_id,language_id=converted.id)
                        except LLMError as exc:
                            await self._finish_attempt(tid,active_attempt,'failed',exc.partial_text,error=str(exc),run_id=exc.run_id)
                            active_attempt=None
                            if exc.code in {'connection','timeout','http_error','configuration','context_budget','protocol'}:
                                await self._status(tid,'paused',str(exc)+'；已保存的候选不受影响')
                                return
                            # A single refusal/truncation/empty result is isolated.
                        active_attempt=None
                        async with s.lock:
                            row,_,_,_,_=self._bundle_locked(tid)
                            stale=self._stale_locked(row)
                        if stale:raise RevisionConflictError('等待模型期间正文或设定已改变；结果保留但不能直接提交')
                async with s.lock:
                    v=self._view_locked(tid)
                await self._status(tid,'ready' if v['usable_count'] else 'failed',
                    None if v['usable_count']==v['candidate_count'] else '部分或全部候选未完成；可单独重试')
        except asyncio.CancelledError:
            if active_attempt:await self._finish_attempt(tid,active_attempt,'interrupted',error='请求取消，未作为可用候选')
            await self._status(tid,'interrupted','程序关闭或任务取消；已有候选已保存，重开不自动发请求')
            raise
        except RevisionConflictError as exc:
            await self._status(tid,'stale',str(exc))
        except Exception:
            log.exception('候选生成失败')
            try:
                if active_attempt:await self._finish_attempt(tid,active_attempt,'failed',error='候选校验或保存失败，请检查日志')
                await self._status(tid,'failed','生成任务异常；已保存的候选不丢弃，请检查磁盘与日志')
            except Exception:log.exception('无法保存生成失败状态')
        finally:self.active_id=None

    async def resume(self,tid,*,index=None,transport=None):
        s=self.session
        async with s.lock:
            self._busy_locked()
            row,_,_,_,attempts=self._bundle_locked(tid)
            if row['status']=='committed':raise ManuscriptError('已经确认的任务不能继续生成')
            if self._stale_locked(row):raise RevisionConflictError('任务基线已过期；请创建新任务，旧候选仍可查看')
            if index is not None and (type(index) is not int or not 0<=index<row['candidate_count']):
                raise ManuscriptError('候选编号不存在')
            if index is None:
                slots=self._slots_locked(row,attempts)
                indices=[s['index'] for s in slots if not s['candidate']]
            else:indices=[index]
            if not indices:return self._view_locked(tid)
            s._commit_snapshot_locked(extra=lambda c:c.execute("UPDATE generation_tasks SET status='generating',error=NULL,updated_at=? WHERE id=?",(utc_now(),tid)))
            self._invalidate(tid);self._launch_locked(tid,indices,transport)
        return await self.view(tid)

    async def pause(self,tid):
        s=self.session
        async with s.lock:
            if self.active_id!=tid:raise ManuscriptError('该任务没有在运行')
            s._commit_snapshot_locked(extra=lambda c:c.execute("UPDATE generation_tasks SET status='pausing',error=?,updated_at=? WHERE id=?",('完成当前请求后暂停',utc_now(),tid)))
            self.pause_requested=True;self._invalidate(tid)
        return await self.view(tid)

    async def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass
