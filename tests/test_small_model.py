"""Weak-output regressions. These mocks intentionally DO NOT implement JSON Schema."""
import asyncio
import json
from collections import Counter

import httpx
import pytest

from noveltool.db import ProjectStore
from noveltool.domain import ProjectConfig
from noveltool.runtime import ProjectSession
from noveltool.chunker import PlanSettings
from noveltool.small_model import decode_names, decode_text, clean_answer, cut_container
from noveltool.small_workflows import split_source
from noveltool.ideas import IdeaRequest, IdeaResume, IdeaResult, ProposalAccept, ProposalDraft
from noveltool.settings import ReviewWrite
from noveltool.manuscript import ManuscriptError
from noveltool.llm import LLMError
from test_llm import envelope

SOURCE='林是一名记者。林来到法院。'


@pytest.mark.parametrize('raw',[
    '林 | 人物\n周 | 人物',
    '1. 林（人物）\n2. 周（人物）',
    '人物：林、周',
    '[{"name":"林","kind":"character"},{"name":"周","type":"人物"}]',
    "```json\n{'entities':[{'name':'林','type':'人物'}, {'name':'周','type':'人物'},],}\n```",
    '| 名称 | 类型 |\n|---|---|\n| 林 | 人物 |\n| 周 | 人物 |',
])
def test_names_accept_familiar_loose_forms(raw):
    d=decode_names(raw,source='林和周来到法院。')
    assert d.complete
    assert [x['name'] for x in d.value]==['林','周']


def test_cut_outer_json_only_recovers_complete_cards_and_no_fake_name():
    raw='{"entities":[{"name":"林","type":"人物"},{"name":"幽灵","type":"人物"},{"name":"周'
    d=decode_names(raw,source=SOURCE)
    assert d.value==[{'name':'林','kind':'character'}]
    assert not d.complete
    assert cut_container(raw)
    assert not cut_container('{"name":"{林}"}')


@pytest.mark.parametrize('raw,expected',[
    ('<think>Do not show this.</think>回答：他是一名记者。','他是一名记者。'),
    ('```json\n{"description":"他是一名记者。"}\n```','他是一名记者。'),
    ("{'回答':'他是一名记者。',}",'他是一名记者。'),
    ('<think>the answer is not finished',''),
    ('无。',''),
    ('{"value":"他是一名记者',''),
])
def test_plain_answers_and_thinking_do_not_need_a_schema(raw,expected):
    assert decode_text(raw).value==expected


def test_partial_plain_sentence_not_half_sentence_and_limit_enforced():
    d=decode_text('他是一名记者。随后他还没有说',truncated=True)
    assert d.value=='他是一名记者。' and not d.complete
    d=decode_text('字'*2000)
    assert d.value=='' and not d.complete
    assert not decode_text('字'*100).value is None


def test_split_source_covers_unicode_exactly_once():
    source=('林说：“😀这里很冷。”\n\n'+'周'*120)*8
    parts=list(split_source(source,100))
    assert ''.join(t for _,_,t in parts)==source
    assert all(len(t)<=100 and source[a:b]==t for a,b,t in parts)
    assert all(x[1]==y[0] for x,y in zip(parts,parts[1:]))


async def ready(path,text=None,**cfg):
    s=ProjectSession(ProjectStore.open(path))
    await s.update_config(ProjectConfig(analysis_model='weak',output_retry_limit=0,**cfg),s.project.data.meta.data_version)
    if text:
        await s.import_manuscript(text,0)
        await s.imports.create_plan(PlanSettings(),1)
    return s


def responder(overrides=None,calls=None):
    overrides=overrides or {}
    def handler(req):
        body=json.loads(req.content);prompt=body['messages'][-1]['content'];question=prompt.split('【本次只回答】\n')[-1]
        if calls is not None:calls.append(question)
        assert '$defs' not in prompt and 'evidence.block' not in prompt and 'B001' not in prompt
        for needle,response in overrides.items():
            if needle in question:
                if isinstance(response,Exception):raise response
                if callable(response):response=response(req)
                return httpx.Response(200,json=envelope(response))
        if '一行一个' in question:answer='林 | 人物'
        elif '未解' in question or '尚未解答' in question:answer='无。'
        elif '风格' in question:answer='克制，短句。'
        elif '视角' in question:answer='第三人称跟随林的视角。'
        elif '初始目标' in question:answer='查清失踪者的下落。'
        elif '身份' in question or '固有特征' in question:answer='记者。'
        elif '状态' in question:answer='林来到法院。'
        elif '前提' in question:answer='林在小城调查失踪案。'
        elif '关系' in question:answer='两人是旧友。'
        else:answer='林来到法院。'
        return httpx.Response(200,json=envelope(answer))
    return httpx.MockTransport(handler)


def test_small_analysis_uses_plain_questions_and_requires_human_review(project_path):
    async def work():
        s=await ready(project_path,SOURCE);calls=[]
        try:
            r=await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=responder(calls=calls))
            assert len(calls)==4
            assert r['status']=='done' and r['quality']['complete'] and r['requires_review']
            assert len(r['observations'])==4
            for o in r['observations']:
                assert o['evidence'][0]['quote']==SOURCE
                assert o['evidence'][0]['end_cp']-o['evidence'][0]['start_cp']==len(SOURCE)
            graph=await s.knowledge.view()
            assert not graph['entities'] and graph['review_queue']
            await s.settings.review(ReviewWrite(expected_version=graph['version'],observation_ids=[o['id'] for o in r['observations']],decision='accepted'))
            assert len((await s.knowledge.view())['entities'])==1
            assert s.manuscript.text==SOURCE
            assert all('"schema"' not in row['request_json'] for row in s.store.connection.execute('SELECT request_json FROM llm_runs'))
        finally:await s.close()
    asyncio.run(work())


def test_bad_one_answer_does_not_discard_others_and_retry_reuses_checkpoints(project_path):
    async def work():
        s=await ready(project_path,SOURCE);calls=[]
        try:
            plan=s.imports.last_plan
            r=await s.analysis.run_chunk(plan.id,0,1,transport=responder({'什么身份':'{"value":"半截'},calls))
            assert not r['quality']['complete'] and r['quality']['incomplete_steps']==1
            assert len(r['observations'])==3  # entity, state, event survived
            status=await s.semantic.status()
            assert status['partial_runs']==1 and status['coverage']['facts']==0
            assert any(step['raw_output']=='{"value":"半截' for step in r['steps'])
            calls.clear()
            fixed=await s.analysis.run_chunk(plan.id,0,1,transport=responder(calls=calls))
            assert len(calls)==1 and '什么身份' in calls[0]
            assert fixed['quality']['complete'] and fixed['quality']['reused_steps']==3
            assert (await s.semantic.status())['coverage']['facts']==len(SOURCE)
            assert s.manuscript.text==SOURCE
        finally:await s.close()
    asyncio.run(work())


def test_cut_name_list_keeps_complete_grounded_rows_and_continues(project_path):
    async def work():
        s=await ready(project_path,SOURCE)
        try:
            r=await s.analysis.run_chunk(s.imports.last_plan.id,0,1,
                transport=responder({'一行一个':'{"entities":[{"name":"林","kind":"character"},{"name":"周'}))
            assert not r['quality']['complete']
            assert any(o['kind']=='event' for o in r['observations'])
            assert [o['payload']['name'] for o in r['observations'] if o['kind']=='entity']==['林']
            assert (await s.semantic.status())['coverage']['facts']==0
        finally:await s.close()
    asyncio.run(work())


def test_source_change_does_not_reuse_wrong_micro_results(project_path):
    async def work():
        s=await ready(project_path,SOURCE);calls=[]
        try:
            await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=responder(calls=calls))
            await s.replace_manuscript(0,len(SOURCE),'林不是记者。',1)
            await s.imports.create_plan(PlanSettings(),2)
            calls.clear()
            changed=await s.analysis.run_chunk(s.imports.last_plan.id,0,2,transport=responder(calls=calls))
            assert changed['quality']['reused_steps']==0 and len(calls)==4
            assert all(o['evidence'][0]['quote']=='林不是记者。' for o in changed['observations'])
        finally:await s.close()
    asyncio.run(work())


async def make_idea(s,transport):
    return await s.ideas.generate(IdeaRequest(idea_text='主角是记者，不是警察；小城悬疑故事。',expected_version=(await s.knowledge.view())['version']),transport=transport)


def test_idea_plain_answers_build_editable_proposal_without_json(project_path):
    async def work():
        s=await ready(project_path);calls=[]
        try:
            p=await make_idea(s,responder(calls=calls))
            assert p['status']=='ready' and p['quality']['complete']
            assert p['draft']['entities'][0]['name']=='林'
            assert p['requires_review'] and not s.settings.entries
            assert len(calls)==6
            assert all('schema' not in c.lower() for c in calls)
            await s.ideas.accept(p['id'],ProposalAccept(expected_version=(await s.knowledge.view())['version'],
                expected_proposal_version=p['version'],draft=IdeaResult.model_validate(p['draft'])))
            assert s.manuscript.revision_no==0
            assert len((await s.knowledge.view())['entities'])==1
            assert not (await s.knowledge.view())['events']
        finally:await s.close()
    asyncio.run(work())


def test_partial_idea_can_be_edited_but_requires_explicit_acknowledgement(project_path):
    async def work():
        s=await ready(project_path)
        try:
            p=await make_idea(s,responder({'身份':'{"content":'}))
            assert p['draft'] and not p['quality']['complete']
            body=dict(expected_version=(await s.knowledge.view())['version'],expected_proposal_version=p['version'],draft=IdeaResult.model_validate(p['draft']))
            with pytest.raises(ManuscriptError,match='不完整'):await s.ideas.accept(p['id'],ProposalAccept(**body))
            assert not s.settings.entries
            await s.ideas.accept(p['id'],ProposalAccept(**body,acknowledge_incomplete=True))
            assert s.settings.entries
        finally:await s.close()
    asyncio.run(work())


def test_idea_network_stop_then_resume_skips_completed_answers(project_path):
    async def work():
        s=await ready(project_path);calls=[]
        try:
            with pytest.raises(LLMError):await make_idea(s,responder({'初始身份':httpx.ConnectError('offline')},calls))
            p=(await s.ideas.list())[0];p=await s.ideas.get(p['id'])
            assert p['status']=='failed' and p['draft'] and p['step_progress']['completed']==2
            calls.clear()
            resumed=await s.ideas.resume(p['id'],IdeaResume(expected_version=(await s.knowledge.view())['version'],expected_proposal_version=p['version']),transport=responder(calls=calls))
            assert resumed['quality']['complete'] and resumed['quality']['reused_steps']==2
            assert len(calls)==4 and all('一行一个' not in q for q in calls)
        finally:await s.close()
    asyncio.run(work())


def test_idea_resume_never_overwrites_human_edits(project_path):
    async def work():
        s=await ready(project_path)
        try:
            p=await make_idea(s,responder({'身份':'{"text":'}))
            d=p['draft'];d['premise_summary']='人工改写的前提。'
            p=await s.ideas.save_draft(p['id'],ProposalDraft(expected_proposal_version=p['version'],draft=IdeaResult.model_validate(d)))
            with pytest.raises(ManuscriptError,match='人工编辑'):
                await s.ideas.resume(p['id'],IdeaResume(expected_version=(await s.knowledge.view())['version'],expected_proposal_version=p['version']),transport=responder())
            assert (await s.ideas.get(p['id']))['draft']['premise_summary']=='人工改写的前提。'
        finally:await s.close()
    asyncio.run(work())


def test_json_string_array_prefix_is_not_lost_or_called_complete():
    d=decode_names('["林", "周", "半',source='林和周。')
    assert [r['name'] for r in d.value]==['林','周']
    assert not d.complete


def test_repeat_source_keeps_human_review_decisions(project_path):
    async def work():
        s=await ready(project_path,SOURCE)
        try:
            r=await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=responder())
            graph=await s.knowledge.view()
            await s.settings.review(ReviewWrite(expected_version=graph['version'],observation_ids=[o['id'] for o in r['observations']],decision='accepted'))
            again=await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=responder())
            assert all(o['status']=='accepted' for o in again['observations'])
            assert again['quality']['reused_steps']==4
            assert (await s.knowledge.view())['entities']
        finally:await s.close()
    asyncio.run(work())


def test_cooperative_pause_stops_at_micro_checkpoint_and_resume(project_path):
    async def work():
        s=await ready(project_path,SOURCE);calls=[]
        def pause(req):
            calls.append(1);s.jobs.pause_requested=True
            return httpx.Response(200,json=envelope('林 | 人物'))
        try:
            j=await s.jobs.start(s.imports.last_plan.id,1,['facts'],True,transport=httpx.MockTransport(pause))
            await s.jobs.task
            assert (await s.jobs.view(j['job']['id']))['job']['status']=='paused'
            assert len(calls)==1
            step=s.store.connection.execute("SELECT status FROM model_steps ORDER BY rowid LIMIT 1").fetchone()
            assert step['status']=='complete'
            calls.clear()
            j=await s.jobs.start(s.imports.last_plan.id,1,['facts'],True,transport=responder(calls=calls))
            await s.jobs.task
            assert (await s.jobs.view(j['job']['id']))['job']['status']=='done'
            assert len(calls)==3
        finally:await s.close()
    asyncio.run(work())


def test_small_default_http_diagnostic_and_project_settings(project_path):
    from fastapi.testclient import TestClient
    from noveltool.app import create_app
    with TestClient(create_app(project_path,llm_transport=responder()),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']}
        v=c.get('/api/config').json()
        assert v['config']['analysis_protocol']=='small'
        cfg={**v['config'],'analysis_model':'weak','small_output_tokens':2500,'output_retry_limit':0}
        assert c.put('/api/config',headers=h,json={'expected_memory_version':v['memory_version'],'config':cfg}).status_code==200
        r=c.post('/api/llm/structured-test',headers=h,json={'source':SOURCE})
        assert r.status_code==200,r.text
        assert r.json()['value']['quality']['complete']
        assert len(r.json()['value']['observations'])==4
        assert c.get('/api/manuscript').json()['revision_no']==0
        assert not c.get('/api/settings').json()['entities']
        runs=c.get('/api/llm/runs').json()['runs']
        assert all(r['requested_max_tokens']==2500 for r in runs)


def test_partial_names_do_not_collapse_ambiguous_types():
    d=decode_names('林 | 人物\n林 | 地点',source='林')
    assert len(d.value)==1 and not d.complete
