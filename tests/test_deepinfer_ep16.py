import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import runpy
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import serve_deepinfer_ep16 as serving


class DeepInferEP16Tests(unittest.TestCase):
    def setUp(self):
        self.recipe = serving.load(serving.PROFILE / 'recipe.json')
        self.args = ['--role', 'coordinator', '--coordinator', '192.0.2.10', '--peer', '192.0.2.20',
                     '--interface', 'eth0', '--run-id', 'example-run']

    def plan(self, **changes):
        values = dict(recipe=self.recipe, source='/workspace/src/runtime', role='coordinator',
                      coordinator='192.0.2.10', peer='192.0.2.20', interface='eth0', run_id='example-run')
        values.update(changes)
        return serving.build_plan(**values)

    def test_patch_chain_model_and_runtime_identity(self):
        source = serving.load(serving.PROFILE / 'source-files.json')
        self.assertEqual(source['base_commit'], self.recipe['source']['commit'])
        self.assertEqual(source['patches'], self.recipe['source']['patches'])
        for item in source['patches']:
            self.assertEqual(hashlib.sha256((ROOT / item['path']).read_bytes()).hexdigest(), item['sha256'])
        self.assertEqual(len(source['after_sha256']), 309)
        model = serving.load(serving.PROFILE / 'model-files.json')
        self.assertEqual(model['revision'], self.recipe['model']['revision'])
        self.assertEqual(len(model['files']), 152)
        lock = serving.load(serving.PROFILE / 'runtime-lock.json')
        requirements = dict(line.split('==', 1) for line in (serving.PROFILE / 'requirements.lock').read_text().splitlines())
        self.assertEqual(requirements, lock['packages'])
        self.assertEqual(lock['native_extension']['sha256'], 'b8c962a9ce6c175c178c06cdf0457342ab606d78d2c1eafb77ef078cd2d4217b')
        self.assertEqual(len(lock['workspace_artifacts']), 139)
        self.assertEqual(self.recipe['validation']['context_capacity'], 'CONFIGURED_262144_NOT_VALIDATED')
        self.assertEqual(self.recipe['topology']['ep'], 16)
        self.assertEqual(self.recipe['hardware']['hosts'], 2)

    def test_control_sources_are_frozen_and_complete(self):
        manifest = serving.load(serving.PROFILE / 'control-files.json')['files']
        self.assertEqual(set(manifest), {p.name for p in serving.BRIDGE.glob('*.py')})
        self.assertEqual(len(manifest), 9)
        for name, sha in manifest.items():
            self.assertEqual(serving.digest(serving.BRIDGE / name, normalize_lf=True), sha)

    def test_both_nodes_share_world_mtp_cache_and_resident_profile(self):
        first, second = self.plan(), self.plan(role='worker', interface='eth1')
        for plan in (first, second):
            env = plan['env']
            self.assertEqual(env['MASTER_ADDR'], '192.0.2.10')
            self.assertEqual(env['MASTER_PORT'], '29550')
            self.assertEqual(plan['distributed_store_port'], 29500)
            self.assertEqual(env['DEEPINFER_USE_NEXTN_DECODE'], '5')
            self.assertEqual(env['DEEPINFER_PREFILL_STORE_EXTRA_STATE'], '1')
            self.assertEqual(env['DEEPINFER_ENFORCE_EAGER'], '1')
            self.assertEqual(env['DEEPINFER_CONTROL_RUN_ID'], 'example-run')
            self.assertIn('/workspace/envs/native-extra-site', env['PYTHONPATH'])
            self.assertIn('/workspace/envs/runtime-site', env['PYTHONPATH'])
        self.assertEqual(first['env']['TASK_NODE_RANK'], '0')
        self.assertEqual(second['env']['TASK_NODE_RANK'], '1')
        self.assertTrue(self.recipe['server']['dspark'])
        self.assertEqual(self.recipe['server']['gpu_memory_utilization'], .95)
        self.assertEqual(self.recipe['server']['max_num_seqs'], 1)

    def test_invalid_paths_world_identity_and_port_aliases_fail_before_start(self):
        for values in ({'source':'/outside'}, {'source':'/workspace/../outside'},
                       {'source':'/workspace/src/invalid\n'}, {'role':'other'},
                       {'peer':'192.0.2.10'}, {'coordinator':'0.0.0.0'}, {'peer':'224.0.0.1'},
                       {'peer':'host.example'}, {'run_id':'../escape'}, {'run_id':'a'*65},
                       {'interface':'../eth0'}, {'port':True}, {'master_port':50},
                       {'port':29500}, {'control_port':29550}, {'control_port':29500}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.plan(**values)

    def test_plan_cannot_call_runtime_checks_or_exec(self):
        with (patch.object(serving,'check_runtime',side_effect=AssertionError('runtime read')),
             patch.object(serving.os,'execvpe',side_effect=AssertionError('start')),
             contextlib.redirect_stdout(io.StringIO()) as output):
            serving.main(['--plan', *self.args])
        self.assertEqual(json.loads(output.getvalue())['node_role'], 'coordinator')

    def test_default_check_does_not_start_or_demand_idle_devices(self):
        with (patch.object(serving,'check_runtime',return_value={'state':'test_identity'}) as check,
             patch.object(serving,'idle_preflight',side_effect=AssertionError('idle read')),
             patch.object(serving.os,'execvpe',side_effect=AssertionError('start')),
             contextlib.redirect_stdout(io.StringIO())):
            serving.main(self.args)
        check.assert_called_once()

    def test_spawn_import_cannot_start_a_second_world(self):
        with (patch.dict(sys.modules, {'api_entry':None, 'agent':None, 'resident_worker':None}),
             patch.object(sys, 'argv', ['ignored'])):
            runpy.run_path(str(serving.BRIDGE / 'runtime_entry.py'), run_name='__mp_main__')

    def test_head_entry_uses_fixed_server_options(self):
        captured = []
        fake = types.SimpleNamespace(main=lambda: captured.extend(sys.argv))
        with (patch.dict(sys.modules, {'api_entry':fake}),
             patch.object(sys, 'argv', ['entry','--role','coordinator','--port','8900'])):
            runpy.run_path(str(serving.BRIDGE / 'runtime_entry.py'), run_name='__main__')
        for key, value in self.recipe['server'].items():
            option = '--' + key.replace('_', '-')
            if value is True:
                self.assertIn(option, captured)
            else:
                self.assertEqual(captured[captured.index(option)+1], str(value))

    def test_supervisor_keeps_a_child_parent_and_maps_signal_exit(self):
        spec = importlib.util.spec_from_file_location('ep16_supervisor', serving.BRIDGE / 'supervisor.py')
        supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supervisor)
        child = Mock()
        child.wait.return_value = -15
        entry = serving.BRIDGE / 'runtime_entry.py'
        with (patch.object(sys,'argv',['supervisor',str(entry),'--role','worker','--port','8900']),
             patch.object(supervisor.subprocess,'Popen',return_value=child) as spawn,
             patch.object(supervisor.signal,'signal')):
            self.assertEqual(supervisor.main(), 143)
        self.assertEqual(spawn.call_args.args[0][1], str(entry.resolve()))
        with patch.object(sys,'argv',['supervisor',str(serving.BRIDGE / 'other.py')]), self.assertRaises(ValueError):
            supervisor.main()


if __name__ == '__main__':
    unittest.main()
