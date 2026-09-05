"""Lossless, traceable chunk planning without a model/tokenizer dependency.

Estimates use UTF-8 byte count plus explicit per-fragment overhead. This is a
conservative heuristic, NOT a guarantee about a provider's actual tokenizer.
Core ranges cover the manuscript exactly once; overlap ranges are context only.
"""
from __future__ import annotations
from array import array
from bisect import bisect_right
from hashlib import sha256
import re
from uuid import uuid4
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .domain import utc_now
from .manuscript import Manuscript, ManuscriptError, text_hash

REF_OVERHEAD=24
CHUNK_OVERHEAD=64
MAX_CHUNKS=4096
MAX_FRAGMENTS=100_000

class Strict(BaseModel):
    model_config=ConfigDict(strict=True,extra='forbid',frozen=True,allow_inf_nan=False)

class PlanSettings(Strict):
    target_tokens: int = Field(default=6000,ge=256,le=32000)
    overlap_tokens: int = Field(default=400,ge=0,le=4096)
    prompt_reserve: int = Field(default=4096,ge=256,le=16000)
    output_reserve: int = Field(default=1536,ge=128,le=8192)

    @model_validator(mode='after')
    def valid_overlap(self):
        if 0<self.overlap_tokens<32: raise ValueError('overlap 为 0 或至少 32')
        return self

class Slice(Strict):
    block_id: str = Field(min_length=1)
    start_cp: int = Field(ge=0)
    end_cp: int = Field(ge=1)

    @model_validator(mode='after')
    def nonempty(self):
        if self.end_cp<=self.start_cp: raise ValueError('切片不能为空')
        return self

class Chunk(Strict):
    ordinal: int = Field(ge=0)
    core: tuple[Slice,...] = Field(min_length=1)
    overlap: tuple[Slice,...]
    estimated_tokens: int = Field(ge=1)
    core_hash: str = Field(min_length=64,max_length=64)

class ChunkPlan(Strict):
    id: str
    algorithm: Literal['utf8-bytes-v1'] = 'utf8-bytes-v1'
    base_revision_no: int = Field(ge=1)
    manuscript_hash: str = Field(min_length=64,max_length=64)
    context_window: int = Field(ge=2048)
    safety_ratio: float = Field(gt=0,le=1)
    settings: PlanSettings
    effective_chunk_limit: int = Field(ge=1) # retained estimate
    chunks: tuple[Chunk,...] = Field(min_length=1,max_length=MAX_CHUNKS)
    created_at: str


def slice_text(part: Slice, blocks: dict[str,str]) -> str:
    text=blocks.get(part.block_id)
    if text is None or not 0<=part.start_cp<part.end_cp<=len(text):
        raise ManuscriptError('分块引用不存在或超出正文块范围')
    return text[part.start_cp:part.end_cp]


def estimate_parts(parts,blocks:dict[str,str])->int:
    return sum(len(slice_text(p,blocks).encode('utf-8'))+REF_OVERHEAD for p in parts)


def _overlap(previous:tuple[Slice,...],blocks:dict[str,str],budget:int)->tuple[Slice,...]:
    out=[]
    for part in reversed(previous):
        room=budget-REF_OVERHEAD
        if room<1:break
        text=slice_text(part,blocks)
        start=len(text);used=0
        while start>0:
            cost=len(text[start-1].encode('utf-8'))
            if used+cost>room:break
            used+=cost;start-=1
        if start<len(text):
            out.append(Slice(block_id=part.block_id,start_cp=part.start_cp+start,end_cp=part.end_cp))
            budget-=used+REF_OVERHEAD
        if start>0:break
    return tuple(reversed(out))


def make_plan(doc:Manuscript,settings:PlanSettings,context_window:int,safety_ratio:float)->ChunkPlan:
    if type(context_window) is not int or context_window<2048 or not 0<safety_ratio<=1:
        raise ManuscriptError('模型上下文与安全系数无效')
    if not doc.blocks or not doc.text.strip():raise ManuscriptError('请先导入或确认非空正文')
    if len(doc.blocks)>MAX_FRAGMENTS:raise ManuscriptError('正文块过多，无法安全构造分块计划')
    available=int(context_window*safety_ratio)-settings.prompt_reserve-settings.output_reserve
    limit=min(settings.target_tokens,available)
    capacity=limit-CHUNK_OVERHEAD-settings.overlap_tokens
    if capacity<128:
        raise ManuscriptError('上下文预算不足：请降低提示/输出/重叠预留，或提高模型上下文上限')
    blocks={b.id:b.text for b in doc.blocks}
    cores=[];current=[];used=0;total_fragments=0
    def flush():
        nonlocal current,used
        if current:
            cores.append(tuple(current));current=[];used=0
            if len(cores)>MAX_CHUNKS:raise ManuscriptError('分块超过 4096 块，请提高块预算')
    for block in doc.blocks:
        text=block.text
        # Prefix byte offsets for only this block, never the entire novel twice.
        prefix=array('I',[0])
        for ch in text:prefix.append(prefix[-1]+len(ch.encode('utf-8')))
        pos=0
        while pos<len(text):
            remaining=prefix[-1]-prefix[pos]
            room=capacity-used-REF_OVERHEAD
            if room<4 or (current and remaining>room and remaining+REF_OVERHEAD<=capacity):
                flush();room=capacity-REF_OVERHEAD
            end=min(len(text),bisect_right(prefix,prefix[pos]+room,lo=pos+1)-1)
            if end<=pos:raise ManuscriptError('预算不足以容纳一个 Unicode 字符')
            if end<len(text):
                # Prefer a nearby natural break without losing any characters.
                boundary=0
                for match in re.finditer(r'[\n。！？.!?]',text[pos:end]):
                    if match.end()>=(end-pos)//2:boundary=pos+match.end()
                if boundary>pos:end=boundary
            current.append(Slice(block_id=block.id,start_cp=pos,end_cp=end))
            used+=prefix[end]-prefix[pos]+REF_OVERHEAD
            total_fragments+=1
            if total_fragments>MAX_FRAGMENTS:raise ManuscriptError('切片超过 100000 个，请提高块预算')
            pos=end
            if pos<len(text):flush()
    flush()
    chunks=[]
    for ordinal,core in enumerate(cores):
        overlap=_overlap(cores[ordinal-1],blocks,settings.overlap_tokens) if ordinal else ()
        checksum=sha256()
        for p in core:checksum.update(slice_text(p,blocks).encode('utf-8'))
        chunks.append(Chunk(ordinal=ordinal,core=core,overlap=overlap,
                           estimated_tokens=CHUNK_OVERHEAD+estimate_parts(core+overlap,blocks),core_hash=checksum.hexdigest()))
    plan=ChunkPlan(id=uuid4().hex,base_revision_no=doc.revision_no,manuscript_hash=text_hash(doc.text),
                   context_window=context_window,safety_ratio=float(safety_ratio),settings=settings,
                   effective_chunk_limit=limit,chunks=tuple(chunks),created_at=utc_now())
    verify_plan(plan,doc)
    return plan


def verify_plan(plan:ChunkPlan,doc:Manuscript)->None:
    if plan.algorithm!='utf8-bytes-v1' or plan.base_revision_no!=doc.revision_no or plan.manuscript_hash!=text_hash(doc.text):
        raise ManuscriptError('分块计划已过期或算法不受支持')
    blocks={b.id:b.text for b in doc.blocks}
    offsets={};at=0
    for b in doc.blocks:offsets[b.id]=at;at+=len(b.text)
    def bounds(p):
        slice_text(p,blocks)
        return offsets[p.block_id]+p.start_cp,offsets[p.block_id]+p.end_cp
    cursor=0;previous_core_start=0
    allowed=min(plan.settings.target_tokens,int(plan.context_window*plan.safety_ratio)-plan.settings.prompt_reserve-plan.settings.output_reserve)
    if plan.effective_chunk_limit!=allowed:raise ManuscriptError('计划预算与配置不匹配')
    for i,chunk in enumerate(plan.chunks):
        if chunk.ordinal!=i:raise ManuscriptError('分块顺序不连续')
        core_start=cursor;check=sha256()
        for p in chunk.core:
            start,end=bounds(p)
            if start!=cursor:raise ManuscriptError('核心切片遗漏、重复或乱序')
            cursor=end;check.update(slice_text(p,blocks).encode('utf-8'))
        if check.hexdigest()!=chunk.core_hash:raise ManuscriptError('核心切片校验和错误')
        if chunk.overlap:
            start,end=bounds(chunk.overlap[0])
            if i==0 or start<previous_core_start:raise ManuscriptError('重叠不属于紧邻的前一块核心')
            overlap_cursor=start
            for p in chunk.overlap:
                a,b=bounds(p)
                if a!=overlap_cursor:raise ManuscriptError('重叠切片不连续')
                overlap_cursor=b
            if overlap_cursor!=core_start:raise ManuscriptError('重叠没有紧接当前核心')
        if estimate_parts(chunk.overlap,blocks)>plan.settings.overlap_tokens:raise ManuscriptError('重叠超预算')
        actual=CHUNK_OVERHEAD+estimate_parts(chunk.core+chunk.overlap,blocks)
        if actual!=chunk.estimated_tokens or actual>allowed:raise ManuscriptError('分块超出估计预算')
        previous_core_start=core_start
    if cursor!=at:raise ManuscriptError('计划未覆盖全部正文')


def plan_view(plan:ChunkPlan,doc:Manuscript,*,context_window:int|None=None,safety_ratio:float|None=None)->dict:
    text_changed=plan.base_revision_no!=doc.revision_no or plan.manuscript_hash!=text_hash(doc.text)
    budget_changed=(context_window is not None and context_window!=plan.context_window) or (safety_ratio is not None and safety_ratio!=plan.safety_ratio)
    stale=text_changed or budget_changed
    blocks={b.id:b.text for b in doc.blocks} if not stale else {}
    rows=[]
    # Metadata is complete; text excerpts are bounded to the first 80 chunks.
    for chunk in plan.chunks:
        excerpt=''
        if not stale and chunk.ordinal<80:
            for p in chunk.core:
                excerpt+=slice_text(p,blocks)[:max(0,180-len(excerpt))]
                if len(excerpt)>=180:break
        rows.append({'ordinal':chunk.ordinal,'estimated_tokens':chunk.estimated_tokens,
                     'core_chars':sum(p.end_cp-p.start_cp for p in chunk.core),
                     'overlap_chars':sum(p.end_cp-p.start_cp for p in chunk.overlap),
                     'core_ranges':[p.model_dump() for p in chunk.core],
                     'overlap_ranges':[p.model_dump() for p in chunk.overlap],
                     'excerpt':excerpt})
    return {'id':plan.id,'base_revision_no':plan.base_revision_no,'stale':stale,
            'stale_reason':('正文版本已变化' if text_changed else '模型上下文预算已变化' if budget_changed else ''),
            'algorithm':plan.algorithm,'chunk_count':len(plan.chunks),'settings':plan.settings.model_dump(),
            'effective_chunk_limit':plan.effective_chunk_limit,'context_window':plan.context_window,
            'safety_ratio':plan.safety_ratio,'chunks':rows,
            'notice':'token 为保守估计，不是模型精确计数；重叠只作上下文，核心文本才是本块新增分析对象。'}
