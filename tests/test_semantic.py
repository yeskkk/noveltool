import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from noveltool.app import create_app
from noveltool.chunker import PlanSettings, verify_plan
from noveltool.db import ProjectStore
from noveltool.runtime import ProjectSession
from noveltool.llm import LLMError
from noveltool.manuscript import RevisionConflictError
from test_analysis import session_ready, SOURCE, prepare
from test_analysis_jobs import auto_response
from test_llm import envelope

def response(req):
    data=json.loads(req.content);body=json.loads(data['messages'][-1]['content'])
    core=body['core_blocks']
    # A separate paragraph separator may be B001. Model evidence must name a
    # NONBLANK actual prose slice, not invent content in a whitespace block.
    body['core_blocks']={k:v for k,v in core.items() if v.strip()}
    if not body['core_blocks']:
        props=body['schema']['properties']
        return httpx.Response(200,json=envelope(json.dumps({k:[] for k in props})))
    data['messages'][-1]['content']=json.dumps(body,ensure_ascii=False)
    return auto_response(httpx.Request('POST',str(req.url),json=data))

TR=httpx.MockTransport(response)

async def analyze(s):
    await s.jobs.start(s.imports.last_plan.id,s.manuscript.revision_no,['facts','links','narrative'],True,transport=TR)
    await s.jobs.task


def test_append_reuses_old_units_and_preserves_observation_identity(project_path):
    async def scenario():
        s=await session_ready(project_path,(SOURCE+'\n\n')*120,PlanSettings(target_tokens=900,overlap_tokens=64))
        try:
            await analyze(s)
            old=await s.knowledge.view();ids={r['id'] for r in old['observations']}
            old_units=(await s.semantic.status())['selected_runs']
            assert (await s.semantic.status())['status']=='synced'
            await s.append_manuscript('林回到家中，仍然不知道传票来自谁。',1)
            current=await s.knowledge.view()
            assert ids=={r['id'] for r in current['observations']}
            assert (await s.semantic.status())['status']=='pending'
            calls=[]
            def handler(req):calls.append(req);return response(req)
            job=await s.semantic.start(2,transport=httpx.MockTransport(handler));await s.jobs.task
            assert job['reused_units']==old_units
            assert len(calls)==3
            assert (await s.semantic.status())['status']=='synced'
            assert ids<={r['id'] for r in (await s.knowledge.view())['observations']}
            verify_plan(s.imports.last_plan,s.manuscript)
            before=len(calls)
            await s.semantic.start(2,transport=httpx.MockTransport(handler));await s.jobs.task
            assert len(calls)==before
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            assert (await s.semantic.status())['status']=='synced'
            assert ids<={r['id'] for r in (await s.knowledge.view())['observations']}
        finally:await s.close()
    asyncio.run(scenario())


def test_rewrite_invalidates_whole_input_and_undo_reactivates(project_path):
    async def scenario():
        s=await session_ready(project_path,(SOURCE+'\n\n')*120,PlanSettings(target_tokens=900,overlap_tokens=64))
        try:
            await analyze(s);s.semantic.refresh_locked();old=list(s.semantic.selected)
            run=old[len(old)//2];bid=next(v['block_id'] for v in run['refs'].values() if v['scope']=='core')
            span=next(sp for sp in s.manuscript.render().spans if sp.block_id==bid)
            original=s.manuscript.text
            await s.replace_manuscript(span.start_cp,span.start_cp+1,'新',1)
            s.semantic.refresh_locked();valid={r['id'] for r in s.semantic.selected}
            for r in old:
                if any(v['block_id']==bid for v in r['refs'].values()):assert r['id'] not in valid
            assert valid
            assert (await s.semantic.status())['status']=='pending'
            await s.semantic.start(2,transport=TR);await s.jobs.task
            assert (await s.semantic.status())['status']=='synced'
            await s.undo_manuscript(2)
            assert s.manuscript.text==original
            assert (await s.semantic.status())['status']=='synced'
        finally:await s.close()
    asyncio.run(scenario())


def test_overlap_context_change_invalidates_next_chunk(project_path):
    async def scenario():
        s=await session_ready(project_path,(SOURCE+'\n\n')*120,PlanSettings(target_tokens=600,overlap_tokens=100))
        try:
            await analyze(s);s.semantic.refresh_locked()
            run=next(r for r in s.semantic.eligible if any(v['scope']=='overlap' for v in r['refs'].values()))
            p=next(v for v in run['refs'].values() if v['scope']=='overlap')
            sp=next(sp for sp in s.manuscript.render().spans if sp.block_id==p['block_id'])
            await s.replace_manuscript(sp.start_cp+p['start_cp'],sp.start_cp+p['start_cp']+1,'改',1)
            s.semantic.refresh_locked()
            assert run['id'] not in {r['id'] for r in s.semantic.eligible}
        finally:await s.close()
    asyncio.run(scenario())


def test_new_empty_success_supersedes_old_facts_and_bad_retry_does_not(project_path):
    async def scenario():
        s=await session_ready(project_path)
        try:
            await analyze(s)
            before=(await s.knowledge.view())['record_count']
            pid=s.imports.last_plan.id
            empty={'entities':[],'facts':[],'events':[]}
            await s.analysis.run_chunk(pid,0,1,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope(json.dumps(empty)))))
            assert (await s.knowledge.view())['record_count']<before
            await s.append_manuscript('新一段。',1)
            assert not any(r['kind']=='fact' for r in (await s.knowledge.view())['observations'])
            old=s.manuscript.text
            await s.semantic.start(2,transport=httpx.MockTransport(lambda r:httpx.Response(503)))
            await s.jobs.task
            assert s.manuscript.text==old and (await s.semantic.status())['status']=='pending'
            await s.semantic.start(2,transport=TR);await s.jobs.task
            assert (await s.semantic.status())['status']=='synced'
        finally:await s.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value',[('refs_json','{}'),('refs_json','not json'),('core_hash','f'*64)])
def test_invalid_run_provenance_is_quarantined(project_path,field,value):
    async def scenario():
        s=await session_ready(project_path)
        try:
            await analyze(s)
            s.store.connection.execute(f"UPDATE analysis_runs SET {field}=? WHERE pass_type='facts'",(value,));s.analysis.epoch+=1
            state=await s.semantic.status()
            assert state['coverage']['facts']==0 and state['excluded_runs']>=1
            assert not any(r['kind']=='fact' for r in (await s.knowledge.view())['observations'])
        finally:await s.close()
    asyncio.run(scenario())


def test_sync_rejects_old_revision_and_stops_if_text_changes(project_path):
    async def scenario():
        s=await session_ready(project_path,SOURCE*20,PlanSettings(target_tokens=800,overlap_tokens=64))
        try:
            with pytest.raises(RevisionConflictError):await s.semantic.start(0,transport=TR)
            async def handler(req):
                await s.append_manuscript('变化。',1)
                return response(req)
            await s.semantic.start(1,transport=httpx.MockTransport(handler));await s.jobs.task
            assert (await s.jobs.view())['job']['status']=='stale'
            assert (await s.semantic.status())['status']=='pending'
        finally:await s.close()
    asyncio.run(scenario())


def test_context_config_change_requires_calls_not_silent_reuse(project_path):
    async def scenario():
        from noveltool.domain import ProjectConfig
        s=await session_ready(project_path)
        try:
            await analyze(s)
            await s.update_config(s.project.data.config.model_copy(update={'analysis_model':'another'}),s.project.data.meta.data_version)
            calls=[]
            def handler(req):calls.append(req);return response(req)
            job=await s.semantic.start(1,transport=httpx.MockTransport(handler));await s.jobs.task
            assert job['reused_units']==0 and len(calls)==3
        finally:await s.close()
    asyncio.run(scenario())


def test_sync_routes_guard_and_completion(project_path):
    import time
    with TestClient(create_app(project_path,llm_transport=TR),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};prepare(c,h)
        assert c.get('/sync').status_code==200
        assert c.post('/api/sync',json={'expected_revision_no':1}).status_code==403
        assert c.post('/api/sync',headers=h,json={'expected_revision_no':1}).status_code==202
        for _ in range(100):
            state=c.get('/api/sync').json()
            if not state['coverage']['busy']:break
            time.sleep(.005)
        assert state['coverage']['status']=='synced'


def test_reused_observation_can_still_be_reviewed_without_reanalysis(project_path):
    from noveltool.settings import ReviewWrite
    async def scenario():
        s=await session_ready(project_path)
        try:
            await analyze(s);old=await s.knowledge.view();oid=old['observations'][0]['id']
            await s.append_manuscript('后来的正文。',1)
            g=await s.knowledge.view()
            updated=await s.settings.review(ReviewWrite(expected_version=g['version'],observation_ids=[oid],decision='accepted'))
            assert next(r for r in updated['observations'] if r['id']==oid)['status']=='accepted'
            await s.semantic.start(2,transport=TR);await s.jobs.task
            assert next(r for r in (await s.knowledge.view())['observations'] if r['id']==oid)['status']=='accepted'
        finally:await s.close()
    asyncio.run(scenario())


def test_modified_chunk_then_many_edits_plan_remains_lossless(project_path):
    import random
    async def scenario():
        s=await session_ready(project_path,(SOURCE+'\n\n')*80,PlanSettings(target_tokens=650,overlap_tokens=64))
        rng=random.Random(140)
        try:
            await analyze(s)
            for _ in range(12):
                original=s.manuscript.text;a=rng.randrange(len(original));b=min(len(original),a+rng.randrange(1,15))
                await s.replace_manuscript(a,b,'新文字😀\n\n',s.manuscript.revision_no)
                await s.semantic.start(s.manuscript.revision_no,transport=TR);await s.jobs.task
                verify_plan(s.imports.last_plan,s.manuscript)
                assert s.manuscript.text==original[:a]+'新文字😀\n\n'+original[b:]
                assert (await s.semantic.status())['status']=='synced'
        finally:await s.close()
    asyncio.run(scenario())
