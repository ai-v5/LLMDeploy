import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import render_pd
import pd_proxy


class PDRecipeTests(unittest.TestCase):
    def setUp(self):
        self.recipe = json.loads(render_pd.RECIPE.read_text(encoding='utf-8'))

    def test_a5_contract_preserves_full_capability(self):
        recipe = self.recipe
        self.assertEqual(recipe['client_limits'], {'contextWindow': 1048576, 'maxTokens': 32768})
        self.assertEqual(recipe['env']['VLLM_USE_V2_MODEL_RUNNER'], '1')
        for role in ('prefill', 'decode'):
            cmd = render_pd.vllm_command(recipe, role, 28410, 28500, 28700, '127.0.0.1')
            self.assertEqual(cmd[cmd.index('--max-model-len') + 1], '1048576')
            self.assertEqual(cmd[cmd.index('--tensor-parallel-size') + 1], '1')
            self.assertEqual(cmd[cmd.index('--data-parallel-size') + 1], '8')
            self.assertNotIn('--speculative-config', cmd)
            self.assertNotIn('--swa-bounded-replay', cmd)
            transfer = json.loads(cmd[cmd.index('--kv-transfer-config') + 1])
            self.assertEqual(transfer['kv_connector'], 'MooncakeHybridConnector')
            self.assertEqual(transfer['kv_role'], 'kv_producer' if role == 'prefill' else 'kv_consumer')
        self.assertEqual(recipe['evidence_state'], 'CANDIDATE')
        self.assertTrue(all(value == 'NOT_RUN' for value in recipe['validation'].values()))

    def test_render_requires_distinct_ports_and_paths(self):
        with self.assertRaises(ValueError):
            render_pd.vllm_command(self.recipe, 'prefill', 28410, 28410, 28700, '127.0.0.1')
        with self.assertRaises(ValueError):
            render_pd.vllm_command(self.recipe, 'prefill', True, 28500, 28700, '127.0.0.1')
        args = dict(port=28410, kv_port=28500, rpc_port=28700, node_ip='127.0.0.1', interface='lo',
                    model_dir='/srv/model', runtime_dir='/srv/runtime', name='pd-prefill')
        cmd = render_pd.command(self.recipe, 'prefill', **args)
        self.assertNotIn('--privileged', cmd)
        self.assertIn('/srv/model:/model:ro', cmd)
        self.assertIn('VLLM_API_KEY', cmd)
        self.assertIn('test -n "${VLLM_API_KEY:-}"', cmd[-1])
        self.assertIn('--host 127.0.0.1', cmd[-1])
        args['runtime_dir'] = '/srv/model/cache'
        with self.assertRaises(ValueError):
            render_pd.command(self.recipe, 'prefill', **args)

    def test_asymmetric_layout_updates_engine_and_connector_together(self):
        recipe = copy.deepcopy(self.recipe)
        recipe['topology']['prefill'].update(TP=2, DP=4)
        cmd = render_pd.vllm_command(recipe, 'prefill', 28410, 28500, 28700, '127.0.0.1')
        self.assertEqual(cmd[cmd.index('--tensor-parallel-size') + 1], '2')
        self.assertEqual(cmd[cmd.index('--data-parallel-size') + 1], '4')
        transfer = json.loads(cmd[cmd.index('--kv-transfer-config') + 1])
        self.assertEqual(transfer['kv_connector_extra_config']['prefill'], {'dp_size': 4, 'tp_size': 2})
        self.assertEqual(transfer['kv_connector_extra_config']['decode'], {'dp_size': 8, 'tp_size': 1})
        recipe['topology']['decode']['TP'] = 4
        with self.assertRaises(ValueError):
            render_pd.vllm_command(recipe, 'decode', 28411, 28600, 28700, '127.0.0.1')
        recipe['topology']['prefill'].update(TP=1, DP=8)
        recipe['topology']['decode'].update(TP=2, DP=4)
        with self.assertRaises(ValueError):
            render_pd.vllm_command(recipe, 'decode', 28411, 28600, 28700, '127.0.0.1')

    def test_role_uses_its_installed_runtime_image(self):
        recipe = copy.deepcopy(self.recipe)
        recipe['image']['runtime_image_ids'] = {'prefill': 'sha256:' + '1' * 64, 'decode': 'sha256:' + '2' * 64}
        args = dict(port=28410, kv_port=28500, rpc_port=28700, node_ip='127.0.0.1', interface='lo',
                    model_dir='/srv/model', runtime_dir='/srv/runtime', name='pd-runtime')
        for role in ('prefill', 'decode'):
            cmd = render_pd.command(recipe, role, **args)
            self.assertEqual(cmd[-5], recipe['image']['runtime_image_ids'][role])
        recipe['image']['runtime_image_ids']['decode'] = 'unverified:latest'
        with self.assertRaises(ValueError):
            render_pd.command(recipe, 'decode', **args)
        del recipe['image']['runtime_image_ids']['decode']
        with self.assertRaises(ValueError):
            render_pd.command(recipe, 'decode', **args)

    def test_proxy_preserves_original_request_and_rejects_fake_pd(self):
        body = {'model': 'test', 'prompt': [1, 2, 3], 'max_tokens': 32768, 'stream': True,
                'stream_options': {'include_usage': True}}
        original = copy.deepcopy(body)
        p = pd_proxy.prefill_request(body)
        self.assertEqual(body, original)
        self.assertEqual(p['max_tokens'], 1)
        self.assertFalse(p['stream'])
        self.assertTrue(p['kv_transfer_params']['do_remote_decode'])
        for bad in ({}, {'kv_transfer_params': {}}, {'kv_transfer_params': {'do_remote_prefill': True}}):
            with self.assertRaises(ValueError):
                pd_proxy.decode_request(body, bad)
        transfer = {'do_remote_prefill': True, 'remote_engine_id': 'p', 'remote_block_ids': [[1]],
                    'remote_request_id': 'req', 'remote_host': '127.0.0.1', 'remote_port': 28500}
        d = pd_proxy.decode_request(body, {'kv_transfer_params': transfer})
        self.assertEqual(d['max_tokens'], 32768)
        self.assertEqual(d['prompt'], original['prompt'])
        self.assertTrue(d['stream'])
        self.assertEqual(d['kv_transfer_params'], transfer)

    def test_proxy_default_budget_matches_recipe_and_preserves_input(self):
        self.assertEqual(pd_proxy.MAX_OUTPUT_TOKENS, self.recipe['client_limits']['maxTokens'])
        for body in ({'model': 'test'}, {'max_tokens': None, 'max_completion_tokens': None}):
            original = copy.deepcopy(body)
            limited = pd_proxy.bounded_request(body)
            self.assertEqual(limited['max_tokens'], 32768)
            self.assertNotIn('max_completion_tokens', limited)
            self.assertEqual(body, original)
            self.assertEqual(pd_proxy.prefill_request(limited)['max_tokens'], 1)

    def test_proxy_preserves_explicit_output_budgets(self):
        for body in ({'max_tokens': 1}, {'max_tokens': 4096}, {'max_tokens': 32768},
                     {'max_completion_tokens': 32768},
                     {'max_tokens': 64, 'max_completion_tokens': 64}):
            self.assertEqual(pd_proxy.bounded_request(body), body)

    def test_proxy_rejects_invalid_or_conflicting_budgets(self):
        for field in ('max_tokens', 'max_completion_tokens'):
            for value in (0, -1, 32769, True, 1.5, '32', [], {}):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    pd_proxy.bounded_request({field: value})
        for body in ([], None, {'max_tokens': 64, 'max_completion_tokens': 32}):
            with self.assertRaises(ValueError):
                pd_proxy.bounded_request(body)


if __name__ == '__main__':
    unittest.main()
