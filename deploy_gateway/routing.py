"""Keep only irreversible session fingerprints and loopback port bindings."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading


class Unavailable(Exception):
    pass


def affinity_key(headers, body, key_digest):
    session = None
    for name in ('x-session-affinity', 'x-deepseek-harness-session-id', 'x-session-id'):
        values = headers.get_all(name, [])
        if values:
            if len(values) != 1 or not 0 < len(values[0]) <= 256:
                raise ValueError('INVALID_SESSION_IDENTIFIER')
            session = values[0]
            break
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        value = None
    if session is None and isinstance(value, dict):
        if isinstance(value.get('prompt_cache_key'), str) and value['prompt_cache_key']:
            session = value['prompt_cache_key']
    if session is not None:
        seed = 'session:' + session
    elif isinstance(value, dict) and isinstance(value.get('messages'), list):
        first = next((m for m in value['messages'] if isinstance(m, dict) and m.get('role') == 'user'), None)
        if first is None or not isinstance(first.get('content'), (str, list)):
            return None
        seed = 'first-user:' + json.dumps(first['content'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    else:
        return None
    return hashlib.sha256((key_digest + '\0' + seed).encode()).hexdigest()


def prefix_rank(key):
    return int(key[:16], 16) % 8 if key is not None else None


class SessionRouter:
    def __init__(self, ports, bindings_file, max_sessions=16384):
        self.ports = tuple(dict.fromkeys(ports))
        if not self.ports or any(type(p) is not int or not 1 <= p <= 65535 for p in self.ports):
            raise ValueError('INVALID_BACKEND_PORTS')
        self.path = Path(bindings_file)
        self.max_sessions = max_sessions
        self.lock = threading.RLock()
        self.healthy = {p: False for p in self.ports}
        self.inflight = {p: 0 for p in self.ports}
        self.bindings = {}
        self.generations = {}
        self.cursor = 0
        if self.path.exists():
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if value.get('version') != 1 or not isinstance(value.get('bindings'), dict):
                raise ValueError('INVALID_BINDINGS_FILE')
            bindings = value['bindings']
            if len(bindings) > max_sessions or any(not re.fullmatch('[0-9a-f]{64}', k) or type(v) is not int or v not in self.ports for k, v in bindings.items()):
                raise ValueError('INVALID_BINDINGS_FILE')
            self.bindings = bindings

    def _bind(self, key, port):
        if key is None or self.bindings.get(key) == port:
            return
        if key not in self.bindings and len(self.bindings) >= self.max_sessions:
            raise Unavailable('SESSION_CAPACITY_REACHED')
        candidate = {**self.bindings, key: port}
        temp = self.path.with_name(self.path.name + '.new')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temp.open('w', encoding='utf-8') as handle:
                json.dump({'version': 1, 'bindings': candidate}, handle, separators=(',', ':'))
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(self.path)
        except OSError as error:
            raise Unavailable('BINDING_STORAGE_UNAVAILABLE') from error
        self.bindings = candidate
        self.generations[key] = self.generations.get(key, 0) + 1

    def order(self, key=None, balanced=True, eligible_ports=None):
        with self.lock:
            good = [p for p in self.ports if self.healthy[p] and (eligible_ports is None or p in eligible_ports)]
            if not good:
                raise Unavailable('NO_HEALTHY_BACKEND')
            if not balanced:
                return good, None
            port = self.bindings.get(key) if key is not None else None
            if port not in good:
                counts = {p: sum(v == p for v in self.bindings.values()) if key is not None else 0 for p in good}
                rotated = self.ports[self.cursor:] + self.ports[:self.cursor]
                port = min(good, key=lambda p: (counts[p], self.inflight[p], rotated.index(p)))
                self._bind(key, port)
                self.cursor = (self.ports.index(port) + 1) % len(self.ports)
            return [port] + [p for p in good if p != port], self.generations.get(key, 0)

    def bind(self, key, port, expected_generation):
        with self.lock:
            if key is not None and self.generations.get(key, 0) == expected_generation:
                self._bind(key, port)

    def begin(self, port):
        with self.lock:
            self.inflight[port] += 1

    def end(self, port):
        with self.lock:
            self.inflight[port] -= 1

    def snapshot(self):
        with self.lock:
            good = [p for p in self.ports if self.healthy[p]]
            return {'active_port': good[0] if good else None, 'active_ports': good,
                    'routing_mode': 'session_affinity', 'automatic_failback': False,
                    'stream_migration': False,
                    'backends': [{'port': p, 'healthy': self.healthy[p],
                        'inflight': self.inflight[p],
                        'sessions': sum(v == p for v in self.bindings.values())} for p in self.ports]}
