from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone, timedelta
from email.message import Message
import hashlib
from http.client import HTTPConnection, HTTPResponse, IncompleteRead, parse_headers
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import queue
import sqlite3
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from deploy_stats.capture import UsageCapture, extract_usage
from deploy_stats.proxy import Proxy, _headers, client_ip, session_fingerprint
from deploy_stats.backends import read_backend
from deploy_stats.report import report
from deploy_stats.session import BodyBudget, SessionBodyCapture
from deploy_stats.store import _open, Recorder
from deploy_stats.web import Dashboard
from deploy_gateway.server import Gateway
from deploy_gateway.routing import affinity_key


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

    def test_session_fingerprint_matches_gateway_identity_without_storing_key(self):
        headers = Message()
        headers['Authorization'] = 'Bearer synthetic-gateway-key'
        headers['X-Session-Id'] = 'synthetic-private-session'
        digest = hashlib.sha256(b'synthetic-gateway-key').hexdigest()
        self.assertEqual(session_fingerprint(headers), affinity_key(headers, b'{}', digest))
        other = Message()
        other['Authorization'] = 'Bearer other-synthetic-key'
        other['X-Session-Id'] = 'synthetic-private-session'
        self.assertNotEqual(session_fingerprint(headers), session_fingerprint(other))
        headers['X-Session-Id'] = 'duplicate'
        self.assertIsNone(session_fingerprint(headers))

    def test_non_ascii_header_bytes_match_gateway_header_parser(self):
        raw = (b'Authorization: Bearer synthetic-gateway-key\r\n'
               b'X-Session-Id: synthetic-\xe4\xb8\xad')
        actual = _headers(raw)
        gateway = parse_headers(BytesIO(raw + b'\r\n\r\n'))
        self.assertEqual(actual.get('X-Session-Id'), gateway.get('X-Session-Id'))
        digest = hashlib.sha256(b'synthetic-gateway-key').hexdigest()
        self.assertEqual(session_fingerprint(actual), affinity_key(gateway, b'{}', digest))
        self.assertIsNone(client_ip(_headers(b'X-LLMDeploy-Client-IP: \xff')))

    def test_first_visible_delta_ignores_roles_and_empty_events(self):
        observed = []
        parser = UsageCapture('chat', 'text/event-stream', on_first_output=lambda: observed.append(1))
        parser.feed(sse({'choices': [{'delta': {'role': 'assistant', 'content': ''}}]}))
        self.assertEqual(observed, [])
        parser.feed(sse({'choices': [{'delta': {'content': 'synthetic'}}]}))
        parser.feed(sse({'choices': [{'delta': {'content': 'more'}}]}))
        self.assertEqual(observed, [1])
        response = UsageCapture('responses', 'text/event-stream', on_first_output=lambda: observed.append(2))
        response.feed(sse({'type': 'response.created'}))
        response.feed(sse({'type': 'response.output_text.delta', 'delta': 'synthetic'}))
        messages = UsageCapture('messages', 'text/event-stream', on_first_output=lambda: observed.append(3))
        messages.feed(sse({'type': 'message_start'}))
        messages.feed(sse({'type': 'content_block_delta', 'delta': {'thinking': 'synthetic'}}))
        self.assertEqual(observed, [1, 2, 3])

    def test_body_session_fallback_matches_gateway_for_fragmented_json(self):
        headers = Message()
        headers['Authorization'] = 'Bearer synthetic-gateway-key'
        digest = hashlib.sha256(b'synthetic-gateway-key').hexdigest()
        cases = [
            {'prompt_cache_key': 'synthetic-private-cache-key',
             'messages': [{'role': 'user', 'content': 'synthetic-private-prompt'}]},
            {'messages': [{'role': 'system', 'content': 'ignored'},
                          {'role': 'user', 'content': 'synthetic-private-prompt'},
                          {'role': 'user', 'content': 'later'}]},
            {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': '测试'},
                                                       {'z': 2, 'a': 'synthetic'}]}]},
            {'messages': [{'role': 'assistant', 'content': 'no user'}]},
        ]
        for value in cases:
            body = json.dumps(value, ensure_ascii=False).encode()
            for size in (1, 7, 67):
                budget = BodyBudget(2 * len(body))
                capture = SessionBodyCapture(digest, budget=budget)
                for start in range(0, len(body), size):
                    capture.feed(body[start:start + size])
                self.assertEqual(capture.finish(), affinity_key(headers, body, digest))
                self.assertEqual(budget.used, 0)

    def test_session_body_budget_and_size_fail_closed(self):
        budget = BodyBudget(5)
        first = SessionBodyCapture('synthetic-digest', budget=budget, max_body=10)
        second = SessionBodyCapture('synthetic-digest', budget=budget, max_body=10)
        first.feed(b'1234')
        second.feed(b'12')
        self.assertIsNone(second.finish())
        self.assertEqual(budget.used, 4)
        first.close()
        self.assertEqual(budget.used, 0)
        oversized = SessionBodyCapture('synthetic-digest', budget=budget, max_body=3)
        oversized.feed(b'1234')
        self.assertIsNone(oversized.finish())
        self.assertEqual(budget.used, 0)


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
            time.sleep(getattr(self.server, 'reject_delay', 0))
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
            def handle_error(self, request, client_address):
                pass
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

    def test_session_summary_tracks_requests_without_storing_secrets(self):
        for token, session in (('synthetic-key-a', 'synthetic-private-session'),
                               ('synthetic-key-a', 'synthetic-private-session'),
                               ('synthetic-key-b', 'synthetic-private-session')):
            connection = HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
            connection.request('POST', '/v1/chat/completions', b'{}', {
                'Authorization': 'Bearer ' + token, 'X-Session-Id': session,
                'X-LLMDeploy-Client-IP': '192.0.2.4'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
        self.wait_recorded()
        data = report(self.path, 'total', now=time.time() + .01)
        self.assertEqual(data['session_count'], 2)
        self.assertEqual(sorted(item['requests'] for item in data['sessions']), [1, 2])
        self.assertEqual(sorted(item['input'] for item in data['sessions']), [100, 200])
        self.assertTrue(all(item['first_visible'] is not None for item in data['requests']))
        for dbfile in Path(self.temp.name).glob('usage.sqlite3*'):
            raw = dbfile.read_bytes()
            for secret in (b'synthetic-key-a', b'synthetic-key-b', b'synthetic-private-session'):
                self.assertNotIn(secret, raw)

    def test_body_fallback_session_groups_without_persisting_content(self):
        bodies = [
            {'messages': [{'role': 'user', 'content': 'synthetic-private-first'}]},
            {'messages': [{'role': 'user', 'content': 'synthetic-private-first'},
                          {'role': 'assistant', 'content': 'synthetic-response'},
                          {'role': 'user', 'content': 'synthetic-second'}]},
            {'prompt_cache_key': 'synthetic-private-cache',
             'messages': [{'role': 'user', 'content': 'synthetic-private-first'}]},
        ]
        for value in bodies:
            connection = HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
            connection.request('POST', '/v1/chat/completions', json.dumps(value).encode(), {
                'Authorization': 'Bearer synthetic-gateway-key', 'Content-Type': 'application/json',
                'X-LLMDeploy-Client-IP': '192.0.2.4'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
        self.wait_recorded()
        value = report(self.path, 'total', now=time.time() + .01)
        self.assertEqual(value['totals']['unknown_session'], 0)
        self.assertEqual(sorted(item['requests'] for item in value['sessions']), [1, 2])
        for dbfile in Path(self.temp.name).glob('usage.sqlite3*'):
            raw = dbfile.read_bytes()
            for secret in (b'synthetic-gateway-key', b'synthetic-private-first', b'synthetic-private-cache'):
                self.assertNotIn(secret, raw)

    def test_body_session_parser_failure_does_not_interrupt_response(self):
        with patch('deploy_stats.proxy.SessionBodyCapture.finish', side_effect=RuntimeError):
            connection = HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
            connection.request('POST', '/v1/chat/completions', b'{"messages":[]}', {
                'Authorization': 'Bearer synthetic-gateway-key', 'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
        self.wait_recorded()
        self.assertGreater(self.proxy.recorder.dropped, 0)

    def test_body_session_feed_or_budget_failure_does_not_interrupt_response(self):
        for target in ('deploy_stats.proxy.SessionBodyCapture.feed', 'deploy_stats.session.BUDGET.reserve'):
            with self.subTest(target=target), patch(target, side_effect=RuntimeError):
                self.assertEqual(self.request()[0], 200)
                self.wait_recorded()
        self.assertGreaterEqual(self.proxy.recorder.dropped, 2)

    def test_active_snapshot_clears_when_completion_event_is_dropped(self):
        self.upstream.mode = 'slow'
        connection = socket.create_connection(('127.0.0.1', self.proxy.server_port), timeout=5)
        connection.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                           b'Content-Length: 2\r\nX-LLMDeploy-Client-IP: 192.0.2.4\r\n\r\n{}')
        self.assertTrue(connection.recv(1024))
        self.assertTrue(self.upstream.stream_started.wait(2))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            data = report(self.path, 'total', now=time.time() + .01)
            if data['active_ready'] and len(data['active']) == 1:
                break
            time.sleep(.05)
        self.assertEqual(len(data['active']), 1)
        try:
            with patch.object(self.proxy.recorder.pending, 'put_nowait', side_effect=queue.Full):
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
                deadline = time.monotonic() + 3
                while self.proxy.active and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(self.proxy.active, 0)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                data = report(self.path, 'total', now=time.time() + .01)
                if data['active_ready'] and not data['active']:
                    break
                time.sleep(.05)
            self.assertTrue(data['active_ready'])
            self.assertEqual(data['active'], [])
            self.assertGreater(self.proxy.recorder.dropped, 0)
        finally:
            self.upstream.stream_release.set()
            connection.close()

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

    def test_large_combined_headers_reach_the_existing_gateway(self):
        connection = HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        headers = {f'X-Synthetic-{i}': 'x' * 45000 for i in range(3)}
        connection.request('POST', '/v1/chat/completions', b'{}', headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        connection.close()
        self.wait_recorded()
        self.assertEqual(len(self.upstream.calls), 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows selector capacity')
    def test_windows_keeps_480_streams_in_flight(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(480)
        listener.settimeout(.1)
        accepted, clients = [], []
        stop = threading.Event()

        def accept_upstreams():
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                    accepted.append(conn)
                except socket.timeout:
                    continue
                except OSError:
                    return

        thread = threading.Thread(target=accept_upstreams, daemon=True)
        thread.start()
        proxy = Proxy(0, listener.getsockname()[1], Path(self.temp.name) / 'capacity.sqlite3')
        self.start(proxy)
        try:
            for _ in range(480):
                conn = socket.create_connection(('127.0.0.1', proxy.server_port), timeout=15)
                clients.append(conn)
                conn.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                             b'Content-Length: 2\r\nX-LLMDeploy-Client-IP: 192.0.2.4\r\n\r\n{}')
            deadline = time.monotonic() + 20
            while (len(accepted) < 480 or proxy.connections < 480) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(len(accepted), 480)
            self.assertEqual(proxy.connections, 480)
            with socket.create_connection(('127.0.0.1', proxy.server_port), timeout=5) as extra:
                extra.sendall(b'GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n')
                self.assertIn(b'429 Too Many Requests', extra.recv(1024))
            extra = HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)
            try:
                extra.request('POST', '/v1/chat/completions', b'x' * (1024 * 1024))
                rejected = extra.getresponse()
                self.assertEqual(rejected.status, 429)
                rejected.read()
            except (OSError, IncompleteRead):
                # A peer still uploading at the limit may observe a reset.
                pass
            finally:
                extra.close()
            body = sse({'usage': usage()}) + b'data: [DONE]\n\n'
            response = (b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: '
                        + str(len(body)).encode() + b'\r\n\r\n' + body)
            for conn in accepted:
                conn.sendall(response)
            for conn in clients:
                received = bytearray()
                while b'data: [DONE]\n\n' not in received:
                    part = conn.recv(4096)
                    self.assertTrue(part)
                    received.extend(part)
                self.assertIn(b'HTTP/1.1 200 OK', received)
            deadline = time.monotonic() + 10
            while proxy.connections and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(proxy.connections, 0)
            self.assertTrue(proxy.recorder.flush())
            data = report(proxy.recorder.path, 'total', now=time.time() + .01)
            self.assertEqual(data['totals']['known'], 480)
        finally:
            for conn in clients + accepted:
                conn.close()
            stop.set()
            listener.close()
            thread.join(3)

    def test_gateway_can_reject_a_large_body_before_reading_it(self):
        self.upstream.mode = 'reject'
        for delay, expect in ((0, False), (.05, True), (.15, False)):
            self.upstream.reject_delay = delay
            for size in (2, 4095, 4096, 65535, 1024 * 1024):
                connection = HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
                headers = {'X-LLMDeploy-Client-IP': '192.0.2.4'}
                if expect:
                    headers['Expect'] = '100-continue'
                connection.request('POST', '/v1/chat/completions', b'x' * size, headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 429)
                response.read()
                connection.close()
        self.wait_recorded()
        self.assertEqual(report(self.path, 'total', now=time.time() + .01)['totals']['failed'], 15)

    def test_large_invalid_auth_preserves_real_gateway_response(self):
        keyfile = Path(self.temp.name) / 'auth-keys.json'
        keyfile.write_text(json.dumps({'version': 1, 'records': {'fixture': {
            'sha256': hashlib.sha256(b'synthetic-gateway-key').hexdigest(), 'active': True}}}))
        gateway = Gateway(('127.0.0.1', 0), keyfile,
            state_path=Path(self.temp.name) / 'auth-state.json',
            bindings_file=Path(self.temp.name) / 'auth-bindings.json',
            backend_ports=[self.upstream.server_port], backend_managed_affinity=True)
        self.start(gateway)
        proxy = Proxy(0, gateway.server_port, Path(self.temp.name) / 'auth-stats.sqlite3')
        self.start(proxy)
        direct = HTTPConnection('127.0.0.1', gateway.server_port, timeout=5)
        direct.request('POST', '/v1/chat/completions', b'{}', {'Authorization': 'Bearer incorrect-placeholder'})
        response = direct.getresponse()
        expected = response.status, response.read()
        direct.close()
        self.assertEqual(expected[0], 401)
        for size in (2, 4095, 4096, 65535, 65536, 1024 * 1024):
            for expect in (False, True):
                for _ in range(2):
                    connection = HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)
                    headers = {'Authorization': 'Bearer incorrect-placeholder'}
                    if expect:
                        headers['Expect'] = '100-continue'
                    connection.request('POST', '/v1/chat/completions', b'x' * size, headers)
                    response = connection.getresponse()
                    self.assertEqual((response.status, response.read()), expected)
                    connection.close()
        for size in (4095, 4096, 1024 * 1024):
            sock = socket.create_connection(('127.0.0.1', proxy.server_port), timeout=5)
            stop = threading.Event()
            uploader = None
            try:
                sock.sendall((f'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                              f'Authorization: Bearer incorrect-placeholder\r\nContent-Length: {size}\r\n'
                              'Expect: 100-continue\r\n\r\n').encode())
                def upload():
                    remaining = size
                    while remaining and not stop.is_set():
                        amount = min(remaining, 512)
                        try:
                            sock.sendall(b'x' * amount)
                        except OSError:
                            return
                        remaining -= amount
                        time.sleep(.002)
                uploader = threading.Thread(target=upload, daemon=True)
                uploader.start()
                response = HTTPResponse(sock)
                response.begin()
                self.assertEqual((response.status, response.read()), expected)
            finally:
                stop.set()
                if uploader:
                    uploader.join(1)
                sock.close()

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

    def test_server_close_stops_a_running_proxy(self):
        proxy = Proxy(0, self.upstream.server_port, Path(self.temp.name) / 'close.sqlite3')
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        self.assertTrue(proxy.ready.wait(3))
        proxy.server_close()
        thread.join(3)
        self.assertTrue(proxy.finished.is_set())
        self.assertFalse(thread.is_alive())
        self.assertFalse(proxy.recorder.thread.is_alive())

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
        for port in (gateway.server_port, proxy.server_port):
            connection = HTTPConnection('127.0.0.1', port, timeout=5)
            connection.request('POST', '/v1/chat/completions', b'{}', {
                'Authorization': b'Bearer invalid-\xff', 'X-Session-Id': b'synthetic-\xe4\xb8\xad'})
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            connection.close()
            connection = HTTPConnection('127.0.0.1', port, timeout=5)
            connection.request('POST', '/v1/chat/completions', b'{}', {
                'Authorization': 'Bearer synthetic-gateway-key', 'X-Session-Id': b'synthetic-\xe4\xb8\xad'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
        deadline = time.monotonic() + 3
        while proxy.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(proxy.recorder.flush())
        raw = (b'Authorization: Bearer synthetic-gateway-key\r\n'
               b'X-Session-Id: synthetic-\xe4\xb8\xad\r\n\r\n')
        expected = affinity_key(parse_headers(BytesIO(raw)), b'{}', hashlib.sha256(b'synthetic-gateway-key').hexdigest())
        data = report(proxy.recorder.path, 'total', now=time.time() + .01)
        self.assertIn(expected, [row['session'] for row in data['requests']])

    @unittest.skipUnless(os.environ.get('LLMDEPLOY_CADDY'), 'Set LLMDEPLOY_CADDY for local Caddy integration')
    def test_caddy_reload_preserves_an_existing_stream(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            admin_port = probe.getsockname()[1]
        old = Path(self.temp.name) / 'Caddyfile-before'
        new = Path(self.temp.name) / 'Caddyfile-after'
        template = '''{{
    admin 127.0.0.1:{admin}
    auto_https off
}}
http://127.0.0.1:{port} {{
    log {{
        output discard
    }}
    reverse_proxy 127.0.0.1:{upstream} {{
        flush_interval 10ms
    }}
}}
'''
        old.write_text(template.format(admin=admin_port, port=port, upstream=self.upstream.server_port), encoding='utf-8')
        new.write_text(template.format(admin=admin_port, port=port, upstream=self.proxy.server_port), encoding='utf-8')
        environment = dict(os.environ)
        environment['XDG_DATA_HOME'] = self.temp.name
        environment['XDG_CONFIG_HOME'] = self.temp.name
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        process = subprocess.Popen([os.environ['LLMDEPLOY_CADDY'], 'run', '--config', str(old),
            '--adapter', 'caddyfile'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=environment, creationflags=flags)
        client = None
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail('synthetic Caddy did not start')
                    time.sleep(.05)
            self.upstream.mode = 'slow_length'
            client = socket.create_connection(('127.0.0.1', port), timeout=5)
            client.sendall((f'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                            'Content-Length: 2\r\n\r\n{}').encode())
            self.assertTrue(self.upstream.stream_started.wait(2))
            received = bytearray()
            while b'data:' not in received:
                received.extend(client.recv(4096))
            self.upstream.mode = 'json'
            reload = subprocess.run([os.environ['LLMDEPLOY_CADDY'], 'reload', '--address',
                f'127.0.0.1:{admin_port}', '--config', str(new), '--adapter', 'caddyfile'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, creationflags=flags, timeout=5)
            self.assertEqual(reload.returncode, 0)
            connection = HTTPConnection('127.0.0.1', port, timeout=5)
            connection.request('POST', '/v1/chat/completions', b'{}')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn('application/json', response.getheader('Content-Type'))
            response.read()
            connection.close()
            self.wait_recorded()
            self.assertEqual(report(self.path, 'total', now=time.time() + .01)['totals']['known'], 1)
            self.upstream.stream_release.set()
            while b'data: [DONE]\n\n' not in received:
                part = client.recv(4096)
                self.assertTrue(part)
                received.extend(part)
        finally:
            self.upstream.stream_release.set()
            if client:
                client.close()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(os.environ.get('LLMDEPLOY_CADDY'), 'Set LLMDEPLOY_CADDY for local Caddy integration')
    def test_caddy_fallback_preserves_auth_and_never_replays_post(self):
        keyfile = Path(self.temp.name) / 'fallback-keys.json'
        keyfile.write_text(json.dumps({'version': 1, 'records': {'fixture': {
            'sha256': hashlib.sha256(b'synthetic-gateway-key').hexdigest(), 'active': True}}}))
        gateway = Gateway(('127.0.0.1', 0), keyfile,
            state_path=Path(self.temp.name) / 'fallback-state.json',
            bindings_file=Path(self.temp.name) / 'fallback-bindings.json',
            backend_ports=[self.upstream.server_port], backend_managed_affinity=True)
        self.start(gateway)
        proxy = Proxy(0, gateway.server_port, Path(self.temp.name) / 'fallback-stats.sqlite3')
        self.start(proxy)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            primary_port = probe.getsockname()[1]
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        config = Path(self.temp.name) / 'Caddyfile-fallback'
        config.write_text('''{
    admin off
    auto_https off
}
http://127.0.0.1:%d {
    log {
        output discard
    }
    reverse_proxy 127.0.0.1:%d 127.0.0.1:%d {
        lb_policy first
        fail_duration 2s
        lb_try_duration 6s
        lb_try_interval 20ms
        header_up X-LLMDeploy-Client-IP {remote_host}
        flush_interval 10ms
        transport http {
            dial_timeout 5s
        }
    }
}
''' % (port, primary_port, gateway.server_port), encoding='utf-8')
        environment = dict(os.environ)
        environment['XDG_DATA_HOME'] = self.temp.name
        environment['XDG_CONFIG_HOME'] = self.temp.name
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        process = subprocess.Popen([os.environ['LLMDEPLOY_CADDY'], 'run', '--config', str(config),
            '--adapter', 'caddyfile'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=environment, creationflags=flags)
        primary = None
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail('synthetic Caddy did not start')
                    time.sleep(.05)

            def request(key):
                connection = HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('POST', '/v1/chat/completions',
                    b'{"messages":[{"role":"user","content":"synthetic"}]}',
                    {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
                return connection, connection.getresponse()

            connection, response = request('incorrect-placeholder')
            self.assertEqual(response.status, 401)
            response.read()
            connection.close()
            self.assertEqual(len(self.upstream.calls), 0)
            connection, response = request('synthetic-gateway-key')
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            self.assertEqual(len(self.upstream.calls), 1)

            class Primary(BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.1'
                def log_message(self, format, *args):
                    pass
                def do_POST(self):
                    payload = self.rfile.read(int(self.headers['Content-Length']))
                    self.server.calls += 1
                    if self.server.mode == 'drop':
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        self.close_connection = True
                        return
                    if self.server.mode == 'sse-drop':
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/event-stream')
                        self.send_header('Content-Length', '100')
                        self.end_headers()
                        self.wfile.write(b'data: partial\n\n')
                        self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        self.close_connection = True
                        return
                    upstream = HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)
                    upstream.request('POST', self.path, payload, {
                        'Content-Type': self.headers.get('Content-Type'),
                        'Authorization': self.headers.get('Authorization'),
                        'X-LLMDeploy-Client-IP': self.headers.get('X-LLMDeploy-Client-IP')})
                    result = upstream.getresponse()
                    body = result.read()
                    self.send_response(result.status)
                    self.send_header('Content-Type', result.getheader('Content-Type'))
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    upstream.close()

            class PrimaryServer(ThreadingHTTPServer):
                def handle_error(self, request, client_address):
                    pass  # Deliberate reset scenarios in this test.

            primary = PrimaryServer(('127.0.0.1', primary_port), Primary)
            primary.mode, primary.calls = 'normal', 0
            primary.daemon_threads = True
            self.start(primary)
            time.sleep(2.2)
            connection, response = request('synthetic-gateway-key')
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            self.assertEqual((primary.calls, len(self.upstream.calls)), (1, 2))
            self.assertTrue(proxy.recorder.flush())
            self.assertEqual(report(proxy.recorder.path, 'total', now=time.time() + .01)['totals']['known'], 1)

            primary.mode = 'drop'
            try:
                connection, response = request('synthetic-gateway-key')
                self.assertNotEqual(response.status, 200)
                response.read()
                connection.close()
            except (OSError, IncompleteRead):
                pass
            self.assertEqual((primary.calls, len(self.upstream.calls)), (2, 2))
            connection, response = request('synthetic-gateway-key')
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            self.assertEqual((primary.calls, len(self.upstream.calls)), (2, 3))

            time.sleep(2.2)
            primary.mode = 'sse-drop'
            connection, response = request('synthetic-gateway-key')
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(5), b'data:')
            with self.assertRaises(IncompleteRead):
                response.read()
            connection.close()
            self.assertEqual((primary.calls, len(self.upstream.calls)), (3, 3))

            time.sleep(2.2)
            primary.mode = 'normal'
            connection, response = request('synthetic-gateway-key')
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            self.assertEqual((primary.calls, len(self.upstream.calls)), (4, 4))
        finally:
            process.terminate()
            process.wait(timeout=5)

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
            connection = HTTPConnection('127.0.0.1', port, timeout=5)
            connection.request('POST', '/v1/chat/completions', b'{}', {
                'Authorization': 'Bearer synthetic-placeholder', 'X-Session-Id': b'synthetic-\xe4\xb8\xad'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            self.wait_recorded()
            gateway_headers = parse_headers(BytesIO(b'Authorization: Bearer synthetic-placeholder\r\n'
                b'X-Session-Id: synthetic-\xe4\xb8\xad\r\n\r\n'))
            expected = affinity_key(gateway_headers, b'{}', hashlib.sha256(b'synthetic-placeholder').hexdigest())
            self.assertIn(expected, [row['session'] for row in report(self.path, 'total', now=time.time() + .01)['requests']])
            large_headers = {f'X-Synthetic-{i}': 'x' * 5000 for i in range(3)}
            connection = HTTPConnection('127.0.0.1', port, timeout=5)
            connection.request('POST', '/v1/chat/completions', b'{}', large_headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
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
    def test_subsecond_coverage_gap_is_incomplete_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.sqlite3'
            now = datetime(2026, 1, 3, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
            start = now - 12 * 3600
            db = _open(path)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('first', start, now - 300.4, now - 300.4, 0))
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('second', now - 299.6, now, now, 0))
            db.commit()
            db.close()
            value = report(path, 'today', now=now)
            self.assertFalse(value['totals']['complete'])
            self.assertFalse(value['days'][-1]['complete'])
            bucket = next(point for point in value['series_24h'] if point['start'] == now - 600)
            self.assertFalse(bucket['complete'])
            self.assertAlmostEqual(bucket['covered_seconds'], 599.2, places=4)

    def test_stale_active_snapshot_is_unknown_even_with_live_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.sqlite3'
            now = time.time()
            db = _open(path)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('live', now - 60, now - 1, None, 0))
            db.execute('INSERT INTO active_samples VALUES (?,?)', ('live', now - 4))
            db.execute('INSERT INTO active_requests VALUES (?,?,?,?,?,?)',
                       ('fixture', 'live', now - 30, '192.0.2.4', None, None))
            db.commit()
            db.close()
            value = report(path, 'total', now=now)
            self.assertTrue(value['collector_live'])
            self.assertFalse(value['active_ready'])
            self.assertEqual(value['active'], [])

    def test_session_and_request_pagination_do_not_drop_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.sqlite3'
            now = time.time()
            db = _open(path)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('fixture', now - 1000, now, now, 0))
            for i in range(205):
                db.execute('''INSERT INTO requests(run_id,finished,client_ip,elapsed,http_status,state,
                    input_tokens,output_tokens,cached_tokens,session_id,first_visible)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    ('fixture', now - 205 + i, '192.0.2.4', 2, 200, 'known', 100, 7, 60,
                     f'session-{i % 105}', .5))
            db.commit()
            db.close()
            value = report(path, 'total', now=now + .01, request_offset=100, session_offset=100)
            self.assertEqual((value['request_count'], len(value['requests'])), (205, 100))
            self.assertEqual((value['session_count'], len(value['sessions'])), (105, 5))
            self.assertEqual(value['totals']['input'], 20500)

    def test_schema_one_migrates_without_losing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.sqlite3'
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE stats_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO stats_meta VALUES ('application','llmdeploy-gateway-stats');
                CREATE TABLE runs (id TEXT PRIMARY KEY,started REAL NOT NULL,last_seen REAL NOT NULL,
                    ended REAL,dropped INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE requests (id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,finished REAL NOT NULL,
                    client_ip TEXT,elapsed REAL NOT NULL,http_status INTEGER,state TEXT NOT NULL,
                    input_tokens INTEGER,output_tokens INTEGER,cached_tokens INTEGER);
                INSERT INTO runs VALUES ('old',100,200,200,0);
                INSERT INTO requests VALUES (1,'old',150,'192.0.2.4',2,200,'known',100,7,60);
                PRAGMA user_version=1;''')
            db.commit()
            db.close()
            migrated = _open(path)
            self.assertEqual(migrated.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(migrated.execute('SELECT input_tokens,session_id,first_visible FROM requests').fetchone(),
                             (100, None, None))
            migrated.execute("UPDATE runs SET started=?,last_seen=?,ended=? WHERE id='old'",
                             (1800000100, 1800000200, 1800000200))
            migrated.execute("UPDATE requests SET finished=1800000150 WHERE run_id='old'")
            migrated.commit()
            migrated.close()
            value = report(path, 'total', now=1800000201)
            self.assertEqual((value['totals']['input'], value['totals']['unknown_session']), (100, 1))

    def test_backend_metric_subset_and_unavailable(self):
        class Metrics(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
            def do_GET(self):
                body = (b'deepinfer:requests_running 2\n' b'deepinfer:requests_waiting 1\n'
                    b'deepinfer:workers_alive 8\n' b'deepinfer:admission_ready 1\n'
                    b'deepinfer:prompt_tokens_total 100\n' b'deepinfer:prompt_cache_hit_tokens_total 30\n'
                    b'deepinfer:prompt_cache_hit_requests_total 4\n')
                if getattr(self.server, 'invalid_ratio', False):
                    body = body.replace(b'prompt_cache_hit_tokens_total 30', b'prompt_cache_hit_tokens_total 130')
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Metrics)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            value = read_backend(server.server_port)
            self.assertTrue(value['available'])
            self.assertEqual((value['running'], value['waiting'], value['cached_total']), (2, 1, 30))
            self.assertEqual(value['hit_rate'], .3)
            server.invalid_ratio = True
            self.assertIsNone(read_backend(server.server_port)['hit_rate'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

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
