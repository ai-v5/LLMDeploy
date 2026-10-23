import contextlib
import hashlib
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import serve_deepinfer_a2 as serving


class DeepInferA2Tests(unittest.TestCase):
    def setUp(self):
        self.recipe = serving.load(serving.PROFILE / 'recipe.json')

    def test_model_and_patch_identity_chain(self):
        source = serving.load(serving.PROFILE / self.recipe['source']['manifest'])
        model = serving.load(serving.PROFILE / self.recipe['model']['manifest'])
        payload = ROOT / self.recipe['source']['patch']
        self.assertEqual(hashlib.sha256(payload.read_bytes()).hexdigest(), self.recipe['source']['patch_sha256'])
        self.assertEqual(source['patch_sha256'], self.recipe['source']['patch_sha256'])
        self.assertEqual(source['base_commit'], self.recipe['source']['commit'])
        self.assertEqual(len(source['after_sha256']), 305)
        self.assertEqual(len(source['before_sha256']), 65)
        self.assertEqual(sum(value is None for value in source['before_sha256'].values()), 13)
        self.assertEqual(model['revision'], self.recipe['model']['revision'])
        self.assertFalse(self.recipe['image']['registry_published'])
        self.assertEqual(self.recipe['validation']['context_capacity'], 'HISTORICAL_CONFIGURED_NOT_VALIDATED_NO_ACTIVE_CAPACITY_TEST')

    def test_fixed_mtp_cache_context_and_api_isolation_contract(self):
        plan = serving.build_plan(self.recipe, '/workspace/src/runtime', '/workspace/envs/api/bin/python', 8900, 29500)
        args = plan['argv']
        for option, expected in [('--max-model-len', '262144'), ('--max-output-tokens', '32768'),
                                 ('--max-num-batched-tokens', '2048'), ('--max-num-seqs', '1'),
                                 ('--gpu-memory-utilization', '0.75'), ('--eplb-interval', '0')]:
            self.assertEqual(args[args.index(option) + 1], expected)
        self.assertIn('--dspark', args)
        self.assertEqual(plan['env']['DEEPINFER_USE_NEXTN_DECODE'], '5')
        self.assertEqual(plan['env']['DEEPINFER_ENABLE_MTP_VARLEN'], '1')
        self.assertEqual(plan['env']['DEEPINFER_PREFILL_STORE_EXTRA_STATE'], '1')
        self.assertIn('pinned_reserve_segment_size_mb:64', plan['env']['PYTORCH_NPU_ALLOC_CONF'])
        self.assertNotIn('python-site', plan['env']['PYTHONPATH'])
        self.assertIn('python-site', serving.worker_script('/workspace/src/runtime'))

    def test_all_api_package_pins_and_protocol_binary_are_locked(self):
        lock = serving.load(serving.PROFILE / self.recipe['runtime_lock'])
        text = (serving.PROFILE / self.recipe['api_requirements']).read_text(encoding='utf-8')
        self.assertEqual(dict(line.split('==', 1) for line in text.splitlines() if line), lock['api_packages'])
        self.assertEqual(len(lock['api_packages']), 18)
        self.assertTrue(any(name.endswith('.so') for name in lock['api_artifacts']))

    def test_plan_rejects_outside_paths_and_invalid_ports(self):
        for source, python, port, master in [('/other/source', '/workspace/envs/api/bin/python', 8900, 29500),
                                            ('/workspace/../source', '/workspace/envs/api/bin/python', 8900, 29500),
                                            ('/workspace/src/r\n', '/workspace/envs/api/bin/python', 8900, 29500),
                                            ('/workspace/src/r', '/usr/bin/python3', 8900, 29500),
                                            ('/workspace/src/r', '/workspace/envs/api/bin/python', True, 29500),
                                            ('/workspace/src/r', '/workspace/envs/api/bin/python', 8900, 8900)]:
            with self.subTest(source=source, python=python, port=port, master=master):
                with self.assertRaises(ValueError):
                    serving.build_plan(self.recipe, source, python, port, master)

    def test_changed_source_path_gets_its_own_worker_wrapper(self):
        first = serving.build_plan(self.recipe, '/workspace/src/first', '/workspace/envs/api/bin/python', 8900, 29500)
        second = serving.build_plan(self.recipe, '/workspace/src/second', '/workspace/envs/api/bin/python', 8900, 29500)
        self.assertNotEqual(first['worker'], second['worker'])
        self.assertEqual(first['argv'][first['argv'].index('--worker-python') + 1], first['worker'])

    def test_plan_never_checks_hardware_or_starts_workers(self):
        output = io.StringIO()
        with patch.object(serving, 'check_runtime') as check, patch.object(serving, 'idle_preflight') as idle, \
             patch.object(serving.os, 'execvpe') as execute, contextlib.redirect_stdout(output):
            serving.main(['--plan'])
        check.assert_not_called()
        idle.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['argv'][0], '/workspace/envs/api/bin/python')

    def test_source_integrity_accepts_lf_and_rejects_same_size_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / 'module.py'
            file.write_bytes(b'x = 1\r\n')
            manifest = {'after_sha256': {'module.py': hashlib.sha256(b'x = 1\n').hexdigest()}}
            self.assertEqual(serving.verify_source(root, manifest), 1)
            file.write_bytes(b'x = 2\r\n')
            with self.assertRaisesRegex(ValueError, 'SOURCE_SHA256_MISMATCH'):
                serving.verify_source(root, manifest)

    def test_source_manifest_cannot_read_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (root / 'outside.py').write_bytes(b'x=1\n')
            with self.assertRaisesRegex(ValueError, 'INVALID_MANIFEST_PATH'):
                serving.verify_source(source, {'after_sha256': {'../outside.py': 'unused'}})

    def test_extra_import_shadow_cannot_pass_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'module.py').write_bytes(b'x=1\n')
            manifest = {'after_sha256': {'module.py': hashlib.sha256(b'x=1\n').hexdigest()}}
            self.assertEqual(serving.verify_source(root, manifest), 1)
            (root / 'torch.py').write_bytes(b'x=2\n')
            with self.assertRaisesRegex(ValueError, 'SOURCE_FILE_SET_MISMATCH'):
                serving.verify_source(root, manifest)

    def test_worker_quotes_source_as_one_literal_assignment(self):
        source = "/workspace/src/source 'quoted' $(unexpected)"
        script = serving.worker_script(source)
        assignment = next(line for line in script.splitlines() if line.startswith('export PYTHONPATH='))
        self.assertEqual(shlex.split(assignment), ['export', 'PYTHONPATH=' + source + ':/workspace/envs/python-site'])
        self.assertTrue(script.endswith('exec ' + serving.NATIVE_PYTHON + ' "$@"\n'))

    def test_admit_only_the_exact_healthy_idle_hardware_profile(self):
        healthy = '\n'.join('| ' + str(i) + ' 910B1 | OK |' for i in range(8))
        idle = '\nNo running processes found' * 8
        serving.verify_idle_cards(healthy + idle)
        for invalid in (healthy.replace('910B1', '910B2') + idle,
                        healthy.replace('| 7 910B1', '| 6 910B1') + idle,
                        healthy.replace('OK', 'Alarm', 1) + idle,
                        healthy + '\nNo running processes found' * 7):
            with self.assertRaisesRegex(ValueError, 'HEALTHY_IDLE_910B1'):
                serving.verify_idle_cards(invalid)


if __name__ == '__main__':
    unittest.main()
