"""Authenticated HTTP/SSE gateway to loopback inference backends.

No API key, request body, Authorization header or response body is logged.
Client Authorization is consumed here and never forwarded to the model.
"""
import argparse
import hashlib
import hmac
from http.client import HTTPConnection, HTTPException, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import select
import socket
import threading
import time
from urllib.parse import urlsplit

from .routing import Busy, SessionRouter, Unavailable, affinity_key, prefix_rank

HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade'}


class Backends(SessionRouter):
    def __init__(self, ports, state_path, bindings_file, max_active=32, max_waiting=32, queue_timeout=5, control_file=None):
        super().__init__(ports, bindings_file, max_active=max_active, max_waiting=max_waiting, queue_timeout=queue_timeout)
        self.control_file = Path(control_file) if control_file else None
        self.stop = threading.Event()
        self.state_path = Path(state_path) if state_path else None
        self.versions = {p: 0 for p in self.ports}
        self.successes = {p: 0 for p in self.ports}
        self.probe(bootstrap=True)
        self.thread = threading.Thread(target=self.watch, daemon=True)
        self.thread.start()

    def failed(self, port):
        with self.lock:
            self.versions[port] += 1
            self.successes[port] = 0
            self.healthy[port] = False
            self.changed.notify_all()

    def probe(self, bootstrap=False):
        enabled = set(self.ports)
        if self.control_file:
            try:
                values = json.loads(self.control_file.read_text())['enabled_ports']
                if not isinstance(values, list) or any(type(p) is not int or p not in self.ports for p in values):
                    raise ValueError('INVALID_CONTROL_PORTS')
                enabled = set(values)
            except (OSError, ValueError, KeyError, TypeError):
                enabled = set()
        with self.lock:
            self.enabled_ports = enabled
            versions = dict(self.versions)
        observed = {}
        for port in self.ports:
            connection = HTTPConnection('127.0.0.1', port, timeout=1)
            try:
                connection.request('GET', '/health', headers={'Connection': 'close'})
                response = connection.getresponse()
                observed[port] = response.status == 200
            except (OSError, HTTPException):
                observed[port] = False
            finally:
                connection.close()
        with self.lock:
            for port, good in observed.items():
                if versions[port] != self.versions[port]:
                    continue
                self.successes[port] = self.successes[port] + 1 if good else 0
                self.healthy[port] = good and (bootstrap or self.healthy[port] or self.successes[port] >= 2)
            self.changed.notify_all()
        state = {'epoch': time.time(), **self.snapshot()}
        if self.state_path:
            try:
                temp = self.state_path.with_suffix('.new')
                temp.write_text(json.dumps(state, separators=(',', ':')), encoding='utf-8')
                temp.replace(self.state_path)
            except OSError:
                pass

    def watch(self):
        while not self.stop.wait(2):
            self.probe()

class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256

    def __init__(self, address, key_store, upstream_port=19001, backup_port=19002, state_path=None, bindings_file=None, upstream_timeout=10800, model_context_limit=None, backend_context_limits=None, *, backend_ports=None, max_active=32, max_waiting=32, queue_timeout=5, control_file=None, backend_managed_affinity=False):
        if bindings_file is None or Path(bindings_file).resolve() == Path(key_store).resolve():
            raise ValueError('A separate private bindings file is required')
        if state_path and Path(state_path).resolve() in (Path(key_store).resolve(), Path(bindings_file).resolve()):
            raise ValueError('State, bindings and key store must use separate files')
        paths = [Path(key_store), Path(bindings_file), Path(str(bindings_file) + '.new')]
        if state_path:
            paths += [Path(state_path), Path(state_path).with_suffix('.new')]
        if control_file:
            paths += [Path(control_file), Path(str(control_file) + '.new')]
        if len(set(p.resolve() for p in paths)) != len(paths):
            raise ValueError('Runtime files and temporary files must not overlap')
        if not math.isfinite(upstream_timeout) or upstream_timeout <= 0:
            raise ValueError('Invalid upstream timeout')
        self.upstream_timeout = upstream_timeout
        if model_context_limit is not None and (type(model_context_limit) is not int or model_context_limit <= 0):
            raise ValueError('Invalid common model context limit')
        self.model_context_limit = model_context_limit
        ports = list(backend_ports) if backend_ports is not None else [upstream_port, backup_port]
        if not ports or len(set(ports)) != len(ports) or any(type(p) is not int or not 1 <= p <= 65535 for p in ports):
            raise ValueError('Invalid backend ports')
        self.backend_context_limits = dict(backend_context_limits or {})
        if self.backend_context_limits and (set(self.backend_context_limits) != set(ports)
                or any(type(p) is not int or type(limit) is not int or not 0 < limit <= 10**9 for p, limit in self.backend_context_limits.items())):
            raise ValueError('Context limits must cover every configured backend')
        if model_context_limit is not None and self.backend_context_limits and min(self.backend_context_limits.values()) < model_context_limit:
            raise ValueError('Common model context exceeds a backend limit')
        self.key_store = Path(key_store)
        self.upstream_port = upstream_port
        self.backend_managed_affinity = backend_managed_affinity
        if type(max_active) is not int or max_active < 1 or type(max_waiting) is not int or max_waiting < 0:
            raise ValueError('Invalid admission limits')
        self.request_slots = threading.BoundedSemaphore(max_active * len(ports) + max_waiting)
        self.authorized('startup-validation-placeholder')
        data = json.loads(self.key_store.read_text())
        if not any(record['active'] for record in data['records'].values()):
            raise ValueError('No active gateway keys')
        super().__init__(address, Handler)
        try:
            self.backends = Backends(ports, state_path, bindings_file, max_active, max_waiting, queue_timeout, control_file)
        except Exception:
            super().server_close()
            raise

    def server_close(self):
        self.backends.stop.set()
        super().server_close()

    def authorized(self, value):
        data = json.loads(self.key_store.read_text())
        if data.get('version') != 1 or not isinstance(data.get('records'), dict):
            raise ValueError('Unsupported key store')
        digest = hashlib.sha256(value.encode()).hexdigest()
        accepted = False
        for record in data['records'].values():
            expected = record['sha256']
            if not re.fullmatch(r'[0-9a-f]{64}', expected) or not isinstance(record['active'], bool):
                raise ValueError('Invalid key record')
            matches = hmac.compare_digest(digest, expected)
            accepted = accepted or (matches and record['active'])
        return accepted

    def context_route(self, headers):
        values = headers.get_all('x-model-context-window', [])
        if not values:
            return None, self.model_context_limit
        if len(values) != 1 or not re.fullmatch(r'[0-9]{1,10}', values[0]) or int(values[0]) <= 0:
            raise ValueError('Invalid context window')
        requested = int(values[0])
        if not self.backend_context_limits:
            raise ValueError('Context routing is not configured')
        eligible = {port for port, limit in self.backend_context_limits.items() if limit >= requested}
        if not eligible:
            raise Unavailable('No backend supports this context window')
        return eligible, requested

    def handle_error(self, request, client_address):
        # The client can disconnect during a long stream. Avoid traceback noise.
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'DSHGateway'
    sys_version = ''

    def log_message(self, format, *args):
        pass

    def error(self, status, message):
        body = json.dumps({'error': {'message': message, 'type': 'gateway_error'}}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        if status == 429:
            self.send_header('Retry-After', '1')
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        self.proxy()

    def do_POST(self):
        self.proxy()

    def do_HEAD(self):
        self.proxy()

    def proxy(self):
        if not self.server.request_slots.acquire(blocking=False):
            self.error(429, 'Service request capacity reached')
            return
        try:
            self.proxy_request()
        finally:
            self.server.request_slots.release()

    def proxy_request(self):
        self.connection.settimeout(30)
        authorization = self.headers.get_all('Authorization', [])
        if len(authorization) != 1:
            self.error(401, 'A bearer API key is required')
            return
        fields = authorization[0].split()
        if len(fields) != 2 or fields[0].lower() != 'bearer' or len(fields[1]) > 256:
            self.error(401, 'Invalid bearer API key')
            return
        try:
            accepted = self.server.authorized(fields[1])
        except (OSError, ValueError, KeyError, TypeError):
            self.error(503, 'Gateway credential store is unavailable')
            return
        if not accepted:
            self.error(401, 'Invalid bearer API key')
            return
        url = urlsplit(self.path)
        if url.scheme or url.netloc or not (url.path.startswith('/v1/') or url.path == '/health'):
            self.error(404, 'API route is unavailable')
            return
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) > 1:
            self.error(400, 'A single Content-Length is required')
            return
        try:
            length = int(lengths[0]) if lengths else 0
            if not 0 <= length <= 64 * 1024 * 1024:
                raise ValueError()
        except ValueError:
            self.error(413, 'Invalid or oversized request body')
            return
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            self.error(408, 'Request body timed out')
            return
        if len(body) != length:
            self.error(400, 'Incomplete request body')
            return
        headers = {'Accept-Encoding': 'identity', 'Connection': 'close'}
        for name in ['Content-Type', 'Accept', 'User-Agent']:
            value = self.headers.get(name)
            if value:
                headers[name] = value
        balanced = self.command == 'POST'
        try:
            eligible, context_limit = self.server.context_route(self.headers)
            key = affinity_key(self.headers, body, hashlib.sha256(fields[1].encode()).hexdigest()) if balanced else None
        except ValueError:
            self.error(400, 'Invalid session identifier or context window')
            return
        except (Unavailable, OSError):
            self.error(503, 'Session routing is unavailable')
            return
        for name in ['x-session-affinity', 'x-deepseek-harness-session-id', 'x-session-id', 'x-deepinfer-cache-namespace']:
            if self.headers.get(name):
                headers[name] = self.headers[name]
        if not self.server.backend_managed_affinity and self.command == 'POST' and url.path in ('/v1/chat/completions', '/v1/completions'):
            rank = prefix_rank(key)
            if rank is not None:
                headers['X-data-parallel-rank'] = str(rank)
        excluded = set()
        for _ in self.server.backends.ports:
            try:
                port, generation = self.server.backends.acquire(key, balanced, eligible, excluded)
            except Busy:
                self.error(429, 'Backend capacity reached; retry later')
                return
            except (Unavailable, OSError):
                self.error(503, 'No eligible model backend is available')
                return
            excluded.add(port)
            upstream = HTTPConnection('127.0.0.1', port, timeout=2)
            sent_headers = False
            stopped = threading.Event()
            disconnected = threading.Event()
            watcher = None
            try:
                upstream.connect()
                transport = upstream.sock
                def watch_disconnect():
                    while not stopped.wait(.1):
                        try:
                            ready, _, _ = select.select([self.connection], [], [], 0)
                            if ready and not self.connection.recv(1, socket.MSG_PEEK):
                                disconnected.set()
                                transport.shutdown(socket.SHUT_RDWR)
                                return
                        except OSError:
                            disconnected.set()
                            try:
                                transport.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                            return
                watcher = threading.Thread(target=watch_disconnect, daemon=True)
                watcher.start()
                upstream.sock.settimeout(self.server.upstream_timeout)
                upstream.request(self.command, self.path, body=body, headers=headers)
                response = upstream.getresponse()
                if response.status >= 500:
                    self.server.backends.failed(port)
                    continue
                model_body = None
                if self.command == 'GET' and url.path == '/v1/models' and response.status == 200 and context_limit is not None:
                    raw = response.read(1024 * 1024 + 1)
                    if response.length:
                        raise IncompleteRead(b'', response.length)
                    try:
                        if len(raw) > 1024 * 1024:
                            raise ValueError('Oversized model metadata')
                        models = json.loads(raw)
                        for model in models['data']:
                            limit = model.get('max_model_len')
                            if type(limit) is int and limit > 0:
                                model['max_model_len'] = min(limit, context_limit)
                        model_body = json.dumps(models, separators=(',', ':')).encode()
                    except (ValueError, KeyError, TypeError, AttributeError) as error:
                        raise HTTPException('Invalid model metadata') from error
                try:
                    self.send_response(response.status)
                    dynamic = {x.strip().lower() for x in (response.getheader('Connection') or '').split(',')}
                    for name, value in response.getheaders():
                        if name.lower() not in HOP_HEADERS | dynamic | {'server', 'date', 'content-length', 'x-dsh-backend-port'}:
                            self.send_header(name, value)
                    self.send_header('X-DSH-Backend-Port', str(port))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                except OSError:
                    self.close_connection = True
                    return
                sent_headers = True
                self.close_connection = True
                if model_body is not None:
                    try:
                        self.wfile.write(model_body)
                    except OSError:
                        self.close_connection = True
                    return
                if self.command != 'HEAD':
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            if response.length:
                                raise IncompleteRead(b'', response.length)
                            break
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except OSError:
                            self.close_connection = True
                            return
                return
            except Unavailable:
                if not sent_headers:
                    self.error(503, 'Session binding is unavailable')
                return
            except (OSError, TimeoutError, HTTPException):
                if disconnected.is_set():
                    return
                self.server.backends.failed(port)
                if sent_headers:
                    # Never splice another generation into an already-started stream.
                    self.close_connection = True
                    return
            finally:
                stopped.set()
                if watcher is not None:
                    watcher.join(.3)
                upstream.close()
                if balanced:
                    self.server.backends.end(port)
        self.error(502, 'No healthy model backend is available')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18900)
    parser.add_argument('--upstream-port', type=int, default=19001)
    parser.add_argument('--backup-port', type=int, default=19002)
    parser.add_argument('--backend-port', action='append', type=int)
    parser.add_argument('--max-active-per-backend', type=int, default=32)
    parser.add_argument('--max-waiting', type=int, default=32)
    parser.add_argument('--queue-timeout', type=float, default=5)
    parser.add_argument('--control-file', type=Path)
    parser.add_argument('--backend-managed-affinity', action='store_true')
    parser.add_argument('--backend-state', type=Path, required=True)
    parser.add_argument('--bindings-file', type=Path, required=True)
    parser.add_argument('--key-store', type=Path, required=True)
    parser.add_argument('--upstream-timeout', type=float, default=10800)
    parser.add_argument('--model-context-limit', type=int)
    parser.add_argument('--backend-context-limit', action='append', default=[], metavar='PORT=TOKENS')
    args = parser.parse_args()
    limits = {}
    for item in args.backend_context_limit:
        if not re.fullmatch(r'[0-9]{1,5}=[0-9]{1,10}', item):
            parser.error('Backend context limits must use PORT=TOKENS')
        port, limit = map(int, item.split('='))
        if port in limits:
            parser.error('Duplicate backend context limit')
        limits[port] = limit
    server = Gateway((args.listen, args.port), args.key_store, args.upstream_port, args.backup_port, args.backend_state, args.bindings_file, args.upstream_timeout, args.model_context_limit, limits,
                     backend_ports=args.backend_port, max_active=args.max_active_per_backend, max_waiting=args.max_waiting,
                     queue_timeout=args.queue_timeout, control_file=args.control_file, backend_managed_affinity=args.backend_managed_affinity)
    print(json.dumps({'state': 'LISTENING', 'listen': args.listen, 'port': args.port}), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
