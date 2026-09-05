"""Incremental semantic coverage, source reuse, and checkpointed synchronization.

Reuse validates the ENTIRE original input (core + overlap), not just a quote.
Runs keep their original identifiers; a small mapping table associates unchanged
input with a new plan. No LLM output is copied into a new fake analysis run.
The current projection is independent of the most recently opened chunk plan.
"""
from __future__ import annotations
from bisect import bisect_left
from hashlib import sha256
from uuid import uuid4

from .analysis import SCHEMAS, compact
from .analysis_jobs import model_signature
from .chunker import (Chunk, ChunkPlan, PlanSettings, CHUNK_OVERHEAD, REF_OVERHEAD,
                      _overlap, estimate_parts, make_plan, slice_text, verify_plan)
from .domain import ProjectConfig, utc_now
from .llm import LLMError
from .manuscript import ManuscriptError, text_hash
from .structured_llm import strict_loads, StructuredError


def signature(refs: dict) -> tuple:
    """Prompt short names do not matter, but slice ordering and role do."""
    return tuple((role, tuple((v['block_id'], v['start_cp'], v['end_cp'])
                             for v in refs.values() if v['scope'] == role))
                 for role in ('core', 'overlap'))


def validate_run(run: dict, doc, blocks: dict, offsets: dict) -> dict:
    refs = strict_loads(run['refs_json'])
    if not isinstance(refs, dict) or not refs:
        raise ValueError('缺少原始输入引用')
    groups = {'core': [], 'overlap': []}
    for ref, part in refs.items():
        if not isinstance(ref, str) or not isinstance(part, dict) or set(part) != {'block_id','start_cp','end_cp','scope'}:
            raise ValueError('分析引用结构无效')
        bid, a, b, role = (part[k] for k in ('block_id','start_cp','end_cp','scope'))
        if role not in groups or bid not in blocks or type(a) is not int or type(b) is not int or not 0 <= a < b <= len(blocks[bid]):
            raise ValueError('原输入引用的正文块已退役或越界')
        groups[role].append((offsets[bid]+a, offsets[bid]+b, blocks[bid][a:b]))
    core = groups['core']
    if not core:
        raise ValueError('没有核心范围')
    for parts in groups.values():
        if any(a[1] != b[0] for a,b in zip(parts,parts[1:])):
            raise ValueError('原输入范围不再连续')
    if groups['overlap'] and groups['overlap'][-1][1] != core[0][0]:
        raise ValueError('原来的前置上下文已不再紧邻核心')
    if sha256(''.join(p[2] for p in core).encode()).hexdigest() != run['core_hash']:
        raise ValueError('核心校验和错误')
    return {**run, 'refs': refs, 'start': core[0][0], 'end': core[-1][1], 'signature': signature(refs)}


class SemanticSync:
    def __init__(self, session):
        self.session = session
        self.key = None
        self.eligible = []
        self.selected = []
        self.excluded_count = 0

    def refresh_locked(self):
        s = self.session
        key = (s.manuscript.revision_no, s.analysis.epoch)
        if key == self.key:
            return
        blocks, offsets, cursor = {}, {}, 0
        for b in s.manuscript.blocks:
            blocks[b.id] = b.text; offsets[b.id] = cursor; cursor += len(b.text)
        valid, excluded = [], 0
        for row in s.store.connection.execute("SELECT rowid,* FROM analysis_runs WHERE status='done' ORDER BY rowid DESC"):
            run = dict(row)
            if run['pass_type'] not in SCHEMAS or run['schema_key'] != SCHEMAS[run['pass_type']][1]:
                continue
            try:
                valid.append(validate_run(run, s.manuscript, blocks, offsets))
            except (ValueError, TypeError, KeyError, RecursionError, StructuredError):
                excluded += 1
        # Newest success wins an overlapping source range. A failed retry never
        # enters this set, and a new empty result can deliberately replace old facts.
        chosen, intervals = [], {kind: [] for kind in SCHEMAS}
        for run in valid:
            spans = intervals[run['pass_type']]
            if any(run['start'] < b and a < run['end'] for a,b in spans):
                continue
            spans.append((run['start'],run['end'])); chosen.append(run)
        self.eligible, self.selected, self.excluded_count, self.key = valid, chosen, excluded, key

    def status_locked(self) -> dict:
        self.refresh_locked()
        s = self.session; size = len(s.manuscript.text)
        coverage = {kind: sum(r['end']-r['start'] for r in self.selected if r['pass_type']==kind) for kind in SCHEMAS}
        ids = [r['id'] for r in self.selected if r['requires_review']]
        pending = 0
        for rid in ids:
            pending += s.store.connection.execute("SELECT count(*) FROM observations WHERE run_id=? AND status='pending'",(rid,)).fetchone()[0]
        s.knowledge.refresh_locked()
        errors = len(s.knowledge.errors)
        complete = all(n == size for n in coverage.values())
        return {'revision_no': s.manuscript.revision_no, 'total_chars': size, 'coverage': coverage,
                'status': ('needs_review' if pending or errors else 'synced') if complete else 'pending',
                'covered': complete, 'pending_reviews': pending, 'validation_errors': errors,
                'selected_runs': len(self.selected), 'excluded_runs': self.excluded_count,
                'busy': s.jobs.busy,
                'notice': '覆盖完成只表示抽取流程完成，不保证事实正确。完整旧输入仍有效的分析会复用；失效范围不进入当前设定。返修对后文的因果影响需另外检查。'}

    async def status(self):
        async with self.session.lock:
            return self.status_locked()

    def reuse_for_plan_locked(self, plan_id: str) -> dict:
        self.refresh_locked()
        valid = {r['id']: r for r in self.eligible}
        return {(r['ordinal'],r['pass_type']): valid[r['run_id']]
                for r in self.session.store.connection.execute('SELECT * FROM analysis_reuse WHERE plan_id=?',(plan_id,))
                if r['run_id'] in valid}

    def plan_locked(self, expected: int, settings: PlanSettings | None = None):
        s = self.session; s.manuscript.check_revision(expected)
        if s.jobs.busy or s.generation.busy or s.model_gate.locked() or s.consistency.busy:
            raise LLMError('busy','已有模型任务；正文已保存，可在任务结束后同步设定')
        cfg = s.project.data.config
        if not cfg.analysis_model:
            raise LLMError('configuration','请先填写分析模型；正文已保存，设定等待同步')
        self.refresh_locked()
        settings = settings or (s.imports.last_plan.settings if s.imports.last_plan else PlanSettings())
        baseline = make_plan(s.manuscript, settings, cfg.context_window, cfg.context_safety_ratio)
        rendered = s.manuscript.render(); blocks = {b.id:b.text for b in s.manuscript.blocks}
        spans = rendered.spans; ends = [sp.end_cp for sp in spans]
        def slices(a,b):
            from .chunker import Slice
            out=[]; i=bisect_left(ends,a+1)
            while i<len(spans) and spans[i].start_cp<b:
                sp=spans[i]
                out.append(Slice(block_id=sp.block_id,start_cp=max(a,sp.start_cp)-sp.start_cp,end_cp=min(b,sp.end_cp)-sp.start_cp));i+=1
            return tuple(out)
        capacity = baseline.effective_chunk_limit-CHUNK_OVERHEAD-settings.overlap_tokens
        retained=[]; compatible=[]
        for run in self.eligible:
            try:
                if model_signature(ProjectConfig.model_validate_json(run['config_json'])) != model_signature(cfg):
                    continue
            except (ValueError,TypeError):
                continue
            compatible.append(run)
            a,b=run['start'],run['end']
            if any(a<y and x<b for x,y in retained):
                continue
            if estimate_parts(slices(a,b),blocks)<=capacity:
                retained.append((a,b))
        retained.sort()
        # Fill uncovered gaps using the bounded baseline chunk boundaries.
        cuts=[];at=0
        for chunk in baseline.chunks:
            at += sum(p.end_cp-p.start_cp for p in chunk.core);cuts.append(at)
        ranges=[];cursor=0
        def gap(a,b):
            i=bisect_left(cuts,a+1)
            while a<b:
                end=min(b,cuts[i]);ranges.append((a,end));a=end;i+=1
        for a,b in retained:
            gap(cursor,a);ranges.append((a,b));cursor=b
        gap(cursor,len(s.manuscript.text))
        chunks=[];reuse=[]; lookup={}
        for run in compatible: lookup.setdefault(run['signature']+(run['pass_type'],),run)
        for ordinal,(a,b) in enumerate(ranges):
            core=slices(a,b)
            overlap=_overlap(chunks[-1].core,blocks,settings.overlap_tokens) if chunks else ()
            chunks.append(Chunk(ordinal=ordinal,core=core,overlap=overlap,
                estimated_tokens=CHUNK_OVERHEAD+estimate_parts(core+overlap,blocks),
                core_hash=text_hash(''.join(slice_text(p,blocks) for p in core))))
            refs={str(i):{**p.model_dump(),'scope':scope} for i,(scope,p) in enumerate(
                [('core',p) for p in core]+[('overlap',p) for p in overlap])}
            for kind in SCHEMAS:
                run=lookup.get(signature(refs)+(kind,))
                if run:reuse.append((ordinal,kind,run['id']))
        plan=baseline.model_copy(update={'id':uuid4().hex,'chunks':tuple(chunks),'created_at':utc_now()})
        verify_plan(plan,s.manuscript)
        def persist(conn):
            conn.execute('INSERT INTO chunk_plans VALUES(?,?,?,?,?)',(plan.id,expected,plan.manuscript_hash,plan.model_dump_json(),plan.created_at))
            conn.executemany('INSERT INTO analysis_reuse VALUES(?,?,?,?)',[(plan.id,*r) for r in reuse])
        s._commit_snapshot_locked(extra=persist)
        s.imports.last_plan=plan;s.imports.plan_load_error=None
        return plan,len(reuse)

    async def start(self, expected: int, *, transport=None, settings=None):
        async with self.session.lock:
            plan,count=self.plan_locked(expected,settings)
        result=await self.session.jobs.start(plan.id,expected,list(SCHEMAS),True,transport=transport)
        return {**result,'reused_units':count,'plan_id':plan.id}
