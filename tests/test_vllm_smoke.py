import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from scripts import smoke


class VllmSmokeTests(unittest.TestCase):
    def test_smoke_accepts_current_1m_recipe_and_checks_sse_cache(self):
        model = io.BytesIO(json.dumps({'data': [{'id': 'ds-v4.1-flash-1017', 'max_model_len': 1048576}]}).encode())
        def stream(cached):
            event = {'choices': [{'delta': {'content': 'DEPLOY_OK'}}],
                     'usage': {'prompt_tokens_details': {'cached_tokens': cached}}}
            return io.BytesIO(('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n').encode())
        opener = Mock()
        opener.open.side_effect = [model, stream(0), stream(10)]
        with patch('sys.argv', ['smoke.py']), patch('scripts.smoke.urllib.request.build_opener', return_value=opener), contextlib.redirect_stdout(io.StringIO()) as output:
            smoke.main()
        self.assertEqual(json.loads(output.getvalue())['state'], 'PASS_FUNCTIONAL_ONLY')
        self.assertEqual(opener.open.call_count, 3)


if __name__ == '__main__':
    unittest.main()
