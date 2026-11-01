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
        self.assertIn('/app-telemetry-v2.js', html)
        self.assertIn('/app-telemetry-v2.js', ASSETS)
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


if __name__ == '__main__':
    unittest.main()
