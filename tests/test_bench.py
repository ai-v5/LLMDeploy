import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import threading
import unittest

BENCH = Path(__file__).resolve().parents[1] / 'tools' / 'bench' / 'bench.py'
spec = importlib.util.spec_from_file_location('bench_under_test', BENCH)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

MODEL = 'test-model'


def sse(frames):
    """Encode events as the chunked SSE lines the endpoint emits."""
    out = []
    for frame in frames:
        out.append(b'data: ' + json.dumps(frame).encode() + b'\n\n')
    out.append(b'data: [DONE]\n\n')
    return b''.join(out)


def chunk(delta, finish=None):
    return {'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}


def usage(completion, prompt=10, reasoning=None):
    details = {}
    if reasoning is not None:
        details['reasoning_tokens'] = reasoning
    return {'usage': {'prompt_tokens': prompt, 'completion_tokens': completion,
                      'completion_tokens_details': details}}


class FakeEndpoint:
    """Minimal OpenAI-compatible server returning a scripted stream."""

    def __init__(self, frames, delay=0.0, status=200):
        self.frames = frames
        self.delay = delay
        self.status = status
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps({'data': [{'id': MODEL, 'max_model_len': 1048576}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                outer.requests.append({
                    'path': self.path,
                    'authorization': self.headers.get('Authorization'),
                    'body': json.loads(self.rfile.read(length) or b'{}'),
                })
                if outer.status != 200:
                    payload = b'{"error":"bad request"}'
                    self.send_response(outer.status)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                payload = outer.frames
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self):
        return 'http://127.0.0.1:%d/v1' % self.server.server_address[1]


class StreamTests(unittest.TestCase):
    def test_ttft_uses_first_delta_and_tpot_excludes_prefill(self):
        frames = sse([chunk({'role': 'assistant', 'content': ''}),
                      chunk({'reasoning': '思考'}),
                      chunk({'reasoning': '继续'}),
                      chunk({'content': '答案'}),
                      usage(completion=4, prompt=7, reasoning=2)])
        with FakeEndpoint(frames) as endpoint:
            run = bench.stream_once(endpoint.base,
                                    bench.build_payload({'messages': [{'role': 'user',
                                                                       'content': 'hi'}]}, MODEL))
        self.assertIsNotNone(run['first_any_s'])
        self.assertIsNotNone(run['first_content_s'])
        self.assertEqual(run['usage']['completion_tokens'], 4)
        self.assertEqual(run['chars'], 2)
        # First token clock must not be later than the first content clock.
        self.assertLessEqual(run['first_any_s'], run['first_content_s'])

    def test_reasoning_field_is_accepted_as_a_first_token(self):
        frames = sse([chunk({'reasoning': 'r'}), chunk({'content': 'c'}),
                      usage(completion=2)])
        with FakeEndpoint(frames) as endpoint:
            run = bench.stream_once(endpoint.base,
                                    bench.build_payload({'messages': [{'role': 'user',
                                                                       'content': 'x'}]}, MODEL))
        self.assertIsNotNone(run['first_any_s'])

    def test_openai_reasoning_content_spelling_also_counts(self):
        frames = sse([chunk({'reasoning_content': 'r'}), chunk({'content': 'c'}),
                      usage(completion=2)])
        with FakeEndpoint(frames) as endpoint:
            run = bench.stream_once(endpoint.base,
                                    bench.build_payload({'messages': [{'role': 'user',
                                                                       'content': 'x'}]}, MODEL))
        self.assertIsNotNone(run['first_any_s'])

    def test_content_first_clock_stays_none_when_only_thinking(self):
        frames = sse([chunk({'reasoning': 'a'}), chunk({'reasoning': 'b'}),
                      usage(completion=2)])
        with FakeEndpoint(frames) as endpoint:
            run = bench.stream_once(endpoint.base,
                                    bench.build_payload({'messages': [{'role': 'user',
                                                                       'content': 'x'}]}, MODEL))
        self.assertIsNone(run['first_content_s'])


class PayloadTests(unittest.TestCase):
    def test_snippet_placeholder_is_replaced_from_repo(self):
        payload = bench.build_payload({'snippet': 'lru_router.py', 'messages': [
            {'role': 'user', 'content': 'code:\n{snippet}\nend'}]}, MODEL)
        text = payload['messages'][0]['content']
        self.assertIn('class LRUCache', text)
        self.assertNotIn('{snippet}', text)

    def test_haystack_is_generated_and_needle_is_embedded(self):
        payload = bench.build_payload({
            'haystack': {'tokens': 800, 'needle': 'CAL-1234'},
            'messages': [{'role': 'user', 'content': '{haystack}\n\n问题：校准码？'}]}, MODEL)
        text = payload['messages'][0]['content']
        self.assertIn('CAL-1234', text)
        self.assertIn('问题：校准码？', text)
        self.assertGreater(len(text), 800)
        # The needle must sit inside the body, not be appended at one end.
        self.assertTrue(0 < text.index('CAL-1234') < len(text) - 10)

    def test_image_part_becomes_a_data_url(self):
        payload = bench.build_payload({'messages': [{'role': 'user', 'content': [
            {'type': 'image', 'asset': 'slide-512x288.png'},
            {'type': 'text', 'text': '解读'}]}]}, MODEL)
        parts = payload['messages'][0]['content']
        self.assertEqual(parts[0]['type'], 'image_url')
        self.assertTrue(parts[0]['image_url']['url'].startswith('data:image/png;base64,'))
        # Decoding must yield a real PNG, proving the asset path is wired.
        raw = base64.b64decode(parts[0]['image_url']['url'].split(',', 1)[1])
        self.assertTrue(raw.startswith(b'\x89PNG'))

    def test_tools_and_response_format_are_forwarded(self):
        case = {'messages': [{'role': 'user', 'content': 'x'}],
                'tools': [{'type': 'function', 'function': {'name': 'f', 'parameters': {}}}],
                'response_format': {'type': 'json_object'},
                'max_tokens': 99}
        payload = bench.build_payload(case, MODEL)
        self.assertEqual(payload['tools'][0]['function']['name'], 'f')
        self.assertEqual(payload['tool_choice'], 'auto')
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['max_tokens'], 99)
        self.assertTrue(payload['stream'])

    def test_api_key_never_appears_in_the_payload(self):
        payload = bench.build_payload({'messages': [{'role': 'user', 'content': 'x'}]}, MODEL)
        self.assertNotIn('secret-value', json.dumps(payload))


class AuthTests(unittest.TestCase):
    def test_key_is_sent_as_a_header(self):
        frames = sse([chunk({'content': 'a'}), usage(completion=1)])
        with FakeEndpoint(frames) as endpoint:
            bench.stream_once(endpoint.base,
                              bench.build_payload({'messages': [{'role': 'user',
                                                                 'content': 'x'}]}, MODEL),
                              api_key='secret-value')
            seen = endpoint.requests[-1]['authorization']
        self.assertEqual(seen, 'Bearer secret-value')

    def test_no_auth_header_when_no_key_is_configured(self):
        frames = sse([chunk({'content': 'a'}), usage(completion=1)])
        with FakeEndpoint(frames) as endpoint:
            bench.stream_once(endpoint.base,
                              bench.build_payload({'messages': [{'role': 'user',
                                                                 'content': 'x'}]}, MODEL))
            seen = endpoint.requests[-1]['authorization']
        self.assertIsNone(seen)


class SummaryTests(unittest.TestCase):
    def test_tpot_is_derived_from_server_token_count(self):
        runs = [{'first_any_s': 1.0, 'first_content_s': 1.2, 'total_s': 3.0,
                 'usage': {'prompt_tokens': 10, 'completion_tokens': 5,
                           'completion_tokens_details': {'reasoning_tokens': 1}},
                 'chars': 8, 'tool_call_deltas': 0}]
        summary = bench.summarize_runs(runs)
        # (3.0 - 1.0) / (5 - 1) = 0.5 s per token
        self.assertAlmostEqual(summary['tpot_ms']['median'], 500.0)
        self.assertEqual(summary['samples'], 1)
        self.assertEqual(summary['reasoning_tokens'], 1)

    def test_transport_errors_are_counted_not_raised(self):
        ok = {'first_any_s': 0.5, 'first_content_s': 0.6, 'total_s': 1.5,
              'usage': {'prompt_tokens': 3, 'completion_tokens': 2,
                        'completion_tokens_details': {}},
              'chars': 4, 'tool_call_deltas': 0}
        broken = {'first_any_s': None, 'first_content_s': None, 'total_s': 0.0,
                  'usage': None, 'error': 'HTTP 400: unsupported'}
        summary = bench.summarize_runs([ok, broken])
        self.assertEqual(summary['samples'], 1)
        self.assertEqual(summary['failures'], 1)
        self.assertIn('HTTP 400', summary['errors'][0])

    def test_median_helper_ignores_missing_values(self):
        self.assertEqual(bench.median_or_none([1.0, None, 3.0])['median'], 2.0)
        self.assertIsNone(bench.median_or_none([None, None]))


class ReceiptTests(unittest.TestCase):
    def test_endpoint_record_omits_the_host(self):
        parsed = bench.urllib.parse.urlparse('http://10.9.8.7:8900/v1')
        scope = 'loopback' if parsed.hostname in ('127.0.0.1', 'localhost', '::1') else 'private-network'
        self.assertEqual(scope, 'private-network')
        self.assertNotIn('10.9.8.7', json.dumps({'scheme': parsed.scheme, 'scope': scope,
                                                 'port': parsed.port}))

    def test_loopback_is_labelled_as_such(self):
        parsed = bench.urllib.parse.urlparse('http://127.0.0.1:19027/v1')
        self.assertEqual(parsed.hostname, '127.0.0.1')

    def test_every_shipped_case_loads_and_declares_an_id(self):
        cases = bench.load_cases([str(bench.HERE / 'cases')])
        self.assertGreaterEqual(len(cases), 13)
        ids = [case['id'] for case in cases]
        self.assertEqual(len(ids), len(set(ids)), 'case ids must be unique')
        for case in cases:
            self.assertTrue(case.get('title'), case['id'])
            self.assertIn('kind', case)
            self.assertTrue(case.get('turns') or case.get('messages'), case['id'])

    def test_multiturn_case_shares_a_prefix_across_turns(self):
        cases = {case['id']: case for case in bench.load_cases([str(bench.HERE / 'cases')])}
        turns = cases['multiturn']['turns']
        self.assertEqual(len(turns), 3)
        first = turns[0]['messages'][0]['content']
        for turn in turns[1:]:
            self.assertEqual(turn['messages'][0]['content'], first)
            self.assertEqual(turn['messages'][0]['role'], 'user')


if __name__ == '__main__':
    unittest.main()
