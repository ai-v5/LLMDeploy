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
import threading
import time
from urllib.parse import urlsplit

from .routing import SessionRouter, Unavailable, affinity_key, prefix_rank

HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade'}


class Backends(SessionRouter):
    def __init__(self, ports, state_path, bindings_file):
        super().__init__(ports, bindings_file)
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

    def probe(self, bootstrap=False):
        with self.lock:
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

    def __init__(self, address, key_store, upstream_port=19001, backup_port=19002, state_path=None, bindings_file=None, upstream_timeout=10800, model_context_limit=None):
        if bindings_file is None or Path(bindings_file).resolve() == Path(key_store).resolve():
            raise ValueError('A separate private bindings file is required')
        if state_path and Path(state_path).resolve() in (Path(key_store).resolve(), Path(bindings_file).resolve()):
            raise ValueError('State, bindings and key store must use separate files')
        paths = [Path(key_store), Path(bindings_file), Path(str(bindings_file) + '.new')]
        if state_path:
            paths += [Path(state_path), Path(state_path).with_suffix('.new')]
        if len(set(p.resolve() for p in paths)) != len(paths):
            raise ValueError('Runtime files and temporary files must not overlap')
        if not math.isfinite(upstream_timeout) or upstream_timeout <= 0:
            raise ValueError('Invalid upstream timeout')
        self.upstream_timeout = upstream_timeout
        if model_context_limit is not None and (type(model_context_limit) is not int or model_context_limit <= 0):
            raise ValueError('Invalid common model context limit')
        self.model_context_limit = model_context_limit
        self.key_store = Path(key_store)
        self.upstream_port = upstream_port
        self.authorized('startup-validation-placeholder')
        data = json.loads(self.key_store.read_text())
        if not any(record['active'] for record in data['records'].values()):
            raise ValueError('No active gateway keys')
        super().__init__(address, Handler)
        try:
            self.backends = Backends([upstream_port, backup_port], state_path, bindings_file)
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
            if not 0 <= length <= 32 * 1024 * 1024:
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
            key = affinity_key(self.headers, body, hashlib.sha256(fields[1].encode()).hexdigest()) if balanced else None
            ports, generation = self.server.backends.order(key, balanced)
        except ValueError:
            self.error(400, 'Invalid session identifier')
            return
        except (Unavailable, OSError):
            self.error(503, 'Session routing is unavailable')
            return
        if self.command == 'POST' and url.path in ('/v1/chat/completions', '/v1/completions'):
            rank = prefix_rank(key)
            if rank is not None:
                headers['X-data-parallel-rank'] = str(rank)
        for port in ports:
            self.server.backends.begin(port)
            upstream = HTTPConnection('127.0.0.1', port, timeout=2)
            sent_headers = False
            try:
                upstream.connect()
                upstream.sock.settimeout(self.server.upstream_timeout)
                upstream.request(self.command, self.path, body=body, headers=headers)
                response = upstream.getresponse()
                if response.status >= 500:
                    self.server.backends.failed(port)
                    continue
                model_body = None
                if self.command == 'GET' and url.path == '/v1/models' and response.status == 200 and self.server.model_context_limit is not None:
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
                                model['max_model_len'] = min(limit, self.server.model_context_limit)
                        model_body = json.dumps(models, separators=(',', ':')).encode()
                    except (ValueError, KeyError, TypeError, AttributeError) as error:
                        raise HTTPException('Invalid model metadata') from error
                if balanced and port != ports[0]:
                    self.server.backends.bind(key, port, generation)
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
                self.server.backends.failed(port)
                if sent_headers:
                    # Never splice another generation into an already-started stream.
                    self.close_connection = True
                    return
            finally:
                upstream.close()
                self.server.backends.end(port)
        self.error(502, 'No healthy model backend is available')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18900)
    parser.add_argument('--upstream-port', type=int, default=19001)
    parser.add_argument('--backup-port', type=int, default=19002)
    parser.add_argument('--backend-state', type=Path, required=True)
    parser.add_argument('--bindings-file', type=Path, required=True)
    parser.add_argument('--key-store', type=Path, required=True)
    parser.add_argument('--upstream-timeout', type=float, default=10800)
    parser.add_argument('--model-context-limit', type=int)
    args = parser.parse_args()
    server = Gateway((args.listen, args.port), args.key_store, args.upstream_port, args.backup_port, args.backend_state, args.bindings_file, args.upstream_timeout, args.model_context_limit)
    print(json.dumps({'state': 'LISTENING', 'listen': args.listen, 'port': args.port}), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
