"""Derive the gateway's session fingerprint without persisting request bodies."""
import hashlib
import json
import threading

MAX_BODY = 1024 * 1024
MAX_PENDING = 32 * 1024 * 1024


class BodyBudget:
    def __init__(self, limit=MAX_PENDING):
        self.limit = limit
        self.used = 0
        self.lock = threading.Lock()

    def reserve(self, size):
        with self.lock:
            if size < 0 or self.used + size > self.limit:
                return False
            self.used += size
            return True

    def release(self, size):
        with self.lock:
            self.used -= size


BUDGET = BodyBudget()


def key_digest(headers):
    auth = headers.get_all('Authorization', [])
    if len(auth) != 1 or not isinstance(auth[0], str):
        return None
    fields = auth[0].split()
    if len(fields) != 2 or fields[0].lower() != 'bearer' or len(fields[1]) > 256:
        return None
    try:
        return hashlib.sha256(fields[1].encode()).hexdigest()
    except UnicodeError:
        return None


def fingerprint(digest, seed):
    try:
        return hashlib.sha256((digest + '\0' + seed).encode()).hexdigest()
    except UnicodeError:
        return None


def explicit_session(headers):
    """Return whether an explicit header exists and its fingerprint if valid."""
    digest = key_digest(headers)
    for name in ('x-session-affinity', 'x-deepseek-harness-session-id', 'x-session-id'):
        values = headers.get_all(name, [])
        if values:
            if not digest or len(values) != 1 or not isinstance(values[0], str) or not 0 < len(values[0]) <= 256:
                return True, None
            return True, fingerprint(digest, 'session:' + values[0])
    return False, None


class SessionBodyCapture:
    """Bounded temporary buffering for the gateway's JSON fallback identity."""

    def __init__(self, digest, *, budget=BUDGET, max_body=MAX_BODY):
        self.digest = digest
        self.budget = budget
        self.max_body = max_body
        self.buffer = bytearray()
        self.reserved = 0
        self.disabled = not bool(digest)

    def feed(self, data):
        if self.disabled:
            return
        if len(self.buffer) + len(data) > self.max_body or not self.budget.reserve(len(data)):
            self.close()
            return
        self.reserved += len(data)
        try:
            self.buffer.extend(data)
        except Exception:
            self.close()

    def finish(self):
        if self.disabled:
            return None
        try:
            value = json.loads(self.buffer)
            if not isinstance(value, dict):
                return None
            cache_key = value.get('prompt_cache_key')
            if isinstance(cache_key, str) and cache_key:
                seed = 'session:' + cache_key
            elif isinstance(value.get('messages'), list):
                first = next((item for item in value['messages']
                              if isinstance(item, dict) and item.get('role') == 'user'), None)
                if first is None or not isinstance(first.get('content'), (str, list)):
                    return None
                seed = 'first-user:' + json.dumps(first['content'], sort_keys=True, ensure_ascii=False,
                                                  separators=(',', ':'))
            else:
                return None
            return fingerprint(self.digest, seed)
        except (ValueError, TypeError, UnicodeError, RecursionError, MemoryError):
            return None
        finally:
            self.close()

    def close(self):
        if self.reserved:
            self.budget.release(self.reserved)
            self.reserved = 0
        self.buffer.clear()
        self.disabled = True
