import asyncio
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from noveltool.app import create_app
from noveltool.db import ProjectStore, SaveFailedError
from noveltool.drafts import DraftInput, CommitDraft
from noveltool.manuscript import ManuscriptError, RevisionConflictError
from noveltool.runtime import ProjectSession
from test_generation import ready, request, finished
from test_llm import envelope
from test_llm_routes import configure
from test_settings import add_person


async def completed(s, n=2):
    t = await s.generation.start(await request(s,n=n), transport=httpx.MockTransport(
        lambda r: httpx.Response(200,json=envelope('候选文本。'))))
    return await finished(s,t['id'])


def save_body(text, v=0, force=False):
    return DraftInput(text=text,expected_draft_version=v,force_save=force)


def commit_body(text, v=0, override=False):
    return CommitDraft(text=text,expected_draft_version=v,allow_out_of_range=override)


def test_draft_is_hot_until_batch_flush_and_reopens(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            t=await completed(s);tid=t['id'];base=s.manuscript.text;sem=(await s.knowledge.view())['version']
            draft='  自己拼装的段落。\n\n😀第二段。\n'
            r=await s.generation.drafts.save(tid,save_body(draft))
            assert r['draft_text']==draft and r['draft_dirty'] and r['draft_version']==1 and r['saved_draft_version']==0
            assert s.manuscript.text==base and (await s.knowledge.view())['version']==sem
            disk=s.store.connection.execute('SELECT draft_text,draft_version FROM generation_tasks WHERE id=?',(tid,)).fetchone()
            assert tuple(disk)==('',0)
            assert 'generation_drafts' in s.project.dirty
            trace=[];s.store.connection.set_trace_callback(trace.append)
            await s.generation.view(tid);await s.generation.view(tid)
            assert not trace
            s.store.connection.set_trace_callback(None)
            assert await s.autosave_tick(s._last_attempt+61)
            r=await s.generation.view(tid)
            assert not r['draft_dirty'] and r['saved_draft_version']==1
            assert not s.project.dirty
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            r=await s.generation.view(tid)
            assert r['draft_text']==draft and r['draft_version']==1 and not s.manuscript.text
        finally:await s.close()
    asyncio.run(scenario())


def test_draft_version_guard_and_checkpoint_flush(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            t=await completed(s);tid=t['id']
            r=await s.generation.drafts.save(tid,save_body('第一版草稿。'))
            with pytest.raises(RevisionConflictError):await s.generation.drafts.save(tid,save_body('旧页面企图覆盖。'))
            r=await s.generation.drafts.save(tid,save_body('第一版草稿。',1))
            assert r['draft_version']==1
            r=await s.generation.drafts.save(tid,save_body('第二版草稿。',1));assert r['draft_version']==2
            # A later successful model checkpoint also flushes pending draft data.
            await s.generation.resume(tid,index=0,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('新的候选。'))))
            r=await finished(s,tid)
            assert r['draft_text']=='第二版草稿。' and not r['draft_dirty'] and r['draft_version']==2
            assert r['slots'][0]['attempt_count']==2
            r=await s.generation.drafts.save(tid,save_body('第三版草稿。',2,True))
            assert r['saved_draft_version']==3 and not s.project.dirty
        finally:await s.close()
    asyncio.run(scenario())


def test_failed_autosave_keeps_pending_draft(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            t=await completed(s);tid=t['id']
            await s.generation.drafts.save(tid,save_body('未写盘的草稿。'))
            s.store.connection.execute("CREATE TRIGGER fail_draft BEFORE UPDATE OF draft_text ON generation_tasks BEGIN SELECT RAISE(ABORT,'disk'); END")
            assert not await s.autosave_tick(s._last_attempt+61)
            r=await s.generation.view(tid)
            assert r['draft_text']=='未写盘的草稿。' and r['draft_dirty'] and s.project.last_save_error
            assert s.store.connection.execute('SELECT draft_text FROM generation_tasks WHERE id=?',(tid,)).fetchone()[0]==''
            s.store.connection.execute('DROP TRIGGER fail_draft')
            assert await s.save()
            assert not (await s.generation.view(tid))['draft_dirty']
        finally:await s.close()
    asyncio.run(scenario())


def test_atomic_commit_preserves_prose_links_revision_and_idempotency(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            await s.import_manuscript('原文缩进  \n\n末尾。',0)
            base=s.manuscript.text
            t=await completed(s);tid=t['id'];candidates=t['attempts']
            text='  候选开头😀\n\n手工补写的第二段。\n'
            await s.generation.drafts.save(tid,save_body(text))
            r=await s.generation.drafts.commit(tid,commit_body(text,1))
            assert s.manuscript.text==base+'\n\n'+text
            assert r['revision']['revision_no']==2 and r['task']['status']=='committed'
            assert not r['task']['draft_dirty'] and not s.project.dirty
            assert r['semantic_sync']=='pending' and r['task']['attempts']==candidates
            repeat=await s.generation.drafts.commit(tid,commit_body(text,0))
            assert repeat['already_committed'] and repeat['revision']==r['revision']
            assert s.manuscript.revision_no==2
            with pytest.raises(RevisionConflictError):await s.generation.drafts.commit(tid,commit_body('另一个文本。',1))
            with pytest.raises(ManuscriptError):await s.generation.drafts.save(tid,save_body('试图改已确认草稿。',1))
            with pytest.raises(ManuscriptError):await s.generation.resume(tid)
            await s.undo_manuscript(2);assert s.manuscript.text==base
            # Repeating an old request must NOT undo the author's undo.
            repeat=await s.generation.drafts.commit(tid,commit_body(text))
            assert repeat['already_committed'] and s.manuscript.text==base and s.manuscript.revision_no==3
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            assert s.manuscript.text==base
            assert (await s.generation.view(tid))['committed_revision_id']==r['revision']['id']
        finally:await s.close()
    asyncio.run(scenario())


def test_commit_final_edit_version_length_override_and_empty(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            t=await completed(s);tid=t['id']
            await s.generation.drafts.save(tid,save_body('保留的草稿。'))
            with pytest.raises(RevisionConflictError):await s.generation.drafts.commit(tid,commit_body('另一页草稿。'))
            with pytest.raises(ManuscriptError):await s.generation.drafts.commit(tid,commit_body(' \n ',1))
            with pytest.raises(ManuscriptError):await s.generation.drafts.commit(tid,commit_body('短',1))
            assert not s.manuscript.text
            r=await s.generation.drafts.commit(tid,commit_body('短',1,True))
            assert s.manuscript.text=='短' and r['task']['draft_version']==2 and r['task']['draft_text']=='短'
        finally:await s.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['text','settings'])
def test_stale_task_cannot_commit_but_keeps_editable_draft(project_path,change):
    async def scenario():
        s=await ready(project_path)
        try:
            t=await completed(s);tid=t['id']
            await s.generation.drafts.save(tid,save_body('旧基线草稿。'))
            if change=='text':await s.append_manuscript('另一次确认。',0)
            else:await add_person(s,'新人物')
            before=s.manuscript.text
            with pytest.raises(RevisionConflictError):await s.generation.drafts.commit(tid,commit_body('旧基线草稿。',1))
            r=await s.generation.drafts.save(tid,save_body('仍能整理供复制的草稿。',1,True))
            assert r['stale'] and r['draft_text']=='仍能整理供复制的草稿。' and s.manuscript.text==before
        finally:await s.close()
    asyncio.run(scenario())


def test_live_task_blocks_commit_but_allows_drafting(project_path):
    async def scenario():
        s=await ready(project_path);started=asyncio.Event();release=asyncio.Event()
        async def handler(r):started.set();await release.wait();return httpx.Response(200,json=envelope('可用候选。'))
        try:
            t=await s.generation.start(await request(s),transport=httpx.MockTransport(handler));tid=t['id'];await started.wait()
            await s.generation.drafts.save(tid,save_body('一边等待一边写。'))
            with pytest.raises(ManuscriptError):await s.generation.drafts.commit(tid,commit_body('一边等待一边写。',1))
            await s.generation.pause(tid);release.set();await finished(s,tid)
            r=await s.generation.drafts.commit(tid,commit_body('一边等待一边写。',1))
            assert r['task']['status']=='committed'
        finally:release.set();await s.close()
    asyncio.run(scenario())


def test_commit_failure_rolls_back_manuscript_task_and_draft_flush(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            await s.import_manuscript('原文。',0);t=await completed(s);tid=t['id'];before=s.manuscript
            await s.generation.drafts.save(tid,save_body('待确认的草稿。'))
            s.store.connection.execute("CREATE TRIGGER fail_commit BEFORE UPDATE OF status ON generation_tasks WHEN NEW.status='committed' BEGIN SELECT RAISE(ABORT,'commit failure'); END")
            with pytest.raises(SaveFailedError):await s.generation.drafts.commit(tid,commit_body('待确认的草稿。',1))
            assert s.manuscript==before and s.project.dirty and s.generation.drafts.pending
            row=s.store.connection.execute('SELECT draft_text,draft_version,status FROM generation_tasks WHERE id=?',(tid,)).fetchone()
            assert tuple(row)==('',0,'ready')
            assert s.store.connection.execute('SELECT count(*) FROM revisions').fetchone()[0]==1
            assert (await s.generation.view(tid))['draft_text']=='待确认的草稿。'
            s.store.connection.execute('DROP TRIGGER fail_commit')
            r=await s.generation.drafts.commit(tid,commit_body('待确认的草稿。',1))
            assert r['task']['status']=='committed' and s.manuscript.text=='原文。\n\n待确认的草稿。'
        finally:await s.close()
    asyncio.run(scenario())


def test_draft_schema_preserves_whitespace_and_rejects_bad_unicode():
    assert save_body(' \t😀\n ').text==' \t😀\n '
    assert commit_body(' \t😀\n ').text==' \t😀\n '
    with pytest.raises(ValidationError):save_body('\x00bad')
    with pytest.raises(ValidationError):commit_body('\ud800')
    with pytest.raises(ValidationError):DraftInput(expected_draft_version=True,text='bad')


def test_draft_http_guards_memory_save_and_commit(project_path):
    with TestClient(create_app(project_path,llm_transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('候选正文。')))),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};configure(c,h)
        v=c.get('/api/settings').json()
        t=c.post('/api/generation',headers=h,json={'context':{'expected_revision_no':0,'expected_version':v['version'],'min_chars':2,'max_chars':30},'candidate_count':1}).json()
        tid=t['id'];c.portal.call(lambda:c.app.state.session.generation.task)
        data={'text':'  人工组合。\n','expected_draft_version':0}
        assert c.put(f'/api/generation/{tid}/draft',json=data).status_code==403
        r=c.put(f'/api/generation/{tid}/draft',headers=h,json=data);assert r.status_code==200,r.text
        assert r.json()['draft_dirty']
        assert c.get('/api/manuscript').json()['text']==''
        assert c.put(f'/api/generation/{tid}/draft',headers=h,json=data).status_code==409
        data['expected_draft_version']=1
        r=c.post(f'/api/generation/{tid}/commit',headers=h,json=data);assert r.status_code==200,r.text
        assert c.get('/api/manuscript').json()['text']==data['text']
        assert c.post(f'/api/generation/{tid}/commit',headers=h,json=data).json()['already_committed']
        assert c.get('/api/manuscript').json()['revision_no']==1
