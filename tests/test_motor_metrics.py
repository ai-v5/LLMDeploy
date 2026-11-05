import json
from contextlib import closing
from http.client import HTTPConnection
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy_stats.motor_metrics import MotorCollector, MotorHistory, parse_metrics
from deploy_stats.capture import SafeCapture, extract_usage
from deploy_stats.report import active_report, report
from deploy_stats.store import Recorder
from deploy_stats.web import Dashboard


def instance(name, role, ports):
    return {'id': name, 'role': role, 'status': 'active', 'healthy': True,
            'endpoints': [{'id': str(port), 'ip': '127.0.0.1', 'business_port': str(port)}
                          for port in ports]}


def metrics(start, prefill, decode, count=0, seconds=0):
    labels = 'engine="0",model_name="synthetic"'
    return '\n'.join([
        f'process_start_time_seconds {start}',
        f'vllm:prompt_tokens_by_source_total{{{labels},source="local_compute"}} {prefill}',
        f'vllm:generation_tokens_total{{{labels}}} {decode}',
        f'vllm:request_prefill_time_seconds_sum{{{labels}}} {seconds}',
        f'vllm:request_prefill_time_seconds_count{{{labels}}} {count}',
        f'vllm:request_decode_time_seconds_sum{{{labels}}} {seconds}',
        f'vllm:request_decode_time_seconds_count{{{labels}}} {count}',
    ])


class SequentialExecutor:
    def __init__(self, **_kwargs):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False
    def map(self, function, values):
        return map(function, values)


class MotorMetricsTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.topology = [instance('p', 'prefill', [12001, 12002]),
                         instance('d', 'decode', [12003])]
        self.data = {port: metrics(50, 100, 20, 2, 4) for port in (12001, 12002, 12003)}
        self.failed = set()

        def topology(port):
            return json.dumps({'instances': self.topology})

        def endpoint(ip, port):
            if port in self.failed:
                raise OSError('fixture failure')
            return self.data[port]

        self.collector = MotorCollector([13001, 13002], fetch_topology=topology,
                                        fetch_endpoint=endpoint, wall_time=lambda: self.now,
                                        monotonic=lambda: self.now, interval=30)

    def sample(self, role):
        value = self.collector.collect()
        return next(node for node in value['nodes'] if node['role'] == role), value

    def test_duplicate_coordinators_deduplicated_and_concurrent_rate_uses_counter_delta(self):
        first, result = self.sample('prefill')
        self.assertIsNone(first['prefill'])
        self.assertEqual(len(result['nodes']), 2)
        self.now += 30
        self.data[12001] = metrics(50, 400, 999, 5, 10)
        self.data[12002] = metrics(50, 700, 999, 5, 13)
        self.data[12003] = metrics(50, 9000, 170, 5, 10)
        result = self.collector.collect()
        prefill = next(node for node in result['nodes'] if node['role'] == 'prefill')
        decode = next(node for node in result['nodes'] if node['role'] == 'decode')
        self.assertEqual(prefill['prefill']['tokens_per_second'], 30)
        self.assertEqual(decode['decode']['tokens_per_second'], 5)
        self.assertEqual(prefill['prefill']['valid_endpoints'], 2)
        self.assertEqual(prefill['prefill']['expected_endpoints'], 2)
        self.assertEqual(prefill['phase_timers']['prefill']['mean_ms'], 2500)
        self.assertIsNone(prefill['decode'])
        self.assertIsNone(decode['prefill'])
        self.assertIsNone(result['prefill_mfu'])
        self.assertIsNone(result['hbm_percent'])

    def test_missing_worker_is_partial_and_a_gap_does_not_become_zero(self):
        self.collector.collect()
        self.now += 30
        self.failed.add(12002)
        self.data[12001] = metrics(50, 400, 20)
        node, _ = self.sample('prefill')
        self.assertEqual(node['prefill']['tokens_per_second'], 10)
        self.assertEqual((node['prefill']['valid_endpoints'], node['prefill']['expected_endpoints']), (1, 2))
        self.assertTrue(node['prefill']['partial'])
        self.now += 30
        self.failed.remove(12002)
        self.data[12001] = metrics(50, 700, 20)
        self.data[12002] = metrics(50, 900, 20)
        node, _ = self.sample('prefill')
        self.assertEqual(node['prefill']['tokens_per_second'], 10)
        self.assertEqual(node['prefill']['valid_endpoints'], 1)

    def test_restart_and_counter_reset_do_not_produce_rates(self):
        self.collector.collect()
        self.now += 30
        self.data[12001] = metrics(51, 110, 25)
        self.data[12002] = metrics(50, 2, 1)
        node, _ = self.sample('prefill')
        self.assertIsNone(node['prefill'])

    def test_motor_numeric_instance_ids_are_accepted(self):
        self.topology[0]['id'] = 123
        self.collector.collect()
        self.now += 30
        self.data[12001] = metrics(50, 400, 20)
        self.data[12002] = metrics(50, 700, 20)
        node, _ = self.sample('prefill')
        self.assertEqual(node['prefill']['tokens_per_second'], 30)

    def test_disagreeing_coordinators_do_not_admit_unconfirmed_groups(self):
        self.collector.fetch_topology = lambda port: json.dumps({
            'instances': self.topology if port == 13001 else self.topology[1:]})
        result = self.collector.collect()
        self.assertTrue(result['topology_ambiguous'])
        self.assertEqual([node['role'] for node in result['nodes']], ['decode'])

    def test_role_change_during_worker_scrape_does_not_create_prefill_rate(self):
        self.collector.collect()
        self.now += 30
        self.data[12001] = metrics(50, 400, 20)
        self.data[12002] = metrics(50, 700, 20)
        calls = {13001: 0, 13002: 0}
        changed = [instance('p', 'decode', [12001, 12002]), self.topology[1]]
        def topology(port):
            calls[port] += 1
            return json.dumps({'instances': self.topology if calls[port] == 1 else changed})
        self.collector.fetch_topology = topology
        result = self.collector.collect()
        self.assertTrue(result['topology_ambiguous'])
        self.assertFalse(any(node['prefill'] is not None for node in result['nodes']))

    def test_oldest_worker_sample_controls_group_expiry(self):
        with patch('deploy_stats.motor_metrics.ThreadPoolExecutor', SequentialExecutor):
            self.collector.collect()
            self.now += 30
            self.data[12001] = metrics(50, 400, 20)
            self.data[12002] = metrics(50, 700, 20)
            def slow_second(_ip, port):
                if port == 12002:
                    self.now += 20
                return self.data[port]
            self.collector.fetch_endpoint = slow_second
            node, _ = self.sample('prefill')
            self.assertIsNotNone(node['prefill'])
            self.assertLessEqual(node['prefill']['valid_until'], 1095)

    def test_very_old_worker_sample_cannot_be_relabelled_as_fresh(self):
        with patch('deploy_stats.motor_metrics.ThreadPoolExecutor', SequentialExecutor):
            self.collector.collect()
            self.now += 30
            self.data[12001] = metrics(50, 400, 20)
            self.data[12002] = metrics(50, 700, 20)
            def stalled_second(_ip, port):
                if port == 12002:
                    self.now += 70
                return self.data[port]
            self.collector.fetch_endpoint = stalled_second
            node, _ = self.sample('prefill')
            self.assertIsNone(node['prefill'])

    def test_backend_gauges_expire_at_the_oldest_worker_deadline(self):
        self.collector.collect()
        initial = self.collector.snapshot()
        self.assertTrue(all(node['engine_metrics'] is not None for node in initial['nodes']))
        self.now += 66
        expired = self.collector.snapshot()
        self.assertTrue(expired['nodes'])  # The overall collector snapshot has not expired yet.
        self.assertTrue(all(node['engine_metrics'] is None for node in expired['nodes']))

    def test_slow_worker_response_uses_earliest_possible_sample_time(self):
        self.topology = [instance('p', 'prefill', [12001])]
        with patch('deploy_stats.motor_metrics.ThreadPoolExecutor', SequentialExecutor):
            self.collector.collect()
            self.now += 30
            self.data[12001] = metrics(50, 400, 20)
            def slow_worker(_ip, port):
                self.now += 20
                return self.data[port]
            self.collector.fetch_endpoint = slow_worker
            node, _ = self.sample('prefill')
            self.assertEqual(node['prefill']['valid_until'], 1095)
            self.assertEqual(node['engine_metrics']['sampled_at'], 1030)
            self.assertEqual(node['engine_metrics']['valid_until'], 1095)

    def test_duplicate_worker_address_is_not_counted_twice(self):
        self.topology.append(instance('duplicate', 'prefill', [12001]))
        self.collector.collect()
        self.now += 30
        self.data[12001] = metrics(50, 400, 20)
        self.data[12002] = metrics(50, 700, 20)
        node, result = self.sample('prefill')
        usable = [n for n in result['nodes'] if n['role'] == 'prefill' and n['prefill']]
        self.assertEqual(sum(n['prefill']['tokens_per_second'] for n in usable), 20)
        self.assertTrue(result['topology_ambiguous'])

    def test_counter_series_must_remain_identical(self):
        self.collector.collect()
        self.now += 30
        self.data[12001] = metrics(50, 400, 20).replace('engine="0"', 'engine="1"')
        self.data[12002] = metrics(50, 700, 20)
        node, _ = self.sample('prefill')
        self.assertEqual(node['prefill']['tokens_per_second'], 20)
        self.assertEqual(node['prefill']['valid_endpoints'], 1)

    def test_reassigned_instance_cannot_reuse_previous_topology_history(self):
        first, _ = self.sample('prefill')
        self.now += 30
        self.topology[0] = instance('p', 'prefill', [12001, 12003])
        self.topology[1] = instance('d', 'decode', [12002])
        second, _ = self.sample('prefill')
        self.assertNotEqual(first['backend_port'], second['backend_port'])
        self.assertIsNone(second['prefill'])

    def test_parser_rejects_duplicate_or_nonfinite_counter(self):
        raw = metrics(50, 100, 20)
        with self.assertRaises(ValueError):
            parse_metrics(raw + '\n' + raw.splitlines()[1])
        with self.assertRaises(ValueError):
            parse_metrics(raw.replace(' 100', ' NaN'))

    def test_whitespace_and_one_endpoint_failure_do_not_break_other_workers(self):
        self.assertIn('process_start_time_seconds', parse_metrics('process_start_time_seconds 1\n   \n'))
        self.collector.collect()
        self.now += 30
        self.data[12001] = metrics(50, 400, 20)
        def broken_endpoint(_ip, port):
            if port == 12002:
                raise RuntimeError('fixture failure')
            return self.data[port]
        self.collector.fetch_endpoint = broken_endpoint
        node, _ = self.sample('prefill')
        self.assertEqual(node['prefill']['tokens_per_second'], 10)
        self.assertTrue(node['prefill']['partial'])

    def test_history_persists_samples_without_filling_unknown_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = MotorHistory(Path(tmp) / 'history.sqlite3')
            history.record('group', 'prefill', 100801, 10)
            history.record('group', 'prefill', 100820, 20, partial=True)
            points = history.series('group', 102000)['points']
            point = next(p for p in points if p['start'] == 100800)
            self.assertEqual(point['prefill_tps'], 15)
            self.assertEqual(point['prefill_samples'], 2)
            self.assertTrue(point['prefill_partial'])
            self.assertIsNone(point['decode_tps'])
            self.assertIsNone(next(p for p in points if p['start'] == 101400)['prefill_tps'])

    def test_legacy_heartbeat_does_not_claim_motor_capture_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / 'usage.sqlite3'
            legacy = Recorder(database)
            try:
                self.assertTrue(active_report(database)['collector_live'])
                self.assertFalse(active_report(database, source='gateway-inprocess-v1')['collector_live'])
                current = Recorder(database, source='gateway-inprocess-v1')
                try:
                    self.assertTrue(active_report(database, source='gateway-inprocess-v1')['collector_live'])
                    with closing(sqlite3.connect(database)) as connection, connection:
                        connection.execute('UPDATE runs SET started=? WHERE id=?', (time.time() - 172800, current.run))
                    # Even a full-window heartbeat cannot prove Caddy actually routed traffic through this run.
                    self.assertFalse(report(database, 'today', source='gateway-inprocess-v1')['totals']['complete'])
                finally:
                    current.close()
                self.assertFalse(active_report(database, source='gateway-inprocess-v1')['collector_live'])
            finally:
                legacy.close()

    def test_dashboard_serves_motor_snapshot_without_trusting_legacy_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / 'usage.sqlite3'
            legacy = Recorder(database)
            try:
                node, snapshot = self.sample('prefill')
                with patch('deploy_stats.web.MotorCollector') as collector:
                    collector.return_value.snapshot.return_value = snapshot
                    dashboard = Dashboard(0, database, motor_management_ports=[13001, 13002])
                    worker = threading.Thread(target=dashboard.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
                    worker.start()
                    try:
                        conn = HTTPConnection('127.0.0.1', dashboard.server_port, timeout=3)
                        conn.request('GET', '/api/v5/telemetry')
                        response = conn.getresponse()
                        value = json.loads(response.read())
                        conn.close()
                        self.assertEqual(response.status, 200)
                        self.assertTrue(value['available'])
                        self.assertEqual(value['kind'], 'vllm-motor-telemetry')
                        conn = HTTPConnection('127.0.0.1', dashboard.server_port, timeout=3)
                        conn.request('GET', '/api/v5/stats')
                        response = conn.getresponse()
                        value = json.loads(response.read())
                        conn.close()
                        self.assertEqual(response.status, 200)
                        self.assertFalse(value['collector_live'])
                        self.assertEqual(len(value['backends']), 2)
                        for path in ('/api/v5/active', '/app-telemetry-v5.js'):
                            conn = HTTPConnection('127.0.0.1', dashboard.server_port, timeout=3)
                            conn.request('GET', path)
                            response = conn.getresponse()
                            response.read()
                            conn.close()
                            self.assertEqual(response.status, 200, path)
                        for path in ('/api/stats', '/api/active', '/api/telemetry'):
                            conn = HTTPConnection('127.0.0.1', dashboard.server_port, timeout=3)
                            conn.request('GET', path)
                            response = conn.getresponse()
                            response.read()
                            conn.close()
                            self.assertEqual(response.status, 409, path)
                        conn = HTTPConnection('127.0.0.1', dashboard.server_port, timeout=3)
                        conn.request('GET', '/app-telemetry-v4.js')
                        response = conn.getresponse()
                        response.read()
                        conn.close()
                        self.assertEqual(response.status, 404)
                    finally:
                        dashboard.shutdown()
                        dashboard.server_close()
                        worker.join(3)
                    collector.return_value.close.assert_called_once()
            finally:
                legacy.close()

    def test_partial_vllm_usage_preserves_input_and_output_without_inventing_cache(self):
        usage = {'usage': {'prompt_tokens': 120, 'completion_tokens': 20, 'total_tokens': 140}}
        self.assertIsNone(extract_usage('chat', usage))
        self.assertEqual(extract_usage('chat', usage, allow_partial=True), (120, 20, None))
        reader = SafeCapture('chat', 'text/event-stream', allow_partial=True)
        reader.feed(b'data: {"choices":[{"delta":{"content":"fixture"}}]}\n\n')
        reader.feed(b'data: {"usage":{"prompt_tokens":120,"completion_tokens":20,"total_tokens":140}}\n\n')
        reader.feed(b'data: [DONE]\n\n')
        self.assertEqual(reader.finish(), (120, 20, None))
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / 'usage.sqlite3'
            recorder = Recorder(database, source='gateway-inprocess-v1')
            try:
                now = time.time()
                recorder.submit((now, '127.0.0.1', 2, 200, 'partial', 120, 20, None, 'a'*64, .5))
                recorder.submit((now, '127.0.0.1', 2, 200, 'known', 100, 7, 60, 'a'*64, .5))
                self.assertTrue(recorder.flush())
                result = report(database, 'total', now=now + 1, source='gateway-inprocess-v1')
                self.assertEqual((result['totals']['known'], result['totals']['partial']), (1, 1))
                self.assertEqual((result['totals']['input'], result['totals']['output'], result['totals']['cached']), (220, 27, 60))
                self.assertEqual(result['totals']['hit_rate'], .6)
                partial = next(row for row in result['requests'] if row['state'] == 'partial')
                self.assertEqual((partial['input'], partial['output'], partial['cached'], partial['hit_rate']), (120, 20, None, None))
                self.assertIsNone(partial['input_per_wait'])
                self.assertIsNotNone(partial['output_per_after_first'])
                self.assertEqual(result['sessions'][0]['input'], 220)
                self.assertEqual(result['sessions'][0]['hit_rate'], .6)
                with closing(sqlite3.connect(database)) as connection:
                    legacy = connection.execute('SELECT state,input_tokens,output_tokens,cached_tokens FROM requests ORDER BY id LIMIT 1').fetchone()
                self.assertEqual(legacy, ('missing', None, None, None))
            finally:
                recorder.close()

    def test_partial_does_not_accept_invalid_or_ambiguous_usage(self):
        base = {'prompt_tokens': 120, 'completion_tokens': 20, 'total_tokens': 140}
        for invalid in ({**base, 'total_tokens': 999}, {**base, 'prompt_tokens_details': []},
                        {**base, 'prompt_tokens_details': {'cached_tokens': -1}},
                        {**base, 'prompt_cache_miss_tokens': 80}):
            self.assertIsNone(extract_usage('chat', {'usage': invalid}, allow_partial=True))
        self.assertIsNone(extract_usage('messages', {'usage': {'input_tokens': 120, 'output_tokens': 20}}, allow_partial=True))


if __name__ == '__main__':
    unittest.main()
