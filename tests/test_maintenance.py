import asyncio
import json
import sqlite3
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from noveltool.app import create_app
from noveltool.db import ProjectError, ProjectStore
from noveltool.drafts import DraftInput
from noveltool.maintenance import database_snapshot
from noveltool.manuscript import ManuscriptError, RevisionConflictError
from noveltool.runtime import ProjectSession
from test_generation import ready, request, finished
from test_llm import envelope

TR = httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('新的完整候选。')))


def test_backup_flushes_drafts_and_opens_standalone_without_wal(project_path,tmp_path):
    async def scenario():
        s=await ready(project_path);backup=None
        try:
            await s.import_manuscript('原始小说。\n\n尾声。',0)
            t=await s.generation.start(await request(s,n=1),transport=TR);await finished(s,t['id'])
            await s.generation.drafts.save(t['id'],DraftInput(text='尚未定时写盘的组合。',expected_draft_version=0))
            await s.update_title('新标题',s.project.data.meta.data_version)
            assert s.project.dirty
            backup=await s.maintenance.backup(1)
            assert not s.project.dirty and backup.exists() and (backup.stat().st_mode&0o777)==0o600
            assert not Path(str(backup)+'-wal').exists()
            # Original remains open, but backup is an independent new project lock/file.
            copied=ProjectSession(ProjectStore.open(backup))
            try:
                assert copied.manuscript==s.manuscript and copied.project.data.meta.title=='新标题'
                assert (await copied.generation.view(t['id']))['draft_text']=='尚未定时写盘的组合。'
                assert (await copied.maintenance.check(1))['ok']
            finally:await copied.close()
            await s.append_manuscript('备份之后的改动。',1)
            copied=ProjectSession(ProjectStore.open(backup))
            try:assert copied.manuscript.revision_no==1 and not copied.manuscript.text.endswith('备份之后的改动。')
            finally:await copied.close()
        finally:
            await s.close()
            if backup:
                for suffix in ('','-wal','-shm','.lock'):Path(str(backup)+suffix).unlink(missing_ok=True)
    asyncio.run(scenario())


def test_backup_failures_and_old_version_preserve_original(project_path,monkeypatch,tmp_path):
    async def scenario():
        s=await ready(project_path)
        try:
            await s.import_manuscript('原文',0)
            with pytest.raises(RevisionConflictError):await s.maintenance.backup(0)
            def fail(*args):raise OSError('disk unavailable')
            monkeypatch.setattr('noveltool.maintenance.database_snapshot',fail)
            with pytest.raises(ProjectError):await s.maintenance.backup(1)
            assert s.manuscript.text=='原文' and (await s.maintenance.check(1))['ok']
        finally:await s.close()
    asyncio.run(scenario())


def test_backup_temp_deleted_on_failure(tmp_path,monkeypatch):
    import tempfile
    real=tempfile.mkstemp
    monkeypatch.setattr('noveltool.maintenance.tempfile.mkstemp',lambda **kwargs:real(dir=tmp_path,**kwargs))
    class BrokenSource:
        def backup(self,destination):raise sqlite3.OperationalError('test')
    with pytest.raises(sqlite3.OperationalError):database_snapshot(BrokenSource())
    assert not list(tmp_path.iterdir())


def test_integrity_detects_retired_text_and_chain_damage(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            await s.import_manuscript('旧的段落。\n\n不改后文。',0)
            await s.replace_manuscript(0,5,'全新内容。',1)
            r=await s.maintenance.check(2);assert r['ok'] and r['revisions_checked']==2
            c=s.store.connection
            old=c.execute('SELECT id,text FROM manuscript_blocks WHERE seq IS NULL LIMIT 1').fetchone()
            # Deliberately remove DB guards to simulate external damage, not an application write.
            for trigger in ('block_content_immutable','revision_immutable'):
                c.execute(f'DROP TRIGGER {trigger}')
            c.execute('UPDATE manuscript_blocks SET text=? WHERE id=?',('损坏',old['id']))
            report=await s.maintenance.check(2)
            assert not report['ok'] and report['error_count']==1 and '哈希' in report['errors'][0]
            with pytest.raises(ProjectError):await s.maintenance.revision(1)
            c.execute('UPDATE manuscript_blocks SET text=? WHERE id=?',(old['text'],old['id']))
            prev=c.execute('SELECT before_hash FROM revisions WHERE revision_no=2').fetchone()[0]
            c.execute('UPDATE revisions SET before_hash=? WHERE revision_no=2',('0'*64,))
            assert not (await s.maintenance.check(2))['ok']
            c.execute('UPDATE revisions SET before_hash=? WHERE revision_no=2',(prev,))
            assert (await s.maintenance.check(2))['ok']
        finally:await s.close()
    asyncio.run(scenario())


def test_history_is_readonly_and_bounded_diff(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            text=''.join(f'原来第{i}行\n' for i in range(800))
            await s.import_manuscript(text,0)
            await s.replace_manuscript(0,len(text),'新文本\n'*800,1)
            r=await s.maintenance.revision(2)
            assert r['before']==text and r['after']=='新文本\n'*800 and r['diff_truncated']
            assert s.manuscript.revision_no==2
            await s.undo_manuscript(2)
            r=await s.maintenance.revision(3);assert r['revision']['kind']=='undo' and r['after']==text
            with pytest.raises(ManuscriptError):await s.maintenance.revision(99)
        finally:await s.close()
    asyncio.run(scenario())


def test_recovery_summary_does_not_reissue_model_requests(project_path):
    async def scenario():
        s=await ready(project_path)
        t=await s.generation.start(await request(s,n=1),transport=TR);await finished(s,t['id'])
        await s.close()
        c=sqlite3.connect(project_path)
        try:
            c.execute("UPDATE generation_tasks SET status='generating' WHERE id=?",(t['id'],));c.commit()
        finally:c.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            view=await s.maintenance.overview()
            assert view['startup_recovery']['generation_tasks']==1
            assert view['unfinished']['generation_tasks'][0]['status']=='interrupted'
            assert not s.generation.busy and s.generation.task is None
            assert len((await s.generation.view(t['id']))['attempts'])==1
        finally:await s.close()
    asyncio.run(scenario())


def test_diagnostic_logs_pending_redacted_and_truncated(project_path,monkeypatch):
    from noveltool.llm import LLMClient
    from dataclasses import replace
    async def scenario():
        s=await ready(project_path)
        monkeypatch.setenv('NOVELTOOL_API_KEY','private-token-123')
        try:
            async with LLMClient(s.project.data.config,on_run=s.record_llm_run,transport=TR) as model:
                result=await model.complete(messages=[{'role':'user','content':'测试日志'}],model='fake')
            v=await s.maintenance.run(result.run_id)
            assert v['pending_save'] and 'private-token-123' not in json.dumps(v)
            s._pending_llm_runs[0]=replace(s._pending_llm_runs[0],raw_response='a'*64001)
            v=await s.maintenance.run(result.run_id);assert len(v['raw_response'])==64000 and 'raw_response' in v['truncated_fields']
            await s.save();v=await s.maintenance.run(result.run_id);assert not v['pending_save']
            with pytest.raises(ManuscriptError):await s.maintenance.run('missing')
        finally:await s.close()
    asyncio.run(scenario())


def test_maintenance_http_guard_download_cleanup_and_check(project_path,tmp_path,monkeypatch):
    made=[]
    import noveltool.maintenance as m
    real=m.database_snapshot
    def backup(c):
        p=real(c);made.append(p);return p
    monkeypatch.setattr(m,'database_snapshot',backup)
    with TestClient(create_app(project_path),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']}
        assert c.get('/maintenance').status_code==200
        assert c.post('/api/maintenance/backup',json={'expected_revision_no':0}).status_code==403
        c.post('/api/manuscript/import',headers=h,json={'text':'安全保存的小说。','expected_revision_no':0})
        r=c.post('/api/maintenance/check',headers=h,json={'expected_revision_no':1});assert r.status_code==200 and r.json()['ok']
        assert c.get('/api/maintenance').json()['revision_no']==1
        assert c.get('/api/maintenance/revisions/1').json()['after']=='安全保存的小说。'
        r=c.post('/api/maintenance/backup',headers=h,json={'expected_revision_no':1});assert r.status_code==200
        assert r.content.startswith(b'SQLite format 3') and 'attachment' in r.headers['content-disposition']
        assert not made[0].exists()
        path=tmp_path/'download.sqlite3';path.write_bytes(r.content)
        store=ProjectStore.open(path);store.close()
        assert c.get('/api/maintenance/runs/missing').status_code==422
