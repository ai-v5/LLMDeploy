from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone, timedelta
from email.message import Message
import hashlib
from http.client import HTTPConnection, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from deploy_stats.capture import UsageCapture, extract_usage
from deploy_stats.proxy import Proxy, client_ip
from deploy_stats.report import report
from deploy_stats.store import _open, Recorder
from deploy_stats.web import Dashboard
from deploy_gateway.server import Gateway


def usage(input_tokens=100, output_tokens=7, cached=60):
    return {'prompt_tokens': input_tokens, 'completion_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens,
            'prompt_tokens_details': {'cached_tokens': cached}, 'prompt_cache_hit_tokens': cached}


def sse(value):
    return b'data: ' + json.dumps(value, ensure_ascii=False).encode() + b'\n\n'


class CaptureTests(unittest.TestCase):
    def test_pinned_protocol_usage_and_invalid_counts(self):
        self.assertEqual(extract_usage('chat', {'usage': usage()}), (100, 7, 60))
        self.assertEqual(extract_usage('responses', {'response': {'usage': {
            'input_tokens': 100, 'output_tokens': 7, 'input_tokens_details': {'cached_tokens': 60},
            'total_tokens': 107}}}), (100, 7, 60))
        self.assertEqual(extract_usage('messages', {'usage': {'input_tokens': 35, 'output_tokens': 7,
            'cache_read_input_tokens': 60, 'cache_creation_input_tokens': 5}}), (100, 7, 60))
        for invalid in ({**usage(), 'completion_tokens': True}, {**usage(), 'total_tokens': 999},
                        {**usage(), 'prompt_cache_hit_tokens': 50}, {**usage(), 'prompt_tokens': -1}):
            self.assertIsNone(extract_usage('chat', {'usage': invalid}))

    def test_fragmented_chat_sse_requires_final_marker(self):
        content = sse({'choices': [{'delta': {'content': 'synthetic'}}]})
        content += sse({'choices': [{'finish_reason': 'stop'}], 'usage': usage()}) + b'data: [DONE]\n\n'
        for size in (1, 2, 7, 257):
            parser = UsageCapture('chat', 'text/event-stream; charset=utf-8')
            for at in range(0, len(content), size):
                parser.feed(content[at:at + size])
            self.assertEqual(parser.finish(), (100, 7, 60))
        parser = UsageCapture('chat', 'text/event-stream')
        parser.feed(content.removesuffix(b'data: [DONE]\n\n'))
        self.assertIsNone(parser.finish())

    def test_responses_and_messages_stream_terminal_events(self):
        response = {'type': 'response.completed', 'response': {'usage': {'input_tokens': 100,
            'output_tokens': 7, 'input_tokens_details': {'cached_tokens': 60}, 'total_tokens': 107}}}
        parser = UsageCapture('responses', 'text/event-stream')
        parser.feed(sse(response))
        self.assertEqual(parser.finish(), (100, 7, 60))
        parser = UsageCapture('messages', 'text/event-stream')
        parser.feed(sse({'type': 'message_delta', 'usage': {'input_tokens': 40,
            'cache_read_input_tokens': 60, 'cache_creation_input_tokens': 0, 'output_tokens': 7}}))
        self.assertIsNone(parser.finish())
        parser.feed(sse({'type': 'message_stop'}))
        self.assertEqual(parser.finish(), (100, 7, 60))

    def test_origin_header_requires_one_valid_ip(self):
        headers = Message()
        self.assertIsNone(client_ip(headers))
        headers['X-LLMDeploy-Client-IP'] = '::ffff:192.0.2.4'
        self.assertEqual(client_ip(headers), '192.0.2.4')
        headers['X-LLMDeploy-Client-IP'] = '192.0.2.5'
        self.assertIsNone(client_ip(headers))


class SyntheticGateway(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == '/health' else 404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        if self.server.mode == 'reject':
            self.send_response(429)
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            return
        payload = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.calls.append((payload, self.headers.get('Authorization'),
                                  self.headers.get('X-LLMDeploy-Client-IP')))
        if self.server.barrier:
            self.server.barrier.wait(timeout=5)
        mode = self.server.mode
        if mode == 'slow_headers':
            self.server.stream_started.set()
            self.server.stream_release.wait(3)
            self.close_connection = True
            return
        if mode == 'slow':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(sse({'choices': [{'delta': {'content': 'synthetic'}}]}))
            self.wfile.flush()
            self.server.stream_started.set()
            self.server.stream_release.wait(3)
            try:
                self.wfile.write(sse({'usage': usage()}) + b'data: [DONE]\n\n')
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True
            return
        if mode == 'json':
            body = json.dumps({'content': 'synthetic-output', 'usage': usage()}).encode()
            media = 'application/json'
        elif mode == 'missing':
            body = sse({'choices': [{'finish_reason': 'stop'}]}) + b'data: [DONE]\n\n'
            media = 'text/event-stream'
        else:
            body = sse({'choices': [{'delta': {'content': 'synthetic-output'}}]})
            body += sse({'choices': [{'finish_reason': 'stop'}], 'usage': usage()}) + b'data: [DONE]\n\n'
            media = 'text/event-stream'
        self.server.last_body = body
        self.send_response(200)
        self.send_header('Content-Type', media)
        if mode in ('slow_length', 'slow_chunked'):
            if mode == 'slow_chunked':
                self.send_header('Transfer-Encoding', 'chunked')
            else:
                self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if mode == 'slow_chunked':
                self.wfile.write(f'{len(body):x}\r\n'.encode())
            self.wfile.write(body[:24])
            self.wfile.flush()
            self.server.stream_started.set()
            self.server.stream_release.wait(3)
            try:
                self.wfile.write(body[24:] + (b'\r\n0\r\n\r\n' if mode == 'slow_chunked' else b''))
                self.wfile.flush()
            except OSError:
                pass
            return
        if mode == 'chunked':
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            for at in range(0, len(body), 11):
                part = body[at:at + 11]
                self.wfile.write(f'{len(part):x}\r\n'.encode() + part + b'\r\n')
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
            return
        self.send_header('Content-Length', str(len(body) + (10 if mode == 'truncate' else 0)))
        self.end_headers()
        for at in range(0, len(body), 13):
            self.wfile.write(body[at:at + 13])
            self.wfile.flush()
        if mode == 'truncate':
            self.close_connection = True


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'usage.sqlite3'
        class SyntheticServer(ThreadingHTTPServer):
            request_queue_size = 128
        self.upstream = SyntheticServer(('127.0.0.1', 0), SyntheticGateway)
        self.upstream.daemon_threads = True
        self.upstream.mode = 'sse'
        self.upstream.calls = []
        self.upstream.barrier = None
        self.upstream.stream_started = threading.Event()
        self.upstream.stream_release = threading.Event()
        self.start(self.upstream)
        self.proxy = Proxy(0, self.upstream.server_port, self.path)
        self.start(self.proxy)

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.addCleanup(cleanup)

    def request(self, ip='192.0.2.4'):
        connection = HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        payload = b'{"prompt":"synthetic-prompt"}'
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer synthetic-placeholder',
                   'X-LLMDeploy-Client-IP': ip}
        try:
            connection.request('POST', '/v1/chat/completions', payload, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def wait_recorded(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.proxy.active_lock:
                if self.proxy.active == 0:
                    self.assertTrue(self.proxy.recorder.flush())
                    return
            time.sleep(.01)
        self.fail('proxy requests did not drain')

    def test_stream_is_unchanged_and_only_metadata_is_stored(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body, self.upstream.last_body)
        self.assertEqual(self.upstream.calls[-1], (b'{"prompt":"synthetic-prompt"}',
            'Bearer synthetic-placeholder', None))
        self.wait_recorded()
        data = report(self.path, 'total', now=time.time() + .01)
        self.assertEqual((data['totals']['input'], data['totals']['output'], data['totals']['cached']), (100, 7, 60))
        self.assertEqual(data['clients'][0]['ip'], '192.0.2.4')
        for dbfile in Path(self.temp.name).glob('usage.sqlite3*'):
            raw = dbfile.read_bytes()
            for secret in (b'synthetic-prompt', b'synthetic-output', b'synthetic-placeholder'):
                self.assertNotIn(secret, raw)

    def test_json_and_unknown_usage_are_separate(self):
        self.upstream.mode = 'json'
        self.assertEqual(self.request()[0], 200)
        self.upstream.mode = 'missing'
        self.assertEqual(self.request()[0], 200)
        self.wait_recorded()
        data = report(self.path, 'total', now=time.time() + .01)
        self.assertEqual((data['totals']['requests'], data['totals']['known'], data['totals']['missing']), (2, 1, 1))
        self.assertFalse(data['totals']['complete'])

    def test_invalid_client_ip_is_shown_as_unknown(self):
        self.assertEqual(self.request('invalid-source')[0], 200)
        self.wait_recorded()
        data = report(self.path, 'total', now=time.time() + .01)
        self.assertEqual(data['totals']['unknown_source'], 1)
        self.assertIsNone(data['clients'][0]['ip'])

    def test_parser_failure_does_not_interrupt_the_response(self):
        with patch('deploy_stats.capture.UsageCapture.feed', side_effect=ValueError('synthetic failure')):
            status, body = self.request()
        self.assertEqual((status, body), (200, self.upstream.last_body))
        self.wait_recorded()
        data = report(self.path, 'total', now=time.time() + .01)
        self.assertEqual((data['totals']['known'], data['totals']['missing']), (0, 1))

    def test_recorder_failure_does_not_interrupt_the_response(self):
        self.proxy.recorder.heartbeat = .02
        with patch.object(self.proxy.recorder, 'submit', side_effect=OSError('synthetic failure')):
            status, body = self.request()
            deadline = time.monotonic() + 2
            while self.proxy.active and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual((status, body), (200, self.upstream.last_body))
        self.wait_recorded()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            data = report(self.path, 'total', now=time.time() + .01)
            if data['totals']['dropped']:
                break
            time.sleep(.02)
        self.assertGreater(data['totals']['dropped'], 0)
        self.assertFalse(data['totals']['complete'])

    def test_chunked_sse_is_forwarded_and_accounted(self):
        self.upstream.mode = 'chunked'
        status, body = self.request()
        self.assertEqual((status, body), (200, self.upstream.last_body))
        self.wait_recorded()
        self.assertEqual(report(self.path, 'total', now=time.time() + .01)['totals']['output'], 7)

    def test_sse_first_bytes_do_not_wait_for_remaining_body(self):
        for mode in ('slow_length', 'slow_chunked'):
            with self.subTest(mode=mode):
                self.upstream.mode = mode
                self.upstream.stream_started.clear()
                self.upstream.stream_release.clear()
                connection = socket.create_connection(('127.0.0.1', self.proxy.server_port), timeout=1)
                connection.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                    b'Content-Length: 2\r\nX-LLMDeploy-Client-IP: 192.0.2.4\r\n\r\n{}')
                try:
                    self.assertTrue(self.upstream.stream_started.wait(1))
                    received = b''
                    while b'data:' not in received:
                        received += connection.recv(4096)
                    self.assertIn(b'data:', received)
                finally:
                    self.upstream.stream_release.set()
                    connection.close()

    def test_truncated_stream_is_not_scored_as_zero(self):
        self.upstream.mode = 'truncate'
        with self.assertRaises(IncompleteRead):
            self.request()
        self.wait_recorded()
        data = report(self.path, 'total', now=time.time() + .01)
        self.assertEqual((data['totals']['known'], data['totals']['interrupted']), (0, 1))
        self.assertFalse(data['totals']['complete'])

    def test_concurrent_clients_are_attributed_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.request(f'192.0.2.{i + 1}')[0], range(12)))
        self.assertEqual(results, [200] * 12)
        self.wait_recorded()
        data = report(self.path, 'total', now=time.time() + .01)
        self.assertEqual((data['totals']['known'], data['totals']['output'], len(data['clients'])), (12, 84, 12))

    def test_sixty_four_simultaneous_streams_are_independently_accounted(self):
        self.upstream.barrier = threading.Barrier(64)
        with ThreadPoolExecutor(max_workers=64) as pool:
            statuses = list(pool.map(lambda i: self.request(f'192.0.2.{i + 1}')[0], range(64)))
        self.assertEqual(statuses, [200] * 64)
        self.wait_recorded()
        data = report(self.path, 'total', now=time.time() + .01)
        self.assertEqual((data['totals']['known'], data['totals']['output'], len(data['clients'])), (64, 448, 64))

    def test_client_cancellation_is_recorded_as_incomplete(self):
        self.upstream.mode = 'slow'
        connection = socket.create_connection(('127.0.0.1', self.proxy.server_port), timeout=5)
        connection.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                           b'Content-Length: 2\r\nX-LLMDeploy-Client-IP: 192.0.2.4\r\n\r\n{}')
        self.assertTrue(connection.recv(1024))
        self.assertTrue(self.upstream.stream_started.wait(2))
        connection.shutdown(socket.SHUT_RDWR)
        connection.close()
        try:
            self.wait_recorded()
        finally:
            self.upstream.stream_release.set()
        data = report(self.path, 'total', now=time.time() + .01)
        self.assertEqual((data['totals']['known'], data['totals']['interrupted']), (0, 1))

    def test_client_cancellation_before_response_headers(self):
        self.upstream.mode = 'slow_headers'
        connection = socket.create_connection(('127.0.0.1', self.proxy.server_port), timeout=5)
        connection.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                           b'Content-Length: 2\r\nX-LLMDeploy-Client-IP: 192.0.2.4\r\n\r\n{}')
        self.assertTrue(self.upstream.stream_started.wait(2))
        connection.shutdown(socket.SHUT_RDWR)
        connection.close()
        try:
            self.wait_recorded()
        finally:
            self.upstream.stream_release.set()
        data = report(self.path, 'total', now=time.time() + .01)
        self.assertEqual(data['totals']['interrupted'], 1)

    def test_large_body_is_forwarded_intact(self):
        payload = b'x' * (1024 * 1024 + 123)
        connection = HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        connection.request('POST', '/v1/chat/completions', payload, {'X-LLMDeploy-Client-IP': '192.0.2.4'})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        connection.close()
        self.wait_recorded()
        self.assertEqual(self.upstream.calls[-1][0], payload)

    def test_gateway_can_reject_a_large_body_before_reading_it(self):
        self.upstream.mode = 'reject'
        connection = HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        connection.request('POST', '/v1/chat/completions', b'x' * (1024 * 1024),
                           {'X-LLMDeploy-Client-IP': '192.0.2.4'})
        response = connection.getresponse()
        self.assertEqual(response.status, 429)
        response.read()
        connection.close()
        self.wait_recorded()
        self.assertEqual(report(self.path, 'total', now=time.time() + .01)['totals']['failed'], 1)

    def test_dashboard_is_loopback_only_and_rejects_untrusted_hosts(self):
        self.request()
        self.wait_recorded()
        dashboard = Dashboard(0, self.path)
        self.start(dashboard)
        connection = HTTPConnection('127.0.0.1', dashboard.server_port, timeout=5)
        connection.request('GET', '/api/stats?window=total')
        result = connection.getresponse()
        self.assertEqual(result.status, 200)
        self.assertEqual(json.loads(result.read())['totals']['output'], 7)
        connection.close()
        for headers in ({'Host': 'untrusted.invalid'}, {'Origin': 'https://untrusted.invalid'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            connection = HTTPConnection('127.0.0.1', dashboard.server_port, timeout=5)
            connection.request('GET', '/', headers=headers)
            self.assertEqual(connection.getresponse().status, 403)
            connection.close()

    def test_existing_listener_is_never_reused(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            with self.assertRaises(OSError):
                Dashboard(port, self.path)
            with self.assertRaises(OSError):
                Proxy(port, self.upstream.server_port, Path(self.temp.name) / 'must-not-exist.sqlite3')
            self.assertFalse((Path(self.temp.name) / 'must-not-exist.sqlite3').exists())

    def test_connection_limit_rejects_and_marks_a_gap(self):
        self.upstream.mode = 'slow_headers'
        self.proxy.recorder.heartbeat = .02
        with patch('deploy_stats.proxy.MAX_CONNECTIONS', 1):
            first = socket.create_connection(('127.0.0.1', self.proxy.server_port), timeout=2)
            first.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}')
            self.assertTrue(self.upstream.stream_started.wait(2))
            second = socket.create_connection(('127.0.0.1', self.proxy.server_port), timeout=2)
            try:
                second.sendall(b'GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n')
                self.assertIn(b'429 Too Many Requests', second.recv(1024))
            finally:
                second.close()
                first.close()
                self.upstream.stream_release.set()
            self.wait_recorded()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                data = report(self.path, 'total', now=time.time() + .01)
                if data['totals']['dropped']:
                    break
                time.sleep(.02)
            self.assertGreater(data['totals']['dropped'], 0)
            self.assertFalse(data['totals']['complete'])

    def test_real_gateway_authentication_is_preserved_through_stats_proxy(self):
        keyfile = Path(self.temp.name) / 'synthetic-keys.json'
        keyfile.write_text(json.dumps({'version': 1, 'records': {'fixture': {
            'sha256': hashlib.sha256(b'synthetic-gateway-key').hexdigest(), 'active': True}}}))
        gateway = Gateway(('127.0.0.1', 0), keyfile,
            state_path=Path(self.temp.name) / 'gateway-state.json',
            bindings_file=Path(self.temp.name) / 'gateway-bindings.json',
            backend_ports=[self.upstream.server_port], backend_managed_affinity=True)
        self.start(gateway)
        proxy = Proxy(0, gateway.server_port, Path(self.temp.name) / 'gateway-stats.sqlite3')
        self.start(proxy)
        for token, expected in ((None, 401), ('incorrect-placeholder', 401), ('synthetic-gateway-key', 200)):
            headers = {'Content-Type': 'application/json', 'X-LLMDeploy-Client-IP': '192.0.2.8'}
            if token is not None:
                headers['Authorization'] = 'Bearer ' + token
            connection = HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)
            connection.request('POST', '/v1/chat/completions',
                json.dumps({'messages': [{'role': 'user', 'content': 'synthetic'}]}).encode(), headers)
            response = connection.getresponse()
            self.assertEqual(response.status, expected)
            response.read()
            connection.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with proxy.active_lock:
                if proxy.active == 0:
                    break
            time.sleep(.01)
        self.assertTrue(proxy.recorder.flush())
        data = report(proxy.recorder.path, 'total', now=time.time() + .01)
        self.assertEqual((data['totals']['known'], data['totals']['failed']), (1, 2))
        self.assertIsNone(self.upstream.calls[-1][1])

    @unittest.skipUnless(os.environ.get('LLMDEPLOY_CADDY'), 'Set LLMDEPLOY_CADDY for local Caddy integration')
    def test_caddy_overwrites_spoofed_origin_and_preserves_response(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        config = Path(self.temp.name) / 'Caddyfile'
        config.write_text('''{
    admin off
    auto_https off
}
http://127.0.0.1:%d {
    log {
        output discard
    }
    reverse_proxy 127.0.0.1:%d {
        header_up X-LLMDeploy-Client-IP {remote_host}
        flush_interval 10ms
    }
}
''' % (port, self.proxy.server_port), encoding='utf-8')
        environment = dict(os.environ)
        environment['XDG_DATA_HOME'] = self.temp.name
        environment['XDG_CONFIG_HOME'] = self.temp.name
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        process = subprocess.Popen([os.environ['LLMDEPLOY_CADDY'], 'run', '--config', str(config),
            '--adapter', 'caddyfile'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=environment, creationflags=flags)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    connection = socket.create_connection(('127.0.0.1', port), timeout=.2)
                    connection.close()
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail('synthetic Caddy did not start')
                    time.sleep(.05)
            connection = HTTPConnection('127.0.0.1', port, timeout=5)
            connection.request('POST', '/v1/chat/completions', b'{}', {
                'Authorization': 'Bearer synthetic-placeholder',
                'X-LLMDeploy-Client-IP': '192.0.2.99'})
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (200, self.upstream.last_body))
            connection.close()
            self.wait_recorded()
            data = report(self.path, 'total', now=time.time() + .01)
            self.assertEqual(data['clients'][0]['ip'], '127.0.0.1')
            self.assertEqual(self.upstream.calls[-1][1:], ('Bearer synthetic-placeholder', None))
            self.upstream.mode = 'slow_length'
            self.upstream.stream_started.clear()
            self.upstream.stream_release.clear()
            client = socket.create_connection(('127.0.0.1', port), timeout=1)
            client.sendall((f'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                            'Content-Length: 2\r\n\r\n{}').encode())
            try:
                self.assertTrue(self.upstream.stream_started.wait(1))
                received = b''
                while b'data:' not in received:
                    received += client.recv(4096)
                self.assertIn(b'data:', received)
                client.shutdown(socket.SHUT_RDWR)
                client.close()
                self.wait_recorded()
                data = report(self.path, 'total', now=time.time() + .01)
                self.assertEqual(data['totals']['interrupted'], 1)
            finally:
                self.upstream.stream_release.set()
                client.close()
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


class ReportTests(unittest.TestCase):
    def test_unclean_stale_run_never_claims_a_historical_day_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.sqlite3'
            tz = timezone(timedelta(hours=8))
            now = datetime(2026, 1, 3, 12, tzinfo=tz).timestamp()
            start = datetime(2026, 1, 1, 0, tzinfo=tz).timestamp()
            sample = datetime(2026, 1, 2, 12, tzinfo=tz).timestamp()
            db = _open(path)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('unclean', start, now - 7200, None, 0))
            db.execute('''INSERT INTO requests(run_id,finished,client_ip,elapsed,http_status,state,
                input_tokens,output_tokens,cached_tokens) VALUES(?,?,?,?,?,?,?,?,?)''',
                ('unclean', sample, '192.0.2.4', 2, 200, 'known', 100, 7, 60))
            db.commit()
            db.close()
            value = report(path, 'total', now=now)
            day = next(item for item in value['days'] if item['date'] == '2026-01-02')
            self.assertEqual(day['covered_seconds'], 86400)
            self.assertEqual(day['known'], 1)
            self.assertFalse(day['complete'])

    def test_cleanly_drained_run_can_cover_a_complete_historical_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.sqlite3'
            tz = timezone(timedelta(hours=8))
            now = datetime(2026, 1, 3, 12, tzinfo=tz).timestamp()
            start = datetime(2026, 1, 1, 0, tzinfo=tz).timestamp()
            db = _open(path)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('clean', start, now - 7200, now - 7200, 0))
            db.commit()
            db.close()
            value = report(path, 'total', now=now)
            day = next(item for item in value['days'] if item['date'] == '2026-01-02')
            self.assertTrue(day['complete'])
            self.assertEqual(day['unclean_runs'], 0)

    def test_local_days_and_gaps_are_reported_without_zero_filling(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.sqlite3'
            now = datetime(2026, 1, 2, 1, tzinfo=timezone(timedelta(hours=8))).timestamp()
            db = _open(path)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('fixture', now - 7200, now - 1, None, 0))
            for stamp, state, values in ((now - 3700, 'known', (100, 7, 60)),
                                         (now - 100, 'missing', (None, None, None))):
                db.execute('''INSERT INTO requests(run_id,finished,client_ip,elapsed,http_status,state,
                    input_tokens,output_tokens,cached_tokens) VALUES(?,?,?,?,?,?,?,?,?)''',
                    ('fixture', stamp, '192.0.2.4', 2, 200, state, *values))
            db.commit()
            db.close()
            value = report(path, '24h', now=now)
            self.assertFalse(value['totals']['complete'])
            self.assertEqual(value['totals']['known'], 1)
            self.assertEqual(value['totals']['missing'], 1)
            yesterday = next(d for d in value['days'] if d['date'] == '2026-01-01')
            self.assertEqual((yesterday['input'], yesterday['output'], yesterday['cached']), (100, 7, 60))
            self.assertTrue(any(not point['complete'] and not point['known'] for point in value['series_24h']))

    def test_existing_unrelated_database_is_not_repurposed(self):
        for version in (0, 1):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'existing.sqlite3'
                with closing(sqlite3.connect(path)) as db:
                    db.execute('CREATE TABLE keep_me(value TEXT)')
                    db.execute("INSERT INTO keep_me VALUES ('preserve')")
                    db.execute(f'PRAGMA user_version={version}')
                    db.commit()
                with self.assertRaises(ValueError):
                    Recorder(path)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('SELECT value FROM keep_me').fetchone()[0], 'preserve')
                    self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')

    def test_full_writer_queue_is_reported_as_a_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.sqlite3'
            recorder = Recorder(path, queue_size=1, heartbeat=.02)
            with closing(sqlite3.connect(path, timeout=1)) as blocker:
                blocker.execute('BEGIN IMMEDIATE')
                event = (time.time(), '192.0.2.4', 1.0, 200, 'known', 10, 2, 5)
                accepted = [recorder.submit(event) for _ in range(20)]
                self.assertIn(False, accepted)
                blocker.rollback()
            self.assertTrue(recorder.flush())
            self.assertTrue(recorder.close())
            data = report(path, 'total', now=time.time() + .01)
            self.assertGreater(data['totals']['dropped'], 0)
            self.assertFalse(data['totals']['complete'])


if __name__ == '__main__':
    unittest.main()
