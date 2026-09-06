"""Regression cases for real-world truncation, not just well-formed JSON."""
import asyncio
import json

import httpx
import pytest

from noveltool.domain import ProjectConfig
from noveltool.llm import LLMClient, LLMError
from noveltool.llm_schemas import FactExtractionResult
from noveltool.structured_llm import StructuredError, StructuredLLM
from test_llm import envelope

GOOD = '{"entities":[],"facts":[],"events":[]}'


def run(handler, **config):
    calls, logs = [], []
    async def record(item):
        logs.append(item)
    def transport(req):
        payload = json.loads(req.content)
        calls.append(payload)
        return handler(payload, len(calls))
    async def work():
        async with LLMClient(ProjectConfig(analysis_model='small', **config), on_run=record,
                             transport=httpx.MockTransport(transport)) as client:
            return await StructuredLLM(client).call(messages=[{'role':'user','content':'只抽取一句话。'}],
                model='small', schema=FactExtractionResult, max_tokens=512)
    return work, calls, logs


@pytest.mark.parametrize('reason', ['length', 'stop'])
def test_budget_retry_is_fresh_request_not_tail_stitching(reason):
    def response(payload, n):
        return httpx.Response(200, json=envelope('{"entities":[', reason) if n == 1 else envelope(GOOD))
    work, calls, logs = run(response)
    result = asyncio.run(work())
    assert [c['max_tokens'] for c in calls] == [512, 1024]
    assert calls[0]['messages'] == calls[1]['messages']
    assert result.original_output == '{"entities":['
    assert result.value.model_dump() == {'entities':[], 'facts':[], 'events':[]}
    assert result.requires_review and 'output_regenerated' in result.repairs
    assert len(result.run_ids) == 2
    assert logs[0].requested_max_tokens == 512
    assert json.loads(logs[0].usage_json)['completion_tokens'] == 2


def test_retry_ceiling_and_count_are_real_limits():
    work, calls, logs = run(lambda _, n: httpx.Response(200, json=envelope('{"entities":[')),
                           structured_output_ceiling=700, output_retry_limit=2)
    with pytest.raises(StructuredError, match='未完整'):
        asyncio.run(work())
    assert [c['max_tokens'] for c in calls] == [512, 700]
    assert all(log.requested_max_tokens <= 700 for log in logs)


def test_usage_is_retained_even_if_only_reasoning_used_the_output_cap():
    async def work():
        logs=[]
        async def record(log): logs.append(log)
        response = envelope(None, 'length')
        response['choices'][0]['message']['reasoning_content'] = 'not a final answer'
        response['usage'] = {'completion_tokens':512, 'prompt_tokens':100,
                             'completion_tokens_details':{'reasoning_tokens':512}}
        async with LLMClient(ProjectConfig(retain_llm_logs=False), on_run=record,
                transport=httpx.MockTransport(lambda r:httpx.Response(200,json=response))) as client:
            with pytest.raises(LLMError) as error:
                await client.complete(messages=[{'role':'user','content':'hi'}],model='small', max_tokens=512)
        assert error.value.code == 'truncated'
        assert error.value.usage['completion_tokens_details']['reasoning_tokens'] == 512
        assert '单次输出上限' in str(error.value)
        assert logs[0].raw_response == ''
        assert json.loads(logs[0].usage_json)['completion_tokens'] == 512
    asyncio.run(work())


def test_http_errors_and_fabricated_evidence_are_not_length_retried():
    work,calls,logs=run(lambda _,n:httpx.Response(503))
    with pytest.raises(LLMError): asyncio.run(work())
    assert len(calls)==1


def test_complete_json_does_not_trigger_output_retry():
    work,calls,logs=run(lambda _,n:httpx.Response(200,json=envelope(GOOD)))
    result=asyncio.run(work())
    assert not result.repairs and len(calls)==1


def test_m18_config_and_diagnostics_roundtrip(project_path):
    from noveltool.db import ProjectStore
    from noveltool.runtime import ProjectSession
    async def work():
        s=ProjectSession(ProjectStore.open(project_path))
        await s.update_config(ProjectConfig(structured_output_ceiling=12000, output_retry_limit=2),
                              s.project.data.meta.data_version)
        await s.close()
        with ProjectStore.open(project_path) as store:
            assert store.load().config.structured_output_ceiling==12000
    asyncio.run(work())
