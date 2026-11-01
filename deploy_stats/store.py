"""Asynchronous, local-only storage of request accounting metadata."""
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid


SCHEMA = 1
IDENTITY = 'llmdeploy-gateway-stats'


def _open(path):
    path = Path(path)
    if not path.parent.is_dir():
        raise ValueError('STATS_DIRECTORY_MISSING')
    db = sqlite3.connect(path, timeout=1)
    try:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        if version not in (0, SCHEMA) or version == 0 and tables:
            raise ValueError('STATS_DATABASE_SCHEMA_MISMATCH')
        if version == SCHEMA:
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
                    CHECK(state != 'known' OR (input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                        AND cached_tokens IS NOT NULL AND input_tokens >= 0 AND output_tokens >= 0
                        AND cached_tokens >= 0 AND cached_tokens <= input_tokens)),
                    CHECK(state = 'known' OR (input_tokens IS NULL AND output_tokens IS NULL AND cached_tokens IS NULL))
                );
                CREATE INDEX requests_finished ON requests(finished);
                PRAGMA user_version=1;
            ''')
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
        self.dropped = 0
        self.failed = False
        self.thread = threading.Thread(target=self._write, name='stats-writer', daemon=True)
        self.thread.start()
        if not self.ready.wait(3) or self.failed:
            self.stop.set()
            raise ValueError('STATS_DATABASE_UNAVAILABLE')

    def submit(self, event):
        if self.failed or self.stop.is_set():
            return False
        try:
            self.pending.put_nowait(('event', event))
            return True
        except queue.Full:
            self.note_drop()
            return False

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
            db.commit()
            self.ready.set()
            last_tick = time.monotonic()
            while not self.stop.is_set() or not self.pending.empty():
                try:
                    kind, item = self.pending.get(timeout=min(self.heartbeat, 0.2))
                except queue.Empty:
                    kind, item = None, None
                if kind == 'event':
                    db.execute('''INSERT INTO requests
                        (run_id,finished,client_ip,elapsed,http_status,state,
                         input_tokens,output_tokens,cached_tokens)
                        VALUES (?,?,?,?,?,?,?,?,?)''', (self.run, *item))
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
            with self.lock:
                dropped = self.dropped
            now = time.time()
            db.execute('UPDATE runs SET last_seen=?,ended=?,dropped=? WHERE id=?',
                       (now, now, dropped, self.run))
            db.commit()
        except Exception:
            self.failed = True
            self.ready.set()
        finally:
            if db is not None:
                db.close()
