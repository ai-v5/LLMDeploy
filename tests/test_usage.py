from datetime import datetime
from contextlib import closing
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deploy_usage.report import report, TZ, _covered
from deploy_usage.store import Store
from deploy_notify import tracker, usage, worker, incoming


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.midnight = datetime(2026, 10, 20, tzinfo=TZ).timestamp()

    def store(self, run='run-a', expected=1, now=None):
        store = Store(self.root, run, expected, self.midnight if now is None else now)
        self.addCleanup(store.db.close)
        return store

    def test_midnight_and_rolling_windows_and_total_survive_restart(self):
        day = self.midnight
        old = self.store(now=day - 8 * 86400)
        for seconds, tokens in [(-8 * 86400, 10), (-4 * 86400, 20), (-2 * 86400, 30), (-100, 40), (100, 50)]:
            old.record((tokens, tokens, tokens // 2, 1, tokens - 1, 2), day + seconds)
        old.close(day + 101)
        new = self.store('run-b', now=day + 102)
        new.record((60, 60, 30, 1, 59, 2), day + 200)
        value = report(self.root, day + 300)
        self.assertEqual([value['windows'][k]['output'] for k in ('today', '24h', '3d', '7d', 'total')], [110, 150, 180, 200, 210])
        self.assertAlmostEqual(value['windows']['today']['output_tps'], 110 / 300)
        self.assertEqual(value['windows']['total']['cached'], 105)

    def test_all_writers_must_be_covered_and_runs_are_not_multiplied(self):
        now = self.midnight
        a = self.store(expected=2)
        a.record((100, 20, 30, 1, 19, 2), now + 10)
        self.assertEqual(report(self.root, now + 20)['windows']['total']['covered_seconds'], 0)
        b = self.store(expected=2)
        b.record((200, 40, 100, 1, 39, 3), now + 10)
        a.heartbeat(now + 20)
        b.heartbeat(now + 20)
        value = report(self.root, now + 21)['windows']['total']
        self.assertEqual((value['input'], value['output'], value['cached']), (300, 60, 130))
        self.assertEqual(value['covered_seconds'], 20)
        self.assertAlmostEqual(value['decode_tps'], 58 / 5)
        a.heartbeat(now + 100)
        b.heartbeat(now + 100)
        self.assertEqual(report(self.root, now + 101)['windows']['total']['covered_seconds'], 20)

    def test_store_fault_or_missing_database_never_becomes_complete_zero(self):
        empty = report(self.root, self.midnight)
        self.assertIsNone(empty['since'])
        a = self.store()
        a.record((10, 3, 5, 1, 2, 1), self.midnight + 5)
        a.fail()
        value = report(self.root, self.midnight + 10)
        value.update(received_epoch=self.midnight + 10, clock_skew_seconds=0)
        self.assertEqual(value['errors'], 1)
        self.assertEqual(value['windows']['total']['covered_seconds'], 0)
        self.assertIn('统计不完整', '\n'.join(usage.lines(value, self.midnight + 10)))
        (self.root / 'broken.sqlite3').write_bytes(b'broken')
        self.assertEqual(report(self.root, self.midnight + 10)['errors'], 2)

    def test_one_writer_clock_overlap_cannot_substitute_for_missing_writer(self):
        runs = {'run': (2, [[(0, 10), (5, 15)], []])}
        self.assertEqual(_covered(runs, 0, 20), 0)
        runs['run'][1][1] = [(0, 20)]
        self.assertEqual(_covered(runs, 0, 20), 15)

    def test_committed_counts_survive_unclosed_connection_and_read_does_not_write(self):
        a = self.store()
        a.record((12, 7, 8, 0, 0, 0), self.midnight + 2)
        # A separate reader sees the committed WAL before the writer closes.
        self.assertEqual(report(self.root, self.midnight + 3)['windows']['total']['output'], 7)
        with closing(sqlite3.connect(a.path)) as db:
            self.assertEqual(dict(db.execute('SELECT key,value FROM meta'))['closed'], '0')

    def test_status_has_all_windows_per_node_and_never_remote_strings(self):
        a = self.store()
        a.record((100, 20, 40, 1, 19, 2), self.midnight + 10)
        now = self.midnight + 20
        value = report(self.root, now)
        value.update(received_epoch=now, clock_skew_seconds=0)
        config = {'nodes': {'19001': '192.0.2.106', '19002': '198.51.100.47'},
                  'freshness_seconds': 15, 'usage': {'enabled': True},
                  '_usage_snapshot': {'19001': value, '19002': value}}
        snapshot = {'epoch': now, 'backends': [{'port': 19001, 'healthy': True}, {'port': 19002, 'healthy': True}]}
        text = tracker.status_text(config, snapshot, now)
        for label in ('今天', '近24小时', '近3天', '近7天', '累计'):
            self.assertEqual(text.count(label + '：'), 2)
        self.assertNotIn('192.0.2.106', text)
        self.assertIn('*.*.*.106', text)
        self.assertIn('入100 出20 缓40', text)
        self.assertIn('不能视为 0', usage.lines(value, now + 100)[0])
        settings = {'target': 'fixture-host', 'code_dir': '/opt/usage', 'data_dir': '/var/lib/usage'}
        value['windows']['today']['input'] = 'private-text-never-forward'
        with patch('deploy_notify.usage.subprocess.run', return_value=Mock(returncode=0, stdout=json.dumps(value).encode())):
            self.assertEqual(usage.fetch_all({'19001': settings}), {'19001': None})

    def test_target_and_path_cannot_inject_ssh_options_or_shell_commands(self):
        base = {'target': 'fixture-host', 'code_dir': '/opt/usage', 'data_dir': '/var/lib/usage'}
        for key, value in [('target', '-oProxyCommand=bad'), ('target', 'host;id'), ('code_dir', '/opt/x;id'), ('data_dir', '/var/../etc')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                usage.validate({'1': {**base, key: value}}, {'1': 'node'})

    def test_remote_clock_skew_does_not_make_fresh_received_data_unavailable(self):
        a = self.store()
        a.record((100, 20, 40, 1, 19, 2), self.midnight + 10)
        settings = {'target': 'fixture-host', 'code_dir': '/opt/usage', 'data_dir': '/var/lib/usage'}
        for skew in (-40, 40):
            local = self.midnight + 100
            remote = report(self.root, local + skew)
            with patch('deploy_notify.usage.time.time', side_effect=[local - 1, local]), patch(
                    'deploy_notify.usage.subprocess.run', return_value=Mock(returncode=0, stdout=json.dumps(remote).encode())):
                fetched = usage._fetch(settings)
            text = '\n'.join(usage.lines(fetched, local + 2))
            self.assertIn('今天：', text)
            self.assertIn('主机时钟', text)
            self.assertIn('不能视为 0', '\n'.join(usage.lines(fetched, local + 91)))

    def test_plugin_deltas_match_engine_and_storage_failure_is_fail_open(self):
        fake = ModuleType('vllm.v1.metrics.loggers')
        fake.AggregateStatLoggerBase = type('AggregateStatLoggerBase', (), {})
        with patch.dict(sys.modules, {'vllm.v1.metrics.loggers': fake}):
            sys.modules.pop('deploy_usage.plugin', None)
            plugin = importlib.import_module('deploy_usage.plugin')
        with patch.dict('os.environ', {'LLMDEPLOY_USAGE_DIR': str(self.root), 'LLMDEPLOY_USAGE_RUN': 'fixture', 'LLMDEPLOY_USAGE_WRITERS': '1'}):
            logger = plugin.UsageLogger(None, [0, 1])
            self.assertTrue(logger.flush())
        self.addCleanup(logger._close)
        stats = SimpleNamespace(num_prompt_tokens=100, num_generation_tokens=5,
            prompt_token_stats=SimpleNamespace(cached_tokens=60), iteration_timestamp=self.midnight + 10,
            finished_requests=[SimpleNamespace(num_generation_tokens=5, decode_time=2)])
        logger.record(None, stats)
        self.assertTrue(logger.flush())
        self.assertEqual(report(self.root, self.midnight + 11)['windows']['total']['output'], 5)
        with patch.object(logger.store, 'record', side_effect=OSError('private path')):
            logger.record(None, stats)
            logger.writer.join(timeout=2)
        self.assertTrue(logger.failed)
        self.assertEqual(report(self.root, self.midnight + 11)['errors'], 1)

    def test_plugin_initialization_and_slow_disk_do_not_block_callbacks(self):
        fake = ModuleType('vllm.v1.metrics.loggers')
        fake.AggregateStatLoggerBase = type('AggregateStatLoggerBase', (), {})
        with patch.dict(sys.modules, {'vllm.v1.metrics.loggers': fake}):
            sys.modules.pop('deploy_usage.plugin', None)
            plugin = importlib.import_module('deploy_usage.plugin')
        with patch('deploy_usage.plugin.Store', side_effect=OSError('private path')):
            failed = plugin.UsageLogger(None, [0])
            failed.writer.join(timeout=2)
            failed.record(None, None)
            failed.log()
            failed._close()
            self.assertTrue(failed.failed)
        with patch.dict('os.environ', {'LLMDEPLOY_USAGE_DIR': str(self.root), 'LLMDEPLOY_USAGE_RUN': 'slow', 'LLMDEPLOY_USAGE_WRITERS': '1'}):
            logger = plugin.UsageLogger(None, [0])
            self.assertTrue(logger.flush())
        self.addCleanup(logger._close)
        stats = SimpleNamespace(num_prompt_tokens=1, num_generation_tokens=1,
            prompt_token_stats=SimpleNamespace(cached_tokens=0), iteration_timestamp=self.midnight + 1, finished_requests=[])
        with patch.object(logger.store, 'record', side_effect=lambda *args: time.sleep(0.2)):
            start = time.monotonic()
            logger.record(None, stats)
            self.assertLess(time.monotonic() - start, 0.05)
            self.assertTrue(logger.flush())

    def test_renderer_usage_is_explicit_and_preserves_recipe(self):
        from scripts import render
        host = render.load(Path(__file__).resolve().parents[1] / 'hosts/950dt8.example.json')
        recipe = render.load(render.RECIPE)
        args = render.command(host, recipe, repo_path='/srv/LLMDeploy', optional_devices=[], optional_mounts=[], usage=True)
        self.assertIn('LLMDEPLOY_USAGE_DIR=/artifacts/usage', args)
        self.assertIn('LLMDEPLOY_USAGE_WRITERS=8', args)
        self.assertIn('register_usage.py', args[-1])
        self.assertIn('--limit-mm-per-prompt', args[-1])
        self.assertIn('{"image":640}', args[-1])


if __name__ == '__main__':
    unittest.main()
