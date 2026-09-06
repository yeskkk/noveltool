import asyncio
import copy
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from noveltool.app import create_app
from noveltool.db import ProjectStore, SaveFailedError
from noveltool.domain import ProjectConfig
from noveltool.runtime import ProjectSession
from noveltool.ideas import IdeaRequest, IdeaResult, ProposalAccept, ProposalDraft, validate_seed
from noveltool.llm import LLMError
from noveltool.structured_llm import StructuredError
from noveltool.manuscript import ManuscriptError, RevisionConflictError
from test_llm import envelope
from test_llm_routes import configure
from test_settings import add_person, entry


def seed():
    return {'premise_summary':'九十年代小城里，一个记者调查失踪案。',
       'entities':[{'name':'林','kind':'character','aliases':['小林'],'facts':[{'field':'职业','value':'记者'}]},
                   {'name':'周','kind':'character','aliases':[],'facts':[]}],
       'relationships':[{'a':'林','b':'周','label':'朋友','description':'相识多年'}],
       'threads':[{'title':'谁留下信件','description':'可能由旧友寄出。','related_entities':['林']}],
       'style_notes':['克制，短句。']}


def transport(data=None):
    return httpx.MockTransport(lambda req:httpx.Response(200,json=envelope(json.dumps(seed() if data is None else data,ensure_ascii=False))))


async def ready(path):
    s=ProjectSession(ProjectStore.open(path))
    await s.update_config(ProjectConfig(analysis_protocol="strict", analysis_model='fake'),s.project.data.meta.data_version)
    return s

async def req(s,text='写一个侦探故事'):
    return IdeaRequest(idea_text=text,expected_version=(await s.knowledge.view())['version'])

async def accept(s,p,draft=None):
    return await s.ideas.accept(p['id'],ProposalAccept(expected_proposal_version=p['version'],
        expected_version=(await s.knowledge.view())['version'],draft=IdeaResult.model_validate(draft or p['draft'])))


def test_idea_is_proposal_until_explicit_atomic_accept_and_reopen(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            p=await s.ideas.generate(await req(s),transport=transport());pid=p['id']
            assert p['status']=='ready' and len(p['llm_run_ids'])==1
            assert not (await s.knowledge.view())['entities'] and s.manuscript.revision_no==0
            edited=copy.deepcopy(p['draft']);edited['entities'][0]['facts'][0]['value']='记者兼译者'
            p=await s.ideas.save_draft(pid,ProposalDraft(expected_proposal_version=1,draft=IdeaResult.model_validate(edited)))
            assert p['version']==2 and not s.settings.entries
            r=await accept(s,p);assert r['proposal']['status']=='accepted'
            v=r['settings'];assert len(v['entities'])==2 and not v['events']
            assert v['threads'][0]['status']=='uncertain'
            assert next(e for e in v['entities'] if e['name']=='林')['facts'][0]['values'][0]['value']=='记者兼译者'
            assert not s.manuscript.text and not any(e.kind in {'event','state'} for e in s.settings.entries.values())
            with pytest.raises(ManuscriptError):await accept(s,p)
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            assert (await s.ideas.get(pid))['draft']==edited
            assert len((await s.knowledge.view())['entities'])==2
        finally:await s.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mutate',[
    lambda d:d['relationships'][0].update(b='幽灵'),
    lambda d:d['relationships'][0].update(b='小林'),
    lambda d:d['entities'][1]['aliases'].append('林'),
    lambda d:d['entities'][0]['facts'].append({'field':'职业','value':'律师'}),
    lambda d:d['threads'][0]['related_entities'].append('幽灵'),
    lambda d:d.update(events=[{'summary':'案子已经破了'}]),
    lambda d:d.update(id='a'*32),
])
def test_invalid_seed_never_creates_settings(project_path,mutate):
    async def scenario():
        s=await ready(project_path)
        try:
            d=seed();mutate(d)
            with pytest.raises(StructuredError):await s.ideas.generate(await req(s),transport=transport(d))
            assert not s.settings.profiles and not s.settings.entries
            assert (await s.ideas.list())[0]['status']=='failed'
        finally:await s.close()
    asyncio.run(scenario())


def test_idea_stale_during_network_and_edit_version_guard(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            async def handler(request):
                await add_person(s,'新的人工人物')
                return httpx.Response(200,json=envelope(json.dumps(seed())))
            p=await s.ideas.generate(await req(s),transport=httpx.MockTransport(handler))
            assert p['stale'] and p['status']=='stale'
            with pytest.raises(RevisionConflictError):await accept(s,p)
            p2=await s.ideas.save_draft(p['id'],ProposalDraft(expected_proposal_version=p['version'],draft=IdeaResult.model_validate(p['draft'])))
            assert p2['version']==p['version']+1
            with pytest.raises(RevisionConflictError):await s.ideas.save_draft(p['id'],ProposalDraft(expected_proposal_version=p['version'],draft=IdeaResult.model_validate(p['draft'])))
        finally:await s.close()
    asyncio.run(scenario())


def test_existing_manual_conflict_does_not_partially_apply(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            eid=await add_person(s)
            await entry(s,'fact',{'entity_id':eid,'field':'职业','value':'银行职员'})
            p=await s.ideas.generate(await req(s),transport=transport())
            with pytest.raises(ManuscriptError,match='冲突'):await accept(s,p)
            assert len(s.settings.profiles)==1 and len(s.settings.entries)==1
            assert (await s.ideas.get(p['id']))['status']=='ready'
            p['draft']['entities'][0]['facts'][0]['value']='银行职员'
            r=await accept(s,p)
            assert len(r['settings']['entities'])==2
        finally:await s.close()
    asyncio.run(scenario())


def test_idea_accept_rollback_and_bad_disk_proposal(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            p=await s.ideas.generate(await req(s),transport=transport())
            version=(await s.knowledge.view())['version']
            s.store.connection.execute("CREATE TRIGGER fail_accept BEFORE INSERT ON setting_entries BEGIN SELECT RAISE(ABORT,'test'); END")
            with pytest.raises(SaveFailedError):await accept(s,p)
            assert not s.settings.profiles and not s.settings.entries
            assert s.store.connection.execute('SELECT count(*) FROM setting_entities').fetchone()[0]==0
            assert (await s.ideas.get(p['id']))['status']=='ready'
            assert (await s.knowledge.view())['version']==version
            s.store.connection.execute('DROP TRIGGER fail_accept')
            s.store.connection.execute("UPDATE idea_proposals SET draft_json='{}' WHERE id=?",(p['id'],))
            with pytest.raises(ManuscriptError,match='校验'):await s.ideas.get(p['id'])
        finally:await s.close()
    asyncio.run(scenario())


def test_idea_cancel_busy_budget_and_text_guard(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            request=await req(s)
            await s.model_gate.acquire()
            with pytest.raises(LLMError,match='其他任务'):await s.ideas.generate(request)
            s.model_gate.release()
            await s.update_config(ProjectConfig(analysis_protocol="strict", analysis_model='fake',context_window=2048),s.project.data.meta.data_version)
            with pytest.raises(LLMError,match='预算'):await s.ideas.generate(request)
            await s.update_config(ProjectConfig(analysis_protocol="strict", analysis_model='fake'),s.project.data.meta.data_version)
            started=asyncio.Event()
            async def block(r):started.set();await asyncio.Event().wait()
            task=asyncio.create_task(s.ideas.generate(await req(s),transport=httpx.MockTransport(block)))
            await started.wait();task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert (await s.ideas.list())[0]['status']=='interrupted' and not s.model_gate.locked()
            await s.import_manuscript('已有正文',0)
            with pytest.raises(ManuscriptError,match='空正文'):await s.ideas.generate(await req(s))
        finally:await s.close()
    asyncio.run(scenario())


def test_ideas_http_endpoints_guards_and_editable_draft(project_path):
    with TestClient(create_app(project_path,llm_transport=transport()),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};configure(c,h)
        v=c.get('/api/settings').json()['version']
        assert c.get('/ideas').status_code==200 and c.get('/static/ideas.js').status_code==200
        assert c.post('/api/ideas',json={'idea_text':'test','expected_version':v}).status_code==403
        p=c.post('/api/ideas',headers=h,json={'idea_text':'test','expected_version':v})
        assert p.status_code==200,p.text
        p=p.json();data={'draft':p['draft'],'expected_proposal_version':p['version']}
        assert c.put('/api/ideas/'+p['id']+'/draft',headers=h,json=data).status_code==200
        assert c.put('/api/ideas/'+p['id']+'/draft',headers=h,json=data).status_code==409
        data['expected_proposal_version']+=1;data['expected_version']=v
        r=c.post('/api/ideas/'+p['id']+'/accept',headers=h,json=data)
        assert r.status_code==200,r.text
        assert c.get('/api/ideas').json()['proposals'][0]['status']=='accepted'
        assert c.get('/api/ideas/missing').status_code==422
        assert c.get('/api/manuscript').json()['revision_no']==0
