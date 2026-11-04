import configparser
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy_recovery import deepinfer, pd, pd_guard, pd_ssh_notice
from deploy_recovery.ssh_notice import render_notice
from test_recovery import snapshot


class PdRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        launcher = root / 'launcher.py'
        launcher.write_text('task launcher', encoding='utf-8')
        self.receipt = root / 'faults.json'
        self.image = 'sha256:' + 'a' * 64
        self.baseline = {'failure': None, 'image_id': self.image,
                         'health_before': {str(i): 'Alarm' if i == 5 else 'OK' for i in range(8)},
                         'faults_before': {str(i): [] for i in range(8)}}
        self.receipt.write_text(json.dumps(self.baseline), encoding='utf-8')
        self.config = {'launcher_file': str(launcher), 'launcher_sha256': hashlib.sha256(launcher.read_bytes()).hexdigest(),
                       'fault_receipt': str(self.receipt), 'fault_receipt_sha256': hashlib.sha256(self.receipt.read_bytes()).hexdigest(),
                       'image_id': self.image, 'npu_smi': '/fixture/npu-smi',
                       'container_id': 'b' * 64}

    def command(self, card_output):
        def run(args):
            if args[-1] == 'info':
                return card_output
            return ''
        return run

    def test_complete_idle_and_known_alarm_is_admissible(self):
        with patch('deploy_recovery.deepinfer.command', side_effect=self.command(snapshot(alarm_card=5))) as command:
            self.assertEqual(pd.observe(self.config), 'IDLE')
            self.assertEqual(command.call_count, 9)

    def test_npu_process_or_utilization_prevents_recovery_even_with_alarm(self):
        for output in (snapshot(alarm_card=5, busy_card=5), snapshot(alarm_card=5, util=1)):
            with self.subTest(output=output), patch('deploy_recovery.deepinfer.command', side_effect=self.command(output)):
                self.assertEqual(pd.observe(self.config), 'BUSY')

    def test_changed_fault_missing_card_or_launcher_are_unknown(self):
        with patch('deploy_recovery.deepinfer.command', side_effect=self.command(snapshot(alarm_card=5, missing_process_card=7))):
            self.assertEqual(pd.observe(self.config), 'UNKNOWN')
        changed = dict(self.baseline)
        changed['faults_before'] = {**self.baseline['faults_before'], '4': [['0x1', '2']]}
        self.receipt.write_text(json.dumps(changed), encoding='utf-8')
        with patch('deploy_recovery.deepinfer.command', side_effect=self.command(snapshot(alarm_card=5))):
            self.assertEqual(pd.observe(self.config), 'UNKNOWN')
        self.config['fault_receipt_sha256'] = hashlib.sha256(self.receipt.read_bytes()).hexdigest()
        with patch('deploy_recovery.deepinfer.command', side_effect=self.command(snapshot(alarm_card=5))):
            self.assertEqual(pd.observe(self.config), 'UNKNOWN')
        Path(self.config['launcher_file']).write_text('changed', encoding='utf-8')
        with patch('deploy_recovery.deepinfer.command') as command:
            self.assertEqual(pd.observe(self.config), 'UNKNOWN')
            command.assert_not_called()

    def test_start_pins_launcher_and_starts_only_target(self):
        with patch('deploy_recovery.deepinfer.docker_command') as command:
            pd.start(self.config)
            command.assert_called_once_with(self.config, ['start', self.config['container_id']], timeout=45)
        Path(self.config['launcher_file']).write_text('changed', encoding='utf-8')
        with patch('deploy_recovery.deepinfer.docker_command') as command:
            with self.assertRaises(ValueError):
                pd.start(self.config)
            command.assert_not_called()

    def test_pd_uses_existing_two_hour_state_machine(self):
        with patch('deploy_recovery.deepinfer.main') as entry:
            pd.main()
            entry.assert_called_once_with(config_loader=pd.load_pd_config, sample=pd.observe, start=pd.start)
        state = {}
        for i in range(61):
            state, ready = deepinfer.sample_window(state, 120 * i, 'IDLE')
            self.assertEqual(ready, i == 60)

    def test_identity_changes_with_launcher_and_fault_baseline(self):
        baseline = deepinfer.target_fingerprint(self.config)
        for key in ('launcher_file', 'launcher_sha256', 'fault_receipt', 'fault_receipt_sha256'):
            with self.subTest(key=key):
                self.assertNotEqual(baseline, deepinfer.target_fingerprint({**self.config, key: 'different'}))

    def test_unit_and_ssh_notice(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(Path(__file__).parents[1] / 'deploy_recovery/llmdeploy-pd-recovery.service')
        self.assertIn('-m deploy_recovery.pd', unit['Service']['ExecStart'])
        self.assertEqual(unit['Service']['Restart'], 'always')
        notice = render_notice('b' * 64, 'llmdeploy-pd-decode', notice=pd_ssh_notice.NOTICE)
        self.assertIn('vLLM-Ascend', notice)
        self.assertIn('llmdeploy-pd-decode', notice)
        self.assertIn('SSH_CONNECTION', notice)
        with self.assertRaises(ValueError):
            render_notice('b' * 64, 'valid', notice='bad\nnotice')

    def test_safety_guard_recognizes_own_and_external_processes(self):
        output = snapshot(alarm_card=5, busy_card=5)
        with patch('deploy_recovery.deepinfer.inspect_target', return_value={'Running': True}), \
             patch('deploy_recovery.deepinfer.command', side_effect=self.command(output)), \
             patch('deploy_recovery.deepinfer.docker_command', return_value='PID\n42\n'):
            self.assertEqual(pd_guard.probe(self.config), {'state': 'MONITORING', 'owned_npu_processes': 1})
        with patch('deploy_recovery.deepinfer.inspect_target', return_value={'Running': True}), \
             patch('deploy_recovery.deepinfer.command', side_effect=self.command(output)), \
             patch('deploy_recovery.deepinfer.docker_command', return_value='PID\n99\n'):
            self.assertEqual(pd_guard.probe(self.config)['state'], 'NPU_CONFLICT')

    def test_safety_guard_only_stops_the_fixed_container(self):
        with patch('deploy_recovery.deepinfer.inspect_target', return_value={'Running': True}), \
             patch('deploy_recovery.deepinfer.docker_command') as command:
            pd_guard.stop_owned(self.config)
            command.assert_called_once_with(self.config, ['stop', '--time', '5', self.config['container_id']], timeout=40)
        with patch('deploy_recovery.deepinfer.inspect_target', side_effect=ValueError('identity changed')), \
             patch('deploy_recovery.deepinfer.docker_command') as command:
            with self.assertRaises(ValueError):
                pd_guard.stop_owned(self.config)
            command.assert_not_called()

    def test_device_health_change_does_not_stop_a_running_service(self):
        for condition in ('HEALTH_CHANGED', 'FAULT_CHANGED'):
            with self.subTest(condition=condition), \
                 patch('deploy_recovery.pd_guard.probe', return_value={'state': condition}), \
                 patch('deploy_recovery.pd_guard.stop_owned') as stop:
                self.assertEqual(pd_guard.check_once(self.config)['state'], condition)
                stop.assert_not_called()
                self.assertEqual(pd_guard.poll_interval(condition), 3)

    def test_main_loop_does_not_stop_on_health_or_fault_change(self):
        for condition in ('HEALTH_CHANGED', 'FAULT_CHANGED'):
            args = SimpleNamespace(config=Path('unused.json'))
            with self.subTest(condition=condition), \
                 patch('argparse.ArgumentParser.parse_args', return_value=args), \
                 patch('deploy_recovery.pd.load_pd_config', return_value={'state_file': 'state.json'}), \
                 patch('deploy_recovery.pd_guard.probe', side_effect=[{'state': condition}, KeyboardInterrupt]), \
                 patch('deploy_recovery.pd_guard.stop_owned') as stop, \
                 patch('deploy_recovery.pd_guard.write_state'), \
                 patch('deploy_recovery.pd_guard.time.sleep'):
                with self.assertRaises(KeyboardInterrupt):
                    pd_guard.main()
                stop.assert_not_called()

    def test_conflict_is_detected_even_when_device_health_changes(self):
        output = snapshot(alarm_card=4, busy_card=5)
        with patch('deploy_recovery.deepinfer.inspect_target', return_value={'Running': True}), \
             patch('deploy_recovery.deepinfer.command', side_effect=self.command(output)), \
             patch('deploy_recovery.deepinfer.docker_command', return_value='PID\n99\n'):
            self.assertEqual(pd_guard.probe(self.config)['state'], 'NPU_CONFLICT')

    def test_probe_reports_changed_device_without_stopping(self):
        output = snapshot(alarm_card=4, busy_card=5)
        with patch('deploy_recovery.deepinfer.inspect_target', return_value={'Running': True}), \
             patch('deploy_recovery.deepinfer.command', side_effect=self.command(output)), \
             patch('deploy_recovery.deepinfer.docker_command', return_value='PID\n42\n'), \
             patch('deploy_recovery.pd_guard.stop_owned') as stop:
            result = pd_guard.check_once(self.config)
            self.assertEqual(result['state'], 'HEALTH_CHANGED')
            self.assertEqual(result['health_changed_devices'], ['4', '5'])
            stop.assert_not_called()

    def test_fault_change_remains_visible_between_full_fault_checks(self):
        args = SimpleNamespace(config=Path('unused.json'))
        fault = {'state': 'FAULT_CHANGED', 'owned_npu_processes': 1, 'fault_changed_devices': ['4']}
        normal = {'state': 'MONITORING', 'owned_npu_processes': 1}
        with patch('argparse.ArgumentParser.parse_args', return_value=args), \
             patch('deploy_recovery.pd.load_pd_config', return_value={'state_file': 'state.json'}), \
             patch('deploy_recovery.pd_guard.probe', side_effect=[fault, normal, KeyboardInterrupt]), \
             patch('deploy_recovery.pd_guard.time.monotonic', side_effect=[100, 100, 101, 102]), \
             patch('deploy_recovery.pd_guard.stop_owned') as stop, \
             patch('deploy_recovery.pd_guard.write_state') as write, \
             patch('deploy_recovery.pd_guard.time.sleep'), patch('builtins.print') as log:
            with self.assertRaises(KeyboardInterrupt):
                pd_guard.main()
            stop.assert_not_called()
            self.assertEqual(write.call_args_list[1].args[1]['state'], 'FAULT_CHANGED')
            self.assertEqual(write.call_args_list[1].args[1]['fault_changed_devices'], ['4'])
            self.assertEqual(log.call_count, 1)

    def test_conflict_is_rechecked_and_only_stops_when_confirmed(self):
        with patch('deploy_recovery.pd_guard.probe', side_effect=[{'state': 'NPU_CONFLICT'},
                                                                  {'state': 'HEALTH_CHANGED'}]), \
             patch('deploy_recovery.pd_guard.time.sleep'), \
             patch('deploy_recovery.pd_guard.stop_owned') as stop:
            self.assertEqual(pd_guard.check_once(self.config)['state'], 'HEALTH_CHANGED')
            stop.assert_not_called()
        with patch('deploy_recovery.pd_guard.probe', side_effect=[{'state': 'NPU_CONFLICT'},
                                                                  {'state': 'NPU_CONFLICT'}]), \
             patch('deploy_recovery.pd_guard.time.sleep'), \
             patch('deploy_recovery.pd_guard.stop_owned') as stop:
            self.assertEqual(pd_guard.check_once(self.config),
                             {'state': 'STOPPED_BY_GUARD', 'reason': 'NPU_CONFLICT'})
            stop.assert_called_once_with(self.config)

    def test_safety_guard_does_not_probe_stopped_container(self):
        with patch('deploy_recovery.deepinfer.inspect_target', return_value={'Running': False}), \
             patch('deploy_recovery.deepinfer.command') as command:
            self.assertEqual(pd_guard.probe(self.config), {'state': 'STOPPED'})
            command.assert_not_called()

    def test_safety_guard_rejects_incomplete_npu_snapshot(self):
        with self.assertRaises(ValueError):
            pd_guard.npu_processes(snapshot(missing_process_card=7))


if __name__ == '__main__':
    unittest.main()
