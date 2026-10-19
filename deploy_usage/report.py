"""Read-only five-window summaries of local usage databases."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time

from .store import FIELDS

TZ = timezone(timedelta(hours=8))
WINDOWS = ('today', '24h', '3d', '7d', 'total')


def _covered(runs, start, end):
    # All API processes of a run must be covered; union across successive runs.
    complete = []
    for expected, writers in runs.values():
        if len(writers) != expected:
            continue
        points = []
        for intervals in writers:
            # Clock corrections or delayed callbacks can overlap intervals of
            # the same writer. Never count one writer as two healthy writers.
            merged = []
            for a, b in sorted(intervals):
                if merged and a <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                else:
                    merged.append((a, b))
            for a, b in merged:
                if b > a:
                    points += [(max(start, a), 1), (min(end, b), -1)] if a < end and b > start else []
        level, previous = 0, start
        for stamp, change in sorted(points):
            if level == expected and stamp > previous:
                complete.append((previous, stamp))
            level += change
            previous = stamp
    total, right = 0.0, start
    for a, b in sorted(complete):
        total += max(0, b - max(a, right))
        right = max(right, b)
    return min(end - start, total)


def report(root, now=None):
    now = time.time() if now is None else now
    midnight = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    starts = [midnight, now - 86400, now - 3 * 86400, now - 7 * 86400, now]
    totals = [[0] * len(FIELDS) for _ in WINDOWS]
    runs, first, last, errors, writers = {}, now, 0, 0, 0
    for path in sorted(Path(root).glob('*.sqlite3')):
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)) as db:
                db.execute('BEGIN')
                meta = dict(db.execute('SELECT key,value FROM meta'))
                assert meta['schema'] == '1'
                start, seen = float(meta['start']), float(meta['last_seen'])
                first, last = min(first, start), max(last, seen)
                expected = int(meta['expected_writers'])
                assert 1 <= expected <= 128
                group = runs.setdefault(meta['run'], (expected, []))
                assert group[0] == expected
                intervals = list(db.execute('SELECT start,end FROM coverage'))
                group[1].append(intervals if meta['error'] == '0' else [])
                errors += int(meta['error'] != '0')
                writers += 1
                for i, bound in enumerate(starts):
                    lower = 0 if WINDOWS[i] == 'total' else bound
                    row = db.execute('SELECT ' + ','.join('COALESCE(SUM(' + k + '),0)' for k in FIELDS)
                        + ' FROM samples WHERE second>=? AND second<?', (int(lower), int(now))).fetchone()
                    totals[i] = [a + b for a, b in zip(totals[i], row)]
        except (sqlite3.Error, KeyError, ValueError, AssertionError, OverflowError):
            errors += 1
    starts[-1] = first
    windows = {}
    for name, bound, values in zip(WINDOWS, starts, totals):
        seconds = max(0, now - bound)
        counts = dict(zip(FIELDS, values))
        covered = _covered(runs, bound, now) if writers else 0
        counts.update(seconds=seconds, covered_seconds=covered,
            output_tps=values[1] / seconds if seconds else 0,
            decode_tps=values[4] / values[5] if values[5] else None,
            hit_rate=values[2] / values[0] if values[0] else None)
        windows[name] = counts
    return {'schema': 1, 'as_of': now, 'since': first if writers else None,
            'last_seen': last, 'errors': errors, 'writers': writers,
            'resolution_seconds': 1, 'power_loss_safe': False, 'windows': windows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    print(json.dumps(report(args.root), allow_nan=False))
