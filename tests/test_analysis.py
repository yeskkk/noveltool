import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from noveltool.app import create_app
from noveltool.db import ProjectStore, SaveFailedError
from noveltool.domain import ProjectConfig
from noveltool.runtime import ProjectSession
from noveltool.chunker import PlanSettings
from noveltool.analysis import build_input
from noveltool.structured_llm import StructuredError
from noveltool.llm import LLMError
from test_llm import envelope
from test_llm_routes import configure

SOURCE = '林来到法院。'

def finding(ref='B001',quote=SOURCE):
    evidence=[{'block':ref,'quote':quote}]
    return {'entities':[{'name':'林','kind':'character','evidence':evidence}],
        'facts':[{'subject':'林','field':'位置','value':'法院','mode':'state','evidence':evidence}],
        'events':[{'summary':'林来到法院。','participants':['林'],'story_time':None,'evidence':evidence}]}

def prepare(c,h,text=SOURCE):
    configure(c,h)
    assert c.post('/api/manuscript/import',headers=h,json={'text':text,'expected_revision_no':0}).status_code==200
    p=c.post('/api/import/plan',headers=h,json={'expected_revision_no':1}).json()
    return {'plan_id':p['id'],'expected_revision_no':1}

def test_chunk_success_pending_retries_and_reopen(project_path):
    calls=[]
    def handler(req):
        calls.append(json.loads(req.content));return httpx.Response(200,json=envelope(json.dumps(finding(),ensure_ascii=False)))
    with TestClient(create_app(project_path,llm_transport=httpx.MockTransport(handler)),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};b=prepare(c,h)
        assert c.get('/analysis').status_code==200
        assert c.get('/static/workspace.js').status_code==200
        preview=c.post('/api/analysis/chunks/0/preview',headers=h,json=b)
        assert preview.status_code==200 and not calls
        r=c.post('/api/analysis/chunks/0/run',headers=h,json=b)
        assert r.status_code==200,r.text
        data=r.json();rid=data['id']
        assert data['status']=='done' and not data['stale']
        assert len(data['observations'])==3 and all(o['status']=='pending' for o in data['observations'])
        assert data['observations'][1]['evidence'][0]['block_id']==next(iter(data['refs'].values()))['block_id']
        assert c.get('/api/manuscript').json()['text']==SOURCE
        assert c.post('/api/analysis/chunks/0/run',headers=h,json=b).status_code==200
        assert len(c.get('/api/analysis/runs').json()['runs'])==2
        assert len(calls)==2
    with TestClient(create_app(project_path),base_url='http://127.0.0.1') as c:
        assert c.get('/api/analysis/runs/'+rid).json()['observations']==data['observations']
        assert c.get('/api/analysis/runs/missing').status_code==422

@pytest.mark.parametrize('data',[finding('B999'),finding(quote='捏造证据'),{}, {'entities':[],'facts':[],'events':[],'extra':'x'}])
def test_invalid_output_does_not_modify_text(project_path,data):
    tr=httpx.MockTransport(lambda req:httpx.Response(200,json=envelope(json.dumps(data))))
    with TestClient(create_app(project_path,llm_transport=tr),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};b=prepare(c,h)
        r=c.post('/api/analysis/chunks/0/run',headers=h,json=b)
        assert r.status_code==422
        runs=c.get('/api/analysis/runs').json()['runs'];assert runs[0]['status']=='failed'
        assert c.get('/api/analysis/runs/'+runs[0]['id']).json()['observations']==[]
        assert c.get('/api/manuscript').json()['revision_no']==1


def test_empty_analysis_is_valid_and_repaired_requires_review(project_path):
    tr=httpx.MockTransport(lambda req:httpx.Response(200,json=envelope("{'entities':[], 'facts':[], 'events':[],}")))
    with TestClient(create_app(project_path,llm_transport=tr),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};b=prepare(c,h)
        r=c.post('/api/analysis/chunks/0/run',headers=h,json=b).json()
        assert r['requires_review'] and r['status']=='done' and not r['observations']
        assert c.post('/api/analysis/chunks/999/run',headers=h,json=b).status_code==422
        assert c.post('/api/analysis/chunks/0/run',json=b).status_code==403
        c.post('/api/manuscript/append',headers=h,json={'text':'后文','expected_revision_no':1})
        assert c.get('/api/analysis/runs/'+r['id']).json()['stale']
        assert c.post('/api/analysis/chunks/0/run',headers=h,json=b).status_code==409

async def session_ready(path,text=SOURCE,settings=None):
    s=ProjectSession(ProjectStore.open(path))
    await s.update_config(ProjectConfig(analysis_model='fake'),s.project.data.meta.data_version)
    await s.import_manuscript(text,0)
    await s.imports.create_plan(settings or PlanSettings(),1)
    return s

def test_revision_changed_while_model_runs(project_path):
    async def scenario():
        s=await session_ready(project_path)
        try:
            async def handler(req):
                await s.append_manuscript('修改后文',1)
                return httpx.Response(200,json=envelope(json.dumps(finding())))
            r=await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=httpx.MockTransport(handler))
            assert r['status']=='stale' and r['stale'] and len(r['observations'])==3
            assert s.manuscript.text.endswith('修改后文')
        finally:await s.close()
    asyncio.run(scenario())

def test_core_evidence_required_and_budget_checked(project_path):
    async def scenario():
        s=await session_ready(project_path,SOURCE*150,PlanSettings(target_tokens=512,overlap_tokens=128))
        try:
            p=build_input(s.imports.last_plan,1,s.manuscript,s.project.data.config)
            ref=next(k for k in p.blocks if k not in p.core_refs)
            from noveltool.llm_schemas import FactExtractionResult
            data=FactExtractionResult.model_validate(finding(ref,p.blocks[ref]))
            with pytest.raises(StructuredError,match='核心'):p.validate(data)
            cfg=s.project.data.config.model_copy(update={'context_window':2048})
            from noveltool.manuscript import ManuscriptError
            with pytest.raises(ManuscriptError):build_input(s.imports.last_plan,1,s.manuscript,cfg)
        finally:await s.close()
    asyncio.run(scenario())

def test_observation_transaction_failure_is_atomic(project_path):
    async def scenario():
        s=await session_ready(project_path)
        try:
            s.store.connection.execute("CREATE TRIGGER fail_obs BEFORE INSERT ON observations BEGIN SELECT RAISE(ABORT,'test'); END")
            with pytest.raises(SaveFailedError):await s.analysis.run_chunk(s.imports.last_plan.id,0,1,
                transport=httpx.MockTransport(lambda req:httpx.Response(200,json=envelope(json.dumps(finding())))))
            assert not s.store.connection.execute('SELECT * FROM observations').fetchall()
            assert (await s.analysis.list_runs())[0]['status']=='failed'
            assert s.manuscript.text==SOURCE
            s.store.connection.execute('DROP TRIGGER fail_obs')
        finally:await s.close()
    asyncio.run(scenario())

def test_cancel_releases_gate_and_marks_interrupted(project_path):
    async def scenario():
        s=await session_ready(project_path)
        try:
            async def handler(req):raise asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=httpx.MockTransport(handler))
            assert not s.model_gate.locked()
            assert (await s.analysis.list_runs())[0]['status']=='interrupted'
            # Simulate persisted running row left by hard process death.
            s.store.connection.execute("UPDATE analysis_runs SET status='running'")
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:assert (await s.analysis.list_runs())[0]['status']=='interrupted'
        finally:await s.close()
    asyncio.run(scenario())

def test_busy_no_model_and_wrong_plan_do_not_start(project_path):
    async def scenario():
        s=await session_ready(project_path)
        try:
            async with s.model_gate:
                with pytest.raises(LLMError,match='正在运行'):await s.analysis.run_chunk(s.imports.last_plan.id,0,1)
            from noveltool.manuscript import ManuscriptError
            with pytest.raises(ManuscriptError):await s.analysis.run_chunk('bad',0,1)
            await s.update_config(ProjectConfig(),s.project.data.meta.data_version)
            with pytest.raises(LLMError,match='名称'):await s.analysis.run_chunk(s.imports.last_plan.id,0,1)
            assert not await s.analysis.list_runs()
        finally:await s.close()
    asyncio.run(scenario())
