import asyncio
from copy import deepcopy
import json
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
import pytest

from noveltool.llm import Completion, LLMError
from noveltool.llm_schemas import FactExtractionResult
from noveltool.structured_llm import (StructuredLLM, StructuredError, extract_json_candidate,
    parse_and_validate, validate_evidence)

SOURCE='K把钥匙交给B。B把钥匙放进外套口袋。'


def finding():
    evidence=[{'block':'B001','quote':'K把钥匙交给B。'}]
    return {'entities':[{'name':'K','kind':'character','evidence':evidence},
                        {'name':'B','kind':'character','evidence':evidence}],
            'facts':[{'subject':'B','field':'持有物','value':'钥匙','mode':'state','evidence':evidence}],
            'events':[]}


def validate(raw):
    return parse_and_validate(raw,FactExtractionResult,lambda x:validate_evidence(x,{'B001':SOURCE}))


class FakeLLM:
    def __init__(self,outputs):self.outputs=list(outputs);self.calls=[]
    async def complete(self,**kwargs):
        self.calls.append(kwargs)
        output=self.outputs.pop(0)
        if isinstance(output,BaseException):raise output
        return Completion(output,'{}',uuid4().hex,'stop',{})


@pytest.mark.parametrize('wrap',[lambda s:s,lambda s:'```json\n'+s+'\n```',lambda s:'以下结果：\n'+s+'\n结束。'])
def test_normal_and_wrapped_json(wrap):
    data=finding();value,repairs=validate(wrap(json.dumps(data,ensure_ascii=False)))
    assert value.model_dump()==data


@pytest.mark.parametrize('raw',['{"entities":[],"facts":[],"events":[],}', "{'entities':[],'facts':[],'events':[]}"])
def test_local_repairs_are_visible(raw):
    result,repairs=validate(raw)
    assert result.entities==[] and 'local_repaired' in repairs


@pytest.mark.parametrize('raw,code',[
    ('{"entities":[],"entities":[],"facts":[],"events":[]}','duplicate_key'),
    ("{'entities':[],'entities':[],'facts':[],'events':[],}",'duplicate_key'),
    ('{"entities":[],"facts":[],"events":[]}\n{"entities":[],"facts":[],"events":[]}','ambiguous'),
    ('{"entities":[],"facts":[','unbalanced'),
    ('{"entities":[],"facts":[],"events":[]}}\n{','unbalanced'),
    ('{"entities":[],"facts":[],"events":NaN}','invalid_constant'),
    ('{"entities":[],"facts":[],"events":Infinity}','invalid_constant'),
    ('['*34+']'*34,'too_deep'),
    ('x'*100001,'output_too_large'),
    ('{}','schema'),
    ('{"entities":[],"facts":[]}','schema'),
])
def test_unsafe_outputs_rejected(raw,code):
    with pytest.raises(StructuredError) as exc:validate(raw)
    assert exc.value.code==code


def test_quote_aware_balancing():
    class Value(BaseModel):value:str
    text='他说："这里有 } 和 {，还有 \\"。'
    raw='```json\n'+json.dumps({'value':text},ensure_ascii=False)+'\n```'
    candidate,wrapped=extract_json_candidate(raw)
    assert wrapped and json.loads(candidate)['value']==text


def test_no_literal_execution(tmp_path):
    target=tmp_path/'not_created'
    raw="{'entities': [], 'facts': [], 'events': __import__('pathlib').Path('"+str(target)+"').touch()}"
    with pytest.raises(StructuredError):validate(raw)
    assert not target.exists()


@pytest.mark.parametrize('change',[
    lambda d:d['entities'][0]['evidence'][0].update(block='B999'),
    lambda d:d['entities'][0]['evidence'][0].update(quote='不存在的证据'),
    lambda d:d['facts'][0].update(subject='未声明的甲'),
    lambda d:d['entities'].append(deepcopy(d['entities'][0])),
])
def test_invalid_semantics_never_repaired(change):
    data=finding();change(data);fake=FakeLLM([json.dumps(data),json.dumps(finding())])
    async def run():
        return await StructuredLLM(fake).call(messages=[],schema=FactExtractionResult,model='m',
            semantic_validator=lambda x:validate_evidence(x,{'B001':SOURCE}))
    with pytest.raises(StructuredError) as exc:asyncio.run(run())
    assert exc.value.code=='semantic' and len(fake.calls)==1


def test_generated_id_is_forbidden():
    data=finding();data['entities'][0]['id']='a'*32
    with pytest.raises(StructuredError) as exc:validate(json.dumps(data))
    assert exc.value.code=='schema'


def test_model_repair_is_bounded_and_does_not_resend_source():
    class Value(BaseModel):
        model_config=ConfigDict(strict=True,extra='forbid')
        value:str
    fake=FakeLLM(['{"value":7}','{"value":"7"}'])
    records=[]
    async def record(row):records.append(row)
    async def run():
        return await StructuredLLM(fake,on_validation=record).call(
            messages=[{'role':'user','content':'DO_NOT_RESEND_SOURCE_10983'}],schema=Value,model='small')
    result=asyncio.run(run())
    assert result.value.value=='7' and result.requires_review
    assert len(fake.calls)==2 and fake.calls[1]['temperature']==0
    assert 'DO_NOT_RESEND_SOURCE_10983' not in str(fake.calls[1]['messages'])
    assert records[0].status=='failed' and records[1].status=='repaired'


def test_repair_limit_and_preserved_bad_output():
    class Value(BaseModel):
        model_config=ConfigDict(strict=True)
        value:str
    fake=FakeLLM(['{"value":7}']*3)
    async def run():return await StructuredLLM(fake).call(messages=[],schema=Value,model='small')
    with pytest.raises(StructuredError) as exc:asyncio.run(run())
    assert len(fake.calls)==3 and len(exc.value.run_ids)==3
    assert exc.value.raw_output=='{"value":7}'


def test_missing_fields_do_not_turn_into_empty_success():
    fake=FakeLLM(['{}','{"entities":[],"facts":[],"events":[]}'])
    async def run():return await StructuredLLM(fake).call(messages=[],schema=FactExtractionResult,model='small')
    with pytest.raises(StructuredError):asyncio.run(run())
    assert len(fake.calls)==1


def test_upstream_truncation_does_not_trigger_format_repair():
    fake=FakeLLM([LLMError('truncated','not done',partial_text='{"entities":[')])
    async def run():return await StructuredLLM(fake).call(messages=[],schema=FactExtractionResult,model='small')
    with pytest.raises(LLMError):asyncio.run(run())
    assert len(fake.calls)==1


def test_repairs_are_flagged_for_review():
    fake=FakeLLM([repr(finding())])
    async def run():return await StructuredLLM(fake).call(messages=[],schema=FactExtractionResult,model='small')
    result=asyncio.run(run())
    assert result.requires_review and result.repairs==('local_repaired',)


@pytest.mark.parametrize('number',[-1,3,True,1.5])
def test_bad_retry_limit(number):
    with pytest.raises(ValueError):StructuredLLM(FakeLLM([]),max_repairs=number)
