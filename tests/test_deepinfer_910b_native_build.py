"""CPU-only tests for the native builder's input and output boundaries."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_builder', REPO / 'scripts/build_deepinfer_910b_native.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class NativeBuildTests(unittest.TestCase):
    def setUp(self):
        local = REPO / 'local'
        local.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='native-builder-', dir=local)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'sources'
        self.source.mkdir()
        names = {name + '.cce' for name in builder.KERNELS} | {entry[0] for entry in builder.BRIDGES.values()}
        self.expected = {}
        for name in names:
            raw = ('fixture:' + name).encode()
            (self.source / name).write_bytes(raw)
            self.expected[name] = hashlib.sha256(raw).hexdigest()

    def test_committed_manifest_has_exact_inputs_and_outputs(self):
        manifest = json.loads(builder.MANIFEST.read_bytes())
        self.assertEqual(set(manifest['source_sha256']), set(self.expected))
        self.assertEqual(set(manifest['artifact_sha256']), set(builder.KERNELS.values()) | {item[1] for item in builder.BRIDGES.values()})
        self.assertEqual(manifest['scope'], 'NATIVE_INPUT_IDENTITIES_ONLY_NOT_A_DEPLOYMENT_RECIPE')
        bridges = REPO / 'recipes/a2/910b/deepinfer/w4a8-source/native-bridges'
        for filename, _ in builder.BRIDGES.values():
            self.assertEqual(builder.sha(bridges / filename), manifest['source_sha256'][filename])

    def test_source_drift_fails_before_compilation(self):
        self.assertEqual(builder.verify_sources(self.source, self.expected), self.source.resolve())
        (self.source / 'indexer_gather_r722.cce').write_bytes(b'drift')
        with self.assertRaisesRegex(RuntimeError, 'Source checksum mismatch'):
            builder.verify_sources(self.source, self.expected)

    def test_existing_or_source_nested_output_is_rejected(self):
        with self.assertRaises(FileExistsError):
            builder.new_output(self.source.resolve(), self.root)
        with self.assertRaisesRegex(RuntimeError, 'outside the source'):
            builder.new_output(self.source.resolve(), self.source / 'out')
        self.assertFalse((self.source / 'out').exists())

    def test_wrong_cxx_is_rejected(self):
        compiler = self.root / 'compiler'
        compiler.write_bytes(b'compiler fixture')
        expected = hashlib.sha256(compiler.read_bytes()).hexdigest()
        with mock.patch.dict(os.environ, {'CXX': str(compiler)}), mock.patch.object(builder.shutil, 'which', return_value=str(compiler)):
            builder.verify_cxx(expected)
            with self.assertRaisesRegex(RuntimeError, 'pinned compiler'):
                builder.verify_cxx('0' * 64)

    def test_check_inputs_never_creates_output_or_compiles(self):
        manifest = {'scope': 'NATIVE_INPUT_IDENTITIES_ONLY_NOT_A_DEPLOYMENT_RECIPE', 'source_sha256': self.expected,
                    'artifact_sha256': {name: '0' * 64 for name in set(builder.KERNELS.values()) | {x[1] for x in builder.BRIDGES.values()}}}
        manifest_path = self.root / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest), encoding='utf8')
        with mock.patch.object(builder, 'MANIFEST', manifest_path), mock.patch.object(builder, 'kernel_phase') as kernel, \
                mock.patch.object(builder, 'bridge_phase') as bridge, mock.patch.object(sys, 'argv', ['builder', '--source-dir', str(self.source), '--check-inputs']), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main(), 0)
        kernel.assert_not_called()
        bridge.assert_not_called()
        self.assertEqual({p.name for p in self.root.iterdir()}, {'sources', 'manifest.json'})


if __name__ == '__main__':
    unittest.main()
