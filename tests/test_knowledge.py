import asyncio
import json
import httpx
from noveltool.knowledge import build_graph, normalize_name
from test_analysis import session_ready,finding,SOURCE
from test_llm import envelope


def rec(id,kind,payload,at=10,review=False):
    return {'id':id,'kind':kind,'payload':payload,'at_cp':at,'evidence':[], 'requires_review':review,'status':'pending'}

def test_conservative_name_merge_conflicts_and_direction():
    records=[rec('1','entity',{'name':'Ｋ','kind':'character'}),rec('2','entity',{'name':'k','kind':'character'}),
             rec('3','entity',{'name':'律师','kind':'character'}),
             rec('4','fact',{'subject':'K','field':'职业','value':'银行职员','mode':'stable'}),
             rec('5','fact',{'subject':'K','field':'职业','value':'律师','mode':'stable'}),
             rec('6','fact',{'subject':'K','field':'职业','value':'银行职员','mode':'stable'}),
             rec('7','fact',{'subject':'K','field':'目标','value':'离开','mode':'state'},20),
             rec('8','relationship',{'a':'K','b':'律师','label':'信任','description':'K 信任律师'}),
             rec('9','relationship',{'a':'律师','b':'K','label':'信任','description':'律师信任K'})]
    graph=build_graph('a'*32,records)
    assert len(graph['entities'])==2
    k=next(e for e in graph['entities'] if e['name']=='Ｋ')
    assert k['facts'][0]['conflict'] and len(k['facts'][0]['values'])==2
    assert len(k['facts'][0]['values'][0]['observation_ids'])==2
    assert graph['relationships'][0]['a']!=graph['relationships'][1]['a']
    assert k['states'][0]['at_cp']==20
    assert normalize_name('K先生')!=normalize_name('K')


def test_uncertain_repaired_and_ambiguous_stay_pending():
    rows=[rec('1','entity',{'name':'城堡','kind':'location'}),rec('2','entity',{'name':'城堡','kind':'item'}),
          rec('3','fact',{'subject':'城堡','field':'主人','value':'K','mode':'stable'}),
          rec('4','entity',{'name':'修复的人','kind':'character'},review=True),
          rec('5','entity',{'name':'甲','kind':'character'}),
          rec('6','fact',{'subject':'甲','field':'身份','value':'凶手','mode':'uncertain'})]
    g=build_graph('a'*32,rows)
    assert len(g['entities'])==3 and len(g['review_queue'])==3
    assert not any(e['facts'] for e in g['entities'])


def test_failed_retry_preserves_success_and_corruption_isolated(project_path):
    async def scenario():
        s=await session_ready(project_path)
        try:
            pid=s.imports.last_plan.id
            good=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope(json.dumps(finding()))))
            await s.analysis.run_chunk(pid,0,1,transport=good)
            old=await s.knowledge.view()
            try:await s.analysis.run_chunk(pid,0,1,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('{}'))))
            except Exception:pass
            assert (await s.knowledge.view())['entities']==old['entities']
            s.store.connection.execute("UPDATE observations SET evidence_json='[]' WHERE kind='fact'")
            s.analysis.epoch+=1
            new=await s.knowledge.view()
            assert len(new['errors'])==1 and not new['entities'][0]['states']
            assert s.manuscript.text==SOURCE
        finally:await s.close()
    asyncio.run(scenario())
