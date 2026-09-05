import asyncio
from contextlib import closing
import json
import sqlite3
import pytest
from fastapi.testclient import TestClient
from noveltool.app import create_app
from noveltool.db import ProjectStore,SaveFailedError,ProjectError
from noveltool.runtime import ProjectSession
from noveltool.import_text import decode_import,PREVIEW_TTL_SECONDS
from noveltool.chunker import PlanSettings
from noveltool.manuscript import ManuscriptError,RevisionConflictError

RAW=('\ufeff第一章\r\n\r\n　甲😀乙。\r\n'+('第二段很长。'*500)+'\r\n').encode()

def preview(client,headers,raw=RAW,**query):
    return client.post('/api/import/preview',params={'filename':'原作.txt',**query},headers=headers,content=raw)

def test_preview_commit_raw_and_reopen_plan(client,write_headers,project_path):
    assert client.get('/import').status_code==200
    assert client.get('/static/import.js').status_code==200
    p=preview(client,write_headers).json()
    assert p['can_commit'] and p['encoding']=='utf-8-sig'
    assert client.get('/api/manuscript').json()['revision_no']==0
    assert client.get('/api/import/sources').json()['sources']==[]
    got=client.post('/api/import/commit',headers=write_headers,json={'preview_id':p['preview_id'],'expected_revision_no':0})
    assert got.status_code==200 and got.json()['text'].startswith('第一章\n\n')
    source=client.get('/api/import/sources').json()['sources'][0]
    assert client.get('/api/import/sources/'+source['id']+'/raw').content==RAW
    plan=client.post('/api/import/plan',headers=write_headers,json={'expected_revision_no':1,'settings':{'target_tokens':512,'overlap_tokens':64}})
    assert plan.status_code==200 and plan.json()['chunk_count']>5
    assert not client.get('/api/status').json()['dirty']
    with closing(sqlite3.connect(project_path)) as db:
        assert db.execute('SELECT length(raw_bytes) FROM source_imports').fetchone()[0]==len(RAW)
        assert db.execute('SELECT count(*) FROM chunk_plans').fetchone()[0]==1
    client.post('/api/manuscript/append',headers=write_headers,json={'expected_revision_no':1,'text':'后文'})
    assert client.get('/api/import/plan').json()['plan']['stale']
    bad=client.post('/api/import/plan',headers=write_headers,json={'expected_revision_no':1})
    assert bad.status_code==409


def test_invalid_previews_auth_and_no_overwrite(client,write_headers):
    assert client.post('/api/import/preview?filename=a.txt',content=b'abc').status_code==403
    assert preview(client,write_headers,b'\xff\x00').status_code==422
    assert preview(client,write_headers,encoding='wrong').status_code==422
    assert client.get('/api/import/sources/missing/raw').status_code==422
    client.post('/api/manuscript/import',headers=write_headers,json={'text':'existing','expected_revision_no':0})
    p=preview(client,write_headers).json()
    assert not p['can_commit']
    got=client.post('/api/import/commit',headers=write_headers,json={'preview_id':p['preview_id'],'expected_revision_no':1})
    assert got.status_code==422
    assert client.get('/api/manuscript').json()['text']=='existing'


def test_preview_bound_expiry_and_commit_conflict(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            ids=[]
            for i in range(3):ids.append((await s.imports.remember(decode_import(b'abc',f'{i}.txt')))['preview_id'])
            assert len(s.imports.previews)==2 and ids[0] not in s.imports.previews
            with pytest.raises(ManuscriptError):await s.imports.commit(ids[0],0)
            stamp,p,rev=s.imports.previews[ids[1]]
            s.imports.previews[ids[1]]=(stamp-PREVIEW_TTL_SECONDS-1,p,rev)
            with pytest.raises(ManuscriptError):await s.imports.commit(ids[1],0)
            await s.import_manuscript('other',0)
            with pytest.raises(RevisionConflictError):await s.imports.commit(ids[2],0)
        finally:await s.close()
    asyncio.run(scenario())


def test_atomic_failure_and_retry(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            p=await s.imports.remember(decode_import(RAW,'book.txt'))
            s.store.connection.execute("CREATE TRIGGER fail_source BEFORE INSERT ON source_imports BEGIN SELECT RAISE(ABORT,'disk simulation'); END")
            with pytest.raises(SaveFailedError):await s.imports.commit(p['preview_id'],0)
            assert s.manuscript.revision_no==0 and p['preview_id'] in s.imports.previews
            assert s.store.connection.execute('SELECT count(*) FROM revisions').fetchone()[0]==0
            s.store.connection.execute('DROP TRIGGER fail_source')
            await s.imports.commit(p['preview_id'],0)
            await s.imports.create_plan(PlanSettings(),1)
            previous=s.imports.last_plan
            s.store.connection.execute("CREATE TRIGGER fail_plan BEFORE INSERT ON chunk_plans BEGIN SELECT RAISE(ABORT,'disk simulation'); END")
            with pytest.raises(SaveFailedError):await s.imports.create_plan(PlanSettings(target_tokens=7000),1)
            assert s.imports.last_plan==previous
            s.store.connection.execute('DROP TRIGGER fail_plan')
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            assert not s.imports.previews
            assert s.imports.last_plan==previous
            assert await s.imports.original(p['preview_id'])==RAW
            await s.undo_manuscript(1)
            assert (await s.imports.current_plan())['plan']['stale']
            assert await s.imports.original(p['preview_id'])==RAW
        finally:await s.close()
    asyncio.run(scenario())


def test_corrupt_stored_plan_does_not_block_manuscript(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        await s.import_manuscript('正文',0)
        await s.imports.create_plan(PlanSettings(),1)
        await s.close()
    asyncio.run(scenario())
    with closing(sqlite3.connect(project_path)) as c:
        c.execute("UPDATE chunk_plans SET plan_json='{}'");c.commit()
    async def check_recovery():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            assert s.manuscript.text=='正文'
            assert s.imports.last_plan is None and s.imports.plan_load_error
            await s.imports.create_plan(PlanSettings(),1)
            assert s.imports.last_plan and not s.imports.plan_load_error
        finally:await s.close()
    asyncio.run(check_recovery())


def test_context_change_invalidates_old_budget(client,write_headers):
    client.post('/api/manuscript/import',headers=write_headers,json={'text':'一段正文','expected_revision_no':0})
    made=client.post('/api/import/plan',headers=write_headers,json={'expected_revision_no':1})
    assert made.status_code==200
    view=client.get('/api/config').json()
    changed=client.put('/api/config',headers=write_headers,json={'expected_memory_version':view['memory_version'],
        'config':{**view['config'],'context_window':4096}})
    assert changed.status_code==200
    plan=client.get('/api/import/plan').json()['plan']
    assert plan['stale'] and '预算' in plan['stale_reason']


def test_raw_checksum_failure_preserves_manuscript(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            p=await s.imports.remember(decode_import(b'original','test.txt'))
            await s.imports.commit(p['preview_id'],0)
            s.store.connection.execute("UPDATE source_imports SET raw_bytes=?",(b'changed',))
            with pytest.raises(ManuscriptError,match='哈希'):await s.imports.original(p['preview_id'])
            assert s.manuscript.text=='original'
        finally:await s.close()
    asyncio.run(scenario())
