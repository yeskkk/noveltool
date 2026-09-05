"""Offline import/chunking demo: no model, no user database."""
from noveltool.import_text import decode_import
from noveltool.chunker import make_plan, verify_plan, PlanSettings, slice_text
from noveltool.manuscript import Manuscript,Block,split_text
raw=('第一章\r\n\r\n'+('　甲把钥匙交给乙。乙向城门走去。\r\n'*10000)).encode('utf-16')
p=decode_import(raw,'sample.txt')
doc=Manuscript(1,tuple(Block.new(t,'demo') for t in split_text(p.text,'blankline')))
plan=make_plan(doc,PlanSettings(),20000,.85)
verify_plan(plan,doc)
blocks={b.id:b.text for b in doc.blocks}
assert ''.join(slice_text(part,blocks) for chunk in plan.chunks for part in chunk.core)==p.text
print(f'PASS: {len(p.text)} Unicode characters, {len(plan.chunks)} chunks; lossless core coverage, overlap and estimates checked. No model call.')
