"""Deterministic language/rendering tests; no assertion of semantic translation quality."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from noveltool.app import create_app
from noveltool.db import ProjectStore
from noveltool.domain import ProjectConfig
from noveltool.runtime import ProjectSession
from noveltool.language import ChineseRenderer,needs_chinese,split_segments,identity_check,for_id
from noveltool.llm import LLMClient,LLMError
from noveltool.small_model import StepRunner
from noveltool.ideas import IdeaRequest
from noveltool.generation import GenerationRequest
from test_context import body
from test_generation import finished
from test_small_model import SOURCE,ready,responder
from test_llm import envelope


@pytest.mark.parametrize('text,names,expected',[
    ('林在法院门口停下。',[],False),
    ('这是 John Smith 的笔记。',['John Smith'],False),
    ('He waited outside the courtroom.',[],True),
    ('林等待着。Then he started walking towards the silent door.',[],True),
    ('John Smith',['John Smith'],False),
    ('journalist',[],False),
])
def test_heuristic_not_a_general_language_detector(text,names,expected):
    assert needs_chinese(text,names)==expected
    assert needs_chinese('journalist',short_answer=True)


def test_segments_preserve_exact_unicode_and_decimal_numbers():
    text=('  林说。He spent 3.50 dollars.\n\n'+'English words '*60+'\n')*3
    parts=list(split_segments(text))
    assert ''.join(parts)==text
    assert all(len(p)<=360 for p in parts)
    assert '3.50' in parts[1]
    assert identity_check('林 waited 12 days.','他等了十三天。',['林'])
    assert not identity_check('林 waited 12 days.','林等了12天。',['林'])


async def render_with(s,text,answers,**kwargs):
    calls=[]
    def handler(req):
        q=json.loads(req.content);calls.append(q)
        answer=answers[min(len(calls)-1,len(answers)-1)]
        if isinstance(answer,Exception):raise answer
        return httpx.Response(200,json=answer if isinstance(answer,dict) else envelope(answer))
    async with LLMClient(s.project.data.config,on_run=s.record_llm_run,transport=httpx.MockTransport(handler)) as c:
        result=await ChineseRenderer(s,c,'diagnostic','sample').render(text,**kwargs)
    return result,calls


def test_success_preserves_original_even_without_debug_logs_and_caches(project_path):
    async def work():
        s=await ready(project_path,retain_llm_logs=False)
        original='  林 waited outside for 12 hours.\n\n'
        try:
            result,calls=await render_with(s,original,['林在外面等了12小时。'],protected_names=['林'])
            assert result.complete and result.changed and len(calls)==1
            assert result.text=='  林在外面等了12小时。\n\n'
            stored=for_id(s,result.id)
            assert stored['original_text']==original and stored['final_text']==result.text
            logs=[dict(r) for r in s.store.connection.execute('SELECT * FROM llm_runs')]
            assert logs and all(not row['raw_response'] for row in logs)
            again,no_calls=await render_with(s,original,['不应调用。'],protected_names=['林'])
            assert again.id==result.id and again.text==result.text and not no_calls
            assert s.manuscript.revision_no==0
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:assert for_id(s,result.id)['original_text']==original
        finally:await s.close()
    asyncio.run(work())


@pytest.mark.parametrize('answer',[
    'He waited outside the courtroom for 12 hours.',
    '他在法院门口等了12小时。', # lost protected name
    '林在法院门口等了13小时。', # changed number
    {'choices':[{'message':{'content':'林在法院门口等了12'},'finish_reason':'length'}]},
    '```json\n{"text":"林在法院门口',
    httpx.ConnectError('offline'),
])
def test_failed_conversion_keeps_whole_original_segment(project_path,answer):
    async def work():
        s=await ready(project_path)
        original='林 waited outside the courtroom for 12 hours.'
        try:
            result,calls=await render_with(s,original,[answer],protected_names=['林'])
            assert not result.complete and not result.changed and result.text==original
            assert len(calls)==1 and result.warnings
            assert for_id(s,result.id)['status']=='partial'
        finally:await s.close()
    asyncio.run(work())


def test_mixed_long_chinese_english_sentence_and_partial_resume(project_path):
    async def work():
        s=await ready(project_path)
        original='林在门口等待。'*100+'He waited for a long time. He found the door locked.\n'
        try:
            result,calls=await render_with(s,original,['他等了很久。','He found the door locked.'])
            assert not result.complete and len(calls)==2
            assert result.text.startswith('林在门口等待。'*100+'他等了很久。 ')
            assert result.text.endswith('He found the door locked.\n')
            second,calls=await render_with(s,original,['他发现门锁着。'])
            assert second.complete and len(calls)==1 # first translated segment was a checkpoint
            assert second.text.endswith('他等了很久。 他发现门锁着。\n')
        finally:await s.close()
    asyncio.run(work())


def test_disabled_normalization_sends_no_request_and_persists(project_path):
    async def work():
        s=await ready(project_path,auto_chinese=False)
        try:
            original='He waited outside the courtroom.'
            result,calls=await render_with(s,original,['不应发送'])
            assert result.text==original and result.id is None and not calls
            await s.save()
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:assert s.project.data.config.auto_chinese is False
        finally:await s.close()
    asyncio.run(work())


def test_small_analysis_translates_descriptions_not_source_or_names(project_path):
    async def work():
        s=await ready(project_path,SOURCE);calls=[]
        def handler(req):
            q=json.loads(req.content);calls.append(q)
            prompt=q['messages'][-1]['content']
            if '【待翻译的小段】' in prompt:return httpx.Response(200,json=envelope('记者。'))
            if '一行一个' in prompt:return httpx.Response(200,json=envelope('林 | 人物'))
            if '什么身份' in prompt:return httpx.Response(200,json=envelope('journalist'))
            return httpx.Response(200,json=envelope('林来到法院。'))
        try:
            r=await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=httpx.MockTransport(handler))
            assert r['quality']['complete'] and r['requires_review']
            assert len(calls)==5 # names, identity, translate, state, event
            facts=[o for o in r['observations'] if o['kind']=='fact']
            assert any(o['payload']['value']=='记者。' for o in facts)
            assert all(o['evidence'][0]['quote']==SOURCE for o in r['observations'])
            translations=[step['language'] for step in r['steps'] if step['language']]
            assert translations[0]['original_text']=='journalist'
            assert not (await s.knowledge.view())['entities']
            again=await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=httpx.MockTransport(lambda req:(_ for _ in ()).throw(AssertionError('not cached'))))
            assert again['quality']['reused_steps']==4
            assert any(step['language'] for step in again['steps'])
        finally:await s.close()
    asyncio.run(work())


def test_language_toggle_changes_checkpoint_fingerprint(project_path):
    async def work():
        s=await ready(project_path);calls=[]
        transport=httpx.MockTransport(lambda r:(calls.append(r),httpx.Response(200,json=envelope('这里是中文。')))[1])
        try:
            async with LLMClient(s.project.data.config,on_run=s.record_llm_run,transport=transport) as c:
                await StepRunner(s,c,'analysis','x',output_tokens=512).ask('x','说明这里','原文')
            await s.update_config(s.project.data.config.model_copy(update={'auto_chinese':False}),s.project.data.meta.data_version)
            async with LLMClient(s.project.data.config,on_run=s.record_llm_run,transport=transport) as c:
                await StepRunner(s,c,'analysis','y',output_tokens=512).ask('x','说明这里','原文')
            assert len(calls)==2
        finally:await s.close()
    asyncio.run(work())


def test_idea_short_plain_english_becomes_editable_chinese(project_path):
    async def work():
        s=await ready(project_path);calls=[]
        def handler(req):
            q=json.loads(req.content);calls.append(q);p=q['messages'][-1]['content']
            if '【待翻译的小段】' in p:return httpx.Response(200,json=envelope('克制的叙事风格。'))
            if '一行一个' in p:answer='林 | 人物'
            elif '叙事风格' in p:answer='A restrained narrative voice.'
            else:answer='林在小城查案。'
            return httpx.Response(200,json=envelope(answer))
        try:
            r=await s.ideas.generate(IdeaRequest(idea_text='小城记者林。',expected_version=(await s.knowledge.view())['version']),transport=httpx.MockTransport(handler))
            assert r['quality']['complete']
            assert r['draft']['style_notes']==['克制的叙事风格。']
            assert any(x['language'] for x in r['steps'])
            assert not (await s.knowledge.view())['entities'] and s.manuscript.revision_no==0
        finally:await s.close()
    asyncio.run(work())


@pytest.mark.parametrize('translation,complete', [('他在法院外面等候。',True),('He waited outside the courthouse.',False)])
def test_writer_keeps_original_and_recounts_after_rendering(project_path,translation,complete):
    async def work():
        s=await ready(project_path,writer_model='writer',retain_llm_logs=False);calls=[]
        original='He waited outside the courthouse.'
        def handler(req):
            q=json.loads(req.content);calls.append(q)
            answer=translation if '【待翻译的小段】' in q['messages'][-1]['content'] else original
            return httpx.Response(200,json=envelope(answer))
        try:
            request=GenerationRequest(context=await body(s,min_chars=2,max_chars=20),candidate_count=1)
            t=await s.generation.start(request,transport=httpx.MockTransport(handler))
            result=await finished(s,t['id']);candidate=result['slots'][0]['candidate']
            assert result['status']=='ready' and candidate
            assert candidate['text']==(translation if complete else original)
            assert candidate['status']==('complete' if complete else 'length_mismatch')
            assert candidate['language']['original_text']==original
            assert candidate['language']['status']==('complete' if complete else 'partial')
            assert len(calls)==2 and calls[0]['model']=='writer' and calls[1]['model']=='weak'
            assert s.manuscript.text==''
            # Repeated hot reads still don't repeatedly load translation history from SQLite.
            sql=[];s.store.connection.set_trace_callback(sql.append)
            await s.generation.view(t['id']);await s.generation.view(t['id'])
            assert not sql;s.store.connection.set_trace_callback(None)
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:assert (await s.generation.view(t['id']))['slots'][0]['candidate']['language']['original_text']==original
        finally:await s.close()
    asyncio.run(work())


def test_reasoning_only_stop_is_empty_not_fake_visible_content(project_path):
    async def work():
        s=await ready(project_path)
        payload={'choices':[{'message':{'content':None,'reasoning_content':'I thought about the question.'},'finish_reason':'stop'}],
                 'usage':{'completion_tokens':1024,'completion_tokens_details':{'reasoning_tokens':1024}}}
        try:
            async with LLMClient(s.project.data.config,on_run=s.record_llm_run,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload))) as c:
                with pytest.raises(LLMError) as err:await c.complete(messages=[{'role':'user','content':'你好'}],model='weak')
            assert err.value.code=='empty' and err.value.usage['completion_tokens']==1024
            assert not err.value.partial_text
        finally:await s.close()
    asyncio.run(work())


def test_bad_translation_is_pending_not_complete_source_coverage(project_path):
    async def work():
        s=await ready(project_path,SOURCE)
        def handler(req):
            p=json.loads(req.content)['messages'][-1]['content']
            if '【待翻译的小段】' in p:answer='Still a journalist.'
            elif '一行一个' in p:answer='林 | 人物'
            elif '什么身份' in p:answer='journalist'
            else:answer='林来到法院。'
            return httpx.Response(200,json=envelope(answer))
        try:
            r=await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=httpx.MockTransport(handler))
            assert r['status']=='done' and not r['quality']['complete']
            assert len(r['observations'])==4 # untranslated fact still available for manual editing/rejection
            assert any(o['payload'].get('value')=='journalist' for o in r['observations'])
            assert (await s.semantic.status())['coverage']['facts']==0
            assert any(x['language'] and x['language']['status']=='partial' for x in r['steps'])
        finally:await s.close()
    asyncio.run(work())


def test_partial_step_original_trace_visible_after_cancellation(project_path):
    async def work():
        s=await ready(project_path);started=asyncio.Event()
        async def handler(req):
            started.set();await asyncio.Event().wait()
        try:
            async with LLMClient(s.project.data.config,on_run=s.record_llm_run,transport=httpx.MockTransport(handler)) as c:
                task=asyncio.create_task(ChineseRenderer(s,c,'generation','cancelled-attempt').render('He waited outside the courthouse.'))
                await started.wait();task.cancel()
                with pytest.raises(asyncio.CancelledError):await task
            row=s.store.connection.execute("SELECT * FROM language_outputs WHERE owner_id='cancelled-attempt'").fetchone()
            assert row['status']=='interrupted' and row['final_text']==row['original_text']
        finally:await s.close()
    asyncio.run(work())


def test_http_config_and_candidate_language_controls_are_packaged(project_path):
    with TestClient(create_app(project_path),base_url='http://127.0.0.1') as c:
        config=c.get('/api/config').json()['config']
        assert config['auto_chinese'] is True and config['analysis_protocol']=='small'
        assert 'name="auto_chinese"' in c.get('/').text
        js=c.get('/static/generation.js').text
        assert '转换前文本采用到草稿' in js and 'language.original_text' in js
        assert c.get('/generation').status_code==200
