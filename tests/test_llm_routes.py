from contextlib import closing
import asyncio
import json
import sqlite3
import httpx
from fastapi.testclient import TestClient

from noveltool.app import create_app
from noveltool.db import ProjectStore, SaveFailedError
from noveltool.domain import ProjectConfig
from noveltool.llm import LLMRun
from noveltool.runtime import ProjectSession
from test_llm import envelope


def configure(client,headers):
    view=client.get('/api/config').json()
    client.put('/api/config',headers=headers,json={'expected_memory_version':view['memory_version'],
      'config':{**view['config'],'writer_model':'writer','analysis_model':'analyzer'}})


def test_explicit_model_test_does_not_change_manuscript(project_path):
    def handler(req):return httpx.Response(200,json=envelope('接口可以使用'))
    app=create_app(project_path,llm_transport=httpx.MockTransport(handler))
    with TestClient(app,base_url='http://127.0.0.1') as c:
        headers={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']}
        configure(c,headers)
        result=c.post('/api/llm/test',headers=headers,json={'model_slot':'writer'})
        assert result.status_code==200
        assert result.json()['text']=='接口可以使用'
        assert c.get('/api/manuscript').json()['revision_no']==0
        assert c.get('/api/llm/runs').json()['runs'][0]['status']=='ok'
        with closing(sqlite3.connect(project_path)) as db:
            assert db.execute('SELECT count(*) FROM llm_runs').fetchone()[0]==0
        c.post('/api/save',headers=headers)
    with ProjectStore.open(project_path) as store:
        assert store.connection.execute('SELECT count(*) FROM llm_runs').fetchone()[0]==1


def test_missing_model_and_upstream_errors(client,write_headers):
    r=client.post('/api/llm/test',headers=write_headers,json={})
    assert r.status_code==422 and r.json()['code']=='configuration'
    assert client.get('/model').status_code==200
    assert client.get('/static/model.js').status_code==200
    assert client.post('/api/llm/test',json={}).status_code==403


def test_truncation_returns_error_not_success(project_path):
    app=create_app(project_path,llm_transport=httpx.MockTransport(lambda _:httpx.Response(200,json=envelope('没有写完','length'))))
    with TestClient(app,base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};configure(c,h)
        result=c.post('/api/llm/test',headers=h,json={})
        assert result.status_code==502
        assert result.json()['code']=='truncated'
        assert result.json()['partial_text']=='没有写完'


def test_log_and_manuscript_commit_share_atomic_flush(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            run=LLMRun('a'*32,'test','model','ok','{}','{}',None,None,'stop','{}',1,'now','now',1)
            await s.record_llm_run(run)
            await s.import_manuscript('保存正文也保存待存日志',0)
            assert not s._pending_llm_runs
            assert s.store.connection.execute('SELECT count(*) FROM llm_runs').fetchone()[0]==1
            assert not s.project.dirty
        finally:await s.close()
    asyncio.run(scenario())


def test_requests_dont_hold_project_lock(project_path):
    # Event-controlled live transport, no timing-dependent sleep assertions.
    async def scenario():
        from noveltool.llm import LLMClient
        session=ProjectSession(ProjectStore.open(project_path))
        started,release=asyncio.Event(),asyncio.Event()
        async def handler(req):
            started.set();await release.wait();return httpx.Response(200,json=envelope())
        try:
            async with LLMClient(ProjectConfig(writer_model='m'),on_run=session.record_llm_run,
                                 transport=httpx.MockTransport(handler)) as llm:
                task=asyncio.create_task(llm.complete(model='m',messages=[{'role':'user','content':'test'}]))
                await started.wait()
                await asyncio.wait_for(session.import_manuscript('请求期间可以确认正文',0),timeout=1)
                release.set();await task
            assert session.manuscript.revision_no==1
        finally:release.set();await session.close()
    asyncio.run(scenario())
