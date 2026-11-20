'use strict';
/* 跨夜召回保障页面：事件明细、补卡、费率改动与凭证版本核对。 */
const recallMsg = document.getElementById('message');

async function call(url, options) {
  const resp = await fetch(url, Object.assign({
    headers: { 'Content-Type': 'application/json' }
  }, options || {}));
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.message || ('请求失败 ' + resp.status));
  return data;
}

function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function statusBadge(status) {
  const cls = status === 'CURRENT' ? 'holiday' : status === 'FAILED' ? 'overtime' : '';
  return `<span class="state ${cls}">${esc(status)}</span>`;
}

function renderDetail(detail) {
  document.getElementById('detail-event').textContent = detail.event.event_id;
  const v = detail.current_voucher;
  const body = v ? v.body : null;
  let html = '';

  if (!body) {
    html += '<p class="hash">该事件尚无可支付凭证（数据不完整或裁决未通过），请补卡后重算。</p>';
  } else {
    html += `<dl class="reconcile-grid" style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));padding:14px;gap:12px">
      <div><label>凭证号</label><b style="font-size:14px">${esc(v.voucher_no)}</b></div>
      <div><label>凭证版本</label><b style="font-size:14px">v${esc(v.version)} ${statusBadge(v.status)}</b></div>
      <div><label>费率版本</label><b style="font-size:14px">v${esc(v.rate_version)}</b></div>
      <div><label>应付 CNY</label><b style="font-size:14px">${esc(body.payable)}</b></div>
    </dl>`;
    html += `<dl><dt>工作区间</dt><dd>${esc(body.window_start)} → ${esc(body.window_end)}</dd>
      <dt>全事件实际工作</dt><dd>${esc(body.total_work_minutes)} 分钟</dd>
      <dt>单次保底</dt><dd>${esc(body.guarantee_minutes)} 分钟</dd>
      <dt>保障补足合计</dt><dd>${esc(body.makeup_minutes)} 分钟</dd>
      <dt>本版成因</dt><dd>${esc(v.reason)}</dd></dl>`;
    html += '<ol class="hash" style="padding-left:26px">' + body.explanation.map(n => `<li>${esc(n)}</li>`).join('') + '</ol>';
    html += `<div class="scroll"><table class="ledger"><thead>
      <tr><th>日历日</th><th>倍率</th><th>实际工作分钟</th><th>工作金额</th><th>保障补足分钟</th><th>补足金额</th><th>当日应付</th><th>形成原因</th></tr></thead><tbody>`;
    for (const line of body.lines) {
      html += `<tr><td>${esc(line.calendar_day)}</td><td class="num">${esc(line.multiplier)}</td>
        <td class="num">${esc(line.work_minutes)}</td><td class="num">${esc(line.work_amount)}</td>
        <td class="num">${esc(line.makeup_minutes)}</td><td class="num">${esc(line.makeup_amount)}</td>
        <td class="num">${esc(line.day_payable)}</td><td title="${esc(line.reason)}" style="max-width:320px">${esc(line.reason)}</td></tr>`;
    }
    html += '</tbody></table></div>';
  }

  html += `<header style="margin-top:14px"><h2>原始/补卡打卡（只追加，旧版留档）</h2><span>${detail.punches.length} 条</span></header>
    <div class="scroll"><table class="ledger"><thead><tr><th>punch_id</th><th>方向</th><th>时刻</th><th>来源</th><th>版本seq</th></tr></thead><tbody>`;
  for (const p of detail.punches) {
    html += `<tr><td>${esc(p.punch_id)}</td><td>${esc(p.direction)}</td><td>${esc(p.at)}</td>
      <td><span class="state ${p.source === 'MAKEUP' ? 'overtime' : ''}">${esc(p.source)}</span></td><td class="num">${esc(p.seq)}</td></tr>`;
  }
  html += '</tbody></table></div>';

  if (detail.breaks.length) {
    html += '<header style="margin-top:14px"><h2>休息区间</h2></header><div class="scroll"><table class="ledger"><thead><tr><th>开始</th><th>结束</th></tr></thead><tbody>';
    for (const b of detail.breaks) html += `<tr><td>${esc(b.break_start)}</td><td>${esc(b.break_end)}</td></tr>`;
    html += '</tbody></table></div>';
  }

  html += `<header style="margin-top:14px"><h2>凭证版本链</h2><span>旧版作废不删除</span></header>
    <div class="scroll"><table class="ledger"><thead><tr><th>凭证号</th><th>版本</th><th>状态</th><th>费率版本</th><th>成因</th><th>生成时间(UTC)</th></tr></thead><tbody>`;
  for (const h of detail.voucher_history) {
    html += `<tr><td>${esc(h.voucher_no)}</td><td class="num">${esc(h.version)}</td><td>${statusBadge(h.status)}</td>
      <td class="num">v${esc(h.rate_version)}</td><td style="max-width:300px">${esc(h.reason)}</td><td>${esc(h.created_at)}</td></tr>`;
  }
  html += '</tbody></table></div>';

  html += `<header style="margin-top:14px"><h2>重试/重算日志</h2><span>${detail.retry_log.length} 条</span></header>
    <div class="scroll"><table class="ledger"><thead><tr><th>结果</th><th>关联凭证</th><th>说明</th><th>时间(UTC)</th></tr></thead><tbody>`;
  for (const r of detail.retry_log) {
    html += `<tr><td><span class="state ${r.outcome === 'DEDUPED' ? 'holiday' : r.outcome === 'REJECTED' ? 'overtime' : ''}">${esc(r.outcome)}</span></td>
      <td>${esc(r.voucher_no || '—')}</td><td style="max-width:320px">${esc(r.detail)}</td><td>${esc(r.created_at)}</td></tr>`;
  }
  html += '</tbody></table></div>';
  document.getElementById('detail-body').innerHTML = html;
}

async function openEvent(eventId) {
  recallMsg.textContent = '加载事件 ' + eventId;
  try {
    const detail = await call('/api/m8_recall/events/' + encodeURIComponent(eventId));
    renderDetail(detail);
    recallMsg.textContent = '已核对 ' + eventId;
  } catch (e) {
    recallMsg.textContent = e.message;
  }
}

document.querySelectorAll('.event-row').forEach(row => {
  row.addEventListener('click', () => openEvent(row.dataset.id));
  row.addEventListener('keydown', e => { if (e.key === 'Enter') openEvent(row.dataset.id); });
});

document.getElementById('refresh').addEventListener('click', () => location.reload());
document.getElementById('seed-btn').addEventListener('click', async () => {
  try { await call('/api/m8_recall/seed', { method: 'POST' }); location.reload(); }
  catch (e) { recallMsg.textContent = e.message; }
});

document.getElementById('register-form').addEventListener('submit', async ev => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const payload = {
    event_id: f.get('event_id'), employee: f.get('employee'), hourly_rate: f.get('hourly_rate'),
    punches: [
      { punch_id: 'P-IN', direction: 'IN', at: f.get('in_at') },
      { punch_id: 'P-OUT', direction: 'OUT', at: f.get('out_at') }
    ],
    breaks: []
  };
  if (f.get('break_start') && f.get('break_end')) {
    payload.breaks = [{ start: f.get('break_start'), end: f.get('break_end') }];
  }
  if (f.get('simulate_fail')) payload.simulate_replace_failure = true;
  try {
    const r = await call('/api/m8_recall/events', { method: 'POST', body: JSON.stringify(payload) });
    recallMsg.textContent = r.message;
    await openEvent(payload.event_id);
  } catch (e) { recallMsg.textContent = e.message; }
});

document.getElementById('makeup-form').addEventListener('submit', async ev => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const payload = { punch_id: f.get('punch_id'), direction: f.get('direction'), at: f.get('at') };
  if (f.get('simulate_fail')) payload.simulate_replace_failure = true;
  try {
    const r = await call('/api/m8_recall/events/' + encodeURIComponent(f.get('event_id')) + '/makeup',
      { method: 'POST', body: JSON.stringify(payload) });
    recallMsg.textContent = r.message;
    await openEvent(f.get('event_id'));
  } catch (e) { recallMsg.textContent = e.message; }
});

document.getElementById('rate-form').addEventListener('submit', async ev => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  let calendar;
  try { calendar = JSON.parse(f.get('rate_calendar')); }
  catch (e) { recallMsg.textContent = '费率日历 JSON 无法解析'; return; }
  const payload = { rate_calendar: calendar };
  if (f.get('affected_events')) payload.affected_events = f.get('affected_events').split(',').map(s => s.trim()).filter(Boolean);
  try {
    const r = await call('/api/m8_recall/rates', { method: 'PUT', body: JSON.stringify(payload) });
    recallMsg.textContent = '费率版本 v' + r.rate_version + ' 已生效，受影响事件已重算';
    location.reload();
  } catch (e) { recallMsg.textContent = e.message; }
});

const firstRow = document.querySelector('.event-row');
if (firstRow) openEvent(firstRow.dataset.id);
