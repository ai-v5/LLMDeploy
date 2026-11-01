import shutil
import subprocess
import unittest
from pathlib import Path


class DashboardEfficiencyTests(unittest.TestCase):
    def test_request_level_estimates_and_unknown_inputs(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function estimateEfficiency(');
const end = source.indexOf('\n}\n', start);
assert(start >= 0 && end > start);
const estimate = vm.runInNewContext('(' + source.slice(start, end + 2) + ')');
const profileStart = source.indexOf('function efficiencyProfile(');
const profileEnd = source.indexOf('\n}\n', profileStart);
assert(profileStart >= 0 && profileEnd > profileStart);
let data = {prefillFlops: '2', peakFlops: '1000', decodeBytesLow: '3',
  decodeBytesHigh: '6', bandwidth: '2000', description: 'synthetic profile'};
const readProfile = vm.runInNewContext('(' + source.slice(profileStart, profileEnd + 2) + ')',
  { $: () => ({dataset: data}) });
assert.equal(readProfile().prefillFlops, 2);
data = {...data, decodeBytesLow: '9'};
assert.equal(readProfile(), null);
data = {...data, decodeBytesLow: '3', bandwidth: 'Infinity'};
assert.equal(readProfile(), null);
data = {...data, bandwidth: '2000', description: ''};
assert.equal(readProfile(), null);
const profile = {prefillFlops: 2, peakFlops: 1000, decodeBytesLow: 3,
  decodeBytesHigh: 6, bandwidth: 2000};
const sample = {prefill_estimate_tps: 100, decode_estimate_tps: 40,
  prefill_samples: 2, decode_samples: 3};
let result = estimate(sample, profile);
assert.equal(result.mfu, .2);
assert.equal(result.mbuLow, .06);
assert.equal(result.mbuHigh, .12);
result = estimate({...sample, prefill_samples: 0, decode_estimate_tps: null}, profile);
assert.equal(result.mfu, null);
assert.equal(result.mbuLow, null);
assert.equal(result.mbuHigh, null);
result = estimate({...sample, prefill_estimate_tps: 1e308}, profile);
assert.equal(result.mfu, null);
result = estimate({...sample, prefill_estimate_tps: 1}, {...profile, prefillFlops: 1, peakFlops: 1e-308});
assert.equal(result.mfu, null); // Ratio is finite, but conversion to percent would overflow.
result = estimate(sample, {...profile, bandwidth: 0});
assert.equal(result.mbuLow, null);
assert.equal(result.mbuHigh, null);
result = estimate({...sample, prefill_estimate_tps: 1000}, profile);
assert.equal(result.mfu, 2);  // Do not hide an implausible value by clamping.
assert.equal(estimate(sample, null).mfu, null);
'''
        source = Path(__file__).resolve().parents[1]/'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
