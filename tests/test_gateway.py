import hashlib
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from deploy_gateway.routing import SessionRouter, Unavailable
from deploy_gateway.server import Gateway


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/v1/models' and self.server.up:
            body = json.dumps({'data': [{'id': 'example', 'max_model_len': self.server.context}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body) + (100 if getattr(self.server, 'metadata_truncate', False) else 0)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200 if self.server.up else 503)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.calls.append({'body': body, 'auth': self.headers.get('Authorization'),
                                  'rank': self.headers.get('X-data-parallel-rank')})
        if self.server.mode == 'disconnect':
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if self.server.mode == 'truncate':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', '999')
            self.end_headers()
            self.wfile.write(b'data: first\n\n')
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        value = json.dumps({'port': self.server.server_port}).encode()
        self.send_response(503 if self.server.mode == 'error' else 200)
        self.send_header('Content-Length', str(len(value)))
        self.end_headers()
        self.wfile.write(value)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Synthetic test material, never a runtime credential.
        self.key = 'unit-test-placeholder'
        self.store = self.root / 'keys.json'
        self.store.write_text(json.dumps({'version': 1, 'records': {'example': {
            'sha256': hashlib.sha256(self.key.encode()).hexdigest(), 'active': True}}}))
        self.models = []
        for _ in range(2):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
            server.daemon_threads = True
            server.up, server.mode, server.calls = True, 'normal', []
            server.context = 1048576
            self.start(server)
            self.models.append(server)
        self.ports = [m.server_port for m in self.models]
        self.gateway = self.make_gateway()

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.addCleanup(cleanup)

    def make_gateway(self):
        server = Gateway(('127.0.0.1', 0), self.store, *self.ports,
                         self.root / 'state.json', self.root / 'bindings.json', 3)
        server.backends.stop.set()  # Tests explicitly advance probes; no timing races.
        server.backends.thread.join(3)
        self.start(server)
        return server

    def request(self, session=None, body=None, token=None, method='POST', gateway=None, path='/v1/chat/completions'):
        server = gateway or self.gateway
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        headers = {'Authorization': 'Bearer ' + (self.key if token is None else token),
                   'Content-Type': 'application/json'}
        if session is not None:
            headers['x-deepseek-harness-session-id'] = session
        payload = json.dumps(body if body is not None else {'messages': [{'role': 'user', 'content': 'example'}]}).encode()
        try:
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
            return response.status, response.getheader('X-DSH-Backend-Port'), response.read()
        finally:
            connection.close()

    def test_balance_sticky_history_auth_and_private_state(self):
        first = self.request('example-session-a')
        second = self.request('example-session-b')
        self.assertEqual([first[0], second[0]], [200, 200])
        self.assertNotEqual(first[1], second[1])
        changed = {'messages': [{'role': 'user', 'content': 'compacted history'},
                                {'role': 'assistant', 'content': 'answer'}]}
        self.assertEqual(self.request('example-session-a', changed)[1], first[1])
        model = self.models[self.ports.index(int(first[1]))]
        self.assertEqual(json.loads(model.calls[-1]['body']), changed)
        self.assertTrue(all(c['auth'] is None for m in self.models for c in m.calls))
        ranks = [c['rank'] for c in model.calls]
        self.assertEqual(ranks[0], ranks[-1])
        self.assertIn(ranks[0], list(map(str, range(8))))
        saved = (self.root / 'bindings.json').read_text()
        for private in (self.key, 'example-session-a', 'example-session-b', 'compacted history'):
            self.assertNotIn(private, saved)
            self.assertNotIn(private, (self.root / 'state.json').read_text())
        self.assertEqual(self.request('bad', token='incorrect-placeholder')[0], 401)
        self.assertEqual(sum(len(m.calls) for m in self.models), 3)

    def test_transport_failover_persists_after_recovery_and_restart(self):
        first = self.request('sticky')
        old = self.models[self.ports.index(int(first[1]))]
        old.mode = 'disconnect'
        failed_over = self.request('sticky')
        self.assertEqual(failed_over[0], 200)
        self.assertNotEqual(first[1], failed_over[1])
        old.mode = 'normal'
        self.gateway.backends.probe()
        self.gateway.backends.probe()
        self.assertEqual(self.request('sticky')[1], failed_over[1])
        self.assertEqual(self.request('new-session')[1], first[1])
        self.gateway.shutdown()
        self.gateway.server_close()
        restarted = self.make_gateway()
        self.assertEqual(self.request('sticky', gateway=restarted)[1], failed_over[1])

    def test_http_error_failover_and_no_healthy_backend(self):
        self.models[0].mode = 'error'
        result = self.request('server-error')
        self.assertEqual(result[:2], (200, str(self.ports[1])))
        self.models[0].up = self.models[1].up = False
        self.gateway.backends.probe()
        self.assertEqual(self.request('down')[0], 503)

    def test_stream_is_never_spliced_after_headers(self):
        self.models[0].mode = 'truncate'
        status, port, body = self.request('stream')
        self.assertEqual((status, port, body), (200, str(self.ports[0]), b'data: first\n\n'))
        self.assertEqual(len(self.models[1].calls), 0)
        self.assertFalse(self.gateway.backends.healthy[self.ports[0]])

    def test_concurrent_sessions_balanced_and_same_session_serialized(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.request('parallel-' + str(i)), range(12)))
        self.assertTrue(all(r[0] == 200 for r in results))
        self.assertEqual([sum(r[1] == str(p) for r in results) for p in self.ports], [6, 6])
        with ThreadPoolExecutor(max_workers=8) as pool:
            same = list(pool.map(lambda _: self.request('one-session'), range(8)))
        self.assertEqual(len({r[1] for r in same}), 1)

    def test_stateless_balance_invalid_session_and_store_failure(self):
        results = [self.request(body={'prompt': 'example'}) for _ in range(4)]
        self.assertEqual([r[1] for r in results], list(map(str, self.ports)) * 2)
        self.assertEqual(self.request('x' * 257)[0], 400)
        with patch('deploy_gateway.routing.os.fsync', side_effect=OSError('test disk failure')):
            self.assertEqual(self.request('disk-failure')[0], 503)
        self.assertEqual(sum(len(m.calls) for m in self.models), 4)
        self.assertEqual(self.gateway.backends.bindings, {})

    def test_key_namespace_and_fallback_history(self):
        initial = self.request()
        history = {'messages': [{'role': 'system', 'content': 'updated'},
                               {'role': 'user', 'content': 'example'},
                               {'role': 'assistant', 'content': 'response'}]}
        self.assertEqual(self.request(body=history)[1], initial[1])
        store = json.loads(self.store.read_text())
        other = 'other-unit-test-placeholder'
        store['records']['other'] = {'active': True, 'sha256': hashlib.sha256(other.encode()).hexdigest()}
        self.store.write_text(json.dumps(store))
        self.request(token=other)
        self.assertEqual(len(self.gateway.backends.bindings), 2)

    def test_long_cache_keys_do_not_collapse_into_one_session(self):
        a = self.request(body={'prompt_cache_key': 'x' * 256 + 'a', 'prompt': 'example'})
        b = self.request(body={'prompt_cache_key': 'x' * 256 + 'b', 'prompt': 'example'})
        self.assertEqual([a[0], b[0]], [200, 200])
        self.assertNotEqual(a[1], b[1])
        self.assertEqual(len(self.gateway.backends.bindings), 2)

    def test_common_model_limit_stays_conservative_after_failover(self):
        self.gateway.model_context_limit = 524288
        for primary_up in (True, False, True):
            self.models[0].up = primary_up
            self.gateway.backends.probe()
            self.gateway.backends.probe()
            status, _, body = self.request(method='GET', path='/v1/models')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['data'][0]['max_model_len'], 524288)
        self.models[0].context = 131072
        self.assertEqual(json.loads(self.request(method='GET', path='/v1/models')[2])['data'][0]['max_model_len'], 131072)

    def test_truncated_metadata_retries_before_headers(self):
        self.gateway.model_context_limit = 524288
        self.models[0].metadata_truncate = True
        status, port, body = self.request(method='GET', path='/v1/models')
        self.assertEqual((status, port), (200, str(self.ports[1])))
        self.assertEqual(json.loads(body)['data'][0]['max_model_len'], 524288)
        self.assertFalse(self.gateway.backends.healthy[self.ports[0]])

    def test_client_cancellation_never_marks_healthy_model_down(self):
        self.gateway.model_context_limit = 524288
        original = socketserver._SocketWriter.write
        for target in ('metadata', 'headers'):
            def disconnect(writer, data):
                front = writer._sock.getsockname()[1] == self.gateway.server_port
                matched = data.startswith(b'{"data":') if target == 'metadata' else data.startswith(b'HTTP/1.1')
                if front and matched:
                    raise BrokenPipeError('synthetic client cancellation')
                return original(writer, data)
            with patch.object(socketserver._SocketWriter, 'write', disconnect):
                if target == 'metadata':
                    self.assertEqual(self.request(method='GET', path='/v1/models')[::2], (200, b''))
                else:
                    with self.assertRaises(HTTPException):
                        self.request('cancelled')
            self.assertTrue(all(self.gateway.backends.healthy.values()))
        self.assertEqual(self.request('cancelled')[1], str(self.ports[0]))


class BindingTests(unittest.TestCase):
    def test_capacity_and_inflight_old_response_cannot_revert_migration(self):
        with tempfile.TemporaryDirectory() as root:
            router = SessionRouter([1234, 5678], Path(root) / 'bindings.json', max_sessions=1)
            router.healthy = {1234: True, 5678: True}
            key = 'a' * 64
            ports, old_generation = router.order(key)
            self.assertEqual(ports[0], 1234)
            router.bind(key, 5678, old_generation)
            self.assertEqual(router.order(key)[0][0], 5678)
            # A second failure can return to A before the original request finishes.
            _, current_generation = router.order(key)
            router.bind(key, 1234, current_generation)
            router.bind(key, 5678, old_generation)
            self.assertEqual(router.order(key)[0][0], 1234)
            with self.assertRaises(Unavailable):
                router.order('b' * 64)
            self.assertEqual(router.order(key)[0][0], 1234)

    def test_invalid_persistence_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'bindings.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                SessionRouter([1234, 5678], path)
            self.assertEqual(path.read_text(), '{broken')


if __name__ == '__main__':
    unittest.main()
