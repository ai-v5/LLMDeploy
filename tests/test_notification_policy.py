import copy
import unittest
from unittest.mock import Mock

from deploy_notify.tracker import Tracker
from deploy_notify.worker import dispatch


class NotificationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = {'nodes': {'19001': 'first', '19002': 'second', '19008': 'third'},
                       'down_seconds': 60, 'node_down_seconds': 86400,
                       'recovery_seconds': 10, 'freshness_seconds': 15}
        self.tracker = Tracker(self.config)

    def sample(self, now, good, tracker=None):
        return (tracker or self.tracker).observe({'epoch': now, 'backends': [
            {'port': int(port), 'healthy': port in good} for port in self.config['nodes']]}, now)

    def interval(self, start, end, good, tracker=None):
        events = []
        for now in range(start, end + 1, 10):
            events.extend(self.sample(now, good, tracker))
        return events

    def test_single_node_day_boundary_and_silent_short_recovery(self):
        healthy = {'19002', '19008'}
        self.assertEqual(self.interval(100, 86490, healthy), [])
        events = self.sample(86500, healthy)
        self.assertEqual([e['kind'] for e in events], ['NODE_DOWN'])
        self.assertIn('86400', events[0]['content'])
        self.assertEqual(self.interval(86510, 86530, healthy), [])
        self.assertEqual([e['kind'] for e in self.interval(86540, 86550, set(self.config['nodes']))],
                         ['NODE_RECOVERED'])

    def test_two_down_with_one_available_do_not_alert_before_day(self):
        self.assertEqual(self.interval(100, 86490, {'19008'}), [])
        self.assertEqual([e['kind'] for e in self.sample(86500, {'19008'})],
                         ['NODE_DOWN', 'NODE_DOWN'])

    def test_short_node_outage_and_recovery_are_both_silent(self):
        self.assertEqual(self.interval(100, 3700, {'19002', '19008'}), [])
        self.assertEqual(self.interval(3710, 3740, set(self.config['nodes'])), [])

    def test_all_down_still_alerts_at_sixty_seconds_without_node_duplicates(self):
        self.assertEqual(self.interval(100, 150, set()), [])
        self.assertEqual([e['kind'] for e in self.sample(160, set())], ['BOTH_DOWN'])
        self.assertEqual(self.interval(170, 86610, set()), [])
        self.assertEqual([e['kind'] for e in self.interval(86620, 86630, set(self.config['nodes']))],
                         ['SERVICE_RECOVERED'])

    def test_aggregate_partial_recovery_does_not_report_short_node_recovery(self):
        self.interval(100, 160, set())
        self.assertEqual([e['kind'] for e in self.interval(170, 180, {'19008'})], ['SERVICE_RECOVERED'])
        self.assertEqual(self.interval(190, 220, set(self.config['nodes'])), [])

    def test_restart_retains_day_window_and_dedup(self):
        self.interval(100, 43300, {'19002', '19008'})
        restarted = Tracker(self.config, copy.deepcopy(self.tracker.state))
        self.assertEqual(self.interval(43310, 86490, {'19002', '19008'}, restarted), [])
        self.assertEqual([e['kind'] for e in self.sample(86500, {'19002', '19008'}, restarted)], ['NODE_DOWN'])
        restarted = Tracker(self.config, copy.deepcopy(restarted.state))
        self.assertEqual(self.interval(86510, 86610, {'19002', '19008'}, restarted), [])

    def test_gap_restarts_continuous_day_window(self):
        self.interval(100, 86490, {'19002', '19008'})
        self.assertEqual(self.sample(86600, {'19002', '19008'}), [])
        self.assertEqual(self.tracker.state['nodes']['19001']['bad_since'], 86600)

    def test_omitted_node_threshold_defaults_to_day(self):
        config = {k: v for k, v in self.config.items() if k != 'node_down_seconds'}
        tracker = Tracker(config)
        self.assertEqual(self.interval(100, 170, {'19002', '19008'}, tracker), [])
        self.assertEqual(tracker.state['notification_policy']['node_down_seconds'], 86400)

    def test_policy_upgrade_expires_only_unsent_node_events_and_preserves_history(self):
        old = Tracker({**self.config, 'node_down_seconds': 60})
        self.interval(100, 160, {'19002', '19008'}, old)
        pending = old.state['outbox'][0]
        recovery = old.enqueue('NODE_RECOVERED', 'old recovery', 170)
        delivered = old.enqueue('NODE_DOWN', 'historical', 80)
        delivered['status'] = 'delivered'
        uncertain = old.enqueue('NODE_DOWN', 'uncertain', 90)
        uncertain['status'] = 'uncertain'
        critical = old.enqueue('BOTH_DOWN', 'critical', 100)
        old.state['message_cursor'] = 123
        old.state['both']['alerted'] = True
        # Old deployed states predate the policy metadata.
        old.state.pop('notification_policy')
        before_ids = [e['id'] for e in old.state['outbox']]
        upgraded = Tracker(self.config, old.state)
        self.assertEqual(pending['status'], 'expired')
        self.assertEqual(recovery['status'], 'expired')
        self.assertEqual(delivered['status'], 'delivered')
        self.assertEqual(uncertain['status'], 'uncertain')
        self.assertEqual(critical['status'], 'pending')
        self.assertEqual(upgraded.state['message_cursor'], 123)
        self.assertTrue(upgraded.state['both']['alerted'])
        self.assertFalse(upgraded.state['nodes']['19001']['alerted'])
        self.assertEqual(upgraded.state['nodes']['19001']['bad_since'], 100)
        self.assertEqual([e['id'] for e in upgraded.state['outbox']], before_ids)
        backend = Mock()
        dispatch(upgraded, lambda: None, backend, 180)
        self.assertEqual(backend.send.call_count, 1)
        self.assertIn('critical', backend.send.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
