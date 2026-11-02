import shutil
import subprocess
import unittest
from pathlib import Path


class DashboardEfficiencyTests(unittest.TestCase):
    def test_old_and_new_phase_cards_cannot_cross_write(self):
        from deploy_stats.web import ASSETS
        root = Path(__file__).resolve().parents[1]/'deploy_stats/static'
        html = (root/'index.html').read_text(encoding='utf-8')
        script = (root/'app.js').read_text(encoding='utf-8')
        legacy_ids = ('overview-prefill-tps', 'overview-prefill-samples', 'overview-decode-tps',
                      'overview-decode-samples', 'overview-mfu', 'overview-mfu-detail',
                      'overview-mbu', 'overview-mbu-detail', 'overview-efficiency-note')
        for legacy in legacy_ids:
            self.assertNotIn(f'id="{legacy}"', html)
            self.assertNotIn(f"'{legacy}'", script)
        self.assertIn('/app-telemetry-v4.js', html)
        self.assertIn('/app-telemetry-v4.js', ASSETS)
        self.assertNotIn('/app-telemetry-v3.js', ASSETS)
        self.assertNotIn('/app-telemetry-v2.js', ASSETS)
        self.assertIn('chart-device-prefill', html)
        self.assertIn('chart-device-decode', html)
        self.assertIn('value="all">跨设备组（全部）', html)
        self.assertIn("let telemetryNodePort = 'all'", script)
        self.assertNotIn('id="chart-prefill"', html)
        self.assertNotIn('id="chart-decode"', html)
        self.assertNotIn('/app-trends-v1.js', ASSETS)
        self.assertNotIn('/app.js', ASSETS)

    def test_telemetry_values_preserve_unknown_and_real_zero(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const freshStart = source.indexOf('function freshTelemetry(');
const freshEnd = source.indexOf('\n}\n', freshStart);
assert(freshStart >= 0 && freshEnd > freshStart);
const freshTelemetry = vm.runInNewContext('(' + source.slice(freshStart, freshEnd + 2) + ')');
const start = source.indexOf('function telemetryValues(');
const end = source.indexOf('\n}\n', start);
assert(start >= 0 && end > start);
const read = vm.runInNewContext('(' + source.slice(start, end + 2) + ')', {freshTelemetry});
let data = {available: true, nodes: [{backend_port: 1, source_ok: true, backend_ready: true,
  clock_ok: true, clock_uncertainty_seconds: .1,
  prefill: {tokens_per_second: 0, mfu_estimate: .25, valid_until: 101},
  decode: {tokens_per_second: 100, valid_until: 101}, hbm: {mean_percent: 35, valid_until: 101}}]};
let value = read(data, 1, 100);
assert.equal(value.prefill, 0);
assert.equal(value.decode, 100);
assert.equal(value.mfu, .25);
assert.equal(value.hbm, 35);
value = read({available: true, nodes: [{backend_port: 1, source_ok: true, backend_ready: true,
  clock_ok: true, clock_uncertainty_seconds: .1,
  prefill: {tokens_per_second: null, mfu_estimate: -1, valid_until: 101},
  decode: {tokens_per_second: Infinity, valid_until: 101}, hbm: {mean_percent: 101, valid_until: 101}}]}, 1, 100);
for (const key of ['prefill', 'decode', 'mfu', 'hbm']) assert.equal(value[key], null);
assert.equal(read(data, 1, 101).prefill, null);
assert.equal(read(data, 2, 100).prefill, null);
assert.equal(read({available: false, nodes: data.nodes}, 1, 100).prefill, null);
data.nodes[0].backend_ready = false;
value = read(data, 1, 100);
assert.equal(value.prefill, null); assert.equal(value.decode, null); assert.equal(value.mfu, null);
assert.equal(value.hbm, 35); // Device reading remains separately valid.
data.nodes[0].backend_ready = true; data.nodes[0].clock_ok = false;
value = read(data, 1, 100);
for (const key of ['prefill', 'decode', 'mfu', 'hbm']) assert.equal(value[key], null);
data.nodes[0].clock_ok = true; data.nodes[0].clock_uncertainty_seconds = null;
assert.equal(read(data, 1, 100).decode, null);
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_backend_table_does_not_show_unready_or_clock_invalid_rates(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name, ctx) {
  const start = source.indexOf(`function ${name}(`), end = source.indexOf('\n}\n', start);
  assert(start >= 0 && end > start); return vm.runInNewContext('(' + source.slice(start, end + 2) + ')', ctx);
}
const body = {children: [], replaceChildren() {this.children = [];}, appendChild(row) {this.children.push(row);}};
const context = {telemetrySnapshot: null, telemetryExpiry: null, telemetryNodePort: 'all', telemetryOptionsKey: null,
  Date: class extends Date {static now() {return 100000;}}, clearTimeout: () => {}, setTimeout: () => 1,
  $: (id) => id === 'telemetry-body' ? body : null, document: {createElement: () => ({cells: []})},
  cell: (row, value) => row.cells.push(value), setTextIfPresent: () => {},
  number: {format: String}, speed: (v) => `${v} tok/s`, percent: (v) => `${v * 100}%`,
  clock: {format: () => 'time'}, combinedDeviceHistory: () => null, renderDeviceHistory: () => {}};
context.freshTelemetry = extract('freshTelemetry', context); context.telemetryValues = extract('telemetryValues', context);
const render = extract('renderTelemetry', context);
const n = {backend_port: 1, source_ok: true, backend_ready: true, clock_ok: true, clock_uncertainty_seconds: .1,
  prefill: {tokens_per_second: 20, mfu_estimate: .2, log_at: 95, log_skew_seconds: 0, valid_until: 110},
  decode: {tokens_per_second: 100, log_at: 95, log_skew_seconds: 0, valid_until: 110},
  hbm: {mean_percent: 30, device_count: 1, sample_at: 95, span_seconds: 0, valid_until: 110}};
const data = {available: true, sampled_at: 100, nodes: [n]};
render(data); assert.match(body.children[0].cells[2], /^100 tok\/s/);
n.backend_ready = false; render(data);
assert.equal(body.children[0].cells[1], '—'); assert.equal(body.children[0].cells[2], '—');
assert.equal(body.children[0].cells[4], '30% · 1 卡');
n.backend_ready = true; n.clock_ok = false; render(data);
assert.equal(body.children[0].cells[2], '—'); assert.equal(body.children[0].cells[4], '—');
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_device_history_uses_only_device_samples_and_preserves_gaps(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function renderDeviceHistory(');
const end = source.indexOf('\n}\n', start);
assert(start >= 0 && end > start);
const elements = {};
function makeElement() { return {textContent: '', children: [], replaceChildren() {this.children=[];}, appendChild(v) {this.children.push(v);}}; }
for (const phase of ['prefill', 'decode']) {
  elements[`chart-device-${phase}`] = makeElement();
  elements[`chart-device-${phase}-svg`] = makeElement();
}
const context = {$: (id) => elements[id], svgElement: () => makeElement(), setTextIfPresent: () => {},
  clock: {format: () => 'time'}, speed: (v) => `${v.toFixed(2)} tok/s`, count: String};
const render = vm.runInNewContext('(' + source.slice(start, end + 2) + ')', context);
const points = Array.from({length: 144}, (_, i) => ({start: i*600, end: (i+1)*600,
  prefill_tps: null, decode_tps: null, prefill_samples: 0, decode_samples: 0, prefill_estimate_tps: 99999}));
points[1].prefill_tps = 0; points[1].prefill_samples = 1;
points[3].prefill_tps = 3000; points[3].prefill_samples = 2;
const current = {history: {method: 'deepinfer-log-step-approx-v1', bucket_seconds: 600, points}};
render({available: true, history_available: true}, current);
assert.equal(elements['chart-device-prefill'].textContent, '2000.00 tok/s · 3 个采样');
assert.equal(elements['chart-device-decode'].textContent, '暂无设备采样');
const lines = elements['chart-device-prefill-svg'].children.filter((x) => x.children.length > 0);
assert.equal(lines.length, 2); // Two isolated markers, no line across the missing bucket.
render({available: true, history_available: false}, current);
assert.equal(elements['chart-device-prefill'].textContent, '设备历史采样未连接');
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cross_group_values_ignore_missing_and_weight_only_valid_groups(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name, context) {
  const start = source.indexOf(`function ${name}(`), end = source.indexOf('\n}\n', start);
  assert(start >= 0 && end > start);
  return vm.runInNewContext('(' + source.slice(start, end + 2) + ')', context);
}
const freshTelemetry = extract('freshTelemetry', {});
const read = extract('telemetryValues', {freshTelemetry});
function group(port, rate, mfu, peak, devices, hbm, at = 95) {
  return {backend_port: port, source_ok: true, backend_ready: true, clock_ok: true, clock_uncertainty_seconds: .1,
    prefill: {tokens_per_second: rate, mfu_estimate: mfu, peak_flops: peak, log_at: at, log_skew_seconds: 0, valid_until: 110},
    decode: {tokens_per_second: rate * 2, log_at: at, log_skew_seconds: 0, valid_until: 110},
    hbm: {mean_percent: hbm, device_count: devices, sample_at: at, span_seconds: 0, valid_until: 110}};
}
const a = group(1, 100, .2, 100, 2, 20), b = group(2, 300, .6, 300, 6, 60);
let data = {available: true, nodes: [a, b]};
let value = read(data, 'all', 100);
assert.equal(value.prefill, 400); assert.equal(value.decode, 800);
assert(Math.abs(value.mfu - .5) < 1e-12); assert.equal(value.hbm, 50);
assert.equal(value.coverage.prefill.valid, 2); assert.equal(value.coverage.prefill.total, 2);
assert.equal(read(data, 1, 100).prefill, 100);
b.prefill.tokens_per_second = 0; assert.equal(read(data, 'all', 100).prefill, 100);
b.prefill.log_at = 60; value = read(data, 'all', 100);
assert.equal(value.prefill, 100); assert(value.coverage.prefill.span_seconds > 30);
b.prefill.log_at = 95; b.decode = null; value = read(data, 'all', 100);
assert.equal(value.decode, 200); assert.equal(value.coverage.decode.valid, 1);
b.source_ok = false; value = read(data, 'all', 100);
assert.equal(value.prefill, 100); assert.equal(value.coverage.prefill.valid, 1);
assert.equal(value.coverage.prefill.total, 2); assert.equal(value.mfu, .2); assert.equal(value.hbm, 20);
b.source_ok = true; b.prefill = null; value = read(data, 'all', 100);
assert.equal(value.prefill, 100); assert.equal(value.mfu, .2);
assert.equal(value.coverage.mfu.valid, 1); assert.equal(value.hbm, 50);
b.backend_ready = false; value = read(data, 'all', 100);
assert.equal(value.hbm, 50); assert.equal(value.coverage.hbm.valid, 2);
b.backend_ready = true;
a.prefill.tokens_per_second = 0; value = read(data, 'all', 100);
assert.equal(value.prefill, 0); assert.equal(value.coverage.prefill.valid, 1);
a.prefill.valid_until = 100; value = read(data, 'all', 100);
assert.equal(value.prefill, null); assert.equal(value.coverage.prefill.valid, 0);
b.source_ok = true; b.backend_port = 1; assert.equal(read(data, 'all', 100).prefill, null);
assert.equal(read({available: false, nodes: [a]}, 'all', 100).prefill, null);
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_overview_shows_partial_coverage_and_no_fake_decode_mbu(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name, context) {
  const start = source.indexOf(`function ${name}(`), end = source.indexOf('\n}\n', start);
  assert(start >= 0 && end > start);
  return vm.runInNewContext('(' + source.slice(start, end + 2) + ')', context);
}
const elements = {};
for (const id of ['live-prefill-tps', 'live-decode-tps', 'live-mfu', 'live-mbu', 'live-hbm',
  'live-prefill-samples', 'live-decode-samples', 'live-mfu-detail', 'live-hbm-detail']) elements[id] = {textContent: ''};
const context = {telemetrySnapshot: null, telemetryExpiry: null, telemetryNodePort: 'all', telemetryOptionsKey: null,
  Date: class extends Date {static now() {return 100000;}}, clearTimeout: () => {},
  $: (id) => elements[id] || null, setTextIfPresent: (id, value) => {if (elements[id]) elements[id].textContent = value;},
  number: {format: String}, speed: (v) => `${v} tok/s`, percent: (v) => `${v * 100}%`,
  clock: {format: () => 'time'}, combinedDeviceHistory: () => null, renderDeviceHistory: () => {}};
context.freshTelemetry = extract('freshTelemetry', context);
context.telemetryValues = extract('telemetryValues', context);
const render = extract('renderTelemetry', context);
const base = {source_ok: true, backend_ready: true, clock_ok: true, clock_uncertainty_seconds: .1};
const a = {...base, backend_port: 1, prefill: {tokens_per_second: 100, mfu_estimate: .2, peak_flops: 100,
  log_at: 95, log_skew_seconds: 0, valid_until: 110}, decode: null,
  hbm: {mean_percent: 20, device_count: 2, sample_at: 95, span_seconds: 0, valid_until: 110}};
const b = {...base, backend_port: 2, prefill: null, decode: null, hbm: null};
render({available: true, sampled_at: 100, nodes: [a, b]});
assert.equal(elements['live-prefill-tps'].textContent, '100 tok/s');
assert.match(elements['live-prefill-samples'].textContent, /1\/2 组有样本 · 部分数据/);
assert.match(elements['live-mfu-detail'].textContent, /1\/2 组有样本 · 部分数据/);
assert.equal(elements['live-decode-tps'].textContent, '—');
assert.match(elements['live-decode-samples'].textContent, /0\/2 组有样本/);
assert.equal(elements['live-mbu'].textContent, '—');
assert.equal(elements['live-hbm'].textContent, '20%');
assert.match(elements['live-hbm-detail'].textContent, /1\/2 组有样本 · 部分数据/);
render({available:false,nodes:[]});
assert.equal(elements['live-prefill-tps'].textContent, '—');
assert.equal(elements['live-prefill-samples'].textContent, '采集未连接 · 组数未知');
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cross_group_history_sums_available_groups_and_labels_coverage(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function combinedDeviceHistory('), end = source.indexOf('\n}\n', start);
assert(start >= 0 && end > start);
const combine = vm.runInNewContext('(' + source.slice(start, end + 2) + ')');
function group(port, rate, samples) {
  const points = Array.from({length: 144}, (_, i) => ({start: i*600, end: (i+1)*600,
    prefill_tps: rate, prefill_samples: samples, decode_tps: null, decode_samples: 0}));
  return {backend_port: port, source_ok: true, history: {method: 'deepinfer-log-step-approx-v1', bucket_seconds: 600, points}};
}
const a = group(1, 100, 100), b = group(2, 300, 1), data = {available: true, history_available: true};
let result = combine(data, [a, b]);
assert.equal(result.history.points[0].prefill_tps, 400); // Sum group means, never pool samples.
assert.equal(result.history.points[0].prefill_samples, 1); // Each complete bucket has equal weight.
assert.equal(result.history.points[0].decode_tps, null);
b.history.points[2].prefill_samples = 0;
result = combine(data, [a, b]); assert.equal(result.history.points[2].prefill_tps, 100);
assert.equal(result.history.points[2].prefill_groups, 1); assert.equal(result.history.group_count, 2);
b.history = null; result = combine(data, [a, b]);
assert.equal(result.history.points[0].prefill_tps, 100); assert.equal(result.history.points[0].prefill_groups, 1);
const shifted = group(2, 300, 1); shifted.history.points.forEach((point) => {point.start += 600; point.end += 600;});
result = combine(data, [a, shifted]); assert.equal(result.history.points[0].prefill_tps, 100);
assert.equal(result.history.points[0].prefill_groups, 1);
assert.equal(result.history.points[1].prefill_tps, 400); assert.equal(result.history.points[1].prefill_groups, 2);
a.history.points[1].start = a.history.points[0].start; a.history.points[1].end = a.history.points[0].end;
assert.equal(combine(data, [a, b]), null); // Duplicate buckets cannot be trusted.
a.history.points[1].start = 600; a.history.points[1].end = 1200;
a.source_ok = false; assert.equal(combine(data, [a, b]), null);
assert.equal(combine({available: true, history_available: false}, [a]), null);
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_history_marks_partially_observed_buckets(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function renderDeviceHistory('), end = source.indexOf('\n}\n', start);
assert(start >= 0 && end > start);
function element(name, attrs = {}) {return {name, attrs, textContent: '', children: [],
  replaceChildren() {this.children=[];}, appendChild(value) {this.children.push(value);}};}
const elements = {};
for (const phase of ['prefill', 'decode']) {
  elements[`chart-device-${phase}`] = element('label');
  elements[`chart-device-${phase}-svg`] = element('svg');
}
const context = {$: (id) => elements[id], svgElement: element, setTextIfPresent: () => {},
  clock: {format: () => 'time'}, speed: (value) => `${value} tok/s`, count: String};
const render = vm.runInNewContext('(' + source.slice(start, end + 2) + ')', context);
const points = Array.from({length: 144}, (_, i) => ({start:i*600,end:(i+1)*600,
  prefill_tps:null,prefill_samples:0,prefill_groups:0,decode_tps:null,decode_samples:0,decode_groups:0}));
points[1].prefill_tps = 100; points[1].prefill_samples = 1; points[1].prefill_groups = 1;
points[2].prefill_tps = 120; points[2].prefill_samples = 1; points[2].prefill_groups = 1;
points[3].prefill_tps = 200; points[3].prefill_samples = 1; points[3].prefill_groups = 2;
render({available:true,history_available:true},{history:{method:'deepinfer-log-step-approx-v1',
  bucket_seconds:600,aggregate:true,group_count:2,points}});
assert.match(elements['chart-device-prefill'].textContent, /3 个有采样区间 · 2 个仅部分组/);
const children = elements['chart-device-prefill-svg'].children;
assert(children.some((item) => item.name === 'polyline' && item.attrs.class === 'prefill partial'));
const partial = children.find((item) => item.name === 'circle' && item.attrs.class === 'prefill partial');
assert.match(partial.children[0].textContent, /1\/2 组采样均值之和 · 部分数据/);
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
