import pytest
from app.workflow import run
from app.modules.m3_versions.rules import schedule,apply
from app.errors import DomainError

def test_real_pair_count(raw):assert len(run(raw,{},3)['shifts'])==18
def test_real_crossnight_planned(raw):
    r=run(raw,{},2)['rows'][0];a,b,_,_=schedule(r);assert (b-a).total_seconds()==8*3600 and b.day==2
def test_real_break_following_day(raw):
    r=run(raw,{},2)['rows'][0];_,_,a,b=schedule(r);assert a.day==2 and a.hour==2 and b.hour==3
def test_invalid_break(raw):
    r=run(raw,{},2)['rows'][0];r['break_start']='12:00'
    with pytest.raises(DomainError):schedule(r)
def test_shift_day_is_preserved(raw):assert run(raw,{},3)['shifts'][0]['day']=='2026-10-01'
def test_newer_punch_selected(raw):
    s=run(raw,{},2);r=dict(s['rows'][0],id='new',version='2',at='2026-10-01T15:00:00+00:00');s['rows'].append(r);assert apply(s)['shifts'][0]['in']['id']=='new'
def test_bad_punch_kind(raw):
    s=run(raw,{},2);s['rows'][0]['punch']='OTHER';assert any(q['code']=='RANGE' for q in apply(s)['quarantine'])


def test_same_instant_conflicting_rate_isolates_whole_shift(raw):
    s=run(raw,{},2);duplicate=dict(s['rows'][0],id='rate-conflict',hourly_rate='30',_line=999);s['rows'].append(duplicate)
    result=apply(s);assert len(result['shifts'])==17 and sum(q['code']=='CONFLICT' for q in result['quarantine'])==5

def test_same_instant_conflicting_break_isolated(raw):
    s=run(raw,{},2);duplicate=dict(s['rows'][0],id='break-conflict',break_start='01:00',_line=999);s['rows'].append(duplicate)
    result=apply(s);assert all(p['employee']!='E001' for p in result['shifts'])

def test_in_out_schedule_must_agree(raw):
    s=run(raw,{},2);s['rows'][1]['hourly_rate']='30';result=apply(s)
    assert all(p['employee']!='E001' for p in result['shifts']) and sum(q['code']=='CONFLICT' for q in result['quarantine'])==4
