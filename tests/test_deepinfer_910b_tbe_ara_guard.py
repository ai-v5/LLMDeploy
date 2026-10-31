"""Exercise the TBE generator's filesystem guard without importing TBE."""
import ast
from pathlib import Path
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
GENERATOR = REPO / 'scripts/generate_deepinfer_910b_tbe_ara.py'


class TbeAraGuardTests(unittest.TestCase):
    def setUp(self):
        local = REPO / 'local'
        local.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='tbe-ara-guard-', dir=local)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'home').mkdir()
        (self.root / 'tmp').mkdir()
        (self.root / 'compile.py').write_bytes(GENERATOR.read_bytes())

    def guard(self):
        tree = ast.parse(GENERATOR.read_text(encoding='utf8'))
        # Run the actual preflight statements only, replacing the fixed container
        # mount with this fixture; TBE imports and generation remain unexecuted.
        statements = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'report' for t in node.targets):
                break
            statements.append(node)
        class Workspace(ast.NodeTransformer):
            def visit_Constant(self, node):
                return ast.copy_location(ast.Constant(str(self_outer.root)), node) if node.value == '/workspace' else node
        self_outer = self
        code = compile(ast.fix_missing_locations(Workspace().visit(ast.Module(body=statements, type_ignores=[]))), str(GENERATOR), 'exec')
        with mock.patch.object(Path, 'cwd', return_value=self.root):
            exec(code, {'__file__': str(GENERATOR)})

    def test_new_workspace_is_accepted(self):
        self.guard()

    def test_existing_unrelated_file_is_preserved_and_rejected(self):
        extra = self.root / 'keep'
        extra.write_bytes(b'untouched')
        with self.assertRaisesRegex(RuntimeError, 'Workspace must be new'):
            self.guard()
        self.assertEqual(extra.read_bytes(), b'untouched')

    def test_nonempty_tmp_is_rejected(self):
        (self.root / 'tmp' / 'keep').write_bytes(b'untouched')
        with self.assertRaisesRegex(RuntimeError, 'must be empty'):
            self.guard()
        self.assertEqual((self.root / 'tmp' / 'keep').read_bytes(), b'untouched')

    def test_different_compile_script_is_rejected(self):
        (self.root / 'compile.py').write_bytes(b'different')
        with self.assertRaisesRegex(RuntimeError, 'must match'):
            self.guard()


if __name__ == '__main__':
    unittest.main()
