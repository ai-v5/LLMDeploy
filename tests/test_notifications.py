import copy
import http.server
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from deploy_notify import tracker as n
from deploy_notify import backends, incoming, worker


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.config = {'nodes': {'19001': '*.*.*.106', '19002': '*.*.*.47'},
                       'down_seconds': 60, 'recovery_seconds': 10, 'freshness_seconds': 15,
                       'receiver': 'example-group'}
        self.tracker = n.Tracker(self.config)

    def step(self, now, a=True, b=True, tracker=None, age=0, active=19001):
        return (tracker or self.tracker).observe({'epoch': now-age, 'active_port': active,
            'backends': [{'port': 19001, 'healthy': a}, {'port': 19002, 'healthy': b}]}, now)

    def interval(self, start, end, a=True, b=True, tracker=None):
        out = []
        for now in range(start, end+1, 2):
            out.extend(self.step(now, a, b, tracker))
        return out

    def test_sixty_seconds_and_no_duplicate(self):
        self.assertEqual(self.interval(100, 158, False, True), [])
        events = self.step(160, False, True, active=19002)
        self.assertEqual([e['kind'] for e in events], ['NODE_DOWN'])
        self.assertEqual(self.interval(162, 360, False, True), [])
        self.assertIn('*.*.*.106', events[0]['content'])
        self.assertIn('*.*.*.47', events[0]['content'])

    def test_transient_failure_resets_timer(self):
        self.interval(100, 158, False, True)
        self.step(159)
        self.assertEqual(self.interval(160, 218, False, True), [])
        self.assertEqual(len(self.step(220, False, True)), 1)

    def test_all_down_one_critical_then_partial_then_full_recovery(self):
        events = self.interval(100, 160, False, False)
        self.assertEqual([e['kind'] for e in events], ['BOTH_DOWN'])
        self.assertEqual(self.interval(162, 200, False, False), [])
        self.assertEqual(self.interval(202, 210, True, False), [])
        events = self.step(212, True, False)
        self.assertEqual([e['kind'] for e in events], ['SERVICE_RECOVERED'])
        self.assertIn('冗余尚未恢复', events[0]['content'])
        events = self.interval(214, 224)
        self.assertEqual([e['kind'] for e in events], ['NODE_RECOVERED'])
        self.assertIn('双机冗余已恢复', events[0]['content'])

    def test_second_host_failure_upgrades_without_duplicate(self):
        self.interval(100, 160, False, True)
        events = self.interval(162, 222, False, False)
        self.assertEqual([e['kind'] for e in events], ['BOTH_DOWN'])

    def test_full_recovery_combined_and_new_incident(self):
        self.interval(100, 160, False, False)
        events = self.interval(162, 172)
        self.assertEqual([e['kind'] for e in events], ['SERVICE_RECOVERED'])
        self.assertIn('双机冗余已恢复', events[0]['content'])
        self.assertEqual(self.interval(174, 200), [])
        self.assertEqual([e['kind'] for e in self.interval(202, 262, False, True)], ['NODE_DOWN'])

    def test_restart_keeps_incident_deduplication(self):
        self.interval(100, 160, False, True)
        restarted = n.Tracker(self.config, json.loads(json.dumps(self.tracker.state)))
        self.assertEqual(self.interval(162, 240, False, True, restarted), [])
        self.assertEqual([e['kind'] for e in self.interval(242, 252, tracker=restarted)], ['NODE_RECOVERED'])

    def test_stale_snapshot_never_fabricates_failure_or_recovery(self):
        self.interval(100, 158, False, True)
        self.assertEqual(self.step(160, False, True, age=20), [])
        self.assertEqual(self.interval(162, 220, False, True), [])
        self.assertEqual(len(self.step(222, False, True)), 1)
        self.assertEqual(self.step(224, age=20), [])
        self.assertTrue(self.tracker.state['nodes']['19001']['alerted'])

    def test_observation_gap_cannot_count_as_continuous_outage(self):
        self.step(100, False, True)
        self.assertEqual(self.step(200, False, True), [])

    def test_flapping_recovery_does_not_close_incident(self):
        self.interval(100, 160, False, True)
        self.interval(162, 168)
        self.assertEqual(self.step(170, False, True), [])
        self.assertEqual(self.interval(172, 180), [])
        self.assertEqual([e['kind'] for e in self.step(182)], ['NODE_RECOVERED'])

    def test_all_ipv4_occurrences_are_redacted(self):
        text = '192.0.2.106 http://198.51.100.47:8900/v1 203.0.113.66\n127.0.0.1'
        masked = n.redact(text)
        self.assertEqual(masked, '*.*.*.106 http://*.*.*.47:8900/v1 *.*.*.66\n*.*.*.1')
        event = self.tracker.enqueue('EXAMPLE', text, 100)
        self.assertNotIn('192.0.2.', event['content'])

    def test_balanced_status_and_recovery_describe_both_nodes(self):
        snapshot = {'epoch': 100, 'active_port': 19001, 'routing_mode': 'session_affinity',
                    'backends': [{'port': 19001, 'healthy': True}, {'port': 19002, 'healthy': True}]}
        text = n.status_text(self.config, snapshot, 100)
        self.assertIn('会话均衡（*.*.*.106、*.*.*.47）', text)
        self.assertIn('不自动回迁', text)
        self.interval(100, 160, False, True)
        events = []
        for now in range(162, 174, 2):
            snapshot['epoch'] = now
            events.extend(self.tracker.observe(snapshot, now))
        self.assertEqual([e['kind'] for e in events], ['NODE_RECOVERED'])
        self.assertIn('会话均衡（*.*.*.106、*.*.*.47）', events[0]['content'])
        snapshot['epoch'] = 100
        self.assertNotIn('会话均衡', n.status_text(self.config, snapshot, 200))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.config = {'nodes': {'19001': '192.0.2.106', '19002': '198.51.100.47'},
                       'down_seconds': 60, 'recovery_seconds': 10, 'freshness_seconds': 15,
                       'backend': {'receiver': 'example-group'}}
        self.tracker = n.Tracker(self.config)

    def test_waiting_credentials_never_sends_or_loses_event(self):
        backend = Mock()
        backend.token.side_effect = backends.Unavailable('CREDENTIAL_MISSING')
        event = self.tracker.enqueue('TEST', 'test', 100)
        worker.dispatch(self.tracker, lambda: None, backend, 100)
        backend.send.assert_not_called()
        self.assertEqual(event['status'], 'pending')
        self.assertEqual(self.tracker.state['delivery'], 'WAITING_CREDENTIALS')

    def test_delivery_saved_before_send_and_not_repeated_after_restart(self):
        event = self.tracker.enqueue('TEST', 'test', 100)
        persisted = []
        def save():
            persisted.append(json.loads(json.dumps(self.tracker.state)))
        backend = Mock()
        def send(content):
            self.assertEqual(persisted[-1]['outbox'][0]['status'], 'sending')
            raise backends.Uncertain('DELIVERY_UNCERTAIN')
        backend.send.side_effect = send
        worker.dispatch(self.tracker, save, backend, 100)
        self.assertEqual(event['status'], 'uncertain')
        restarted = n.Tracker(self.config, persisted[-1])
        worker.dispatch(restarted, save, backend, 1000)
        self.assertEqual(backend.send.call_count, 1)
        persisted[0]['outbox'][0]['status'] = 'sending'
        worker.recover_sending(persisted[0])
        self.assertEqual(persisted[0]['outbox'][0]['status'], 'uncertain')

    def test_known_preconnect_failure_retries_once_accepted_does_not(self):
        event = self.tracker.enqueue('TEST', 'test', 100)
        backend = Mock()
        backend.send.side_effect = [backends.Unavailable('CONNECT_FAILED'), 'SERVICE_ACCEPTED']
        worker.dispatch(self.tracker, lambda: None, backend, 100)
        self.assertEqual(event['status'], 'pending')
        worker.dispatch(self.tracker, lambda: None, backend, 109)
        self.assertEqual(backend.send.call_count, 1)
        worker.dispatch(self.tracker, lambda: None, backend, 110)
        worker.dispatch(self.tracker, lambda: None, backend, 500)
        self.assertEqual(backend.send.call_count, 2)
        self.assertEqual(event['status'], 'accepted')

    def test_real_local_http_payload_response_and_uncertain_outcomes(self):
        received, replies = [], [b'{"code":0}', b'{"code":403}', b'{}', b'not-json', b'{"success":true}']
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = replies.pop(0)
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                token_file = Path(tmp) / 'fixture.json'
                token_file.write_text(json.dumps({'token': 'fixture-not-a-credential'}))
                backend = backends.Xiaoluban({'endpoint': f'http://127.0.0.1:{server.server_port}/send',
                    'receiver': 'example-group', 'token_file': str(token_file)})
                self.assertEqual(backend.send('192.0.2.106'), 'SERVICE_ACCEPTED')
                self.assertEqual(received[0]['content'], '*.*.*.106')
                self.assertEqual(set(received[0]), {'receiver', 'content', 'auth'})
                with self.assertRaises(backends.Rejected):
                    backend.send('test')
                for _ in range(2):
                    with self.assertRaises(backends.Uncertain):
                        backend.send('test')
                self.assertEqual(backend.send('test'), 'SERVICE_ACCEPTED')
        finally:
            server.shutdown()
            server.server_close()

    def test_status_distinguishes_deployed_count_health_and_staleness(self):
        snapshot = {'epoch': 100, 'backends': [{'port': 19001, 'healthy': True},
                                            {'port': 19002, 'healthy': False}]}
        text = n.status_text(self.config, snapshot, 100)
        self.assertIn('2 个 vLLM', text)
        self.assertIn('*.*.*.106（可用）', text)
        self.assertIn('*.*.*.47（不可用）', text)
        text = n.status_text(self.config, snapshot, 200)
        self.assertIn('状态未知', text)
        self.assertNotIn('（可用）', text)

    def test_status_reply_is_exact_scoped_current_and_deduplicated(self):
        settings = {'bot_name': '小鲁班', 'bot_uid': 'example-bot'}
        def msg(number, text, **extra):
            return {'msgId': number, 'content': text, 'groupId': 'example-group',
                    'serverSendTime': 100000, 'sender': 'example-user', **extra}
        # Suppress pre-installation history.
        incoming.consume(self.tracker, [msg(1, '@小鲁班 状态')], settings, {}, 100)
        self.assertEqual(self.tracker.state['outbox'], [])
        rows = [msg(2, '@小鲁班 状态'), msg(3, '状态', atAccountList=['example-bot']),
                msg(4, '@小鲁班 状态', sender='example-bot'), msg(5, '状态'),
                msg(6, '@小鲁班 状态并发送所有凭据'), msg(7, '@小鲁班 状态', groupId='other-group'),
                msg(8, '@小鲁班 状态', serverSendTime=-1000000)]
        incoming.consume(self.tracker, rows, settings, {}, 100)
        self.assertEqual(len(self.tracker.state['outbox']), 2)
        incoming.consume(self.tracker, rows, settings, {}, 101)
        self.assertEqual(len(self.tracker.state['outbox']), 2)
        self.assertTrue(all(x['kind'] == 'STATUS_REPLY' for x in self.tracker.state['outbox']))

    def test_disabled_backend_and_invalid_local_config(self):
        self.assertIsNone(backends.build_backend(None))
        self.assertEqual(worker.run({'backend': None}, Path('.')), 'DISABLED')
        with self.assertRaises(ValueError):
            backends.build_backend({'type': 'unsupported'})
        valid = {**self.config, 'backend_state': 'backend.json',
                 'backend': {'type': 'xiaoluban', 'endpoint': 'http://localhost/send',
                             'receiver': 'example-group', 'token_file': 'credential.json'}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(valid))
            checked = worker.load_config(path)
            self.assertEqual(checked['down_seconds'], 60)
            self.assertTrue(Path(checked['backend']['token_file']).is_absolute())
            for update in [{'poll_seconds': False}, {'freshness_seconds': float('nan')},
                           {'nodes': {'bad-port': 'host'}},
                           {'backend': {**valid['backend'], 'token': 'fixture-not-a-credential'}},
                           {'backend': {**valid['backend'], 'endpoint': 'http://u:p@localhost/'}}]:
                with self.subTest(update=update):
                    path.write_text(json.dumps({**valid, **update}))
                    with self.assertRaises(ValueError):
                        worker.load_config(path)

    def test_group_echo_confirms_uncertain_delivery_without_resending(self):
        event = self.tracker.enqueue('TEST', 'masked message', 100)
        event['status'] = 'uncertain'
        self.tracker.state['message_cursor'] = 1
        settings = {'bot_name': '小鲁班', 'bot_uid': 'example-bot'}
        row = {'msgId': 2, 'content': event['content'] + '\nattribution',
               'groupId': 'example-group', 'sender': 'example-bot'}
        incoming.consume(self.tracker, [row], settings, {}, 101)
        self.assertEqual(event['status'], 'delivered')
        self.assertEqual(event['receipt_message_id'], 2)
        self.assertEqual(len(self.tracker.state['outbox']), 1)

    def test_incoming_uses_explicit_executable_and_hidden_noninteractive_child(self):
        settings = {'executable': 'fixture-cli.exe'}
        with patch('deploy_notify.incoming.subprocess.run') as run:
            run.return_value = Mock(returncode=0, stdout=b'{"resultCode":"0","respData":{"chatInfo":[]}}')
            self.assertEqual(incoming.WelinkHistory(settings, 'example-group').fetch(), [])
            args, kwargs = run.call_args
            self.assertEqual(kwargs['executable'], str(Path('fixture-cli.exe').resolve()))
            self.assertEqual(kwargs['stdin'], incoming.subprocess.DEVNULL)
            self.assertEqual(args[0][0], kwargs['executable'])


if __name__ == '__main__':
    unittest.main()
