from dataclasses import replace
import random
import pytest
from pydantic import ValidationError
from noveltool.manuscript import Manuscript,Block,split_text,ManuscriptError
from noveltool.chunker import PlanSettings,ChunkPlan,make_plan,verify_plan,plan_view,slice_text


def document(text,mode='auto'):
    return Manuscript(1,tuple(Block.new(t,'revision') for t in split_text(text,mode)))

def test_long_paragraph_lossless_budget_and_overlap():
    text=('甲😀和乙说话。继续向前走。\n'*3000)+'收尾 '
    doc=document(text,'blankline')
    plan=make_plan(doc,PlanSettings(target_tokens=512,overlap_tokens=64),20000,.85)
    byid={b.id:b.text for b in doc.blocks}
    assert len(doc.blocks)==1 and len(plan.chunks)>100
    assert ''.join(slice_text(p,byid) for c in plan.chunks for p in c.core)==text
    assert all(c.estimated_tokens<=512 for c in plan.chunks)
    assert not plan.chunks[0].overlap
    assert all(c.overlap for c in plan.chunks[1:])
    assert ChunkPlan.model_validate_json(plan.model_dump_json())==plan
    verify_plan(plan,doc)


def test_prefers_whole_paragraphs_when_they_fit():
    doc=document('甲'*40+'\n\n'+'乙'*40+'\n\n'+'丙'*40)
    plan=make_plan(doc,PlanSettings(target_tokens=350,overlap_tokens=32),20000,.85)
    for c in plan.chunks:
        for part in c.core:
            assert part.start_cp==0 and part.end_cp==len({b.id:b.text for b in doc.blocks}[part.block_id])


def test_randomized_coverage():
    rng=random.Random(734)
    for _ in range(80):
        text=''.join(rng.choice(['甲','😀','e\u0301','。','\n',' ','\t']) for _ in range(rng.randint(50,3000)))
        doc=document(text)
        settings=PlanSettings(target_tokens=rng.randint(350,1500),overlap_tokens=rng.choice([0,32,64,128]))
        plan=make_plan(doc,settings,20000,.85)
        verify_plan(plan,doc)
        byid={b.id:b.text for b in doc.blocks}
        assert ''.join(slice_text(p,byid) for c in plan.chunks for p in c.core)==text


def test_budget_limits_and_empty_text():
    for doc in [Manuscript(),document('   ')]:
        with pytest.raises(ManuscriptError):make_plan(doc,PlanSettings(),20000,.85)
    with pytest.raises(ManuscriptError,match='预算'):make_plan(document('abc'),PlanSettings(),2048,.85)
    plan=make_plan(document('word '*1000),PlanSettings(prompt_reserve=256,output_reserve=128,overlap_tokens=32),2048,.85)
    assert plan.effective_chunk_limit==int(2048*.85)-256-128
    with pytest.raises(ValidationError):PlanSettings(overlap_tokens=1)
    with pytest.raises(ValidationError):PlanSettings(target_tokens='1000')


def test_tampered_plans_and_revision_change():
    doc=document('甲'*900+'\n\n乙'*90)
    plan=make_plan(doc,PlanSettings(target_tokens=512,overlap_tokens=64),20000,.85)
    first=plan.chunks[0]
    cases=[plan.model_copy(update={'manuscript_hash':'0'*64}),
           plan.model_copy(update={'effective_chunk_limit':999}),
           plan.model_copy(update={'chunks':(first.model_copy(update={'core_hash':'0'*64}),)+plan.chunks[1:]}),
           plan.model_copy(update={'chunks':(first.model_copy(update={'ordinal':9}),)+plan.chunks[1:]}),
           plan.model_copy(update={'chunks':(first.model_copy(update={'estimated_tokens':9}),)+plan.chunks[1:]}),
           plan.model_copy(update={'chunks':plan.chunks[1:]})]
    for broken in cases:
        with pytest.raises(ManuscriptError):verify_plan(broken,doc)
    later=replace(doc,revision_no=2)
    assert plan_view(plan,later)['stale']
    assert not plan_view(plan,doc)['stale']
    with pytest.raises(ManuscriptError):verify_plan(plan,later)
