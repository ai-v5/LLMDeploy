'use strict';

const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 2});
const integer = new Intl.NumberFormat('zh-CN');
const clock = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
const dateTime = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
let selected = '24h';
let revision = 0;
let requestOffset = 0;
let sessionOffset = 0;
const pageSize = 100;
const seconds = (value) => value === null || value === undefined ? '—' : `${number.format(value)} s`;
const rate = (value) => value === null || value === undefined ? '—' : number.format(value);
const percent = (value) => value === null || value === undefined ? '—' : `${number.format(value * 100)}%`;
const count = (value) => value === null || value === undefined ? '—' : integer.format(value);
const fingerprint = (value) => value ? value.slice(0, 12) : '未知';

function display(value, stats, digits = false) {
  if (!stats.known && !stats.complete) return '—';
  const prefix = stats.complete ? '' : '≥';
  return prefix + (digits ? number.format(value) : integer.format(value));
}

function cell(row, text) {
  const node = document.createElement('td');
  node.textContent = text;
  row.appendChild(node);
}

function empty(body, columns, text) {
  const row = document.createElement('tr');
  const item = document.createElement('td');
  item.colSpan = columns;
  item.className = 'empty';
  item.textContent = text;
  row.appendChild(item);
  body.appendChild(row);
}

function renderClients(data) {
  const body = $('clients-body');
  body.replaceChildren();
  $('client-count').textContent = `${data.clients.length} 个来源`;
  if (!data.clients.length) {
    empty(body, 8, '当前范围内暂无请求记录');
    return;
  }
  data.clients.forEach((client) => {
    const row = document.createElement('tr');
    const complete = data.totals.complete && !client.missing && !client.failed && !client.interrupted;
    const view = {...client, complete, covered_seconds: data.totals.covered_seconds};
    const unknown = client.missing + client.failed + client.interrupted;
    cell(row, client.ip || '未知来源');
    cell(row, unknown ? `${integer.format(client.requests)}（${integer.format(unknown)} 未计量）` : integer.format(client.requests));
    cell(row, display(client.input + client.output, view));
    cell(row, display(client.input, view));
    cell(row, display(client.output, view));
    cell(row, display(client.cached, view));
    cell(row, rate(client.request_tps));
    cell(row, client.window_tps === null ? '—' : display(client.window_tps, view, true));
    body.appendChild(row);
  });
}

function renderActive(data) {
  const body = $('active-body');
  body.replaceChildren();
  $('active-count').textContent = data.active_ready ? count(data.active.length) : '—';
  $('active-badge').textContent = data.active_ready ? `快照 ${clock.format(new Date(data.active_sampled_at * 1000))}` : '快照不可用';
  if (!data.active_ready || !data.active.length) {
    empty(body, 8, data.active_ready ? '当前采集快照中暂无在途推理请求' : '采集快照过期或不可用');
    return;
  }
  data.active.forEach((item) => {
    const row = document.createElement('tr');
    cell(row, item.ip || '未知来源');
    cell(row, fingerprint(item.session));
    cell(row, seconds(item.elapsed));
    cell(row, item.first_visible === null ? '等待可见增量' : '已有可见增量');
    ['input', 'output'].forEach((key) => cell(row, count(item[key])));
    cell(row, '—');
    cell(row, '—');
    body.appendChild(row);
  });
}

function renderBackends(data) {
  const body = $('backends-body');
  body.replaceChildren();
  if (!data.backends.length) { empty(body, 7, '未配置后端原生指标'); return; }
  data.backends.forEach((backend) => {
    const row = document.createElement('tr');
    cell(row, String(backend.port));
    if (!backend.available) {
      cell(row, '不可用');
      for (let i = 0; i < 5; i++) cell(row, '—');
    } else {
      cell(row, count(backend.running));
      cell(row, count(backend.waiting));
      cell(row, count(backend.workers_alive));
      cell(row, backend.admission_ready === 1 ? '就绪' : '未就绪');
      cell(row, count(backend.input_total));
      cell(row, percent(backend.hit_rate));
    }
    body.appendChild(row);
  });
}

function renderSessions(data) {
  const summary = $('session-summary-body');
  summary.replaceChildren();
  $('session-count').textContent = `${count(data.session_count)} 个显式会话 · ${count(data.totals.unknown_session)} 个请求未知`;
  if (!data.sessions.length) empty(summary, 11, '当前页暂无可识别的显式会话');
  data.sessions.forEach((item) => {
    const row = document.createElement('tr');
    const view = {known: item.known, complete: data.totals.complete && item.known === item.requests};
    cell(row, fingerprint(item.session));
    cell(row, item.ips.length ? item.ips.join(', ') : '未知来源');
    cell(row, `${count(item.requests)} / ${count(item.known)}`);
    cell(row, item.input === null ? '—' : display(item.input, view));
    cell(row, item.output === null ? '—' : display(item.output, view));
    cell(row, percent(item.hit_rate));
    cell(row, seconds(item.first_visible)); cell(row, seconds(item.after_first));
    cell(row, rate(item.input_per_wait)); cell(row, rate(item.output_per_after_first));
    cell(row, item.first_share === null ? '—' : `${percent(item.first_share)} / ${percent(1 - item.first_share)}`);
    summary.appendChild(row);
  });
  $('session-page-label').textContent = `${data.session_count ? sessionOffset + 1 : 0}–${Math.min(sessionOffset + data.sessions.length, data.session_count)} / ${data.session_count}`;
  $('session-previous').disabled = sessionOffset === 0;
  $('session-next').disabled = sessionOffset + data.sessions.length >= data.session_count;
  const body = $('sessions-body');
  body.replaceChildren();
  if (!data.requests.length) empty(body, 12, '当前页暂无请求记录');
  const states = {known: '已计量', missing: '未计量', failed: '失败', interrupted: '中断'};
  data.requests.forEach((item) => {
    const row = document.createElement('tr');
    cell(row, dateTime.format(new Date(item.finished * 1000)));
    cell(row, item.ip || '未知来源');
    cell(row, fingerprint(item.session));
    cell(row, `${states[item.state] || '未知'}${item.status ? ` ${item.status}` : ''}`);
    cell(row, count(item.input)); cell(row, count(item.output)); cell(row, percent(item.hit_rate));
    cell(row, seconds(item.first_visible)); cell(row, seconds(item.after_first));
    cell(row, rate(item.input_per_wait)); cell(row, rate(item.output_per_after_first));
    cell(row, item.first_share === null ? '—' : `${percent(item.first_share)} / ${percent(item.after_first_share)}`);
    body.appendChild(row);
  });
  $('page-label').textContent = `${data.request_count ? requestOffset + 1 : 0}–${Math.min(requestOffset + data.requests.length, data.request_count)} / ${data.request_count}`;
  $('previous-page').disabled = requestOffset === 0;
  $('next-page').disabled = requestOffset + data.requests.length >= data.request_count;
}

function renderDays(data) {
  const body = $('days-body');
  body.replaceChildren();
  data.days.slice().reverse().forEach((day) => {
    const row = document.createElement('tr');
    cell(row, day.date);
    cell(row, display(day.input, day));
    cell(row, display(day.output, day));
    cell(row, display(day.cached, day));
    body.appendChild(row);
  });
}

function svgElement(name, attributes) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', name);
  Object.entries(attributes).forEach(([key, value]) => el.setAttribute(key, String(value)));
  return el;
}

function renderPlot(field, points) {
  const svg = $(`chart-${field}-svg`);
  svg.replaceChildren();
  [16, 34, 52].forEach((y) => svg.appendChild(svgElement('line', {x1: 0, y1: y, x2: 800, y2: y, class: 'grid'})));
  const values = points.map((point) => point.end - point.start >= 599.99 && (point.known || point.complete) ? point[field] : null);
  const peak = Math.max(1, ...values.filter((value) => value !== null));
  let segment = [];
  const draw = () => {
    if (segment.length > 1) svg.appendChild(svgElement('polyline', {points: segment.join(' '), class: field}));
    segment = [];
  };
  values.forEach((value, i) => {
    if (value === null) { draw(); return; }
    const x = points.length > 1 ? i * 800 / (points.length - 1) : 0;
    const y = 59 - value / peak * 50;
    segment.push(`${x.toFixed(2)},${y.toFixed(2)}`);
    const marker = svgElement('circle', {cx: x, cy: y, r: 2.5, class: field});
    const title = svgElement('title', {});
    title.textContent = `${clock.format(new Date(points[i].start * 1000))}–${clock.format(new Date(points[i].end * 1000))}：${points[i].complete ? '' : '至少 '}${integer.format(value)} Token`;
    marker.appendChild(title);
    svg.appendChild(marker);
  });
  draw();
  const total = points.reduce((sum, point) => sum + point[field], 0);
  const any = points.some((point) => point.known || point.complete);
  const complete = points.every((point) => point.complete);
  $(`chart-${field}`).textContent = any ? `${complete ? '' : '≥'}${integer.format(total)}` : '暂无数据';
}

function render(data) {
  const stats = data.totals;
  $('status-dot').classList.toggle('live', data.collector_live);
  $('status-label').textContent = data.collector_live ? '采集运行中' : '采集未连接';
  $('updated').textContent = `更新于 ${clock.format(new Date(data.as_of * 1000))}`;
  $('total-usage').textContent = display(stats.input + stats.output, stats);
  $('total-input').textContent = display(stats.input, stats);
  $('total-output').textContent = display(stats.output, stats);
  $('total-cached').textContent = display(stats.cached, stats);
  $('total-tps').textContent = stats.window_tps === null ? '—' : display(stats.window_tps, stats, true);
  $('cache-detail').textContent = stats.hit_rate === null ? '已计量请求命中率 —' : `已计量请求命中率 ${number.format(stats.hit_rate * 100)}%`;
  $('success-rate').textContent = percent(stats.success_rate);
  $('success-detail').textContent = `${count(stats.successes)} / ${count(stats.classified)} 个可判定请求`;
  $('requests-rate').textContent = !stats.covered_seconds || stats.requests_per_second === null ? '—' :
    `${stats.complete ? '' : '≥'}${rate(stats.requests_per_second)}`;
  $('first-p95').textContent = seconds(stats.first_p95);
  $('first-detail').textContent = `${count(stats.first_samples)} 个入口观测样本`;
  $('elapsed-p95').textContent = seconds(stats.elapsed_p95);
  $('elapsed-detail').textContent = `${count(stats.elapsed_samples)} 个已完成样本`;
  const unknown = stats.missing + stats.interrupted + stats.failed;
  const coverage = stats.seconds ? Math.min(100, stats.covered_seconds / stats.seconds * 100) : 0;
  const notice = $('notice');
  notice.classList.toggle('warning', !stats.complete || !data.collector_live || stats.unknown_source > 0);
  notice.textContent = `已计量 ${integer.format(stats.known)} 个请求 · 未计量 ${integer.format(unknown)} 个 · 未知来源 ${integer.format(stats.unknown_source)} 个 · 未正常结束或失联 ${integer.format(stats.unclean_runs)} 次 · 相关采集周期队列丢失 ${integer.format(stats.dropped)} 个 · 采集进程运行覆盖 ${number.format(coverage)}%。仅统计经过本代理且已写盘的最终响应；入口回退、网关内部重试及取消时的消耗可能无法观测，因此不是后端物理总消耗。${stats.complete ? '' : ' ≥ 表示已观测下限；无覆盖不视为零。'}`;
  ['input', 'output', 'cached'].forEach((field) => renderPlot(field, data.series_24h));
  renderClients(data);
  renderDays(data);
  renderActive(data);
  renderBackends(data);
  renderSessions(data);
}

async function refresh() {
  const current = ++revision;
  try {
    const response = await fetch(`/api/stats?window=${encodeURIComponent(selected)}&offset=${requestOffset}&session_offset=${sessionOffset}`, {cache: 'no-store'});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    if (current === revision) render(data);
  } catch (_) {
    if (current !== revision) return;
    $('status-dot').classList.remove('live');
    $('status-label').textContent = '统计不可用';
    $('updated').textContent = '请检查本机采集和账本';
    ['total-usage', 'total-input', 'total-output', 'total-cached', 'total-tps'].forEach((id) => { $(id).textContent = '—'; });
    ['input', 'output', 'cached'].forEach((field) => { $(`chart-${field}-svg`).replaceChildren(); $(`chart-${field}`).textContent = '暂无数据'; });
    $('clients-body').replaceChildren();
    $('days-body').replaceChildren();
    empty($('clients-body'), 8, '统计不可用');
    empty($('days-body'), 4, '统计不可用');
    ['active-count', 'success-rate', 'requests-rate', 'first-p95', 'elapsed-p95'].forEach((id) => { $(id).textContent = '—'; });
    [['active-body', 8], ['backends-body', 7], ['session-summary-body', 11], ['sessions-body', 12]].forEach(([id, columns]) => {
      $(id).replaceChildren(); empty($(id), columns, '统计不可用');
    });
    $('notice').classList.add('warning');
    $('notice').textContent = '无法读取统计账本。历史缺失与采集故障不会显示为零。';
  }
}

document.querySelectorAll('[data-window]').forEach((button) => button.addEventListener('click', () => {
  selected = button.dataset.window;
  requestOffset = 0;
  sessionOffset = 0;
  document.querySelectorAll('[data-window]').forEach((item) => item.classList.toggle('is-active', item === button));
  refresh();
}));
$('refresh').addEventListener('click', refresh);
$('previous-page').addEventListener('click', () => { requestOffset = Math.max(0, requestOffset - pageSize); refresh(); });
$('next-page').addEventListener('click', () => { requestOffset += pageSize; refresh(); });
$('session-previous').addEventListener('click', () => { sessionOffset = Math.max(0, sessionOffset - pageSize); refresh(); });
$('session-next').addEventListener('click', () => { sessionOffset += pageSize; refresh(); });
refresh();
setInterval(refresh, 10000);
