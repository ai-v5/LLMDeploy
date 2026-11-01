"""Read-only windows, per-IP aggregates, and completion-time buckets."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import time

from .store import IDENTITY, SCHEMA

TZ = timezone(timedelta(hours=8))
BUCKET_SECONDS = 600


def _coverage(runs, start, end, now):
    intervals = []
    dropped = 0
    unclean = 0
    for run in runs:
        left, seen, ended, lost = run
        live = ended is None and 0 <= now - seen < 15
        right = ended if ended is not None else (now if live else seen)
        if left < end and right > start:
            intervals.append((max(start, left), min(end, right)))
            dropped += lost
            if ended is None and not live:
                # Events queued before the last heartbeat may never have been
                # committed when a process exited without a clean drain.
                unclean += 1
    intervals.sort()
    covered, edge = 0.0, start
    for left, right in intervals:
        covered += max(0, right - max(left, edge))
        edge = max(edge, right)
    return min(max(0.0, end - start), covered), dropped, unclean


def _counts(rows):
    out = {'requests': 0, 'known': 0, 'missing': 0, 'failed': 0, 'interrupted': 0,
           'input': 0, 'output': 0, 'cached': 0, 'elapsed': 0.0}
    for row in rows:
        state, prompt, output, cached, elapsed = row
        out['requests'] += 1
        if state == 'known':
            out['known'] += 1
            out['input'] += prompt
            out['output'] += output
            out['cached'] += cached
            out['elapsed'] += elapsed
        else:
            out[state if state in ('missing', 'failed', 'interrupted') else 'missing'] += 1
    out['hit_rate'] = out['cached'] / out['input'] if out['input'] else None
    out['request_tps'] = out['output'] / out['elapsed'] if out['elapsed'] else None
    return out


def report(path, window='24h', *, now=None):
    if window not in ('today', '24h', '7d', 'total'):
        raise ValueError('INVALID_STATS_WINDOW')
    now = time.time() if now is None else float(now)
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA:
            raise ValueError('STATS_DATABASE_SCHEMA_MISMATCH')
        try:
            marker = db.execute("SELECT value FROM stats_meta WHERE key='application'").fetchone()
        except sqlite3.Error as error:
            raise ValueError('STATS_DATABASE_SCHEMA_MISMATCH') from error
        if marker != (IDENTITY,):
            raise ValueError('STATS_DATABASE_SCHEMA_MISMATCH')
        runs = list(db.execute('SELECT started,last_seen,ended,dropped FROM runs'))
        since = min((r[0] for r in runs), default=now)
        midnight = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        start = {'today': midnight, '24h': now - 86400, '7d': now - 7 * 86400,
                 'total': since}[window]
        start = min(start, now)
        rows = list(db.execute('''SELECT finished,client_ip,state,input_tokens,output_tokens,cached_tokens,elapsed
            FROM requests WHERE finished>=? AND finished<? ORDER BY finished''',
            (min(start, now - 14 * 86400), now)))
    selected, grouped, series_rows, day_rows = [], {}, {}, {}
    for row in rows:
        stamp = row[0]
        if stamp >= start:
            selected.append(row[2:])
            grouped.setdefault(row[1], []).append(row[2:])
        if stamp >= now - 86400:
            bucket = int(stamp // BUCKET_SECONDS) * BUCKET_SECONDS
            series_rows.setdefault(bucket, []).append(row[2:])
        if stamp >= midnight - 13 * 86400:
            day = int((stamp + 8 * 3600) // 86400) * 86400 - 8 * 3600
            day_rows.setdefault(day, []).append(row[2:])
    totals = _counts(selected)
    seconds = max(0.0, now - start)
    covered, dropped, unclean = _coverage(runs, start, now, now)
    totals['window_tps'] = totals['output'] / seconds if seconds else None
    totals['unknown_source'] = len(grouped.get(None, []))
    totals['covered_seconds'] = covered
    totals['seconds'] = seconds
    totals['dropped'] = dropped
    totals['unclean_runs'] = unclean
    totals['complete'] = (bool(runs) and seconds > 0 and covered >= seconds - 1 and dropped == 0 and unclean == 0 and
                          totals['missing'] == 0 and totals['interrupted'] == 0 and totals['failed'] == 0)
    clients = []
    for ip, items in grouped.items():
        item = _counts(items)
        item['ip'] = ip
        item['window_tps'] = item['output'] / seconds if seconds else None
        clients.append(item)
    clients.sort(key=lambda x: (-x['output'], x['ip'] or ''))
    first_bucket = int((now - 86400) // BUCKET_SECONDS) * BUCKET_SECONDS
    last_bucket = int(now // BUCKET_SECONDS) * BUCKET_SECONDS
    series = []
    for bucket in range(first_bucket, last_bucket + 1, BUCKET_SECONDS):
        a, b = max(bucket, now - 86400), min(bucket + BUCKET_SECONDS, now)
        counts = _counts(series_rows.get(bucket, []))
        coverage, lost, unclean = _coverage(runs, a, b, now)
        counts.update(start=a, end=b, covered_seconds=coverage, dropped=lost, unclean_runs=unclean,
                      complete=bool(runs) and b > a and coverage >= b - a - 1 and lost == 0 and unclean == 0 and
                      counts['missing'] == 0 and counts['interrupted'] == 0 and counts['failed'] == 0)
        series.append(counts)
    days = []
    for day in range(13, -1, -1):
        a, b = midnight - day * 86400, min(midnight - (day - 1) * 86400, now)
        counts = _counts(day_rows.get(a, []))
        coverage, lost, unclean = _coverage(runs, a, b, now)
        counts.update(date=datetime.fromtimestamp(a, TZ).strftime('%Y-%m-%d'), start=a, end=b,
                      covered_seconds=coverage, dropped=lost, unclean_runs=unclean,
                      complete=bool(runs) and b > a and coverage >= b - a - 1 and lost == 0 and unclean == 0 and
                      counts['missing'] == 0 and counts['interrupted'] == 0 and counts['failed'] == 0)
        days.append(counts)
    live = any(ended is None and 0 <= now - seen < 15 for _, seen, ended, _ in runs)
    return {'schema': SCHEMA, 'as_of': now, 'since': since if runs else None,
            'window': window, 'collector_live': live, 'totals': totals,
            'clients': clients, 'series_24h': series, 'days': days}
