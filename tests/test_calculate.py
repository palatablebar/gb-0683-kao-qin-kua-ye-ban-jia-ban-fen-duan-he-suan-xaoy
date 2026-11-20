from decimal import Decimal
from app.workflow import run
from app.modules.m5_calculate.rules import price

def segment(day,minutes=540):return {'id':'S','employee':'E','shift_day':day,'calendar_day':day,'start':day+'T09:00:00+08:00','end':day+'T18:00:00+08:00','minutes':minutes,'rate':'20','line':2}
def test_real_holiday_then_normal(raw):
    s=run(raw,{});r=[r for r in s['calculations'] if r['employee']=='E001'];assert sum(Decimal(x['amount']) for x in r)==220
def test_real_holiday_only_two_hours(raw):
    r=[r for r in run(raw,{})['calculations'] if r['employee']=='E001' and r['code']=='HOLIDAY'];assert sum(x['minutes'] for x in r)==120
def test_normal_overtime():
    r=price([segment('2026-10-02')],set());assert [(x['minutes'],x['multiplier']) for x in r]==[(480,'1'),(60,'1.5')] and sum(Decimal(x['amount']) for x in r)==190
def test_rest_double_rate():
    r=price([segment('2026-10-03')],set());assert r[0]['code']=='REST' and r[0]['amount']=='360.00'
def test_holiday_overrides_rest():
    # TRAP-19
    r=price([segment('2026-10-03')],{'2026-10-03'});assert r[0]['amount']=='540.00'
def test_real_minutes_conserved(raw):
    s=run(raw,{});assert s['business']['minutes']==18*420 and s['business']['total']=='3960.00'
def test_daily_threshold_carries_between_segments():
    a=segment('2026-10-02',300);b=dict(segment('2026-10-02',300),start='2026-10-02T15:00:00+08:00');r=price([a,b],set());assert sum(x['minutes'] for x in r if x['code']=='OVERTIME')==120
