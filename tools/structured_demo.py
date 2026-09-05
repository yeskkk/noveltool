"""Offline deterministic format-check demonstration, no model or API needed."""
import json
from noveltool.llm_schemas import FactExtractionResult
from noveltool.structured_llm import parse_and_validate, StructuredError, validate_evidence

source='K把钥匙交给B。'
data={'entities':[{'name':'K','kind':'character','evidence':[{'block':'B001','quote':source}]}],
      'facts':[],'events':[]}
value,repairs=parse_and_validate(repr(data),FactExtractionResult,lambda x:validate_evidence(x,{'B001':source}))
print('修复标记：',repairs)
print(json.dumps(value.model_dump(),ensure_ascii=False,indent=2))
data['entities'][0]['evidence'][0]['block']='B999'
try:
    parse_and_validate(json.dumps(data),FactExtractionResult,lambda x:validate_evidence(x,{'B001':source}))
except StructuredError as exc:
    print('坏引用已被拒绝：',exc.code,str(exc))
else:
    raise AssertionError('bad evidence should not pass')
