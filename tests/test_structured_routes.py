import json
import httpx
from fastapi.testclient import TestClient
from noveltool.app import create_app
from noveltool.db import ProjectStore
from test_llm import envelope
from test_llm_routes import configure
from test_structured_llm import finding,SOURCE


def test_local_check_is_offline_and_changes_nothing(client,write_headers):
    before=client.get('/api/status').json()
    result=client.post('/api/llm/validate',headers=write_headers,json={'source':SOURCE,'raw_output':repr(finding())})
    assert result.status_code==200,result.text
    assert result.json()['requires_review']
    after=client.get('/api/status').json()
    assert before['memory_version']==after['memory_version']
    assert client.get('/api/llm/runs').json()['runs']==[]
    assert 'required' in client.get('/api/llm/fact-schema').json()


def test_structured_request_persists_validation_with_llm_run(project_path):
    handler=lambda _:httpx.Response(200,json=envelope(json.dumps(finding(),ensure_ascii=False)))
    with TestClient(create_app(project_path,llm_transport=httpx.MockTransport(handler)),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};configure(c,h)
        result=c.post('/api/llm/structured-test',headers=h,json={'source':SOURCE})
        assert result.status_code==200,result.text
        assert result.json()['value']==finding()
        assert c.get('/api/manuscript').json()['revision_no']==0
        assert c.get('/api/llm/runs').json()['runs'][0]['validation_status']=='ok'
        c.post('/api/save',headers=h)
    with ProjectStore.open(project_path) as s:
        row=s.connection.execute('SELECT * FROM llm_runs').fetchone()
        assert row['validation_status']=='ok'
        assert json.loads(row['parsed_json'])==finding()


def test_failed_evidence_logs_no_canonical_changes(project_path):
    data=finding();data['facts'][0]['evidence'][0]['block']='B999'
    attempts=[]
    def handler(req):attempts.append(req);return httpx.Response(200,json=envelope(json.dumps(data)))
    with TestClient(create_app(project_path,llm_transport=httpx.MockTransport(handler)),base_url='http://127.0.0.1') as c:
        h={'X-Noveltool-Token':c.get('/api/session').json()['csrf_token']};configure(c,h)
        result=c.post('/api/llm/structured-test',headers=h,json={'source':SOURCE})
        assert result.status_code==422
        assert result.json()['code']=='structured_semantic'
        assert len(attempts)==1
        assert c.get('/api/llm/runs').json()['runs'][0]['validation_status']=='failed'
    with ProjectStore.open(project_path) as s:
        row=s.connection.execute('SELECT * FROM llm_runs').fetchone()
        assert row['parsed_json'] is None and 'B999' in row['validation_error']
