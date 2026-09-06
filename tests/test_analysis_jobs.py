import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from noveltool.app import create_app
from noveltool.db import ProjectStore
from noveltool.runtime import ProjectSession
from noveltool.chunker import PlanSettings
from noveltool.domain import ProjectConfig
from noveltool.analysis import build_input
from noveltool.llm_schemas import LinksResult
from noveltool.structured_llm import StructuredError
from noveltool.llm import LLMError
from test_analysis import session_ready, finding, SOURCE, prepare
from test_llm import envelope


def auto_response(req):
    data=json.loads(req.content)
    body=json.loads(data['messages'][-1]['content'])
    schema=body['schema']['properties']
    blocks=body['core_blocks']
    ref=next(iter(blocks));quote=blocks[ref][:120]
    ev=[{'block':ref,'quote':quote}]
    if 'facts' in schema:
        result=finding(ref,quote)
    elif 'relationships' in schema:
        result={'entities':[{'name':'林','kind':'character','evidence':ev},{'name':'周','kind':'character','evidence':ev}],
          'relationships':[{'a':'林','b':'周','label':'拜访','description':'林拜访周。','evidence':ev}],
          'threads':[{'title':'传票的来历','description':'传票来源未明。','status':'open','related_entities':['林'],'evidence':ev}]}
    else:
        result={'analyses':[{'kind':'summary','text':'林到法院寻找线索。','evidence':ev},
                            {'kind':'style','text':'简短的陈述句。','evidence':ev}]}
    return httpx.Response(200,json=envelope(json.dumps(result,ensure_ascii=False)))


def test_all_passes_job_resume_skips_done_and_catalog(project_path):
    async def scenario():
        s=await session_ready(project_path,SOURCE*150,PlanSettings(target_tokens=1200,overlap_tokens=128))
        calls=[]
        def handler(req):calls.append(req);return auto_response(req)
        try:
            p=s.imports.last_plan
            result=await s.jobs.start(p.id,1,['facts','links','narrative'],True,transport=httpx.MockTransport(handler))
            await s.jobs.task
            state=(await s.jobs.view(result['job']['id']))['job']
            assert state['status']=='done' and state['progress']['completed']==len(p.chunks)*3
            assert len(calls)==len(p.chunks)*3
            graph=await s.knowledge.view()
            assert {e['name'] for e in graph['entities']}=={'林','周'}
            assert graph['relationships'] and graph['threads'] and graph['events'] and graph['narrative']
            assert not graph['errors']
            before=len(calls)
            await s.jobs.start(p.id,1,['facts','links','narrative'],True,transport=httpx.MockTransport(handler))
            await s.jobs.task
            assert len(calls)==before
            # Cache hit performs no SQLite reads.
            queries=[];s.store.connection.set_trace_callback(queries.append)
            assert (await s.knowledge.view())['record_count']==graph['record_count']
            s.store.connection.set_trace_callback(None)
            assert not queries
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            assert (await s.knowledge.view())['entities']==graph['entities']
            assert (await s.jobs.view())['job']['status']=='done'
        finally:await s.close()
    asyncio.run(scenario())


def test_partial_job_retries_only_failed_chunks(project_path):
    async def scenario():
        s=await session_ready(project_path,SOURCE*100,PlanSettings(target_tokens=900,overlap_tokens=64))
        calls=[]
        def bad_once(req):
            calls.append(req)
            if len(calls)==2:return httpx.Response(200,json=envelope('{}'))
            return auto_response(req)
        try:
            p=s.imports.last_plan
            await s.jobs.start(p.id,1,['facts'],True,transport=httpx.MockTransport(bad_once));await s.jobs.task
            state=(await s.jobs.view())['job'];assert state['status']=='partial' and state['progress']['failed']==1
            count=len(calls)
            await s.jobs.start(p.id,1,['facts'],False,transport=httpx.MockTransport(bad_once));await s.jobs.task
            assert len(calls)==count and (await s.jobs.view())['job']['status']=='partial'
            await s.jobs.start(p.id,1,['facts'],True,transport=httpx.MockTransport(bad_once));await s.jobs.task
            assert len(calls)==count+1 and (await s.jobs.view())['job']['status']=='done'
        finally:await s.close()
    asyncio.run(scenario())


def test_pause_saves_current_chunk_and_resume(project_path):
    async def scenario():
        s=await session_ready(project_path,SOURCE*100,PlanSettings(target_tokens=900,overlap_tokens=64))
        started,release=asyncio.Event(),asyncio.Event();calls=[]
        async def handler(req):
            calls.append(req);started.set();await release.wait();return auto_response(req)
        try:
            p=s.imports.last_plan
            result=await s.jobs.start(p.id,1,['facts'],True,transport=httpx.MockTransport(handler))
            await started.wait()
            with pytest.raises(LLMError):await s.jobs.start(p.id,1,['facts'],True)
            with pytest.raises(LLMError):await s.analysis.run_chunk(p.id,0,1)
            paused=await s.jobs.pause(result['job']['id'])
            assert paused['job']['status']=='pausing' and s.jobs.busy
            release.set();await s.jobs.task
            assert len(calls)==1 and (await s.jobs.view())['job']['status']=='paused'
            assert (await s.jobs.view())['job']['progress']['completed']==1
            await s.jobs.start(p.id,1,['facts'],True,transport=httpx.MockTransport(handler));await s.jobs.task
            assert len(calls)==len(p.chunks)
        finally:release.set();await s.close()
    asyncio.run(scenario())


def test_shutdown_cancels_worker_before_database_close(project_path):
    async def scenario():
        s=await session_ready(project_path)
        started=asyncio.Event()
        async def handler(req):started.set();await asyncio.Event().wait()
        await s.jobs.start(s.imports.last_plan.id,1,['facts'],True,transport=httpx.MockTransport(handler))
        await started.wait();await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            assert (await s.jobs.view())['job']['status']=='interrupted'
            assert (await s.analysis.list_runs())[0]['status']=='interrupted'
            assert not s.jobs.busy
        finally:await s.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['text','model','network'])
def test_job_stops_on_revision_config_or_network_change(project_path,change):
    async def scenario():
        s=await session_ready(project_path,SOURCE*100,PlanSettings(target_tokens=900,overlap_tokens=64))
        calls=[]
        async def handler(req):
            calls.append(req)
            if change=='text':await s.append_manuscript('更改',1)
            elif change=='model':await s.update_config(ProjectConfig(analysis_protocol="strict", analysis_model='another'),s.project.data.meta.data_version)
            else:raise httpx.ConnectError('offline')
            return auto_response(req)
        try:
            await s.jobs.start(s.imports.last_plan.id,1,['facts'],True,transport=httpx.MockTransport(handler));await s.jobs.task
            state=(await s.jobs.view())['job']
            assert state['status']==('stale' if change=='text' else 'paused')
            assert len(calls)==1
            if change=='text':assert not (await s.knowledge.view())['entities']
        finally:await s.close()
    asyncio.run(scenario())


def test_links_schema_checks_entities_and_core(project_path):
    async def scenario():
        s=await session_ready(project_path)
        try:
            p=build_input(s.imports.last_plan,0,s.manuscript,s.project.data.config,'links')
            value=LinksResult.model_validate({'entities':[], 'relationships':[{'a':'林','b':'周','label':'认识','description':'认识','evidence':[{'block':'B001','quote':SOURCE}]}],'threads':[]})
            with pytest.raises(StructuredError):p.validate(value)
            with pytest.raises(Exception):await s.jobs.start(s.imports.last_plan.id,1,['facts','facts'],True)
        finally:await s.close()
    asyncio.run(scenario())


def test_job_http_endpoints_and_local_guard(project_path):
    import time
    with TestClient(create_app(project_path,llm_transport=httpx.MockTransport(auto_response)),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};b=prepare(c,h)
        assert c.get('/api/analysis/jobs/latest').json()['job'] is None
        assert c.post('/api/analysis/jobs',json=b).status_code==403
        r=c.post('/api/analysis/jobs',headers=h,json={**b,'passes':['facts','links','narrative']})
        assert r.status_code==200,r.text
        jid=r.json()['job']['id']
        for _ in range(100):
            v=c.get('/api/analysis/jobs/'+jid).json()['job']
            if not v['active']:break
            time.sleep(.005)
        assert v['status']=='done'
        graph=c.get('/api/knowledge').json()
        assert len(graph['entities'])==2
        assert c.get('/api/analysis/jobs/missing').status_code==422
        assert c.post('/api/analysis/jobs/'+jid+'/pause',headers=h,json={}).status_code==422
