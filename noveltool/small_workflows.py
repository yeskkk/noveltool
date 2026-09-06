"""Compose tiny, plain-language questions into existing strict domain objects."""
from __future__ import annotations
from dataclasses import dataclass
from itertools import combinations
import re
from uuid import uuid4

from .small_model import StepRunner, Decoded, decode_names, decode_text, compact
from .llm import LLMError
from .llm_schemas import (EntityFinding,FactFinding,EventFinding,RelationshipFinding,
                          ThreadFinding,NarrativeFinding)

NAMES_QUESTION='只列出核心片段中确实出现的至多 {n} 个有名字的人物、地点、组织或物品。一行一个：名字 | 类别。不需要解释，也不必JSON；没有就写 无。'


def split_source(text: str, maximum: int):
    """Lossless natural-boundary slices. Positions refer to the original string."""
    at=0
    while at<len(text):
        end=min(len(text),at+maximum)
        if end<len(text):
            breaks=[m.end()+at for m in re.finditer(r'[。！？.!?\n]',text[at:end]) if m.end()>=maximum//2]
            if breaks:end=breaks[-1]
        yield at,end,text[at:end]
        at=end


@dataclass
class SmallAnalysis:
    records: list[dict]
    run_ids: tuple[str,...]
    quality: dict
    repairs: tuple[str,...] = ('source_scope_provenance',)
    requires_review: bool = True


async def analyze_small(session, client, package, run_id: str, *, owner_type: str = "analysis") -> SmallAnalysis:
    cfg=package.config
    async def preflight():
        if owner_type != 'analysis':
            return
        async with session.lock:
            if session.jobs.pause_requested and session.jobs.busy:
                raise LLMError('paused','当前小问题已保存，暂停后续小步骤；可继续并复用成功检查点')
            from .analysis_jobs import model_signature
            if model_signature(session.project.data.config) != model_signature(cfg):
                raise LLMError('stale_input','模型配置已改变，保留已完成小问题，停止发送旧配置请求')
            if session.manuscript.revision_no!=package.plan.base_revision_no:
                raise LLMError('stale_input','正文已改变，停止发送旧输入；已完成小步骤仍保留')
    runner=StepRunner(session,client,owner_type,run_id,output_tokens=package.output_tokens,preflight=preflight)
    records=[];extra_warnings=[]
    models={'entity':EntityFinding,'fact':FactFinding,'event':EventFinding,
            'relationship':RelationshipFinding,'thread':ThreadFinding,'narrative':NarrativeFinding}
    def add(kind,payload,part,text):
        quote=text.strip()
        if not quote:return
        # This is a source scope, not a fabricated verbatim quote attributed to the model.
        model=models[kind].model_validate({**payload,'evidence':[{'block':'B001','quote':quote}]},strict=True)
        records.append({'id':uuid4().hex,'kind':kind,'payload':model.model_dump(exclude={'evidence'}),
            'evidence':[{**part,'quote':quote}],'status':'pending'})
    overlap=''.join(package.blocks[r] for r in package.blocks if r not in package.core_refs)[-160:]
    previous=overlap
    for ref in package.blocks:
        if ref not in package.core_refs:continue
        original=package.refs[ref];whole=package.blocks[ref]
        for at,end,text in split_source(whole,cfg.small_source_chars):
            part={**original,'start_cp':original['start_cp']+at,'end_cp':original['start_cp']+end}
            if not text.strip():previous=(previous+text)[-160:];continue
            key=f'{ref}:{at}:{end}'
            source=f'【前文，仅帮助理解】\n{previous}\n【核心片段，仅分析这里】\n{text}'
            scope=compact(part)
            async def ask(suffix,question,decoder=decode_text,kind='text'):
                return await runner.ask(key+':'+suffix,question,source,decoder=decoder,kind=kind,cache_scope=scope,protected_names=[e['name'] for e in names])
            names=[]
            if package.pass_type in {'facts','links'}:
                decoded=await ask('names',NAMES_QUESTION.format(n=cfg.small_max_entities),
                    lambda raw,**kw:decode_names(raw,source=text,maximum=cfg.small_max_entities,**kw),'names')
                names=decoded.value or []
                for entity in names:add('entity',entity,part,text)
            if package.pass_type=='facts':
                for entity in names:
                    name=entity['name']
                    d=await ask('identity:'+name,f'核心片段明确写出了“{name}”的什么身份、职业或固有特征？只回答一句不超过80字的中文；未写明就写 无，不推测。')
                    if d.value:add('fact',{'subject':name,'field':'身份或特征','value':d.value,'mode':'stable'},part,text)
                    if entity['kind']=='character':
                        d=await ask('state:'+name,f'只用一句不超过80字的中文说明“{name}”在核心片段里此刻的行动、处境或状态。没有明确描述就写 无。')
                        if d.value:add('fact',{'subject':name,'field':'片段状态','value':d.value,'mode':'state'},part,text)
                d=await ask('event','核心片段实际发生了什么？只概括一个主要事件，一句不超过100字。不要把计划、梦境或人物声称当事实；没有就写 无。')
                if d.value:add('event',{'summary':d.value,'participants':[e['name'] for e in names if e['name'] in d.value],'story_time':None},part,text)
            elif package.pass_type=='links':
                for a,b in list(combinations(names,2))[:3]:
                    d=await ask('rel:'+a['name']+':'+b['name'],f'核心片段明确显示“{a["name"]}”与“{b["name"]}”的什么关系？只用一句不超过80字的中文描述；未写明就写 无。')
                    if d.value:add('relationship',{'a':a['name'],'b':b['name'],'label':'片段关系','description':d.value},part,text)
                d=await ask('thread','核心片段直接提出了哪个尚未解答的问题？只写一个，不超过80字。不要自行设计新伏笔；没有就写 无。')
                if d.value:add('thread',{'title':d.value[:70],'description':d.value,'status':'uncertain',
                     'related_entities':[e['name'] for e in names if e['name'] in d.value]},part,text)
            else:
                for kind,question in (
                    ('summary','只用一句不超过100字的中文概括核心片段。'),
                    ('pov','核心片段从谁的视角叙述？只用一句中文说明；无法确定就写 无。'),
                    ('style','核心片段最明显的一项语言风格是什么？只用一句不超过60字的中文说明，不需要举例。')):
                    d=await ask(kind,question)
                    if d.value:add('narrative',{'kind':kind,'text':d.value},part,text)
            previous=(previous+text)[-160:]
    quality=runner.quality()
    quality['scope_provenance']=True
    quality['sampling_limits']={'source_chars':cfg.small_source_chars,'entities_per_slice':cfg.small_max_entities,
                                'relations_per_slice':3,'threads_per_slice':1}
    return SmallAnalysis(records,runner.run_ids,quality)


@dataclass
class SmallIdea:
    value: object
    run_ids: tuple[str,...]
    quality: dict
    repairs: tuple[str,...] = ('small_question_composition',)
    requires_review: bool = True


async def expand_small(session,client,idea_text: str,proposal_id: str,output_tokens: int,
                       progress=None) -> SmallIdea:
    # Runtime imports keep the service graph free of circular imports at startup.
    from .ideas import IdeaResult, validate_seed
    runner=StepRunner(session,client,'idea',proposal_id,output_tokens=output_tokens)
    cfg=client.config
    draft={'premise_summary':idea_text if len(idea_text)<=8000 else '原始创意保存在本提案中，请核对并补充摘要。',
           'entities':[],'relationships':[],'threads':[],'style_notes':[]}
    async def publish():
        value=IdeaResult.model_validate(draft,strict=True);validate_seed(value)
        if progress:await progress(value,runner.quality())
    names=[]
    async def ask(key,question,source=None,decoder=decode_text,kind='text'):
        return await runner.ask(key,question,source if source is not None else idea_text,decoder=decoder,kind=kind,protected_names=[e['name'] for e in names])
    d=await ask('premise','这是小说创意，不是原文抽取。请根据资料构思一句中文故事前提，不超过150字。尊重用户明确限制，未定内容可提出建议，不要写已经发生的事件。')
    if d.value:draft['premise_summary']=d.value
    await publish()
    d=await ask('names',f'这是小说创意。只建议至多{cfg.small_max_entities}个核心人物或地点的名字。一行一个：名字 | 人物/地点。可以创造，不要解释，不必JSON。',
                decoder=lambda raw,**kw:decode_names(raw,maximum=cfg.small_max_entities,**kw),kind='names')
    names=d.value or []
    for entity in names:
        draft['entities'].append({**entity,'aliases':[],'facts':[]})
    await publish()
    for entity in draft['entities']:
        source=idea_text+'\n本次设计的实体：'+entity['name']
        for key,question in [('身份','只为这个实体建议一句中文初始身份或设定描述，不超过80字。'),
                             ('初始目标','只为这个实体建议一句中文初始目标或故事作用，不超过80字。')]:
            d=await ask(entity['name']+':'+key,question+'这是构思，不能宣称剧情已经发生。',source)
            if d.value:entity['facts'].append({'field':key,'value':d.value})
            await publish()
    for a,b in list(combinations(names,2))[:3]:
        d=await ask('relationship:'+a['name']+':'+b['name'],f'这是构思。只建议“{a["name"]}”与“{b["name"]}”在故事开始前的一种关系，一句中文，不超过80字。无需关系时写 无。')
        if d.value:draft['relationships'].append({'a':a['name'],'b':b['name'],'label':'初始关系','description':d.value})
        await publish()
    d=await ask('thread','这是构思。只建议一个可能推动故事的未解问题，一句中文，不超过80字。不宣称答案或事件已经发生。')
    if d.value:draft['threads'].append({'title':d.value[:70],'description':d.value,
                                      'related_entities':[e['name'] for e in names if e['name'] in d.value]})
    await publish()
    d=await ask('style','这是构思。只建议一种适合本故事的中文叙事风格，一句话，不超过60字。')
    if d.value:draft['style_notes'].append(d.value)
    await publish()
    value=IdeaResult.model_validate(draft,strict=True);validate_seed(value)
    return SmallIdea(value,runner.run_ids,runner.quality())
