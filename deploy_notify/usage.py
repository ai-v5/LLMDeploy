"""Fetch bounded numeric summaries over existing SSH trust; no inference calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import PurePosixPath
import re
import shlex
import subprocess

NAMES = (('today', '今天'), ('24h', '近24小时'), ('3d', '近3天'), ('7d', '近7天'), ('total', '累计'))
TZ = timezone(timedelta(hours=8))


def validate(settings, nodes):
    if not isinstance(settings, dict) or set(settings) != set(nodes):
        raise ValueError('USAGE_NODE_MISMATCH')
    for value in settings.values():
        if not isinstance(value, dict) or set(value) != {'target', 'code_dir', 'data_dir'}:
            raise ValueError('INVALID_USAGE_SETTINGS')
        if not isinstance(value['target'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,250}', value['target']):
            raise ValueError('INVALID_USAGE_TARGET')
        for key in ('code_dir', 'data_dir'):
            path = value[key]
            if (not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_/.-]+', path)
                    or '..' in PurePosixPath(path).parts or path == '/'):
                raise ValueError('INVALID_USAGE_PATH')


def _fetch(settings):
    command = shlex.join(['env', 'PYTHONPATH=' + settings['code_dir'], 'python3',
        '-m', 'deploy_usage.report', '--root', settings['data_dir']])
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ConnectTimeout=5', settings['target'], command],
        capture_output=True, stdin=subprocess.DEVNULL, timeout=15,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError('USAGE_UNAVAILABLE')
    value = json.loads(result.stdout)
    # Never carry remote text into the group. Reconstruct from bounded numbers only.
    assert value['schema'] == 1
    numeric = lambda n: type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 10**18
    safe = {}
    for key in ('as_of', 'since', 'last_seen', 'errors', 'writers'):
        n = value[key]
        assert numeric(n) or (key == 'since' and n is None)
        if key in ('as_of', 'since', 'last_seen') and n is not None:
            assert n <= 4102444800
        safe[key] = n
    safe['windows'] = {}
    for name, _ in NAMES:
        row = value['windows'][name]
        safe['windows'][name] = {}
        for key in ('input', 'output', 'cached', 'requests', 'seconds', 'covered_seconds', 'output_tps', 'decode_tps', 'hit_rate'):
            n = row[key]
            assert numeric(n) or (key in ('decode_tps', 'hit_rate') and n is None)
            safe['windows'][name][key] = n
        assert row['cached'] <= row['input'] and row['covered_seconds'] <= row['seconds'] + 1
        assert row['hit_rate'] is None or row['hit_rate'] <= 1
    return safe


def fetch_all(settings):
    def one(item):
        port, config = item
        try:
            return port, _fetch(config)
        except Exception:
            return port, None
    with ThreadPoolExecutor(max_workers=len(settings)) as pool:
        return dict(pool.map(one, settings.items()))


def lines(value, now):
    try:
        if value is None or not 0 <= now - value['as_of'] <= 90 or value['since'] is None:
            return ['统计暂不可用，不能视为 0。']
        stamp = lambda epoch: datetime.fromtimestamp(epoch, TZ).strftime('%m-%d %H:%M:%S')
        output = ['统计自 ' + stamp(value['since']) + '，截止 ' + stamp(value['as_of']) + '（北京时间）']
        for name, title in NAMES:
            row = value['windows'][name]
            # A callback heartbeat is periodic; allow up to one heartbeat at the tail.
            missing = max(0, row['seconds'] - row['covered_seconds'])
            partial = missing > 30 or value['errors'] or value['as_of'] - value['last_seen'] > 30
            note = f'；未覆盖约{missing / 60:.1f}分钟' if partial else ''
            rate = '—' if row['decode_tps'] is None else f"{row['decode_tps']:.2f}"
            hit = '—' if row['hit_rate'] is None else f"{row['hit_rate']:.1%}"
            output.append(f"{title}：入{int(row['input']):,} 出{int(row['output']):,} 缓{int(row['cached']):,}"
                f"（{hit}）；均{row['output_tps']:.2f} tok/s；生成{rate} tok/s{note}")
        if value['errors']:
            output.append('记账异常：以上为已保存数量，统计不完整。')
        return output
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return ['统计暂不可用，不能视为 0。']
