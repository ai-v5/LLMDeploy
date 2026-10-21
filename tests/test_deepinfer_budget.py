"""Run the client token-admission regression with the repository test command."""
from pathlib import Path
import shutil
import subprocess
import unittest


class DeepInferBudgetTests(unittest.TestCase):
    def test_node_admission_regressions(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for the DSH client patch tests')
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, '--test', *map(str, sorted((root / 'client-patches/deepinfer-budget/tests').glob('*.test.mjs')))],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
