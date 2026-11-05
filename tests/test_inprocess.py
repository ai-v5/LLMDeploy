import hashlib
from http.client import HTTPConnection, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from email.message import Message
from unittest.mock import patch

import deploy_gateway.server as gateway_module
from deploy_common.reject_drain import MAX_REJECTIONS
from deploy_gateway.routing import affinity_key
from deploy_stats import inprocess
from deploy_stats.capture import MAX_FRAME, UsageCapture, extract_usage
from deploy_stats.inprocess import _Observer, install
from deploy_stats.report import report
from deploy_stats.session import BodyBudget
from deploy_stats.web import Dashboard
from tests.caddy_helpers import synthetic_caddy_owns_listener


STREAM = (b'data: {"choices":[{"delta":{"content":"synthetic"}}]}\n\n'
          b'data: {"usage":{"prompt_tokens":100,"completion_tokens":7,"total_tokens":107,'
          b'"prompt_tokens_details":{"cached_tokens":60}}}\n\n' b'data: [DONE]\n\n')


class Model(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.calls.append(body)
        mode = self.server.mode
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(STREAM) + (20 if mode == 'truncate' else 0)))
        self.end_headers()
        if mode == 'slow':
            self.wfile.write(STREAM[:40])
            self.wfile.flush()
            self.server.started.set()
            self.server.release.wait(5)
            try:
                self.wfile.write(STREAM[40:])
                self.wfile.flush()
            except OSError:
                self.server.cancelled.set()
            return
        self.wfile.write(STREAM)
        self.wfile.flush()
        if mode == 'truncate':
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            self.close_connection = True


class InProcessCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = 'synthetic-gateway-key'
        self.keys = self.root/'keys.json'
        self.keys.write_text(json.dumps({'version': 1, 'records': {'fixture': {
            'sha256': hashlib.sha256(self.key.encode()).hexdigest(), 'active': True}}}))
        self.model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        self.model.daemon_threads = True
        self.model.calls = []
        self.model.mode = 'normal'
        self.model.started, self.model.release, self.model.cancelled = (threading.Event() for _ in range(3))
        self.start(self.model)
        self.original_handler = gateway_module.Handler
        self.baseline = self.gateway('baseline')
        self.recorder = install(gateway_module, self.root/'usage.sqlite3')
        self.assertIsNotNone(self.recorder)
        self.addCleanup(self.recorder.close)
        self.addCleanup(setattr, gateway_module, 'Handler', self.original_handler)
        self.gateway_under_test = self.gateway('observed')
        gateway_module.Handler = self.original_handler

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.addCleanup(close)

    def gateway(self, name):
        server = gateway_module.Gateway(('127.0.0.1', 0), self.keys,
            state_path=self.root/(name+'-state.json'), bindings_file=self.root/(name+'-bindings.json'),
            backend_ports=[self.model.server_port], backend_managed_affinity=True)
        self.start(server)
        return server

    def request(self, server, payload, headers=None):
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        try:
            connection.request('POST', '/v1/chat/completions', payload, headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def wait_recorded(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with self.recorder.live_lock:
                if not self.recorder.live:
                    self.assertTrue(self.recorder.flush())
                    return
            time.sleep(.01)
        self.fail('observer did not finish')

    def test_response_and_session_match_existing_gateway_without_storing_content(self):
        payload = b'{"messages":[{"role":"user","content":"synthetic-private-prompt"}]}'
        headers = {'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json',
                   'X-Forwarded-For': '192.0.2.4'}
        self.assertEqual(self.request(self.baseline, payload, headers), (200, STREAM))
        self.assertEqual(self.request(self.gateway_under_test, payload, headers), (200, STREAM))
        self.wait_recorded()
        result = report(self.recorder.path, 'total', now=time.time() + .01)
        self.assertEqual((result['totals']['known'], result['totals']['input'],
                          result['totals']['output'], result['totals']['cached']), (1, 100, 7, 60))
        self.assertEqual(result['requests'][0]['ip'], '192.0.2.4')
        message = Message()
        message['Authorization'] = 'Bearer ' + self.key
        digest = hashlib.sha256(self.key.encode()).hexdigest()
        self.assertEqual(result['requests'][0]['session'], affinity_key(message, payload, digest))
        self.assertIsNotNone(result['requests'][0]['first_visible'])
        for file in self.root.glob('usage.sqlite3*'):
            data = file.read_bytes()
            for private in (self.key.encode(), b'synthetic-private-prompt'):
                self.assertNotIn(private, data)

    def test_inprocess_source_uses_only_single_caddy_forwarded_address(self):
        headers = {'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json',
                   'X-Forwarded-For': '192.0.2.4', 'X-LLMDeploy-Client-IP': '198.51.100.9'}
        self.assertEqual(self.request(self.gateway_under_test, b'{}', headers), (200, STREAM))
        self.wait_recorded()
        result = report(self.recorder.path, 'total', now=time.time() + .01)
        self.assertEqual(result['requests'][0]['ip'], '192.0.2.4')
        headers.pop('X-Forwarded-For')
        self.assertEqual(self.request(self.gateway_under_test, b'{}', headers), (200, STREAM))
        self.wait_recorded()
        result = report(self.recorder.path, 'total', now=time.time() + .01)
        self.assertEqual(result['totals']['unknown_source'], 1)
        for forwarded in ('192.0.2.4, 198.51.100.2', 'not-an-ip'):
            headers['X-Forwarded-For'] = forwarded
            self.assertEqual(self.request(self.gateway_under_test, b'{}', headers), (200, STREAM))
            self.wait_recorded()
        result = report(self.recorder.path, 'total', now=time.time() + .01)
        self.assertEqual(result['totals']['unknown_source'], 3)
        message = Message()
        message['X-Forwarded-For'] = '192.0.2.4'
        message['X-Forwarded-For'] = '198.51.100.2'
        observer = _Observer(self.recorder, BodyBudget(), message, 'chat')
        self.assertIsNone(observer.source)
        observer.finish()

    @unittest.skipUnless(os.environ.get('LLMDEPLOY_CADDY'), 'Set LLMDEPLOY_CADDY for Caddy integration')
    def test_caddy_default_forwarding_overwrites_spoofed_client_addresses(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            front = probe.getsockname()[1]
        config = self.root/'Caddyfile-default-xff'
        config.write_text('{\n admin off\n auto_https off\n}\n'
            f'http://127.0.0.1:{front} {{\n bind 127.0.0.1\n'
            f' reverse_proxy 127.0.0.1:{self.gateway_under_test.server_port}\n}}\n', encoding='utf-8')
        env = dict(os.environ, XDG_DATA_HOME=str(self.root), XDG_CONFIG_HOME=str(self.root),
                   APPDATA=str(self.root), LOCALAPPDATA=str(self.root))
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        caddy = subprocess.Popen([os.environ['LLMDEPLOY_CADDY'], 'run', '--config', str(config),
            '--adapter', 'caddyfile'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, creationflags=flags)
        try:
            deadline = time.monotonic() + 5
            while not synthetic_caddy_owns_listener(caddy, front):
                if caddy.poll() is not None or time.monotonic() >= deadline:
                    self.fail('synthetic Caddy listener ownership could not be verified')
                time.sleep(.02)
            cases = ([], [('X-Forwarded-For', '192.0.2.55')],
                     [('X-Forwarded-For', '192.0.2.55, 198.51.100.8')],
                     [('X-Forwarded-For', '192.0.2.55'), ('x-forwarded-for', '198.51.100.8')],
                     [('X-LLMDeploy-Client-IP', '192.0.2.55')])
            for extra in cases:
                self.assertTrue(synthetic_caddy_owns_listener(caddy, front))
                conn = HTTPConnection('127.0.0.1', front, timeout=5)
                try:
                    conn.putrequest('POST', '/v1/chat/completions')
                    conn.putheader('Authorization', 'Bearer '+self.key)
                    conn.putheader('Content-Length', '2')
                    for name, value in extra:
                        conn.putheader(name, value)
                    conn.endheaders(b'{}')
                    response = conn.getresponse()
                    self.assertEqual((response.status, response.read()), (200, STREAM))
                finally:
                    conn.close()
            self.wait_recorded()
            result = report(self.recorder.path, 'total', now=time.time() + .01)
            self.assertEqual(result['totals']['known'], len(cases))
            self.assertEqual(result['totals']['unknown_source'], 0)
            self.assertEqual({item['ip'] for item in result['clients']}, {'127.0.0.1'})
        finally:
            if caddy.poll() is None:
                caddy.terminate()
            caddy.wait(timeout=5)

    def test_dashboard_api_updates_after_successive_gateway_requests(self):
        dashboard = Dashboard(0, self.recorder.path)
        self.start(dashboard)
        headers = {'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json',
                   'X-Forwarded-For': '192.0.2.4'}
        def stats():
            conn = HTTPConnection('127.0.0.1', dashboard.server_port, timeout=5)
            try:
                conn.request('GET', '/api/stats?window=total')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                return json.loads(response.read())
            finally:
                conn.close()
        self.assertEqual(stats()['totals']['known'], 0)
        for expected in (1, 2):
            self.assertEqual(self.request(self.gateway_under_test, b'{}', headers), (200, STREAM))
            self.wait_recorded()
            deadline = time.monotonic() + 4
            value = stats()
            while value['totals']['known'] < expected and time.monotonic() < deadline:
                time.sleep(.02)
                value = stats()
            self.assertEqual((value['totals']['known'], value['totals']['input'], value['totals']['output'],
                              value['totals']['cached']), (expected, 100*expected, 7*expected, 60*expected))

    def test_motor_readiness_tagged_capture_and_dashboard_end_to_end(self):
        class Readiness(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                body = self.server.instance_body if self.path == '/instances' else b'{"ready":true}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        readiness = ThreadingHTTPServer(('127.0.0.1', 0), Readiness)
        endpoint = {'ip': '127.0.0.1', 'business_port': str(self.model.server_port), 'headless': False}
        readiness.instance_body = json.dumps({'instances': [
            {'role': role, 'pool': 'available', 'status': 'active', 'healthy': True,
             'model_name': 'example', 'endpoints': [endpoint]}
            for role in ('prefill', 'decode')]}).encode()
        self.start(readiness)
        database = self.root/'motor-usage.sqlite3'
        old_handler = gateway_module.Handler
        recorder = install(gateway_module, database, source='gateway-inprocess-v1')
        self.assertIsNotNone(recorder)
        self.addCleanup(recorder.close)
        try:
            gateway = gateway_module.Gateway(('127.0.0.1', 0), self.keys,
                state_path=self.root/'motor-state.json', bindings_file=self.root/'motor-bindings.json',
                backend_ports=[self.model.server_port], backend_managed_affinity=True,
                backend_readiness_ports={self.model.server_port: readiness.server_port},
                pd_model='example', pd_probe_endpoints=True)
        finally:
            gateway_module.Handler = old_handler
        self.start(gateway)
        self.assertTrue(gateway.backends.healthy[self.model.server_port])
        with patch('deploy_stats.web.MotorCollector') as collector:
            collector.return_value.snapshot.return_value = {'sampled_at': time.time(), 'nodes': []}
            dashboard = Dashboard(0, database, motor_management_ports=[readiness.server_port])
        self.start(dashboard)
        headers = {'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json',
                   'X-Forwarded-For': '192.0.2.4'}
        self.assertEqual(self.request(gateway, b'{}', headers), (200, STREAM))
        self.assertTrue(recorder.flush())
        deadline = time.monotonic() + 4
        value = report(database, 'total', source='gateway-inprocess-v1')
        while value['totals']['known'] < 1 and time.monotonic() < deadline:
            time.sleep(.02)
            value = report(database, 'total', source='gateway-inprocess-v1')
        self.assertTrue(value['collector_live'])
        self.assertEqual((value['totals']['input'], value['totals']['output'], value['totals']['cached']), (100, 7, 60))
        conn = HTTPConnection('127.0.0.1', dashboard.server_port, timeout=5)
        try:
            conn.request('GET', '/api/v5/stats?window=total')
            response = conn.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertTrue(payload['collector_live'])
            self.assertEqual(payload['requests'][0]['ip'], '192.0.2.4')
            self.assertEqual(payload['totals']['known'], 1)
        finally:
            conn.close()

    def test_vllm_stream_without_cache_details_records_partial_usage(self):
        old_handler = gateway_module.Handler
        recorder = install(gateway_module, self.root/'partial-usage.sqlite3', source='gateway-inprocess-v1')
        self.assertIsNotNone(recorder)
        self.addCleanup(recorder.close)
        try:
            gateway = gateway_module.Gateway(('127.0.0.1', 0), self.keys,
                state_path=self.root/'partial-state.json', bindings_file=self.root/'partial-bindings.json',
                backend_ports=[self.model.server_port], backend_managed_affinity=True)
        finally:
            gateway_module.Handler = old_handler
        self.start(gateway)
        partial_stream = (b'data: {"choices":[{"delta":{"content":"synthetic"}}]}\n\n'
                          b'data: {"usage":{"prompt_tokens":120,"completion_tokens":20,"total_tokens":140}}\n\n'
                          b'data: [DONE]\n\n')
        headers = {'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json',
                   'X-Forwarded-For': '192.0.2.4'}
        with patch(f'{__name__}.STREAM', partial_stream):
            self.assertEqual(self.request(gateway, b'{}', headers), (200, partial_stream))
        deadline = time.monotonic() + 4
        value = report(recorder.path, 'total', source='gateway-inprocess-v1')
        while value['totals']['partial'] < 1 and time.monotonic() < deadline:
            time.sleep(.02)
            value = report(recorder.path, 'total', source='gateway-inprocess-v1')
        self.assertEqual((value['totals']['partial'], value['totals']['input'], value['totals']['output']), (1, 120, 20))
        self.assertIsNone(value['totals']['hit_rate'])
        self.assertIsNone(value['requests'][0]['cached'])

    @unittest.skipUnless(os.environ.get('LLMDEPLOY_CADDY'), 'Set LLMDEPLOY_CADDY for Caddy integration')
    def test_caddy_reload_keeps_one_active_stream(self):
        # This checks an active stream, not idle keep-alive connections.
        def port():
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                return probe.getsockname()[1]
        front, admin = port(), port()
        before, maintenance, after = (self.root/'before.Caddyfile', self.root/'maintenance.Caddyfile',
                                      self.root/'after.Caddyfile')
        template = ('{{\n admin 127.0.0.1:{admin}\n auto_https off\n}}\n'
                    'http://127.0.0.1:{front} {{\n bind 127.0.0.1\n reverse_proxy 127.0.0.1:{gateway} {{\n'
                    '{header} }}\n}}\n')
        values = {'admin': admin, 'front': front, 'gateway': self.gateway_under_test.server_port}
        before.write_text(template.format(**values, header=''), encoding='utf-8')
        maintenance.write_text(('{{\n admin 127.0.0.1:{admin}\n auto_https off\n}}\n'
                                'http://127.0.0.1:{front} {{\n bind 127.0.0.1\n respond 503\n}}\n').format(**values),
                               encoding='utf-8')
        after.write_text(template.format(**values,
            header='  header_up X-LLMDeploy-Client-IP {remote_host}\n'), encoding='utf-8')
        env = dict(os.environ, XDG_DATA_HOME=str(self.root), XDG_CONFIG_HOME=str(self.root),
                   APPDATA=str(self.root), LOCALAPPDATA=str(self.root))
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        caddy = subprocess.Popen([os.environ['LLMDEPLOY_CADDY'], 'run', '--config', str(before),
            '--adapter', 'caddyfile'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, creationflags=flags)
        client = None
        try:
            deadline = time.monotonic() + 5
            while not (synthetic_caddy_owns_listener(caddy, front) and
                       synthetic_caddy_owns_listener(caddy, admin)):
                if caddy.poll() is not None or time.monotonic() >= deadline:
                    self.fail('synthetic Caddy listener ownership could not be verified')
                time.sleep(.02)
            self.model.mode = 'slow'
            client = socket.create_connection(('127.0.0.1', front), timeout=5)
            client.sendall((f'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{front}\r\n'
                            f'Authorization: Bearer {self.key}\r\nContent-Length: 2\r\n\r\n{{}}').encode())
            self.assertTrue(self.model.started.wait(3))
            received = bytearray(client.recv(4096))
            self.assertIn(b'200', received)
            self.model.mode = 'normal'
            def reload(config):
                self.assertTrue(synthetic_caddy_owns_listener(caddy, admin))
                self.assertTrue(synthetic_caddy_owns_listener(caddy, front))
                result = subprocess.run([os.environ['LLMDEPLOY_CADDY'], 'reload', '--address',
                    f'127.0.0.1:{admin}', '--config', str(config), '--adapter', 'caddyfile'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=env, creationflags=flags, timeout=5)
                self.assertEqual(result.returncode, 0)
            reload(maintenance)
            self.assertTrue(synthetic_caddy_owns_listener(caddy, front))
            conn = HTTPConnection('127.0.0.1', front, timeout=5)
            conn.request('POST', '/v1/chat/completions', b'{}')
            maintenance_response = conn.getresponse()
            self.assertEqual(maintenance_response.status, 503)
            maintenance_response.read()
            conn.close()
            reload(after)
            self.assertTrue(synthetic_caddy_owns_listener(caddy, front))
            conn = HTTPConnection('127.0.0.1', front, timeout=5)
            conn.request('POST', '/v1/chat/completions', b'{}',
                {'Authorization': 'Bearer '+self.key, 'X-LLMDeploy-Client-IP': '192.0.2.55'})
            response = conn.getresponse()
            self.assertEqual((response.status, response.read()), (200, STREAM))
            conn.close()
            self.model.release.set()
            while b'data: [DONE]\n\n' not in received:
                chunk = client.recv(4096)
                self.assertTrue(chunk)
                received.extend(chunk)
            self.wait_recorded()
            result = report(self.recorder.path, 'total', now=time.time() + .01)
            sources = {item['ip'] for item in result['clients']}
            self.assertIn('127.0.0.1', sources)
            self.assertNotIn('192.0.2.55', sources)
        finally:
            self.model.release.set()
            if client:
                client.close()
            if caddy.poll() is None:
                caddy.terminate()
            caddy.wait(timeout=5)

    def test_terminal_marker_does_not_make_a_truncated_response_complete(self):
        self.model.mode = 'truncate'
        headers = {'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'}
        self.assertEqual(self.request(self.gateway_under_test, b'{}', headers), (200, STREAM))
        self.wait_recorded()
        result = report(self.recorder.path, 'total', now=time.time() + .01)
        self.assertEqual((result['totals']['known'], result['totals']['interrupted']), (0, 1))

    def test_recording_and_parser_faults_never_change_response(self):
        headers = {'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'}
        for target in ('deploy_stats.inprocess.SafeCapture.feed', 'deploy_stats.inprocess._Observer.session'):
            with self.subTest(target=target), patch(target, side_effect=RuntimeError('synthetic failure')):
                self.assertEqual(self.request(self.gateway_under_test, b'{}', headers), (200, STREAM))
                self.wait_recorded()
        self.assertGreaterEqual(self.recorder.dropped, 2)

    def test_unavailable_recorder_does_not_change_handler(self):
        module = type('SyntheticModule', (), {'Handler': self.original_handler})
        with patch('deploy_stats.inprocess.Recorder', side_effect=OSError('synthetic failure')):
            self.assertIsNone(install(module, self.root/'unavailable.sqlite3'))
        self.assertIs(module.Handler, self.original_handler)

    def test_bridge_start_failure_closes_recorder_and_restores_handler(self):
        module = type('SyntheticModule', (), {'Handler': self.original_handler})
        with patch('deploy_stats.inprocess.Recorder') as recorder:
            def unavailable():
                raise OSError('synthetic bind failure')
            with self.assertRaises(OSError):
                inprocess.serve(module, self.root/'failed-start.sqlite3', unavailable)
        recorder.return_value.close.assert_called_once()
        self.assertIs(module.Handler, self.original_handler)

    def test_recorder_failure_does_not_prevent_bridge_start(self):
        module = type('SyntheticModule', (), {'Handler': self.original_handler})
        called = []
        def unavailable_server():
            called.append(True)
            raise OSError('synthetic bind failure')
        with patch('deploy_stats.inprocess.Recorder', side_effect=OSError('synthetic storage failure')):
            with self.assertRaises(OSError):
                inprocess.serve(module, self.root/'failed-recorder.sqlite3', unavailable_server)
        self.assertEqual(called, [True])
        self.assertIs(module.Handler, self.original_handler)

    @unittest.skipUnless(hasattr(socket, 'SO_EXCLUSIVEADDRUSE'), 'Windows exclusive socket option')
    def test_gateway_cannot_bind_over_same_address_listener(self):
        duplicate = None
        try:
            with self.assertRaises(OSError):
                duplicate = gateway_module.Gateway(('127.0.0.1', self.gateway_under_test.server_port), self.keys,
                    state_path=self.root/'duplicate-state.json', bindings_file=self.root/'duplicate-bindings.json',
                    backend_ports=[self.model.server_port], backend_managed_affinity=True)
        finally:
            if duplicate is not None:
                duplicate.server_close()

    def test_early_auth_rejection_keeps_the_response_complete_during_upload(self):
        connection = socket.create_connection(('127.0.0.1', self.gateway_under_test.server_port), timeout=5)
        try:
            connection.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                               b'Authorization: Bearer synthetic-invalid\r\nContent-Length: 1048576\r\n\r\n'
                               + b'x' * 1024)
            response = HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 401)
            self.assertEqual(json.loads(response.read())['error']['message'], 'Invalid bearer API key')
            for _ in range(15):
                connection.sendall(b'x' * 65536)
        finally:
            connection.close()
        deadline = time.monotonic() + 3
        while self.gateway_under_test.rejection_drainer._slots._value != MAX_REJECTIONS and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.gateway_under_test.rejection_drainer._slots._value, MAX_REJECTIONS)

    def test_install_is_idempotent_and_does_not_start_second_recorder(self):
        module = type('SyntheticModule', (), {'Handler': self.original_handler})
        first = install(module, self.root/'idempotent.sqlite3')
        self.assertIsNotNone(first)
        self.addCleanup(first.close)
        installed_handler = module.Handler
        with patch('deploy_stats.inprocess.Recorder') as second_recorder:
            self.assertIsNone(install(module, self.root/'idempotent.sqlite3'))
        second_recorder.assert_not_called()
        self.assertIs(module.Handler, installed_handler)

    def test_discarded_oversize_sse_releases_budget_for_other_requests(self):
        for long_line in (False, True):
            with self.subTest(long_line=long_line):
                budget = BodyBudget(MAX_FRAME + 256 * 1024)
                observer = _Observer(self.recorder, budget, Message(), 'chat')
                observer.response(200, 'text/event-stream', '')
                if long_line:
                    observer.chunk(b'data: prior\n')
                    while not observer.capture.reader.discard_line:
                        observer.chunk(b'x' * 65536)
                else:
                    while not observer.capture.reader.discard_event:
                        observer.chunk(b'data: ' + b'x' * 65000 + b'\n')
                self.assertEqual(budget.used, 0)
                other = _Observer(self.recorder, budget, Message(), 'chat')
                other.response(200, 'text/event-stream', '')
                other.chunk(STREAM)
                other.complete(True)
                other.finish()
                observer.finish()
                self.assertEqual(budget.used, 0)
                self.assertTrue(self.recorder.flush())
                result = report(self.recorder.path, 'total', now=time.time() + .01)
                self.assertEqual(result['totals']['known'], 1 if not long_line else 2)

    def test_discarded_sse_frame_never_reports_stale_usage_or_late_first_output(self):
        budget = BodyBudget(MAX_FRAME + 256 * 1024)
        observer = _Observer(self.recorder, budget, Message(), 'chat')
        observer.response(200, 'text/event-stream', '')
        observer.chunk(b'data: {"usage":{"prompt_tokens":2,"completion_tokens":1,'
                       b'"prompt_tokens_details":{"cached_tokens":1}}}\n\n')
        while not observer.capture.reader.discard_event:
            observer.chunk(b'data: ' + b'x' * 65000 + b'\n')
        observer.chunk(b'\n' + STREAM)
        observer.complete(True)
        observer.finish()
        self.assertTrue(self.recorder.flush())
        result = report(self.recorder.path, 'total', now=time.time() + .01)
        self.assertEqual((result['totals']['known'], result['totals']['missing']), (0, 1))
        self.assertIsNone(result['requests'][0]['first_visible'])

    def test_large_inprocess_frames_are_skipped_without_stopping_other_requests(self):
        budget = BodyBudget(MAX_FRAME + 256 * 1024)
        for content_type in ('text/event-stream', 'application/json'):
            with self.subTest(content_type=content_type):
                observer = _Observer(self.recorder, budget, Message(), 'chat')
                observer.response(200, content_type, '')
                if content_type == 'text/event-stream':
                    observer.chunk(b'data: ')
                for _ in range(65):
                    observer.chunk(b'x' * 65536)
                reader = observer.capture.reader
                self.assertTrue(reader.discard_event if reader.kind == 'sse' else reader.too_large)
                self.assertEqual(budget.used, 0)
                observer.complete(True)
                observer.finish()

    def test_invalid_later_sse_never_reuses_previous_usage(self):
        bad_events = (
            b'{"usage":{"prompt_tokens":1,"completion_tokens":1,'
            b'"prompt_tokens_details":{"cached_tokens":2}}}',
            b'{"usage": BROKEN}',
            b'{"unclosed":',
        )
        for bad in bad_events:
            for first_before in (False, True):
                with self.subTest(bad=bad, first_before=first_before):
                    first = []
                    parser = UsageCapture('chat', 'text/event-stream', on_first_output=lambda: first.append(True))
                    if first_before:
                        parser.feed(b'data: {"choices":[{"delta":{"content":"visible"}}]}\n\n')
                    parser.feed(b'data: {"usage":{"prompt_tokens":2,"completion_tokens":1,'
                                b'"prompt_tokens_details":{"cached_tokens":1}}}\n\n')
                    parser.feed(b'data: ' + bad + b'\n\n')
                    parser.feed(STREAM)
                    self.assertIsNone(parser.finish())
                    self.assertEqual(first, [True] if first_before else [])

    def test_many_empty_or_mixed_data_lines_are_bounded_and_do_not_starve_other_requests(self):
        for line in (b'data:\n', b'data: x\ndata:\n'):
            with self.subTest(line=line):
                budget = BodyBudget(MAX_FRAME)
                observer = _Observer(self.recorder, budget, Message(), 'chat')
                observer.response(200, 'text/event-stream', '')
                for _ in range(12):
                    observer.chunk(line * 500)
                self.assertTrue(observer.capture.reader.discard_event)
                self.assertEqual(budget.used, 0)
                other = _Observer(self.recorder, budget, Message(), 'chat')
                other.response(200, 'text/event-stream', '')
                other.chunk(STREAM)
                other.complete(True)
                other.finish()
                observer.finish()
                self.assertEqual(budget.used, 0)
                self.assertTrue(self.recorder.flush())
                result = report(self.recorder.path, 'total', now=time.time() + .01)
                self.assertEqual(result['totals']['known'], 1 if line == b'data:\n' else 2)

    def test_large_simple_responses_completion_retains_usage(self):
        budget = BodyBudget(32 * 1024 * 1024)
        observer = _Observer(self.recorder, budget, Message(), 'responses')
        observer.response(200, 'text/event-stream', '')
        observer.chunk(b'data: {"type":"response.output_text.delta","delta":"x"}\n\n')
        text = 'x' * (1024 * 1024) + ('[{"值":"中文🙂","s":"\\\\","q":"\\\""}]' * 1000)
        completed = {'type': 'response.completed', 'response': {'output': text,
            'usage': {'input_tokens': 100, 'output_tokens': 7, 'input_tokens_details': {'cached_tokens': 60}}}}
        frame = b'data: ' + json.dumps(completed, ensure_ascii=False).encode() + b'\n\n'
        for start in range(0, len(frame), 65536):
            observer.chunk(frame[start:start + 65536])
        observer.complete(True)
        observer.finish()
        self.assertTrue(self.recorder.flush())
        result = report(self.recorder.path, 'total', now=time.time() + .01)
        self.assertEqual((result['totals']['known'], result['totals']['input'], result['totals']['output'],
                          result['totals']['cached']), (1, 100, 7, 60))

    def test_structurally_dense_json_is_missing_but_other_requests_still_record(self):
        budget = BodyBudget(MAX_FRAME)
        for content_type in ('text/event-stream', 'application/json'):
            with self.subTest(content_type=content_type):
                observer = _Observer(self.recorder, budget, Message(), 'chat')
                observer.response(200, content_type, '')
                body = json.dumps({'choices': [], 'extra': [0] * 20000,
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 7,
                              'prompt_tokens_details': {'cached_tokens': 60}}}).encode()
                frame = b'data: ' + body + b'\n\ndata: [DONE]\n\n' if content_type.startswith('text/') else body
                for start in range(0, len(frame), 8192):
                    observer.chunk(frame[start:start + 8192])
                observer.complete(True)
                observer.finish()
                self.assertEqual(budget.used, 0)
        self.assertTrue(self.recorder.flush())
        result = report(self.recorder.path, 'total', now=time.time() + .01)
        self.assertEqual((result['totals']['known'], result['totals']['missing']), (0, 2))

    def test_large_sse_delta_requires_later_usage_and_preserves_first_output_certainty(self):
        large = b'data: ' + json.dumps({'choices': [{'delta': {'content': 'x' * 70000}}],
                                     'usage': None}).encode() + b'\n\n'
        usage = (b'data: {"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":7,'
                 b'"prompt_tokens_details":{"cached_tokens":60}}}\n\n')
        first_delta = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        for first_before, final_usage in ((False, True), (True, True), (True, False)):
            with self.subTest(first_before=first_before, final_usage=final_usage):
                first = []
                parser = UsageCapture('chat', 'text/event-stream', on_first_output=lambda: first.append(True),
                                      max_structure=16384, max_delta=65536)
                if first_before:
                    parser.feed(first_delta)
                parser.feed(usage)
                parser.feed(large)
                if final_usage:
                    parser.feed(usage)
                parser.feed(b'data: [DONE]\n\n')
                self.assertEqual(parser.finish(), (100, 7, 60) if final_usage else None)
                self.assertEqual(first, [True] if first_before else [])

    def test_chat_usage_after_skipped_event_requires_verified_finish(self):
        large = b'data: ' + json.dumps({'choices': [{'delta': {'content': 'x' * 70000}}]}).encode() + b'\n\n'
        usage = (b'data: {"usage":{"prompt_tokens":100,"completion_tokens":7,'
                 b'"prompt_tokens_details":{"cached_tokens":60}}}\n\n')
        finish = b'data: {"choices":[{"finish_reason":"stop"}]}\n\n'
        for finish_first in (False, True):
            with self.subTest(finish_first=finish_first):
                parser = UsageCapture('chat', 'text/event-stream', max_delta=65536)
                parser.feed(large)
                if finish_first:
                    parser.feed(finish)
                parser.feed(usage + b'data: [DONE]\n\n')
                self.assertEqual(parser.finish(), (100, 7, 60) if finish_first else None)

    def test_data_after_done_never_restores_usage(self):
        large = b'data: ' + json.dumps({'choices': [{'delta': {'content': 'x' * 70000}}]}).encode() + b'\n\n'
        final = (b'data: {"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":100,'
                 b'"completion_tokens":7,"prompt_tokens_details":{"cached_tokens":60}}}\n\n')
        parser = UsageCapture('chat', 'text/event-stream', max_delta=65536)
        parser.feed(large + b'data: [DONE]\n\n' + final)
        self.assertIsNone(parser.finish())

    def test_fragmented_long_sse_line_does_not_rescan_previous_bytes(self):
        class CountedBuffer(bytearray):
            scanned = 0
            def find(self, value, start=0, end=None):
                self.scanned += (len(self) if end is None else min(end, len(self))) - start
                return super().find(value, start) if end is None else super().find(value, start, end)
        parser = UsageCapture('chat', 'text/event-stream')
        parser.buffer = CountedBuffer()
        for _ in range(32):
            parser.feed(b'x' * 65536)
        self.assertLessEqual(parser.buffer.scanned, 2 * 32 * 65536)

    def test_duplicate_usage_or_count_keys_do_not_produce_known_usage(self):
        for body in (
            b'{"usage":{"prompt_tokens":2,"completion_tokens":1,"prompt_tokens_details":{"cached_tokens":1}},'
            b'"usage":{"prompt_tokens":20,"completion_tokens":1,"prompt_tokens_details":{"cached_tokens":1}}}',
            b'{"usage":{"prompt_tokens":2,"prompt_tokens":20,"completion_tokens":1,'
            b'"prompt_tokens_details":{"cached_tokens":1}}}',
        ):
            with self.subTest(body=body):
                parser = UsageCapture('chat', 'text/event-stream')
                parser.feed(b'data: ' + body + b'\n\ndata: [DONE]\n\n')
                self.assertIsNone(parser.finish())

    def test_present_cache_hit_and_miss_counts_must_be_valid_and_consistent(self):
        valid = {'prompt_tokens': 100, 'completion_tokens': 7, 'prompt_cache_hit_tokens': 60,
                 'prompt_cache_miss_tokens': 40, 'prompt_tokens_details': {'cached_tokens': 60}}
        self.assertEqual(extract_usage('chat', {'usage': valid}), (100, 7, 60))
        invalid = [dict(valid, prompt_cache_miss_tokens=99), dict(valid, prompt_cache_hit_tokens='bad'),
                   dict(valid, prompt_cache_miss_tokens='bad'),
                   dict(valid, prompt_tokens_details={'cached_tokens': 'bad'}),
                   dict(valid, prompt_tokens_details='bad')]
        for usage in invalid:
            with self.subTest(usage=usage):
                self.assertIsNone(extract_usage('chat', {'usage': usage}))

    def test_large_event_with_nested_null_and_final_usage_is_not_skipped(self):
        payload = {'extra': {'usage': None, 'text': 'x' * 70000}, 'choices': [{'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 100, 'completion_tokens': 7,
                             'prompt_tokens_details': {'cached_tokens': 60}}}
        parser = UsageCapture('chat', 'text/event-stream', max_delta=65536, max_structure=16384)
        parser.feed(b'data: ' + json.dumps(payload).encode() + b'\n\ndata: [DONE]\n\n')
        self.assertEqual(parser.finish(), (100, 7, 60))

    def test_large_nonterminal_event_with_usage_waits_for_verified_terminal(self):
        first = []
        parser = UsageCapture('chat', 'text/event-stream', on_first_output=lambda: first.append(True),
                              max_delta=65536, max_structure=16384)
        frame = {'choices': [{'delta': {'content': 'x' * 70000}}],
                 'usage': {'prompt_tokens': 100, 'completion_tokens': 7,
                           'prompt_tokens_details': {'cached_tokens': 60}}}
        parser.feed(b'data: ' + json.dumps(frame).encode() + b'\n\n')
        self.assertTrue(parser.skipped)
        self.assertEqual(first, [])
        parser.feed(b'data: {"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":100,'
                    b'"completion_tokens":7,"prompt_tokens_details":{"cached_tokens":60}}}\n\n'
                    b'data: [DONE]\n\n')
        self.assertEqual(parser.finish(), (100, 7, 60))

    def test_nested_finish_marker_cannot_validate_large_chat_usage(self):
        payload = {'meta': {'finish_reason': 'stop'}, 'content': 'x' * 70000,
                   'usage': {'prompt_tokens': 100, 'completion_tokens': 7,
                             'prompt_tokens_details': {'cached_tokens': 60}}}
        parser = UsageCapture('chat', 'text/event-stream', max_delta=65536)
        parser.feed(b'data: ' + json.dumps(payload).encode() + b'\n\ndata: [DONE]\n\n')
        self.assertIsNone(parser.finish())

    def test_capture_budget_exhaustion_does_not_interrupt_response(self):
        headers = {'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'}
        with patch('deploy_stats.inprocess.CAPTURE_BUDGET', 10):
            recorder = install(gateway_module, self.root/'limited-usage.sqlite3')
            self.assertIsNotNone(recorder)
            self.addCleanup(recorder.close)
            gateway = self.gateway('limited')
            gateway_module.Handler = self.original_handler
            self.assertEqual(self.request(gateway, b'{}', headers), (200, STREAM))
            deadline = time.monotonic() + 4
            while recorder.live and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(recorder.flush())
            result = report(recorder.path, 'total', now=time.time() + .01)
            self.assertEqual((result['totals']['known'], result['totals']['missing']), (0, 1))
            self.assertGreaterEqual(recorder.dropped, 1)

    def test_large_request_session_uses_gateway_fingerprint_without_storing_body(self):
        payload = json.dumps({'messages': [{'role': 'user', 'content': 'x' * (1024 * 1024 + 123)}]}).encode()
        headers = {'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'}
        self.assertEqual(self.request(self.gateway_under_test, payload, headers), (200, STREAM))
        self.wait_recorded()
        result = report(self.recorder.path, 'total', now=time.time() + .01)
        message = Message()
        message['Authorization'] = 'Bearer ' + self.key
        digest = hashlib.sha256(self.key.encode()).hexdigest()
        self.assertEqual(result['requests'][0]['session'], affinity_key(message, payload, digest))
        self.assertEqual(result['totals']['unknown_source'], 1)
        for file in self.root.glob('usage.sqlite3*'):
            self.assertNotIn(b'x' * 100, file.read_bytes())

    def test_client_cancellation_is_not_counted_as_complete(self):
        self.model.mode = 'slow'
        conn = socket.create_connection(('127.0.0.1', self.gateway_under_test.server_port), timeout=5)
        try:
            conn.sendall((f'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                          f'Authorization: Bearer {self.key}\r\nContent-Length: 2\r\n\r\n{{}}').encode())
            self.assertTrue(self.model.started.wait(3))
            self.assertTrue(conn.recv(4096))
            conn.shutdown(socket.SHUT_RDWR)
            conn.close()
            # The existing gateway may not unblock a silent upstream read on
            # Windows until the upstream sends again; keep that baseline.
            time.sleep(.3)
            self.model.release.set()
            self.wait_recorded()
            result = report(self.recorder.path, 'total', now=time.time() + .01)
            self.assertEqual((result['totals']['known'], result['totals']['interrupted']), (0, 1))
        finally:
            self.model.release.set()
            conn.close()


if __name__ == '__main__':
    unittest.main()
