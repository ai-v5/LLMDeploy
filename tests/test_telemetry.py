from copy import deepcopy
from http.client import HTTPConnection
import threading
import time
import unittest
from unittest import mock

from deploy_stats import remote_observe, telemetry, web


HASHES = {key: 'a' * 64 for key in telemetry.SOURCE_KEYS}


def node(label='one', port=19001):
    return {'label': label, 'backend_port': port, 'ssh_target': 'user@example.invalid',
            'container': 'example-container', 'image_fingerprint': 'c' * 64, 'driver_sha256': '9' * 64,
            'source_sha256': HASHES.copy(), 'rank_devices': [0, 1],
            'prefill_flops_per_token': 10.0, 'peak_flops_per_device': 1000.0}


def sample(now, *, phase=True):
    rows = [{'kind': 'stats', 'at': now - 1 + rank * .01, 'rank': rank, 'device': rank,
             'step': 10, 'is_prompt': phase, 'current_tps': 100 + rank * 10,
             'current_sps': 10 + rank * .01} for rank in range(2)]
    rows += [{'kind': 'cpu', 'at': now - .9 + rank * .01, 'rank': rank, 'device': rank, 'step': 10,
              'timers': {'prefill': {'mean_ms': 20 + rank * 10, 'calls': rank + 1}}} for rank in range(2)]
    return {'schema': 1, 'source_id': 'b' * 64, 'host_fingerprint': 'd' * 64, 'image_fingerprint': 'c' * 64,
            'driver_sha256': '9' * 64,
            'source_sha256': HASHES.copy(), 'source_before_start': True,
            'source_started_at': now - 100, 'started_at': now - 2, 'finished_at': now,
            'hbm': [{'id': i, 'started_at': now - .55 + i * .01, 'finished_at': now - .45 + i * .01,
                     'hbm_bandwidth_percent': 20 + i * 10} for i in range(2)],
            'engine_rows': rows}


class TelemetryTests(unittest.TestCase):
    def test_log_parsing_keeps_good_fields_when_phase_share_is_nan(self):
        line = ('2026-11-02T00:00:00.123456789Z INFO RANK 0, GPU 0, step 17, [stats] '
                '(is_prompt True | cur_SPS 10.000 | cur_TPS 12.5 | prompt_TPS 0.0 | '
                'completion_TPS 0.0 | prefill/decode nan%/nan% | MFU 0.123)\n')
        row = remote_observe.parse_log(line.encode())[0]
        self.assertTrue(row['is_prompt'])
        self.assertEqual(row['current_tps'], 12.5)
        self.assertEqual(row['current_sps'], 10)
        self.assertIsNone(row['phase_share'])

    def test_same_second_source_mtime_does_not_prove_runtime_identity(self):
        count = len(remote_observe.SOURCES)
        self.assertFalse(remote_observe.source_predates(['100'] * count, 100.9))
        self.assertTrue(remote_observe.source_predates(['99'] * count, 100.1))
        self.assertFalse(remote_observe.source_predates(['99'] * (count - 1), 100.1))

    def test_docker_observation_does_not_activate_socket(self):
        active = mock.Mock(returncode=0, stdout=b'active\n')
        inactive = mock.Mock(returncode=3, stdout=b'inactive\n')
        with mock.patch.object(remote_observe.subprocess, 'run', side_effect=[active, inactive]):
            self.assertTrue(remote_observe.docker_ready())
        with mock.patch.object(remote_observe.subprocess, 'run', side_effect=[inactive, active]):
            self.assertFalse(remote_observe.docker_ready())
        with mock.patch.object(remote_observe, 'docker_ready', return_value=False), \
                mock.patch.object(remote_observe.subprocess, 'Popen') as popen:
            with self.assertRaises(ValueError):
                remote_observe.bounded_command(['docker', 'inspect', 'synthetic'], 1, 100)
            popen.assert_not_called()

    def test_complete_matching_step_and_device_samples(self):
        now = time.time()
        result = telemetry.summarize_node(sample(now), node(), now - 3, now + 1)
        self.assertTrue(result['source_ok'])
        self.assertEqual(result['prefill']['tokens_per_second'], 210)
        self.assertAlmostEqual(result['prefill']['mfu_estimate'], 1.05)
        self.assertEqual(result['hbm']['mean_percent'], 25)
        self.assertAlmostEqual(result['phase_timers']['prefill']['mean_ms'], 80 / 3)
        self.assertIsNone(result['decode'])

    def test_missing_duplicate_mixed_stale_and_skewed_steps_are_unknown(self):
        now = time.time()
        variants = []
        missing = sample(now); missing['engine_rows'] = missing['engine_rows'][1:]; variants.append(missing)
        duplicate = sample(now); duplicate['engine_rows'].append(deepcopy(duplicate['engine_rows'][0])); variants.append(duplicate)
        mixed = sample(now); mixed['engine_rows'][1]['is_prompt'] = False; variants.append(mixed)
        stale = sample(now); stale['engine_rows'][0]['at'] = now - 70; variants.append(stale)
        spread = sample(now); spread['engine_rows'][1]['current_sps'] = 20; variants.append(spread)
        rounded = sample(now); rounded['engine_rows'][0]['current_sps'] = .001; rounded['engine_rows'][1]['current_sps'] = .001; variants.append(rounded)
        wrong_device = sample(now); wrong_device['engine_rows'][1]['device'] = 0; variants.append(wrong_device)
        for raw in variants:
            with self.subTest(raw=raw):
                value = telemetry.summarize_node(raw, node(), now - 3, now + 1)
                self.assertIsNone(value['prefill'])
                self.assertIsNotNone(value['hbm'])

    def test_unknown_source_invalid_bandwidth_and_clock_are_not_zero(self):
        now = time.time()
        raw = sample(now); raw['source_sha256']['stats'] = 'b' * 64
        self.assertFalse(telemetry.summarize_node(raw, node(), now - 3, now + 1)['source_ok'])
        raw = sample(now); raw['source_before_start'] = False
        self.assertFalse(telemetry.summarize_node(raw, node(), now - 3, now + 1)['source_ok'])
        raw = sample(now); raw['image_fingerprint'] = 'e' * 64
        self.assertFalse(telemetry.summarize_node(raw, node(), now - 3, now + 1)['source_ok'])
        raw = sample(now); raw['driver_sha256'] = '8' * 64
        value = telemetry.summarize_node(raw, node(), now - 3, now + 1)
        self.assertTrue(value['source_ok'])
        self.assertFalse(value['driver_ok'])
        self.assertIsNone(value['hbm'])
        self.assertIsNotNone(value['prefill'])
        raw = sample(now); raw['hbm'][1]['hbm_bandwidth_percent'] = None
        value = telemetry.summarize_node(raw, node(), now - 3, now + 1)
        self.assertIsNone(value['hbm'])
        self.assertIsNotNone(value['prefill'])
        raw = sample(now); raw['finished_at'] = now + 20
        self.assertFalse(telemetry.summarize_node(raw, node(), now - 3, now + 1)['clock_ok'])

    def test_host_clock_offset_is_bounded_before_cross_node_combination(self):
        now = time.time()
        raw = sample(now)
        for key in ('source_started_at', 'started_at', 'finished_at'):
            raw[key] += 9
        for item in raw['engine_rows']:
            item['at'] += 9
        for item in raw['hbm']:
            item['started_at'] += 9
            item['finished_at'] += 9
        corrected = telemetry.summarize_node(raw, node(), now - 3, now + 1)
        self.assertTrue(corrected['clock_ok'])
        self.assertAlmostEqual(corrected['prefill']['log_at'], now - .99)
        self.assertAlmostEqual(corrected['clock_uncertainty_seconds'], 1)
        self.assertIsNotNone(corrected['hbm'])
        self.assertEqual(telemetry.summarize([corrected], now)['prefill_tps'], 210)

    def test_distinct_device_groups_are_not_combined(self):
        now = time.time()
        first = telemetry.summarize_node(sample(now), node(), now - 3, now + 1)
        other_node = node('two', 19002); other_node['peak_flops_per_device'] = 2000.0
        raw = sample(now); raw['host_fingerprint'] = 'e' * 64; raw['source_id'] = 'f' * 64
        second = telemetry.summarize_node(raw, other_node, now - 3, now + 1)
        total = telemetry.summarize([first, second], now)
        self.assertIsNone(total['prefill_tps'])
        self.assertIsNone(total['prefill_mfu'])
        self.assertIsNone(total['hbm_percent'])
        self.assertTrue(all(n['source_ok'] for n in total['nodes']))

    def test_duplicate_lifecycle_or_host_invalidates_both_groups(self):
        now = time.time()
        first = telemetry.summarize_node(sample(now), node(), now - 3, now + 1)
        second = telemetry.summarize_node(sample(now), node('two', 19002), now - 3, now + 1)
        values = telemetry.summarize([first, second], now)['nodes']
        self.assertTrue(all(n['duplicate_source'] and not n['source_ok'] and n['hbm'] is None for n in values))

    def test_public_snapshot_omits_internal_identity_fingerprints(self):
        now = time.time()
        value = telemetry.summarize([telemetry.summarize_node(sample(now), node(), now - 3, now + 1)], now)
        public = telemetry.public_snapshot(value)
        self.assertNotIn('source_id', public['nodes'][0])
        self.assertNotIn('host_fingerprint', public['nodes'][0])
        self.assertIn('source_id', value['nodes'][0])

    def test_combination_checks_full_rank_and_device_intervals(self):
        now = time.time()
        first = telemetry.summarize_node(sample(now), node(), now - 3, now + 1)
        second = telemetry.summarize_node(sample(now), node('two', 19002), now - 3, now + 1)
        first['clock_uncertainty_seconds'] = second['clock_uncertainty_seconds'] = 0
        first['prefill']['log_at'], first['prefill']['log_skew_seconds'] = now - 5, 5
        second['prefill']['log_at'], second['prefill']['log_skew_seconds'] = now, 5
        first['hbm']['sample_at'], first['hbm']['span_seconds'] = now - 5, 5
        second['hbm']['sample_at'], second['hbm']['span_seconds'] = now, 4.8
        result = telemetry.summarize([first, second], now)
        self.assertIsNone(result['prefill_tps'])
        self.assertIsNone(result['hbm_percent'])

    def test_snapshot_expires_by_oldest_rank_not_latest(self):
        now = time.time()
        collector = telemetry.Collector({'interval_seconds': 30, 'nodes': [node()]})
        item = telemetry.summarize_node(sample(now), node(), now - 3, now + 1)
        collector.value = telemetry.summarize([item], now)
        with mock.patch.object(telemetry.time, 'time', return_value=now + 59):
            self.assertIsNone(collector.snapshot()['nodes'][0]['prefill'])

    def test_device_mean_rejects_long_or_slow_sampling_spans(self):
        now = time.time()
        raw = sample(now)
        raw['started_at'] = now - 19
        raw['hbm'][0]['started_at'] = now - 18
        raw['hbm'][0]['finished_at'] = now - 17
        value = telemetry.summarize_node(raw, node(), now - 20, now + 1)
        self.assertTrue(value['source_ok'])
        self.assertIsNone(value['hbm'])

    def test_old_lifecycle_cpu_timer_cannot_attach_to_new_step(self):
        now = time.time()
        raw = sample(now)
        raw['source_started_at'] = now - 5
        for row in raw['engine_rows']:
            if row['kind'] == 'cpu':
                row['at'] = now - 60
        result = telemetry.summarize_node(raw, node(), now - 3, now + 1)
        self.assertIsNotNone(result['prefill'])
        self.assertIsNone(result['phase_timers']['prefill']['mean_ms'])

    def test_newer_log_group_wins_even_if_worker_step_counter_restarts(self):
        now = time.time()
        raw = sample(now)
        old = deepcopy(raw['engine_rows'][:2])
        for row in old:
            row['step'] = 99999
            row['at'] -= 20
        raw['engine_rows'].extend(old)
        result = telemetry.summarize_node(raw, node(), now - 3, now + 1)
        self.assertEqual(result['prefill']['step'], 10)

    def test_config_rejects_command_injection_and_duplicate_devices(self):
        config = {'interval_seconds': 30, 'nodes': [node()]}
        telemetry.validate_config(config)
        config['nodes'][0]['container'] = 'valid;evil'
        with self.assertRaises(ValueError):
            telemetry.validate_config(config)
        config = {'interval_seconds': 30, 'nodes': [node()]}
        config['nodes'][0]['rank_devices'] = [0, 0]
        with self.assertRaises(ValueError):
            telemetry.validate_config(config)
        config = {'interval_seconds': 30, 'nodes': [node()]}
        config['nodes'][0]['prefill_flops_per_token'] = 10 ** 400
        with self.assertRaises(ValueError):
            telemetry.validate_config(config)
        config = {'interval_seconds': 30, 'nodes': [node(), node('two', 19002)]}
        with self.assertRaises(ValueError):
            telemetry.validate_config(config)

    def test_loopback_server_rejects_untrusted_origin(self):
        collector = telemetry.Collector({'interval_seconds': 30, 'nodes': [node()]})
        server = telemetry.Server(0, collector)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        connection.request('GET', '/snapshot', headers={'Origin': 'https://example.invalid'})
        self.assertEqual(connection.getresponse().status, 403)
        connection.close()

    def test_dashboard_exposes_only_configured_telemetry(self):
        server = web.Dashboard(0, 'unused.db', telemetry_port=19883)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.object(web, 'read_telemetry', return_value={'available': True, 'nodes': []}) as reader:
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request('GET', '/api/telemetry')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'"available":true', response.read())
            reader.assert_called_once_with(19883)
            connection.close()


if __name__ == '__main__':
    unittest.main()
