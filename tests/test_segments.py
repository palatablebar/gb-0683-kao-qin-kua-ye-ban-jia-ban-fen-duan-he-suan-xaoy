import pytest
from app.workflow import run
from app.modules.m4_segments.rules import split_shift,apply
from app.errors import DomainError

def test_real_night_seven_hours(raw):
    s=run(raw,{},4);assert sum(r['minutes'] for r in s['segments'] if r['employee']=='E001')==420
def test_actual_calendar_dates(raw):
    s=run(raw,{},4);assert {r['calendar_day'] for r in s['segments'] if r['employee']=='E001'}=={'2026-10-01','2026-10-02'}
def test_break_removed(raw):
    # TRAP-23
    s=run(raw,{},4);assert [r['minutes'] for r in s['segments'] if r['employee']=='E001']==[120,120,180]
def test_missing_out_rejected(raw):
    p=run(raw,{},3)['shifts'][0];p['out']=None
    with pytest.raises(DomainError) as e:split_shift(p)
    assert e.value.code=='MISSING'
def test_negative_time_rejected(raw):
    p=run(raw,{},3)['shifts'][0];p['out']['at']='2026-10-01T12:00:00+00:00'
    with pytest.raises(DomainError):split_shift(p)
def test_more_than_eighteen_hours(raw):
    p=run(raw,{},3)['shifts'][0];p['out']['at']='2026-10-03T06:00:00+08:00'
    with pytest.raises(DomainError):split_shift(p)
def test_missing_card_quarantine_balances(raw):
    s=run(raw,{},3);s['shifts'][0]['out']=None;r=apply(s);assert len(r['rows'])==34 and len(r['segments'])==51
