import copy
import json
import unittest
from unittest.mock import patch

from deploy_recovery.watchdog import advance, check, inspect, npu_idle


def snapshot(util=0, busy_card=None, missing_process_card=None, alarm_card=None):
    rows = ['| NPU ID | Name | Health | Power |']
    for i in range(8):
        rows += [f'| {i} | Ascend950DT | {"Alarm" if i == alarm_card else "OK"} | 1 |',
                 f'| | | NA | {util} 0 / 0 0 / 98304 |']
    rows.append('| NPU ID | Process id | Process name | Process memory |')
    for i in range(8):
        if i == missing_process_card:
            continue
        rows.append(f'| {i} | 42 | example | 1 |' if i == busy_card else f'| No running processes found in NPU {i} |')
    return '\n'.join(rows)


class RecoveryTests(unittest.TestCase):
    def test_all_cards_and_process_sections_are_required(self):
        self.assertTrue(npu_idle(snapshot()))
        self.assertFalse(npu_idle(snapshot(util=1)))
        self.assertFalse(npu_idle(snapshot(busy_card=7)))
        for value in [snapshot(missing_process_card=7), snapshot().replace('Ascend950DT', 'Other'),
                      snapshot().replace('| 7 | Ascend950DT', '| 6 | Ascend950DT'), snapshot().replace('NA | 0 ', 'NA | nan ')]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    npu_idle(value)

    def test_alarm_does_not_block_complete_idle_snapshot(self):
        self.assertTrue(npu_idle(snapshot(alarm_card=5)))
        self.assertTrue(npu_idle(snapshot().replace('| OK |', '| Alarm |')))

    def test_alarm_does_not_hide_occupancy_or_incomplete_snapshot(self):
        self.assertFalse(npu_idle(snapshot(alarm_card=5, busy_card=5)))
        self.assertFalse(npu_idle(snapshot(alarm_card=5, busy_card=0)))
        self.assertFalse(npu_idle(snapshot(alarm_card=5, util=1)))
        with self.assertRaises(ValueError):
            npu_idle(snapshot(alarm_card=5, missing_process_card=7))

    def test_other_health_states_still_block_idle_admission(self):
        for health in ['Unknown', 'Critical', 'Offline', '']:
            with self.subTest(health=health):
                with self.assertRaises(ValueError):
                    npu_idle(snapshot(alarm_card=5).replace('| Alarm |', f'| {health} |'))

    def test_at_least_one_hour_and_seven_observations(self):
        state = {}
        for i in range(6):
            state, start = advance(state, i * 600, 'boot', 'IDLE')
            self.assertFalse(start)
        state, start = advance(state, 3599, 'boot', 'IDLE')
        self.assertFalse(start)
        state, start = advance(state, 3600, 'boot', 'IDLE')
        self.assertTrue(start)

    def test_duplicate_process_header_cannot_erase_a_busy_process(self):
        value = snapshot(busy_card=0) + '\n| NPU ID | Process id | Process name |'
        with self.assertRaises(ValueError):
            npu_idle(value)

    def test_busy_unknown_reboot_and_missing_checks_reset_window(self):
        for observation, boot, timestamp in [('BUSY', 'boot', 3600), ('UNKNOWN', 'boot', 3600),
                                             ('IDLE', 'next', 3600), ('IDLE', 'boot', 4600), ('IDLE', 'boot', 2000)]:
            previous = {'version': 1, 'boot_id': 'boot', 'last_check': 3000, 'idle_since': 0, 'idle_checks': 6}
            state, start = advance(previous, timestamp, boot, observation)
            self.assertFalse(start)
            self.assertLessEqual(state['idle_checks'], 1)

    def test_probe_duration_does_not_count_toward_idle_hour(self):
        starts = []
        state = check({}, {}, 0, 'b', observe=lambda c: 'IDLE', start=starts.append,
                      persist=lambda s: None, clock=lambda: 60)
        self.assertEqual(state['idle_since'], 60)
        state.update(last_check=3060, idle_checks=6)
        state = check({}, state, 3600, 'b', observe=lambda c: 'IDLE', start=starts.append,
                      persist=lambda s: None, clock=lambda: 3600)
        self.assertEqual(starts, [])
        self.assertEqual(state['state'], 'WAITING_IDLE')

    def test_running_and_probe_errors_never_start(self):
        for observation in ['RUNNING', 'BUSY', 'UNKNOWN', ValueError()]:
            def observe(_):
                if isinstance(observation, Exception):
                    raise observation
                return observation
            with patch('deploy_recovery.watchdog.command') as command:
                state = check({}, {}, 4000, 'b', observe=observe, start=command, persist=lambda s: None)
                command.assert_not_called()
                self.assertIsNone(state['idle_since'])

    def test_second_probe_and_persist_before_start(self):
        previous = {'version': 1, 'boot_id': 'b', 'last_check': 3000, 'idle_since': 0, 'idle_checks': 6}
        for second in ['BUSY', 'RUNNING', 'UNKNOWN']:
            observations = iter(['IDLE', second]); starts = []
            check({}, previous, 3600, 'b', observe=lambda c: next(observations), start=starts.append, persist=lambda s: None)
            self.assertEqual(starts, [])
        events = []
        state = check({}, previous, 3600, 'b', observe=lambda c: 'IDLE',
                      start=lambda c: events.append('start'), persist=lambda s: events.append(copy.deepcopy(s)))
        self.assertEqual(events[1], 'start')
        self.assertIsNone(events[0]['idle_since'])
        self.assertEqual(events[0]['idle_checks'], 0)
        self.assertEqual(state['state'], 'START_REQUESTED')

    def test_storage_failure_blocks_start_and_start_failure_resets(self):
        previous = {'version': 1, 'boot_id': 'b', 'last_check': 3000, 'idle_since': 0, 'idle_checks': 6}
        with patch('deploy_recovery.watchdog.command') as start:
            with self.assertRaises(OSError):
                check({}, previous, 3600, 'b', observe=lambda c: 'IDLE', start=start,
                      persist=lambda s: (_ for _ in ()).throw(OSError()))
            start.assert_not_called()
        state = check({}, previous, 3600, 'b', observe=lambda c: 'IDLE',
                      start=lambda c: (_ for _ in ()).throw(RuntimeError()), persist=lambda s: None)
        self.assertEqual(state['state'], 'START_FAILED')
        state, start = advance(state, 4200, 'b', 'IDLE')
        self.assertFalse(start)
        self.assertEqual(state['idle_checks'], 1)

    def test_pinned_identity_rejects_renamed_or_reconfigured_container(self):
        import hashlib
        row = {'Id': 'a' * 64, 'Name': '/example', 'Image': 'sha256:' + 'b' * 64,
               'Config': {'Cmd': ['example']}, 'HostConfig': {}, 'State': {'Running': False, 'Status': 'exited'}}
        config = {'docker': '/usr/bin/docker', 'container_id': row['Id'], 'container_name': 'example', 'image_id': row['Image'],
                  'spec_sha256': hashlib.sha256(json.dumps({'config': row['Config'], 'hostconfig': row['HostConfig']}, sort_keys=True).encode()).hexdigest()}
        for field, value in [('Name', '/rollback'), ('Id', 'c' * 64), ('Image', 'sha256:' + 'd' * 64), ('Config', {})]:
            changed = {**row, field: value}
            with patch('deploy_recovery.watchdog.command', return_value=json.dumps([changed])):
                with self.assertRaises(ValueError):
                    inspect(config)
        with patch('deploy_recovery.watchdog.command', return_value=json.dumps([row])):
            self.assertEqual(inspect(config), 'STOPPED')


if __name__ == '__main__':
    unittest.main()
