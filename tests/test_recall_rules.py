"""跨夜召回保障裁决规则单测：不依赖数据库，纯规则层。"""
from datetime import datetime
from decimal import Decimal

import pytest
from zoneinfo import ZoneInfo

from app.errors import DomainError
from app.modules.m8_recall.rules import (
    GUARANTEE_MINUTES,
    Punch,
    RecallEvent,
    ZONE,
    cut_work_minutes,
    largest_remainder,
    settle,
)

RATES = {
    '2026-10-01': {'multiplier': '3', 'label': '假日'},
    '2026-10-02': {'multiplier': '1', 'label': '工作日'},
}


def dt(text):
    return datetime.fromisoformat(text).astimezone(ZONE)


def make_event(event_id='E1', in_at='2026-10-01T23:00:00+08:00',
               out_at='2026-10-02T01:00:00+08:00', rate='30', breaks=None):
    e = RecallEvent(event_id=event_id, employee='E501', hourly_rate=Decimal(rate))
    e.punches = [
        Punch('P-IN', 'IN', dt(in_at), 'ORIGINAL', 1),
        Punch('P-OUT', 'OUT', dt(out_at), 'ORIGINAL', 1),
    ]
    e.breaks = [(dt(a), dt(b)) for a, b in (breaks or [])]
    return e


def test_work_minutes_split_across_midnight():
    # 23:00→01:00，工作 120 分钟，两个日历日各 60 分钟
    result = settle(make_event(), RATES)
    days = {line['calendar_day']: line for line in result['lines']}
    assert set(days) == {'2026-10-01', '2026-10-02'}
    assert days['2026-10-01']['work_minutes'] == 60
    assert days['2026-10-02']['work_minutes'] == 60
    assert result['total_work_minutes'] == 120


def test_makeup_total_is_single_240_guarantee():
    result = settle(make_event(), RATES)
    # 工作 120 分钟 → 全事件补足 120 分钟，只补一次
    assert result['makeup_minutes'] == GUARANTEE_MINUTES - 120
    assert sum(line['makeup_minutes'] for line in result['lines']) == 120


def test_makeup_split_proportional_and_sum_exact():
    # 工作 100 分钟落在 D1，20 分钟落在 D2（23:20→01:20 休息 0 点到 1 点 → 40+20? 用显式构造）
    e = make_event(in_at='2026-10-01T22:00:00+08:00', out_at='2026-10-02T00:01:00+08:00')
    result = settle(e, RATES)
    weights = [line['work_minutes'] for line in result['lines']]
    makeup = [line['makeup_minutes'] for line in result['lines']]
    assert weights == [120, 1]
    # 补足 = 240-121 = 119，按 120:1 分摊
    assert sum(makeup) == 119
    assert makeup == largest_remainder(weights, 119)
    assert makeup[0] >= makeup[1]


def test_largest_remainder_never_exceeds_total():
    parts = largest_remainder([33, 33, 34], 100)
    assert sum(parts) == 100 and max(parts) - min(parts) <= 1
    assert largest_remainder([0, 10, 0], 5) == [0, 5, 0]
    assert largest_remainder([7, 3], 0) == [0, 0]


def test_zero_work_day_gets_no_makeup():
    # 若某日历日没有工作分钟，不会凭空分到补足
    e = make_event(in_at='2026-10-02T00:30:00+08:00', out_at='2026-10-02T01:30:00+08:00')
    result = settle(e, RATES)
    assert [line['calendar_day'] for line in result['lines']] == ['2026-10-02']
    assert result['lines'][0]['makeup_minutes'] == 180


def test_no_makeup_when_work_meets_guarantee():
    e = make_event(in_at='2026-10-01T20:00:00+08:00', out_at='2026-10-02T00:00:00+08:00')
    result = settle(e, RATES)
    assert result['total_work_minutes'] == 240
    assert result['makeup_minutes'] == 0
    assert all(line['makeup_minutes'] == 0 for line in result['lines'])


def test_over_240_minutes_no_makeup_and_no_clawback():
    e = make_event(in_at='2026-10-01T19:00:00+08:00', out_at='2026-10-02T00:00:00+08:00')
    result = settle(e, RATES)
    assert result['total_work_minutes'] == 300
    assert result['makeup_minutes'] == 0


def test_break_minutes_excluded_before_split():
    e = make_event(
        in_at='2026-10-01T23:00:00+08:00', out_at='2026-10-02T01:00:00+08:00',
        breaks=[('2026-10-02T00:00:00+08:00', '2026-10-02T00:30:00+08:00')],
    )
    result = settle(e, RATES)
    days = {line['calendar_day']: line for line in result['lines']}
    assert days['2026-10-01']['work_minutes'] == 60
    assert days['2026-10-02']['work_minutes'] == 30
    assert result['total_work_minutes'] == 90
    assert result['makeup_minutes'] == 150


def test_payable_uses_each_day_rate_separately():
    result = settle(make_event(), RATES)
    d1, d2 = result['lines']
    # D1: 工作60×3 + 补足60×3；D2: 工作60×1 + 补足60×1；时薪30
    assert Decimal(d1['work_amount']) == Decimal('90.00')
    assert Decimal(d1['makeup_amount']) == Decimal('90.00')
    assert Decimal(d2['work_amount']) == Decimal('30.00')
    assert Decimal(d2['makeup_amount']) == Decimal('30.00')
    assert Decimal(result['payable']) == Decimal('240.00')
    # 逐行金额之和等于总应付
    assert sum((Decimal(l['day_payable']) for l in result['lines']), Decimal(0)) == Decimal(result['payable'])


def test_makeup_never_creates_second_guarantee_pool():
    # 跨两个日历日，总补足仍以一次 240 保底为上限
    result = settle(make_event(), RATES)
    assert result['total_work_minutes'] + result['makeup_minutes'] == GUARANTEE_MINUTES


def test_missing_punch_rejected():
    e = make_event()
    e.punches = [p for p in e.punches if p.direction != 'OUT']
    with pytest.raises(DomainError) as exc:
        settle(e, RATES)
    assert exc.value.code == 'MISSING'


def test_rate_missing_for_work_day_rejected():
    e = make_event()
    with pytest.raises(DomainError) as exc:
        settle(e, {'2026-10-01': {'multiplier': '3'}})
    assert exc.value.code == 'RATE'


def test_cut_work_minutes_half_open_break_boundary():
    e = make_event(breaks=[('2026-10-01T23:30:00+08:00', '2026-10-02T00:30:00+08:00')])
    window = e.work_window()
    parts = cut_work_minutes(window, e.breaks)
    total = sum(p['work_minutes'] for p in parts)
    assert total == 60  # 120 分钟区间扣除 60 分钟休息


def test_makeup_punch_replaces_but_old_remains_in_event():
    e = make_event()
    e.punches.append(Punch('P-IN', 'IN', dt('2026-10-01T22:30:00+08:00'), 'MAKEUP', 2))
    result = settle(e, RATES)
    # 采信最新 IN（22:30），窗口变为 22:30→01:00 = 150 分钟
    assert result['window_start'].endswith('22:30:00+08:00')
    assert result['total_work_minutes'] == 150
    assert result['makeup_minutes'] == 90
    # 事件里仍保留两张 IN 卡
    assert len([p for p in e.punches if p.direction == 'IN']) == 2
