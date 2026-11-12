import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build/pd-runtime-candidate'
SPEC = importlib.util.spec_from_file_location('pd_runtime_verify', BUILD / 'verify_runtime.py')
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


def wheel_names(lock):
    result = [f'ops/{module}.py' for module in lock['import_modules']]
    result += [f'ops/_aicpu/{name}' for name in lock['expected_aicpu_libraries']]
    for operator, count in lock['expected_native_profiles'].items():
        for index in range(count):
            for name in ('provider.so', 'provider.so.meta.json', 'variant.json'):
                result.append(f'ops/_native/operators/{operator}/variants/fixture-{index}/{name}')
    return result


class PDRuntimeBuildTests(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads((BUILD / 'dependencies.json').read_text(encoding='utf-8'))

    def test_lock_matches_frozen_recipe_and_exact_used_wrapper(self):
        recipe = json.loads((ROOT / 'recipes/deepseek-v4.1-flash-1017-pd/recipe.json').read_text(encoding='utf-8'))
        self.assertEqual(self.lock['evidence_state'], 'PREPARATION_NOT_REBUILT_NOT_NPU')
        self.assertTrue(self.lock['publication_gate'].startswith('HOLD_'))
        self.assertEqual(self.lock['base_image']['config_image_id'], recipe['image']['image_id'])
        self.assertTrue(self.lock['base_image']['reference'].endswith(recipe['image']['index_digest']))
        self.assertEqual(self.lock['source_revision'], recipe['runtime_operator_build']['source_revision'])
        self.assertEqual(self.lock['historical_crlf_archive_sha256'], recipe['runtime_operator_build']['source_git_archive_sha256'])
        self.assertNotEqual(self.lock['raw_archive_sha256'], self.lock['historical_crlf_archive_sha256'])
        self.assertEqual(self.lock['raw_archive_bytes'], 14090240)
        self.assertIn('core.autocrlf=false', self.lock['archive_method'])
        self.assertEqual(self.lock['source_public_access'], 'ANONYMOUS_EXACT_COMMIT_FETCH_AND_LICENSE_IDENTITY_VERIFIED')
        self.assertEqual(self.lock['expected_native_profiles'], {'mqsmla': 36, 'qli': 18, 'qsli': 18})
        self.assertEqual(hashlib.sha256((ROOT / self.lock['build']['wrapper']).read_bytes()).hexdigest(), self.lock['build']['wrapper_sha256'])
        wrapper = (ROOT / 'build/build_ops.sh').read_text(encoding='utf-8')
        self.assertIn('--operator mqsmla qli qsli --target dav-3510', wrapper)
        self.assertIn('--output-root /build/output --work-root /build/work', wrapper)

    def test_dockerfile_uses_public_base_and_separate_inputs(self):
        dockerfile = (BUILD / 'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('FROM --platform=linux/arm64 ' + self.lock['base_image']['reference'], dockerfile)
        self.assertNotIn('COPY . ', dockerfile)
        self.assertIn('COPY LICENSE.cannbot ', dockerfile)
        self.assertIn('--install-only', dockerfile)
        recipe = json.loads((ROOT / 'recipes/deepseek-v4.1-flash-1017-pd/recipe.json').read_text())
        for image in recipe['image']['runtime_image_ids'].values():
            self.assertNotIn(image, dockerfile)

    def test_complete_synthetic_wheel_structure_passes_without_native_execution(self):
        self.assertEqual(VERIFY.check_operator_files(wheel_names(self.lock), self.lock), self.lock['expected_native_profiles'])

    def test_missing_profile_and_incomplete_variant_are_rejected(self):
        names = wheel_names(self.lock)
        missing = [name for name in names if '/mqsmla/variants/fixture-0/' not in name]
        with self.assertRaisesRegex(ValueError, 'NATIVE_PROFILE_COUNTS_MISMATCH'):
            VERIFY.check_operator_files(missing, self.lock)
        names.remove('ops/_native/operators/qli/variants/fixture-0/provider.so')
        with self.assertRaisesRegex(ValueError, 'INCOMPLETE_NATIVE_PROFILE'):
            VERIFY.check_operator_files(names, self.lock)

    def test_missing_aicpu_or_python_module_is_rejected(self):
        names = wheel_names(self.lock)
        names.remove('ops/_aicpu/metadata_kernel.so')
        with self.assertRaisesRegex(ValueError, 'AICPU_LIBRARIES_MISMATCH'):
            VERIFY.check_operator_files(names, self.lock)
        names = wheel_names(self.lock)
        names.remove('ops/quant_lightning_indexer_dsl.py')
        with self.assertRaisesRegex(ValueError, 'OPERATOR_PYTHON_MODULE_MISSING'):
            VERIFY.check_operator_files(names, self.lock)

    def test_fresh_wheel_identity_is_recorded_without_claiming_old_hash(self):
        temporary_root = ROOT / 'local/pd-runtime-tests'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as temporary:
            folder = Path(temporary)
            framework = folder / self.lock['framework_wheel']
            framework.write_bytes(b'UNIT_TEST_FRAMEWORK_BYTES_NOT_A_REAL_WHEEL')
            license_file = folder / 'LICENSE.cannbot'
            license_file.write_bytes(b'UNIT_TEST_LICENSE_NOT_VENDOR_CONTENT')
            operator = folder / self.lock['operator_wheel']
            with zipfile.ZipFile(operator, 'w') as wheel:
                for name in wheel_names(self.lock):
                    wheel.writestr(name, b'UNIT_TEST_STRUCTURE_ONLY_NO_NATIVE_CODE')
            lock = copy.deepcopy(self.lock)
            lock['framework_wheel_sha256'] = VERIFY.file_hash(framework)
            lock['license']['source_license_sha256'] = VERIFY.file_hash(license_file)
            _, _, facts = VERIFY.check_inputs(lock, folder, folder, license_file)
            self.assertFalse(facts['operator_hash_is_historical_reference'])
            self.assertEqual(facts['operator_sha256'], VERIFY.file_hash(operator))
            lock['framework_wheel_sha256'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'FRAMEWORK_WHEEL_HASH_MISMATCH'):
                VERIFY.check_inputs(lock, folder, folder, license_file)
            lock['framework_wheel_sha256'] = VERIFY.file_hash(framework)
            lock['license']['source_license_sha256'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'UPSTREAM_LICENSE_HASH_MISMATCH'):
                VERIFY.check_inputs(lock, folder, folder, license_file)

    def test_cpu_check_rejects_npu_devices_before_imports(self):
        with patch.object(Path, 'glob', return_value=iter([Path('/dev/davinci0')])):
            with patch.object(VERIFY.importlib, 'import_module') as imported:
                with self.assertRaisesRegex(ValueError, 'CPU_VALIDATION_MUST_HAVE_NO_NPU_DEVICES'):
                    VERIFY.installed_fingerprints(self.lock)
                imported.assert_not_called()

    def test_source_archive_and_license_reject_mixed_lf_crlf_identity(self):
        temporary_root = ROOT / 'local/pd-runtime-tests'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as temporary:
            folder = Path(temporary)
            lf = b'SYNTHETIC_TEST_LICENSE_ONLY\nSECOND_LINE\n'
            archives = {}
            for label, data in [('lf', lf), ('crlf', lf.replace(b'\n', b'\r\n'))]:
                path = folder / (label + '.tar')
                with tarfile.open(path, 'w') as archive:
                    member = tarfile.TarInfo('LICENSE')
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
                archives[label] = path
            lock = copy.deepcopy(self.lock)
            lock['license']['source_license_sha256'] = hashlib.sha256(lf).hexdigest()
            # Genuine mixed contract: CRLF archive with a canonical LF license hash.
            lock['raw_archive_sha256'] = VERIFY.file_hash(archives['crlf'])
            with self.assertRaisesRegex(ValueError, 'UPSTREAM_LICENSE_HASH_MISMATCH'):
                VERIFY.check_source_archive(lock, archives['crlf'])
            lock['raw_archive_sha256'] = VERIFY.file_hash(archives['lf'])
            facts = VERIFY.check_source_archive(lock, archives['lf'])
            self.assertEqual(facts['source_license_sha256'], lock['license']['source_license_sha256'])
            with self.assertRaisesRegex(ValueError, 'SOURCE_ARCHIVE_HASH_MISMATCH'):
                VERIFY.check_source_archive(lock, archives['crlf'])

    def test_document_requires_explicit_canonical_tar_and_no_source_fallback_mount(self):
        document = (ROOT / 'docs/pd-runtime-build.md').read_text(encoding='utf-8')
        self.assertIn('-c core.autocrlf=false archive', document)
        self.assertIn('--check-source-archive', document)
        self.assertNotIn('pd-runtime-candidate:/deps:ro', document)
        self.assertIn('cannbot-source-4025ade4.tar:/deps/cannbot-source-4025ade4.tar:ro', document)
        self.assertIn('source-only fallback', document)
        self.assertNotIn('HOLD_EXACT_SOURCE_COMMIT_PUBLIC_ACCESS_NOT_REVALIDATED', document)

    def test_document_custom_recipe_fence_renders_both_roles_and_preserves_output(self):
        document = (ROOT / 'docs/pd-runtime-build.md').read_text(encoding='utf-8')
        def fence(marker, language):
            match = re.search(r'<!-- ' + marker + r' -->\s*```' + language + r'\n(.*?)\n```', document, re.S)
            self.assertIsNotNone(match)
            return match.group(1)
        source = fence('render-pd-copy-python', 'python')
        host_template = json.loads(fence('render-pd-host-json', 'json'))
        original_recipe = ROOT / 'recipes/deepseek-v4.1-flash-1017-pd/recipe.json'
        original_bytes = original_recipe.read_bytes()
        recipe = json.loads(original_bytes)
        recipe['image']['runtime_image_ids'] = {'prefill': 'sha256:' + '1' * 64, 'decode': 'sha256:' + '2' * 64}
        temporary_root = ROOT / 'local/pd-runtime-tests'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as temporary:
            folder = Path(temporary)
            script = folder / 'render_pd_copy.py'
            script.write_text(source, encoding='utf-8')
            recipe_path = folder / 'recipe-copy.json'
            recipe_path.write_text(json.dumps(recipe), encoding='utf-8')
            recipe_copy_bytes = recipe_path.read_bytes()
            env = {**os.environ, 'PYTHONPATH': str(ROOT / 'scripts')}
            for role, port, kv_port in [('prefill', 28410, 28500), ('decode', 28411, 28600)]:
                host = {**host_template, 'role': role, 'port': port, 'kv_port': kv_port,
                        'node_ip': '127.0.0.1', 'interface': 'lo', 'name': 'offline-' + role}
                host_path = folder / (role + '-host.json')
                host_path.write_text(json.dumps(host), encoding='utf-8')
                host_bytes = host_path.read_bytes()
                output = folder / (role + '.sh')
                args = [sys.executable, str(script), str(recipe_path), str(host_path), str(output)]
                result = subprocess.run(args, cwd=folder, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['state'], 'RENDERED_ONLY_NOT_DEPLOYED')
                raw = output.read_bytes()
                self.assertNotIn(b'\r', raw)
                command = shlex.split(raw.decode().splitlines()[2])
                self.assertEqual(command[:2], ['docker', 'run'])
                self.assertIn(recipe['image']['runtime_image_ids'][role], command)
                self.assertIn('VLLM_HOST_IP=127.0.0.1', command)
                self.assertIn('HCCL_SOCKET_IFNAME=lo', command)
                self.assertIn('VLLM_API_KEY', command)
                self.assertNotIn('VLLM_API_KEY=', raw.decode())
                again = subprocess.run(args, cwd=folder, env=env, capture_output=True, text=True)
                self.assertNotEqual(again.returncode, 0)
                self.assertIn('FileExistsError', again.stderr)
                self.assertEqual(output.read_bytes(), raw)
                self.assertEqual(host_path.read_bytes(), host_bytes)
            self.assertEqual(recipe_path.read_bytes(), recipe_copy_bytes)
        self.assertEqual(original_recipe.read_bytes(), original_bytes)


if __name__ == '__main__':
    unittest.main()
