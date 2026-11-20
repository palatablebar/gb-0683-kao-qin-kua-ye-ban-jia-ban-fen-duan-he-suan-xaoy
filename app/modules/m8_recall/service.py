"""跨夜召回保障服务编排。

对外能力：
- register_event 注册召回事件并录入原始打卡/休息（同一事件重复提交按幂等处理）；
- add_makeup_punch 补卡：只追加新版本打卡，旧卡留档，随后重算并作废旧凭证；
- update_rate_calendar 费率改动：保存新版费率快照并使全部现行凭证作废重算；
- recalculate 重新生成可支付凭证，替换失败自动恢复旧版凭证；
- 连续重试同一召回事件按请求指纹去重，不新增保障分钟。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from app.errors import DomainError
from app.modules.m8_recall import recall_store as rs
from app.modules.m8_recall.rules import (
    GUARANTEE_MINUTES,
    Punch,
    RecallEvent,
    money,
    parse_dt,
    settle,
)

# 演示用默认费率日历：2026-10-01 法定假日 3 倍，2026-10-02 工作日 1 倍。
DEFAULT_RATE_CALENDAR = {
    '2026-10-01': {'multiplier': '3', 'label': '法定假日'},
    '2026-10-02': {'multiplier': '1', 'label': '工作日'},
}


def ensure_rate_calendar() -> int:
    version = rs.latest_rate_version()
    if version is None:
        version = rs.save_rate_calendar(DEFAULT_RATE_CALENDAR)
    return version


def _request_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_event(event_id: str) -> RecallEvent:
    row = rs.get_event(event_id)
    event = RecallEvent(event_id=row['event_id'], employee=row['employee'],
                        hourly_rate=Decimal(row['hourly_rate']))
    for p in rs.list_punches(event_id):
        event.punches.append(Punch(
            punch_id=p['punch_id'], direction=p['direction'],
            at=parse_dt(p['at'], '打卡'), source=p['source'], seq=p['seq'],
        ))
    for b in rs.list_breaks(event_id):
        event.breaks.append((parse_dt(b['break_start'], '休息开始'),
                             parse_dt(b['break_end'], '休息结束')))
    return event


def _validate_rate_calendar(calendar: dict) -> dict:
    if not isinstance(calendar, dict) or not calendar:
        raise DomainError(
            'RATE', '费率日历必须是非空对象 {日期: {multiplier: 倍率}}')
    clean = {}
    for day, entry in calendar.items():
        if not isinstance(entry, dict) or 'multiplier' not in entry:
            raise DomainError(
                'RATE', f'{day} 缺少 multiplier')
        multiplier = Decimal(str(entry['multiplier']))
        if multiplier <= 0:
            raise DomainError(
                'RATE', f'{day} 倍率必须为正数')
        clean[day] = {'multiplier': str(multiplier),
                      'label': str(entry.get('label', ''))}
    return dict(sorted(clean.items()))


def register_event(payload: dict) -> dict:
    """注册/重试召回事件。相同请求指纹视为连续重试，直接返回现行凭证且不新增保障。"""
    event_id = str(payload.get('event_id') or '').strip()
    if not event_id:
        raise DomainError(
            'PARAMETERS', 'event_id 必填')
    fingerprint = _request_hash(payload)
    existing = {e['event_id'] for e in rs.list_events()}

    if event_id in existing and fingerprint in rs.recent_request_hashes(event_id):
        current = rs.current_voucher(event_id)
        rs.log_retry(event_id, fingerprint, 'DEDUPED',
                     '相同请求连续重试，复用现行凭证，不新增保障分钟',
                     current['voucher_no'] if current else None)
        return {'outcome': 'DEDUPED',
                'voucher': current,
                'message': '重复召回请求已幂等忽略，保障分钟未增加'}

    if event_id not in existing:
        _create_event(event_id, payload)
        reason = '首次裁决：按一次召回 240 分钟保底成立'
    else:
        reason = '同源请求重新裁决：输入未变化，凭证内容保持一致'

    voucher = _recalculate(event_id, reason, fail=bool(payload.get('simulate_replace_failure')))
    rs.log_retry(event_id, fingerprint, 'PROCESSED',
                 f'已生成/复用凭证 {voucher["voucher_no"]}', voucher['voucher_no'])
    return {'outcome': 'PROCESSED', 'voucher': voucher,
            'message': '召回事件已裁决并生成可支付凭证'}


def _create_event(event_id: str, payload: dict) -> None:
    employee = str(payload.get('employee') or '').strip()
    if not employee:
        raise DomainError(
            'PARAMETERS', 'employee 必填')
    try:
        hourly_rate = money(payload.get('hourly_rate'))
    except Exception:
        raise DomainError(
            'NUMBER', 'hourly_rate 必须为非负金额')
    if hourly_rate < 0:
        raise DomainError(
            'NUMBER', 'hourly_rate 不能为负')
    rs.upsert_event(event_id, employee, str(hourly_rate))

    punches = payload.get('punches') or []
    if not isinstance(punches, list) or not punches:
        raise DomainError(
            'PARAMETERS', 'punches 至少包含一条 IN 与一条 OUT 打卡')
    seen_directions = set()
    for p in punches:
        direction = str(p.get('direction') or '').upper()
        if direction not in ('IN', 'OUT'):
            raise DomainError(
                'RANGE', '打卡方向只能是 IN/OUT')
        # 先校验时间合法性（含时区偏移）再落库，错误码精确到 DATETIME
        at_dt = parse_dt(p.get('at'), '打卡')
        if not str(p.get('punch_id') or '').strip():
            raise DomainError('PARAMETERS', '每条打卡必须包含 punch_id')
        seen_directions.add(direction)
        rs.add_punch(event_id, str(p['punch_id']).strip(), direction, at_dt.isoformat(), 'ORIGINAL')
    if seen_directions != {'IN', 'OUT'}:
        raise DomainError(
            'MISSING', 'punches 必须同时包含一条 IN 与一条 OUT 打卡')

    for b in payload.get('breaks') or []:
        bs = parse_dt(b['start'], '休息开始')
        be = parse_dt(b['end'], '休息结束')
        if be <= bs:
            raise DomainError(
                'RANGE', '休息区间结束必须晚于开始')
        rs.add_break(event_id, bs.isoformat(), be.isoformat())


def add_makeup_punch(event_id: str, payload: dict) -> dict:
    """补卡：追加 MAKEUP 打卡（同 punch_id 产生新 seq），旧卡保留，随后重算作废旧凭证。"""
    rs.get_event(event_id)  # 事件必须存在
    punch_id = str(payload.get('punch_id') or '').strip()
    direction = str(payload.get('direction') or '').upper()
    if not punch_id or direction not in ('IN', 'OUT'):
        raise DomainError(
            'PARAMETERS', 'punch_id 与方向(IN/OUT) 必填')
    at_dt = parse_dt(payload.get('at'), '补卡时间')
    seq = rs.add_punch(event_id, punch_id, direction, at_dt.isoformat(), 'MAKEUP')
    fingerprint = _request_hash({'makeup': event_id, **payload})

    try:
        voucher = _recalculate(event_id, f'补卡生效：{punch_id} 追加第 {seq} 版打卡，旧凭证作废重算',
                               fail=bool(payload.get('simulate_replace_failure')))
    except Exception as exc:
        # 重算失败：记录失败版本并恢复旧版 CURRENT，补卡记录本身保留（只追加，不回滚）
        rs.log_retry(event_id, fingerprint, 'REJECTED', f'补卡重算失败，已恢复旧版凭证：{exc}')
        raise
    rs.log_retry(event_id, fingerprint, 'PROCESSED',
                 f'补卡后重算成功，新凭证 {voucher["voucher_no"]}', voucher['voucher_no'])
    return {'outcome': 'MAKEUP_APPLIED', 'seq': seq, 'voucher': voucher,
            'message': '补卡已留档并重算，旧凭证标记作废，原始打卡未删除'}


def update_rate_calendar(calendar: dict, affected_events: list[str] | None = None) -> dict:
    """费率改动：保存新版本快照，受影响事件全部重算；任一事件失败则其旧凭证保持有效。"""
    clean = _validate_rate_calendar(calendar)
    rate_version = rs.save_rate_calendar(clean)
    targets = affected_events or [e['event_id'] for e in rs.list_events()]
    results = []
    for event_id in targets:
        try:
            voucher = _recalculate(
                event_id, f'费率日历变更为版本 {rate_version}，旧凭证作废重算',
                rate_version_override=rate_version)
            results.append({'event_id': event_id, 'status': 'RECALCULATED',
                            'voucher_no': voucher['voucher_no']})
        except Exception as exc:
            results.append({'event_id': event_id, 'status': 'KEPT_OLD',
                            'reason': str(exc)})
    return {'rate_version': rate_version, 'results': results}


def _recalculate(event_id: str, reason: str, *, rate_version_override: int | None = None,
                 fail: bool = False) -> dict:
    """重算并原子替换凭证。fail=True 用于演示替换失败：新版本标记 FAILED 并恢复旧版。"""
    event = _load_event(event_id)
    rate_version = rate_version_override or ensure_rate_calendar()
    calendar = rs.get_rate_calendar(rate_version)

    # 规则层先完整裁决；这里抛错则根本不会写入新凭证，旧 CURRENT 自然保持有效
    body = settle(event, calendar)
    body['rate_version'] = rate_version
    body['rate_calendar_snapshot'] = calendar

    if fail:
        # 演示替换失败路径：单个原子事务写入 FAILED 版本并恢复上一版 CURRENT
        rs.fail_and_restore(event_id, body, rate_version,
                            f'模拟替换失败，已恢复上一版 CURRENT（{reason}）')
        raise DomainError('VOUCHER', '凭证替换失败，已恢复旧版凭证；重算期间记录均保留')

    voucher = rs.issue_voucher(event_id, body, rate_version, reason)
    return voucher


def event_detail(event_id: str) -> dict:
    rs.get_event(event_id)
    return {
        'event': rs.get_event(event_id),
        'punches': rs.list_punches(event_id),
        'breaks': rs.list_breaks(event_id),
        'current_voucher': rs.current_voucher(event_id),
        'voucher_history': rs.voucher_history(event_id),
        'retry_log': rs.list_retry_log(event_id),
    }


def overview() -> dict:
    rate_version = rs.latest_rate_version()
    calendar = rs.get_rate_calendar(rate_version) if rate_version else DEFAULT_RATE_CALENDAR
    events = []
    for e in rs.list_events():
        current = rs.current_voucher(e['event_id'])
        events.append({
            'event_id': e['event_id'],
            'employee': e['employee'],
            'hourly_rate': e['hourly_rate'],
            'voucher_no': current['voucher_no'] if current else None,
            'payable': current['body']['payable'] if current else None,
            'total_work_minutes': current['body']['total_work_minutes'] if current else None,
            'makeup_minutes': current['body']['makeup_minutes'] if current else None,
        })
    return {'rate_version': rate_version, 'rate_calendar': calendar,
            'guarantee_minutes': GUARANTEE_MINUTES, 'events': events}


def seed_demo() -> str:
    """写入一个典型跨夜召回样例：工作 120 分钟，跨 10-01（3倍）与 10-02（1倍）。"""
    ensure_rate_calendar()
    payload = {
        'event_id': 'RC-DEMO-01',
        'employee': 'E501',
        'hourly_rate': '30',
        'punches': [
            {'punch_id': 'P-IN', 'direction': 'IN', 'at': '2026-10-01T23:00:00+08:00'},
            {'punch_id': 'P-OUT', 'direction': 'OUT', 'at': '2026-10-02T01:00:00+08:00'},
        ],
        'breaks': [],
    }
    if 'RC-DEMO-01' in {e['event_id'] for e in rs.list_events()}:
        return 'RC-DEMO-01'
    register_event(payload)
    return 'RC-DEMO-01'
