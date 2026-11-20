"""跨夜召回保障服务/API 测试：幂等、补卡、费率改动、失败回滚、记录只追加。"""
import pytest

from app.errors import DomainError
from app.modules.m8_recall import recall_store as rs
from app.modules.m8_recall import service


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch, request):
    # 请求 client 的用例由 conftest 的 client fixture 统一设置 DB_PATH，这里不重复指向另一库
    if 'client' in request.fixturenames:
        yield
        return
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'recall.db'))
    yield


def base_payload(event_id='RC-T1', **over):
    payload = {
        'event_id': event_id,
        'employee': 'E501',
        'hourly_rate': '30',
        'punches': [
            {'punch_id': 'P-IN', 'direction': 'IN', 'at': '2026-10-01T23:00:00+08:00'},
            {'punch_id': 'P-OUT', 'direction': 'OUT', 'at': '2026-10-02T01:00:00+08:00'},
        ],
        'breaks': [],
    }
    payload.update(over)
    return payload


def test_first_registration_creates_current_voucher():
    out = service.register_event(base_payload())
    assert out['outcome'] == 'PROCESSED'
    v = out['voucher']
    assert v['status'] == 'CURRENT' and v['version'] == 1
    assert v['voucher_no'] == 'RC-T1-V01'
    body = v['body']
    assert body['makeup_minutes'] == 120 and body['total_work_minutes'] == 120
    assert Decimal_payable_invariant(body)


def Decimal_payable_invariant(body):
    from decimal import Decimal
    total = sum((Decimal(l['day_payable']) for l in body['lines']), Decimal(0))
    return total == Decimal(body['payable'])


def test_repeated_identical_request_is_deduped_and_adds_no_minutes():
    first = service.register_event(base_payload())
    second = service.register_event(base_payload())
    third = service.register_event(base_payload())
    assert second['outcome'] == 'DEDUPED' and third['outcome'] == 'DEDUPED'
    # 凭证不新增版本
    history = rs.voucher_history('RC-T1')
    assert len(history) == 1
    # 保障分钟不变
    assert first['voucher']['body']['makeup_minutes'] == second['voucher']['body']['makeup_minutes']
    logs = rs.list_retry_log('RC-T1')
    assert [l['outcome'] for l in logs] == ['PROCESSED', 'DEDUPED', 'DEDUPED']


def test_makeup_punch_supersedes_voucher_but_keeps_old_and_originals():
    service.register_event(base_payload())
    before_punches = len(rs.list_punches('RC-T1'))
    out = service.add_makeup_punch('RC-T1', {
        'punch_id': 'P-IN', 'direction': 'IN', 'at': '2026-10-01T22:30:00+08:00',
    })
    assert out['outcome'] == 'MAKEUP_APPLIED'
    assert out['voucher']['version'] == 2 and out['voucher']['status'] == 'CURRENT'
    history = rs.voucher_history('RC-T1')
    assert [(h['version'], h['status']) for h in history] == [(1, 'SUPERSEDED'), (2, 'CURRENT')]
    # 原始打卡与旧版补卡都还在（只追加）
    punches = rs.list_punches('RC-T1')
    assert len(punches) == before_punches + 1
    assert {p['source'] for p in punches} == {'ORIGINAL', 'MAKEUP'}
    assert len([p for p in punches if p['punch_id'] == 'P-IN']) == 2
    # 新凭证反映补卡后的工作区间
    assert out['voucher']['body']['window_start'].endswith('22:30:00+08:00')


def test_replace_failure_restores_previous_voucher_and_keeps_records():
    first = service.register_event(base_payload())
    original_no = first['voucher']['voucher_no']
    with pytest.raises(DomainError) as exc:
        service.add_makeup_punch('RC-T1', {
            'punch_id': 'P-OUT', 'direction': 'OUT',
            'at': '2026-10-02T02:00:00+08:00', 'simulate_replace_failure': True,
        })
    assert exc.value.code == 'VOUCHER'
    current = rs.current_voucher('RC-T1')
    # 旧版凭证恢复为 CURRENT（失败的是新版本，替换前现行凭证保持可支付）
    assert current['voucher_no'] == original_no and current['status'] == 'CURRENT'
    history = rs.voucher_history('RC-T1')
    statuses = {h['version']: h['status'] for h in history}
    assert statuses[1] == 'CURRENT' and statuses[2] == 'SUPERSEDED' and statuses[3] == 'FAILED'
    # 重算期间的补卡与原始打卡都没有丢
    punches = rs.list_punches('RC-T1')
    assert len(punches) == 3
    assert any(p['source'] == 'MAKEUP' for p in punches)
    # 失败也记入重试日志
    assert any(l['outcome'] == 'REJECTED' for l in rs.list_retry_log('RC-T1'))


def test_failure_on_first_settlement_leaves_no_broken_current():
    # 首次注册即模拟失败：没有旧凭证可恢复，CURRENT 为空但事件与打卡已留档
    with pytest.raises(DomainError):
        service.register_event(base_payload(event_id='RC-T2', simulate_replace_failure=True))
    assert rs.current_voucher('RC-T2') is None
    history = rs.voucher_history('RC-T2')
    assert len(history) == 1 and history[0]['status'] == 'FAILED'
    # 修正后重新提交可以生成有效凭证
    ok = service.register_event(base_payload(event_id='RC-T2'))
    assert ok['voucher']['status'] == 'CURRENT'
    statuses = {h['version']: h['status'] for h in rs.voucher_history('RC-T2')}
    assert statuses[1] == 'FAILED' and statuses[2] == 'CURRENT'


def test_rate_change_invalidates_and_recalculates():
    service.register_event(base_payload())
    old = rs.current_voucher('RC-T1')
    result = service.update_rate_calendar({
        '2026-10-01': {'multiplier': '3', 'label': '假日'},
        '2026-10-02': {'multiplier': '2', 'label': '周末'},
    })
    assert result['rate_version'] == 2
    new = rs.current_voucher('RC-T1')
    assert new['version'] == 2 and new['rate_version'] == 2
    # 旧凭证作废但仍可查
    assert rs.get_voucher('RC-T1-V01')['status'] == 'SUPERSEDED'
    # 新凭证冻结了新费率快照
    assert new['body']['rate_calendar_snapshot']['2026-10-02']['multiplier'] == '2'
    # D2 倍率从 1 变 2，应付上涨
    from decimal import Decimal
    assert Decimal(new['body']['payable']) > Decimal(old['body']['payable'])


def test_rate_change_keeps_old_voucher_when_recalc_fails():
    service.register_event(base_payload())
    old_no = rs.current_voucher('RC-T1')['voucher_no']
    # 新费率缺少事件覆盖到的日历日 2026-10-02，裁决失败
    result = service.update_rate_calendar({
        '2026-10-01': {'multiplier': '3', 'label': '假日'},
    })
    assert result['results'][0]['status'] == 'KEPT_OLD'
    assert rs.current_voucher('RC-T1')['voucher_no'] == old_no


def test_rate_version_frozen_inside_voucher_body():
    service.register_event(base_payload())
    v1 = rs.current_voucher('RC-T1')
    service.update_rate_calendar({
        '2026-10-01': {'multiplier': '3'}, '2026-10-02': {'multiplier': '1.5'},
    })
    v2 = rs.current_voucher('RC-T1')
    assert v1['body']['rate_version'] != v2['body']['rate_version']
    # 旧凭证体里的倍率快照不被后续费率改动污染
    assert v1['body']['rate_calendar_snapshot']['2026-10-02']['multiplier'] == '1'


def test_event_detail_contains_full_audit_trail():
    service.register_event(base_payload())
    service.register_event(base_payload())  # 重试
    service.add_makeup_punch('RC-T1', {
        'punch_id': 'P-IN', 'direction': 'IN', 'at': '2026-10-01T22:00:00+08:00',
    })
    detail = service.event_detail('RC-T1')
    assert len(detail['punches']) == 3
    assert len(detail['voucher_history']) == 2
    assert detail['current_voucher']['version'] == 2
    assert any(l['outcome'] == 'DEDUPED' for l in detail['retry_log'])
    # 每行都带形成原因
    for line in detail['current_voucher']['body']['lines']:
        assert '保障补足' in line['reason'] and line['calendar_day'] in line['reason']


def test_overview_and_seed_demo(client=None):
    eid = service.seed_demo()
    again = service.seed_demo()  # 幂等
    assert eid == again == 'RC-DEMO-01'
    overview = service.overview()
    assert overview['guarantee_minutes'] == 240
    assert any(e['event_id'] == 'RC-DEMO-01' and e['payable'] for e in overview['events'])


def test_invalid_inputs_rejected():
    with pytest.raises(DomainError) as exc:
        service.register_event(base_payload(event_id=''))
    assert exc.value.code == 'PARAMETERS'
    with pytest.raises(DomainError) as exc:
        service.register_event(base_payload(event_id='RC-BAD', hourly_rate='abc'))
    assert exc.value.code == 'NUMBER'
    with pytest.raises(DomainError):
        service.register_event(base_payload(event_id='RC-BAD2', punches=[]))


# ---------- HTTP 层 ----------

def test_api_full_flow(client):
    # client 与本文件 isolated_db 均把 DB_PATH 指向临时库，互不污染
    # 页面与总览
    assert client.get('/m8_recall').status_code == 200
    r = client.get('/api/m8_recall/events')
    assert r.status_code == 200 and r.get_json()['guarantee_minutes'] == 240

    # 注册
    r = client.post('/api/m8_recall/events', json=base_payload(event_id='RC-API-1'))
    assert r.status_code == 200 and r.get_json()['voucher']['status'] == 'CURRENT'
    # 重试幂等
    r2 = client.post('/api/m8_recall/events', json=base_payload(event_id='RC-API-1'))
    assert r2.get_json()['outcome'] == 'DEDUPED'

    # 明细
    d = client.get('/api/m8_recall/events/RC-API-1').get_json()
    assert len(d['voucher_history']) == 1

    # 补卡
    m = client.post('/api/m8_recall/events/RC-API-1/makeup',
                    json={'punch_id': 'P-IN', 'direction': 'IN',
                          'at': '2026-10-01T22:30:00+08:00'})
    assert m.status_code == 200 and m.get_json()['voucher']['version'] == 2

    # 改费率
    rr = client.put('/api/m8_recall/rates', json={'rate_calendar': {
        '2026-10-01': {'multiplier': '3'}, '2026-10-02': {'multiplier': '2'}}})
    assert rr.status_code == 200 and rr.get_json()['rate_version'] >= 2

    # 替换失败 API 返回 422 且旧凭证仍 CURRENT
    fail = client.post('/api/m8_recall/events/RC-API-1/makeup',
                       json={'punch_id': 'P-OUT', 'direction': 'OUT',
                             'at': '2026-10-02T03:00:00+08:00',
                             'simulate_replace_failure': True})
    assert fail.status_code == 422 and fail.get_json()['code'] == 'VOUCHER'
    assert client.get('/api/m8_recall/events/RC-API-1').get_json()['current_voucher']['status'] == 'CURRENT'


def test_api_404_for_unknown_event(client):
    assert client.get('/api/m8_recall/events/NOPE').status_code == 404
