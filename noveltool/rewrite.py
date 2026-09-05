"""Stable, validated selection snapshots for AI rewrite tasks.

Offsets are Unicode code points, end-exclusive. UI line numbers never become
persistent identities. A snapshot binds revision, immutable source slices,
verbatim selected text and hash. Verification is repeated at commit.
"""
from __future__ import annotations
from pydantic import ConfigDict, Field, model_validator
from .settings import Strict, ID
from .chunker import Slice
from .manuscript import ManuscriptError, text_hash, validate_range

class RewriteTarget(Strict):
    model_config=ConfigDict(strict=True,extra='forbid',frozen=True,str_strip_whitespace=False)
    base_revision_no:int=Field(ge=1)
    start_cp:int=Field(ge=0)
    end_cp:int=Field(ge=1)
    selected_text:str=Field(min_length=1,max_length=1_000_000)
    selected_hash:str=Field(pattern=r'^[0-9a-f]{64}$')
    slices:list[Slice]=Field(min_length=1)

    @model_validator(mode='after')
    def consistent(self):
        if (self.end_cp-self.start_cp!=len(self.selected_text)
            or self.selected_hash!=text_hash(self.selected_text)
            or sum(p.end_cp-p.start_cp for p in self.slices)!=len(self.selected_text)):
            raise ValueError('返修选区长度、切片或哈希不一致')
        return self


def capture_target(doc,start:int,end:int) -> RewriteTarget:
    validate_range(doc.text,start,end)
    if start==end:raise ManuscriptError('AI 返修需要非空选区；删除全文请使用手工编辑')
    spans=doc.render().spans
    slices=[Slice(block_id=sp.block_id,start_cp=max(start,sp.start_cp)-sp.start_cp,
                  end_cp=min(end,sp.end_cp)-sp.start_cp)
            for sp in spans if start<sp.end_cp and sp.start_cp<end]
    selected=doc.text[start:end]
    return RewriteTarget(base_revision_no=doc.revision_no,start_cp=start,end_cp=end,
                         selected_text=selected,selected_hash=text_hash(selected),slices=slices)


def verify_target(target:RewriteTarget,doc) -> None:
    doc.check_revision(target.base_revision_no)
    if capture_target(doc,target.start_cp,target.end_cp)!=target:
        raise ManuscriptError('返修原文或块引用与任务快照不一致；未修改正文')
