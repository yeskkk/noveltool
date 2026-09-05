import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from noveltool.app import create_app
from noveltool.context import ContextRequest
from noveltool.db import ProjectStore
from noveltool.domain import ProjectConfig
from noveltool.generation import GenerationRequest
from noveltool.runtime import ProjectSession
from noveltool.llm import LLMError
from noveltool.manuscript import ManuscriptError,RevisionConflictError
from test_context import body
from test_llm import envelope
from test_llm_routes import configure
from test_settings import add_person

async def ready(path):
    s=ProjectSession(ProjectStore.open(path));await s.update_config(ProjectConfig(writer_model='fake-writer',analysis_model='fake-analyzer'),s.project.data.meta.data_version);return s

async def request(s,n=3,**kwargs):
    return GenerationRequest(context=await body(s,min_chars=2,max_chars=30,**kwargs),candidate_count=n)

async def finished(s,tid):
    if s.generation.task:await s.generation.task
    return await s.generation.view(tid)


def test_n_independent_calls_snapshots_and_reopen(project_path):
    async def scenario():
        s=await ready(project_path);calls=[]
        async def handler(r):
            calls.append(json.loads(r.content))
            if len(calls)==1:
                await s.update_config(ProjectConfig(writer_model='new',candidate_count=1,min_chars=800,max_chars=900),s.project.data.meta.data_version)
            return httpx.Response(200,json=envelope(f'林走出了房门，候选{len(calls)}。'))
        try:
            task=await s.generation.start(await request(s),transport=httpx.MockTransport(handler));tid=task['id']
            result=await finished(s,tid)
            assert result['status']=='ready' and result['usable_count']==result['in_range_count']==3
            assert len(calls)==3 and all(c['model']=='fake-writer' for c in calls)
            assert all(c['messages']==calls[0]['messages'] and 'response_format' not in c for c in calls)
            assert s.manuscript.revision_no==0 and not (await s.knowledge.view())['entities']
            assert result['candidate_count']==3 and result['min_chars']==2 and result['max_chars']==30
            sql=[];s.store.connection.set_trace_callback(sql.append)
            await s.generation.view(tid);await s.generation.view(tid)
            assert not sql  # Hot task and knowledge view do not poll SQLite.
            s.store.connection.set_trace_callback(None)
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            reopened=await s.generation.view(tid)
            assert [x['candidate']['text'] for x in reopened['slots']]==[x['candidate']['text'] for x in result['slots']]
            assert not s.generation.busy
        finally:await s.close()
    asyncio.run(scenario())


def test_lengths_duplicates_and_regenerate_keeps_older_success(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            task=await s.generation.start(await request(s),transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('短'))))
            r=await finished(s,task['id'])
            assert r['usable_count']==3 and r['in_range_count']==0
            assert r['slots'][1]['duplicate_of']==0 and r['slots'][2]['duplicate_of']==0
            await s.generation.resume(task['id'],index=0,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('截断内容','length'))))
            r=await finished(s,task['id'])
            assert r['slots'][0]['candidate']['text']=='短' and r['slots'][0]['latest_attempt']['status']=='failed'
            assert len(r['attempts'])==4
            await s.generation.resume(task['id'],index=0,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('新的完整文本。'))))
            r=await finished(s,task['id']);assert r['slots'][0]['candidate']['text']=='新的完整文本。'
        finally:await s.close()
    asyncio.run(scenario())


def test_network_pause_and_resume_only_missing_slots(project_path):
    async def scenario():
        s=await ready(project_path);calls=[]
        def handler(r):
            calls.append(1)
            if len(calls)==2:raise httpx.ConnectError('offline')
            return httpx.Response(200,json=envelope('可用候选。'))
        try:
            t=await s.generation.start(await request(s),transport=httpx.MockTransport(handler));v=await finished(s,t['id'])
            assert v['status']=='paused' and len(calls)==2 and v['usable_count']==1
            calls.clear()
            def success(r):calls.append(1);return httpx.Response(200,json=envelope('新的可用候选。'))
            await s.generation.resume(t['id'],transport=httpx.MockTransport(success));v=await finished(s,t['id'])
            assert len(calls)==2 and v['usable_count']==3 and v['status']=='ready'
        finally:await s.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['text','settings'])
def test_changed_baseline_stops_but_preserves_candidate(project_path,change):
    async def scenario():
        s=await ready(project_path);calls=[]
        async def handler(r):
            calls.append(1)
            if change=='text':await s.append_manuscript('用户确认的其他正文',0)
            else:await add_person(s,'新人物')
            return httpx.Response(200,json=envelope('旧上下文返回的候选。'))
        try:
            t=await s.generation.start(await request(s),transport=httpx.MockTransport(handler));v=await finished(s,t['id'])
            assert len(calls)==1 and v['stale'] and v['status']=='stale'
            assert v['slots'][0]['candidate']['text']=='旧上下文返回的候选。'
            with pytest.raises(RevisionConflictError):await s.generation.resume(t['id'])
        finally:await s.close()
    asyncio.run(scenario())


def test_pause_busy_and_shutdown_checkpoint(project_path):
    async def scenario():
        s=await ready(project_path);started=asyncio.Event();release=asyncio.Event()
        async def handler(r):started.set();await release.wait();return httpx.Response(200,json=envelope('已完成的候选。'))
        t=await s.generation.start(await request(s),transport=httpx.MockTransport(handler));await started.wait()
        with pytest.raises(LLMError):await s.generation.start(await request(s))
        await s.generation.pause(t['id']);release.set();v=await finished(s,t['id'])
        assert v['status']=='paused' and v['usable_count']==1
        started.clear();release.clear()
        await s.generation.resume(t['id'],transport=httpx.MockTransport(handler));await started.wait()
        await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            v=await s.generation.view(t['id'])
            assert v['status']=='interrupted' and not v['live'] and v['usable_count']==1
            assert v['slots'][1]['latest_attempt']['status']=='interrupted'
        finally:await s.close()
    asyncio.run(scenario())


def test_generation_disk_failure_does_not_destroy_existing_success(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            t=await s.generation.start(await request(s,n=1),transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('原来候选。'))));await finished(s,t['id'])
            s.store.connection.execute("CREATE TRIGGER fail_candidate BEFORE UPDATE ON generation_attempts WHEN NEW.status='complete' BEGIN SELECT RAISE(ABORT,'test'); END")
            await s.generation.resume(t['id'],index=0,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('新的候选。'))));v=await finished(s,t['id'])
            assert v['slots'][0]['candidate']['text']=='原来候选。' and v['status']=='failed'
            assert not s.manuscript.text
            s.store.connection.execute('DROP TRIGGER fail_candidate')
        finally:await s.close()
    asyncio.run(scenario())


def test_generation_http_and_rewrite_not_enabled_yet(project_path):
    with TestClient(create_app(project_path,llm_transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('候选文本。')))),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};configure(c,h)
        assert c.get('/generation').status_code==200 and c.get('/static/generation.js').status_code==200
        v=c.get('/api/settings').json();b={'context':{'expected_revision_no':0,'expected_version':v['version'],'min_chars':1,'max_chars':30},'candidate_count':1}
        assert c.post('/api/generation',json=b).status_code==403
        p=c.post('/api/generation',headers=h,json=b);assert p.status_code==202,p.text
        tid=p.json()['id'];c.portal.call(lambda: c.app.state.session.generation.task)
        r=c.get('/api/generation/'+tid).json();assert r['usable_count']==1
        assert c.get('/api/generation/'+tid+'/context').json()['messages']
        assert c.post('/api/generation/'+tid+'/regenerate/99',headers=h).status_code==422
        assert len(c.get('/api/generation').json()['tasks'])==1
        assert c.get('/api/generation/missing').status_code==422
        assert c.get('/api/manuscript').json()['revision_no']==0
        b['context'].update(task_type='rewrite',start_cp=0,end_cp=1)
        assert c.post('/api/generation',headers=h,json=b).status_code==422
