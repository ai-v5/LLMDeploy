import hashlib
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from deploy_gateway.routing import SessionRouter, Unavailable
from deploy_gateway.server import Gateway, main


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


class Readiness(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.paths.append(self.path)
        self.send_response(self.server.status)
        body = self.server.instance_body if self.path == '/instances' and hasattr(self.server, 'instance_body') else self.server.body
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class GatewayCliTests(unittest.TestCase):
    def test_statistics_paths_cannot_overlap_gateway_state_or_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ['gateway', '--key-store', str(root/'keys'), '--bindings-file', str(root/'bindings'),
                    '--backend-state', str(root/'state.json'), '--control-file', str(root/'control')]
            for database in (root/'state.json', root/'state.new', root/'bindings.new', root/'control.new', root/'keys'):
                with self.subTest(database=database), patch.object(sys, 'argv', base + ['--statistics-database', str(database)]), \
                        patch('deploy_stats.inprocess.install') as install, patch('sys.stderr'), self.assertRaises(SystemExit):
                    main()
                install.assert_not_called()
                self.assertFalse(database.exists())
            database = root/'metrics'
            sidecar_state = root/'metrics-wal'
            base = ['gateway', '--key-store', str(root/'keys'), '--bindings-file', str(root/'bindings'),
                    '--backend-state', str(sidecar_state), '--statistics-database', str(database)]
            with patch.object(sys, 'argv', base), patch('deploy_stats.inprocess.install') as install, \
                    patch('sys.stderr'), self.assertRaises(SystemExit):
                main()
            install.assert_not_called()
            self.assertFalse(database.exists())

    def test_statistics_database_cannot_be_hardlink_to_runtime_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, database = root/'state.json', root/'usage.sqlite3'
            state.write_text('synthetic state', encoding='utf-8')
            try:
                database.hardlink_to(state)
            except OSError:
                self.skipTest('Hard links are not supported by the test filesystem')
            argv = ['gateway', '--key-store', str(root/'keys'), '--bindings-file', str(root/'bindings'),
                    '--backend-state', str(state), '--statistics-database', str(database)]
            with patch.object(sys, 'argv', argv), patch('deploy_stats.inprocess.install') as install, \
                    patch('sys.stderr'), self.assertRaises(SystemExit):
                main()
            install.assert_not_called()
            self.assertEqual(state.read_text(encoding='utf-8'), 'synthetic state')

    def test_statistics_recorder_is_tagged_and_closed(self):
        argv = ['gateway', '--key-store', 'keys.json', '--bindings-file', 'bindings.json',
                '--backend-state', 'state.json', '--statistics-database', 'usage.sqlite3']
        with patch.object(sys, 'argv', argv), patch('deploy_gateway.server.Gateway') as gateway, \
                patch('deploy_stats.inprocess.install') as install, patch('builtins.print'):
            main()
        self.assertEqual(install.call_args.kwargs['source'], 'gateway-inprocess-v1')
        install.return_value.close.assert_called_once()
        gateway.return_value.server_close.assert_called_once()

    def test_statistics_setup_failure_does_not_stop_gateway(self):
        argv = ['gateway', '--key-store', 'keys.json', '--bindings-file', 'bindings.json',
                '--backend-state', 'state.json', '--statistics-database', 'usage.sqlite3']
        with patch.object(sys, 'argv', argv), patch('deploy_gateway.server.Gateway') as gateway, \
                patch('deploy_stats.inprocess.install', side_effect=OSError), patch('builtins.print'):
            main()
        gateway.return_value.serve_forever.assert_called_once()

    def test_repeated_management_mappings_reach_gateway(self):
        argv = ['gateway', '--key-store', 'keys.json', '--bindings-file', 'bindings.json',
                '--backend-state', 'state.json', '--backend-port', '19001', '--backend-port', '19002',
                '--backend-readiness-port', '19001=19101', '--backend-readiness-port', '19002=19102',
                '--backend-pd-model', 'example', '--backend-pd-probe-endpoints',
                '--statistics-database', 'usage.sqlite3']
        with patch.object(sys, 'argv', argv), patch('deploy_gateway.server.Gateway') as gateway, \
                patch('deploy_stats.inprocess.install') as install, patch('builtins.print'):
            main()
        self.assertEqual(gateway.call_args.kwargs['backend_readiness_ports'], {19001: 19101, 19002: 19102})
        self.assertEqual(gateway.call_args.kwargs['pd_model'], 'example')
        self.assertTrue(gateway.call_args.kwargs['pd_probe_endpoints'])
        self.assertEqual(install.call_args.kwargs['source'], 'gateway-inprocess-v1')
        install.return_value.close.assert_called_once()
        gateway.return_value.serve_forever.assert_called_once()

    def test_duplicate_or_malformed_management_mapping_is_rejected(self):
        base = ['gateway', '--key-store', 'keys.json', '--bindings-file', 'bindings.json',
                '--backend-state', 'state.json']
        for suffix in [['--backend-readiness-port', '19001'],
                       ['--backend-readiness-port', '19001=19101', '--backend-readiness-port', '19001=19102']]:
            with self.subTest(suffix=suffix), patch.object(sys, 'argv', base + suffix), \
                    patch('sys.stderr'), self.assertRaises(SystemExit):
                main()


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

    def request(self, session=None, body=None, token=None, method='POST', gateway=None, path='/v1/chat/completions', extra_headers=None):
        server = gateway or self.gateway
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        headers = {'Authorization': 'Bearer ' + (self.key if token is None else token),
                   'Content-Type': 'application/json'}
        if session is not None:
            headers['x-deepseek-harness-session-id'] = session
        headers.update(extra_headers or {})
        payload = None if body is None and method in ('GET','HEAD') else json.dumps(
            body if body is not None else {'messages': [{'role': 'user', 'content': 'example'}]}).encode()
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

    def test_management_readiness_controls_routing_and_recovery(self):
        management = ThreadingHTTPServer(('127.0.0.1', 0), Readiness)
        management.daemon_threads = True
        management.status, management.body, management.paths = 200, b'{"ready":false}', []
        self.start(management)
        gateway = Gateway(('127.0.0.1', 0), self.store, *self.ports,
                          self.root / 'motor-state.json', self.root / 'motor-bindings.json', 3,
                          backend_readiness_ports={self.ports[0]: management.server_port})
        gateway.backends.stop.set()
        gateway.backends.thread.join(3)
        self.start(gateway)

        self.assertFalse(gateway.backends.healthy[self.ports[0]])
        self.assertTrue(gateway.backends.healthy[self.ports[1]])
        self.assertEqual(self.request('management-unready', gateway=gateway)[1], str(self.ports[1]))
        self.assertEqual(management.paths, ['/readiness'])

        management.body = b'{"ready":true}'
        gateway.backends.probe()
        self.assertFalse(gateway.backends.healthy[self.ports[0]])
        gateway.backends.probe()
        self.assertTrue(gateway.backends.healthy[self.ports[0]])

        for status, body in [(200, b'{"ready":false}'), (200, b'{"ready":1}'),
                             (200, b'{"ready":"true"}'), (200, b'[]'),
                             (200, b'invalid'), (200, b' ' * 65537),
                             (503, b'{"ready":true}')]:
            with self.subTest(status=status, body=body[:30]):
                management.status, management.body = status, body
                gateway.backends.probe()
                self.assertFalse(gateway.backends.healthy[self.ports[0]])
                self.assertTrue(gateway.backends.healthy[self.ports[1]])
                management.status, management.body = 200, b'{"ready":true}'
                gateway.backends.probe()
                gateway.backends.probe()
                self.assertTrue(gateway.backends.healthy[self.ports[0]])

    def test_invalid_management_readiness_mapping_is_rejected(self):
        bad_mappings = [{self.ports[0]: 0}, {self.ports[0]: 65536},
                        {self.ports[0]: True}, {self.ports[0]: '1234'},
                        {0: 1234}, {True: 1234}]
        for mapping in bad_mappings:
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                Gateway(('127.0.0.1', 0), self.store, *self.ports,
                        self.root / 'invalid-state.json', self.root / 'invalid-bindings.json',
                        backend_readiness_ports=mapping)

    def test_motor_pd_requires_healthy_prefill_and_decode_despite_ready(self):
        management = ThreadingHTTPServer(('127.0.0.1', 0), Readiness)
        management.daemon_threads = True
        management.status, management.body, management.paths = 200, b'{"ready":true}', []
        prefill = {'role': 'prefill', 'pool': 'available', 'status': 'active', 'healthy': True, 'model_name': 'example'}
        decode = {'role': 'decode', 'pool': 'available', 'status': 'active', 'healthy': True, 'model_name': 'example'}
        management.instance_body = json.dumps({'instances': [prefill]}).encode()
        self.start(management)
        backup_management = ThreadingHTTPServer(('127.0.0.1', 0), Readiness)
        backup_management.daemon_threads = True
        backup_management.status, backup_management.body, backup_management.paths = 200, b'{"ready":true}', []
        backup_management.instance_body = json.dumps({'instances': [prefill, decode]}).encode()
        self.start(backup_management)
        gateway = Gateway(('127.0.0.1', 0), self.store, *self.ports,
                          self.root / 'pd-state.json', self.root / 'pd-bindings.json', 3,
                          backend_readiness_ports={self.ports[0]: management.server_port,
                                                   self.ports[1]: backup_management.server_port},
                          pd_model='example')
        gateway.backends.stop.set()
        gateway.backends.thread.join(3)
        self.start(gateway)
        self.assertFalse(gateway.backends.healthy[self.ports[0]])
        self.assertEqual(self.request('only-prefill', gateway=gateway)[1], str(self.ports[1]))

        for instances in ([decode], [prefill, {**decode, 'healthy': False}],
                          [prefill, {**decode, 'pool': 'unavailable'}],
                          [prefill, {**decode, 'healthy': 1}],
                          [prefill, {**decode, 'model_name': 'other'}],
                          [prefill, {**decode, 'role': []}], []):
            with self.subTest(instances=instances):
                management.instance_body = json.dumps({'instances': instances}).encode()
                gateway.backends.probe()
                self.assertFalse(gateway.backends.healthy[self.ports[0]])
        for body in (b'invalid', b'[]', b'{"instances":{}}', b' ' * 1048577):
            management.instance_body = body
            gateway.backends.probe()
            self.assertFalse(gateway.backends.healthy[self.ports[0]])

        management.instance_body = json.dumps({'instances': [prefill, decode]}).encode()
        gateway.backends.probe()
        self.assertFalse(gateway.backends.healthy[self.ports[0]])
        gateway.backends.probe()
        self.assertTrue(gateway.backends.healthy[self.ports[0]])

    def test_pd_model_requires_all_management_mappings(self):
        with self.assertRaises(ValueError):
            Gateway(('127.0.0.1', 0), self.store, *self.ports,
                    self.root / 'pd-invalid-state.json', self.root / 'pd-invalid-bindings.json',
                    backend_readiness_ports={self.ports[0]: 12345}, pd_model='example')
        with self.assertRaises(ValueError):
            Gateway(('127.0.0.1', 0), self.store, *self.ports,
                    self.root / 'pd-invalid-state.json', self.root / 'pd-invalid-bindings.json',
                    pd_probe_endpoints=True)

    def test_pd_endpoint_probe_rejects_stale_motor_health_and_accepts_a_live_alternative(self):
        management = ThreadingHTTPServer(('127.0.0.1', 0), Readiness)
        management.daemon_threads = True
        management.status, management.body, management.paths = 200, b'{"ready":true}', []
        self.start(management)
        endpoint = lambda port: {'ip': '127.0.0.1', 'business_port': str(port), 'headless': False}
        row = lambda role, port: {'role': role, 'pool': 'available', 'status': 'active',
                                  'healthy': True, 'model_name': 'example', 'endpoints': [endpoint(port)]}
        prefill, decode = row('prefill', self.ports[0]), row('decode', self.ports[1])
        management.instance_body = json.dumps({'instances': [prefill, decode]}).encode()
        self.models[1].up = False
        gateway = Gateway(('127.0.0.1', 0), self.store, self.ports[0], self.ports[0] + 1,
                          self.root / 'endpoint-state.json', self.root / 'endpoint-bindings.json', 3,
                          backend_ports=[self.ports[0]],
                          backend_readiness_ports={self.ports[0]: management.server_port},
                          pd_model='example', pd_probe_endpoints=True)
        gateway.backends.stop.set()
        gateway.backends.thread.join(3)
        self.start(gateway)
        self.assertFalse(gateway.backends.healthy[self.ports[0]])

        management.instance_body = json.dumps({'instances': [prefill, decode, row('decode', self.ports[0])]}).encode()
        gateway.backends.probe()
        gateway.backends.probe()
        self.assertTrue(gateway.backends.healthy[self.ports[0]])
        for invalid in ([], [{'ip': [], 'business_port': '123'}], [endpoint(0)]):
            management.instance_body = json.dumps({'instances': [prefill, {**decode, 'endpoints': invalid}]}).encode()
            gateway.backends.probe()
            gateway.backends.probe()
            self.assertFalse(gateway.backends.healthy[self.ports[0]])

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
        self.assertEqual({r[1] for r in results}, set(map(str, self.ports)))
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

    def configure_contexts(self):
        self.gateway.model_context_limit = 524288
        self.gateway.backend_context_limits = dict(zip(self.ports, [524288, 1048576]))
        self.models[0].context = 524288

    def test_context_eligibility_migrates_and_never_falls_back_to_short_node(self):
        self.configure_contexts()
        self.assertEqual(self.request('context-session')[1], str(self.ports[0]))
        header = {'x-model-context-window': '1048576'}
        self.assertEqual(self.request('context-session', extra_headers=header)[1], str(self.ports[1]))
        self.assertEqual(self.request('context-session')[1], str(self.ports[1]))
        self.models[1].up = False
        self.gateway.backends.probe()
        count = len(self.models[0].calls)
        self.assertEqual(self.request('context-session', extra_headers=header)[0], 503)
        self.assertEqual(len(self.models[0].calls), count)
        self.assertEqual(self.request('ordinary')[1], str(self.ports[0]))

    def test_context_header_controls_metadata_without_expanding_normal_catalog(self):
        self.configure_contexts()
        ordinary = self.request(method='GET', path='/v1/models')
        long = self.request(method='GET', path='/v1/models', extra_headers={'x-model-context-window': '1048576'})
        self.assertEqual(json.loads(ordinary[2])['data'][0]['max_model_len'], 524288)
        self.assertEqual((long[0], long[1]), (200, str(self.ports[1])))
        self.assertEqual(json.loads(long[2])['data'][0]['max_model_len'], 1048576)

    def test_invalid_or_unsupported_context_does_not_reach_models(self):
        self.configure_contexts()
        for value, expected in [('1M', 400), ('0', 400), ('999999999999999', 400), ('1048577', 503)]:
            self.assertEqual(self.request('bad-context', extra_headers={'x-model-context-window': value})[0], expected)
        self.assertEqual(sum(len(m.calls) for m in self.models), 0)

    def test_context_config_rejects_partial_or_overstated_capabilities(self):
        for limits, common in [({self.ports[0]:1048576}, 524288), (dict(zip(self.ports,[524288,-1])),524288),
                               (dict(zip(self.ports,[524288,1048576])),1048576)]:
            with self.assertRaises(ValueError):
                Gateway(('127.0.0.1',0), self.store, *self.ports, self.root/'other-state.json',self.root/'other-bindings.json',
                        model_context_limit=common, backend_context_limits=limits)


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
