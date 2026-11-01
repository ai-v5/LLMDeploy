"""Asynchronous, local-only storage of request accounting metadata."""
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid


SCHEMA = 2
IDENTITY = 'llmdeploy-gateway-stats'


def _open(path):
    path = Path(path)
    if not path.parent.is_dir():
        raise ValueError('STATS_DIRECTORY_MISSING')
    db = sqlite3.connect(path, timeout=1)
    try:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        if version not in (0, 1, SCHEMA) or version == 0 and tables:
            raise ValueError('STATS_DATABASE_SCHEMA_MISMATCH')
        if version in (1, SCHEMA):
            try:
                marker = db.execute("SELECT value FROM stats_meta WHERE key='application'").fetchone()
            except sqlite3.Error as error:
                raise ValueError('STATS_DATABASE_SCHEMA_MISMATCH') from error
            if marker != (IDENTITY,):
                raise ValueError('STATS_DATABASE_SCHEMA_MISMATCH')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=NORMAL')
        if version == 0:
            db.executescript('''
                CREATE TABLE stats_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO stats_meta VALUES ('application','llmdeploy-gateway-stats');
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY, started REAL NOT NULL, last_seen REAL NOT NULL,
                    ended REAL, dropped INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE requests (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, finished REAL NOT NULL,
                    client_ip TEXT, elapsed REAL NOT NULL, http_status INTEGER,
                    state TEXT NOT NULL CHECK(state IN ('known','missing','failed','interrupted')),
                    input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                    session_id TEXT, first_visible REAL,
                    CHECK(state != 'known' OR (input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                        AND cached_tokens IS NOT NULL AND input_tokens >= 0 AND output_tokens >= 0
                        AND cached_tokens >= 0 AND cached_tokens <= input_tokens)),
                    CHECK(state = 'known' OR (input_tokens IS NULL AND output_tokens IS NULL AND cached_tokens IS NULL))
                );
                CREATE INDEX requests_finished ON requests(finished);
                CREATE TABLE active_requests (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, started REAL NOT NULL,
                    client_ip TEXT, session_id TEXT, first_visible REAL
                );
                CREATE TABLE active_samples (run_id TEXT PRIMARY KEY, seen REAL NOT NULL);
                PRAGMA user_version=2;
            ''')
            db.commit()
        elif version == 1:
            db.execute('BEGIN IMMEDIATE')
            db.execute('ALTER TABLE requests ADD COLUMN session_id TEXT')
            db.execute('ALTER TABLE requests ADD COLUMN first_visible REAL')
            db.execute('''CREATE TABLE active_requests (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, started REAL NOT NULL,
                client_ip TEXT, session_id TEXT, first_visible REAL
            )''')
            db.execute('CREATE TABLE active_samples (run_id TEXT PRIMARY KEY, seen REAL NOT NULL)')
            db.execute('PRAGMA user_version=2')
            db.commit()
        return db
    except Exception:
        db.close()
        raise


class Recorder:
    def __init__(self, path, *, queue_size=4096, heartbeat=5):
        self.path = Path(path)
        self.run = uuid.uuid4().hex
        self.started = time.time()
        self.heartbeat = heartbeat
        self.pending = queue.Queue(maxsize=queue_size)
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.live_lock = threading.Lock()
        self.live = {}
        self.dropped = 0
        self.failed = False
        self.thread = threading.Thread(target=self._write, name='stats-writer', daemon=True)
        self.thread.start()
        if not self.ready.wait(3) or self.failed:
            self.stop.set()
            raise ValueError('STATS_DATABASE_UNAVAILABLE')

    def _enqueue(self, item):
        if self.failed or self.stop.is_set():
            return False
        try:
            self.pending.put_nowait(item)
            return True
        except queue.Full:
            self.note_drop()
            return False

    def begin(self, request_id, started, client_ip, session_id):
        with self.live_lock:
            self.live[request_id] = (started, client_ip, session_id, None)

    def first_output(self, request_id, first_visible):
        with self.live_lock:
            item = self.live.get(request_id)
            if item is not None:
                self.live[request_id] = (*item[:3], first_visible)

    def end(self, request_id):
        with self.live_lock:
            self.live.pop(request_id, None)

    def submit(self, event, request_id=None):
        return self._enqueue(('event', (request_id, event)))

    def note_drop(self, count=1):
        with self.lock:
            self.dropped += count

    def flush(self, timeout=3):
        done = threading.Event()
        try:
            self.pending.put(('barrier', done), timeout=timeout)
        except queue.Full:
            return False
        return done.wait(timeout) and not self.failed

    def close(self, timeout=3):
        self.stop.set()
        self.thread.join(timeout)
        return not self.thread.is_alive() and not self.failed

    def _write(self):
        db = None
        try:
            db = _open(self.path)
            db.execute('INSERT INTO runs(id,started,last_seen) VALUES (?,?,?)',
                       (self.run, self.started, self.started))
            db.execute('INSERT INTO active_samples(run_id,seen) VALUES (?,?)', (self.run, self.started))
            db.commit()
            self.ready.set()
            last_tick = time.monotonic()
            last_snapshot = last_tick
            while not self.stop.is_set() or not self.pending.empty():
                try:
                    kind, item = self.pending.get(timeout=min(self.heartbeat, 0.2))
                except queue.Empty:
                    kind, item = None, None
                if kind == 'event':
                    request_id, event = item
                    event = tuple(event) + (None,) * (10 - len(event))
                    db.execute('''INSERT INTO requests
                        (run_id,finished,client_ip,elapsed,http_status,state,
                         input_tokens,output_tokens,cached_tokens,session_id,first_visible)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (self.run, *event))
                    db.commit()
                elif kind == 'barrier':
                    item.set()
                if time.monotonic() - last_tick >= self.heartbeat:
                    with self.lock:
                        dropped = self.dropped
                    db.execute('UPDATE runs SET last_seen=?,dropped=? WHERE id=?',
                               (time.time(), dropped, self.run))
                    db.commit()
                    last_tick = time.monotonic()
                if time.monotonic() - last_snapshot >= 1:
                    with self.live_lock:
                        snapshot = list(self.live.items())
                        sampled_at = time.time()
                    db.execute('DELETE FROM active_requests WHERE run_id=?', (self.run,))
                    db.executemany('''INSERT INTO active_requests
                        (id,run_id,started,client_ip,session_id,first_visible) VALUES (?,?,?,?,?,?)''',
                        [(request_id, self.run, *item) for request_id, item in snapshot])
                    db.execute('UPDATE active_samples SET seen=? WHERE run_id=?', (sampled_at, self.run))
                    db.commit()
                    last_snapshot = time.monotonic()
            with self.lock:
                dropped = self.dropped
            now = time.time()
            db.execute('DELETE FROM active_requests WHERE run_id=?', (self.run,))
            db.execute('UPDATE runs SET last_seen=?,ended=?,dropped=? WHERE id=?',
                       (now, now, dropped, self.run))
            db.commit()
        except Exception:
            self.failed = True
            self.ready.set()
        finally:
            if db is not None:
                db.close()
