"""One SQLite WAL per API process; never stores requests or credentials."""
import math
from pathlib import Path
import sqlite3
import time
import uuid

FIELDS = ('input', 'output', 'cached', 'requests', 'decode_tokens', 'decode_seconds')


class Store:
    def __init__(self, root, run, expected_writers, now=None):
        now = time.time() if now is None else now
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / (uuid.uuid4().hex + '.sqlite3')
        self.db = sqlite3.connect(self.path, timeout=0.05)
        self.db.execute('PRAGMA journal_mode=WAL')
        # Process exits preserve committed WAL. Power loss may lose recent commits.
        # FULL on every decode iteration would put fsync on the inference path.
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.executescript('''
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE samples (second INTEGER PRIMARY KEY,
                input INTEGER, output INTEGER, cached INTEGER, requests INTEGER,
                decode_tokens INTEGER, decode_seconds REAL);
            CREATE TABLE coverage (id INTEGER PRIMARY KEY, start REAL, end REAL);
        ''')
        self.db.executemany('INSERT INTO meta VALUES (?,?)', [
            ('schema', '1'), ('run', run), ('expected_writers', str(expected_writers)),
            ('start', str(now)), ('last_seen', str(now)), ('closed', '0'), ('error', '0')])
        self.interval = 1
        self.last_seen = now
        self.db.execute('INSERT INTO coverage VALUES (1,?,?)', (now, now))
        self.db.commit()

    def record(self, values, now=None):
        now = time.time() if now is None else now
        if len(values) != len(FIELDS) or any(not math.isfinite(x) or x < 0 for x in values):
            raise ValueError('INVALID_USAGE_COUNTS')
        if values[2] > values[0]:
            raise ValueError('INVALID_CACHED_COUNTS')
        try:
            self.db.execute('''INSERT INTO samples VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(second) DO UPDATE SET
                input=input+excluded.input, output=output+excluded.output,
                cached=cached+excluded.cached, requests=requests+excluded.requests,
                decode_tokens=decode_tokens+excluded.decode_tokens,
                decode_seconds=decode_seconds+excluded.decode_seconds''',
                (int(now), *values))
            self._touch(now)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _touch(self, now):
        if not self.last_seen <= now <= self.last_seen + 30:
            self.interval += 1
            self.db.execute('INSERT INTO coverage VALUES (?,?,?)', (self.interval, now, now))
        else:
            self.db.execute('UPDATE coverage SET end=? WHERE id=?', (now, self.interval))
        self.last_seen = now
        self.db.execute("UPDATE meta SET value=? WHERE key='last_seen'", (str(now),))

    def heartbeat(self, now=None):
        self._touch(time.time() if now is None else now)
        self.db.commit()

    def fail(self):
        self.db.rollback()
        self.db.execute("UPDATE meta SET value='1' WHERE key='error'")
        self.db.commit()

    def close(self, now=None):
        self.heartbeat(now)
        self.db.execute("UPDATE meta SET value='1' WHERE key='closed'")
        self.db.commit()
        self.db.close()
