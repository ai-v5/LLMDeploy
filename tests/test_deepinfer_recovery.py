import copy
import configparser
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deploy_recovery import deepinfer as recovery
from deploy_recovery.deepinfer import npu_observation, prepare_previous, sample_window, step
from test_recovery import snapshot

TRUST_FIXED = recovery.target_fingerprint({'container_id': 'fixed'})


class ObserverServiceTests(unittest.TestCase):
    def test_signalled_observer_is_restarted(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(Path(__file__).parents[1] / 'deploy_recovery/llmdeploy-deepinfer-recovery.service')
        self.assertEqual(unit['Service']['Restart'], 'always')
        self.assertEqual(unit['Service']['RestartSec'], '60')
        self.assertIn('-m deploy_recovery.deepinfer', unit['Service']['ExecStart'])
        self.assertNotIn('docker.service', unit['Unit'].get('Requires', '').split())
        self.assertNotIn('docker.service', unit['Unit'].get('Wants', '').split())
        self.assertIn('docker.service', unit['Unit']['After'].split())
        self.assertIn('docker.service', unit['Unit']['PartOf'].split())
        self.assertIn('docker.service', unit['Install']['WantedBy'].split())


class AdaptiveIntervalTests(unittest.TestCase):
    def test_interval_follows_observation_and_keeps_running_checks(self):
        cases = [
            ({'state': 'RUNNING'}, 60),
            ({'state': 'WAITING_IDLE', 'observation': 'IDLE'}, 120),
            ({'state': 'BUSY', 'observation': 'BUSY'}, 1200),
            ({'state': 'WAITING_IDLE', 'observation': 'UNKNOWN'}, 1200),
            ({'state': 'CONTROL_UNKNOWN'}, 1200),
            ({'state': 'MANUAL_HOLD'}, 60),
        ]
        for state, expected in cases:
            with self.subTest(state=state):
                self.assertEqual(recovery.next_interval(state), expected)

    def test_two_minute_samples_need_two_full_hours(self):
        state = {}
        for i in range(61):
            state, ready = sample_window(state, i * 120, 'IDLE')
            self.assertEqual(ready, i == 60, f'sample {i}')

    def test_missing_two_minute_observation_restarts_window(self):
        state, _ = sample_window({}, 0, 'IDLE')
        state, _ = sample_window(state, 120, 'IDLE')
        state, ready = sample_window(state, 360, 'IDLE')
        self.assertFalse(ready)
        self.assertEqual(state['idle_since'], 360)
        self.assertEqual(len(state['samples']), 1)


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

    def test_unknown_observation_restarts_idle_window(self):
        state, _ = self.collect(end=20)
        state, ready = sample_window(state, 1260, 'UNKNOWN')
        self.assertFalse(ready)
        self.assertIsNone(state['idle_since'])
        self.assertEqual(state['samples'], [])

    def test_two_hours_required_without_unknown_samples(self):
        self.assertFalse(self.collect(end=119)[1])
        self.assertTrue(self.collect()[1])
        rejected, ready = self.collect(failures=[10])
        self.assertFalse(ready)
        self.assertEqual(rejected['idle_since'], 11 * 60)

    def test_unknown_and_busy_reset_window(self):
        state, _ = self.collect(end=20)
        unknown, ready = sample_window(state, 1260, 'UNKNOWN')
        self.assertIsNone(unknown['idle_since'])
        self.assertFalse(ready)
        busy, ready = sample_window(state, 1260, 'BUSY')
        self.assertIsNone(busy['idle_since'])
        self.assertFalse(ready)
        self.assertFalse(self.collect(busy=[30])[1])

    def test_last_sample_must_be_successful(self):
        self.assertFalse(self.collect(failures=[120])[1])

    def test_missed_minutes_restart_window(self):
        state = {'samples': [{'time': 0, 'value': 'IDLE'}], 'idle_since': 0}
        state, ready = sample_window(state, 7200, 'IDLE')
        self.assertFalse(ready)
        self.assertEqual(state['samples'], [{'time': 7200, 'value': 'IDLE'}])
        self.assertEqual(state['idle_since'], 7200)

    def test_fresh_two_hour_window_after_unknown(self):
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
        def run():
            return step({'container_id': 'fixed'}, previous, inspect=inspect or (lambda c: copy.deepcopy(self.container)),
                        events=events or (lambda c, a, b: self.changes), sample=observe,
                        start=self.starts.append, persist=lambda s: self.saved.append(copy.deepcopy(s)),
                        clock=lambda: self.now, wall=lambda: 100000 + self.now)
        if isinstance(recovery.control_history_complete, Mock):
            return run()
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            return run()

    def stop_target(self):
        self.container.update(Running=False, Status='exited', ExitCode=137, FinishedAt='failure')

    def test_recent_idle_window_survives_restart_with_verified_history(self):
        previous = {'version': 1, 'target_id': 'fixed', 'boot_id': 'same', 'policy_version': 4,
                    'trusted_fingerprint': TRUST_FIXED,
                    'state': 'WAITING_IDLE', 'observation': 'IDLE', 'armed': True, 'manual_hold': False,
                    'started': 'start', 'finished': 'failure', 'event_cursor': 100120, 'last_check': 120,
                    'idle_since': 0, 'samples': [{'time': 0, 'value': 'IDLE'},
                                                  {'time': 120, 'value': 'IDLE'}]}
        resumed = prepare_previous(previous, {'container_id': 'fixed'}, 'same', 99900)
        self.assertEqual(resumed['samples'], previous['samples'])
        self.stop_target(); self.now = 180
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True) as history:
            state = self.check(resumed)
        history.assert_called_once()
        self.assertEqual(state['idle_since'], 0)
        self.assertEqual(len(state['samples']), 3)
        self.assertFalse(self.starts)

    def test_restart_discards_window_when_control_history_is_incomplete(self):
        previous = {'version': 1, 'target_id': 'fixed', 'boot_id': 'same', 'policy_version': 4,
                    'trusted_fingerprint': TRUST_FIXED,
                    'state': 'WAITING_IDLE', 'observation': 'IDLE', 'armed': True, 'manual_hold': False,
                    'started': 'start', 'event_cursor': 100120, 'last_check': 120,
                    'idle_since': 0, 'samples': [{'time': 0, 'value': 'IDLE'},
                                                  {'time': 120, 'value': 'IDLE'}]}
        resumed = prepare_previous(previous, {'container_id': 'fixed'}, 'same', 99900)
        self.stop_target(); self.now = 180
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=False) as history:
            state = self.check(resumed)
        history.assert_called_once()
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(state['idle_since'], 180)
        self.assertFalse(self.starts)

    def test_missing_event_cursor_cannot_reuse_an_idle_window_after_restart(self):
        previous = {'version': 1, 'target_id': 'fixed', 'boot_id': 'same', 'policy_version': 4,
                    'trusted_fingerprint': TRUST_FIXED,
                    'state': 'WAITING_IDLE', 'observation': 'IDLE', 'armed': True, 'manual_hold': False,
                    'started': 'start', 'finished': 'failure', 'last_check': 7200, 'idle_since': 0,
                    'samples': [{'time': i * 120, 'value': 'IDLE'} for i in range(61)]}
        resumed = prepare_previous(previous, {'container_id': 'fixed'}, 'same', 99900)
        self.stop_target()
        self.now = 7320
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            state = self.check(resumed)
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertFalse(state['armed'])
        self.assertEqual(state['samples'], [])
        self.assertFalse(self.starts)
        self.now += 1200
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            state = self.check(state)
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertFalse(self.starts)

    def test_corrupt_early_idle_since_cannot_shorten_the_sampled_window(self):
        previous = {'version': 1, 'target_id': 'fixed', 'boot_id': 'same', 'policy_version': 4,
                    'trusted_fingerprint': TRUST_FIXED,
                    'state': 'WAITING_IDLE', 'observation': 'IDLE', 'armed': True, 'manual_hold': False,
                    'started': 'start', 'finished': 'failure', 'event_cursor': 107080,
                    'last_check': 7080, 'idle_since': 0,
                    'samples': [{'time': i * 120, 'value': 'IDLE'} for i in range(1, 60)]}
        resumed = prepare_previous(previous, {'container_id': 'fixed'}, 'same', 99900)
        self.stop_target()
        self.now = 7200
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            state = self.check(resumed)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertFalse(self.starts)
        self.now = 7320
        state = self.check(state)
        self.assertEqual(state['state'], 'START_REQUESTED')
        self.assertEqual(len(self.starts), 1)

    def test_incomplete_history_restarts_the_full_window_without_disarming(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state, observe=lambda _: 'BUSY')
        self.now = 1260
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=False):
            state = self.check(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertTrue(state['armed'])
        self.assertEqual(state['idle_since'], 1260)
        for i in range(1, 61):
            self.now = 1260 + i * 120
            with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
                state = self.check(state)
            self.assertEqual(len(self.starts), int(i == 60))

    def test_history_is_checked_at_the_exact_two_minute_interval(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state)
        self.now = 180
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=False) as history:
            state = self.check(state)
        history.assert_called_once()
        self.assertEqual(state['idle_since'], 180)
        self.assertEqual(len(state['samples']), 1)
        self.assertFalse(self.starts)

    def test_history_rebase_requires_an_unchanged_stopped_target(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state, observe=lambda _: 'BUSY')
        self.now = 1260
        changed = dict(self.container, StartedAt='other-run', FinishedAt='other-exit')
        targets = iter([copy.deepcopy(self.container), changed])
        def unexpected(_):
            self.fail('changed target must not be sampled')
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=False):
            out = self.check(state, inspect=lambda _: next(targets), observe=unexpected)
        self.assertEqual(out['state'], 'CONTROL_UNKNOWN')
        self.assertEqual(out['samples'], [])
        self.assertFalse(self.starts)

    def test_changed_config_never_inherits_stopped_target_trust(self):
        old = {'container_id': 'fixed', 'container_name': 'one', 'image_id': 'image',
               'spec_sha256': 'old'}
        previous = {'version': 1, 'target_id': 'fixed', 'boot_id': 'same', 'policy_version': 4,
                    'trusted_fingerprint': recovery.target_fingerprint(old), 'state': 'WAITING_IDLE',
                    'armed': True, 'manual_hold': False, 'started': 'start', 'finished': 'failure',
                    'event_cursor': 100000, 'last_check': 0, 'samples': [], 'idle_since': None}
        self.stop_target()
        for key, new_value in [('container_name', 'two'), ('image_id', 'other'), ('spec_sha256', 'new')]:
            with self.subTest(key=key):
                config = {**old, key: new_value}
                resumed = prepare_previous(previous, config, 'same', 99900)
                self.assertFalse(resumed['armed'])
                self.assertIsNone(resumed['trusted_fingerprint'])
                state = step(config, resumed, inspect=lambda _: copy.deepcopy(self.container),
                             events=lambda *_: [], sample=lambda _: self.fail('untrusted target sampled'),
                             start=self.starts.append, persist=lambda _: None, clock=lambda: 60, wall=lambda: 100060)
                self.assertEqual(state['state'], 'MANUAL_HOLD')
                self.assertFalse(self.starts)
    def test_running_pinned_target_can_observe_from_a_new_cursor(self):
        previous = {'armed': True, 'manual_hold': False, 'started': 'start'}
        state = self.check(previous)
        self.assertEqual(state['state'], 'RUNNING')
        self.assertEqual(state['event_cursor'], 100000)
        self.assertTrue(state['armed'])
        self.assertFalse(self.starts)

    def test_restart_after_missing_an_idle_check_resets_the_window(self):
        previous = {'version':1, 'target_id':'fixed', 'boot_id':'same', 'policy_version':4,
                    'trusted_fingerprint': TRUST_FIXED,
                    'state':'WAITING_IDLE', 'observation':'IDLE', 'armed':True, 'manual_hold':False,
                    'started':'start', 'event_cursor':100120, 'last_check':120, 'idle_since':0,
                    'samples':[{'time':0,'value':'IDLE'},{'time':120,'value':'IDLE'}]}
        resumed = prepare_previous(previous, {'container_id':'fixed'}, 'same', 99900)
        self.stop_target(); self.now = 361
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            state = self.check(resumed)
        self.assertEqual(state['idle_since'],361)
        self.assertEqual(state['samples'],[{'time':361,'value':'IDLE'}])
        self.assertFalse(self.starts)

    def test_stopped_busy_observer_restart_keeps_the_next_check_deadline(self):
        previous = {'version': 1, 'target_id': 'fixed', 'boot_id': 'same', 'policy_version': 2,
                    'state': 'BUSY', 'observation': 'BUSY', 'armed': True, 'manual_hold': False,
                    'last_check': 100}
        for now, expected in [(400, 900), (700, 600), (1300, 0)]:
            self.assertEqual(recovery.restart_delay(previous, {'container_id': 'fixed'}, 'same', now), expected)
        self.assertEqual(recovery.restart_delay(previous, {'container_id': 'fixed'}, 'new-boot', 400), 0)

    def test_sparse_but_valid_idle_samples_eventually_complete_the_window(self):
        state = {}
        for i in range(60):
            state, ready = sample_window(state, i * 180, 'IDLE')
            self.assertEqual(ready, i == 59)
        self.assertGreaterEqual(59 * 180, recovery.WINDOW)

    def test_repeated_verified_observer_restarts_eventually_allow_idle_recovery(self):
        state = self.check({})
        state.update(boot_id='same', policy_version=4, manual_hold=False)
        self.stop_target()
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            for i in range(60):
                self.now = 60 + i * 180
                if i:
                    state = prepare_previous(state, {'container_id': 'fixed'}, 'same', 99900)
                state = self.check(state)
                self.assertEqual(len(self.starts), int(i == 59))
        self.assertEqual(state['state'], 'START_REQUESTED')

    def test_operator_stop_during_observer_restart_restarts_idle_window(self):
        state = self.check({})
        state.update(boot_id='same', policy_version=4, manual_hold=False)
        self.stop_target(); self.now = 60
        state = self.check(state)
        resumed = prepare_previous(state, {'container_id': 'fixed'}, 'same', 99900)
        self.now = 120; self.changes = [{'action':'stop'}]
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            state = self.check(resumed)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertTrue(state['armed'])
        self.assertEqual(state['idle_since'], 120)
        self.assertFalse(self.starts)

    def test_restart_history_check_survives_transient_inspection_failure(self):
        state = self.check({})
        state.update(boot_id='same', policy_version=4, manual_hold=False)
        self.stop_target(); self.now = 60
        state = self.check(state)
        resumed = prepare_previous(state, {'container_id': 'fixed'}, 'same', 99900)
        self.now = 120
        state = self.check(resumed, inspect=lambda _: (_ for _ in ()).throw(RuntimeError('unavailable')))
        self.assertEqual(state['state'], 'CONTROL_UNKNOWN')
        self.assertTrue(state['restart_history_pending'])
        self.assertEqual(state['last_check'], 60)
        self.assertEqual(state['last_attempt'], 120)
        self.assertEqual(recovery.restart_delay(state, {'container_id':'fixed'}, 'same', 140), 1180)
        self.now = 180
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=False) as history:
            state = self.check(state)
        history.assert_called_once()
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(state['idle_since'], 180)

    def test_invalid_persisted_idle_samples_are_not_reused(self):
        previous = {'version':1, 'target_id':'fixed', 'boot_id':'same', 'policy_version':4,
                    'trusted_fingerprint': TRUST_FIXED,
                    'state':'WAITING_IDLE', 'observation':'IDLE', 'armed':True, 'manual_hold':False,
                    'last_check':120, 'idle_since':0}
        for samples in ([], [{'time':120,'value':'IDLE'},{'time':120,'value':'IDLE'}],
                        [{'time':120,'value':'BUSY'}], [{'time':float('nan'),'value':'IDLE'}]):
            with self.subTest(samples=samples):
                resumed = prepare_previous({**previous,'samples':samples}, {'container_id':'fixed'}, 'same', 99900)
                self.assertEqual(resumed['samples'], [])
                self.assertIsNone(resumed['idle_since'])

    def test_restart_keeps_a_sample_taken_after_the_check_began(self):
        previous = {'version':1, 'target_id':'fixed', 'boot_id':'same', 'policy_version':4,
                    'trusted_fingerprint': TRUST_FIXED,
                    'state':'WAITING_IDLE', 'observation':'IDLE', 'armed':True, 'manual_hold':False,
                    'event_cursor':100120, 'last_check':120, 'idle_since':2,
                    'samples':[{'time':2,'value':'IDLE'},{'time':125,'value':'IDLE'}]}
        resumed = prepare_previous(previous, {'container_id':'fixed'}, 'same', 99900)
        self.assertEqual(resumed['samples'], previous['samples'])
        self.assertEqual(resumed['idle_since'], 2)

    def test_planned_busy_interval_rechecks_without_manual_hold(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state, observe=lambda c: 'BUSY')
        self.assertEqual(state['state'], 'BUSY')
        self.now += 1200
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            state = self.check(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertTrue(state['armed'])
        self.assertEqual(len(state['samples']), 1)
        self.assertFalse(self.starts)

    def test_busy_then_two_hour_idle_window_before_start(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state, observe=lambda c: 'BUSY')
        self.now += 1200
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            state = self.check(state)
        for i in range(1, 61):
            self.now = 1260 + i * 120
            state = self.check(state)
            self.assertEqual(len(self.starts), int(i == 60))
        self.assertEqual(state['state'], 'START_REQUESTED')

    def test_stop_during_busy_wait_requires_complete_history(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state, observe=lambda c: 'BUSY')
        self.now += 1200
        self.changes = [{'action': 'stop'}]
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=False):
            state = self.check(state)
        self.assertEqual(state['state'], 'CONTROL_UNKNOWN')
        self.assertFalse(self.starts)

    def test_planned_busy_wait_restarts_after_incomplete_control_history(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state, observe=lambda c: 'BUSY')
        self.now += 1200
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=False) as history:
            state = self.check(state)
        history.assert_called_once()
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(state['idle_since'], 1260)
        self.assertFalse(self.starts)

    def test_idle_wait_with_jitter_restarts_incomplete_history(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state)
        self.now += 121
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=False) as history:
            state = self.check(state)
        history.assert_called_once()
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(state['idle_since'], 181)
        self.assertFalse(self.starts)

    def test_idle_wait_with_jitter_preserves_window_when_history_is_complete(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state)
        self.now += 121
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True) as history:
            state = self.check(state)
        history.assert_called_once()
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(state['idle_since'], 60)
        self.assertEqual(len(state['samples']), 2)
        self.assertFalse(self.starts)

    def window(self, state):
        for i in range(1, 123):
            self.now = i * 60
            state = self.check(state)
        return state

    def test_abnormal_death_waits_two_hours_and_persists_before_start(self):
        state = self.check({})
        self.stop_target()
        for i in range(1, 121):
            self.now = i * 60
            state = self.check(state)
        self.assertFalse(self.starts)
        self.now = 7260
        state = self.check(state)
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(self.saved[-2]['state'], 'START_PENDING')
        self.assertEqual(state['state'], 'START_REQUESTED')

    def test_stop_and_kill_recover_after_a_fresh_two_hour_idle_window(self):
        for action, exit_code in [('stop', 0), ('kill', 137)]:
            with self.subTest(action=action):
                self.setUp()
                state = self.check({})
                self.stop_target()
                self.container['ExitCode'] = exit_code
                self.changes = [{'action': action}]
                self.now = 60
                state = self.check(state)
                self.assertEqual(state['state'], 'WAITING_IDLE')
                self.assertTrue(state['armed'])
                self.changes = []
                for i in range(1, 60):
                    self.now = 60 + i * 120
                    with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
                        state = self.check(state)
                self.assertFalse(self.starts)
                self.now = 60 + 60 * 120
                with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
                    state = self.check(state)
                self.assertEqual(state['state'], 'START_REQUESTED')
                self.assertEqual(len(self.starts), 1)

    def test_manual_stop_busy_then_idle_starts_a_new_window(self):
        state = self.check({})
        self.stop_target()
        self.container['ExitCode'] = 0
        self.changes = [{'action': 'stop'}]
        self.now = 60
        state = self.check(state, observe=lambda _: 'BUSY')
        self.assertEqual(state['state'], 'BUSY')
        self.assertEqual(recovery.next_interval(state), 1200)
        self.assertEqual(state['samples'], [])
        self.changes = []
        self.now = 1260
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            state = self.check(state)
        self.assertEqual(state['idle_since'], 1260)
        self.assertEqual(recovery.next_interval(state), 120)
        self.assertFalse(self.starts)

    def test_old_manual_hold_requires_explicit_rearm(self):
        self.stop_target()
        previous = {'version': 1, 'target_id': 'fixed', 'boot_id': 'same', 'policy_version': 2,
                    'state': 'MANUAL_HOLD', 'armed': False, 'manual_hold': True,
                    'started': 'start', 'event_cursor': 100000, 'last_check': 0}
        resumed = prepare_previous(previous, {'container_id': 'fixed'}, 'same', 99900)
        self.now = 60
        state = self.check(resumed)
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertFalse(state['armed'])
        result = recovery.rearm_stopped({'container_id': 'fixed'}, resumed, 'same',
                                        inspect=lambda _: copy.deepcopy(self.container), events=lambda *_: [],
                                        clock=lambda: 60, wall=lambda: 100060)
        self.now = 120
        state = self.check(result)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertTrue(state['armed'])
        self.assertFalse(state['manual_hold'])
        self.assertEqual(state['idle_since'], 120)
        self.assertFalse(self.starts)

    def test_old_hold_migration_rejects_destroy_and_missing_cursor(self):
        self.stop_target()
        prior = {'version': 1, 'target_id': 'fixed', 'boot_id': 'same', 'policy_version': 2,
                 'state': 'MANUAL_HOLD', 'armed': False, 'manual_hold': True,
                 'started': 'start', 'event_cursor': 100000, 'last_check': 0}
        for change in ('destroy', 'missing_cursor'):
            with self.subTest(change=change):
                item = dict(prior)
                self.changes = [{'action': 'destroy'}] if change == 'destroy' else []
                if change == 'missing_cursor':
                    del item['event_cursor']
                with self.assertRaises(ValueError):
                    recovery.rearm_stopped({'container_id': 'fixed'}, item, 'same',
                        inspect=lambda _: copy.deepcopy(self.container), events=lambda *_: self.changes,
                        clock=lambda: 60, wall=lambda: 100060)
                self.assertFalse(self.starts)

    def test_old_idle_window_without_finish_time_is_not_reused(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state)
        del state['finished']
        self.now = 120
        state = self.check(state)
        self.assertEqual(state['idle_since'], 120)
        self.assertEqual(len(state['samples']), 1)
        self.assertFalse(self.starts)

    def test_start_stop_between_checks_resets_the_idle_window(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state)
        self.now = 120
        self.container.update(StartedAt='second-start', FinishedAt='second-stop')
        self.changes = [{'action': 'start', 'epoch': 100080}, {'action': 'stop', 'epoch': 100100}]
        state = self.check(state)
        self.assertEqual(state['idle_since'], 120)
        self.assertTrue(state['armed'])
        self.assertFalse(self.starts)

    def test_alarm_idle_samples_allow_recovery_only_after_full_window(self):
        state = self.check({})
        self.stop_target()
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

    def test_destroy_or_unarmed_stopped_container_never_starts(self):
        state = self.check({}); self.stop_target()
        self.changes = [{'action': 'destroy'}]
        state = self.window(state)
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertFalse(self.starts)
        self.setUp(); self.stop_target()
        self.assertEqual(self.window({})['state'], 'MANUAL_HOLD')

    def test_observer_gap_resets_and_unknown_target_never_starts(self):
        state = self.check({}); self.stop_target()
        self.now = 180
        self.assertEqual(self.check(state)['state'], 'WAITING_IDLE')
        def broken(_):
            raise ValueError('TARGET_CHANGED')
        state = self.check(state, inspect=broken)
        self.assertEqual(state['state'], 'CONTROL_UNKNOWN')
        self.assertTrue(state['armed'])
        self.assertFalse(self.starts)

    def test_verified_observer_gap_restarts_observation_without_disarming(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        state = self.check(state)
        self.now = 300
        with patch('deploy_recovery.deepinfer.control_history_complete', create=True, return_value=True):
            state = self.check(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertTrue(state['armed'])
        self.assertEqual(state['idle_since'], 300)
        self.assertFalse(self.starts)

    def test_transient_control_error_retains_cursor_and_restarts_the_window(self):
        state = self.check({})
        self.stop_target()
        self.now = 60
        def unavailable(_):
            raise RuntimeError('temporarily unavailable')
        state = self.check(state, inspect=unavailable)
        self.assertEqual(state['state'], 'CONTROL_UNKNOWN')
        self.assertTrue(state['armed'])
        self.assertEqual(state['event_cursor'], 100000)
        self.assertEqual(state['last_check'], 0)
        self.now = 180
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            state = self.check(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(state['idle_since'], 180)
        self.assertFalse(self.starts)

    def test_running_unrelated_container_blocks_an_idle_npu(self):
        config = {'npu_smi': '/usr/bin/npu-smi', 'docker': '/usr/bin/docker',
                  'container_id': 'a' * 64}
        with patch('deploy_recovery.deepinfer.command', return_value=snapshot()), \
             patch('deploy_recovery.deepinfer.docker_command', return_value='b' * 64 + '\n'):
            observe = getattr(recovery, 'machine_observation', npu_observation)
            self.assertEqual(observe(config), 'BUSY')

    def test_inactive_or_socket_activated_docker_is_never_contacted(self):
        for service, socket in [('inactive', 'inactive'), ('active', 'active')]:
            with self.subTest(service=service, socket=socket):
                calls = []
                def run(args, **kwargs):
                    calls.append(args)
                    if args[0] != 'systemctl':
                        self.fail('Docker CLI must not run for this service state')
                    name = args[2]
                    value = service if name == 'docker.service' else socket
                    return SimpleNamespace(returncode=0, stdout=f'LoadState=loaded\nActiveState={value}\n')
                with patch('deploy_recovery.deepinfer.subprocess.run', side_effect=run):
                    state = step({'container_id': 'fixed', 'docker': '/usr/bin/docker'}, {},
                                 persist=lambda _: None, start=self.starts.append,
                                 clock=lambda: 1, wall=lambda: 100001)
                self.assertEqual(state['state'], 'CONTROL_UNKNOWN')
                self.assertTrue(calls)
                self.assertTrue(all(args[0] == 'systemctl' for args in calls))
                self.assertFalse(self.starts)

    def test_active_docker_with_inactive_or_absent_socket_can_be_queried(self):
        for load, active in [('loaded', 'inactive'), ('not-found', 'inactive')]:
            with self.subTest(load=load, active=active):
                calls = []
                def run(args, **kwargs):
                    calls.append(args)
                    if args[0] != 'systemctl':
                        return SimpleNamespace(returncode=0, stdout='ok')
                    text = ('LoadState=loaded\nActiveState=active\n' if args[2] == 'docker.service'
                            else f'LoadState={load}\nActiveState={active}\n')
                    return SimpleNamespace(returncode=0, stdout=text)
                with patch('deploy_recovery.deepinfer.subprocess.run', side_effect=run):
                    out = recovery.docker_command({'docker': '/usr/bin/docker'}, ['ps'])
                self.assertEqual(out, 'ok')
                self.assertEqual(calls[-1], ['/usr/bin/docker', 'ps'])

    def test_only_pinned_infrastructure_is_exempt(self):
        config = {'npu_smi': '/usr/bin/npu-smi', 'docker': '/usr/bin/docker',
                  'container_id': 'a' * 64,
                  'non_workload_containers': [{'container_id': 'b' * 64, 'image_id': 'sha256:' + 'c' * 64}]}
        def docker_command(_, args, *unused, **kwargs):
            if args[0] == 'ps':
                return 'b' * 64 + '\n'
            return 'sha256:' + 'c' * 64
        with patch('deploy_recovery.deepinfer.command', return_value=snapshot()), \
             patch('deploy_recovery.deepinfer.docker_command', side_effect=docker_command):
            self.assertEqual(recovery.machine_observation(config), 'IDLE')
            config['non_workload_containers'][0]['image_id'] = 'sha256:' + 'd' * 64
            self.assertEqual(recovery.machine_observation(config), 'BUSY')
        with patch('deploy_recovery.deepinfer.command', return_value=snapshot()), \
             patch('deploy_recovery.deepinfer.docker_command', side_effect=RuntimeError('docker unavailable')):
            self.assertEqual(recovery.machine_observation(config), 'UNKNOWN')

    def test_infrastructure_config_rejects_unpinned_or_duplicate_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'docker': str(root / 'docker'), 'npu_smi': str(root / 'npu-smi'),
                      'state_file': str(root / 'state.json'), 'container_id': 'a' * 64,
                      'container_name': 'model', 'image_id': 'sha256:' + 'b' * 64,
                      'spec_sha256': 'c' * 64}
            path = root / 'config.json'
            infrastructure = {'container_id': 'd' * 64, 'image_id': 'sha256:' + 'e' * 64}
            config['non_workload_containers'] = [infrastructure]
            path.write_text(json.dumps(config), encoding='utf-8')
            self.assertEqual(recovery.load_deepinfer_config(path)['non_workload_containers'], [infrastructure])
            for invalid in ([{'container_id': 'd' * 64}], [infrastructure, infrastructure],
                            [{'container_id': 'a' * 64, 'image_id': 'sha256:' + 'e' * 64}]):
                with self.subTest(invalid=invalid):
                    config['non_workload_containers'] = invalid
                    path.write_text(json.dumps(config), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        recovery.load_deepinfer_config(path)

    def test_gap_history_requires_unchanged_daemon_and_complete_event_buffer(self):
        config = {'docker': '/usr/bin/docker'}
        previous = {'last_check': 100}
        active = 'ActiveState=active\nExecMainStartTimestampMonotonic=20000000\n'
        with patch('deploy_recovery.deepinfer.command', side_effect=[active, active]), \
             patch('deploy_recovery.deepinfer.docker_command', return_value='1\n' * 255):
            self.assertTrue(recovery.control_history_complete(config, previous, 100000, 100300))
        with patch('deploy_recovery.deepinfer.command', return_value=active), \
             patch('deploy_recovery.deepinfer.docker_command', return_value='1\n' * 256):
            self.assertFalse(recovery.control_history_complete(config, previous, 100000, 100300))
        with patch('deploy_recovery.deepinfer.command', return_value=
                   'ActiveState=active\nExecMainStartTimestampMonotonic=101000000\n'):
            self.assertFalse(recovery.control_history_complete(config, previous, 100000, 100300))

    def test_daemon_restart_during_event_query_invalidates_history(self):
        config = {'docker': '/usr/bin/docker'}
        previous = {'last_check': 100}
        before = 'ActiveState=active\nExecMainStartTimestampMonotonic=20000000\n'
        after = 'ActiveState=active\nExecMainStartTimestampMonotonic=30000000\n'
        with patch('deploy_recovery.deepinfer.command', side_effect=[before, after]), \
             patch('deploy_recovery.deepinfer.docker_command', return_value=''):
            self.assertFalse(recovery.control_history_complete(config, previous, 100000, 100300))

    def test_verified_gap_after_operator_stop_starts_fresh_idle_window(self):
        state = self.check({})
        self.stop_target()
        self.now = 180
        self.changes = [{'action': 'stop'}]
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            state = self.check(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertTrue(state['armed'])
        self.assertEqual(state['idle_since'], 180)
        self.assertFalse(self.starts)

    def test_operator_can_rearm_verified_stopped_target(self):
        previous = {'version': 1, 'target_id': 'fixed', 'boot_id': 'boot', 'started': 'start',
                    'event_cursor': 100000, 'manual_hold': False, 'armed': False,
                    'state': 'MANUAL_HOLD', 'samples': [{'time': 1, 'value': 'IDLE'}]}
        self.stop_target()
        result = recovery.rearm_stopped({'container_id': 'fixed'}, previous, 'boot',
                                        inspect=lambda _: copy.deepcopy(self.container), events=lambda *_: [],
                                        clock=lambda: 300, wall=lambda: 100300)
        self.assertTrue(result['armed'])
        self.assertEqual(result['state'], 'REARMED')
        self.assertEqual(result['samples'], [])
        self.assertEqual(result['rearm_finished_at'], 'failure')
        self.container['FinishedAt'] = 'changed-after-rearm'
        self.now = 360
        state = self.check(result)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertTrue(state['armed'])
        self.assertEqual(state['idle_since'], 360)
        self.assertFalse(self.starts)
        self.container['FinishedAt'] = 'failure'
        for changed in ({**previous, 'manual_hold': None}, {**previous, 'target_id': 'other'},
                        {**previous, 'boot_id': 'different'}):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    recovery.rearm_stopped({'container_id': 'fixed'}, changed, 'boot',
                                           inspect=lambda _: copy.deepcopy(self.container), events=lambda *_: [],
                                           clock=lambda: 300, wall=lambda: 100300)
        with self.assertRaises(ValueError):
            recovery.rearm_stopped({'container_id': 'fixed'}, previous, 'boot',
                                   inspect=lambda _: copy.deepcopy(self.container),
                                   events=lambda *_: [{'action': 'stop'}],
                                   clock=lambda: 300, wall=lambda: 100300)

    def test_running_target_never_samples_or_starts(self):
        def unexpected(_):
            self.fail('running target sampled')
        state = self.check({}, observe=unexpected)
        self.assertEqual(state['state'], 'RUNNING')
        self.assertFalse(self.starts)

    def test_final_probe_busy_unknown_and_manual_race_block_start(self):
        for last in ['BUSY', 'UNKNOWN']:
            self.setUp(); state = self.check({}); self.stop_target()
            for i in range(1, 121):
                self.now = i * 60; state = self.check(state)
            self.now = 7260
            values = iter(['IDLE', last])
            out = self.check(state, observe=lambda c: next(values))
            self.assertFalse(self.starts)
            if last == 'UNKNOWN':
                self.assertEqual(out['samples'], [])
                self.assertIsNone(out['idle_since'])
        self.setUp(); state = self.check({}); self.stop_target()
        for i in range(1, 121):
            self.now = i * 60; state = self.check(state)
        self.now = 7260
        events = iter([[], [{'action': 'stop'}]])
        out = self.check(state, events=lambda *args: next(events))
        self.assertEqual(out['state'], 'WAITING_IDLE')
        self.assertTrue(out['armed'])
        self.assertEqual(out['samples'], [])
        self.assertFalse(self.starts)

    def test_operator_action_during_slow_final_probe_is_preserved(self):
        state = self.check({}); self.stop_target()
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
        self.assertEqual(out['state'], 'WAITING_IDLE')
        self.assertTrue(out['armed'])
        self.assertEqual(out['samples'], [])
        self.assertFalse(self.starts)


class BootRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = {'container_id': 'fixed'}
        self.boot_epoch = 2_000_000_000
        self.now = 0
        self.prior = {'version': 1, 'target_id': 'fixed', 'boot_id': 'old',
                      'state': 'RUNNING', 'armed': True, 'manual_hold': False,
                      'policy_version': 4, 'trusted_fingerprint': TRUST_FIXED,
                      'event_cursor': self.boot_epoch - 300, 'last_check': 120,
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
        def run():
            return step(self.config, previous, inspect=inspect or (lambda c: copy.deepcopy(self.container)),
                        events=events or (lambda c, a, b: self.changes), sample=sample,
                        start=self.starts.append, persist=lambda s: self.saved.append(copy.deepcopy(s)),
                        clock=lambda: self.now, wall=lambda: self.boot_epoch + 10 + self.now)
        if isinstance(recovery.control_history_complete, Mock):
            return run()
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            return run()

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

    def test_unclaimed_stopped_container_stays_unclaimed_after_reboot(self):
        initial = prepare_previous({}, self.config, 'old', self.boot_epoch - 1000)
        first = self.check(initial)
        self.assertEqual(first['state'], 'MANUAL_HOLD')
        self.assertFalse(first['armed'])
        state = self.prepare(first)
        for i in range(121):
            self.now = i * 60
            state = self.check(state)
        self.assertFalse(state['armed'])
        self.assertFalse(self.starts)

    def test_incomplete_trusted_state_does_not_recover_after_reboot(self):
        for missing in ('armed', 'manual_hold', 'state', 'event_cursor', 'last_check'):
            with self.subTest(missing=missing):
                prior = dict(self.prior)
                del prior[missing]
                state = self.check(self.prepare(prior))
                self.assertFalse(state['armed'])
                self.assertFalse(self.starts)

    def test_legacy_unverified_stopped_armed_state_is_not_grandfathered(self):
        prior = {**self.prior, 'boot_id': 'new', 'policy_version': 3, 'state': 'WAITING_IDLE',
                 'armed': True, 'manual_hold': False, 'event_cursor': self.boot_epoch,
                 'last_check': 0, 'started': self.container['StartedAt'],
                 'finished': self.container['FinishedAt'], 'samples': [], 'idle_since': None}
        with patch('deploy_recovery.deepinfer.control_history_complete', return_value=True):
            state = self.check(self.prepare(prior, boot_id='new'))
        self.assertFalse(state['armed'])
        self.assertFalse(self.starts)

    def test_boot_preflight_reads_control_events_from_current_boot(self):
        ranges = []
        def events(_, since, until):
            ranges.append((since, until))
            return []
        state = self.check(self.prepare(), events=events)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(ranges[0][0], self.boot_epoch)

    def test_unarmed_same_boot_state_is_not_automatically_claimed(self):
        prior = {**self.prior, 'boot_id': 'new', 'state': 'MANUAL_HOLD', 'armed': False,
                 'trusted_fingerprint': None}
        state = self.check(self.prepare(prior))
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertFalse(state['armed'])
        self.assertFalse(self.starts)

    def test_host_reboot_starts_new_window_after_previous_manual_hold(self):
        state = self.check(self.prepare({**self.prior, 'manual_hold': True}))
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertFalse(state['manual_hold'])
        self.assertTrue(state['armed'])
        self.assertTrue(state['boot_recovery'])
        self.assertFalse(self.starts)

    def test_reboot_does_not_trust_unfinished_legacy_migration(self):
        prior = {**self.prior, 'policy_version': 3, 'state': 'CONTROL_UNKNOWN',
                 'manual_hold': True, 'armed': False, 'legacy_hold_pending': True}
        state = self.check(self.prepare(prior))
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertFalse(state['armed'])
        self.assertFalse(state.get('legacy_hold_pending', False))
        self.assertFalse(self.starts)

    def test_known_manual_hold_is_preserved_within_current_boot(self):
        prior = {**self.prior, 'boot_id': 'new', 'state': 'MANUAL_HOLD',
                 'armed': False, 'manual_hold': True}
        state = self.check(self.prepare(prior))
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertTrue(state['manual_hold'])
        self.assertFalse(self.starts)

    def test_postboot_stop_or_kill_starts_a_fresh_window(self):
        for action in ('stop', 'kill'):
            with self.subTest(action=action):
                self.setUp()
                self.container['FinishedAt'] = self.finished(1)
                self.changes = [{'action': 'start'}, {'action': action}]
                state = self.check(self.prepare())
                self.assertEqual(state['state'], 'WAITING_IDLE')
                self.assertEqual(state['idle_since'], 0)
                self.assertTrue(state['armed'])
                self.assertFalse(self.starts)
                self.changes = []
                for i in range(1, 121):
                    self.now = i * 60
                    state = self.check(state)
                self.assertEqual(state['state'], 'START_REQUESTED')
                self.assertEqual(len(self.starts), 1)

    def test_postboot_destroy_does_not_claim_the_target(self):
        self.changes = [{'action': 'destroy'}]
        self.assertEqual(self.check(self.prepare())['state'], 'MANUAL_HOLD')
        self.assertFalse(self.starts)

    def test_changed_finish_time_during_window_restarts_observation(self):
        state = self.check(self.prepare())
        self.now = 60
        self.container['FinishedAt'] = self.finished(1)
        state = self.check(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertTrue(state['armed'])
        self.assertEqual(state['idle_since'], 60)
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
