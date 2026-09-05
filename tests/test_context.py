import asyncio
import copy
import json
import pytest
from noveltool.context import ContextRequest,build_package,match_name
from noveltool.state import project_at,reduce_fields
from noveltool.domain import ProjectConfig
from noveltool.llm import LLMError,request_token_estimate
from noveltool.manuscript import ManuscriptError,RevisionConflictError
from noveltool.runtime import ProjectSession
from noveltool.db import ProjectStore
from noveltool.settings import Profile
from test_settings import add_person,entry

PID='0'*32

def record(kind,p,at,i):
    return {'id':f'{i:032x}','kind':kind,'payload':p,'at_cp':at,'evidence':[],
            'status':'pending','requires_review':False,'chunk_ordinal':0,'run_id':'1'*32}


def test_filter_before_merge_avoids_future_stable_fact_conflict():
    rows=[record('entity',{'name':'林','kind':'character'},5,1),
      record('fact',{'subject':'林','field':'职业','value':'记者','mode':'stable'},5,2),
      record('fact',{'subject':'林','field':'职业','value':'SECRET','mode':'stable'},50,3)]
    early=project_at(PID,rows,[],[],10)
    assert early['entities'][0]['effective_fields'][0]['value']=='记者'
    assert 'SECRET' not in json.dumps(early,ensure_ascii=False)
    later=project_at(PID,rows,[],[],50)
    assert later['entities'][0]['effective_fields'][0]['status']=='conflict'
    assert project_at(PID,rows,[],[],0)['entities']==[]


def test_future_thread_resolution_and_relationship_not_applied_early():
    rows=[record('entity',{'name':'林','kind':'character'},1,1),record('entity',{'name':'周','kind':'character'},1,2)]
    for at,status,i in [(2,'open',3),(9,'resolved',4)]:
        rows.append(record('thread',{'title':'谜','description':'问题','status':status,'related_entities':['林']},at,i))
    for at,description,i in [(2,'友好',5),(9,'敌对',6)]:
        rows.append(record('relationship',{'a':'林','b':'周','label':'态度','description':description},at,i))
    early=project_at(PID,rows,[],[],5);late=project_at(PID,rows,[],[],10)
    assert early['threads'][0]['status']=='open' and late['threads'][0]['status']=='resolved'
    assert early['relationships'][0]['description']=='友好' and late['relationships'][0]['description']=='敌对'


def stateitem(value,at,i,op='set',source='auto',field='位置'):
    return {'id':f'{i:032x}','at_cp':at,'value':value,'field':field,'operation':op,'source':source}


def test_reducer_same_position_not_random_id_and_later_set_resolves():
    e={'facts':[],'states':[stateitem('家',1,1),stateitem('法院',2,2),stateitem('医院',2,3)]}
    assert reduce_fields(e)[0]['status']=='conflict'
    e['states'].reverse();assert reduce_fields(e)[0]['status']=='conflict'
    e['states'].append(stateitem('学校',2,4,source='manual'))
    assert reduce_fields(e)[0]['value']=='学校'
    e['states'].append(stateitem('办公室',3,5))
    assert reduce_fields(e)[0]['value']=='办公室'


def test_reducer_collection_partial_remove_and_conflict():
    e={'facts':[],'states':[stateitem('钥匙',1,1,'add'),stateitem('信件',2,2,'add')]}
    f=reduce_fields(e)[0];assert f['status']=='partial' and set(f['value'])=={'钥匙','信件'}
    e['states'].append(stateitem('钥匙',3,3,'remove'));assert reduce_fields(e)[0]['value']==['信件']
    e['states'].extend([stateitem('证据',4,4,'add'),stateitem('证据',4,5,'remove')])
    assert reduce_fields(e)[0]['status']=='conflict'
    e['states'].append(stateitem('空信封',5,6));assert reduce_fields(e)[0]['value']=='空信封'


def test_manual_fact_anchor_after_older_state_not_overwritten(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            await s.import_manuscript('甲乙丙丁戊己庚辛壬癸',0);eid=await add_person(s)
            await entry(s,'state',{'entity_id':eid,'field':'身份','value':'学徒'},2)
            await entry(s,'fact',{'entity_id':eid,'field':'身份','value':'师傅'},8)
            early=await s.state_reducer.view(5);late=await s.state_reducer.view(9)
            assert early['entities'][0]['effective_fields'][0]['value']=='学徒'
            assert late['entities'][0]['effective_fields'][0]['value']=='师傅'
            await entry(s,'state',{'entity_id':eid,'field':'身份','value':'隐退'},10)
            assert (await s.state_reducer.view(10))['entities'][0]['effective_fields'][0]['value']=='隐退'
        finally:await s.close()
    asyncio.run(scenario())


async def body(s,**kwargs):
    v=await s.knowledge.view()
    return ContextRequest(expected_revision_no=s.manuscript.revision_no,expected_version=v['version'],**kwargs)


def test_context_exact_budget_pinned_and_no_calls(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            await s.import_manuscript('林的正文。'*1200,0);eid=await add_person(s)
            await entry(s,'fact',{'entity_id':eid,'field':'职业','value':'记者'})
            for i in range(12):await entry(s,'note',{'kind':'style','text':f'风格{i}：'+'描写细节。'*300})
            p=await s.context.preview(await body(s,instruction='让林去法院',pinned_entities=[eid]))
            assert request_token_estimate(p['messages'])==p['input_token_estimate']
            assert p['input_token_estimate']+p['output_token_reserve']<=p['safe_budget']
            assert p['dropped_sections']
            assert f'entity:{eid}' in p['included_sections']
            prompt=json.dumps(p['messages'],ensure_ascii=False)
            assert '记者' in prompt and eid not in prompt
            assert not s._pending_llm_runs and s.manuscript.revision_no==1
            again=await s.context.preview(await body(s,instruction='让林去法院',pinned_entities=[eid]))
            assert p['fingerprint']==again['fingerprint']
        finally:await s.close()
    asyncio.run(scenario())


def test_context_rewrite_filters_future_manual_fact_and_preserves_target(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            text='林今天出门。\n\n他路过小巷。\n\n后来走到河边。'
            await s.import_manuscript(text,0);eid=await add_person(s)
            await entry(s,'fact',{'entity_id':eid,'field':'秘密','value':'只在结尾揭示的身份'},len(text))
            b=await body(s,task_type='rewrite',start_cp=3,end_cp=8,instruction='更简洁')
            p=await s.context.preview(b);prompt=json.dumps(p['messages'],ensure_ascii=False)
            assert '只在结尾揭示的身份' not in prompt
            assert next(x for x in p['sections'] if x['key']=='target')['text']==text[3:8]
            assert any('不是此刻' in x['label'] for x in p['sections'] if 'after' in x['key'])
            await s.append_manuscript('新正文',1)
            with pytest.raises(RevisionConflictError):await s.context.preview(b)
        finally:await s.close()
    asyncio.run(scenario())


def test_required_oversize_rejected_and_stale_anchors_excluded(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            await s.import_manuscript('旧'*10000,0);eid=await add_person(s)
            await entry(s,'state',{'entity_id':eid,'field':'位置','value':'旧法院'},5)
            with pytest.raises(LLMError,match='必需'):
                await s.context.preview(await body(s,task_type='rewrite',start_cp=0,end_cp=10000))
            await s.replace_manuscript(0,1,'新',1)
            state=await s.state_reducer.view()
            assert not state['entities'][0]['effective_fields']
            assert any('失效' in x for x in state['warnings'])
            with pytest.raises(ManuscriptError):await s.state_reducer.view(10001)
            with pytest.raises(ManuscriptError):await s.context.preview(await body(s,pinned_entities=['a'*32]))
        finally:await s.close()
    asyncio.run(scenario())


def test_empty_context_and_latin_name_boundary(project_path):
    assert match_name('K','K stood up') and not match_name('K','King stood up')
    assert match_name('林','小林出门')
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            p=await s.context.preview(await body(s,instruction='开始第一段'))
            assert p['start_cp']==p['end_cp']==0 and not p['sections']
            assert p['input_token_estimate']+p['output_token_reserve']<=p['safe_budget']
        finally:await s.close()
    asyncio.run(scenario())


def test_context_routes_readonly_and_validation(client,write_headers):
    assert client.get('/context').status_code==200 and client.get('/static/context.js').status_code==200
    v=client.get('/api/settings').json()['version']
    b={'expected_revision_no':0,'expected_version':v,'instruction':'新故事'}
    assert client.post('/api/context/preview',json=b).status_code==403
    p=client.post('/api/context/preview',headers=write_headers,json=b)
    assert p.status_code==200,p.text
    assert client.get('/api/state').json()['at_cp']==0
    for extra in [{'task_type':'rewrite','start_cp':0,'end_cp':0},{'start_cp':0},{'min_chars':999,'max_chars':1}]:
        assert client.post('/api/context/preview',headers=write_headers,json={**b,**extra}).status_code==422
    assert client.get('/api/state?at_cp=1').status_code==422
    assert not client.get('/api/llm/runs').json()['runs']
