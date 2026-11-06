"""Contract tests for request token accounting and speculative metrics."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from deploy_stats.report import report
from deploy_stats.store import _enable_partial_schema, _open
from deploy_stats.motor_metrics import MotorCollector, MotorHistory, _mtp_change, _mtp_counts, parse_metrics


class RequestAccountingTests(unittest.TestCase):
    def test_session_prefill_rate_excludes_zero_wait_from_numerator(self):
        from deploy_stats.report import _request_view, _session_views
        rows = [(100, None, 'known', 100, 2, 0, 1, 's', 0, 200, 1),
                (101, None, 'known', 10, 2, 0, 2, 's', 1, 200, 2)]
        views = [_request_view(row) for row in rows]
        self.assertIsNone(views[0]['input_per_wait'])
        self.assertEqual(_session_views(views)[0]['input_per_wait'], 10)

    def test_cached_and_uncached_requests_use_only_known_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.sqlite3'
            now = time.time()
            with closing(_open(path)) as db:
                _enable_partial_schema(db)
                db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('run', now - 100, now, now, 0))
                statement = '''INSERT INTO requests(run_id,finished,client_ip,elapsed,http_status,state,
                    input_tokens,output_tokens,cached_tokens,session_id,first_visible)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)'''
                db.execute(statement, ('run', now - 3, '192.0.2.1', 1, 200, 'known', 100, 8, 60, 's', .5))
                db.execute(statement, ('run', now - 2, '192.0.2.1', 1, 200, 'known', 20, 2, 0, 's', .5))
                partial = db.execute(statement, ('run', now - 1, '192.0.2.1', 1, 200, 'missing',
                                                 None, None, None, 's', .5)).lastrowid
                db.execute('INSERT INTO stats_partial_usage VALUES (?,?,?)', (partial, 30, 5))
                db.commit()
            result = report(path, 'total', now=now + 1, request_sort='uncached_input', request_dir='desc')
            self.assertEqual([item['uncached_input'] for item in result['requests']], [40, 20, None])
            self.assertEqual([item['cached'] for item in result['requests']], [60, 0, None])
            self.assertEqual(result['totals']['uncached_input'], 60)
            session = result['sessions'][0]
            self.assertEqual((session['input'], session['output'], session['cached'], session['uncached_input']),
                             (150, 15, 60, 60))
            self.assertEqual((session['known'], session['partial']), (2, 1))

    def test_no_cache_data_does_not_become_zero_uncached(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.sqlite3'
            now = time.time()
            with closing(_open(path)) as db:
                db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('run', now - 10, now, now, 0))
                db.execute('''INSERT INTO requests(run_id,finished,client_ip,elapsed,http_status,state,
                    input_tokens,output_tokens,cached_tokens,session_id,first_visible)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''', ('run', now - 1, None, 1, 200, 'missing',
                                                       None, None, None, 's', None))
                db.commit()
            result = report(path, 'total', now=now + 1)
            self.assertIsNone(result['requests'][0]['uncached_input'])
            self.assertIsNone(result['totals']['uncached_input'])
            self.assertIsNone(result['sessions'][0]['uncached_input'])


def mtp_metrics(start, steps, drafted, accepted, *, model='synthetic', engine='0'):
    labels = f'engine="{engine}",model_name="{model}"'
    return '\n'.join((f'process_start_time_seconds {start}',
                      f'vllm:spec_decode_num_drafts_total{{{labels}}} {steps}',
                      f'vllm:spec_decode_num_draft_tokens_total{{{labels}}} {drafted}',
                      f'vllm:spec_decode_num_accepted_tokens_total{{{labels}}} {accepted}'))


class MtpTests(unittest.TestCase):
    def test_matching_labels_and_counter_reset_are_required(self):
        before = parse_metrics(mtp_metrics(10, 10, 50, 20))
        after = parse_metrics(mtp_metrics(10, 15, 75, 35))
        self.assertEqual(_mtp_counts(after, 'synthetic'), (15, 75, 35))
        self.assertEqual(_mtp_change(after, before, 'synthetic'), (5, 25, 15))
        self.assertIsNone(_mtp_counts(after, 'different-model'))
        self.assertIsNone(_mtp_change(before, after, 'synthetic'))
        mismatched = mtp_metrics(10, 15, 75, 35).replace(
            'vllm:spec_decode_num_accepted_tokens_total{engine="0"',
            'vllm:spec_decode_num_accepted_tokens_total{engine="1"')
        self.assertIsNone(_mtp_counts(parse_metrics(mismatched), 'synthetic'))
        self.assertIsNone(_mtp_counts(parse_metrics(mtp_metrics(10, 10, 20, 21)), 'synthetic'))
        self.assertIsNone(_mtp_counts(parse_metrics(mtp_metrics(10, 10, '10.5', 2)), 'synthetic'))

    def test_counts_are_weighted_and_partial_not_fabricated_as_zero(self):
        now = [102000.0]
        topology = [{'id': 'd', 'role': 'decode', 'status': 'active', 'healthy': True,
                     'model_name': 'synthetic',
                     'endpoints': [{'ip': '127.0.0.1', 'business_port': 12001},
                                   {'ip': '127.0.0.1', 'business_port': 12002}]}]
        data = {12001: mtp_metrics(10, 10, 100, 90), 12002: mtp_metrics(10, 10, 900, 90)}
        with tempfile.TemporaryDirectory() as tmp:
            history = MotorHistory(Path(tmp) / 'history.sqlite3')
            collector = MotorCollector([13001], fetch_topology=lambda _: json.dumps({'instances': topology}),
                                       fetch_endpoint=lambda _ip, port: data[port], history=history,
                                       wall_time=lambda: now[0], monotonic=lambda: now[0])
            first = collector.collect()['nodes'][0]
            self.assertAlmostEqual(first['mtp']['lifetime']['acceptance_rate'], .18)
            self.assertIsNone(first['mtp']['interval'])
            now[0] += 30
            data[12001] = mtp_metrics(10, 11, 110, 99)
            data[12002] = mtp_metrics(10, 11, 990, 99)
            second = collector.collect()['nodes'][0]
            interval = second['mtp']['interval']
            self.assertEqual((interval['drafted'], interval['accepted']), (100, 18))
            self.assertAlmostEqual(interval['acceptance_rate'], .18)
            self.assertFalse(interval['partial'])
            now[0] += 30
            data[12001] = mtp_metrics(11, 1, 5, 1)  # restarted; no delta for this worker
            data[12002] = mtp_metrics(10, 12, 1080, 108)
            third = collector.collect()['nodes'][0]
            self.assertTrue(third['mtp']['interval']['partial'])
            self.assertEqual(third['mtp']['interval']['valid_endpoints'], 1)
            self.assertEqual(third['mtp']['interval']['drafted'], 90)
            now[0] += 600
            point = next(p for p in history.mtp_series(
                hashlib.sha256(json.dumps(
                    ['mtp-v1', 'd', 'decode', (('127.0.0.1', 12001), ('127.0.0.1', 12002)), 'synthetic'],
                    separators=(',', ':')).encode()).hexdigest(), now[0])['points']
                         if p['drafted'] is not None)
            self.assertEqual((point['drafted'], point['accepted']), (190, 27))
            self.assertTrue(point['partial'])

    def test_zero_drafted_is_unknown_rate(self):
        from deploy_stats.motor_metrics import _mtp_summary
        data = _mtp_summary([(0, 0, 0)], 1)
        self.assertIsNone(data['acceptance_rate'])
        self.assertIsNone(data['mean_acceptance_length'])

    def test_invalid_optional_mtp_does_not_discard_other_metrics(self):
        baseline = 'process_start_time_seconds 10\nvllm:generation_tokens_total{model_name="synthetic"} 123\n'
        for bad in ('NaN', '1.5'):
            with self.subTest(bad=bad):
                values = parse_metrics(baseline + mtp_metrics(10, 3, bad, 1).split('\n', 1)[1])
                self.assertEqual(next(iter(values['vllm:generation_tokens_total'].values())), 123)
                self.assertIsNone(_mtp_counts(values, 'synthetic'))
        duplicate = mtp_metrics(10, 3, 10, 1).split('\n', 1)[1]
        values = parse_metrics(baseline + duplicate + '\n' + duplicate.splitlines()[0])
        self.assertIn('vllm:generation_tokens_total', values)
        self.assertIsNone(_mtp_counts(values, 'synthetic'))

    def test_sparse_history_does_not_claim_full_time_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = MotorHistory(Path(tmp) / 'history.sqlite3')
            history.record_mtp('g', 90030, 1, 10, 5)
            point = next(p for p in history.mtp_series('g', 91200)['points'] if p['start'] == 90000)
            self.assertTrue(point['partial'])
            self.assertEqual(point['time_coverage'], 'unverified')

    def test_model_change_cannot_reuse_rates_or_record_mismatched_metrics(self):
        now = [102000.0]
        topology = [{'id': 'd', 'role': 'decode', 'status': 'active', 'healthy': True,
                     'model_name': 'a', 'endpoints': [{'ip': '127.0.0.1', 'business_port': 12001}]}]
        data = [None]
        def metrics_for(model, amount):
            return (f'process_start_time_seconds 10\n'
                    f'vllm:generation_tokens_total{{model_name="{model}"}} {amount}\n')
        with tempfile.TemporaryDirectory() as tmp:
            collector = MotorCollector([13001], fetch_topology=lambda _: json.dumps({'instances': topology}),
                fetch_endpoint=lambda *_: data[0], history=MotorHistory(Path(tmp) / 'history.sqlite3'),
                wall_time=lambda: now[0], monotonic=lambda: now[0])
            data[0] = metrics_for('a', 100)
            first = collector.collect()['nodes'][0]
            now[0] += 30
            data[0] = metrics_for('a', 130)
            self.assertEqual(collector.collect()['nodes'][0]['decode']['tokens_per_second'], 1)
            now[0] += 600
            topology[0]['model_name'] = 'b'
            data[0] = metrics_for('b', 200)
            second = collector.collect()['nodes'][0]
            self.assertNotEqual(first['backend_port'], second['backend_port'])
            self.assertIsNone(second['decode'])
            self.assertFalse(any(point['decode_samples'] for point in second['history']['points']))
            now[0] += 30
            data[0] = metrics_for('c', 300)
            collector.collect()
            now[0] += 30
            data[0] = metrics_for('c', 330)
            self.assertIsNone(collector.collect()['nodes'][0]['decode'])

    def test_older_history_file_is_upgraded_without_losing_rate_buckets(self):
        import sqlite3
        from deploy_stats.motor_metrics import APPLICATION_ID, SCHEMA_VERSION
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'history.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(f'PRAGMA application_id = {APPLICATION_ID}')
                db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                db.execute('''CREATE TABLE rate_buckets (group_key TEXT NOT NULL, phase TEXT NOT NULL,
                    bucket INTEGER NOT NULL, rate_sum REAL NOT NULL, samples INTEGER NOT NULL,
                    partial_samples INTEGER NOT NULL, PRIMARY KEY (group_key, phase, bucket)) WITHOUT ROWID''')
                db.execute('INSERT INTO rate_buckets VALUES (?,?,?,?,?,?)', ('g', 'decode', 90000, 60, 2, 0))
            history = MotorHistory(path)
            self.assertEqual(next(p for p in history.series('g', 91200)['points']
                                  if p['start'] == 90000)['decode_tps'], 30)
            history.record_mtp('g', 90001, 10, 100, 25)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(db.execute('SELECT rate_sum, samples FROM rate_buckets').fetchone(), (60, 2))
            self.assertAlmostEqual(next(p for p in history.mtp_series('g', 91200)['points']
                                        if p['start'] == 90000)['acceptance_rate'], .25)


class MtpDashboardTests(unittest.TestCase):
    def run_javascript(self, script):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard calculations')
        source = Path(__file__).resolve().parents[1] / 'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_summary_weights_drafts_and_preserves_missing_and_stale_data(self):
        self.run_javascript(r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8'), context = {};
function extract(name) {
  const start = source.indexOf(`function ${name}(`), end = source.indexOf('\n}\n', start);
  assert(start >= 0 && end > start); return vm.runInNewContext('(' + source.slice(start, end + 2) + ')', context);
}
context.freshTelemetry = extract('freshTelemetry'); context.validMtp = extract('validMtp');
const summary = extract('mtpSummary');
const data = {available: true, kind: 'vllm-motor-telemetry'};
const part = (drafted, accepted) => ({sampled_at: 90, valid_until: 110, steps: 10, drafted, accepted,
  valid_endpoints: 1, expected_endpoints: 1, partial: false});
const group = (backend_port, sample) => ({backend_port, role: 'decode', source_ok: true, backend_ready: true,
  expected_endpoints: 1, mtp: {lifetime: sample}});
const a = group(1, part(100, 90)), b = group(2, part(900, 90));
let value = summary(data, [a, b], 'all', 100, 'lifetime');
assert.equal(value.rate, .18); assert.equal(value.partial, false);
value = summary(data, [a, b], 1, 100, 'lifetime'); assert.equal(value.rate, .9);
b.source_ok = false; value = summary(data, [a, b], 'all', 100, 'lifetime');
assert.equal(value.rate, .9); assert.equal(value.partial, true); assert.equal(value.groups, 1);
b.source_ok = true; b.mtp.lifetime.valid_until = 99;
assert.equal(summary(data, [b], 'all', 100, 'lifetime'), null);
assert.equal(summary(data, [group(3, part(0, 0))], 'all', 100, 'lifetime').rate, null);
assert.equal(summary(data, [group(3, part(1, 2))], 'all', 100, 'lifetime'), null);
assert.equal(summary(data, [a, {...a}], 'all', 100, 'lifetime'), null);
''')

    def test_history_weights_counts_marks_partial_and_keeps_gaps(self):
        self.run_javascript(r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function renderMtpHistory('), end = source.indexOf('\n}\n', start);
assert(start >= 0 && end > start);
const element = () => ({children: [], textContent: '', replaceChildren() {this.children = [];},
  appendChild(value) {this.children.push(value);}});
const svg = element(), label = element(); let descriptions;
const context = {$: (id) => id === 'chart-mtp-svg' ? svg : label,
  svgElement: (name, attrs) => Object.assign(element(), {name, attrs}),
  attachTrendTooltip: (_svg, values) => {descriptions = values;},
  dateTime: {format: () => 'time'}, percent: (v) => `${v * 100}%`, count: String};
const render = vm.runInNewContext('(' + source.slice(start, end + 2) + ')', context);
const points = () => Array.from({length: 144}, (_, i) => ({start: i * 600, end: (i + 1) * 600,
  drafted: null, accepted: null, samples: 0, partial: false}));
const first = points(), second = points();
Object.assign(first[1], {drafted: 100, accepted: 90, samples: 1});
Object.assign(second[1], {drafted: 900, accepted: 90, samples: 1});
Object.assign(first[3], {drafted: 10, accepted: 5, samples: 1});
const group = (backend_port, values) => ({backend_port, role: 'decode', mtp_history:
  {method: 'vllm-mtp-counter-delta-v1', bucket_seconds: 600, points: values}});
const a = group(1, first), b = group(2, second), data = {available: true, history_available: true,
  kind: 'vllm-motor-telemetry'};
render(data, [a, b], 'all');
assert.match(descriptions[1], /18%/); assert.match(descriptions[1], /2\/2/);
assert.equal(descriptions[2], null); assert.match(descriptions[3], /50%/);
assert.match(descriptions[3], /1\/2.*部分数据/);
assert.match(descriptions[1], /时间覆盖未核验/);
assert.match(label.textContent, /2 个部分区间/);
assert.equal(svg.children.filter((v) => v.name === 'circle').length, 2);
render(data, [a, {...a}], 'all'); assert.equal(label.textContent, '等待 MTP 历史采样');
''')

    def test_recent_coverage_is_not_borrowed_from_lifetime(self):
        self.run_javascript(r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8'), texts = {};
const context = {setTextIfPresent: (id, value) => {texts[id] = value;}, percent: (v) => `${v * 100}%`,
  renderMtpHistory: () => {}};
function extract(name) {
  const start = source.indexOf(`function ${name}(`), end = source.indexOf('\n}\n', start);
  assert(start >= 0 && end > start); return vm.runInNewContext('(' + source.slice(start, end + 2) + ')', context);
}
for (const name of ['freshTelemetry', 'validMtp', 'mtpSummary']) context[name] = extract(name);
const render = extract('renderMtp');
const part = (valid, drafted, accepted) => ({sampled_at: 90, valid_until: 110, steps: 10, drafted, accepted,
  valid_endpoints: valid, expected_endpoints: 8, partial: valid < 8});
const node = {backend_port: 1, role: 'decode', source_ok: true, backend_ready: true, expected_endpoints: 8,
  mtp: {lifetime: part(8, 100, 90), interval: part(1, 100, 20)}};
render({available: true, kind: 'vllm-motor-telemetry'}, [node], 'all', 100);
assert.match(texts['mtp-detail'], /8\/8 端点/);
assert.match(texts['mtp-detail'], /最近采样 20%（1\/1 组，1\/8 端点，部分数据）/);
''')

    def test_partial_mtp_markers_remain_hollow(self):
        css = (Path(__file__).resolve().parents[1] / 'deploy_stats/static/app.css').read_text(encoding='utf-8')
        self.assertIn('.lane svg circle.mtp.partial{fill:#fff}', css)


if __name__ == '__main__':
    unittest.main()
