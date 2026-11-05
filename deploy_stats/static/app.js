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
let pendingTelemetry = null;
let telemetrySnapshot = null;
let telemetryExpiry = null;
let backendExpiry = null;
let telemetryNodePort = 'all';
let telemetryOptionsKey = null;
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
  overview: ['总览', 'OVERVIEW'], active: ['当前连接', 'LIVE REQUESTS'],
  sessions: ['会话记录', 'SESSIONS'], clients: ['请求来源', 'CLIENTS'],
  daily: ['每日用量', 'DAILY'], backends: ['后端状态', 'BACKENDS'],
};
const seconds = (value) => value === null || value === undefined ? '—' : `${number.format(value)} s`;
const rate = (value) => value === null || value === undefined ? '—' : number.format(value);
const speed = (value) => value === null || value === undefined ? '—' : `${rate(value)} tok/s`;
const percent = (value) => value === null || value === undefined ? '—' : `${number.format(value * 100)}%`;
const count = (value) => value === null || value === undefined ? '—' : integer.format(value);
const fingerprint = (value) => value ? value.slice(0, 12) : '未知';
const setTextIfPresent = (id, value) => { const element = $(id); if (element) element.textContent = value; };

function freshTelemetry(part, now) {
  return part && typeof part.valid_until === 'number' && Number.isFinite(part.valid_until) && now < part.valid_until ? part : null;
}

function telemetryValues(data, port, now) {
  const nodes = data && data.available === true && Array.isArray(data.nodes) ? data.nodes : [];
  const read = (value, max = Infinity) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= max ? value : null;
  const motor = data && data.kind === 'vllm-motor-telemetry';
  if (port === 'all') {
    const coverage = Object.fromEntries(['prefill', 'decode', 'mfu', 'hbm'].map((key) =>
      [key, {valid: 0, total: nodes.filter((node) => node && (!motor || key === 'hbm' ||
        node.role === (key === 'mfu' ? 'prefill' : key))).length, span_seconds: null,
      workers_valid: 0, workers_total: nodes.filter((node) => node && (!motor || key === 'hbm' ||
        node.role === (key === 'mfu' ? 'prefill' : key))).reduce((sum, node) => sum + (node.expected_endpoints || 0), 0), partial: false}]));
    const empty = {prefill: null, decode: null, mfu: null, hbm: null, coverage};
    if (!nodes.length || new Set(nodes.map((node) => node && node.backend_port)).size !== nodes.length) return empty;
    const parts = (name, timestamp, duration, requireReady = true) => {
      return nodes.flatMap((node) => {
        if (motor && name !== 'hbm' && node && node.role !== name) return [];
        if (!node || node.source_ok !== true || (requireReady && node.backend_ready !== true) || node.clock_ok !== true ||
            read(node.clock_uncertainty_seconds, 20) === null) return [];
        const part = freshTelemetry(node[name], now);
        if (!part || read(part[timestamp]) === null || read(part[duration], 60) === null) return [];
        return [{part, first: part[timestamp] - part[duration] - node.clock_uncertainty_seconds,
          last: part[timestamp] + node.clock_uncertainty_seconds}];
      });
    };
    const prefill = parts('prefill', 'log_at', 'log_skew_seconds');
    const decode = parts('decode', 'log_at', 'log_skew_seconds');
    const hbm = parts('hbm', 'sample_at', 'span_seconds', false);
    const recordCoverage = (key, values) => {
      coverage[key].valid = values.length;
      coverage[key].workers_valid = values.reduce((sum, {part}) => sum + (part.valid_endpoints || 0), 0);
      coverage[key].partial = values.some(({part}) => part.partial === true);
      if (values.length) coverage[key].span_seconds = Math.max(...values.map((x) => x.last)) - Math.min(...values.map((x) => x.first));
    };
    const sumRate = (key, values) => {
      const valid = values.filter(({part}) => read(part.tokens_per_second) !== null);
      recordCoverage(key, valid);
      const total = valid.reduce((sum, {part}) => sum + part.tokens_per_second, 0);
      return valid.length && Number.isFinite(total) ? total : null;
    };
    const weighted = (key, values, value, weight, limit = Infinity) => {
      const valid = values.filter(({part}) => read(part[value], limit) !== null && read(part[weight]) > 0 &&
        (weight !== 'device_count' || Number.isSafeInteger(part[weight])));
      recordCoverage(key, valid);
      const denominator = valid.reduce((sum, {part}) => sum + part[weight], 0);
      const numerator = valid.reduce((sum, {part}) => sum + part[value] * part[weight], 0);
      return denominator > 0 && Number.isFinite(denominator) && Number.isFinite(numerator) ? numerator / denominator : null;
    };
    return {prefill: sumRate('prefill', prefill), decode: sumRate('decode', decode),
      mfu: weighted('mfu', prefill, 'mfu_estimate', 'peak_flops'),
      hbm: weighted('hbm', hbm, 'mean_percent', 'device_count', 100), coverage};
  }
  const item = nodes.find((node) => node && node.backend_port === port && node.source_ok === true);
  const usable = item && item.clock_ok === true && read(item.clock_uncertainty_seconds, 20) !== null;
  const engineReady = usable && item.backend_ready === true;
  const prefill = engineReady && freshTelemetry(item.prefill, now);
  const decode = engineReady && freshTelemetry(item.decode, now);
  const hbm = usable && freshTelemetry(item.hbm, now);
  return {prefill: read(prefill && prefill.tokens_per_second), decode: read(decode && decode.tokens_per_second),
    mfu: read(prefill && prefill.mfu_estimate), hbm: read(hbm && hbm.mean_percent, 100)};
}

function latestHistoricalSample(history, phase, now, bucketStart = null) {
  if (!history || !['prefill', 'decode'].includes(phase) || !Number.isFinite(now) ||
      !['deepinfer-log-step-approx-v1', 'vllm-counter-rate-v1'].includes(history.method) || history.bucket_seconds !== 600 ||
      !Array.isArray(history.points) || history.points.length !== 144) return null;
  const points = history.points;
  if (points.some((point, i) => !point || !Number.isSafeInteger(point.start) || point.start < 0 ||
      point.end !== point.start + 600 || point.end > now || (i && point.start !== points[i - 1].start + 600))) return null;
  if (history.aggregate && (!Number.isSafeInteger(history.group_count) || history.group_count <= 0)) return null;
  for (let i = points.length - 1; i >= 0; i--) {
    const point = points[i], value = point[`${phase}_tps`], samples = point[`${phase}_samples`];
    if (bucketStart !== null && point.start !== bucketStart) continue;
    const groups = history.aggregate ? point[`${phase}_groups`] : 1;
    const total = history.aggregate ? (history[`${phase}_group_count`] ?? history.group_count) : 1;
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0 &&
        Number.isSafeInteger(samples) && samples > 0 && Number.isSafeInteger(groups) && groups > 0 && groups <= total) {
      return {value, samples, start: point.start, end: point.end, groups, total, partial: point[`${phase}_partial`] === true};
    }
  }
  return null;
}

function latestGroupHistory(data, nodes, port, phase, now) {
  if (!data || data.available !== true || data.history_available !== true || !Array.isArray(nodes) || !nodes.length ||
      new Set(nodes.map((node) => node && node.backend_port)).size !== nodes.length) return null;
  const selected = (port === 'all' ? nodes : nodes.filter((node) => node && node.backend_port === port))
    .filter((node) => data.kind !== 'vllm-motor-telemetry' || node.role === phase);
  if (!selected.length || (port !== 'all' && selected.length !== 1)) return null;
  const candidates = selected.flatMap((node) => {
    if (!node || node.source_ok !== true || !node.history || node.history.aggregate) return [];
    const value = latestHistoricalSample(node.history, phase, now);
    return value ? [value] : [];
  });
  if (!candidates.length) return null;
  const newest = Math.max(...candidates.map((value) => value.start));
  const matching = candidates.filter((value) => value.start === newest);
  const value = matching.reduce((sum, point) => sum + point.value, 0);
  if (!Number.isFinite(value)) return null;
  return {value, start: newest, end: newest + 600, groups: matching.length,
    total: selected.length, samples: port === 'all' ? 1 : matching[0].samples,
    partial: matching.some((point) => point.partial)};
}

function historicalPrefillMfu(nodes, port, sample, now) {
  if (!sample || !Array.isArray(nodes)) return null;
  let work = 0, peak = 0, groups = 0;
  nodes.forEach((node) => {
    if (!node || (port !== 'all' && node.backend_port !== port) || node.source_ok !== true ||
        node.clock_ok !== true || typeof node.clock_uncertainty_seconds !== 'number' ||
        !Number.isFinite(node.clock_uncertainty_seconds) || node.clock_uncertainty_seconds < 0 ||
        node.clock_uncertainty_seconds > 20) return;
    const point = latestHistoricalSample(node.history, 'prefill', now, sample.start);
    const hbm = freshTelemetry(node.hbm, now), assumptions = node.assumptions;
    if (!point || point.end !== sample.end || !hbm || !Number.isSafeInteger(hbm.device_count) ||
        hbm.device_count < 1 || hbm.device_count > 64 || !assumptions ||
        typeof assumptions.prefill_flops_per_token !== 'number' || !Number.isFinite(assumptions.prefill_flops_per_token) ||
        assumptions.prefill_flops_per_token <= 0 || typeof assumptions.peak_flops_per_device !== 'number' ||
        !Number.isFinite(assumptions.peak_flops_per_device) || assumptions.peak_flops_per_device <= 0) return;
    const numerator = point.value * assumptions.prefill_flops_per_token;
    const denominator = hbm.device_count * assumptions.peak_flops_per_device;
    if (!Number.isFinite(numerator) || !Number.isFinite(denominator) || denominator <= 0) return;
    work += numerator; peak += denominator; groups++;
  });
  const value = work / peak;
  return groups && Number.isFinite(value) && value >= 0 ? {...sample, value, groups} : null;
}

function renderTelemetry(data) {
  telemetrySnapshot = data;
  if (telemetryExpiry) clearTimeout(telemetryExpiry);
  telemetryExpiry = null;
  const now = Date.now() / 1000;
  const motor = data && data.kind === 'vllm-motor-telemetry';
  const nodes = data && data.available === true && Array.isArray(data.nodes) ? data.nodes.filter((item) => item && typeof item === 'object') : [];
  if (telemetryNodePort !== 'all' && !nodes.some((item) => item.backend_port === telemetryNodePort)) telemetryNodePort = 'all';
  const select = $('telemetry-node');
  if (select) {
    const key = JSON.stringify(nodes.map((item) => [item.backend_port, item.label]));
    if (key !== telemetryOptionsKey) {
      select.replaceChildren();
      const all = document.createElement('option'); all.value = 'all'; all.textContent = '跨设备组（全部）'; select.appendChild(all);
      nodes.forEach((item) => {
        const option = document.createElement('option'); option.value = String(item.backend_port);
        option.textContent = item.label || '未知实例'; select.appendChild(option);
      });
      telemetryOptionsKey = key;
    }
    select.value = String(telemetryNodePort);
  }
  const values = telemetryValues(data, telemetryNodePort, now);
  const selectedNode = telemetryNodePort === 'all' ? combinedDeviceHistory(data, nodes) : nodes.find((item) => item.backend_port === telemetryNodePort);
  renderDeviceHistory(data, selectedNode);
  const prefillSample = selectedNode && freshTelemetry(selectedNode.prefill, now);
  const decodeSample = selectedNode && freshTelemetry(selectedNode.decode, now);
  const measured = (value) => value === null || value === undefined ? '—' : speed(value);
  const showMfu = (value) => value !== null && value !== undefined && value > 0 && value < .0001 ? '<0.01%' : percent(value);
  const previousPrefill = latestGroupHistory(data, nodes, telemetryNodePort, 'prefill', now);
  const previousDecode = latestGroupHistory(data, nodes, telemetryNodePort, 'decode', now);
  const previousMfu = historicalPrefillMfu(nodes, telemetryNodePort, previousPrefill, now);
  // A browser can receive old HTML and new JS across a local restart. Never
  // place a historical value in the primary position unless both its title
  // and its non-real-time detail can be shown by that HTML version.
  const usePrefillHistory = values.prefill === null && previousPrefill && $('live-prefill-title') && $('live-prefill-samples');
  const useDecodeHistory = values.decode === null && previousDecode && $('live-decode-title') && $('live-decode-samples');
  const useMfuHistory = values.mfu === null && previousMfu && $('live-mfu-title') && $('live-mfu-detail');
  setTextIfPresent('live-prefill-title', usePrefillHistory ? 'Prefill 并发吞吐（历史均值）' : 'Prefill 并发吞吐（采样）');
  setTextIfPresent('live-decode-title', useDecodeHistory ? 'Decode 并发吞吐（历史均值）' : 'Decode 并发吞吐（采样）');
  setTextIfPresent('live-mfu-title', useMfuHistory ? 'Prefill-MFU（历史估算）' : 'Prefill-MFU（单步估算）');
  setTextIfPresent('live-prefill-tps', measured(usePrefillHistory ? previousPrefill.value : values.prefill));
  setTextIfPresent('live-decode-tps', measured(useDecodeHistory ? previousDecode.value : values.decode));
  setTextIfPresent('live-mfu', showMfu(useMfuHistory ? previousMfu.value : values.mfu));
  setTextIfPresent('live-mbu', '—');
  setTextIfPresent('live-hbm', values.hbm === null ? '—' : `${number.format(values.hbm)}%`);
  const historicalDetail = (sample, method) => {
    if (!sample) return '';
    const period = `${dateTime.format(new Date(sample.start * 1000))}–${clock.format(new Date(sample.end * 1000))}`;
    const scope = `${sample.groups}/${sample.total} 组` +
      (telemetryNodePort === 'all' ? '' : ` · ${count(sample.samples)} 个采样`);
    return `${method} · ${period} · 非实时 · ${scope}${sample.partial ? ' · 部分数据' : ''}`;
  };
  const groupDetail = (key, method) => {
    const item = values.coverage[key];
    if (!item.total) return '采集未连接 · 组数未知';
    if (!item.valid) return `0/${item.total} 组有样本 · 暂无有效数据`;
    const partial = item.valid < item.total || item.partial ? ' · 部分数据' : '';
    const span = item.span_seconds > 30 ? ` · 时间跨度约 ${number.format(item.span_seconds)} s` : '';
    const workers = motor && (key === 'prefill' || key === 'decode') ? ` · ${item.workers_valid}/${item.workers_total} worker` : '';
    return `${item.valid}/${item.total} 组有样本${partial}${workers} · ${method}${span}`;
  };
  const detail = (key, value, sample) => telemetryNodePort === 'all' ? groupDetail(key, motor ? '计数差 / 间隔' : '采样和') :
    value === null ? '所选设备组暂无有效样本' : motor ?
      `${sample.valid_endpoints}/${sample.expected_endpoints} worker · 计数差 / 间隔${sample.partial ? ' · 部分数据' : ''}` :
      `日志 ${clock.format(new Date(sample.log_at * 1000))} 写出 · rank 近似和`;
  setTextIfPresent('live-prefill-samples', usePrefillHistory ? historicalDetail(previousPrefill, '历史采样均值') :
    detail('prefill', values.prefill, prefillSample));
  setTextIfPresent('live-decode-samples', useDecodeHistory ? historicalDetail(previousDecode, '历史采样均值') :
    detail('decode', values.decode, decodeSample));
  setTextIfPresent('live-mfu-detail', useMfuHistory ? historicalDetail(previousMfu, '按当前假设估算') :
    telemetryNodePort === 'all' ? groupDetail('mfu', '按峰值算力加权') :
      values.mfu === null ? '所选设备组暂无有效样本' : '引擎吞吐与模型假设换算');
  setTextIfPresent('live-hbm-detail', telemetryNodePort === 'all' ? groupDetail('hbm', '按卡数加权') :
    values.hbm === null ? '所选设备组暂无有效样本' : '所选设备组驱动读数的平均');
  const topology = motor && data.topology_sources ? ` · 管理源 ${data.topology_sources.available}/${data.topology_sources.total}${data.topology_ambiguous ? ' · 拓扑待确认' : ''}` : '';
  setTextIfPresent('telemetry-status', nodes.length ? `最近读取于 ${clock.format(new Date(data.sampled_at * 1000))}${topology}` : '采样未连接');
  const tbody = $('telemetry-body');
  if (!tbody) return;
  tbody.replaceChildren();
  nodes.forEach((item) => {
    const row = document.createElement('tr');
    const usable = item.source_ok === true && item.clock_ok === true &&
      typeof item.clock_uncertainty_seconds === 'number' && Number.isFinite(item.clock_uncertainty_seconds) &&
      item.clock_uncertainty_seconds >= 0 && item.clock_uncertainty_seconds <= 20;
    const prefill = usable && item.backend_ready === true ? freshTelemetry(item.prefill, now) : null;
    const decode = usable && item.backend_ready === true ? freshTelemetry(item.decode, now) : null;
    const hbm = usable ? freshTelemetry(item.hbm, now) : null;
    const timerStep = item.phase_timers && item.phase_timers.step;
    const timersFresh = prefill && prefill.step === timerStep || decode && decode.step === timerStep;
    const timer = (phase) => {
      const value = timersFresh && item.phase_timers && item.phase_timers[phase];
      return value && value.mean_ms !== null ? motor ? `${number.format(value.mean_ms)} ms · ${count(value.samples)} 请求 · ${value.ranks}/${value.expected_ranks} worker` :
        `${number.format(value.mean_ms)} ms · ${value.ranks}/${value.expected_ranks} rank` : '—';
    };
    cell(row, item.label || '未知实例');
    cell(row, prefill ? `${measured(prefill.tokens_per_second)} · ${motor ? '采样' : '日志'} ${clock.format(new Date(prefill.log_at * 1000))}` : '—');
    cell(row, decode ? `${measured(decode.tokens_per_second)} · ${motor ? '采样' : '日志'} ${clock.format(new Date(decode.log_at * 1000))}` : '—');
    cell(row, prefill ? showMfu(prefill.mfu_estimate) : '—');
    cell(row, hbm ? `${number.format(hbm.mean_percent)}% · ${hbm.device_count} 卡` : '—');
    cell(row, timer('prefill'));
    cell(row, timer('decode'));
    const engine = Boolean(prefill || decode);
    const device = Boolean(hbm);
    cell(row, item.duplicate_source ? '设备组重复，待核验' : !item.clock_ok ? '时钟不可用' :
      !item.source_ok ? (motor ? '指标不可达或实例未就绪' : '引擎版本待核验') : item.backend_ready === false ? '后端未就绪' :
      item.driver_ok === false && !motor ? '驱动版本待核验' :
      engine && device ? '引擎与设备有样本' : engine ? '仅引擎有样本' : device ? '仅设备有样本' : '来源已核验，暂无样本');
    tbody.appendChild(row);
  });
  if (!nodes.length) {
    const row = document.createElement('tr');
    const value = document.createElement('td'); value.colSpan = 8; value.className = 'empty';
    value.textContent = '真实采样尚未连接'; row.appendChild(value); tbody.appendChild(row);
  }
  const profile = selectedNode && selectedNode.assumptions;
  const assumption = profile ? `当前假设 ${number.format(profile.prefill_flops_per_token / 1e9)} GFLOP/输入 Token、${number.format(profile.peak_flops_per_device / 1e12)} TFLOP/s/卡；` : '';
  setTextIfPresent('live-efficiency-note', motor ? '当前 vLLM 指标未提供可用的真实阶段算力或 HBM 带宽归因；Prefill-MFU、Decode-MBU 和 HBM 利用率暂显示未知。' :
    `${assumption}Prefill-MFU 按引擎吞吐和模型假设估算，未计上下文 Attention 等，并非硬件算力实测。HBM 为设备级混合阶段采样，无法直接归因 Decode。`);
  setTextIfPresent('phase-source-note', motor ?
    '吞吐为各 worker 在连续两次采样间的计数增量除以墙钟间隔并相加；Prefill 只计本地计算的输入，Decode 只计输出。采样前后核对拓扑，但无法保证两次读取之间没有变化；通过进程标识和计数回退检测重启，仍可能漏掉内部重启。缺失或冲突的样本不作为零。' :
    '并发吞吐是相同日志 step 的各 DP rank 单步速率近似和。跨组视图对有效采样求和，缺失组不参与并标注覆盖数；利用率只对有效组加权。各组并非同时测量，两个阶段也可能来自不同时刻。');
  setTextIfPresent('telemetry-source-note', motor ?
    '阶段耗时为引擎请求阶段直方图的增量均值，不能还原单个会话；Prefill 和 Decode 分别取对应角色。HBM 阶段带宽尚未接入。' :
    '步耗时来自引擎 CPU 计时日志，包含调度等开销；括号内为有有效计时的 rank 数。HBM 为驱动设备级百分比，不能可靠分离纯 Decode 阶段。');
  setTextIfPresent('device-history-note', motor ?
    '速度为每个 10 分钟区间内的 vLLM 计数差速率采样均值；跨组对当前设备组的有效采样求和。空心点表示部分采集；采集前或不可用期间留空。' :
    '速度是各组日志步骤速率的区间采样均值之和；空心点表示部分设备组有样本。采集前或不可用期间留空。');
  const deadlines = nodes.flatMap((item) => [item.prefill, item.decode, item.hbm])
    .map((part) => part && part.valid_until).filter((value) => typeof value === 'number' && Number.isFinite(value) && value > now);
  if (deadlines.length) telemetryExpiry = setTimeout(() => renderTelemetry(telemetrySnapshot),
    Math.min(2147483647, Math.max(1, (Math.min(...deadlines) - now) * 1000 + 20)));
}

async function refreshTelemetry() {
  if (pendingTelemetry) return pendingTelemetry;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  pendingTelemetry = (async () => {
    try {
      const response = await fetch('/api/v5/telemetry', {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('telemetry unavailable');
      renderTelemetry(await response.json());
    } catch (_) {
      renderTelemetry({available: false, nodes: []});
    } finally {
      clearTimeout(timeout);
      pendingTelemetry = null;
    }
  })();
  return pendingTelemetry;
}

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

function display(value, stats, digits = false, includePartial = true) {
  if (!stats.known && !(includePartial && stats.partial) && !stats.complete) return '—';
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
  if (name === 'clients' && key === 'total') return item.known || item.partial || data.totals.complete ? item.input + item.output : null;
  if (name === 'clients' && key === 'cached' && !item.known && !data.totals.complete) return null;
  if (name === 'clients' && ['input', 'output', 'window_tps'].includes(key) && !item.known && !item.partial && !data.totals.complete) return null;
  if (name === 'days' && key === 'cached' && !item.known && !item.complete) return null;
  if (name === 'days' && key !== 'date' && !item.known && !item.partial && !item.complete) return null;
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
    const complete = data.totals.complete && !client.partial && !client.missing && !client.failed && !client.interrupted;
    const view = {...client, complete, covered_seconds: data.totals.covered_seconds};
    const unknown = client.missing + client.failed + client.interrupted;
    cell(row, client.ip || '未知来源');
    const details = [client.partial ? `${integer.format(client.partial)} 部分` : '', unknown ? `${integer.format(unknown)} 未计量` : ''].filter(Boolean).join('，');
    cell(row, details ? `${integer.format(client.requests)}（${details}）` : integer.format(client.requests));
    cell(row, display(client.input + client.output, view));
    cell(row, display(client.input, view));
    cell(row, display(client.output, view));
    cell(row, display(client.cached, view, false, false));
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
    const response = await fetch('/api/v5/active', {cache: 'no-store', signal: controller.signal});
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
  if (backendExpiry) clearTimeout(backendExpiry);
  backendExpiry = null;
  const now = Date.now() / 1000;
  const body = $('backends-body');
  body.replaceChildren();
  if (!data.backends.length) { empty(body, 7, '未配置后端原生指标'); return; }
  localPage('backends', data.backends, data).forEach((backend) => {
    const row = document.createElement('tr');
    cell(row, backend.label || String(backend.port));
    const hasDeadline = Object.prototype.hasOwnProperty.call(backend, 'valid_until');
    const current = backend.available && (!hasDeadline || typeof backend.valid_until === 'number' &&
      Number.isFinite(backend.valid_until) && now < backend.valid_until);
    if (!current) {
      cell(row, '不可用');
      for (let i = 0; i < 5; i++) cell(row, '—');
    } else {
      const prefix = backend.partial ? '≥' : '';
      cell(row, backend.running === null || backend.running === undefined ? '—' : prefix + count(backend.running));
      cell(row, backend.waiting === null || backend.waiting === undefined ? '—' : prefix + count(backend.waiting));
      cell(row, backend.workers_expected === undefined ? count(backend.workers_alive) : `${count(backend.workers_alive)}/${count(backend.workers_expected)}`);
      cell(row, backend.admission_ready === 1 ? '就绪' : '未就绪');
      cell(row, backend.input_total === null || backend.input_total === undefined ? '—' : prefix + count(backend.input_total));
      cell(row, `${percent(backend.hit_rate)}${backend.partial && backend.hit_rate !== null && backend.hit_rate !== undefined ? ' · 部分' : ''}`);
    }
    body.appendChild(row);
  });
  const deadlines = data.backends.filter((backend) => backend.available && typeof backend.valid_until === 'number' &&
    Number.isFinite(backend.valid_until) && backend.valid_until > now).map((backend) => backend.valid_until);
  if (deadlines.length) backendExpiry = setTimeout(() => renderBackends(data),
    Math.min(2147483647, Math.max(1, (Math.min(...deadlines) - now) * 1000 + 20)));
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
    const view = {known: item.known, partial: item.partial, complete: data.snapshot_complete && item.known === item.requests};
    cell(row, fingerprint(item.session));
    cell(row, item.ips.length ? item.ips.join(', ') : '未知来源');
    cell(row, `${count(item.requests)} / ${count(item.known)} 完整 / ${count(item.partial || 0)} 部分`);
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
  const states = {known: '已计量', partial: '部分用量', missing: '未计量', failed: '失败', interrupted: '中断'};
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
    cell(row, display(day.cached, day, false, false));
    body.appendChild(row);
  });
}

function svgElement(name, attributes) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', name);
  Object.entries(attributes).forEach(([key, value]) => el.setAttribute(key, String(value)));
  return el;
}

function attachTrendTooltip(svg, descriptions) {
  const tooltip = $('trend-tooltip');
  const previousPointer = tooltip && tooltip._trendActive === svg ? svg._trendPointer : null;
  const previousKeyboard = tooltip && tooltip._trendActive === svg ? svg._trendKeyboardIndex : null;
  const hide = () => {
    if (tooltip && tooltip._trendOwner === svg) { tooltip.hidden = true; tooltip._trendOwner = null; }
  };
  hide();
  svg.onpointermove = svg.onpointerleave = svg.onfocus = svg.onblur = svg.onkeydown = null;
  svg.removeAttribute('tabindex');
  svg.removeAttribute('aria-describedby');
  if (!tooltip || !Array.isArray(descriptions) || !descriptions.some((text) => typeof text === 'string' && text)) {
    svg._trendPointer = null; svg._trendKeyboardIndex = null;
    if (tooltip && tooltip._trendActive === svg) tooltip._trendActive = null;
    return;
  }
  svg.appendChild(svgElement('rect', {x: 0, y: 0, width: 800, height: 68, fill: 'transparent', class: 'trend-hover-overlay'}));
  svg.setAttribute('tabindex', '0');
  svg.setAttribute('aria-describedby', 'trend-tooltip');
  let selected = Number.isSafeInteger(svg._trendKeyboardIndex) ? svg._trendKeyboardIndex : -1;
  const activate = () => {
    if (tooltip._trendOwner && tooltip._trendOwner !== svg) { tooltip.hidden = true; tooltip._trendOwner = null; }
    tooltip._trendActive = svg;
  };
  const show = (index, x, y) => {
    const description = descriptions[index];
    selected = index;
    if (typeof description !== 'string' || !description) { hide(); return; }
    tooltip._trendOwner = svg;
    tooltip.textContent = description;
    tooltip.hidden = false;
    const box = tooltip.getBoundingClientRect();
    const width = document.documentElement.clientWidth || window.innerWidth;
    const height = document.documentElement.clientHeight || window.innerHeight;
    tooltip.style.left = `${Math.max(8, Math.min(x + 12, width - box.width - 8))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(y + 12, height - box.height - 8))}px`;
  };
  svg.onpointermove = (event) => {
    const bounds = svg.getBoundingClientRect();
    if (!Number.isFinite(event.clientX) || !Number.isFinite(event.clientY) || bounds.width <= 0 ||
        event.clientX < bounds.left || event.clientX > bounds.left + bounds.width) { svg._trendPointer = null; hide(); return; }
    activate();
    svg._trendPointer = {clientX: event.clientX, clientY: event.clientY};
    svg._trendKeyboardIndex = null;
    const index = descriptions.length === 1 ? 0 : Math.round((event.clientX - bounds.left) / bounds.width * (descriptions.length - 1));
    show(index, event.clientX, event.clientY);
  };
  const showSelected = (index) => {
    const bounds = svg.getBoundingClientRect();
    const fraction = descriptions.length === 1 ? 0 : index / (descriptions.length - 1);
    svg._trendKeyboardIndex = index;
    svg._trendPointer = null;
    show(index, bounds.left + fraction * bounds.width, bounds.top);
  };
  svg.onpointerleave = () => { svg._trendPointer = null; hide(); if (tooltip._trendActive === svg) tooltip._trendActive = null; };
  svg.onfocus = () => { activate(); const first = descriptions.findIndex((value) => typeof value === 'string' && value); if (first >= 0) showSelected(first); };
  svg.onblur = () => { svg._trendKeyboardIndex = null; hide(); if (tooltip._trendActive === svg) tooltip._trendActive = null; };
  svg.onkeydown = (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    activate();
    const direction = event.key === 'ArrowRight' ? 1 : -1;
    for (let index = selected + direction; index >= 0 && index < descriptions.length; index += direction) {
      if (typeof descriptions[index] === 'string' && descriptions[index]) { event.preventDefault(); showSelected(index); break; }
    }
  };
  if (previousPointer) svg.onpointermove(previousPointer);
  else if (Number.isSafeInteger(previousKeyboard) && previousKeyboard >= 0 && previousKeyboard < descriptions.length) showSelected(previousKeyboard);
}

const charts = [
  {id: 'input', field: 'input', kind: 'tokens', label: '输入'},
  {id: 'output', field: 'output', kind: 'tokens', label: '输出'},
  {id: 'cache-rate', style: 'cached', field: 'mean_request_hit_rate', kind: 'hit', label: '平均请求命中率', samples: 'request_hit_samples'},
];

function combinedDeviceHistory(data, nodes) {
  if (!data || data.available !== true || data.history_available !== true || !nodes.length ||
      new Set(nodes.map((node) => node && node.backend_port)).size !== nodes.length) return null;
  const method = data.kind === 'vllm-motor-telemetry' ? 'vllm-counter-rate-v1' : 'deepinfer-log-step-approx-v1';
  const usable = nodes.flatMap((node) => {
    if (!node || node.source_ok !== true || !node.history || node.history.method !== method ||
        node.history.bucket_seconds !== 600 || !Array.isArray(node.history.points) || node.history.points.length !== 144) return [];
    const buckets = new Map();
    for (const point of node.history.points) {
      if (!point || !Number.isSafeInteger(point.start) || point.end !== point.start + 600 || buckets.has(point.start)) return [];
      buckets.set(point.start, point);
    }
    return [{points: node.history.points, buckets}];
  });
  if (!usable.length) return null;
  const points = usable[0].points.map((point, index) => {
    if (point.start !== usable[0].points[0].start + index * 600) return null;
    const all = usable.map((node) => node.buckets.get(point.start)).filter((item) =>
      item && item.start === point.start && item.end === point.end);
    const combined = {start: point.start, end: point.end};
    ['prefill', 'decode'].forEach((phase) => {
      const valid = all.filter((item) => typeof item[`${phase}_tps`] === 'number' &&
        Number.isFinite(item[`${phase}_tps`]) && item[`${phase}_tps`] >= 0 &&
        Number.isSafeInteger(item[`${phase}_samples`]) && item[`${phase}_samples`] > 0);
      const total = valid.length ? valid.reduce((sum, item) => sum + item[`${phase}_tps`], 0) : null;
      combined[`${phase}_tps`] = total !== null && Number.isFinite(total) ? total : null;
      combined[`${phase}_samples`] = combined[`${phase}_tps`] === null ? 0 : 1;
      combined[`${phase}_groups`] = combined[`${phase}_tps`] === null ? 0 : valid.length;
      combined[`${phase}_partial`] = valid.some((item) => item[`${phase}_partial`] === true);
    });
    return combined;
  });
  if (points.some((point) => point === null)) return null;
  return {history: {method, bucket_seconds: 600, aggregate: true, group_count: nodes.length,
    prefill_group_count: nodes.filter((node) => data.kind !== 'vllm-motor-telemetry' || node.role === 'prefill').length,
    decode_group_count: nodes.filter((node) => data.kind !== 'vllm-motor-telemetry' || node.role === 'decode').length, points}};
}

function renderDeviceHistory(data, selectedNode) {
  const dropped = data && Number.isSafeInteger(data.history_dropped) && data.history_dropped > 0 ? data.history_dropped : 0;
  setTextIfPresent('device-history-health', dropped ? ` 历史队列已跳过 ${count(dropped)} 次采集。` : '');
  const history = data && data.available === true && data.history_available === true && selectedNode && selectedNode.history;
  const points = history && ['deepinfer-log-step-approx-v1', 'vllm-counter-rate-v1'].includes(history.method) && history.bucket_seconds === 600 &&
    Array.isArray(history.points) && history.points.length === 144 ? history.points : null;
  ['prefill', 'decode'].forEach((phase) => {
    const svg = $(`chart-device-${phase}-svg`);
    const label = $(`chart-device-${phase}`);
    if (!svg || !label) return;
    svg.replaceChildren();
    [16, 34, 52].forEach((y) => svg.appendChild(svgElement('line', {x1: 0, y1: y, x2: 800, y2: y, class: 'grid'})));
    if (!points) { attachTrendTooltip(svg, []); label.textContent = '设备历史采样未连接'; return; }
    const valid = points.map((point) => {
      const value = point && point[`${phase}_tps`];
      const samples = point && point[`${phase}_samples`];
      return typeof value === 'number' && Number.isFinite(value) && value >= 0 && Number.isSafeInteger(samples) && samples > 0 ? value : null;
    });
    const peak = Math.max(1, ...valid.filter((value) => value !== null));
    const descriptions = Array(points.length).fill(null);
    let segment = [], total = 0, samples = 0, segmentPartial = false, partialBuckets = 0;
    const draw = () => {
      if (segment.length > 1) svg.appendChild(svgElement('polyline', {points: segment.join(' '), class: `${phase}${segmentPartial ? ' partial' : ''}`}));
      segment = [];
    };
    valid.forEach((value, i) => {
      if (value === null) { draw(); return; }
      const groups = history[`${phase}_group_count`] ?? history.group_count;
      const partial = points[i][`${phase}_partial`] === true || history.aggregate && points[i][`${phase}_groups`] < groups;
      if (segment.length && partial !== segmentPartial) draw();
      segmentPartial = partial;
      const x = i * 800 / (points.length - 1), y = 59 - value / peak * 50;
      segment.push(`${x.toFixed(2)},${y.toFixed(2)}`);
      const marker = svgElement('circle', {cx: x, cy: y, r: 2.5, class: `${phase}${partial ? ' partial' : ''}`});
      const title = svgElement('title', {});
      const scope = history.aggregate ? `${points[i][`${phase}_groups`]}/${groups} 组采样均值之和${partial ? ' · 部分数据' : ''}` : `${count(points[i][`${phase}_samples`])} 个采样${partial ? ' · 部分数据' : ''}`;
      const period = `${dateTime.format(new Date(points[i].start * 1000))}–${dateTime.format(new Date(points[i].end * 1000))}`;
      title.textContent = `${period}：${speed(value)} · ${scope}`;
      descriptions[i] = `${period}\n${phase === 'prefill' ? 'Prefill' : 'Decode'} 并发吞吐：${speed(value)}\n${scope}`;
      marker.appendChild(title); svg.appendChild(marker);
      if (partial) partialBuckets++;
      total += value * points[i][`${phase}_samples`]; samples += points[i][`${phase}_samples`];
    });
    draw();
    attachTrendTooltip(svg, descriptions);
    label.textContent = samples ? `${speed(total / samples)} · ${count(samples)} ${history.aggregate ? `个有采样区间${partialBuckets ? ` · ${count(partialBuckets)} 个仅部分组` : ''}` : '个采样'}` : '暂无设备采样';
  });
}

function renderPlot(chart, points) {
  const {id, field, kind} = chart;
  const svg = $(`chart-${id}-svg`);
  const label = $(`chart-${id}`);
  if (!svg || !label) return;
  svg.replaceChildren();
  [16, 34, 52].forEach((y) => svg.appendChild(svgElement('line', {x1: 0, y1: y, x2: 800, y2: y, class: 'grid'})));
  const required = kind === 'hit' ? [field, 'request_hit_sum', 'request_hit_samples'] :
    kind === 'speed' ? [field, `${id}_tokens`, `${id}_seconds`, chart.samples] : [];
  if (points.some((point) => required.some((key) => !Object.prototype.hasOwnProperty.call(point, key)))) {
    attachTrendTooltip(svg, []);
    label.textContent = '当前数据版本不支持';
    return;
  }
  const values = points.map((point) => point.end - point.start >= 599.99 && (point.known || point.partial || point.complete) &&
    typeof point[field] === 'number' && Number.isFinite(point[field]) ? point[field] : null);
  const peak = Math.max(1, ...values.filter((value) => value !== null));
  const descriptions = Array(points.length).fill(null);
  let segment = [];
  const draw = () => {
    if (segment.length > 1) svg.appendChild(svgElement('polyline', {points: segment.join(' '), class: chart.style || id}));
    segment = [];
  };
  values.forEach((value, i) => {
    if (value === null) { draw(); return; }
    const x = points.length > 1 ? i * 800 / (points.length - 1) : 0;
    const y = 59 - value / peak * 50;
    segment.push(`${x.toFixed(2)},${y.toFixed(2)}`);
    const marker = svgElement('circle', {cx: x, cy: y, r: 2.5, class: chart.style || id});
    const title = svgElement('title', {});
    const formatted = kind === 'tokens' ? `${integer.format(value)} Token` : kind === 'hit' ? percent(value) : speed(value);
    const boundary = points[i].complete ? '' : kind === 'tokens' ? '至少 ' : '已观测样本 ';
    const samples = chart.samples ? ` · ${count(points[i][chart.samples])} 个样本` : '';
    const period = `${dateTime.format(new Date(points[i].start * 1000))}–${dateTime.format(new Date(points[i].end * 1000))}`;
    title.textContent = `${period}：${boundary}${formatted}${samples}`;
    descriptions[i] = `${period}\n${chart.label || id}：${boundary}${formatted}${samples}`;
    marker.appendChild(title);
    svg.appendChild(marker);
  });
  draw();
  attachTrendTooltip(svg, descriptions);
  const sum = (key) => points.reduce((total, point) => total + (point[key] || 0), 0);
  if (kind === 'tokens') {
    const any = points.some((point) => point.known || point.partial || point.complete);
    const complete = points.filter((point) => point.end > point.start).every((point) => point.complete);
    label.textContent = any ? `${complete ? '' : '≥'}${integer.format(sum(field))}` : '暂无数据';
  } else if (kind === 'hit') {
    const samples = sum(chart.samples);
    label.textContent = samples ? `${percent(sum('request_hit_sum') / samples)} · ${count(samples)} 请求` : '暂无样本';
  } else {
    const samples = sum(chart.samples);
    const duration = sum(`${id}_seconds`);
    label.textContent = samples && duration ? `${speed(sum(`${id}_tokens`) / duration)} · ${count(samples)} 样本` : '暂无样本';
  }
}

function render(data) {
  const stats = data.totals;
  $('status-dot').classList.toggle('live', data.collector_live);
  $('status-label').textContent = data.collector_live ? '采集器心跳正常' : '入口采集未连接';
  $('updated').textContent = `数据查询于 ${clock.format(new Date(data.as_of * 1000))}`;
  $('total-usage').textContent = display(stats.input + stats.output, stats);
  $('total-input').textContent = display(stats.input, stats);
  $('total-output').textContent = display(stats.output, stats);
  $('total-cached').textContent = display(stats.cached, stats, false, false);
  $('total-tps').textContent = stats.window_tps === null ? '—' : display(stats.window_tps, stats, true);
  $('cache-detail').textContent = stats.hit_rate === null ? '有缓存明细的请求命中率 —' : `有缓存明细的请求按输入 Token 加权 ${number.format(stats.hit_rate * 100)}%`;
  setTextIfPresent('overview-prefill-wait', seconds(stats.first_mean));
  setTextIfPresent('overview-hit-rate', percent(stats.hit_rate));
  setTextIfPresent('overview-wait-samples', `${count(stats.first_samples)} 个可见增量样本`);
  setTextIfPresent('overview-hit-samples', `按输入 Token 加权 · ${count(stats.known)} 个有缓存明细的请求`);
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
  let overload = '';
  if (stats.overload_untracked_runs || stats.unclean_runs) {
    const reasons = [stats.overload_untracked_runs ? '部分周期未细分' : '',
      stats.unclean_runs ? '存在未正常结束周期' : ''].filter(Boolean).join('、');
    overload = stats.overload_tracked_runs ?
      ` · 相关采集周期内代理超载降级已记录至少 ${integer.format(stats.overload_rejected)} 次（${reasons}）` :
      ` · 相关采集周期内代理超载降级次数未知（${reasons}）`;
  } else if (stats.overload_tracked_runs) {
    overload = ` · 相关采集周期内代理超载降级已记录 ${integer.format(stats.overload_rejected)} 次`;
  }
  notice.textContent = `${collectorNotice}完整用量 ${integer.format(stats.known)} 个请求 · 仅输入/输出 ${integer.format(stats.partial || 0)} 个 · 未计量 ${integer.format(unknown)} 个 · 未知来源 ${integer.format(stats.unknown_source)} 个 · 未正常结束或失联 ${integer.format(stats.unclean_runs)} 次 · 相关采集周期未完整计入 ${integer.format(stats.dropped)} 个${overload} · 采集进程运行覆盖 ${number.format(coverage)}%。仅统计采集器已写盘的请求记录；缓存命中率仅使用有缓存明细的请求。入口回退、网关内部重试及取消时的消耗可能无法观测，因此不是后端物理总消耗。${stats.complete ? '' : ' ≥ 表示已观测下限；无覆盖不视为零。'}`;
  charts.forEach((chart) => renderPlot(chart, data.series_24h));
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
    const response = await fetch(`/api/v5/stats?${query}`, {cache: 'no-store', signal: controller.signal});
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
    if (backendExpiry) clearTimeout(backendExpiry);
    backendExpiry = null;
    $('status-dot').classList.remove('live');
    $('status-label').textContent = '统计不可用';
    $('updated').textContent = '请检查本机采集和账本';
    ['total-usage', 'total-input', 'total-output', 'total-cached', 'total-tps',
      'overview-prefill-wait', 'overview-hit-rate'].forEach((id) => setTextIfPresent(id, '—'));
    ['overview-wait-samples', 'overview-hit-samples'].forEach((id) => setTextIfPresent(id, '统计不可用'));
    charts.forEach(({id}) => {
      const svg = $(`chart-${id}-svg`), label = $(`chart-${id}`);
      if (svg) { svg.replaceChildren(); attachTrendTooltip(svg, []); }
      if (label) label.textContent = '暂无数据';
    });
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
$('telemetry-node')?.addEventListener('change', (event) => {
  telemetryNodePort = event.target.value === 'all' ? 'all' : Number(event.target.value);
  if (telemetrySnapshot) renderTelemetry(telemetrySnapshot);
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    if (telemetrySnapshot) renderTelemetry(telemetrySnapshot);
    refreshTelemetry();
  }
});
refresh();
refreshTelemetry();
setInterval(() => { if (!pendingRefresh) refresh(); }, 10000);
setInterval(refreshActive, 2000);
setInterval(refreshTelemetry, 15000);
