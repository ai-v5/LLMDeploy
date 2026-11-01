"""Serve the read-only dashboard exclusively on IPv4 loopback."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .backends import read_backend
from .report import active_report, report

STATIC = Path(__file__).resolve().parent / 'static'
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.css': ('app.css', 'text/css; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/app-trends-v1.js': ('app.js', 'text/javascript; charset=utf-8')}


class Dashboard(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port, database, backend_ports=()):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('INVALID_PORT')
        self.database = Path(database)
        if len(backend_ports) > 8 or any(type(p) is not int or not 1 <= p <= 65535 for p in backend_ports):
            raise ValueError('INVALID_BACKEND_PORTS')
        self.backend_ports = tuple(dict.fromkeys(backend_ports))
        super().__init__(('127.0.0.1', port), Handler)

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def trusted(self):
        hosts = self.headers.get_all('Host', [])
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            return False
        origins = self.headers.get_all('Origin', [])
        if len(origins) > 1 or origins and origins[0].lower() != 'http://' + hosts[0].lower():
            return False
        return self.headers.get('Sec-Fetch-Site', '').lower() not in ('cross-site', 'same-site')

    def send(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.trusted():
            self.send(403, b'Forbidden', 'text/plain; charset=utf-8')
            return
        url = urlsplit(self.path)
        if url.path in ASSETS:
            name, media = ASSETS[url.path]
            try:
                self.send(200, (STATIC / name).read_bytes(), media)
            except OSError:
                self.send(503, b'Unavailable', 'text/plain; charset=utf-8')
            return
        if url.path == '/api/active':
            if url.query:
                self.send(400, b'{"error":"INVALID_ACTIVE_QUERY"}', 'application/json; charset=utf-8')
                return
            try:
                value = active_report(self.server.database)
                body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
                self.send(200, body, 'application/json; charset=utf-8')
            except (ValueError, sqlite3.Error, OSError, OverflowError):
                self.send(503, b'{"error":"STATS_UNAVAILABLE"}', 'application/json; charset=utf-8')
            return
        if url.path == '/api/stats':
            values = parse_qs(url.query, keep_blank_values=True)
            defaults = {'window': '24h', 'offset': '0', 'limit': '100', 'sort': 'finished', 'direction': 'desc',
                        'session_offset': '0', 'session_limit': None, 'session_sort': 'last_finished',
                        'session_direction': 'desc', 'snapshot_at': None, 'snapshot_id': None}
            if set(values) - set(defaults) or any(len(value) != 1 for value in values.values()):
                self.send(400, b'{"error":"INVALID_WINDOW"}', 'application/json; charset=utf-8')
                return
            try:
                params = {key: values[key][0] if key in values else default for key, default in defaults.items()}
                if params['session_limit'] is None:
                    params['session_limit'] = params['limit']
                if any(not params[key].isascii() or not params[key].isdigit() or len(params[key]) > 10
                       for key in ('offset', 'limit', 'session_offset', 'session_limit')):
                    raise ValueError('INVALID_STATS_PAGE')
                if params['snapshot_id'] is not None and (not params['snapshot_id'].isascii() or
                        not params['snapshot_id'].isdigit() or len(params['snapshot_id']) > 19):
                    raise ValueError('INVALID_STATS_SNAPSHOT')
                if params['snapshot_at'] is not None and len(params['snapshot_at']) > 32:
                    raise ValueError('INVALID_STATS_SNAPSHOT')
                value = report(self.server.database, params['window'],
                               request_offset=int(params['offset']), request_limit=int(params['limit']),
                               session_offset=int(params['session_offset']), session_limit=int(params['session_limit']),
                               request_sort=params['sort'], request_dir=params['direction'],
                               session_sort=params['session_sort'], session_dir=params['session_direction'],
                               snapshot_at=float(params['snapshot_at']) if params['snapshot_at'] is not None else None,
                               snapshot_id=int(params['snapshot_id']) if params['snapshot_id'] is not None else None)
                value['backends'] = [read_backend(port) for port in self.server.backend_ports]
                body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
                self.send(200, body, 'application/json; charset=utf-8')
            except ValueError:
                self.send(400, b'{"error":"INVALID_OR_UNAVAILABLE_STATS"}', 'application/json; charset=utf-8')
            except (sqlite3.Error, OSError, OverflowError):
                self.send(503, b'{"error":"STATS_UNAVAILABLE"}', 'application/json; charset=utf-8')
            return
        self.send(404, b'Not found', 'text/plain; charset=utf-8')
