import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from noveltool.app import create_app
from noveltool.consistency import CheckRequest, ConflictResult, IssueUpdate, build_units, check_evidence, revision_delta
from noveltool.db import ProjectStore
from noveltool.domain import ProjectConfig
from noveltool.llm import LLMError
from noveltool.manuscript import ManuscriptError, RevisionConflictError
from noveltool.runtime import ProjectSession
from noveltool.structured_llm import StructuredError
from test_generation import ready
from test_llm import envelope
from test_rewrite import SOURCE, NEW

EMPTY = httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('{"issues":[]}')))

async def prepare(path, text=SOURCE, a=2, b=18, replacement=NEW):
    s = await ready(path)
    await s.import_manuscript(text,0)
    await s.replace_manuscript(a,b,replacement,1,text[a:b])
    return s

async def done(s,jid):
    if s.consistency.task: await s.consistency.task
    return await s.consistency.view(jid)

def finding(req):
    body=json.loads(json.loads(req.content)['messages'][-1]['content'])
    refs=body['sources']
    later=next(k for k,v in refs.items() if v['role']=='later' and v['text'].strip())
    return {'issues':[{'severity':'high','title':'钥匙的来源需要核对','explanation':'新选区没有交出钥匙，但后文仍使用，可能存在其他交付途径。',
        'change_evidence':[{'block':'B002','quote':refs['B002']['text']}],
        'later_evidence':[{'block':later,'quote':refs[later]['text']}]}]}


def test_consistency_citations_immutable_body_review_and_restart(project_path):
    async def scenario():
        s=await prepare(project_path);before=s.manuscript.text;calls=[]
        def handler(r):calls.append(r);return httpx.Response(200,json=envelope(json.dumps(finding(r),ensure_ascii=False)))
        try:
            j=await s.consistency.start(CheckRequest(expected_revision_no=2),transport=httpx.MockTransport(handler))
            v=await done(s,j['job']['id']);jid=v['job']['id']
            assert v['job']['status']=='done' and v['checked_chars']==v['job']['total_chars']
            assert len(v['issues'])==len(calls)==1 and s.manuscript.text==before
            evidence=v['issues'][0]['evidence'];assert {e['role'] for e in evidence}=={'change','later'}
            iid=v['issues'][0]['id']
            await s.consistency.update_issue(iid,IssueUpdate(expected_version=0,status='ignored'))
            with pytest.raises(RevisionConflictError):await s.consistency.update_issue(iid,IssueUpdate(expected_version=0,status='resolved'))
            assert s.manuscript.revision_no==2 and (await s.knowledge.view())['entities']==[]
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            v=await s.consistency.view(jid)
            assert not v['active'] and v['issues'][0]['status']=='ignored' and v['issues'][0]['version']==1
            assert s.manuscript.text==before
            await s.consistency.resume(jid,transport=EMPTY)
            assert len(calls)==1
        finally:await s.close()
    asyncio.run(scenario())


def test_consistency_units_cover_all_long_suffix_with_budget(project_path):
    async def scenario():
        text=SOURCE+'\n\n后文。😀叙述。'*1800
        s=await prepare(project_path,text);calls=[]
        def handler(r):calls.append(json.loads(r.content));return httpx.Response(200,json=envelope('{"issues":[]}'))
        try:
            j=await s.consistency.start(CheckRequest(expected_revision_no=2),transport=httpx.MockTransport(handler));v=await done(s,j['job']['id'])
            assert len(calls)>3 and v['job']['status']=='done'
            later=''.join(v['text'] for c in calls for v in json.loads(c['messages'][-1]['content'])['sources'].values() if v['role']=='later')
            _,delta=revision_delta(s,2)
            assert later==s.manuscript.text[delta['end_cp']:] and len(later)==v['checked_chars']
            assert all(u['input_estimate']+1536<=17000 for u in v['units'])
        finally:await s.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault',['wrong_role','wrong_quote','missing_ref'])
def test_consistency_semantic_errors_fail_without_repair(project_path,fault):
    async def scenario():
        s=await prepare(project_path);calls=[]
        def handler(r):
            calls.append(1);data=finding(r);e=data['issues'][0]['later_evidence'][0]
            if fault=='wrong_role':e.update(block='B002',quote=NEW)
            elif fault=='wrong_quote':e['quote']='并不存在的原文'
            else:e['block']='B999'
            return httpx.Response(200,json=envelope(json.dumps(data,ensure_ascii=False)))
        try:
            j=await s.consistency.start(CheckRequest(expected_revision_no=2),transport=httpx.MockTransport(handler));v=await done(s,j['job']['id'])
            assert v['job']['status']=='partial' and not v['issues'] and len(calls)==1
            assert v['checked_chars']==0 and v['units'][0]['status']=='failed'
            await s.consistency.resume(j['job']['id'],transport=EMPTY);v=await done(s,j['job']['id'])
            assert v['job']['status']=='done'
        finally:await s.close()
    asyncio.run(scenario())


def test_consistency_empty_suffix_no_network_and_large_delta_reject(project_path):
    async def scenario():
        s=await prepare(project_path,a=0,b=len(SOURCE));calls=[]
        try:
            j=await s.consistency.start(CheckRequest(expected_revision_no=2),transport=httpx.MockTransport(lambda r:calls.append(r)))
            assert j['job']['status']=='not_applicable' and not calls and j['checked_chars']==0
            await s.append_manuscript('后文',2)
            with pytest.raises(ManuscriptError):await s.consistency.start(CheckRequest(expected_revision_no=3),transport=EMPTY)
            await s.replace_manuscript(0,1,'很长的改写。'*2500,3,s.manuscript.text[:1])
            with pytest.raises(LLMError,match='预算'):await s.consistency.start(CheckRequest(expected_revision_no=4),transport=EMPTY)
        finally:await s.close()
    asyncio.run(scenario())


def test_consistency_network_pause_resume_only_missing(project_path):
    async def scenario():
        s=await prepare(project_path,SOURCE+'\n\n周仍然用钥匙开门。'*900);calls=[]
        def handler(r):
            calls.append(1)
            if len(calls)==2:raise httpx.ConnectError('offline')
            return httpx.Response(200,json=envelope('{"issues":[]}'))
        try:
            j=await s.consistency.start(CheckRequest(expected_revision_no=2),transport=httpx.MockTransport(handler));v=await done(s,j['job']['id'])
            assert v['job']['status']=='paused' and v['units'][0]['status']=='done'
            n=v['job']['unit_count'];calls.clear()
            def ok(r):calls.append(1);return httpx.Response(200,json=envelope('{"issues":[]}'))
            await s.consistency.resume(j['job']['id'],transport=httpx.MockTransport(ok));v=await done(s,j['job']['id'])
            assert v['job']['status']=='done' and len(calls)==n-1
        finally:await s.close()
    asyncio.run(scenario())


def test_consistency_stale_during_network_keeps_history_and_no_edit(project_path):
    async def scenario():
        s=await prepare(project_path)
        async def handler(r):
            await s.append_manuscript('用户的新内容',2)
            return httpx.Response(200,json=envelope(json.dumps(finding(r),ensure_ascii=False)))
        try:
            j=await s.consistency.start(CheckRequest(expected_revision_no=2),transport=httpx.MockTransport(handler));v=await done(s,j['job']['id'])
            assert v['stale'] and v['job']['status']=='stale' and v['units'][0]['status']=='stale'
            assert len(v['issues'])==1 and s.manuscript.text.endswith('用户的新内容') and v['checked_chars']==0
            with pytest.raises(RevisionConflictError):await s.consistency.resume(j['job']['id'],transport=EMPTY)
        finally:await s.close()
    asyncio.run(scenario())


def test_consistency_cancel_restart_and_tampered_manifest(project_path):
    async def scenario():
        s=await prepare(project_path);started=asyncio.Event();release=asyncio.Event()
        async def handler(r):started.set();await release.wait();return httpx.Response(200,json=envelope('{"issues":[]}'))
        j=await s.consistency.start(CheckRequest(expected_revision_no=2),transport=httpx.MockTransport(handler));await started.wait()
        jid=j['job']['id'];await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            v=await s.consistency.view(jid);assert not v['active'] and v['job']['status']=='interrupted'
            s.store.connection.execute("UPDATE consistency_units SET messages_json='[]' WHERE job_id=?",(jid,))
            calls=[]
            await s.consistency.resume(jid,transport=httpx.MockTransport(lambda r:calls.append(r)));v=await done(s,jid)
            assert v['job']['status']=='stale' and not calls and v['checked_chars']==0
        finally:await s.close()
    asyncio.run(scenario())


def test_consistency_wait_for_sync_busy_and_pause(project_path):
    async def scenario():
        s=await prepare(project_path);gate=asyncio.Event();calls=[]
        async def sync():await gate.wait()
        w=asyncio.create_task(sync())
        try:
            j=await s.consistency.start(CheckRequest(expected_revision_no=2),transport=EMPTY,wait_for=w)
            assert j['job']['status']=='waiting_sync' and not s.consistency.busy
            with pytest.raises(LLMError):await s.consistency.start(CheckRequest(expected_revision_no=2),transport=EMPTY)
            await s.consistency.pause(j['job']['id']);gate.set();v=await done(s,j['job']['id'])
            assert v['job']['status']=='paused' and v['checked_chars']==0
            await s.consistency.resume(j['job']['id'],transport=EMPTY);v=await done(s,j['job']['id'])
            assert v['job']['status']=='done'
        finally:
            gate.set();await w;await s.close()
    asyncio.run(scenario())


def test_consistency_persist_failure_does_not_change_text(project_path):
    async def scenario():
        s=await prepare(project_path);text=s.manuscript.text
        try:
            s.store.connection.execute("CREATE TRIGGER fail_issue BEFORE INSERT ON consistency_issues BEGIN SELECT RAISE(ABORT,'test'); END")
            j=await s.consistency.start(CheckRequest(expected_revision_no=2),transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope(json.dumps(finding(r),ensure_ascii=False)))))
            v=await done(s,j['job']['id'])
            assert v['job']['status']=='failed' and not v['issues'] and s.manuscript.text==text
            assert v['units'][0]['status']=='failed'
            s.store.connection.execute('DROP TRIGGER fail_issue')
            await s.consistency.resume(j['job']['id'],transport=EMPTY);v=await done(s,j['job']['id']);assert v['job']['status']=='done'
        finally:await s.close()
    asyncio.run(scenario())


def test_consistency_http_evidence_history_and_input(project_path):
    from test_llm_routes import configure
    tr=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope(json.dumps(finding(r),ensure_ascii=False))))
    with TestClient(create_app(project_path,llm_transport=tr),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};configure(c,h)
        cfg=c.get('/api/config').json();cfg['config']['analysis_model']='fake'
        assert c.put('/api/config',headers=h,json={'expected_memory_version':cfg['memory_version'],'config':cfg['config']}).status_code==200
        c.post('/api/manuscript/import',headers=h,json={'text':SOURCE,'expected_revision_no':0})
        c.portal.call(c.app.state.session.replace_manuscript,2,18,NEW,1,SOURCE[2:18])
        r=c.post('/api/consistency',headers=h,json={'expected_revision_no':2});assert r.status_code==202,r.text
        jid=r.json()['job']['id'];c.portal.call(lambda:c.app.state.session.consistency.task)
        v=c.get(f'/api/consistency/{jid}').json();assert len(v['issues'])==1
        assert c.get('/consistency').status_code==200
        assert c.get('/api/consistency').json()['jobs'][0]['id']==jid
        inp=c.get(f'/api/consistency/{jid}/input/0').json();assert inp['refs']['B002']['text']==NEW
        iid=v['issues'][0]['id']
        assert c.put(f'/api/consistency/issues/{iid}',headers=h,json={'expected_version':0,'status':'resolved'}).status_code==200
        assert c.put(f'/api/consistency/issues/{iid}',headers=h,json={'expected_version':0,'status':'open'}).status_code==409
        assert c.get(f'/api/consistency/{jid}/input/999').status_code==422
        assert c.get('/api/manuscript').json()['text']==SOURCE[:2]+NEW+SOURCE[18:]
