"""Verify CPU-only assembly boundaries using synthetic input files."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('indexer_assembly', REPO / 'scripts/assemble_deepinfer_910b_indexer_bodies.py')
assembly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assembly)


class IndexerAssemblyTests(unittest.TestCase):
    def setUp(self):
        local = REPO / 'local'
        local.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='indexer-assembly-', dir=local)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bodies = self.root / 'bodies'
        self.bodies.mkdir()
        self.baseline = b'fixture header\nstruct BaseTlv {\nbase_symbol\nbase_symbol\n}\n'
        self.names = ['indexer_schedule_rows_r1264.cce', 'indexer_gather_r722.cce',
                      'indexer_select_r760.cce', 'indexer_prefill_gather_r798.cce']
        self.manifest = {'scope': 'FOUR_BODY_IDENTITIES_ONLY_NOT_A_DEPLOYMENT_RECIPE',
                         'ara_base_sha256': assembly.sha(self.baseline), 'ara_base_symbol': 'base_symbol', 'bodies': {}}
        self.expected = {}
        footer = self.baseline[self.baseline.index(assembly.MARKER):]
        for name in self.names:
            body = ('fixture body ' + name + '\n').encode()
            filename = name + '.part'
            (self.bodies / filename).write_bytes(body)
            self.manifest['bodies'][name] = {'file': filename, 'sha256': assembly.sha(body)}
            self.expected[name] = assembly.sha(body + b'\n' + footer.replace(b'base_symbol', name.removesuffix('.cce').encode() + b'__kernel0'))

    def test_reconstructs_exact_expected_sources(self):
        outputs = assembly.assemble(self.baseline, self.bodies, self.manifest, self.expected)
        self.assertEqual({name: assembly.sha(raw) for name, raw in outputs.items()}, self.expected)

    def test_bad_baseline_and_modified_body_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'baseline checksum'):
            assembly.assemble(self.baseline + b'drift', self.bodies, self.manifest, self.expected)
        (self.bodies / (self.names[0] + '.part')).write_bytes(b'drift')
        with self.assertRaisesRegex(RuntimeError, 'body identity'):
            assembly.assemble(self.baseline, self.bodies, self.manifest, self.expected)

    def test_generated_metadata_cannot_be_embedded_in_a_body(self):
        name = self.names[0]
        for marker in (assembly.MARKER, b'.ascend.meta'):
            body = b'fixture\n' + marker + b'\n'
            (self.bodies / (name + '.part')).write_bytes(body)
            self.manifest['bodies'][name]['sha256'] = assembly.sha(body)
            with self.subTest(marker=marker), self.assertRaisesRegex(RuntimeError, 'body identity'):
                assembly.assemble(self.baseline, self.bodies, self.manifest, self.expected)

    def test_body_has_exactly_one_terminal_newline(self):
        name = self.names[0]
        path = self.bodies / (name + '.part')
        raw = path.read_bytes() + b'\n'
        path.write_bytes(raw)
        self.manifest['bodies'][name]['sha256'] = assembly.sha(raw)
        with self.assertRaisesRegex(RuntimeError, 'body identity'):
            assembly.assemble(self.baseline, self.bodies, self.manifest, self.expected)

    def test_source_hash_mismatch_and_path_escape_fail(self):
        bad_expected = dict(self.expected, **{self.names[0]: '0' * 64})
        with self.assertRaisesRegex(RuntimeError, 'Assembled source checksum'):
            assembly.assemble(self.baseline, self.bodies, self.manifest, bad_expected)
        self.manifest['bodies'][self.names[0]]['file'] = '../outside'
        with self.assertRaisesRegex(ValueError, 'Invalid native body path'):
            assembly.assemble(self.baseline, self.bodies, self.manifest, self.expected)

    def test_check_only_and_existing_output_do_not_write(self):
        baseline = self.root / 'base.cce'; baseline.write_bytes(self.baseline)
        bm = self.root / 'body.json'; bm.write_text(json.dumps(self.manifest), encoding='utf8')
        build = self.root / 'build.json'; build.write_text(json.dumps({'source_sha256': self.expected}), encoding='utf8')
        existing = self.root / 'existing'; existing.mkdir()
        keep = existing / 'keep'; keep.write_bytes(b'unchanged')
        with mock.patch.object(assembly, 'BODY_MANIFEST', bm), mock.patch.object(assembly, 'BUILD_MANIFEST', build), \
                mock.patch.object(assembly, 'BODIES', self.bodies), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(sys, 'argv', ['assembly', '--tbe-ara-cce', str(baseline), '--check-only']):
                self.assertEqual(assembly.main(), 0)
            with mock.patch.object(sys, 'argv', ['assembly', '--tbe-ara-cce', str(baseline), '--output', str(existing)]):
                with self.assertRaises(FileExistsError):
                    assembly.main()
        self.assertEqual(keep.read_bytes(), b'unchanged')
        self.assertEqual([p.name for p in existing.iterdir()], ['keep'])


if __name__ == '__main__':
    unittest.main()
