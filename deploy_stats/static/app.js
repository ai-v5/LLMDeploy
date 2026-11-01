'use strict';

const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 2});
const integer = new Intl.NumberFormat('zh-CN');
const clock = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
const dateTime = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
const snapshotTime = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
let selected = '24h';
let revision = 0;
let lastSnapshot = null;
let pendingRefresh = null;
let activeExpiry = null;
let pendingActive = null;
let latestActiveAsOf = 0;
let currentView = 'overview';
const tables = {
  active: {selector: '#active', sort: 'elapsed', direction: 'desc', columns: ['ip', 'session', 'elapsed', 'phase', 'input', 'output', 'hit_rate', 'tps']},
  backends: {selector: '#backends', sort: 'port', direction: 'asc', columns: ['port', 'running', 'waiting', 'workers_alive', 'admission_ready', 'input_total', 'hit_rate'], noPager: true},
  sessions: {selector: '#sessions', sort: 'last_finished', direction: 'desc', columns: ['session', 'ips', 'requests', 'input', 'output', 'hit_rate', 'first_visible', 'after_first', 'input_per_wait', 'output_per_after_first', 'first_share']},
  requests: {selector: '[aria-labelledby="sessions-heading"]', sort: 'finished', direction: 'desc', columns: ['finished', 'ip', 'session', 'state', 'input', 'output', 'hit_rate', 'first_visible', 'after_first', 'input_per_wait', 'output_per_after_first', 'first_share']},
  clients: {selector: '#clients', sort: 'output', direction: 'desc', columns: ['ip', 'requests', 'total', 'input', 'output', 'cached', 'request_tps', 'window_tps']},
  days: {selector: '#daily', sort: 'date', direction: 'desc', columns: ['date', 'input', 'output', 'cached']},
};
const textSorts = new Set(['ip', 'ips', 'session', 'state', 'date']);
Object.values(tables).forEach((table) => { table.offset = 0; table.size = 20; });
const views = {
  overview: ['用量概览', 'OVERVIEW'], active: ['当前连接', 'LIVE REQUESTS'],
  sessions: ['会话记录', 'SESSIONS'], clients: ['请求来源', 'CLIENTS'],
  daily: ['每日用量', 'DAILY'], backends: ['后端状态', 'BACKENDS'],
};
const seconds = (value) => value === null || value === undefined ? '—' : `${number.format(value)} s`;
const rate = (value) => value === null || value === undefined ? '—' : number.format(value);
const percent = (value) => value === null || value === undefined ? '—' : `${number.format(value * 100)}%`;
const count = (value) => value === null || value === undefined ? '—' : integer.format(value);
const fingerprint = (value) => value ? value.slice(0, 12) : '未知';

function activateView(name) {
  const view = Object.prototype.hasOwnProperty.call(views, name) ? name : 'overview';
  currentView = view;
  document.querySelectorAll('[data-view]').forEach((element) => { element.hidden = element.dataset.view !== view; });
  document.querySelectorAll('.sidebar nav a').forEach((link) => {
    const active = link.getAttribute('href') === `#${view}`;
    link.classList.toggle('nav-active', active);
    if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  });
  $('view-title').textContent = views[view][0];
  $('view-kicker').textContent = views[view][1];
  document.querySelector('.segmented').hidden = view === 'active' || view === 'backends';
}

function setupNavigation() {
  const navigate = () => {
    activateView(window.location.hash.slice(1));
    if (currentView === 'overview' || currentView === 'active') refreshActive();
  };
  document.querySelectorAll('.sidebar nav a').forEach((link) => link.addEventListener('click', (event) => {
    event.preventDefault();
    const view = link.getAttribute('href').slice(1);
    if (window.location.hash !== `#${view}`) history.pushState(null, '', `#${view}`);
    activateView(view);
    if (view === 'overview' || view === 'active') refreshActive();
    window.scrollTo(0, 0);
  }));
  window.addEventListener('popstate', navigate);
  window.addEventListener('hashchange', navigate);
  navigate();
}

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

function setupTables() {
  Object.values(tables).forEach((table) => {
    const section = document.querySelector(table.selector);
    const title = section.querySelector('h2').textContent;
    table.headers = [...section.querySelectorAll('thead th')];
    table.headers.forEach((header, index) => {
      const key = table.columns[index];
      const label = header.textContent;
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'sort-control'; button.textContent = label;
      button.setAttribute('aria-label', `${label}，点击排序`);
      header.replaceChildren(button);
      header.addEventListener('click', () => {
        if (table.sort === key) table.direction = table.direction === 'asc' ? 'desc' : 'asc';
        else { table.sort = key; table.direction = textSorts.has(key) ? 'asc' : 'desc'; }
        table.offset = 0;
        refresh();
      });
    });
    if (table.noPager) return;
    let foot = section.querySelector('.panel-foot');
    if (!foot) { foot = document.createElement('div'); foot.className = 'panel-foot'; section.appendChild(foot); }
    let pager = foot.querySelector('.pager');
    if (!pager) { pager = document.createElement('div'); pager.className = 'pager'; foot.appendChild(pager); }
    pager.replaceChildren();
    if (table === tables.sessions || table === tables.requests) {
      const hint = document.createElement('span'); hint.className = 'snapshot-hint';
      hint.textContent = '翻页快照只包含建立时已入账的记录；将会话和请求都返回第一页，或点击右上角刷新，可查看后续入账。';
      foot.insertBefore(hint, pager);
    }
    if (table === tables.active) {
      const hint = document.createElement('span'); hint.className = 'snapshot-hint';
      hint.textContent = '当前连接随采样更新，翻页结果可能变化。';
      foot.insertBefore(hint, pager);
    }
    const label = document.createElement('label');
    label.textContent = '每页 ';
    const select = document.createElement('select');
    select.setAttribute('aria-label', `${title}每页显示条数`);
    [10, 20, 50].forEach((size) => {
      const option = document.createElement('option'); option.value = String(size); option.textContent = String(size);
      select.appendChild(option);
    });
    select.value = String(table.size);
    select.addEventListener('change', () => { table.size = Number(select.value); table.offset = 0; refresh(); });
    label.appendChild(select);
    const previous = document.createElement('button'); previous.type = 'button'; previous.textContent = '上一页'; previous.disabled = true;
    const page = document.createElement('span');
    const next = document.createElement('button'); next.type = 'button'; next.textContent = '下一页'; next.disabled = true;
    previous.setAttribute('aria-label', `${title}上一页`); next.setAttribute('aria-label', `${title}下一页`);
    previous.addEventListener('click', () => { table.offset = Math.max(0, table.offset - table.size); refresh(); });
    next.addEventListener('click', () => { table.offset += table.size; refresh(); });
    pager.append(label, previous, page, next);
    table.pager = {previous, page, next};
  });
}

function updateHeaders(table) {
  table.headers.forEach((header, index) => {
    const direction = table.columns[index] === table.sort ? (table.direction === 'asc' ? 'ascending' : 'descending') : 'none';
    header.setAttribute('aria-sort', direction);
  });
}

function updatePager(table, total, available = true) {
  if (!table.pager) return;
  const at = table === tables.requests || table === tables.sessions ? lastSnapshot?.at :
    table.offset > 0 ? table.localData?.as_of : null;
  const snapshot = at ? ` · 快照 ${snapshotTime.format(new Date(at * 1000))}` : '';
  table.pager.page.textContent = available ? `共 ${count(total)} 条 · ${total ? table.offset + 1 : 0}–${Math.min(table.offset + table.size, total)}${snapshot}` : '数据不可用';
  table.pager.previous.disabled = !available || table.offset === 0;
  table.pager.next.disabled = !available || table.offset + table.size >= total;
}

function localSource(name, data) {
  const table = tables[name];
  if (table.offset > 0 && table.localData) return table.localData;
  table.localData = data;
  return data;
}

function localValue(name, item, key, data) {
  if (name === 'active' && key === 'phase') return item.first_visible === null ? 0 : 1;
  if (name === 'active' && (key === 'hit_rate' || key === 'tps')) return null;
  if (name === 'backends' && key !== 'port' && !item.available) return null;
  if (name === 'clients' && key === 'total') return item.known || data.totals.complete ? item.input + item.output : null;
  if (name === 'clients' && ['input', 'output', 'cached', 'window_tps'].includes(key) && !item.known && !data.totals.complete) return null;
  if (name === 'days' && key !== 'date' && !item.known && !item.complete) return null;
  return item[key] ?? null;
}

function localPage(name, rows, data, available = true) {
  const table = tables[name];
  const ordered = rows.map((item, index) => ({item, index})).sort((a, b) => {
    const left = localValue(name, a.item, table.sort, data), right = localValue(name, b.item, table.sort, data);
    if (left === null || right === null) return left === right ? a.index - b.index : left === null ? 1 : -1;
    const delta = typeof left === 'number' && typeof right === 'number' ? left - right :
      String(left).localeCompare(String(right), 'zh-CN', {numeric: true, sensitivity: 'base'});
    return (table.direction === 'asc' ? delta : -delta) || a.index - b.index;
  }).map((entry) => entry.item);
  if (table.noPager) return ordered;
  table.offset = ordered.length ? Math.min(table.offset, Math.floor((ordered.length - 1) / table.size) * table.size) : 0;
  updatePager(table, ordered.length, available);
  return ordered.slice(table.offset, table.offset + table.size);
}

function renderClients(data) {
  data = localSource('clients', data);
  const body = $('clients-body');
  body.replaceChildren();
  $('client-count').textContent = `${data.clients.length} 个来源`;
  const clients = localPage('clients', data.clients, data);
  if (!data.clients.length) {
    empty(body, 8, '当前范围内暂无请求记录');
    return;
  }
  clients.forEach((client) => {
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
  if (data.as_of < latestActiveAsOf) return;
  latestActiveAsOf = data.as_of;
  const body = $('active-body');
  body.replaceChildren();
  updateHeaders(tables.active);
  if (activeExpiry) clearTimeout(activeExpiry);
  const age = (Date.now() / 1000) - data.active_sampled_at;
  const ready = data.collector_live && data.active_ready && data.active_sampled_at !== null && age >= 0 && age < 3;
  const unavailable = () => {
    tables.active.offset = 0;
    $('active-count').textContent = '—'; $('active-badge').textContent = '快照不可用';
    body.replaceChildren(); empty(body, 8, '采集快照过期或不可用');
    updatePager(tables.active, 0, false);
  };
  if (!ready) {
    unavailable();
    return;
  }
  $('active-count').textContent = count(data.active.length);
  $('active-badge').textContent = `快照 ${clock.format(new Date(data.active_sampled_at * 1000))}`;
  const active = localPage('active', data.active, data);
  activeExpiry = setTimeout(unavailable, Math.max(0, (3 - age) * 1000));
  if (!data.active.length) { empty(body, 8, '当前采集快照中暂无在途推理请求'); return; }
  active.forEach((item) => {
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

async function refreshActive() {
  if (pendingActive || (currentView !== 'overview' && currentView !== 'active')) return;
  const controller = new AbortController();
  pendingActive = controller;
  const timeout = setTimeout(() => controller.abort(), 3000);
  try {
    const response = await fetch('/api/active', {cache: 'no-store', signal: controller.signal});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    if (currentView === 'overview' || currentView === 'active') renderActive(data);
  } catch (_) {
    // The existing sample expires independently if the lightweight poll fails.
  } finally {
    clearTimeout(timeout);
    if (pendingActive === controller) pendingActive = null;
  }
}

function renderBackends(data) {
  const body = $('backends-body');
  body.replaceChildren();
  if (!data.backends.length) { empty(body, 7, '未配置后端原生指标'); return; }
  localPage('backends', data.backends, data).forEach((backend) => {
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
  tables.sessions.offset = data.session_offset;
  tables.requests.offset = data.request_offset;
  updatePager(tables.sessions, data.session_count);
  updatePager(tables.requests, data.request_count);
  $('session-count').textContent = `${count(data.session_count)} 个可识别会话 · ${count(data.session_unknown_count)} 个请求未知`;
  if (!data.sessions.length) empty(summary, 11, '当前页暂无可识别会话');
  data.sessions.forEach((item) => {
    const row = document.createElement('tr');
    const view = {known: item.known, complete: data.snapshot_complete && item.known === item.requests};
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
}

function renderDays(data) {
  data = localSource('days', data);
  const body = $('days-body');
  body.replaceChildren();
  localPage('days', data.days, data).forEach((day) => {
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
  $('status-label').textContent = data.collector_live ? '入口采集运行中' : '入口采集未连接';
  $('updated').textContent = `数据查询于 ${clock.format(new Date(data.as_of * 1000))}`;
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
  $('first-detail').textContent = `${count(stats.first_samples)} 个已计量成功样本`;
  $('elapsed-p95').textContent = seconds(stats.elapsed_p95);
  $('elapsed-detail').textContent = `${count(stats.elapsed_samples)} 个已计量成功样本`;
  const unknown = stats.missing + stats.interrupted + stats.failed;
  const coverage = stats.seconds ? Math.min(100, stats.covered_seconds / stats.seconds * 100) : 0;
  const notice = $('notice');
  notice.classList.toggle('warning', !stats.complete || !data.collector_live || stats.unknown_source > 0);
  const collectorNotice = data.collector_live ? '' : '入口采集未连接不代表模型服务不可用；无法确认新请求已记录，后端状态仍可单独查看。';
  notice.textContent = `${collectorNotice}已计量 ${integer.format(stats.known)} 个请求 · 未计量 ${integer.format(unknown)} 个 · 未知来源 ${integer.format(stats.unknown_source)} 个 · 未正常结束或失联 ${integer.format(stats.unclean_runs)} 次 · 相关采集周期队列丢失 ${integer.format(stats.dropped)} 个 · 采集进程运行覆盖 ${number.format(coverage)}%。仅统计经过本代理且已写盘的最终响应；入口回退、网关内部重试及取消时的消耗可能无法观测，因此不是后端物理总消耗。${stats.complete ? '' : ' ≥ 表示已观测下限；无覆盖不视为零。'}`;
  ['input', 'output', 'cached'].forEach((field) => renderPlot(field, data.series_24h));
  renderClients(data);
  renderDays(data);
  renderActive(data);
  renderBackends(data);
  renderSessions(data);
  Object.values(tables).forEach(updateHeaders);
}

async function refresh() {
  const current = ++revision;
  if (pendingRefresh) pendingRefresh.abort();
  const controller = new AbortController();
  pendingRefresh = controller;
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const query = new URLSearchParams({window: selected,
      offset: String(tables.requests.offset), limit: String(tables.requests.size),
      sort: tables.requests.sort, direction: tables.requests.direction,
      session_offset: String(tables.sessions.offset), session_limit: String(tables.sessions.size),
      session_sort: tables.sessions.sort, session_direction: tables.sessions.direction});
    if (lastSnapshot && (tables.requests.offset > 0 || tables.sessions.offset > 0)) {
      query.set('snapshot_at', String(lastSnapshot.at));
      query.set('snapshot_id', String(lastSnapshot.id));
    }
    const response = await fetch(`/api/stats?${query}`, {cache: 'no-store', signal: controller.signal});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    if (current === revision) {
      lastSnapshot = {at: data.snapshot_at, id: data.snapshot_id};
      render(data);
    }
  } catch (_) {
    if (current !== revision) return;
    if (activeExpiry) clearTimeout(activeExpiry);
    activeExpiry = null;
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
    $('notice').textContent = '查询超时或无法读取统计。历史缺失与采集故障不会显示为零。';
    Object.values(tables).forEach((table) => updatePager(table, 0, false));
  } finally {
    clearTimeout(timeout);
    if (pendingRefresh === controller) pendingRefresh = null;
  }
}

document.querySelectorAll('[data-window]').forEach((button) => button.addEventListener('click', () => {
  selected = button.dataset.window;
  Object.values(tables).forEach((table) => { table.offset = 0; });
  lastSnapshot = null;
  document.querySelectorAll('[data-window]').forEach((item) => item.classList.toggle('is-active', item === button));
  refresh();
}));
$('refresh').addEventListener('click', () => {
  Object.values(tables).forEach((table) => { table.offset = 0; table.localData = null; });
  lastSnapshot = null; refresh();
});
setupTables();
setupNavigation();
refresh();
setInterval(() => { if (!pendingRefresh) refresh(); }, 10000);
setInterval(refreshActive, 2000);
