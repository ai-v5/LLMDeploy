"""Read-only windows, per-IP aggregates, and completion-time buckets."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import ipaddress
import math
from pathlib import Path
import sqlite3
import time

from .store import IDENTITY, SCHEMA

TZ = timezone(timedelta(hours=8))
BUCKET_SECONDS = 600
COVERAGE_EPSILON = 1e-6
REQUEST_SORTS = frozenset(('finished', 'ip', 'session', 'state', 'input', 'output', 'hit_rate',
                           'first_visible', 'after_first', 'input_per_wait', 'output_per_after_first', 'first_share'))
SESSION_SORTS = frozenset(('last_finished', 'session', 'ips', 'requests', 'input', 'output', 'hit_rate',
                           'first_visible', 'after_first', 'input_per_wait', 'output_per_after_first', 'first_share'))


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


def _percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    left = int(position)
    right = min(left + 1, len(values) - 1)
    return values[left] + (values[right] - values[left]) * (position - left)


def _request_view(row):
    finished, ip, state, prompt, output, cached, elapsed, session, first, status, record_id = row
    observed = (state == 'known' and first is not None and math.isfinite(first) and
                0 <= first <= elapsed and elapsed > 0)
    tail = elapsed - first if observed else None
    known = state == 'known'
    return {'id': record_id, 'finished': finished, 'ip': ip, 'session': session, 'state': state,
            'status': status, 'input': prompt, 'output': output, 'cached': cached,
            'hit_rate': cached / prompt if known and prompt else None, 'elapsed': elapsed,
            'first_visible': first if observed else None, 'after_first': tail,
            'first_share': first / elapsed if observed else None,
            'after_first_share': tail / elapsed if observed else None,
            # Estimates use gateway-observed intervals, not engine phase times.
            'input_per_wait': (prompt - cached) / first if known and observed and first > 0 else None,
            'output_per_after_first': (output - 1) / tail if known and observed and output > 1 and tail > 0 else None}


def _session_views(details):
    groups = {}
    for item in details:
        if item['session']:
            groups.setdefault(item['session'], []).append(item)
    result = []
    for session, items in groups.items():
        known = [item for item in items if item['state'] == 'known']
        timed = [item for item in known if item['first_visible'] is not None]
        rate_items = [item for item in known if item['first_visible'] is not None]
        wait = sum(item['first_visible'] for item in timed)
        after = sum(item['after_first'] for item in timed)
        total_input = sum(item['input'] for item in known)
        total_output = sum(item['output'] for item in known)
        total_cached = sum(item['cached'] for item in known)
        in_wait = sum(item['first_visible'] for item in rate_items)
        out_items = [item for item in rate_items if item['output'] > 1 and item['after_first'] > 0]
        out_time = sum(item['after_first'] for item in out_items)
        result.append({'session': session, 'ips': sorted({item['ip'] or '未知来源' for item in items}),
            'requests': len(items), 'known': len(known), 'timed': len(timed),
            'input': total_input if known else None, 'output': total_output if known else None,
            'cached': total_cached if known else None,
            'hit_rate': total_cached / total_input if total_input else None,
            'first_visible': wait if timed else None, 'after_first': after if timed else None,
            'first_share': wait / (wait + after) if timed and wait + after > 0 else None,
            'input_per_wait': sum(item['input'] - item['cached'] for item in rate_items) / in_wait if in_wait else None,
            'output_per_after_first': sum(item['output'] - 1 for item in out_items) / out_time if out_time else None,
            'last_finished': max(item['finished'] for item in items)})
    return sorted(result, key=lambda item: item['last_finished'], reverse=True)


def _ordered_page(items, field, direction, offset, limit, identity):
    def value(item):
        result = item.get(field)
        if field == 'ips':
            known = [address for address in result if address != '未知来源'] if result else []
            result = ', '.join(known) if known else None
        elif field == 'ip' and result is not None:
            try:
                address = ipaddress.ip_address(result)
                return address.version, int(address)
            except ValueError:
                return None
        elif field == 'state':
            labels = {'known': '已计量', 'missing': '未计量', 'failed': '失败', 'interrupted': '中断'}
            return labels.get(result, '未知'), item.get('status') or 0
        if isinstance(result, str):
            result = result.casefold()
        return result

    present = [item for item in items if value(item) is not None]
    missing = [item for item in items if value(item) is None]
    present.sort(key=lambda item: (value(item), item[identity]), reverse=direction == 'desc')
    missing.sort(key=lambda item: item[identity])
    ordered = present + missing
    offset = min(offset, ((len(ordered) - 1) // limit) * limit) if ordered else 0
    return ordered[offset:offset + limit], offset


def _check_database(db):
    if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA:
        raise ValueError('STATS_DATABASE_SCHEMA_MISMATCH')
    try:
        marker = db.execute("SELECT value FROM stats_meta WHERE key='application'").fetchone()
    except sqlite3.Error as error:
        raise ValueError('STATS_DATABASE_SCHEMA_MISMATCH') from error
    if marker != (IDENTITY,):
        raise ValueError('STATS_DATABASE_SCHEMA_MISMATCH')


def _active_snapshot(db, now):
    samples = list(db.execute('''SELECT r.last_seen,s.seen FROM runs r
        LEFT JOIN active_samples s ON s.run_id=r.id WHERE r.ended IS NULL AND r.last_seen>? AND r.last_seen<=?''',
        (now - 15, now)))
    ready = bool(samples) and all(seen is not None and 0 <= now - seen < 3 for _, seen in samples)
    rows = list(db.execute('''SELECT a.client_ip,a.session_id,a.started,a.first_visible
        FROM active_requests a JOIN runs r ON a.run_id=r.id
        WHERE r.ended IS NULL AND r.last_seen>? AND r.last_seen<=? ORDER BY a.started''', (now - 15, now))) if ready else []
    current = [{'ip': ip, 'session': session, 'started': started, 'elapsed': max(0, now - started),
                'first_visible': first, 'input': None, 'output': None, 'cached': None}
               for ip, session, started, first in rows]
    return {'as_of': now, 'collector_live': bool(samples), 'active': current, 'active_ready': ready,
            'active_sampled_at': min((seen for _, seen in samples), default=None) if ready else None}


def active_report(path, *, now=None):
    now = time.time() if now is None else float(now)
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        _check_database(db)
        return _active_snapshot(db, now)


def report(path, window='24h', *, now=None, request_offset=0, request_limit=100, session_offset=0,
           session_limit=None, request_sort='finished', request_dir='desc',
           session_sort='last_finished', session_dir='desc', snapshot_at=None, snapshot_id=None):
    if window not in ('today', '24h', '7d', 'total'):
        raise ValueError('INVALID_STATS_WINDOW')
    if session_limit is None:
        session_limit = request_limit
    if (type(request_offset) is not int or request_offset < 0 or type(session_offset) is not int or session_offset < 0 or
            type(request_limit) is not int or not 1 <= request_limit <= 200 or
            type(session_limit) is not int or not 1 <= session_limit <= 200 or
            not isinstance(request_sort, str) or request_sort not in REQUEST_SORTS or
            not isinstance(session_sort, str) or session_sort not in SESSION_SORTS or
            request_dir not in ('asc', 'desc') or session_dir not in ('asc', 'desc')):
        raise ValueError('INVALID_STATS_PAGE')
    now = time.time() if now is None else float(now)
    if ((snapshot_at is None) != (snapshot_id is None) or
            snapshot_id is not None and (type(snapshot_id) is not int or snapshot_id < 0) or
            snapshot_at is not None and (type(snapshot_at) not in (int, float) or
                not math.isfinite(snapshot_at) or not 0 < snapshot_at <= now)):
        raise ValueError('INVALID_STATS_SNAPSHOT')
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        _check_database(db)
        runs = list(db.execute('SELECT started,last_seen,ended,dropped FROM runs'))
        overload_runs = list(db.execute('''SELECT runs.started,runs.last_seen,runs.ended,stats_meta.value
            FROM runs LEFT JOIN stats_meta ON stats_meta.key='overload:' || runs.id'''))
        since = min((r[0] for r in runs), default=now)
        if snapshot_at is None:
            snapshot_at = now
            snapshot_id = db.execute('SELECT COALESCE(MAX(id),0) FROM requests').fetchone()[0]
        midnight = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        start = {'today': midnight, '24h': now - 86400, '7d': now - 7 * 86400,
                 'total': since}[window]
        start = min(start, now)
        snapshot_midnight = datetime.fromtimestamp(snapshot_at, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        snapshot_start = min(snapshot_at, {'today': snapshot_midnight, '24h': snapshot_at - 86400,
                             '7d': snapshot_at - 7 * 86400, 'total': since}[window])
        rows = list(db.execute('''SELECT finished,client_ip,state,input_tokens,output_tokens,cached_tokens,elapsed,
                session_id,first_visible,http_status,id
            FROM requests WHERE finished>=? AND finished<? ORDER BY finished''',
            (min(start, snapshot_start, now - 14 * 86400), now)))
        live_active = _active_snapshot(db, now)
    selected, grouped, series_rows, day_rows = [], {}, {}, {}
    details, snapshot_details = [], []
    for row in rows:
        stamp = row[0]
        view = None
        if stamp >= start:
            selected.append(row[2:7])
            grouped.setdefault(row[1], []).append(row[2:7])
            view = _request_view(row)
            details.append(view)
        if snapshot_start <= stamp < snapshot_at and row[-1] <= snapshot_id:
            snapshot_details.append(view if view is not None else _request_view(row))
        if stamp >= now - 86400:
            bucket = int(stamp // BUCKET_SECONDS) * BUCKET_SECONDS
            series_rows.setdefault(bucket, []).append(row)
        if stamp >= midnight - 13 * 86400:
            day = int((stamp + 8 * 3600) // 86400) * 86400 - 8 * 3600
            day_rows.setdefault(day, []).append(row[2:7])
    totals = _counts(selected)
    seconds = max(0.0, now - start)
    covered, dropped, unclean = _coverage(runs, start, now, now)
    totals['window_tps'] = totals['output'] / seconds if seconds else None
    totals['unknown_source'] = len(grouped.get(None, []))
    totals['covered_seconds'] = covered
    totals['seconds'] = seconds
    totals['dropped'] = dropped
    totals['unclean_runs'] = unclean
    overload_count, overload_tracked, overload_untracked = 0, 0, 0
    for left, seen, ended, value in overload_runs:
        live = ended is None and 0 <= now - seen < 15
        right = ended if ended is not None else (now if live else seen)
        if left < now and right > start:
            try:
                if value is None or not value.isascii() or not value.isdigit():
                    raise ValueError()
                overload_count += int(value)
                overload_tracked += 1
            except (ValueError, TypeError, OverflowError):
                overload_untracked += 1
    totals['overload_rejected'] = overload_count
    totals['overload_tracked_runs'] = overload_tracked
    totals['overload_untracked_runs'] = overload_untracked
    classified = [item for item in details if item['state'] in ('known', 'failed', 'interrupted')]
    successes = [item for item in classified if item['state'] == 'known' and item['status'] is not None
                 and 200 <= item['status'] < 300]
    timed = [item for item in successes if item['first_visible'] is not None]
    first_values = [item['first_visible'] for item in timed]
    prefill_items = [item for item in timed if item['first_visible'] > 0]
    decode_items = [item for item in timed if item['output'] > 1 and item['after_first'] > 0]
    prefill_time = sum(item['first_visible'] for item in prefill_items)
    decode_time = sum(item['after_first'] for item in decode_items)
    duration_values = [item['elapsed'] for item in successes]
    totals.update(successes=len(successes), classified=len(classified),
                  success_rate=len(successes) / len(classified) if classified else None,
                  requests_per_second=len(details) / seconds if seconds else None,
                  first_p50=_percentile(first_values, .50), first_p95=_percentile(first_values, .95),
                  first_mean=sum(first_values) / len(first_values) if first_values else None,
                  first_samples=len(first_values),
                  prefill_estimate_tps=sum(item['input'] - item['cached'] for item in prefill_items) / prefill_time if prefill_time else None,
                  prefill_samples=len(prefill_items),
                  decode_estimate_tps=sum(item['output'] - 1 for item in decode_items) / decode_time if decode_time else None,
                  decode_samples=len(decode_items), elapsed_p50=_percentile(duration_values, .50),
                  elapsed_p95=_percentile(duration_values, .95), elapsed_samples=len(duration_values),
                  identified_sessions=len({item['session'] for item in details if item['session']}),
                  unknown_session=sum(1 for item in details if not item['session']))
    totals['complete'] = (bool(runs) and seconds > 0 and covered >= seconds - COVERAGE_EPSILON and dropped == 0 and unclean == 0 and
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
        bucket_rows = series_rows.get(bucket, [])
        counts = _counts([row[2:7] for row in bucket_rows])
        bucket_requests = [_request_view(row) for row in bucket_rows]
        hits = [item['hit_rate'] for item in bucket_requests if item['hit_rate'] is not None]
        timed = [item for item in bucket_requests if item['state'] == 'known' and
                 item['status'] is not None and 200 <= item['status'] < 300 and
                 item['first_visible'] is not None]
        prefill = [item for item in timed if item['first_visible'] > 0]
        decode = [item for item in timed if item['output'] > 1 and item['after_first'] > 0]
        prefill_tokens = sum(item['input'] - item['cached'] for item in prefill)
        prefill_seconds = sum(item['first_visible'] for item in prefill)
        decode_tokens = sum(item['output'] - 1 for item in decode)
        decode_seconds = sum(item['after_first'] for item in decode)
        coverage, lost, unclean = _coverage(runs, a, b, now)
        counts.update(start=a, end=b, covered_seconds=coverage, dropped=lost, unclean_runs=unclean,
                      mean_request_hit_rate=sum(hits) / len(hits) if hits else None,
                      request_hit_sum=sum(hits), request_hit_samples=len(hits),
                      prefill_estimate_tps=prefill_tokens / prefill_seconds if prefill_seconds else None,
                      prefill_tokens=prefill_tokens, prefill_seconds=prefill_seconds, prefill_samples=len(prefill),
                      decode_estimate_tps=decode_tokens / decode_seconds if decode_seconds else None,
                      decode_tokens=decode_tokens, decode_seconds=decode_seconds, decode_samples=len(decode),
                      complete=bool(runs) and b > a and coverage >= b - a - COVERAGE_EPSILON and lost == 0 and unclean == 0 and
                      counts['missing'] == 0 and counts['interrupted'] == 0 and counts['failed'] == 0)
        series.append(counts)
    days = []
    for day in range(13, -1, -1):
        a, b = midnight - day * 86400, min(midnight - (day - 1) * 86400, now)
        counts = _counts(day_rows.get(a, []))
        coverage, lost, unclean = _coverage(runs, a, b, now)
        counts.update(date=datetime.fromtimestamp(a, TZ).strftime('%Y-%m-%d'), start=a, end=b,
                      covered_seconds=coverage, dropped=lost, unclean_runs=unclean,
                      complete=bool(runs) and b > a and coverage >= b - a - COVERAGE_EPSILON and lost == 0 and unclean == 0 and
                      counts['missing'] == 0 and counts['interrupted'] == 0 and counts['failed'] == 0)
        days.append(counts)
    snapshot_covered, snapshot_dropped, snapshot_unclean = _coverage(runs, snapshot_start, snapshot_at, now)
    snapshot_complete = (bool(runs) and snapshot_at > snapshot_start and
                         snapshot_covered >= snapshot_at - snapshot_start - COVERAGE_EPSILON and
                         snapshot_dropped == 0 and snapshot_unclean == 0 and
                         all(item['state'] == 'known' for item in snapshot_details))
    sessions = _session_views(snapshot_details)
    recent, request_offset = _ordered_page(snapshot_details, request_sort, request_dir, request_offset, request_limit, 'id')
    session_page, session_offset = _ordered_page(sessions, session_sort, session_dir, session_offset, session_limit, 'session')
    return {'schema': SCHEMA, 'as_of': now, 'since': since if runs else None,
            'window': window, 'collector_live': live_active['collector_live'], 'totals': totals,
            'clients': clients, 'series_24h': series, 'days': days, 'active': live_active['active'],
            'active_ready': live_active['active_ready'], 'active_sampled_at': live_active['active_sampled_at'],
            'snapshot_at': snapshot_at, 'snapshot_id': snapshot_id, 'snapshot_complete': snapshot_complete,
            'requests': recent, 'request_count': len(snapshot_details), 'request_offset': request_offset, 'request_limit': request_limit,
            'sessions': session_page, 'session_count': len(sessions), 'session_offset': session_offset, 'session_limit': session_limit,
            'session_unknown_count': sum(1 for item in snapshot_details if not item['session'])}
