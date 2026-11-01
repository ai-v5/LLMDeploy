'use strict';

const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 2});
const integer = new Intl.NumberFormat('zh-CN');
const clock = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
let selected = '24h';
let revision = 0;

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
    empty(body, 7, '当前范围内暂无请求记录');
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
    cell(row, client.window_tps === null ? '—' : display(client.window_tps, view, true));
    body.appendChild(row);
  });
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
  const unknown = stats.missing + stats.interrupted + stats.failed;
  const coverage = stats.seconds ? Math.min(100, stats.covered_seconds / stats.seconds * 100) : 0;
  const notice = $('notice');
  notice.classList.toggle('warning', !stats.complete || !data.collector_live || stats.unknown_source > 0);
  notice.textContent = `已计量 ${integer.format(stats.known)} 个请求 · 未计量 ${integer.format(unknown)} 个 · 未知来源 ${integer.format(stats.unknown_source)} 个 · 相关采集周期队列丢失 ${integer.format(stats.dropped)} 个 · 采集进程运行覆盖 ${number.format(coverage)}%。仅包含经过本代理的请求；部署时须核验所有入口。${stats.complete ? '' : ' ≥ 表示已观测下限；无覆盖不视为零。'}`;
  ['input', 'output', 'cached'].forEach((field) => renderPlot(field, data.series_24h));
  renderClients(data);
  renderDays(data);
}

async function refresh() {
  const current = ++revision;
  try {
    const response = await fetch(`/api/stats?window=${encodeURIComponent(selected)}`, {cache: 'no-store'});
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
    empty($('clients-body'), 7, '统计不可用');
    empty($('days-body'), 4, '统计不可用');
    $('notice').classList.add('warning');
    $('notice').textContent = '无法读取统计账本。历史缺失与采集故障不会显示为零。';
  }
}

document.querySelectorAll('[data-window]').forEach((button) => button.addEventListener('click', () => {
  selected = button.dataset.window;
  document.querySelectorAll('[data-window]').forEach((item) => item.classList.toggle('is-active', item === button));
  refresh();
}));
$('refresh').addEventListener('click', refresh);
refresh();
setInterval(refresh, 10000);
