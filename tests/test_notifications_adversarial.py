import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy_notify import backends, incoming, tracker, worker


class AdversarialTests(unittest.TestCase):
    def setUp(self):
        self.config = {'nodes': {'19001': '192.0.2.106', '19002': '198.51.100.47'},
                       'down_seconds': 60, 'recovery_seconds': 10, 'freshness_seconds': 15,
                       'backend': {'receiver': 'example-group'}}
        self.tracker = tracker.Tracker(self.config)
        self.incoming = {'bot_uid': 'example-bot', 'bot_name': '小鲁班'}

    def snapshot(self, epoch, a=True, b=True):
        return {'epoch': epoch, 'active_port': 19001,
                'backends': [{'port': 19001, 'healthy': a}, {'port': 19002, 'healthy': b}]}

    def alert_first_node(self):
        for now in range(100, 162, 2):
            self.tracker.observe(self.snapshot(now, False), now)

    def test_frozen_fresh_snapshot_cannot_confirm_recovery(self):
        self.alert_first_node()
        frozen = self.snapshot(162)
        events = []
        for now in range(162, 174, 2):
            events += self.tracker.observe(frozen, now)
        self.assertEqual(events, [])
        self.assertTrue(self.tracker.state['nodes']['19001']['alerted'])

    def test_redundancy_waits_for_each_node_to_stabilize(self):
        self.alert_first_node()
        self.tracker.observe(self.snapshot(162), 162)
        for now in range(164, 172, 2):
            self.tracker.observe(self.snapshot(now, True, False), now)
        events = self.tracker.observe(self.snapshot(172), 172)
        self.assertEqual([e['kind'] for e in events], ['NODE_RECOVERED'])
        self.assertNotIn('双机冗余已恢复', events[0]['content'])

    def test_duplicate_message_in_one_batch_only_replies_once(self):
        self.tracker.state['message_cursor'] = 1
        message = {'msgId': 2, 'groupId': 'example-group', 'sender': 'example-user',
                   'content': '@小鲁班 状态', 'serverSendTime': 100000}
        incoming.consume(self.tracker, [message, dict(message)], self.incoming, self.snapshot(100), 100)
        self.assertEqual(len(self.tracker.state['outbox']), 1)

    def test_expired_pending_status_is_not_sent_after_credentials_return(self):
        event = self.tracker.enqueue('STATUS_REPLY', 'old status', 100)
        backend = Mock()
        worker.dispatch(self.tracker, lambda: None, backend, 221)
        backend.send.assert_not_called()
        self.assertEqual(event['status'], 'expired')

    def test_pending_status_is_refreshed_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'backend.json'
            path.write_text(json.dumps(self.snapshot(110, True, False)), encoding='utf-8')
            self.config['backend_state'] = str(path)
            event = self.tracker.enqueue('STATUS_REPLY', tracker.status_text(self.config, self.snapshot(100), 100), 100)
            backend = Mock()
            backend.send.return_value = 'SERVICE_ACCEPTED'
            worker.dispatch(self.tracker, lambda: None, backend, 110)
            self.assertIn('*.*.*.47（不可用）', backend.send.call_args.args[0])
            self.assertEqual(event['status'], 'accepted')

    def test_ipv4_is_masked_next_to_chinese_and_identifier_characters(self):
        for value in ['后端192.0.2.106已恢复', 'node_192.0.2.106', 'npu192.0.2.106worker']:
            with self.subTest(value=value):
                self.assertNotIn('192.0.2.106', tracker.redact(value))
                self.assertIn('*.*.*.106', tracker.redact(value))

    def test_ipv6_labels_are_not_sent_in_full(self):
        for value in ['[2001:db8::106]:8900', '2001:db8:0:1:2:3:4:47', '::ffff:192.0.2.106']:
            with self.subTest(value=value):
                self.assertNotIn(value.split(']')[0].lstrip('['), tracker.redact(value))

    def send_fixture(self, response, content='test'):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.json'
            path.write_text(json.dumps({'token': 'fixture-not-a-credential'}), encoding='utf-8')
            backend = backends.Xiaoluban({'endpoint': 'http://localhost/send',
                'receiver': 'example-group', 'token_file': str(path)})
            with patch('deploy_notify.backends.http.client.HTTPConnection') as constructor:
                connection = constructor.return_value
                connection.getresponse.return_value.status = 200
                connection.getresponse.return_value.read.return_value = json.dumps(response).encode()
                result = backend.send(content)
                payload = json.loads(connection.request.call_args.args[2])
                return result, payload

    def test_application_server_error_is_uncertain_never_auto_retried(self):
        with self.assertRaises(backends.Uncertain):
            self.send_fixture({'code': 500})

    def test_contradictory_acknowledgement_is_uncertain(self):
        with self.assertRaises(backends.Uncertain):
            self.send_fixture({'success': True, 'code': 403})

    def test_active_token_is_removed_from_outbound_message_content(self):
        _, payload = self.send_fixture({'code': 0}, 'label fixture-not-a-credential')
        self.assertNotIn('fixture-not-a-credential', payload['content'])
        self.assertEqual(payload['auth'], 'fixture-not-a-credential')

    def test_delayed_fault_is_identified_as_historical(self):
        self.alert_first_node()
        backend = Mock()
        backend.send.return_value = 'SERVICE_ACCEPTED'
        worker.dispatch(self.tracker, lambda: None, backend, 200)
        self.assertIn('历史事件补发', backend.send.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
