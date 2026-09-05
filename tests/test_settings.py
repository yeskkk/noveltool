import asyncio
import json
import httpx
import pytest
from noveltool.db import ProjectStore, SaveFailedError
from noveltool.runtime import ProjectSession
from noveltool.settings import ProfileWrite, EntryWrite, ReviewWrite
from noveltool.manuscript import ManuscriptError, RevisionConflictError
from test_analysis import session_ready, finding, SOURCE
from test_llm import envelope

async def add_person(s,name='林'):
    v=await s.knowledge.view()
    v=await s.settings.put_profile(ProfileWrite(expected_version=v['version'],name=name,kind='character'))
    return next(e['id'] for e in v['entities'] if e['name']==name)

async def entry(s,kind,payload,at=None,replaces=None,eid=None):
    v=await s.knowledge.view()
    return await s.settings.put_entry(EntryWrite(expected_version=v['version'],kind=kind,payload=payload,
                         at_cp=at,replaces=replaces or []),eid)


def test_profiles_entries_reopen_and_no_invented_events(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            eid=await add_person(s)
            v=await entry(s,'fact',{'entity_id':eid,'field':'职业','value':'银行职员'})
            assert v['entities'][0]['facts'][0]['manual_override']
            assert not v['events']
            await entry(s,'note',{'kind':'planning','text':'计划让林下一章去法院。'})
            with pytest.raises(ManuscriptError):await entry(s,'event',{'summary':'林去了法院。','participants':[eid]})
            assert not (await s.knowledge.view())['events']
            await s.import_manuscript(SOURCE,0)
            v=await entry(s,'event',{'summary':'林去了法院。','participants':[eid],'story_order':2.0},len(SOURCE))
            version=v['version']
        finally:await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            v=await s.knowledge.view()
            assert v['version']==version
            assert v['events'][0]['participants']==[eid] and len(v['entries'])==3
            assert s.settings.profiles[eid].name=='林'
        finally:await s.close()
    asyncio.run(scenario())


def test_manual_override_survives_analysis_and_text_changes(project_path):
    async def scenario():
        s=await session_ready(project_path)
        data=finding();data['facts'][0].update(field='职业',value='律师',mode='stable')
        tr=httpx.MockTransport(lambda req:httpx.Response(200,json=envelope(json.dumps(data))))
        try:
            await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=tr)
            eid=(await s.knowledge.view())['entities'][0]['id']
            v=await entry(s,'fact',{'entity_id':eid,'field':'职业','value':'银行职员'})
            assert eid in s.settings.profiles  # Auto person pinned by human reference.
            await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=tr)
            v=await s.knowledge.view()
            assert v['entities'][0]['facts'][0]['values'][0]['value']=='银行职员'
            assert v['entities'][0]['facts'][0]['automatic_values'][0]['value']=='律师'
            await s.replace_manuscript(0,len(SOURCE),'全新正文',1)
            v=await s.knowledge.view()
            assert v['entities'][0]['facts'][0]['values'][0]['value']=='银行职员'
        finally:await s.close()
    asyncio.run(scenario())


def test_alias_merge_is_explicit_and_profile_collision_rejected(project_path):
    async def scenario():
        s=await session_ready(project_path)
        result=finding();result['entities'].append({**result['entities'][0],'name':'林先生'})
        result['facts'].append({**result['facts'][0],'subject':'林先生','field':'目标','value':'找律师'})
        try:
            await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=httpx.MockTransport(lambda req:httpx.Response(200,json=envelope(json.dumps(result)))))
            v=await s.knowledge.view();assert len(v['entities'])==2
            eid=next(e['id'] for e in v['entities'] if e['name']=='林')
            v=await s.settings.put_profile(ProfileWrite(expected_version=v['version'],name='林',kind='character',aliases=['林先生']),eid)
            assert len(v['entities'])==1 and len(v['entities'][0]['states'])==2
            with pytest.raises(ManuscriptError,match='占用'):
                await s.settings.put_profile(ProfileWrite(expected_version=v['version'],name='另一人',kind='character',aliases=['林先生']))
        finally:await s.close()
    asyncio.run(scenario())


def test_anchored_state_stales_then_can_relocate_with_old_sources(project_path):
    async def scenario():
        s=await session_ready(project_path)
        try:
            await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=httpx.MockTransport(lambda req:httpx.Response(200,json=envelope(json.dumps(finding())))))
            v=await s.knowledge.view();eid=v['entities'][0]['id']
            oid=next(r['id'] for r in v['observations'] if r['kind']=='fact')
            v=await entry(s,'state',{'entity_id':eid,'field':'位置','value':'住处','operation':'set'},len(SOURCE),[oid])
            manual=v['entries'][0]
            assert len(v['entities'][0]['states'])==1
            await s.replace_manuscript(0,1,'周',1)
            v=await s.knowledge.view()
            assert v['entries'][0]['anchor_stale'] and not v['entities'][0]['states']
            v=await entry(s,'state',manual['payload'],len(SOURCE),[oid],manual['id'])
            assert not v['entries'][0]['anchor_stale'] and v['entities'][0]['states'][0]['value']=='住处'
            await s.replace_manuscript(0,1,'李',2)
            v=await s.knowledge.view()
            await s.settings.disable_entry(manual['id'],v['version'])
            assert not s.settings.entries[manual['id']].active
        finally:await s.close()
    asyncio.run(scenario())


def test_repaired_review_accept_reject_restore(project_path):
    async def scenario():
        s=await session_ready(project_path)
        try:
            r=await s.analysis.run_chunk(s.imports.last_plan.id,0,1,transport=httpx.MockTransport(lambda req:httpx.Response(200,json=envelope(repr(finding()).replace("None", "null")))))
            v=await s.knowledge.view();assert not v['entities']
            ids=[o['id'] for o in r['observations']]
            v=await s.settings.review(ReviewWrite(expected_version=v['version'],observation_ids=ids,decision='accepted'))
            assert len(v['entities'])==1 and v['entities'][0]['states']
            state=next(o['id'] for o in r['observations'] if o['kind']=='fact')
            v=await s.settings.review(ReviewWrite(expected_version=v['version'],observation_ids=[state],decision='rejected'))
            assert not v['entities'][0]['states'] and any(o['status']=='rejected' for o in v['observations'])
            v=await s.settings.review(ReviewWrite(expected_version=v['version'],observation_ids=[state],decision='accepted'))
            assert v['entities'][0]['states']
        finally:await s.close()
    asyncio.run(scenario())


def test_stale_versions_duplicate_slots_and_atomic_failure(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            old=(await s.knowledge.view())['version'];eid=await add_person(s)
            with pytest.raises(RevisionConflictError):await s.settings.put_profile(ProfileWrite(expected_version=old,name='周',kind='character'))
            await entry(s,'fact',{'entity_id':eid,'field':'职业','value':'银行职员'})
            with pytest.raises(ManuscriptError,match='已有'):await entry(s,'fact',{'entity_id':eid,'field':'职业','value':'律师'})
            before=(await s.knowledge.view())['version']
            s.store.connection.execute("CREATE TRIGGER fail_setting BEFORE INSERT ON setting_entries BEGIN SELECT RAISE(ABORT,'test'); END")
            with pytest.raises(SaveFailedError):await entry(s,'note',{'kind':'style','text':'简洁'})
            assert (await s.knowledge.view())['version']==before and len(s.settings.entries)==1
            s.store.connection.execute('DROP TRIGGER fail_setting')
        finally:await s.close()
    asyncio.run(scenario())


def test_settings_routes_validation_history_and_unicode(client,write_headers):
    assert client.get('/settings').status_code==200 and client.get('/timeline').status_code==200
    assert client.get('/static/settings.js').status_code==200
    v=client.get('/api/settings').json()
    assert client.post('/api/settings/entities',json={'expected_version':v['version'],'name':'林','kind':'character'}).status_code==403
    r=client.post('/api/settings/entities',headers=write_headers,json={'expected_version':v['version'],'name':'林','kind':'character'})
    assert r.status_code==200;r=r.json();eid=r['entities'][0]['id']
    malformed=[{'kind':'fact','payload':{'entity_id':eid,'field':'职业','value':7}},
               {'kind':'relationship','payload':{'a':eid,'b':eid,'label':'自己','description':'自己'}},
               {'kind':'event','payload':{'summary':'计划发生'},'at_cp':None},
               {'kind':'note','payload':{'kind':'style','text':'\ud800'}}]
    for invalid in malformed:
        bad=client.post('/api/settings/entries',headers={**write_headers,'Content-Type':'application/json'},content=json.dumps({'expected_version':r['version'],**invalid},ensure_ascii=True))
        assert bad.status_code==422,bad.text
    assert len(client.get('/api/settings/history').json()['changes'])==1
    client.post('/api/manuscript/import',headers=write_headers,json={'text':'甲😀\n乙','expected_revision_no':0})
    pos=client.get('/api/settings/position?line=1&edge=end').json()
    assert pos['at_cp']==2
    assert client.get('/api/settings/position?line=2&edge=start').json()['at_cp']==3
    assert client.get('/api/settings/position?line=99').status_code==422
    assert client.post('/api/settings/entries',headers=write_headers,json={'expected_version':r['version'],'kind':'note','payload':{'kind':'style','text':'简洁'}}).status_code==409


def test_corrupt_manual_records_quarantined(project_path):
    async def scenario():
        s=ProjectSession(ProjectStore.open(project_path));await add_person(s);await s.import_manuscript(SOURCE,0)
        s.store.connection.execute("UPDATE setting_entities SET record_json='{}'");await s.close()
        s=ProjectSession(ProjectStore.open(project_path))
        try:
            v=await s.knowledge.view();assert v['errors'] and not v['entities']
            assert s.manuscript.text==SOURCE
        finally:await s.close()
    asyncio.run(scenario())
