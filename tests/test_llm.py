import asyncio
import json
import httpx
import pytest

from noveltool.domain import ProjectConfig
from noveltool.llm import LLMClient, LLMError, MAX_RESPONSE_BYTES


def envelope(text='你好',finish='stop'):
    return {'choices':[{'message':{'role':'assistant','content':text},'finish_reason':finish}],
            'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}}


def call(handler,*,config=None,max_tokens=128,messages=None):
    runs=[]
    async def record(run):runs.append(run)
    async def scenario():
        async with LLMClient(config or ProjectConfig(writer_model='model'),on_run=record,
                             transport=httpx.MockTransport(handler)) as client:
            return await client.complete(model='model',messages=messages or [{'role':'user','content':'你好'}],max_tokens=max_tokens)
    return scenario,runs


def test_request_contract_and_secret_redaction(monkeypatch):
    secret='sk-test-secret-value'
    monkeypatch.setenv('NOVELTOOL_API_KEY',secret)
    captured=[]
    def handler(req):
        captured.append(req)
        assert req.url.path=='/v1/chat/completions'
        assert req.headers['Authorization']=='Bearer '+secret
        payload=json.loads(req.content)
        assert payload['stream'] is False and 'response_format' not in payload
        return httpx.Response(200,json=envelope('服务器错误地回显 '+secret))
    scenario,runs=call(handler)
    result=asyncio.run(scenario())
    assert result.text=='服务器错误地回显 [REDACTED]'
    assert runs[0].status=='ok' and secret not in str(runs)
    assert 'Authorization' not in runs[0].request_json
    assert len(captured)==1


@pytest.mark.parametrize('response,code',[
    (httpx.Response(401,json={'error':'no'}),'http_error'),
    (httpx.Response(302,headers={'location':'http://example.invalid/steal'}),'http_error'),
    (httpx.Response(200,text='not-json'),'protocol'),
    (httpx.Response(200,json={'choices':[]}),'protocol'),
    (httpx.Response(200,json=envelope('half','length')),'truncated'),
    (httpx.Response(200,json=envelope('half',None)),'unfinished'),
    (httpx.Response(200,json=envelope('')),'empty'),
    (httpx.Response(200,json={'choices':[{'message':{'refusal':'no'},'finish_reason':'stop'}]}),'refusal'),
    (httpx.Response(200,content=b'x'*(MAX_RESPONSE_BYTES+1)),'response_too_large'),
])
def test_bad_responses_are_bounded_and_logged(response,code):
    attempts=[]
    def handler(req):attempts.append(req);return response
    scenario,runs=call(handler)
    with pytest.raises(LLMError) as exc:asyncio.run(scenario())
    assert exc.value.code==code
    assert len(attempts)==1
    assert runs[0].status=='failed' and runs[0].error_code==code
    if code=='truncated':assert exc.value.partial_text=='half'


@pytest.mark.parametrize('error,code',[(httpx.ReadTimeout('bad'),'timeout'),(httpx.ConnectError('bad'),'connection')])
def test_transport_errors(error,code):
    def handler(req):raise error
    scenario,runs=call(handler)
    with pytest.raises(LLMError) as exc:asyncio.run(scenario())
    assert exc.value.code==code and runs[0].error_code==code


def test_logging_disabled_still_records_safe_metadata():
    scenario,runs=call(lambda _:httpx.Response(200,json=envelope('不保留小说文字')),
                       config=ProjectConfig(retain_llm_logs=False))
    result=asyncio.run(scenario())
    assert result.text=='不保留小说文字'
    assert runs[0].request_json==runs[0].raw_response==''
    assert runs[0].retained==0


def test_context_budget_rejected_without_network():
    calls=[]
    scenario,runs=call(lambda req:calls.append(req),config=ProjectConfig(context_window=2048),
                       messages=[{'role':'user','content':'中'*2000}])
    with pytest.raises(LLMError) as exc:asyncio.run(scenario())
    assert exc.value.code=='context_budget'
    assert not calls and not runs


def test_cancellation_logs_and_closes():
    async def handler(req):raise asyncio.CancelledError()
    scenario,runs=call(handler)
    with pytest.raises(asyncio.CancelledError):asyncio.run(scenario())
    assert runs[0].status=='cancelled'
