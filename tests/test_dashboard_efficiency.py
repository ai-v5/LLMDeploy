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
        self.assertIn('/app-telemetry-v3.js', html)
        self.assertIn('/app-telemetry-v3.js', ASSETS)
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
let data = {available: true, nodes: [{backend_port: 1, source_ok: true,
  prefill: {tokens_per_second: 0, mfu_estimate: .25, valid_until: 101},
  decode: {tokens_per_second: 100, valid_until: 101}, hbm: {mean_percent: 35, valid_until: 101}}]};
let value = read(data, 1, 100);
assert.equal(value.prefill, 0);
assert.equal(value.decode, 100);
assert.equal(value.mfu, .25);
assert.equal(value.hbm, 35);
value = read({available: true, nodes: [{backend_port: 1, source_ok: true,
  prefill: {tokens_per_second: null, mfu_estimate: -1, valid_until: 101},
  decode: {tokens_per_second: Infinity, valid_until: 101}, hbm: {mean_percent: 101, valid_until: 101}}]}, 1, 100);
for (const key of ['prefill', 'decode', 'mfu', 'hbm']) assert.equal(value[key], null);
assert.equal(read(data, 1, 101).prefill, null);
assert.equal(read(data, 2, 100).prefill, null);
assert.equal(read({available: false, nodes: data.nodes}, 1, 100).prefill, null);
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

    def test_cross_group_values_require_complete_aligned_samples_and_weight_percentages(self):
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
assert.equal(read(data, 1, 100).prefill, 100);
b.prefill.tokens_per_second = 0; assert.equal(read(data, 'all', 100).prefill, 100);
b.prefill.log_at = 60; assert.equal(read(data, 'all', 100).prefill, null);
b.prefill.log_at = 95; b.decode = null; assert.equal(read(data, 'all', 100).decode, null);
b.source_ok = false; assert.equal(read(data, 'all', 100).prefill, null);
b.source_ok = true; b.backend_port = 1; assert.equal(read(data, 'all', 100).prefill, null);
assert.equal(read({available: false, nodes: [a]}, 'all', 100).prefill, null);
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cross_group_history_requires_each_group_and_matching_buckets(self):
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
result = combine(data, [a, b]); assert.equal(result.history.points[2].prefill_tps, null);
b.history.points[3].start += 600; assert.equal(combine(data, [a, b]), null);
b.history.points[3].start -= 600; b.source_ok = false; assert.equal(combine(data, [a, b]), null);
assert.equal(combine({available: true, history_available: false}, [a]), null);
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
