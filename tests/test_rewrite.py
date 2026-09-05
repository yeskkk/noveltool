import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from noveltool.app import create_app
from noveltool.db import ProjectStore, ProjectError
from noveltool.drafts import CommitDraft, DraftInput
from noveltool.generation import GenerationRequest
from noveltool.manuscript import ManuscriptError, RevisionConflictError
from noveltool.rewrite import capture_target, verify_target, RewriteTarget
from noveltool.runtime import ProjectSession
from test_generation import ready, request, finished
from test_llm import envelope
from test_semantic import response as analysis_response

SOURCE='林😀把钥匙交给周。\n\n周收下了钥匙。\n\n多年以后，周用钥匙打开门。'
NEW='林把钥匙留在自己口袋。\n\n周没有得到钥匙。'
TR=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope(NEW)))

async def task(s,start=2,end=18,n=2):
    await s.import_manuscript(SOURCE,0)
    t=await s.generation.start(await request(s,n=n,task_type='rewrite',start_cp=start,end_cp=end),transport=TR)
    return await finished(s,t['id'])


def test_rewrite_cross_blocks_emoji_draft_preview_commit_undo_reopen(project_path):
    async def scenario():
        s=await ready(project_path);a,b=2,18
        try:
            t=await task(s,a,b);tid=t['id']
            assert t['task_type']=='rewrite' and t['usable_count']==2
            assert t['rewrite_target']['selected_text']==SOURCE[a:b]
            assert len(t['rewrite_target']['slices'])>1
            assert s.manuscript.text==SOURCE
            old=tuple(s.manuscript.blocks)
            p=await s.generation.drafts.preview(tid,CommitDraft(text=NEW,expected_draft_version=0))
            assert p['original']==SOURCE[a:b] and NEW in p['replacement'] and '原选区' in p['diff']
            assert s.manuscript.text==SOURCE
            r=await s.generation.drafts.commit(tid,CommitDraft(text=NEW,expected_draft_version=0))
            assert r['revision']['kind']=='rewrite'
            assert s.manuscript.text==SOURCE[:a]+NEW+SOURCE[b:]
            assert s.manuscript.blocks[-1].id==old[-1].id
            rows=list(s.store.connection.execute('SELECT text FROM manuscript_blocks WHERE seq IS NULL'))
            assert rows and all(any(b.text==r['text'] for b in old) for r in rows)
            again=await s.generation.drafts.commit(tid,CommitDraft(text=NEW,expected_draft_version=0))
            assert again['already_committed'] and s.manuscript.revision_no==2
            await s.undo_manuscript(2);assert s.manuscript.text==SOURCE
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            assert (await s.generation.view(tid))['rewrite_target']['selected_text']==SOURCE[a:b]
            assert s.manuscript.text==SOURCE
            again=await s.generation.drafts.commit(tid,CommitDraft(text=NEW,expected_draft_version=0))
            assert again['already_committed'] and s.manuscript.text==SOURCE
        finally:await s.close()
    asyncio.run(scenario())


def test_rewrite_snapshot_survives_restart_before_commit(project_path):
    async def scenario():
        s=await ready(project_path)
        t=await task(s);await s.generation.drafts.save(t['id'],DraftInput(text=NEW,expected_draft_version=0,force_save=True));await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            t=await s.generation.view(t['id']);assert not t['stale'] and t['draft_text']==NEW
            r=await s.generation.drafts.commit(t['id'],CommitDraft(text=NEW,expected_draft_version=1))
            assert r['revision']['kind']=='rewrite'
        finally:await s.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('start,end',[(0,1),(1,2),(0,len(SOURCE)),(len(SOURCE)-1,len(SOURCE))])
def test_rewrite_edge_ranges_are_exact(project_path,start,end):
    async def scenario():
        s=await ready(project_path)
        try:
            t=await task(s,start,end,n=1)
            await s.generation.drafts.commit(t['id'],CommitDraft(text=NEW,expected_draft_version=0))
            assert s.manuscript.text==SOURCE[:start]+NEW+SOURCE[end:]
            await s.undo_manuscript(2);assert s.manuscript.text==SOURCE
        finally:await s.close()
    asyncio.run(scenario())


def test_rewrite_noop_empty_stale_and_bad_length_do_not_modify(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            t=await task(s)
            for text,allow in [(SOURCE[2:18],True),(' ',True),('一',False)]:
                with pytest.raises(ManuscriptError):
                    await s.generation.drafts.commit(t['id'],CommitDraft(text=text,allow_out_of_range=allow,expected_draft_version=0))
                assert s.manuscript.text==SOURCE
            await s.append_manuscript('另一个页面的修改。',1)
            with pytest.raises(RevisionConflictError):await s.generation.drafts.commit(t['id'],CommitDraft(text=NEW,expected_draft_version=0))
            assert s.manuscript.text.startswith(SOURCE)
        finally:await s.close()
    asyncio.run(scenario())


def test_rewrite_commit_failure_rolls_back_blocks_and_task(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            t=await task(s)
            await s.generation.drafts.save(t['id'],DraftInput(text=NEW,expected_draft_version=0))
            s.store.connection.execute("CREATE TRIGGER fail_rewrite BEFORE UPDATE ON generation_tasks WHEN NEW.status='committed' BEGIN SELECT RAISE(ABORT,'test'); END")
            with pytest.raises(ProjectError):await s.generation.drafts.commit(t['id'],CommitDraft(text=NEW,expected_draft_version=1))
            assert s.manuscript.text==SOURCE and s.manuscript.revision_no==1
            assert s.generation.drafts.pending[t['id']].text==NEW
            assert s.store.connection.execute('SELECT count(*) FROM revisions').fetchone()[0]==1
            assert (await s.generation.view(t['id']))['status']=='ready'
            s.store.connection.execute('DROP TRIGGER fail_rewrite')
            await s.generation.drafts.commit(t['id'],CommitDraft(text=NEW,expected_draft_version=1))
            assert s.manuscript.revision_no==2
        finally:await s.close()
    asyncio.run(scenario())


def test_rewrite_tampered_selection_is_rejected(project_path):
    async def scenario():
        s=await ready(project_path)
        try:
            t=await task(s)
            s.store.connection.execute("UPDATE generation_rewrite_targets SET target_json='{}' WHERE task_id=?",(t['id'],))
            s.generation._invalidate(t['id'])
            with pytest.raises(ManuscriptError):await s.generation.view(t['id'])
            assert s.manuscript.text==SOURCE
            target=capture_target(s.manuscript,2,18)
            wrong=target.model_copy(update={'selected_text':'bad'})
            with pytest.raises(ManuscriptError):verify_target(wrong,s.manuscript)
        finally:await s.close()
    asyncio.run(scenario())


def test_rewrite_http_auto_sync_and_no_double_commit(project_path):
    import time
    def response(req):
        payload=json.loads(req.content);user=json.loads(payload['messages'][-1]['content'])
        if 'sources' in user:return httpx.Response(200,json=envelope('{"issues":[]}'))
        if 'schema' in user:return analysis_response(req)
        return httpx.Response(200,json=envelope(NEW))
    with TestClient(create_app(project_path,llm_transport=httpx.MockTransport(response)),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']}
        from test_llm_routes import configure
        configure(c,h)
        cfg=c.get('/api/config').json();cfg['config']['analysis_model']='fake-analyzer'
        c.put('/api/config',headers=h,json={'expected_memory_version':cfg['memory_version'],'config':cfg['config']})
        c.post('/api/manuscript/import',headers=h,json={'text':SOURCE,'expected_revision_no':0})
        v=c.get('/api/settings').json()
        b={'context':{'task_type':'rewrite','expected_revision_no':1,'expected_version':v['version'],'start_cp':2,'end_cp':18,'min_chars':1,'max_chars':100},'candidate_count':2}
        t=c.post('/api/generation',headers=h,json=b);assert t.status_code==202,t.text
        tid=t.json()['id'];c.portal.call(lambda:c.app.state.session.generation.task)
        commit={'text':NEW,'expected_draft_version':0}
        assert c.post(f'/api/generation/{tid}/preview',headers=h,json=commit).status_code==200
        r=c.post(f'/api/generation/{tid}/commit',headers=h,json=commit)
        assert r.status_code==200,r.text
        assert r.json()['semantic_sync']=='running'
        for _ in range(100):
            status=c.get('/api/sync').json()
            if not status['coverage']['busy']:break
            time.sleep(.005)
        assert status['coverage']['status']=='synced'
        again=c.post(f'/api/generation/{tid}/commit',headers=h,json=commit).json()
        assert again['already_committed'] and 'sync_job' not in again
        assert c.get('/api/manuscript').json()['text']==SOURCE[:2]+NEW+SOURCE[18:]
