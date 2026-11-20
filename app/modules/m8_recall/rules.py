"""跨夜召回保障裁决规则。

裁决要点（与 docs/RECALL-GUARANTEE.md 对应）：

1. 一次召回事件只成立一次 240 分钟保底，保底属于整次召回，不按日历日重复发放。
2. 实际工作分钟 = 打卡区间扣除休息后，按 Asia/Shanghai 切到各法定日历日。
3. 全事件保障补足 = max(0, 240 - 全事件实际工作分钟)；按各日历日实际工作分钟
   占比分摊，最大余数法取整，零工作分钟的日历日不分摊；分摊之和恒等于补足总数。
4. 每个日历日的实际工作分钟与保障补足分钟分别乘以该日历日倍率计薪，逐行留痕。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from app.errors import DomainError

ZONE = ZoneInfo('Asia/Shanghai')
GUARANTEE_MINUTES = 240  # 一次召回保底四小时


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def parse_dt(text: str, field_name: str) -> datetime:
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        raise DomainError('DATETIME', f'{field_name} 时间格式无效')
    if dt.tzinfo is None:
        raise DomainError('DATETIME', f'{field_name} 必须带时区偏移')
    return dt.astimezone(ZONE)


@dataclass
class Punch:
    """一条原始打卡（原始记录只追加、永不删除）。"""
    punch_id: str
    direction: str          # IN / OUT
    at: datetime
    source: str             # ORIGINAL 原始 / MAKEUP 补卡
    seq: int                # 录入序号，同方向时间相同也可稳定排序

    def as_row(self) -> dict:
        return {'punch_id': self.punch_id, 'direction': self.direction,
                'at': self.at.isoformat(), 'source': self.source, 'seq': self.seq}


@dataclass
class WorkWindow:
    start: datetime
    end: datetime


@dataclass
class RecallEvent:
    event_id: str
    employee: str
    hourly_rate: Decimal
    breaks: list[tuple[datetime, datetime]] = field(default_factory=list)
    punches: list[Punch] = field(default_factory=list)

    def active_punches(self) -> list[Punch]:
        """每个方向只采信同一 punch_id 的最新一条（补卡替换旧卡，旧卡仍留档）。"""
        latest: dict[str, Punch] = {}
        for p in self.punches:
            old = latest.get(p.punch_id)
            if old is None or p.seq > old.seq:
                latest[p.punch_id] = p
        return sorted(latest.values(), key=lambda p: (p.at, p.seq))

    def work_window(self) -> WorkWindow:
        active = self.active_punches()
        ins = [p for p in active if p.direction == 'IN']
        outs = [p for p in active if p.direction == 'OUT']
        if not ins or not outs:
            raise DomainError('MISSING', f'召回 {self.event_id} 缺少 IN/OUT 打卡')
        start = ins[-1].at
        end = outs[-1].at
        if start >= end:
            raise DomainError('RANGE', f'召回 {self.event_id} 打卡区间无效（IN 必须早于 OUT）')
        if end - start > timedelta(hours=18):
            raise DomainError('RANGE', f'召回 {self.event_id} 单次时长超出 18 小时上限')
        if start.second or end.second:
            raise DomainError('RANGE', f'召回 {self.event_id} 打卡必须为整分钟')
        for bs, be in self.breaks:
            if not (start <= bs < be <= end):
                raise DomainError('RANGE', f'召回 {self.event_id} 休息区间必须落在工作区间内')
        return WorkWindow(start, end)


def cut_work_minutes(window: WorkWindow, breaks: list[tuple[datetime, datetime]]) -> list[dict]:
    """把实际工作区间（扣除休息）按午夜切成日历日分钟，半开区间 [left, right)。"""
    boundaries = {window.start, window.end}
    for bs, be in breaks:
        if window.start < bs < window.end:
            boundaries.add(bs)
        if window.start < be < window.end:
            boundaries.add(be)
    midnight = window.start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while midnight < window.end:
        boundaries.add(midnight)
        midnight += timedelta(days=1)
    points = sorted(boundaries)
    days: dict[str, int] = {}
    for left, right in zip(points, points[1:]):
        if any(bs <= left < be for bs, be in breaks):
            continue  # 休息分钟不计为工作分钟
        day = left.date().isoformat()
        minutes = int((right - left).total_seconds() // 60)
        days[day] = days.get(day, 0) + minutes
    return [{'calendar_day': d, 'work_minutes': m} for d, m in sorted(days.items()) if m > 0]


def largest_remainder(weights: list[int], total: int) -> list[int]:
    """按权重把 total 分摊为整数，最大余数法；权重为 0 者恒分 0，总和恒等于 total。"""
    if total <= 0:
        return [0] * len(weights)
    weight_sum = sum(weights)
    if weight_sum <= 0:
        # 没有任何工作分钟可作为分摊依据（理论上不会发生：有工作区间才有保底需求）
        raise DomainError('CONSTRAINT', '保障补足缺少可分摊的日历日')
    raw = [total * w / weight_sum for w in weights]
    floors = [int(x) for x in raw]
    leftover = total - sum(floors)
    order = sorted(range(len(weights)),
                   key=lambda i: (raw[i] - floors[i], weights[i], -i), reverse=True)
    out = floors[:]
    for i in order[:leftover]:
        out[i] += 1
    return out


def rate_for_day(calendar_day: str, rate_calendar: dict) -> Decimal:
    entry = rate_calendar.get(calendar_day)
    if entry is None:
        raise DomainError('RATE', f'日历日 {calendar_day} 缺少倍率配置')
    multiplier = Decimal(str(entry['multiplier']))
    if multiplier <= 0:
        raise DomainError('RATE', f'日历日 {calendar_day} 倍率必须为正数')
    return multiplier


def settle(event: RecallEvent, rate_calendar: dict) -> dict:
    """对一次召回事件生成可支付裁决（不含凭证号；由 service 层编号落库）。"""
    window = event.work_window()
    day_work = cut_work_minutes(window, event.breaks)
    total_work = sum(d['work_minutes'] for d in day_work)
    makeup_total = max(0, GUARANTEE_MINUTES - total_work)
    weights = [d['work_minutes'] for d in day_work]
    makeup_parts = largest_remainder(weights, makeup_total)

    lines = []
    payable = Decimal('0')
    for d, makeup in zip(day_work, makeup_parts):
        day = d['calendar_day']
        multiplier = rate_for_day(day, rate_calendar)
        work_amount = money(event.hourly_rate * d['work_minutes'] * multiplier / 60)
        makeup_amount = money(event.hourly_rate * makeup * multiplier / 60)
        payable += work_amount + makeup_amount
        lines.append({
            'event_id': event.event_id,
            'employee': event.employee,
            'calendar_day': day,
            'multiplier': str(multiplier),
            'work_minutes': d['work_minutes'],
            'work_amount': str(work_amount),
            'makeup_minutes': makeup,
            'makeup_amount': str(makeup_amount),
            'day_payable': str(work_amount + makeup_amount),
            'reason': (
                f'{day} 实际工作 {d["work_minutes"]} 分钟 × {multiplier} 倍；'
                f'保障补足按工作分钟占比分摊 {makeup} 分钟 × {multiplier} 倍'
            ),
        })

    return {
        'event_id': event.event_id,
        'employee': event.employee,
        'hourly_rate': str(event.hourly_rate),
        'window_start': window.start.isoformat(),
        'window_end': window.end.isoformat(),
        'total_work_minutes': total_work,
        'guarantee_minutes': GUARANTEE_MINUTES,
        'makeup_minutes': makeup_total,
        'payable': str(payable),
        'lines': lines,
        'explanation': build_explanation(total_work, makeup_total, makeup_parts, day_work),
    }


def build_explanation(total_work: int, makeup_total: int, parts: list[int], day_work: list[dict]) -> list[str]:
    notes = [
        f'一次召回只成立一次 {GUARANTEE_MINUTES} 分钟保底，不按日历日重复计保底。',
        f'全事件实际工作 {total_work} 分钟，保障补足 = max(0, {GUARANTEE_MINUTES} - {total_work}) = {makeup_total} 分钟。',
    ]
    if makeup_total and day_work:
        weights = ', '.join(f"{d['calendar_day']}={d['work_minutes']}" for d in day_work)
        shares = ', '.join(f"{d['calendar_day']}={p}" for d, p in zip(day_work, parts))
        notes.append(f'补足按各日历日工作分钟占比（{weights}）以最大余数法分摊：{shares}，'
                     f'分摊合计 {sum(parts)} 分钟，与补足总数一致。')
    elif not makeup_total:
        notes.append('实际工作已达到或超过 240 分钟，本次召回无需保障补足。')
    notes.append('每个日历日的工作分钟与补足分钟分别乘以该日历日倍率计薪，逐行可核对。')
    return notes
