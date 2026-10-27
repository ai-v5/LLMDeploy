"""Tests for the source-only 910B W4A8 identity checker."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('w4a8_source_check', REPO / 'scripts/verify_deepinfer_910b_w4a8_source.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class SourceIdentityTests(unittest.TestCase):
    def test_committed_manifest_and_patches_are_consistent(self):
        manifest = json.loads(checker.MANIFEST.read_bytes())
        self.assertEqual(len(manifest['python_sha256_lf']), 338)
        self.assertEqual(len(manifest['patches']), 3)
        self.assertEqual(checker.verify_bundle(REPO, manifest), [])

    def test_normalized_bytes_and_source_drift(self):
        local = REPO / 'local'
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='w4a8-source-check-', dir=local) as directory:
            root = Path(directory); package = root / 'package'; package.mkdir()
            source = package / 'example.py'; source.write_bytes(b'answer = 42\r\n')
            expected = {'package/example.py': hashlib.sha256(b'answer = 42\n').hexdigest()}
            self.assertEqual(checker.verify_python(root, expected), [])
            source.write_bytes(b'answer = 43\n')
            self.assertEqual(checker.verify_python(root, expected), ['sha256:package/example.py'])
            (package / 'extra.py').write_bytes(b'pass\n')
            self.assertIn('unexpected:package/extra.py', checker.verify_python(root, expected))

    def test_manifest_paths_cannot_escape(self):
        for name in ('../outside.py', '/outside.py', 'package/../../outside.py', r'C:\outside.py', r'package\..\outside.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                checker.checked_relative(name)


if __name__ == '__main__':
    unittest.main()
