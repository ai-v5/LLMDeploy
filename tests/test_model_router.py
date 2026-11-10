import hashlib
from http.client import HTTPConnection, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from deploy_model_router.server import ModelRouter


class FakeService(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.calls.append(('GET', self.path, self.headers.get('Authorization'), None))
        if self.path == '/health' and not self.server.up:
            self.reply(503, b'{"error":"unavailable"}')
            return
        if self.path == '/v1/models':
            if self.server.model_status:
                self.reply(self.server.model_status, b'{"error":"authentication"}')
                return
            if not self.server.up:
                self.reply(503, b'{"error":"unavailable"}')
                return
            data = {'object': 'list', 'data': [{'id': self.server.model,
                    'object': 'model', 'max_model_len': self.server.context}]}
            self.reply(200, json.dumps(data).encode())
            return
        self.reply(200, b'{"ok":true}')

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.calls.append(('POST', self.path, self.headers.get('Authorization'), raw))
        self.server.seen_headers.append({name.lower(): value for name, value in self.headers.items()})
        value = json.loads(raw)
        if self.server.status_code:
            self.reply(self.server.status_code, b'{"error":"busy"}')
            return
        if not self.server.up:
            self.reply(503, b'{"error":"unavailable"}')
            return
        if value.get('truncate_stream') or self.server.truncate_stream:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            chunk = b'data: {"model":"actual-model"}\n\n'
            self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if value.get('stream'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            event = json.dumps({'model': self.server.model, 'choices': [{'delta': {'content': 'ok'}}]}).encode()
            self.wfile.write(b'data: ' + event + b'\n\n')
            self.wfile.flush()
            if value.get('wait_for_disconnect'):
                self.connection.settimeout(3)
                try:
                    if not self.connection.recv(1):
                        self.server.disconnected.set()
                except OSError:
                    pass
                return
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
            return
        self.reply(200, json.dumps({'model': self.server.model, 'echo': value}).encode())


class ModelRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = 'client-test-placeholder'
        self.backend_key = 'backend-test-placeholder'
        self.store = Path(self.temp.name) / 'keys.json'
        self.store.write_text(json.dumps({'version': 1, 'records': {'test': {
            'sha256': hashlib.sha256(self.key.encode()).hexdigest(), 'active': True}}}))
        self.fallback = self.service('original-model', 1048576)
        self.backend = self.service('actual-model', 524288)
        self.flex = [self.service('flex-model', 1048576) for _ in range(3)]
        routes = [
            {'alias': 'example-alias', 'backend_model': 'actual-model', 'max_context': 524288,
             'backends': [{'origin': f'http://127.0.0.1:{self.backend.server_port}', 'credential_target': 'synthetic'}]},
            {'alias': 'flex-alias', 'backend_model': 'flex-model', 'max_context': 1048576,
             'backends': [{'origin': f'http://127.0.0.1:{s.server_port}'} for s in self.flex]},
        ]
        self.router = ModelRouter(('127.0.0.1', 0), key_store=self.store,
                                  fallback_origin=f'http://127.0.0.1:{self.fallback.server_port}',
                                  routes=routes, key_provider=lambda target: self.backend_key,
                                  upstream_timeout=3)
        self.start(self.router)

    def start(self, server):
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        worker.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            worker.join(3)
        self.addCleanup(cleanup)

    def service(self, model, context):
        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeService)
        server.daemon_threads = True
        server.model, server.context, server.up, server.truncate_stream, server.status_code, server.model_status = model, context, True, False, None, None
        server.calls, server.seen_headers, server.disconnected = [], [], threading.Event()
        self.start(server)
        return server

    def request(self, method, path, body=None, token=None, headers=None):
        conn = HTTPConnection('127.0.0.1', self.router.server_port, timeout=5)
        hdr = {'Authorization': 'Bearer ' + (self.key if token is None else token),
               'Content-Type': 'application/json'}
        hdr.update(headers or {})
        raw = json.dumps(body).encode() if body is not None else None
        try:
            conn.request(method, path, raw, hdr)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_auth_and_model_specific_credential_and_name(self):
        status, _ = self.request('POST', '/v1/chat/completions', {'model': 'example-alias'}, token='wrong')
        self.assertEqual(status, 401)
        self.assertFalse(self.backend.calls)
        payload = {'model': 'example-alias', 'messages': [{'role': 'user', 'content': 'hello'}]}
        status, raw = self.request('POST', '/v1/chat/completions', payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['model'], 'example-alias')
        self.assertEqual(json.loads(raw)['echo']['model'], 'actual-model')
        self.assertEqual(self.backend.calls[-1][2], 'Bearer ' + self.backend_key)
        self.assertFalse(self.fallback.calls)

    def test_existing_model_is_forwarded_unchanged(self):
        payload = {'model': 'original-model', 'stream': False, 'messages': [{'role': 'user', 'content': 'hi'}]}
        status, body = self.request('POST', '/v1/chat/completions', payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['echo'], payload)
        self.assertEqual(self.fallback.calls[-1][2], 'Bearer ' + self.key)
        self.assertFalse(self.backend.calls)

    def test_backend_does_not_receive_other_client_credentials(self):
        headers = {'Cookie': 'synthetic-cookie', 'X-Api-Key': 'synthetic-api-key',
                   'X-Other-Token': 'synthetic-token'}
        self.assertEqual(self.request('POST', '/v1/chat/completions',
                                      {'model': 'example-alias'}, headers=headers)[0], 200)
        seen = self.backend.seen_headers[-1]
        self.assertEqual(seen['authorization'], 'Bearer ' + self.backend_key)
        self.assertNotIn('cookie', seen)
        self.assertNotIn('x-api-key', seen)
        self.assertNotIn('x-other-token', seen)

    def test_model_list_and_context(self):
        status, raw = self.request('GET', '/v1/models')
        self.assertEqual(status, 200)
        models = {m['id']: m for m in json.loads(raw)['data']}
        self.assertEqual(set(models), {'original-model', 'example-alias', 'flex-alias'})
        self.assertEqual(models['original-model']['max_model_len'], 1048576)
        self.assertEqual(models['example-alias']['max_model_len'], 524288)
        self.assertFalse(self.backend.calls)
        status, detail = self.request('GET', '/v1/models/example-alias')
        self.assertEqual((status, json.loads(detail)['id']), (200, 'example-alias'))
        self.assertEqual(self.request('POST', '/v1/chat/completions', {'model': 'example-alias'},
                                      headers={'x-model-context-window': '524289'})[0], 503)

    def test_unavailable_original_does_not_block_new_models(self):
        self.fallback.up = False
        status, raw = self.request('GET', '/v1/models')
        self.assertEqual(status, 200)
        self.assertEqual({x['id'] for x in json.loads(raw)['data']}, {'example-alias', 'flex-alias'})
        self.assertEqual(self.request('GET', '/health')[0], 503)
        self.assertFalse(self.backend.calls)
        self.assertEqual(self.request('POST', '/v1/completions', {'model': 'example-alias'})[0], 200)
        self.assertEqual(self.request('GET', '/v1/models/example-alias')[0], 200)

    def test_existing_gateway_auth_error_is_not_masked(self):
        self.fallback.model_status = 401
        self.assertEqual(self.request('GET', '/v1/models')[0], 401)

    def test_catalog_is_configured_and_does_not_wait_for_backend(self):
        def unavailable(target):
            raise OSError('credential unavailable')
        self.router.key_provider = unavailable
        status, raw = self.request('GET', '/v1/models')
        self.assertEqual(status, 200)
        self.assertIn('example-alias', [x['id'] for x in json.loads(raw)['data']])
        self.assertNotIn('actual-model', raw.decode())
        self.assertEqual(self.request('POST', '/v1/completions', {'model': 'example-alias'})[0], 502)
        self.assertFalse(self.backend.calls)
        self.assertEqual(len(self.fallback.calls), 1)

    def test_flex_balances_sessions_and_rebinds_on_failure(self):
        body = {'model': 'flex-alias', 'messages': [{'role': 'user', 'content': 'hello'}]}
        for i in range(3):
            status, raw = self.request('POST', '/v1/chat/completions', body, headers={'x-session-id': f'session-{i}'})
            self.assertEqual((status, json.loads(raw)['model']), (200, 'flex-alias'))
        self.assertEqual([len(s.calls) for s in self.flex], [1, 1, 1])
        self.assertTrue(all(s.calls[0][2] is None for s in self.flex))
        self.assertTrue(all(json.loads(s.calls[0][3])['model'] == 'flex-model' for s in self.flex))
        self.flex[0].up = False
        status, raw = self.request('POST', '/v1/chat/completions', body, headers={'x-session-id': 'session-0'})
        self.assertEqual((status, json.loads(raw)['model']), (200, 'flex-alias'))
        counts = [len(s.calls) for s in self.flex]
        self.request('POST', '/v1/chat/completions', body, headers={'x-session-id': 'session-0'})
        self.assertEqual(len(self.flex[0].calls), counts[0])

    def test_alias_sse_stream_and_response_name(self):
        status, raw = self.request('POST', '/v1/chat/completions', {'model': 'example-alias', 'stream': True})
        self.assertEqual(status, 200)
        lines = raw.splitlines()
        self.assertEqual(json.loads(lines[0][5:])['model'], 'example-alias')
        self.assertIn(b'data: [DONE]', raw)

    def test_client_cancel_closes_upstream(self):
        conn = HTTPConnection('127.0.0.1', self.router.server_port, timeout=5)
        body = json.dumps({'model': 'example-alias', 'stream': True, 'wait_for_disconnect': True}).encode()
        conn.request('POST', '/v1/chat/completions', body,
                     {'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.readline().startswith(b'data:'))
        response.close()
        conn.close()
        self.assertTrue(self.backend.disconnected.wait(3))

    def test_truncated_sse_is_not_a_successful_eof(self):
        conn = HTTPConnection('127.0.0.1', self.router.server_port, timeout=5)
        body = json.dumps({'model': 'example-alias', 'stream': True, 'truncate_stream': True}).encode()
        conn.request('POST', '/v1/chat/completions', body,
                     {'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        with self.assertRaises(IncompleteRead):
            response.read()
        conn.close()

    def test_stream_failure_rebinds_only_the_next_request(self):
        self.flex[0].truncate_stream = True
        body = {'model': 'flex-alias', 'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]}
        with self.assertRaises(IncompleteRead):
            self.request('POST', '/v1/chat/completions', body, headers={'x-session-id': 'sticky'})
        self.assertEqual([len(s.calls) for s in self.flex], [1, 0, 0])
        status, data = self.request('POST', '/v1/chat/completions', body, headers={'x-session-id': 'sticky'})
        self.assertEqual(status, 200)
        self.assertIn(b'data: [DONE]', data)
        self.assertEqual([len(s.calls) for s in self.flex], [1, 1, 0])

    def test_flex_retries_explicit_capacity_rejection_and_reports_full_pool(self):
        body = {'model': 'flex-alias', 'messages': [{'role': 'user', 'content': 'hello'}]}
        self.flex[0].status_code = 429
        self.assertEqual(self.request('POST', '/v1/chat/completions', body, headers={'x-session-id': 'one'})[0], 200)
        self.assertEqual([len(s.calls) for s in self.flex], [1, 1, 0])
        self.flex[1].status_code = self.flex[2].status_code = 429
        self.assertEqual(self.request('POST', '/v1/chat/completions', body, headers={'x-session-id': 'two'})[0], 429)
        self.assertEqual(self.request('POST', '/v1/chat/completions', body, headers={'x-session-id': 'three'})[0], 429)

    def test_request_admission_rejects_before_reading_or_forwarding(self):
        self.router.body_limit = 1
        self.assertEqual(self.request('POST', '/v1/chat/completions', {'model': 'example-alias'})[0], 429)
        self.assertEqual(self.router.body_bytes, 0)
        self.assertFalse(self.backend.calls)


if __name__ == '__main__':
    unittest.main()
