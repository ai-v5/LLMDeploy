import shutil
import subprocess
import unittest
from pathlib import Path


class DashboardEfficiencyTests(unittest.TestCase):
    def test_motor_backend_row_expires_without_waiting_for_next_poll(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function renderBackends('), end = source.indexOf('\n}\n', start);
assert(start >= 0 && end > start);
let now = 100, timer;
const body = {children: [], replaceChildren() {this.children=[];}, appendChild(row) {this.children.push(row);}};
const context = {backendExpiry: null, Date: class extends Date {static now() {return now * 1000;}},
  $: () => body, localPage: (_name, rows) => rows, document: {createElement: () => ({cells: []})},
  cell: (row, value) => row.cells.push(value), count: String, percent: String, empty: () => {},
  clearTimeout: () => {}, setTimeout: (fn) => {timer=fn; return 1;}};
const render = vm.runInNewContext('(' + source.slice(start, end + 2) + ')', context);
const data = {backends: [{port: 1, label: 'fixture', available: true, valid_until: 105,
  running: 4, waiting: 0, workers_alive: 1, workers_expected: 1, admission_ready: 1, input_total: 10, hit_rate: .5}]};
render(data); assert.equal(body.children[0].cells[1], '4');
now = 106; timer(); assert.equal(body.children[0].cells[1], '不可用');
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_usage_displays_input_output_but_not_unknown_cache(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function display('), end = source.indexOf('\n}\n', start);
assert(start >= 0 && end > start);
const display = vm.runInNewContext('(' + source.slice(start, end + 2) + ')',
  {number: {format: String}, integer: {format: String}});
const partial = {known: 0, partial: 1, complete: false};
assert.equal(display(120, partial), '≥120');
assert.equal(display(20, partial), '≥20');
assert.equal(display(0, partial, false, false), '—');
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_motor_rates_count_only_relevant_phase_groups_and_label_partial_workers(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name, context) {
  const start = source.indexOf(`function ${name}(`), end = source.indexOf('\n}\n', start);
  assert(start >= 0 && end > start); return vm.runInNewContext('(' + source.slice(start, end + 2) + ')', context);
}
const context = {};
context.freshTelemetry = extract('freshTelemetry', context);
const values = extract('telemetryValues', context);
const base = {source_ok: true, backend_ready: true, clock_ok: true, clock_uncertainty_seconds: 0, expected_endpoints: 8};
const part = (rate, valid, partial=false) => ({tokens_per_second: rate, valid_endpoints: valid,
  expected_endpoints: 8, partial, log_at: 100, log_skew_seconds: 30, valid_until: 150});
const data = {available: true, kind: 'vllm-motor-telemetry', nodes: [
  {...base, backend_port: 1, role: 'prefill', prefill: part(20, 7, true)},
  {...base, backend_port: 2, role: 'decode', decode: part(70, 8)}]};
const result = values(data, 'all', 110);
assert.equal(result.prefill, 20); assert.equal(result.decode, 70);
assert.equal(result.coverage.prefill.total, 1); assert.equal(result.coverage.decode.total, 1);
assert.equal(result.coverage.prefill.workers_valid, 7); assert.equal(result.coverage.prefill.workers_total, 8);
assert.equal(result.coverage.prefill.partial, true); assert.equal(result.coverage.decode.partial, false);
assert.equal(result.mfu, null); assert.equal(result.hbm, null);
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

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
        self.assertIn('/app-telemetry-v5.js', html)
        self.assertIn('/app-telemetry-v5.js', ASSETS)
        self.assertNotIn('/app-telemetry-v4.js', ASSETS)
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
  clock: {format: () => 'time'}, combinedDeviceHistory: () => null, renderDeviceHistory: () => {},
  latestGroupHistory: () => null, historicalPrefillMfu: () => null};
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
const context = {$: (id) => elements[id], svgElement: () => makeElement(), setTextIfPresent: () => {}, attachTrendTooltip: () => {},
  clock: {format: () => 'time'}, dateTime: {format: () => 'date'}, speed: (v) => `${v.toFixed(2)} tok/s`, count: String};
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
  clock: {format: () => 'time'}, combinedDeviceHistory: () => null, renderDeviceHistory: () => {},
  latestGroupHistory: () => null, historicalPrefillMfu: () => null};
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

    def test_overview_adds_clearly_labeled_history_only_when_live_samples_are_missing(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name, ctx) {
  const start = source.indexOf(`function ${name}(`), end = source.indexOf('\n}\n', start);
  assert(start >= 0 && end > start, `missing ${name}`); return vm.runInNewContext('(' + source.slice(start, end + 2) + ')', ctx);
}
const elements = {};
for (const id of ['live-prefill-tps','live-decode-tps','live-mfu','live-mbu','live-hbm',
  'live-prefill-samples','live-decode-samples','live-mfu-detail','live-hbm-detail',
  'live-prefill-title','live-decode-title','live-mfu-title']) elements[id] = {textContent:''};
const context = {telemetrySnapshot:null,telemetryExpiry:null,telemetryNodePort:'all',telemetryOptionsKey:null,
  Date:class extends Date {static now() {return 86400000;}},clearTimeout:()=>{},setTimeout:()=>1,
  $:(id)=>elements[id]||null,setTextIfPresent:(id,value)=>{if(elements[id])elements[id].textContent=value;},
  number:{format:String},speed:(v)=>`${v} tok/s`,percent:(v)=>v===null||v===undefined?'—':`${v*100}%`,
  clock:{format:(v)=>`clock-${v.getTime()/1000}`},dateTime:{format:(v)=>`date-${v.getTime()/1000}`},
  renderDeviceHistory:()=>{}};
for (const name of ['freshTelemetry','telemetryValues','combinedDeviceHistory','latestHistoricalSample','latestGroupHistory','historicalPrefillMfu'])
  context[name]=extract(name,context);
const render=extract('renderTelemetry',context);
const points=Array.from({length:144},(_,i)=>({start:i*600,end:(i+1)*600,
  prefill_tps:null,prefill_samples:0,decode_tps:null,decode_samples:0}));
points[142].prefill_tps=100;points[142].prefill_samples=2;
points[143].decode_tps=40;points[143].decode_samples=1;
const a={backend_port:1,source_ok:true,backend_ready:true,clock_ok:true,clock_uncertainty_seconds:.1,
  prefill:null,decode:null,hbm:{mean_percent:0,device_count:2,sample_at:86399,span_seconds:0,valid_until:86401},
  assumptions:{prefill_flops_per_token:4,peak_flops_per_device:1000},
  history:{method:'deepinfer-log-step-approx-v1',bucket_seconds:600,points}};
const b={backend_port:2,source_ok:false,backend_ready:false,clock_ok:true,clock_uncertainty_seconds:.1};
const data={available:true,history_available:true,sampled_at:86400,nodes:[a,b]};
render(data);
assert.equal(elements['live-prefill-tps'].textContent,'100 tok/s');
assert.equal(elements['live-decode-tps'].textContent,'40 tok/s');
assert.equal(elements['live-mfu'].textContent,'20%');
assert.match(elements['live-prefill-title'].textContent,/历史/);
assert.match(elements['live-decode-title'].textContent,/历史/);
assert.match(elements['live-mfu-title'].textContent,/历史/);
assert.match(elements['live-prefill-samples'].textContent,/历史.*非实时.*1\/2/);
assert.match(elements['live-decode-samples'].textContent,/历史.*非实时.*1\/2/);
assert.match(elements['live-mfu-detail'].textContent,/当前假设.*非实时.*1\/2/);
assert.equal(elements['live-mbu'].textContent,'—');
// Old HTML loaded across a dashboard switch lacks the new title nodes.
const savedTitles={};
for(const id of ['live-prefill-title','live-decode-title','live-mfu-title']){savedTitles[id]=elements[id];delete elements[id];}
render(data);
for(const id of ['live-prefill-tps','live-decode-tps','live-mfu'])assert.equal(elements[id].textContent,'—');
Object.assign(elements,savedTitles);
const savedDetail=elements['live-decode-samples'];delete elements['live-decode-samples'];
render(data);assert.equal(elements['live-decode-tps'].textContent,'—');elements['live-decode-samples']=savedDetail;
a.prefill={tokens_per_second:0,mfu_estimate:0,peak_flops:2000,log_at:86399,log_skew_seconds:0,valid_until:86401};
a.decode={tokens_per_second:3,log_at:86399,log_skew_seconds:0,valid_until:86401};
render(data);
assert.equal(elements['live-prefill-tps'].textContent,'0 tok/s');
assert.doesNotMatch(elements['live-prefill-title'].textContent,/历史/);
assert.doesNotMatch(elements['live-decode-title'].textContent,/历史/);
assert.doesNotMatch(elements['live-mfu-title'].textContent,/历史/);
a.prefill=null;a.decode=null;data.history_available=false;render(data);
for(const id of ['live-prefill-tps','live-decode-tps','live-mfu'])assert.equal(elements[id].textContent,'—');
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, encoding='utf-8', timeout=15)
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

    def test_history_fallback_uses_same_bucket_and_weights_only_verified_groups(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const src=fs.readFileSync(process.argv[1],'utf8'),ctx={};
for(const name of ['freshTelemetry','combinedDeviceHistory','latestHistoricalSample','latestGroupHistory','historicalPrefillMfu']){
  const start=src.indexOf(`function ${name}(`),end=src.indexOf('\n}\n',start);
  assert(start>=0&&end>start);ctx[name]=vm.runInNewContext('('+src.slice(start,end+2)+')',ctx);
}
function group(port,rate,count,peak,flops) {
  const points=Array.from({length:144},(_,i)=>({start:i*600,end:(i+1)*600,
    prefill_tps:null,prefill_samples:0,decode_tps:null,decode_samples:0}));
  points[143].prefill_tps=rate; points[143].prefill_samples=3;
  return {backend_port:port,source_ok:true,clock_ok:true,clock_uncertainty_seconds:.1,
    hbm:{device_count:count,valid_until:90000},assumptions:{prefill_flops_per_token:flops,peak_flops_per_device:peak},
    history:{method:'deepinfer-log-step-approx-v1',bucket_seconds:600,points}};
}
const a=group(1,100,2,1000,4),b=group(2,300,3,2000,10),nodes=[a,b];
const data={available:true,history_available:true}, now=86400;
let combined=ctx.combinedDeviceHistory(data,nodes);
let sample=ctx.latestHistoricalSample(combined.history,'prefill',now);
assert.equal(sample.value,400);assert.equal(sample.groups,2);
let mfu=ctx.historicalPrefillMfu(nodes,'all',sample,now);
assert.equal(mfu.value,.425);assert.equal(mfu.groups,2);assert.equal(mfu.total,2);
b.hbm.valid_until=now; mfu=ctx.historicalPrefillMfu(nodes,'all',sample,now);
assert.equal(mfu.value,.2);assert.equal(mfu.groups,1);assert.equal(mfu.total,2);
b.history.points[143].prefill_samples=0;b.history.points[142].prefill_tps=300;b.history.points[142].prefill_samples=2;
combined=ctx.combinedDeviceHistory(data,nodes);sample=ctx.latestHistoricalSample(combined.history,'prefill',now);
assert.equal(sample.value,100);assert.equal(sample.groups,1); // Never add different time buckets.
a.history.points[143].prefill_tps=0;combined=ctx.combinedDeviceHistory(data,nodes);
sample=ctx.latestHistoricalSample(combined.history,'prefill',now);assert.equal(sample.value,0);
assert.equal(ctx.latestHistoricalSample(a.history,'prefill',86399),null); // Future end cannot be historical.
a.history.points[1].start=a.history.points[0].start;
assert.equal(ctx.latestHistoricalSample(a.history,'prefill',now),null);
assert.equal(ctx.latestHistoricalSample({method:'other',bucket_seconds:600,points:[]},'prefill',now),null);
// Misaligned grids: prefer the actual newest complete bucket across groups.
const c=group(3,100,2,1000,4),d=group(4,500,3,2000,10);
d.history.points.forEach((p)=>{p.start+=600;p.end+=600;});
d.history.points[142].prefill_tps=300;d.history.points[142].prefill_samples=1;
let latest=ctx.latestGroupHistory(data,[c,d],'all','prefill',87000);
assert.equal(latest.start,86400);assert.equal(latest.value,500);assert.equal(latest.groups,1);
// Restricting the available timeline to the prior bucket must include both groups.
d.history.points[143].prefill_samples=0;
latest=ctx.latestGroupHistory(data,[c,d],'all','prefill',87000);
assert.equal(latest.start,85800);assert.equal(latest.value,400);assert.equal(latest.groups,2);
const weighted=ctx.historicalPrefillMfu([c,d],'all',latest,87000);
assert.equal(weighted.value,.425);assert.equal(weighted.groups,2);
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
const context = {$: (id) => elements[id], svgElement: element, setTextIfPresent: () => {}, attachTrendTooltip: () => {},
  clock: {format: () => 'time'}, dateTime: {format: () => 'date'}, speed: (value) => `${value} tok/s`, count: String};
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
