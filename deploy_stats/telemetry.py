"""Collect sanitized engine-step and device samples via read-only SSH probes."""
import argparse
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import threading
import time

from .backends import read_backend
from .telemetry_history import History


SOURCE_KEYS = frozenset(('stats', 'backend', 'parallel', 'engine', 'sampler', 'native'))
HOST = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}@[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z')
CONTAINER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
MAX_AGE = 60.0
MAX_LOG_SKEW = 5.0
MAX_STEP_SPREAD = .10
HBM_MAX_AGE = 90.0


def finite(value, *, minimum=None, maximum=None):
    try:
        return (type(value) in (int, float) and math.isfinite(value) and
                (minimum is None or value >= minimum) and (maximum is None or value <= maximum))
    except OverflowError:
        return False


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {'interval_seconds', 'nodes'}:
        raise ValueError('INVALID_TELEMETRY_CONFIG')
    interval, nodes = value['interval_seconds'], value['nodes']
    if not finite(interval, minimum=15, maximum=60) or not isinstance(nodes, list) or not 1 <= len(nodes) <= 8:
        raise ValueError('INVALID_TELEMETRY_CONFIG')
    ports, labels, targets = set(), set(), set()
    fields = {'label', 'backend_port', 'ssh_target', 'container', 'source_sha256', 'rank_devices',
              'image_fingerprint', 'driver_sha256', 'prefill_flops_per_token', 'peak_flops_per_device'}
    for node in nodes:
        if not isinstance(node, dict) or set(node) != fields:
            raise ValueError('INVALID_TELEMETRY_NODE')
        label, port = node['label'], node['backend_port']
        target, container, sources, devices = (node['ssh_target'], node['container'],
                                                node['source_sha256'], node['rank_devices'])
        if (not isinstance(label, str) or not 1 <= len(label) <= 32 or any(ord(c) < 32 for c in label) or
                type(port) is not int or not 1 <= port <= 65535 or port in ports or label in labels or
                not isinstance(target, str) or not HOST.fullmatch(target) or target in targets or
                not isinstance(container, str) or not CONTAINER.fullmatch(container) or
                not isinstance(node['image_fingerprint'], str) or not SHA256.fullmatch(node['image_fingerprint']) or
                not isinstance(node['driver_sha256'], str) or not SHA256.fullmatch(node['driver_sha256']) or
                not isinstance(sources, dict) or set(sources) != SOURCE_KEYS or
                any(not isinstance(v, str) or not SHA256.fullmatch(v) for v in sources.values()) or
                not isinstance(devices, list) or not 1 <= len(devices) <= 64 or
                any(type(d) is not int or not 0 <= d <= 255 for d in devices) or len(set(devices)) != len(devices) or
                not finite(node['prefill_flops_per_token'], minimum=1, maximum=1e30) or
                not finite(node['peak_flops_per_device'], minimum=1, maximum=1e25)):
            raise ValueError('INVALID_TELEMETRY_NODE')
        ports.add(port)
        labels.add(label)
        targets.add(target)
    return value


def _phase_groups(rows, devices, started, observed):
    """Align unique DP ranks by logger step; the result remains a sampled approximation."""
    if not isinstance(rows, list) or len(rows) > 1024:
        return {}
    groups, duplicates = {}, set()
    for row in rows:
        if not isinstance(row, dict) or row.get('kind') != 'stats':
            continue
        rank, device, step, at = (row.get(k) for k in ('rank', 'device', 'step', 'at'))
        if (type(rank) is not int or not 0 <= rank < len(devices) or type(device) is not int or device != devices[rank] or
                type(step) is not int or step < 0 or not finite(at, minimum=started, maximum=observed + 2)):
            continue
        group = groups.setdefault(step, {})
        if rank in group:
            duplicates.add(step)
        group[rank] = row
    latest = {}
    for step, group in groups.items():
        if step in duplicates or set(group) != set(range(len(devices))):
            continue
        data = list(group.values())
        at = [r['at'] for r in data]
        if max(at) - min(at) > MAX_LOG_SKEW or observed - min(at) > MAX_AGE:
            continue
        phases = {r.get('is_prompt') for r in data}
        if len(phases) != 1 or next(iter(phases)) not in (True, False) or any(type(r.get('is_prompt')) is not bool for r in data):
            continue
        if any(not finite(r.get('current_tps'), minimum=0, maximum=1e15) or
               not finite(r.get('current_sps'), minimum=.001, maximum=1e9) for r in data):
            continue
        rates = [r['current_sps'] for r in data]
        # cur_SPS is printed to three decimals; include the rounding interval.
        spread = (max(rates) + .0005) / (min(rates) - .0005) - 1
        if not finite(spread, maximum=MAX_STEP_SPREAD):
            continue
        phase = 'prefill' if data[0]['is_prompt'] else 'decode'
        item = {'step': step, 'log_at': max(at), 'rank_count': len(data),
                'log_skew_seconds': max(at) - min(at), 'step_rate_spread': spread,
                'tokens_per_second': sum(r['current_tps'] for r in data),
                '_rank_at': {rank: row['at'] for rank, row in group.items()}}
        identity = [step, phase, sorted(item['_rank_at'].items())]
        item['_observation_id'] = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
        if phase not in latest or item['log_at'] > latest[phase]['log_at']:
            latest[phase] = item
    return latest


def _phase_timers(rows, step, devices, source_started, rank_times):
    groups, duplicates = {}, set()
    for row in rows:
        if not isinstance(row, dict) or row.get('kind') != 'cpu' or row.get('step') != step:
            continue
        rank, at = row.get('rank'), row.get('at')
        if (type(rank) is not int or not 0 <= rank < len(devices) or row.get('device') != devices[rank] or
                rank not in rank_times or not finite(at, minimum=max(source_started, rank_times[rank]),
                                                     maximum=rank_times[rank] + MAX_LOG_SKEW)):
            continue
        if rank in groups:
            duplicates.add(rank)
        groups[rank] = row.get('timers')
    result = {}
    for phase in ('prefill', 'decode'):
        pairs = []
        for rank, timers in groups.items():
            if rank in duplicates or not isinstance(timers, dict):
                continue
            timer = timers.get(phase)
            if (isinstance(timer, dict) and finite(timer.get('mean_ms'), minimum=0, maximum=1e9) and
                    type(timer.get('calls')) is int and 0 < timer['calls'] < 10**9):
                pairs.append((timer['mean_ms'], timer['calls']))
        calls = sum(c for _, c in pairs)
        result[phase] = {'mean_ms': sum(m*c for m, c in pairs) / calls if calls else None,
                         'ranks': len(pairs), 'expected_ranks': len(devices)}
    return result


def summarize_node(raw, node, local_start, local_end):
    result = {'label': node['label'], 'backend_port': node['backend_port'], 'received_at': local_end,
              'clock_ok': False, 'clock_uncertainty_seconds': None, 'source_ok': False, 'driver_ok': False,
              'prefill': None, 'decode': None,
              'hbm': None, 'phase_timers': None}
    if not isinstance(raw, dict) or raw.get('schema') != 1:
        return result
    start, end, born = (raw.get(k) for k in ('started_at', 'finished_at', 'source_started_at'))
    if (not finite(start) or not finite(end, minimum=start) or not finite(local_start) or
            not finite(local_end, minimum=local_start) or end - start > 20 or
            end - start > local_end - local_start + .1):
        return result
    # Remote start follows local send, and remote finish precedes local receipt.
    # These two inequalities bound the unknown host clock offset without changing clocks.
    low, high = end - local_end, start - local_start
    if high < low:
        return result
    offset, uncertainty = (low + high) / 2, (high - low) / 2
    result['clock_ok'] = True
    result['clock_uncertainty_seconds'] = uncertainty
    result['probe_wall_seconds'] = end - start
    cost = raw.get('probe_cpu_seconds')
    result['probe_cpu_seconds'] = cost if finite(cost, minimum=0, maximum=20) else None
    identity, host, sources = raw.get('source_id'), raw.get('host_fingerprint'), raw.get('source_sha256')
    if (not isinstance(identity, str) or not SHA256.fullmatch(identity) or
            not isinstance(host, str) or not SHA256.fullmatch(host) or
            raw.get('image_fingerprint') != node['image_fingerprint'] or
            sources != node['source_sha256'] or raw.get('source_before_start') is not True or
            not finite(born, maximum=start)):
        return result
    result['source_ok'] = True
    result['host_fingerprint'] = host
    result['source_id'] = identity
    result['driver_ok'] = raw.get('driver_sha256') == node['driver_sha256']
    devices = node['rank_devices']
    result['assumptions'] = {'prefill_flops_per_token': node['prefill_flops_per_token'],
                             'peak_flops_per_device': node['peak_flops_per_device']}
    hbm = raw.get('hbm')
    if result['driver_ok'] and isinstance(hbm, list) and len(hbm) == len(devices):
        values = {}
        for item in hbm:
            if not isinstance(item, dict):
                continue
            device, left, right, value = (item.get('id'), item.get('started_at'), item.get('finished_at'),
                                          item.get('hbm_bandwidth_percent'))
            if (type(device) is not int or device in values or device not in devices or
                    not finite(left, minimum=start, maximum=end) or not finite(right, minimum=left, maximum=end) or
                    not finite(value, minimum=0, maximum=100)):
                continue
            values[device] = (left, right, value)
        if set(values) == set(devices):
            first, last = min(v[0] for v in values.values()), max(v[1] for v in values.values())
            if last - first <= MAX_LOG_SKEW:
                result['hbm'] = {'mean_percent': sum(v[2] for v in values.values()) / len(values),
                                 'device_count': len(values), 'sample_at': last - offset,
                                 'span_seconds': last - first,
                                 'valid_until': first - offset - uncertainty + HBM_MAX_AGE}
    rows = raw.get('engine_rows')
    groups = _phase_groups(rows, devices, born, end)
    for phase in ('prefill', 'decode'):
        result[phase] = groups.get(phase)
        if result[phase] is not None:
            result[phase]['log_at'] -= offset
            result[phase]['valid_until'] = (result[phase]['log_at'] - result[phase]['log_skew_seconds'] -
                                            uncertainty + MAX_AGE)
    if result['prefill'] is not None:
        rate = result['prefill']['tokens_per_second']
        efficiency = rate * node['prefill_flops_per_token'] / (len(devices) * node['peak_flops_per_device'])
        result['prefill']['mfu_estimate'] = efficiency if finite(efficiency, minimum=0) else None
        result['prefill']['peak_flops'] = len(devices) * node['peak_flops_per_device']
    steps = [v['step'] for v in groups.values()]
    if steps and isinstance(rows, list):
        latest = max(groups.values(), key=lambda value: value['log_at'])
        result['phase_timers'] = _phase_timers(rows, latest['step'], devices, born, latest['_rank_at'])
        result['phase_timers']['step'] = latest['step']
    for value in groups.values():
        value.pop('_rank_at', None)
    return result


def summarize(nodes, sampled_at):
    """Keep independent device groups separate and reject duplicate host identities."""
    nodes = deepcopy(nodes)
    for node in nodes:
        host, identity = node.get('host_fingerprint'), node.get('source_id')
        duplicate = any(other is not node and ((host and other.get('host_fingerprint') == host) or
                        (identity and other.get('source_id') == identity)) for other in nodes)
        if duplicate:
            node.update(source_ok=False, duplicate_source=True, prefill=None, decode=None, hbm=None, phase_timers=None)
    out = {'schema': 1, 'kind': 'deepinfer-telemetry', 'sampled_at': sampled_at, 'nodes': nodes,
           'prefill_tps': None, 'decode_tps': None,
           'prefill_mfu': None, 'hbm_percent': None}
    if len(nodes) != 1 or not nodes[0].get('source_ok'):
        return out
    current = nodes[0]
    for phase in ('prefill', 'decode'):
        part = current.get(phase)
        if isinstance(part, dict):
            out[phase + '_tps'] = part['tokens_per_second']
            if phase == 'prefill':
                out['prefill_mfu'] = part['mfu_estimate']
    if isinstance(current.get('hbm'), dict):
        out['hbm_percent'] = current['hbm']['mean_percent']
    return out


def public_snapshot(value):
    value = deepcopy(value)
    for node in value.get('nodes', []):
        node.pop('host_fingerprint', None)
        node.pop('source_id', None)
        for phase in ('prefill', 'decode'):
            if isinstance(node.get(phase), dict):
                node[phase].pop('_observation_id', None)
    return value


def ssh_sample(node, script):
    start = time.time()
    try:
        before = read_backend(node['backend_port'])
        args = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1',
                '-o', 'StrictHostKeyChecking=yes', node['ssh_target'], 'python3 - ' + node['container']]
        result = subprocess.run(args, input=script, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=20, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        end = time.time()
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            return summarize_node(None, node, start, end)
        sampled = summarize_node(json.loads(result.stdout), node, start, end)
        after = read_backend(node['backend_port'])
        ready = all(value.get('available') is True and value.get('admission_ready') == 1 and
                    value.get('workers_alive') == len(node['rank_devices']) for value in (before, after))
        sampled['backend_ready'] = ready
        if not ready:
            sampled.update(prefill=None, decode=None, phase_timers=None)
        return sampled
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        return summarize_node(None, node, start, time.time())


class HistoryWorker:
    """Slow storage can never block the live sampling or HTTP threads."""

    def __init__(self, config, history=None, path=None):
        self.configs = {node['backend_port']: node for node in config['nodes']}
        self.history, self.path = history, path
        self.queue = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.cache = None
        self.dropped = 0
        self.changed = threading.Event()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name='telemetry-history')
        self.thread.start()

    def submit(self, value):
        try:
            self.queue.put_nowait(deepcopy(value))
        except queue.Full:
            with self.lock:
                self.dropped += 1

    def current(self):
        with self.lock:
            return deepcopy(self.cache), self.dropped

    def run(self):
        while not self.stop_event.is_set():
            try:
                value = self.queue.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                if self.history is None:
                    self.history = open_history(self.path)
                if self.history is None:
                    raise ValueError('TELEMETRY_HISTORY_UNAVAILABLE')
                result = {}
                for node in value['nodes']:
                    config = self.configs[node['backend_port']]
                    self.history.record(node, config, value['sampled_at'])
                    host = node.get('host_fingerprint') if node.get('source_ok') else None
                    result[node['backend_port']] = {'host': host,
                        'history': self.history.series(config, host, value['sampled_at']) if host else None}
                with self.lock:
                    self.cache = {'sampled_at': value['sampled_at'], 'nodes': result}
            except (OSError, sqlite3.Error, ValueError, OverflowError):
                with self.lock:
                    self.cache = None
            finally:
                self.changed.set()
                self.queue.task_done()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=1)


class Collector:
    def __init__(self, config, sample=ssh_sample, history=None, history_path=None):
        self.config = validate_config(config)
        self.sample = sample
        self.script = Path(__file__).with_name('remote_observe.py').read_bytes()
        self.lock = threading.Lock()
        self.value = summarize([], 0)
        self.history_worker = (HistoryWorker(self.config, history=history, path=history_path)
                               if history is not None or history_path is not None else None)
        self.stop_event = threading.Event()

    def collect(self):
        with ThreadPoolExecutor(max_workers=len(self.config['nodes'])) as executor:
            nodes = list(executor.map(lambda node: self.sample(node, self.script), self.config['nodes']))
        value = summarize(nodes, time.time())
        with self.lock:
            self.value = value
        if self.history_worker is not None:
            self.history_worker.submit(value)
        return value

    def snapshot(self):
        with self.lock:
            value = deepcopy(self.value)
        now = time.time()
        age = now - value['sampled_at']
        if not 0 <= age <= 2 * self.config['interval_seconds'] + 20:
            return summarize([], 0)
        for node in value['nodes']:
            uncertainty = node.get('clock_uncertainty_seconds')
            for phase in ('prefill', 'decode'):
                part = node.get(phase)
                if (part is not None and (not finite(uncertainty, minimum=0) or
                        not finite(part.get('valid_until')) or now >= part['valid_until'])):
                    node[phase] = None
            part = node.get('hbm')
            if (part is not None and (not finite(uncertainty, minimum=0) or
                    not finite(part.get('valid_until')) or now >= part['valid_until'])):
                node['hbm'] = None
            timers = node.get('phase_timers')
            valid_steps = {node[p]['step'] for p in ('prefill', 'decode') if node.get(p) is not None}
            if isinstance(timers, dict) and timers.get('step') not in valid_steps:
                node['phase_timers'] = None
        value = summarize(value['nodes'], value['sampled_at'])
        cached, dropped = self.history_worker.current() if self.history_worker is not None else (None, 0)
        history_ok = (isinstance(cached, dict) and finite(cached.get('sampled_at')) and
                      0 <= now - cached['sampled_at'] <= 2 * self.config['interval_seconds'] + 20)
        value['history_available'] = bool(history_ok)
        value['history_dropped'] = dropped
        if history_ok:
            for node in value['nodes']:
                entry = cached['nodes'].get(node['backend_port'])
                node['history'] = (entry['history'] if entry and node.get('source_ok') and
                                   entry['host'] == node.get('host_fingerprint') else None)
        return value

    def close(self):
        self.stop_event.set()
        if self.history_worker is not None:
            self.history_worker.close()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.collect()
            except Exception:
                with self.lock:
                    self.value = summarize([], 0)
            self.stop_event.wait(self.config['interval_seconds'])


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port, collector):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('INVALID_PORT')
        self.collector = collector
        super().__init__(('127.0.0.1', port), Handler)

    def server_bind(self):
        import socket
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        hosts = self.headers.get_all('Host', [])
        origins = self.headers.get_all('Origin', [])
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        trusted = (len(hosts) == 1 and hosts[0].lower() in allowed and len(origins) <= 1 and
                   (not origins or origins[0].lower() == 'http://' + hosts[0].lower()) and
                   self.headers.get('Sec-Fetch-Site', '').lower() not in ('cross-site', 'same-site'))
        if not trusted:
            status, body = 403, b'Forbidden'
        elif self.path != '/snapshot':
            status, body = 404, b'Not found'
        else:
            status = 200
            body = json.dumps(public_snapshot(self.server.collector.snapshot()), allow_nan=False, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json' if status == 200 else 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--port', type=int, default=19883)
    parser.add_argument('--history-database', type=Path)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    collector = Collector(config, history_path=args.history_database)
    if args.once:
        collector.collect()
        if collector.history_worker is not None:
            collector.history_worker.changed.wait(timeout=3)
        print(json.dumps(public_snapshot(collector.snapshot()), allow_nan=False, separators=(',', ':')))
        collector.close()
        return
    server = Server(args.port, collector)
    thread = threading.Thread(target=collector.run, daemon=True, name='telemetry-collector')
    thread.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.close()
        server.server_close()


def open_history(path):
    if path is None:
        return None
    try:
        return History(path)
    except (OSError, sqlite3.Error, ValueError, OverflowError):
        return None


if __name__ == '__main__':
    main()
