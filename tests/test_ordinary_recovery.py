import copy
import configparser
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from deploy_recovery import ordinary_recovery as recovery


class OrdinaryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = {'container_id': 'fixed'}
        self.now = 0
        self.boot = 'boot-a'
        self.target = {'Running': True, 'Paused': False, 'Restarting': False,
                       'Status': 'running', 'StartedAt': 'run-a', 'FinishedAt': '',
                       'ExitCode': 0, 'OOMKilled': False}
        self.changes = []
        self.saved, self.starts = [], []
        self.history_ok = True
        self.observation = 'IDLE'

    def prepared(self, previous=None, boot=None, boot_epoch=100000):
        return recovery.prepare(previous or {}, self.config, boot or self.boot, boot_epoch)

    def step(self, previous):
        return recovery.step(
            self.config, previous, inspect=lambda _: copy.deepcopy(self.target),
            events=lambda *_: copy.deepcopy(self.changes),
            history_complete=lambda *_: self.history_ok,
            sample=lambda _: self.observation,
            start=lambda _: self.starts.append('start'),
            persist=lambda s: self.saved.append(copy.deepcopy(s)),
            clock=lambda: self.now, wall=lambda: 100000 + self.now)

    def running(self):
        return self.step(self.prepared())

    def crash(self, code=137, oom=True):
        self.target.update(Running=False, Status='exited', FinishedAt='end-a',
                           ExitCode=code, OOMKilled=oom)

    def wait(self, previous):
        for i in range(61):
            self.now = 120 * (i + 1)
            previous = self.step(previous)
        return previous

    def test_running_service_is_only_observed(self):
        state = recovery.step(
            self.config, self.prepared(), inspect=lambda _: copy.deepcopy(self.target),
            sample=lambda _: self.fail('running target must not cause an NPU scan'),
            start=lambda _: self.fail('running target must not be started'),
            persist=lambda _: None, clock=lambda: 0, wall=lambda: 100000)
        self.assertEqual(state['state'], 'RUNNING')
        self.assertEqual(self.starts, [])
        self.assertEqual(state['samples'], [])

    def test_abnormal_exit_requires_a_full_sampled_window(self):
        state = self.running()
        self.crash()
        for i in range(60):
            self.now = 120 * (i + 1)
            state = self.step(state)
            self.assertEqual(self.starts, [])
        self.now = 7320
        state = self.step(state)
        self.assertEqual(state['state'], 'START_REQUESTED')
        self.assertEqual(self.starts, ['start'])
        self.assertTrue(any(row['state'] == 'START_PENDING' and not row['samples']
                            for row in self.saved))

    def test_own_start_followed_by_fast_failure_begins_a_new_window(self):
        state = self.running()
        self.crash()
        state = self.wait(state)
        self.assertEqual(state['state'], 'START_REQUESTED')
        self.target.update(StartedAt=datetime.fromtimestamp(107321, timezone.utc).isoformat(),
                           FinishedAt='end-b', ExitCode=1, OOMKilled=False)
        self.changes = [{'action': 'start', 'epoch': 107321}]
        self.now = 7380
        state = self.step(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(state['reason'], 'ABNORMAL_EXIT')
        self.assertEqual(state['idle_since'], 7380)
        self.assertEqual(self.starts, ['start'])

    def test_manual_stop_after_own_start_still_holds(self):
        state = self.running()
        self.crash()
        state = self.wait(state)
        self.target.update(StartedAt=datetime.fromtimestamp(107321, timezone.utc).isoformat(),
                           FinishedAt='end-b', ExitCode=143, OOMKilled=False)
        self.changes = [{'action': 'start', 'epoch': 107321}, {'action': 'kill', 'epoch': 107322}]
        self.now = 7380
        self.assertEqual(self.step(state)['state'], 'MANUAL_HOLD')

    def test_fast_failure_after_transient_inspection_error_keeps_own_start(self):
        state = self.running()
        self.crash()
        state = self.wait(state)
        self.now = 7380
        state = recovery.step(
            self.config, state, inspect=lambda _: (_ for _ in ()).throw(RuntimeError('unavailable')),
            persist=lambda s: self.saved.append(copy.deepcopy(s)),
            clock=lambda: self.now, wall=lambda: 100000 + self.now)
        self.assertEqual(state['previous_phase'], 'START_REQUESTED')
        self.target.update(StartedAt=datetime.fromtimestamp(107321, timezone.utc).isoformat(),
                           FinishedAt='end-b', ExitCode=1, OOMKilled=False)
        self.changes = [{'action': 'start', 'epoch': 107321}]
        self.now = 7440
        state = self.step(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(state['idle_since'], 7440)

    def test_timed_out_start_can_have_launched_a_generation_that_failed(self):
        state = self.running()
        self.crash()
        for i in range(60):
            self.now = 120 * (i + 1)
            state = self.step(state)
        self.now = 7320
        state = recovery.step(
            self.config, state, inspect=lambda _: copy.deepcopy(self.target),
            events=lambda *_: [], history_complete=lambda *_: True, sample=lambda _: 'IDLE',
            start=lambda _: (_ for _ in ()).throw(subprocess.TimeoutExpired('docker', 45)),
            persist=lambda s: self.saved.append(copy.deepcopy(s)),
            clock=lambda: self.now, wall=lambda: 100000 + self.now)
        self.assertEqual(state['state'], 'START_FAILED')
        self.target.update(StartedAt=datetime.fromtimestamp(107321, timezone.utc).isoformat(),
                           FinishedAt='end-b', ExitCode=1, OOMKilled=False)
        self.changes = [{'action': 'start', 'epoch': 107321}]
        self.now = 7380
        state = self.step(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(state['idle_since'], 7380)

    def test_manual_stop_is_held_even_after_reboot(self):
        state = self.running()
        self.crash(code=143)
        self.changes = [{'action': 'kill', 'epoch': 100010}]
        self.now = 120
        state = self.step(state)
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        state = self.prepared(state, boot='boot-b', boot_epoch=100100)
        self.changes = []
        state = self.wait(state)
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertEqual(self.starts, [])

    def test_slow_manual_stop_is_remembered_while_target_still_runs(self):
        state = self.running()
        self.changes = [{'action': 'kill', 'epoch': 100010}]
        self.now = 120
        state = self.step(state)
        self.changes = []
        self.crash(code=137, oom=True)
        state = self.wait(state)
        self.assertEqual(state['state'], 'MANUAL_HOLD')
        self.assertEqual(self.starts, [])

    def test_stop_in_event_overlap_is_not_discarded_after_clock_adjustment(self):
        state = self.running()
        self.now = 120
        state = self.step(state)
        self.crash(code=143, oom=False)
        self.changes = [{'action': 'kill', 'epoch': 100119.5}]
        self.now = 180
        self.assertEqual(self.step(state)['state'], 'MANUAL_HOLD')

    def test_nonzero_signal_exit_is_eligible_with_or_without_an_oom_flag(self):
        for oom in (False, True):
            with self.subTest(oom=oom):
                self.setUp()
                state = self.running()
                self.crash(code=137, oom=oom)
                self.now = 120
                self.assertEqual(self.step(state)['state'], 'WAITING_IDLE')

    def test_nonzero_exit_without_control_event_can_recover(self):
        state = self.running()
        self.crash(code=1, oom=False)
        self.now = 120
        self.assertEqual(self.step(state)['state'], 'WAITING_IDLE')

    def test_old_oom_flag_cannot_authorize_a_later_manual_exit(self):
        self.target['OOMKilled'] = True
        state = self.running()
        self.crash(code=0, oom=True)
        self.now = 120
        self.assertEqual(self.step(state)['state'], 'MANUAL_HOLD')

    def test_missing_control_history_rebases_a_stable_exit(self):
        state = self.running()
        self.crash()
        self.history_ok = False
        self.now = 120
        state = self.step(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(len(state['samples']), 1)
        self.assertEqual(self.starts, [])

    def test_missing_history_discards_old_idle_samples_before_rebase(self):
        state = self.running()
        self.crash()
        self.now = 120
        state = self.step(state)
        self.now = 240
        state = self.step(state)
        self.history_ok = False
        self.now = 360
        state = self.step(state)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(state['idle_since'], 360)
        self.assertEqual(len(state['samples']), 1)

    def test_transient_inspection_error_preserves_running_intent(self):
        state = self.running()
        self.now = 60
        state = recovery.step(
            self.config, state, inspect=lambda _: (_ for _ in ()).throw(RuntimeError('unavailable')),
            persist=lambda s: self.saved.append(copy.deepcopy(s)),
            clock=lambda: self.now, wall=lambda: 100000 + self.now)
        self.assertEqual(state['state'], 'CONTROL_UNKNOWN')
        self.assertEqual(state['previous_phase'], 'RUNNING')
        self.crash(code=1, oom=False)
        self.now = 120
        self.assertEqual(self.step(state)['state'], 'WAITING_IDLE')

    def test_reboot_after_transient_running_error_keeps_boot_intent(self):
        state = self.running()
        state.update(state='CONTROL_UNKNOWN', previous_phase='RUNNING')
        state = self.prepared(state, boot='boot-b', boot_epoch=100100)
        self.assertEqual(state['state'], 'BOOT_PENDING')

    def test_reboot_only_recovers_a_previously_observed_running_target(self):
        state = self.running()
        self.crash(code=137)
        self.target['FinishedAt'] = datetime.fromtimestamp(100110, timezone.utc).isoformat()
        self.now = 0
        state = self.prepared(state, boot='boot-b', boot_epoch=100100)
        state = self.wait(state)
        self.assertEqual(state['state'], 'START_REQUESTED')
        self.assertEqual(self.starts, ['start'])
        self.setUp()
        self.crash()
        state = self.prepared({}, boot='boot-b')
        self.assertEqual(self.step(state)['state'], 'MANUAL_HOLD')

    def test_orderly_shutdown_exit_before_boot_can_be_recovered(self):
        state = self.running()
        self.crash(code=0)
        self.target['FinishedAt'] = datetime.fromtimestamp(100090, timezone.utc).isoformat()
        state = self.prepared(state, boot='boot-b', boot_epoch=100100)
        state = self.wait(state)
        self.assertEqual(state['state'], 'START_REQUESTED')
        self.assertEqual(self.starts, ['start'])

    def test_post_boot_manual_stop_cancels_recovery(self):
        state = self.running()
        self.crash()
        state = self.prepared(state, boot='boot-b', boot_epoch=100100)
        self.changes = [{'action': 'stop', 'epoch': 100101}]
        self.now = 120
        self.assertEqual(self.step(state)['state'], 'MANUAL_HOLD')

    def test_observer_restart_clears_the_idle_samples(self):
        state = self.running()
        self.crash()
        self.now = 120
        state = self.step(state)
        self.now = 240
        state = self.step(state)
        self.assertEqual(len(state['samples']), 2)
        state = self.prepared(state)
        self.assertEqual(state['samples'], [])

    def test_long_sampling_pause_resets_the_idle_window(self):
        state = self.running()
        self.crash()
        for i in range(3):
            self.now = 120 * (i + 1)
            state = self.step(state)
        self.now += 200
        state = self.step(state)
        self.assertEqual(len(state['samples']), 1)
        self.assertEqual(state['idle_since'], self.now)

    def test_busy_observation_resets_the_window(self):
        state = self.running()
        self.crash()
        self.now = 120
        state = self.step(state)
        self.observation = 'BUSY'
        self.now = 240
        state = self.step(state)
        self.assertEqual(state['state'], 'BUSY')
        self.assertEqual(state['samples'], [])
        self.assertEqual(self.starts, [])

    def test_busy_final_probe_cancels_a_ready_start(self):
        state = self.running()
        self.crash()
        for i in range(60):
            self.now = 120 * (i + 1)
            state = self.step(state)
        self.now = 7320
        observations = iter(['IDLE', 'BUSY'])
        state = recovery.step(
            self.config, state, inspect=lambda _: copy.deepcopy(self.target),
            events=lambda *_: [], history_complete=lambda *_: True,
            sample=lambda _: next(observations), start=lambda _: self.starts.append('start'),
            persist=lambda s: self.saved.append(copy.deepcopy(s)),
            clock=lambda: self.now, wall=lambda: 100000 + self.now)
        self.assertEqual(state['state'], 'BUSY')
        self.assertEqual(state['samples'], [])
        self.assertEqual(self.starts, [])

    def test_incomplete_final_history_restarts_the_window_without_disarming(self):
        state = self.running()
        self.crash()
        for i in range(60):
            self.now = 120 * (i + 1)
            state = self.step(state)
        self.now = 7320
        history = iter([True, False])
        state = recovery.step(
            self.config, state, inspect=lambda _: copy.deepcopy(self.target),
            events=lambda *_: [], history_complete=lambda *_: next(history),
            sample=lambda _: 'IDLE', start=lambda _: self.starts.append('start'),
            persist=lambda s: self.saved.append(copy.deepcopy(s)),
            clock=lambda: self.now, wall=lambda: 100000 + self.now)
        self.assertEqual(state['state'], 'WAITING_IDLE')
        self.assertEqual(state['waiting_reason'], 'CONTROL_HISTORY_INCOMPLETE')
        self.assertTrue(state['armed'])
        self.assertEqual(state['samples'], [])
        self.assertEqual(self.starts, [])

    def test_incomplete_device_layout_is_unknown(self):
        config = {'container_id': 'fixed', 'device_patterns': [
            {'pattern': '/dev/davinci*', 'count': 9},
            {'pattern': '/dev/infiniband/uverbs*', 'count': 8},
            {'pattern': '/dev/uburma/*', 'count': 10}]}
        npu = ['/dev/davinci' + str(i) for i in range(8)] + ['/dev/davinci_manager']
        with patch.object(recovery.deepinfer, 'machine_observation', return_value='IDLE'), \
             patch.object(recovery.glob, 'glob', side_effect=[npu, [], []]), \
             patch.object(recovery.device_holders, 'inventory_device_holders', return_value=[]):
            self.assertEqual(recovery.machine_observation(config), 'UNKNOWN')

    def test_exact_system_service_holder_can_coexist_but_other_holders_block(self):
        config = {'container_id': 'fixed', 'device_patterns': [
            {'pattern': '/dev/davinci[0-7]', 'count': 8},
            {'pattern': '/dev/davinci_manager', 'count': 1}],
            'non_workload_processes': [{'service': 'exporter.service', 'exe_path': '/opt/exporter',
                                        'exe_sha256': 'a' * 64, 'cmdline_sha256': 'b' * 64,
                                        'environ_sha256': 'd' * 64}]}
        paths = ['/dev/davinci' + str(i) for i in range(8)]
        def inventory(_devices, _cid, *, precheck):
            ok = precheck(42, 17, b'exporter')
            return [{'pid': 42, 'start_ticks': 17, 'preverified': ok}]
        with patch.object(recovery.deepinfer, 'machine_observation', return_value='IDLE'), \
             patch.object(recovery.glob, 'glob', side_effect=[paths, ['/dev/davinci_manager']]), \
             patch.object(recovery.device_holders, 'inventory_device_holders', side_effect=inventory), \
             patch.object(recovery, 'system_process_matches', return_value=True, create=True):
            self.assertEqual(recovery.machine_observation(config), 'IDLE')
        with patch.object(recovery.deepinfer, 'machine_observation', return_value='IDLE'), \
             patch.object(recovery.glob, 'glob', side_effect=[paths, ['/dev/davinci_manager']]), \
             patch.object(recovery.device_holders, 'inventory_device_holders',
                          return_value=[{'pid': 42, 'preverified': True}, {'pid': 43, 'preverified': False}]), \
             patch.object(recovery, 'system_process_matches', return_value=True, create=True):
            self.assertEqual(recovery.machine_observation(config), 'BUSY')

    def test_system_service_identity_must_stay_exact_during_check(self):
        pin = {'service': 'exporter.service', 'exe_path': '/opt/exporter',
               'exe_sha256': 'a' * 64, 'cmdline_sha256': 'b' * 64, 'environ_sha256': 'd' * 64}
        row = {'pid': 42, 'start_ticks': 17}
        unit = {'LoadState': 'loaded', 'ActiveState': 'active', 'MainPID': '42',
                'ControlGroup': '/system.slice/exporter.service'}
        identity = {'root_uid': True, 'exe_path': pin['exe_path'], 'exe_sha256': pin['exe_sha256'],
                    'cmdline_sha256': pin['cmdline_sha256'], 'start_ticks': 17,
                    'environ_sha256': pin['environ_sha256'],
                    'cgroups': ['/system.slice/exporter.service']}
        with patch.object(recovery, '_systemd_main', side_effect=[unit, unit], create=True), \
             patch.object(recovery, '_process_identity', return_value=identity, create=True):
            self.assertTrue(recovery.system_process_matches(row, pin))
        changed = dict(unit, MainPID='43')
        with patch.object(recovery, '_systemd_main', side_effect=[unit, changed], create=True), \
             patch.object(recovery, '_process_identity', return_value=identity, create=True):
            self.assertFalse(recovery.system_process_matches(row, pin))
        changed_identity = dict(identity, cmdline_sha256='c' * 64)
        with patch.object(recovery, '_systemd_main', side_effect=[unit, unit], create=True), \
             patch.object(recovery, '_process_identity', return_value=changed_identity, create=True):
            self.assertFalse(recovery.system_process_matches(row, pin))
        changed_environment = dict(identity, environ_sha256='e' * 64)
        with patch.object(recovery, '_systemd_main', side_effect=[unit, unit], create=True), \
             patch.object(recovery, '_process_identity', return_value=changed_environment, create=True):
            self.assertFalse(recovery.system_process_matches(row, pin))
        with patch.object(recovery, '_systemd_main', side_effect=[unit, unit], create=True), \
             patch.object(recovery, '_process_identity', side_effect=[identity, changed_identity], create=True):
            self.assertFalse(recovery.system_process_matches(row, pin))

    def test_active_socket_is_not_contacted_even_with_running_daemon(self):
        service = 'LoadState=loaded\nActiveState=active\n'
        socket = 'LoadState=loaded\nActiveState=active\n'
        with patch.object(recovery.deepinfer, 'command', side_effect=[service, socket]) as command:
            with self.assertRaises(RuntimeError):
                recovery.docker_command({'docker': '/usr/bin/docker'}, ['ps'])
            self.assertEqual(command.call_count, 2)

    def test_inactive_socket_allows_connection_to_running_daemon(self):
        service = 'LoadState=loaded\nActiveState=active\n'
        socket = 'LoadState=loaded\nActiveState=inactive\n'
        with patch.object(recovery.deepinfer, 'command', side_effect=[service, socket, 'ok']):
            self.assertEqual(recovery.docker_command({'docker': '/usr/bin/docker'}, ['ps']), 'ok')

    def test_inactive_docker_is_not_contacted(self):
        service = 'LoadState=loaded\nActiveState=inactive\n'
        socket = 'LoadState=loaded\nActiveState=active\n'
        with patch.object(recovery.deepinfer, 'command', side_effect=[service, socket]) as command:
            with self.assertRaises(RuntimeError):
                recovery.docker_command({'docker': '/usr/bin/docker'}, ['ps'])
            self.assertEqual(command.call_count, 2)

    def test_unit_never_stops_or_kills_the_target(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(Path(__file__).parents[1] / 'deploy_recovery/llmdeploy-ordinary-recovery.service')
        self.assertIn('-m deploy_recovery.ordinary_recovery', unit['Service']['ExecStart'])
        self.assertNotIn('ExecStop', unit['Service'])
        self.assertNotIn('ExecStopPost', unit['Service'])
        self.assertEqual(unit['Service']['Restart'], 'always')

    def test_start_command_targets_only_the_pinned_container(self):
        with patch.object(recovery, 'docker_command') as docker:
            recovery.start_target(self.config)
            docker.assert_called_once_with(self.config, ['start', 'fixed'], timeout=45)


if __name__ == '__main__':
    unittest.main()
