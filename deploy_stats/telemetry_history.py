"""Persist bounded, device-group step-rate samples without inventing missing data."""
import hashlib
import json
import math
import os
from contextlib import closing
from pathlib import Path
import re
import sqlite3


APPLICATION_ID = 0x4C445448
SCHEMA_VERSION = 1
BUCKET_SECONDS = 600
BUCKET_COUNT = 144
SOURCE = 'deepinfer-log-step-approx-v1'
HASH = re.compile(r'[0-9a-f]{64}\Z')


def _finite(value, minimum=None, maximum=None):
    try:
        return (type(value) in (int, float) and math.isfinite(value) and
                (minimum is None or value >= minimum) and (maximum is None or value <= maximum))
    except OverflowError:
        return False


def _config_key(config):
    keys = ('backend_port', 'ssh_target', 'container', 'rank_devices', 'source_sha256',
            'image_fingerprint', 'driver_sha256')
    value = {key: config[key] for key in keys}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class History:
    """One file, one writer; readers open their own SQLite connections."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            created = False
        else:
            os.close(fd)
            created = True
        with closing(self._connect()) as connection, connection:
            if created:
                connection.execute(f'PRAGMA application_id = {APPLICATION_ID}')
                connection.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                connection.execute('''CREATE TABLE step_samples (
                    config_key TEXT NOT NULL,
                    host_key TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (phase IN ('prefill', 'decode')),
                    observation_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    bucket INTEGER NOT NULL,
                    rate REAL NOT NULL,
                    PRIMARY KEY (config_key, host_key, source_id, phase, observation_id)
                ) WITHOUT ROWID''')
                connection.execute('CREATE INDEX step_samples_by_bucket ON step_samples (config_key, host_key, bucket)')
            elif (connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or
                  connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
                raise ValueError('INVALID_TELEMETRY_HISTORY')
            # Only an identified history file may be modified.
            connection.execute('PRAGMA journal_mode = WAL')

    def _connect(self):
        # mode=rw does not silently create a replacement if an existing database disappears.
        return sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=1)

    def record(self, node, config, now):
        if (not isinstance(node, dict) or node.get('source_ok') is not True or
                node.get('backend_ready') is not True or node.get('clock_ok') is not True or
                node.get('backend_port') != config.get('backend_port') or
                not isinstance(node.get('host_fingerprint'), str) or not HASH.fullmatch(node['host_fingerprint']) or
                not isinstance(node.get('source_id'), str) or not HASH.fullmatch(node['source_id']) or
                not _finite(now, minimum=0) or
                not _finite(node.get('clock_uncertainty_seconds'), minimum=0, maximum=20)):
            return 0
        uncertainty = node['clock_uncertainty_seconds']
        rows = []
        for phase in ('prefill', 'decode'):
            part = node.get(phase)
            if not isinstance(part, dict):
                continue
            at, skew, step, rate, until, identity = (part.get(k) for k in
                ('log_at', 'log_skew_seconds', 'step', 'tokens_per_second', 'valid_until', '_observation_id'))
            if (not _finite(at, minimum=0, maximum=now + uncertainty) or
                    not _finite(skew, minimum=0, maximum=5) or type(step) is not int or not 0 <= step < 2**63 or
                    not isinstance(identity, str) or not HASH.fullmatch(identity) or
                    not _finite(rate, minimum=0, maximum=1e15) or not _finite(until) or until <= now):
                continue
            first, last = at - skew - uncertainty, at + uncertainty
            if first < 0 or int(first // BUCKET_SECONDS) != int(last // BUCKET_SECONDS):
                continue
            rows.append((_config_key(config), node['host_fingerprint'], node['source_id'], phase,
                         identity, step, int(first // BUCKET_SECONDS) * BUCKET_SECONDS, rate))
        if not rows:
            return 0
        with closing(self._connect()) as connection, connection:
            before = connection.total_changes
            connection.executemany('INSERT OR IGNORE INTO step_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)', rows)
            return connection.total_changes - before

    def series(self, config, host_key, now):
        if not isinstance(host_key, str) or not HASH.fullmatch(host_key) or not _finite(now, minimum=0):
            return None
        end = int(now // BUCKET_SECONDS) * BUCKET_SECONDS
        start = end - BUCKET_COUNT * BUCKET_SECONDS
        if start < 0:
            return None
        with closing(self._connect()) as connection, connection:
            rows = connection.execute('''SELECT bucket, phase, SUM(rate), COUNT(*) FROM step_samples
                WHERE config_key = ? AND host_key = ? AND bucket >= ? AND bucket < ?
                GROUP BY bucket, phase''', (_config_key(config), host_key, start, end)).fetchall()
        grouped = {(bucket, phase): (total, count) for bucket, phase, total, count in rows}
        values = []
        for bucket in range(start, end, BUCKET_SECONDS):
            point = {'start': bucket, 'end': bucket + BUCKET_SECONDS}
            for phase in ('prefill', 'decode'):
                total, count = grouped.get((bucket, phase), (0, 0))
                point[phase + '_tps'] = total / count if count and _finite(total, minimum=0) else None
                point[phase + '_samples'] = count
            values.append(point)
        return {'method': SOURCE, 'bucket_seconds': BUCKET_SECONDS, 'points': values}
