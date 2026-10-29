import copy
import configparser
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from deploy_recovery.deepinfer import npu_observation, prepare_previous, sample_window, step
from test_recovery import snapshot


class ObserverServiceTests(unittest.TestCase):
    def test_signalled_observer_is_restarted(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(Path(__file__).parents[1] / 'deploy_recovery/llmdeploy-deepinfer-recovery.service')
        self.assertEqual(unit['Service']['Restart'], 'always')
        self.assertEqual(unit['Service']['RestartSec'], '60')
        self.assertIn('-m deploy_recovery.deepinfer', unit['Service']['ExecStart'])


class IdleWindowTests(unittest.TestCase):
    def collect(self, failures=(), busy=(), end=120):
        state = {}
        ready = False
        for i in range(end + 1):
            obs = 'BUSY' if i in busy else 'UNKNOWN' if i in failures else 'IDLE'
            state, ready = sample_window(state, i * 60, obs)
        return state, ready

    def test_recovery_waits_for_two_full_hours(self):
        state = {}
        for i in range(121):
            state, ready = sample_window(state, i * 60, 'IDLE')
            self.assertEqual(ready, i == 120, f'minute {i}')

    def test_two_hours_required_and_failure_budget(self):
        self.assertFalse(self.collect(end=119)[1])
        self.assertTrue(self.collect()[1])
        tolerated, ready = self.collect(failures=range(10, 22))
        self.assertTrue(ready)
        self.assertAlmostEqual(tolerated['failure_ratio'], 12 / 121)
        rejected, ready = self.collect(failures=range(10, 23))
        self.assertFalse(ready)
        self.assertGreater(rejected['failure_ratio'], 0.1)

    def test_unknown_retains_window_busy_resets_it(self):
        state, _ = self.collect(end=20)
        unknown, ready = sample_window(state, 1260, 'UNKNOWN')
        self.assertEqual(unknown['idle_since'], 0)
        self.assertFalse(ready)
        busy, ready = sample_window(state, 1260, 'BUSY')
        self.assertIsNone(busy['idle_since'])
        self.assertFalse(ready)
        self.assertFalse(self.collect(busy=[30])[1])

    def test_last_sample_must_be_successful(self):
        self.assertFalse(self.collect(failures=[120])[1])

    def test_missed_minutes_consume_budget(self):
        state = {'samples': [{'time': 0, 'value': 'IDLE'}], 'idle_since': 0}
        state, ready = sample_window(state, 7200, 'IDLE')
        self.assertFalse(ready)
        self.assertEqual(sum(s['value'] == 'UNKNOWN' for s in state['samples']), 119)

    def test_failure_ratio_uses_recent_window(self):
        state, _ = self.collect(failures=range(10, 30))
        for i in range(121, 151):
            state, ready = sample_window(state, i * 60, 'IDLE')
        self.assertTrue(ready)
        self.assertEqual(state['failure_ratio'], 0)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.container = {'Running': True, 'StartedAt': 'start', 'FinishedAt': '', 'Status': 'running', 'ExitCode': 0}
        self.changes = []
        self.saved, self.starts = [], []

    def check(self, previous, *, observe=lambda c: 'IDLE', inspect=None, events=None):
        return step({'container_id': 'fixed'}, previous, inspect=inspect or (lambda c: copy.deepcopy(self.container)),
                    events=events or (lambda c, a, b: self.changes), sample=observe,
                    start=self.starts.append, persist=lambda s: self.saved.append(copy.deepcopy(s)),
                    clock=lambda: self.now, wall=lambda: 100000 + self.now)

    def fail(self):
        self.container.update(Running=False, Status='exited', ExitCode=137, FinishedAt='failure')

    def window(self, state):
        for i in range(1, 123):
            self.now = i * 60
            state = self.check(state)
        return state

    def test_abnormal_death_waits_two_hours_and_persists_before_start(self):
        state = self.check({})
        self.fail()
        for i in range(1, 121):
            self.now = i * 60
            state = self.check(state)
        self.assertFalse(self.starts)
        self.now = 7260
        state = self.check(state)
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(self.saved[-2]['state'], 'START_PENDING')
        self.assertEqual(state['state'], 'START_REQUESTED')

    def test_alarm_idle_samples_allow_recovery_only_after_full_window(self):
        state = self.check({})
        self.fail()
        def observe(_):
            return npu_observation({'npu_smi': '/usr/bin/npu-smi'})
        with patch('deploy_recovery.deepinfer.command', return_value=snapshot(alarm_card=5)):
            for i in range(1, 121):
                self.now = i * 60
                state = self.check(state, observe=observe)
            self.assertEqual(state['observation'], 'IDLE')
            self.assertFalse(self.starts)
            self.now = 7260
            state = self.check(state, observe=observe)
        self.assertEqual(state['state'], 'START_REQUESTED')
        self.assertEqual(len(self.starts), 1)

    def test_alarm_does_not_turn_busy_or_unknown_samples_into_idle(self):
        for payload, expected in [
            (snapshot(alarm_card=5, busy_card=5), 'BUSY'),
            (snapshot(alarm_card=5, util=1), 'BUSY'),
            (snapshot(alarm_card=5, missing_process_card=7), 'UNKNOWN'),
            (snapshot(alarm_card=5).replace('| Alarm |', '| Unknown |'), 'UNKNOWN'),
        ]:
            with self.subTest(expected=expected):
                with patch('deploy_recovery.deepinfer.command', return_value=payload):
                    self.assertEqual(npu_observation({'npu_smi': '/usr/bin/npu-smi'}), expected)

    def test_manual_stop_kill_or_unarmed_stopped_container_never_starts(self):
        for action in ['stop', 'kill', 'destroy']:
            with self.subTest(action=action):
                self.setUp()
                state = self.check({}); self.fail()
                self.changes = [{'action': action}]
                state = self.window(state)
                self.assertEqual(state['state'], 'MANUAL_HOLD')
                self.assertFalse(self.starts)
        self.setUp(); self.fail()
        self.assertEqual(self.window({})['state'], 'MANUAL_HOLD')

    def test_unknown_control_and_observer_gap_disarm(self):
        state = self.check({}); self.fail()
        self.now = 180
        self.assertEqual(self.check(state)['state'], 'MANUAL_HOLD')
        def broken(_):
            raise ValueError('TARGET_CHANGED')
        state = self.check(state, inspect=broken)
        self.assertEqual(state['state'], 'CONTROL_UNKNOWN')
        self.assertFalse(state['armed'])

    def test_running_target_never_samples_or_starts(self):
        def unexpected(_):
            self.fail('running target sampled')
        state = self.check({}, observe=unexpected)
        self.assertEqual(state['state'], 'RUNNING')
        self.assertFalse(self.starts)

    def test_final_probe_busy_unknown_and_manual_race_block_start(self):
        for last in ['BUSY', 'UNKNOWN']:
            self.setUp(); state = self.check({}); self.fail()
            for i in range(1, 121):
                self.now = i * 60; state = self.check(state)
            self.now = 7260
            values = iter(['IDLE', last])
            out = self.check(state, observe=lambda c: next(values))
            self.assertFalse(self.starts)
            if last == 'UNKNOWN':
                self.assertEqual(out['samples'][-1]['value'], 'UNKNOWN')
                self.assertEqual(out['idle_since'], 60)
        self.setUp(); state = self.check({}); self.fail()
        for i in range(1, 121):
            self.now = i * 60; state = self.check(state)
        self.now = 7260
        events = iter([[], [{'action': 'stop'}]])
        self.assertEqual(self.check(state, events=lambda *args: next(events))['state'], 'MANUAL_HOLD')
        self.assertFalse(self.starts)

    def test_operator_action_during_slow_final_probe_is_preserved(self):
        state = self.check({}); self.fail()
        for i in range(1, 121):
            self.now = i * 60; state = self.check(state)
        self.now = 7260
        calls = []
        def probe(_):
            calls.append(1)
            if len(calls) == 2:
                self.container.update(StartedAt='operator-start', FinishedAt='operator-stop')
                self.changes = [{'action': 'start'}, {'action': 'kill'}, {'action': 'stop'}]
            return 'IDLE'
        out = self.check(state, observe=probe)
        self.assertEqual(out['state'], 'MANUAL_HOLD')
        self.assertFalse(self.starts)


class BootRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = {'container_id': 'fixed'}
        self.boot_epoch = 2_000_000_000
        self.now = 0
        self.prior = {'version': 1, 'target_id': 'fixed', 'boot_id': 'old',
                      'state': 'RUNNING', 'armed': True, 'manual_hold': False,
                      'started': 'old-start', 'samples': [{'time': 123, 'value': 'IDLE'}]}
        self.container = {'Running': False, 'Status': 'exited', 'ExitCode': 0,
                          'StartedAt': 'old-start', 'FinishedAt': self.finished(-120)}
        self.changes = []
        self.starts, self.saved = [], []

    def finished(self, offset):
        return datetime.fromtimestamp(self.boot_epoch + offset, timezone.utc).isoformat()

    def prepare(self, prior=None, boot_id='new'):
        return prepare_previous(self.prior if prior is None else prior,
                                self.config, boot_id, self.boot_epoch)

    def check(self, previous, *, inspect=None, events=None, sample=lambda c: 'IDLE'):
        return step(self.config, previous, inspect=inspect or (lambda c: copy.deepcopy(self.container)),
                    events=events or (lambda c, a, b: self.changes), sample=sample,
                    start=self.starts.append, persist=lambda s: self.saved.append(copy.deepcopy(s)),
                    clock=lambda: self.now, wall=lambda: self.boot_epoch + 10 + self.now)

    def test_rebooted_clean_exit_requires_fresh_two_hour_window(self):
        state = self.prepare()
        self.assertTrue(state['boot_recovery_pending'])
        self.assertEqual(state['samples'], [])
        for i in range(121):
            self.now = i * 60
            state = self.check(state)
            self.assertEqual(len(self.starts), int(i == 120))
        self.assertEqual(state['state'], 'START_REQUESTED')
        self.assertEqual(self.saved[-2]['state'], 'START_PENDING')

    def test_boot_preflight_reads_control_events_from_current_boot(self):
        ranges = []
        def events(_, since, until):
            ranges.append((since, until))
            return []
        state = self.check(self.prepare(), events=events)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(ranges[0][0], self.boot_epoch)

    def test_legacy_unarmed_post_reboot_state_can_start_a_fresh_window(self):
        prior = {**self.prior, 'boot_id': 'new', 'state': 'MANUAL_HOLD', 'armed': False}
        state = self.check(self.prepare(prior))
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertTrue(state['armed'])
        self.assertTrue(state['boot_recovery'])
        self.assertEqual(state['idle_since'], 0)
        self.assertFalse(self.starts)

    def test_host_reboot_starts_new_window_after_previous_manual_hold(self):
        state = self.check(self.prepare({**self.prior, 'manual_hold': True}))
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertFalse(state['manual_hold'])
        self.assertTrue(state['armed'])
        self.assertTrue(state['boot_recovery'])
        self.assertFalse(self.starts)

    def test_known_manual_hold_is_preserved_within_current_boot(self):
        prior = {**self.prior, 'boot_id': 'new', 'state': 'MANUAL_HOLD',
                 'armed': False, 'manual_hold': True}
        state = self.check(self.prepare(prior))
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertTrue(state['manual_hold'])
        self.assertFalse(self.starts)

    def test_postboot_operator_action_or_newer_exit_prevents_claim(self):
        for action in ('stop', 'kill', 'destroy', 'start'):
            with self.subTest(action=action):
                self.changes = [{'action': action}]
                state = self.check(self.prepare())
                self.assertEqual(state['state'], 'MANUAL_HOLD')
                self.assertFalse(self.starts)
        self.changes = []
        self.container['FinishedAt'] = self.finished(1)
        self.assertEqual(self.check(self.prepare())['state'], 'MANUAL_HOLD')
        self.assertFalse(self.starts)

    def test_changed_finish_time_during_window_blocks_recovery(self):
        state = self.check(self.prepare())
        self.now = 60
        self.container['FinishedAt'] = self.finished(1)
        state = self.check(state)
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertTrue(state['manual_hold'])
        self.assertFalse(self.starts)

    def test_missing_mismatched_or_unknown_prior_intent_is_not_claimed(self):
        cases = ({}, {**self.prior, 'target_id': 'other'},
                 {**self.prior, 'manual_hold': None},
                 {**self.prior, 'policy_version': 999})
        for prior in cases:
            with self.subTest(prior=prior):
                state = self.check(self.prepare(prior))
                self.assertEqual(state['state'], 'MANUAL_HOLD')
                self.assertFalse(self.starts)

    def test_new_policy_hold_is_not_rearmed_on_same_boot_restart(self):
        prior = {**self.prior, 'boot_id': 'new', 'state': 'MANUAL_HOLD',
                 'policy_version': 2, 'armed': False}
        state = self.check(self.prepare(prior))
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertFalse(state.get('boot_recovery_pending', False))

    def test_transient_inspection_failure_retries_boot_preflight(self):
        prior = self.prepare()
        def unavailable(_):
            raise RuntimeError('temporarily unavailable')
        state = self.check(prior, inspect=unavailable)
        self.assertEqual(state['state'], 'CONTROL_UNKNOWN')
        self.assertTrue(state['boot_recovery_pending'])
        self.now = 180
        state = self.check(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertTrue(state['armed'])
        self.assertFalse(self.starts)

    def test_running_container_is_not_sampled_or_restarted(self):
        self.container.update(Running=True, Status='running', StartedAt='new-start')
        def unexpected(_):
            raise AssertionError('running target must not be sampled')
        state = self.check(self.prepare(), sample=unexpected)
        self.assertEqual(state['state'], 'RUNNING')
        self.assertTrue(state['armed'])
        self.assertFalse(state['boot_recovery_pending'])
        self.assertFalse(self.starts)


if __name__ == '__main__':
    unittest.main()
