import copy
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deploy_deepinfer.config import RECIPE, ROOT, load, server_argv, validate, verify_runtime
from deploy_deepinfer.render import command, main as render
from scripts.deepinfer_preflight import complete_empty, check
from scripts.prepare_deepinfer_image import prepare
from scripts.smoke_deepinfer import check as smoke


class DeepInferRecipeTests(unittest.TestCase):
    def setUp(self):
        self.recipe = load(RECIPE)
        self.host = load(ROOT / 'hosts/a5/950dt/deepinfer.example.json')

    def test_historical_launch_contract_and_shared_manifest(self):
        argv = server_argv(self.recipe, 8900)
        for option, value in {'--max-model-len': '1048576', '--max-output-tokens': '32768',
                              '--max-images-per-request': '999', '--max-num-batched-tokens': '2048',
                              '--prefill-any-interval': '64', '--eplb-interval': '8192'}.items():
            self.assertEqual(argv[argv.index(option) + 1], value)
        self.assertIn('--no-dspark', argv)
        args = command(self.host, self.recipe, '/srv/LLMDeploy', ['/dev/uburma/urma0'])
        self.assertEqual(args.count('--device'), 14)
        self.assertNotIn('--privileged', args)
        self.assertEqual(args[args.index('--restart') + 1], 'no')
        self.assertIn('DEEPINFER_USE_NEXTN_DECODE=5', args)
        self.assertIn('DJ_JIT_CACHE_DIR=/artifacts/compiled-cache-r1', args)
        self.assertIn('type=bind,src=/srv/models/DeepSeek-V4.1-Flash-1017,dst=/model,readonly', args)
        self.assertIn('deploy_deepinfer.entrypoint', args[-1])
        self.assertTrue((RECIPE.parent / self.recipe['model']['manifest']).is_file())

    def test_reject_unknown_partial_or_overlapping_host(self):
        for fields in ({'devices': [0, 1]}, {'devices': [False, True, 2, 3, 4, 5, 6, 7]}, {'port': True}, {'port': self.host['master_port']},
                       {'image_id': 'latest'}, {'model_dir': '/'}, {'model_dir': '/data/../model'},
                       {'model_dir': '/bad,src=/private'}, {'warmup_file': self.host['model_dir'] + '/warmup.json'},
                       {'unknown': 'field'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate({**self.host, **fields}, self.recipe)

    def test_shell_quote_and_no_render_execution(self):
        host = {**self.host, 'artifacts_dir': '/srv/cache $(false)'}
        args = command(host, self.recipe, '/srv/LLMDeploy', [])
        self.assertEqual(shlex.split(shlex.join(args)), args)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'run.sh'
            with patch('deploy_deepinfer.render.sys.platform', 'linux'), \
                    patch('deploy_deepinfer.render.ROOT', Path('/srv/LLMDeploy')), \
                    patch('deploy_deepinfer.render.command', return_value=args), \
                    patch('deploy_deepinfer.render.load', side_effect=[host, self.recipe, host, self.recipe]), \
                    patch('subprocess.run') as run:
                render(['--host', str(Path(tmp) / 'host.json'), '--output', str(output)])
                run.assert_not_called()
                self.assertIn('deepinfer_preflight.py', output.read_text())
                with self.assertRaises(FileExistsError):
                    render(['--host', str(Path(tmp) / 'host.json'), '--output', str(output)])

    def test_source_normalizes_only_crlf_and_native_requires_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, native = root / 'sample.py', root / 'native.so'
            source.write_bytes(b'print(1)\r\n')
            native.write_bytes(b'native')
            recipe = copy.deepcopy(self.recipe)
            recipe['source']['runtime_files_sha256'] = {'sample.py': hashlib.sha256(b'print(1)\n').hexdigest()}
            recipe['source']['native_sha256'] = hashlib.sha256(b'native').hexdigest()
            verify_runtime(recipe, root, native)
            source.write_bytes(b'print(2)\n')
            with self.assertRaisesRegex(ValueError, 'RUNTIME_SOURCE_MISMATCH'):
                verify_runtime(recipe, root, native)
            source.write_bytes(b'print(1)\n')
            native.write_bytes(b'NATIVE')
            with self.assertRaisesRegex(ValueError, 'NATIVE_BINARY_MISMATCH'):
                verify_runtime(recipe, root, native)

    def test_process_query_must_be_complete_single_card_and_successful(self):
        empty = 'NPU ID : 3\nNo process in device.\n'
        complete_empty(3, subprocess.CompletedProcess([], 0, empty))
        for rc, value in ((1, empty), (0, ''), (0, empty.replace('3', '2')),
                          (0, empty + 'NPU ID : 3'), (0, empty + 'Process id: 42')):
            with self.subTest(rc=rc, value=value), self.assertRaises(ValueError):
                complete_empty(3, subprocess.CompletedProcess([], rc, value))

    def test_preflight_never_proceeds_after_bad_summary(self):
        with patch('scripts.deepinfer_preflight.platform.system', return_value='Linux'), \
                patch('scripts.deepinfer_preflight.platform.machine', return_value='aarch64'), \
                patch('scripts.deepinfer_preflight.Path.exists', return_value=True), \
                patch('scripts.deepinfer_preflight.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'partial')) as run:
            with self.assertRaises(ValueError):
                check(self.host)
            self.assertEqual(run.call_count, 1)

    def test_overlay_rejects_source_mismatch_before_creating_context(self):
        with tempfile.TemporaryDirectory() as tmp, patch('scripts.prepare_deepinfer_image.subprocess.run',
                return_value=subprocess.CompletedProcess([], 0, b'incorrect')):
            output = Path(tmp) / 'context'
            with self.assertRaisesRegex(ValueError, 'PINNED_PUBLIC_SOURCE_UNAVAILABLE'):
                prepare(Path(tmp), output)
            self.assertFalse(output.exists())

    def test_smoke_validates_profile_and_makes_only_synthetic_request(self):
        replies = [{'status': 'ready', 'workers': 8}, {'data': [{'id': 'ds-v4.1-flash-1017',
                    'max_model_len': 1048576, 'max_output_tokens': 32768, 'max_images_per_request': 999,
                    'dspark': False}]}, {'choices': [{'message': {'content': 'ready'}}]}]
        calls = []
        def request(req, timeout):
            calls.append(req)
            return io.BytesIO(json.dumps(replies[len(calls) - 1]).encode())
        self.assertIn('SHORT_JSON_SMOKE_PASS', smoke('http://127.0.0.1:8900/v1', request=request)['state'])
        self.assertEqual(json.loads(calls[-1].data)['max_tokens'], 32)
        for base in ('http://example.com/v1', 'http://key@127.0.0.1/v1', 'http://127.0.0.1/v1?key=x'):
            with self.assertRaises(ValueError):
                smoke(base, request=request)


if __name__ == '__main__':
    unittest.main()
