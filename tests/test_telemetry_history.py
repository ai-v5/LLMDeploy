import json
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from deploy_stats import telemetry, web
from deploy_stats.telemetry_history import BUCKET_SECONDS, History, SOURCE
from tests.test_telemetry import node as engine_config, sample as engine_sample


def config(port=19001, target='operator@example.invalid'):
    return {'backend_port': port, 'ssh_target': target, 'container': 'example-container',
            'rank_devices': [0, 1], 'source_sha256': {'engine': 'a' * 64},
            'image_fingerprint': 'b' * 64, 'driver_sha256': 'c' * 64}


def node(at, port=19001, host='d' * 64, source='e' * 64, step=1):
    return {'source_ok': True, 'backend_ready': True, 'clock_ok': True, 'backend_port': port,
            'host_fingerprint': host, 'source_id': source, 'clock_uncertainty_seconds': .1,
            'prefill': {'log_at': at, 'log_skew_seconds': .2, 'step': step,
                        'tokens_per_second': 100, 'mfu_estimate': .5, 'valid_until': at + 40,
                        '_observation_id': f'{step:064x}'},
            'decode': {'log_at': at, 'log_skew_seconds': .2, 'step': step + 1,
                       'tokens_per_second': 0, 'valid_until': at + 40, '_observation_id': f'{step+1:064x}'}}


class TelemetryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd() / 'local')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'history.sqlite3'
        self.history = History(self.path)
        self.at = 100000 * BUCKET_SECONDS + 100

    def test_samples_are_per_group_deduplicated_and_keep_true_zero(self):
        item = node(self.at)
        self.assertEqual(self.history.record(item, config(), self.at + 1), 2)
        item['prefill']['log_at'] += .05  # changed clock estimate, same observed step
        self.assertEqual(self.history.record(item, config(), self.at + 1), 0)
        result = self.history.series(config(), item['host_fingerprint'], self.at + BUCKET_SECONDS)
        self.assertEqual(result['method'], SOURCE)
        self.assertEqual(len(result['points']), 144)
        point = result['points'][-1]
        self.assertEqual(point['prefill_tps'], 100)
        self.assertEqual(point['prefill_samples'], 1)
        self.assertEqual(point['decode_tps'], 0)
        self.assertEqual(point['decode_samples'], 1)
        self.assertIsNone(result['points'][-2]['prefill_tps'])

    def test_bucket_boundary_expiry_and_unknown_source_are_not_recorded(self):
        boundary = 100000 * BUCKET_SECONDS + BUCKET_SECONDS - .05
        self.assertEqual(self.history.record(node(boundary), config(), boundary + 1), 0)
        item = node(self.at)
        item['prefill']['valid_until'] = self.at
        item['decode'] = None
        self.assertEqual(self.history.record(item, config(), self.at + 1), 0)
        item = node(self.at)
        item['source_ok'] = False
        self.assertEqual(self.history.record(item, config(), self.at + 1), 0)

    def test_history_is_not_shared_across_host_or_configuration(self):
        item = node(self.at)
        self.history.record(item, config(), self.at + 1)
        for other, host in ((config(19002), item['host_fingerprint']), (config(), 'f' * 64)):
            result = self.history.series(other, host, self.at + BUCKET_SECONDS)
            self.assertIsNone(result['points'][-1]['prefill_tps'])
        item2 = node(self.at, source='1' * 64)
        self.history.record(item2, config(), self.at + 1)
        result = self.history.series(config(), item['host_fingerprint'], self.at + BUCKET_SECONDS)
        self.assertEqual(result['points'][-1]['prefill_samples'], 2)

    def test_worker_restart_can_reuse_step_without_losing_new_observation(self):
        first = node(self.at, step=7)
        self.assertEqual(self.history.record(first, config(), self.at + 1), 2)
        second = node(self.at + BUCKET_SECONDS, step=7)
        second['prefill']['_observation_id'] = '9' * 64
        second['decode']['_observation_id'] = 'a' * 64
        self.assertEqual(self.history.record(second, config(), self.at + BUCKET_SECONDS + 1), 2)
        result = self.history.series(config(), first['host_fingerprint'], self.at + 2 * BUCKET_SECONDS)
        self.assertEqual(result['points'][-1]['prefill_samples'], 1)
        self.assertEqual(result['points'][-2]['prefill_samples'], 1)

    def test_observation_identity_uses_original_rank_logs_not_clock_estimate(self):
        raw = engine_sample(self.at)
        first = telemetry.summarize_node(raw, engine_config(), self.at - 3, self.at + 1)
        same = telemetry.summarize_node(raw, engine_config(), self.at - 2.9, self.at + 1)
        self.assertNotEqual(first['prefill']['log_at'], same['prefill']['log_at'])
        self.assertEqual(first['prefill']['_observation_id'], same['prefill']['_observation_id'])
        later = telemetry.summarize_node(engine_sample(self.at + 10), engine_config(),
                                         self.at + 7, self.at + 11)
        self.assertEqual(first['prefill']['step'], later['prefill']['step'])
        self.assertNotEqual(first['prefill']['_observation_id'], later['prefill']['_observation_id'])

    def test_unidentified_existing_file_is_not_modified(self):
        other = Path(self.tmp.name) / 'not-history.sqlite3'
        with closing(sqlite3.connect(other)) as db, db:
            db.execute('CREATE TABLE unrelated (value TEXT)')
            db.execute('INSERT INTO unrelated VALUES (?)', ('keep',))
        before = other.read_bytes()
        with self.assertRaisesRegex(ValueError, 'INVALID_TELEMETRY_HISTORY'):
            History(other)
        self.assertEqual(other.read_bytes(), before)

    def test_reopen_keeps_previous_samples_and_output_has_no_source_identity(self):
        item = node(self.at)
        self.history.record(item, config(), self.at + 1)
        result = History(self.path).series(config(), item['host_fingerprint'], self.at + BUCKET_SECONDS)
        self.assertEqual(result['points'][-1]['prefill_tps'], 100)
        serialized = json.dumps(result)
        self.assertNotIn(item['host_fingerprint'], serialized)
        self.assertNotIn(item['source_id'], serialized)

    def test_collector_records_and_exposes_only_sanitized_history(self):
        conf = engine_config()
        first = node(self.at)
        collector = telemetry.Collector({'interval_seconds': 30, 'nodes': [conf]},
                                        sample=lambda *_: first, history=self.history)
        self.addCleanup(collector.close)
        with mock.patch.object(telemetry.time, 'time', return_value=self.at + 1):
            collector.collect()
            self.assertTrue(collector.history_worker.changed.wait(3))
            collector.history_worker.changed.clear()
        next_now = (self.at // BUCKET_SECONDS + 1) * BUCKET_SECONDS + 10
        second = node(next_now - 1, step=5)
        collector.sample = lambda *_: second
        with mock.patch.object(telemetry.time, 'time', return_value=next_now):
            collector.collect()
            self.assertTrue(collector.history_worker.changed.wait(3))
            result = telemetry.public_snapshot(collector.snapshot())
        self.assertTrue(result['history_available'])
        point = result['nodes'][0]['history']['points'][-1]
        self.assertEqual(point['prefill_tps'], 100)
        self.assertNotIn('host_fingerprint', result['nodes'][0])
        self.assertNotIn('source_id', result['nodes'][0])
        self.assertNotIn('_observation_id', result['nodes'][0]['prefill'])

    def test_history_failure_does_not_clear_live_observations(self):
        conf = engine_config()
        item = node(self.at)
        broken = mock.Mock()
        broken.record.side_effect = sqlite3.OperationalError('synthetic')
        collector = telemetry.Collector({'interval_seconds': 30, 'nodes': [conf]},
                                        sample=lambda *_: item, history=broken)
        self.addCleanup(collector.close)
        with mock.patch.object(telemetry.time, 'time', return_value=self.at + 1):
            collector.collect()
            self.assertTrue(collector.history_worker.changed.wait(3))
            result = collector.snapshot()
        self.assertFalse(result['history_available'])
        self.assertEqual(result['nodes'][0]['prefill']['tokens_per_second'], 100)

    def test_history_flows_through_the_loopback_dashboard(self):
        now = 100001 * BUCKET_SECONDS + 1
        conf = engine_config()
        item = node(now - 3)
        collector = telemetry.Collector({'interval_seconds': 30, 'nodes': [conf]},
                                        sample=lambda *_: item, history=self.history)
        self.addCleanup(collector.close)
        sidecar = telemetry.Server(0, collector)
        dashboard = web.Dashboard(0, Path(self.tmp.name)/'unused.sqlite3', telemetry_port=sidecar.server_port)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (sidecar, dashboard)]
        for thread in threads:
            thread.start()
        try:
            with mock.patch.object(telemetry.time, 'time', return_value=now):
                collector.collect()
                self.assertTrue(collector.history_worker.changed.wait(3))
                connection = HTTPConnection('127.0.0.1', dashboard.server_port, timeout=3)
                connection.request('GET', '/api/telemetry')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                result = json.loads(response.read())
                connection.close()
            self.assertTrue(result['available'])
            self.assertTrue(result['history_available'])
            self.assertEqual(result['nodes'][0]['history']['points'][-1]['prefill_tps'], 100)
            self.assertEqual(result['nodes'][0]['history']['points'][-1]['decode_tps'], 0)
            self.assertNotIn('host_fingerprint', result['nodes'][0])
            self.assertNotIn('source_id', result['nodes'][0])
        finally:
            for server in (dashboard, sidecar):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(3)

    def test_slow_history_io_does_not_block_live_sampling_or_snapshot(self):
        gate = threading.Event()
        started = threading.Event()
        slow = mock.Mock()
        def slow_record(*_):
            started.set()
            gate.wait(3)
        slow.record.side_effect = slow_record
        slow.series.return_value = None
        collector = telemetry.Collector({'interval_seconds': 30, 'nodes': [engine_config()]},
                                        sample=lambda *_: node(self.at), history=slow)
        self.addCleanup(collector.close)
        self.addCleanup(gate.set)
        with mock.patch.object(telemetry.time, 'time', return_value=self.at + 1):
            start = time.monotonic()
            collector.collect()
            self.assertTrue(started.wait(1))
            result = collector.snapshot()
            self.assertLess(time.monotonic() - start, 1.5)
        self.assertFalse(result['history_available'])
        self.assertEqual(result['nodes'][0]['prefill']['tokens_per_second'], 100)

    def test_history_initialization_failure_does_not_raise(self):
        missing = Path(self.tmp.name) / 'absent' / 'history.sqlite3'
        self.assertIsNone(telemetry.open_history(missing))


if __name__ == '__main__':
    unittest.main()
