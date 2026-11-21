"""Route OpenAI-compatible model aliases alongside an existing gateway.

Client and upstream credentials, request bodies and response bodies are never
logged. The default route remains responsible for its own model scheduling.
"""
import argparse
from collections import OrderedDict
import hashlib
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import select
import socket
import threading
import time
from urllib.parse import quote, urlsplit

from deploy_gateway.auth import authorized
from deploy_gateway.routing import affinity_key
from deploy_gateway.server import HOP_HEADERS
from .credentials import read_windows_credential

MAX_BODY = 64 * 1024 * 1024
MAX_MODELS = 1024 * 1024


def origin(value, *, local=False):
    url = urlsplit(value)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
            or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('An HTTP origin without a path or credentials is required')
    if local and (url.scheme != 'http' or url.hostname not in ('127.0.0.1', '::1')):
        raise ValueError('The existing gateway must use a loopback HTTP origin')
    return url.scheme, url.hostname, url.port or (443 if url.scheme == 'https' else 80)


def connection(target, timeout):
    scheme, host, port = target
    return (HTTPSConnection if scheme == 'https' else HTTPConnection)(host, port, timeout=timeout)


def alias_response(raw, backend_model, alias):
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and value.get('model') == backend_model:
            value['model'] = alias
            return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    except (ValueError, UnicodeError):
        pass
    return raw


class ModelRoute:
    def __init__(self, spec):
        if not isinstance(spec, dict) or set(spec) != {'alias', 'backend_model', 'max_context', 'backends'}:
            raise ValueError('Invalid model route')
        self.alias, self.backend_model, self.max_context = spec['alias'], spec['backend_model'], spec['max_context']
        if (not isinstance(self.alias, str) or not re.fullmatch(r'[A-Za-z0-9._-]+', self.alias)
                or not isinstance(self.backend_model, str) or not self.backend_model
                or type(self.max_context) is not int or self.max_context <= 0):
            raise ValueError('Invalid model mapping')
        if not isinstance(spec['backends'], list) or not spec['backends']:
            raise ValueError('At least one model backend is required')
        self.backends = []
        for item in spec['backends']:
            if not isinstance(item, dict) or set(item) - {'origin', 'credential_target'} or 'origin' not in item:
                raise ValueError('Invalid model backend')
            target = item.get('credential_target')
            if target is not None and (not isinstance(target, str) or not target):
                raise ValueError('Invalid upstream credential target')
            self.backends.append((origin(item['origin']), target))
        if len({entry[0] for entry in self.backends}) != len(self.backends):
            raise ValueError('Duplicate model backend')
        self.lock = threading.Lock()
        self.inflight = [0] * len(self.backends)
        self.failed_until = [0.0] * len(self.backends)
        self.failure_code = [0] * len(self.backends)
        self.bindings = OrderedDict()
        self.cursor = 0

    def acquire(self, key, excluded):
        with self.lock:
            good = [i for i in range(len(self.backends)) if i not in excluded and self.failed_until[i] <= time.monotonic()]
            if not good:
                return None
            preferred = self.bindings.get(key) if key is not None else None
            if preferred in good:
                chosen = preferred
            else:
                chosen = min(good, key=lambda i: (self.inflight[i], (i - self.cursor) % len(self.backends)))
            if key is not None:
                self.bindings[key] = chosen
                self.bindings.move_to_end(key)
                if len(self.bindings) > 16384:
                    self.bindings.popitem(last=False)
            self.inflight[chosen] += 1
            self.cursor = (chosen + 1) % len(self.backends)
            return chosen

    def release(self, index, failure=0):
        with self.lock:
            self.inflight[index] -= 1
            if failure:
                self.failure_code[index] = failure
                self.failed_until[index] = time.monotonic() + (1 if failure == 429 else 5)

    def unavailable_status(self):
        with self.lock:
            now = time.monotonic()
            return 429 if all(t > now and c == 429 for t, c in zip(self.failed_until, self.failure_code)) else 503

    def metadata(self):
        return {'id': self.alias, 'object': 'model', 'created': 0,
                'owned_by': 'model-router', 'max_model_len': self.max_context}


class ModelRouter(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256

    def __init__(self, address, *, key_store, fallback_origin, routes, key_provider=None,
                 upstream_timeout=10800, max_requests=256, max_body_bytes=512 * 1024 * 1024):
        self.key_store = Path(key_store)
        authorized(self.key_store, 'startup-validation-placeholder')
        self.fallback = origin(fallback_origin, local=True)
        if not isinstance(routes, list) or not routes:
            raise ValueError('At least one model route is required')
        models = [ModelRoute(route) for route in routes]
        self.routes = {route.alias: route for route in models}
        if len(self.routes) != len(models):
            raise ValueError('Duplicate model alias')
        if upstream_timeout <= 0:
            raise ValueError('Invalid upstream timeout')
        if type(max_requests) is not int or max_requests < 1 or type(max_body_bytes) is not int or max_body_bytes < 1:
            raise ValueError('Invalid request admission limits')
        self.slots = threading.BoundedSemaphore(max_requests)
        self.body_limit = max_body_bytes
        self.body_bytes = 0
        self.body_lock = threading.Lock()
        self.key_provider = key_provider or read_windows_credential
        self.upstream_timeout = upstream_timeout
        super().__init__(address, Handler)
        if self.fallback[2] == self.server_port:
            super().server_close()
            raise ValueError('The fallback cannot use the router listener port')

    def handle_error(self, request, client_address):
        pass

    def reserve_body(self, size):
        with self.body_lock:
            if self.body_bytes + size > self.body_limit:
                return False
            self.body_bytes += size
            return True

    def release_body(self, size):
        with self.body_lock:
            self.body_bytes -= size


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'ModelRouter'
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

    def authenticate(self):
        values = self.headers.get_all('Authorization', [])
        if len(values) != 1:
            self.error(401, 'A bearer API key is required')
            return False
        fields = values[0].split()
        if len(fields) != 2 or fields[0].lower() != 'bearer' or len(fields[1]) > 256:
            self.error(401, 'Invalid bearer API key')
            return False
        try:
            good = authorized(self.server.key_store, fields[1])
        except (OSError, ValueError, KeyError, TypeError):
            self.error(503, 'Gateway credential store is unavailable')
            return False
        if not good:
            self.error(401, 'Invalid bearer API key')
        else:
            self.key_digest = hashlib.sha256(fields[1].encode()).hexdigest()
        return good

    def request_headers(self, *, backend=False, credential_target=None):
        dynamic = {x.strip().lower() for v in self.headers.get_all('Connection', []) for x in v.split(',')}
        excluded = HOP_HEADERS | dynamic | {'host', 'content-length', 'authorization', 'accept-encoding'}
        if backend:
            allowed = {'content-type', 'accept', 'user-agent'}
            headers = {name: value for name, value in self.headers.items() if name.lower() in allowed}
        else:
            headers = {name: value for name, value in self.headers.items() if name.lower() not in excluded}
        headers.update({'Accept-Encoding': 'identity', 'Connection': 'close'})
        if not backend:
            headers['Authorization'] = self.headers['Authorization']
        elif credential_target is not None:
            headers['Authorization'] = 'Bearer ' + self.server.key_provider(credential_target)
        return headers

    def send_headers(self, response, *, length=None, chunked=False):
        self.send_response(response.status)
        dynamic = {x.strip().lower() for x in (response.getheader('Connection') or '').split(',')}
        for name, value in response.getheaders():
            if name.lower() not in HOP_HEADERS | dynamic | {'server', 'date', 'content-length'}:
                self.send_header(name, value)
        if length is not None:
            self.send_header('Content-Length', str(length))
        elif chunked:
            self.send_header('Transfer-Encoding', 'chunked')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

    def proxy(self):
        if not self.server.slots.acquire(blocking=False):
            self.error(429, 'Service request capacity reached')
            return
        try:
            self.proxy_request()
        finally:
            self.server.slots.release()

    def proxy_request(self):
        self.connection.settimeout(30)
        if not self.authenticate():
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
            if not 0 <= length <= MAX_BODY:
                raise ValueError()
        except ValueError:
            self.error(413, 'Invalid or oversized request body')
            return
        if not self.server.reserve_body(length):
            self.error(429, 'Request body capacity reached')
            return
        try:
            self.proxy_body(url, length)
        finally:
            self.server.release_body(length)

    def proxy_body(self, url, length):
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            self.error(408, 'Request body timed out')
            return
        if len(body) != length:
            self.error(400, 'Incomplete request body')
            return
        if self.command == 'GET' and url.path == '/v1/models':
            self.models()
            return
        if self.command == 'GET':
            for route in self.server.routes.values():
                if url.path == '/v1/models/' + quote(route.alias, safe=''):
                    self.model_detail(route)
                    return
        route = None
        affinity = None
        if self.command == 'POST' and body:
            try:
                value = json.loads(body)
                route = self.server.routes.get(value.get('model')) if isinstance(value, dict) and isinstance(value.get('model'), str) else None
                if route:
                    if len(route.backends) > 1:
                        affinity = affinity_key(self.headers, body, self.key_digest)
                    value['model'] = route.backend_model
                    body = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
            except (ValueError, UnicodeError):
                if route:
                    self.error(400, 'Invalid session identifier')
                    return
        if route:
            windows = self.headers.get_all('x-model-context-window', [])
            if windows and (len(windows) != 1 or not re.fullmatch(r'[0-9]{1,10}', windows[0])
                            or not 0 < int(windows[0]) <= route.max_context):
                self.error(503, 'No backend supports this context window')
                return
            excluded = set()
            while True:
                index = route.acquire(affinity, excluded)
                if index is None:
                    status = route.unavailable_status()
                    self.error(status, 'Model backend capacity reached' if status == 429 else 'No eligible model backend is available')
                    return
                failure = 0
                try:
                    target, credential = route.backends[index]
                    retry, failure = self.forward(target, body, route, credential, retryable=len(route.backends) > 1)
                finally:
                    route.release(index, failure=failure)
                if not retry:
                    return
                excluded.add(index)
        else:
            self.forward(self.server.fallback, body)

    def models(self):
        fallback = connection(self.server.fallback, 2)
        sent = False
        try:
            fallback.request('GET', self.path, headers=self.request_headers())
            response = fallback.getresponse()
            raw = response.read(MAX_MODELS + 1)
            if len(raw) > MAX_MODELS or response.length:
                raise HTTPException('Invalid model list')
            if response.status >= 500:
                self.standalone_catalog()
                return
            if response.status == 200:
                try:
                    models = json.loads(raw)
                    if not isinstance(models.get('data'), list):
                        raise ValueError('Invalid model list')
                    models['data'] = [m for m in models['data'] if not isinstance(m, dict) or m.get('id') not in self.server.routes]
                    models['data'].extend(route.metadata() for route in self.server.routes.values())
                    raw = json.dumps(models, ensure_ascii=False, separators=(',', ':')).encode()
                except (ValueError, TypeError, AttributeError):
                    raise HTTPException('Invalid model list')
            self.send_headers(response, length=len(raw))
            sent = True
            self.wfile.write(raw)
        except (OSError, HTTPException, TimeoutError):
            if not sent:
                self.standalone_catalog()
        finally:
            fallback.close()

    def standalone_catalog(self):
        data = {'object': 'list', 'data': [route.metadata() for route in self.server.routes.values()]}
        raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-LLMDeploy-Legacy-Status', 'unavailable')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def model_detail(self, route):
        raw = json.dumps(route.metadata(), ensure_ascii=False, separators=(',', ':')).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def forward(self, target, body, route=None, credential_target=None, *, retryable=False):
        backend = route is not None
        upstream = connection(target, 5)
        stopped = threading.Event()
        disconnected = threading.Event()
        watcher = None
        sent = False
        try:
            headers = self.request_headers(backend=backend, credential_target=credential_target)
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
            upstream.request(self.command, self.path, body=body if self.command == 'POST' else None, headers=headers)
            response = upstream.getresponse()
            if backend and retryable and (response.status == 429 or response.status >= 500):
                status = 429 if response.status == 429 else 503
                return True, status
            content_type = (response.getheader('Content-Type') or '').lower()
            if backend and response.status in (401, 403):
                self.error(502, 'Model backend authentication failed')
                return False, 0
            if backend and self.command != 'HEAD' and 'application/json' in content_type:
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY or response.length:
                    raise HTTPException('Invalid model response')
                raw = alias_response(raw, route.backend_model, route.alias)
                try:
                    self.send_headers(response, length=len(raw))
                except OSError:
                    disconnected.set()
                    raise
                sent = True
                try:
                    self.wfile.write(raw)
                except OSError:
                    disconnected.set()
                    raise
            else:
                original_length = response.getheader('Content-Length')
                length = int(original_length) if not backend and original_length and original_length.isdecimal() else None
                chunked = self.command != 'HEAD' and length is None
                try:
                    self.send_headers(response, length=length, chunked=chunked)
                except OSError:
                    disconnected.set()
                    raise
                sent = True
                if self.command != 'HEAD':
                    def write_chunk(chunk):
                        try:
                            if chunked:
                                self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
                            else:
                                self.wfile.write(chunk)
                            self.wfile.flush()
                        except OSError:
                            disconnected.set()
                            raise

                    if backend and 'text/event-stream' in content_type:
                        done = False
                        while True:
                            line = response.readline()
                            if not line:
                                break
                            if line.startswith(b'data:'):
                                ending = b'\r\n' if line.endswith(b'\r\n') else b'\n' if line.endswith(b'\n') else b''
                                data = line[5:].strip()
                                done = done or data == b'[DONE]'
                                line = b'data: ' + alias_response(data, route.backend_model, route.alias) + ending
                            write_chunk(line)
                        if (urlsplit(self.path).path in ('/v1/chat/completions', '/v1/completions') and not done):
                            raise HTTPException('Incomplete model stream')
                    else:
                        while True:
                            chunk = response.read1(65536)
                            if not chunk:
                                break
                            write_chunk(chunk)
                    if response.length:
                        raise HTTPException('Incomplete upstream response')
                    if chunked:
                        try:
                            self.wfile.write(b'0\r\n\r\n')
                            self.wfile.flush()
                        except OSError:
                            disconnected.set()
                            raise
        except (OSError, HTTPException, TimeoutError, ValueError, UnicodeError):
            if disconnected.is_set():
                return False, 0
            if not sent and retryable and not disconnected.is_set():
                return True, 503
            if not sent:
                try:
                    self.error(502, 'Model route is unavailable')
                except OSError:
                    pass
            self.close_connection = True
            return False, 503 if backend else 0
        finally:
            stopped.set()
            if watcher is not None:
                watcher.join(.3)
            upstream.close()
        return False, 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--key-store', type=Path, required=True)
    parser.add_argument('--fallback-origin', required=True)
    parser.add_argument('--routes-file', type=Path, required=True)
    args = parser.parse_args()
    server = ModelRouter((args.listen, args.port), key_store=args.key_store,
                         fallback_origin=args.fallback_origin,
                         routes=json.loads(args.routes_file.read_text(encoding='utf-8'))['routes'])
    try:
        server.serve_forever()
    finally:
        server.server_close()
