"""Read Motor's registered vLLM counters without changing the inference path."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
from http.client import HTTPConnection, HTTPException
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time


MAX_PAYLOAD = 1024 * 1024
MAX_INSTANCES = 32
MAX_ENDPOINTS = 256
BUCKET_SECONDS = 600
BUCKET_COUNT = 144
HISTORY_METHOD = 'vllm-counter-rate-v1'
APPLICATION_ID = 0x4C444D48
SCHEMA_VERSION = 1
METRICS = frozenset({
    'process_start_time_seconds', 'vllm:prompt_tokens_by_source_total',
    'vllm:generation_tokens_total', 'vllm:request_prefill_time_seconds_sum',
    'vllm:request_prefill_time_seconds_count', 'vllm:request_decode_time_seconds_sum',
    'vllm:request_decode_time_seconds_count', 'vllm:num_requests_running',
    'vllm:num_requests_waiting', 'vllm:prompt_tokens_total',
    'vllm:prefix_cache_hits_total', 'vllm:prefix_cache_queries_total',
})
SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\d+)?\s*$')
LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*("(?:[^"\\]|\\["\\n])*")\s*(,|$)')


def parse_metrics(payload):
    """Parse only the bounded counter subset; duplicate series are ambiguous."""
    if not isinstance(payload, str) or len(payload.encode('utf-8')) > MAX_PAYLOAD:
        raise ValueError('INVALID_METRICS')
    result = {}
    for line in payload.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name = line.split('{', 1)[0].split(None, 1)[0]
        if name not in METRICS:
            continue
        sample = SAMPLE.fullmatch(line)
        if not sample:
            raise ValueError('INVALID_SAMPLE')
        labels = {}
        raw = sample.group(2)
        if raw is not None:
            offset = 0
            while offset < len(raw):
                match = LABEL.match(raw, offset)
                if not match or match.group(1) in labels:
                    raise ValueError('INVALID_LABELS')
                labels[match.group(1)] = json.loads(match.group(2))
                offset = match.end()
                if match.group(3) == '' and offset != len(raw):
                    raise ValueError('INVALID_LABELS')
                if match.group(3) == ',' and offset == len(raw):
                    raise ValueError('INVALID_LABELS')
        value = float(sample.group(3))
        if not math.isfinite(value) or value < 0:
            raise ValueError('INVALID_VALUE')
        if name.endswith('_count') and not value.is_integer():
            raise ValueError('INVALID_COUNT')
        key = tuple(sorted(labels.items()))
        samples = result.setdefault(name, {})
        if key in samples:
            raise ValueError('DUPLICATE_SAMPLE')
        samples[key] = value
    starts = result.get('process_start_time_seconds', {})
    if set(starts) != {()} or starts[()] <= 0:
        raise ValueError('MISSING_PROCESS_IDENTITY')
    return result


def _get(host, port, path):
    connection = HTTPConnection(host, port, timeout=2)
    try:
        connection.request('GET', path, headers={'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('SOURCE_UNAVAILABLE')
        body = response.read(MAX_PAYLOAD + 1)
        if len(body) > MAX_PAYLOAD:
            raise ValueError('OVERSIZED_SOURCE')
        return body.decode('utf-8')
    finally:
        connection.close()


def _fingerprint(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _topology(payload):
    value = json.loads(payload)
    instances = value.get('instances') if isinstance(value, dict) else None
    if not isinstance(instances, list) or len(instances) > MAX_INSTANCES:
        raise ValueError('INVALID_TOPOLOGY')
    result = {}
    count = 0
    for entry in instances:
        if not isinstance(entry, dict):
            raise ValueError('INVALID_INSTANCE')
        identity, role = entry.get('id'), entry.get('role')
        if type(identity) is int and 0 <= identity < 2**63:
            identity = str(identity)
        if not isinstance(identity, str) or not identity or len(identity) > 512 or identity in result:
            raise ValueError('INVALID_IDENTITY')
        if role not in ('prefill', 'decode'):
            continue
        endpoints = entry.get('endpoints')
        if not isinstance(endpoints, list):
            raise ValueError('INVALID_ENDPOINTS')
        targets = []
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                raise ValueError('INVALID_ENDPOINT')
            address = str(ipaddress.ip_address(endpoint.get('ip')))
            port = endpoint.get('business_port')
            if type(port) is int:
                parsed_port = port
            elif isinstance(port, str) and port.isascii() and port.isdigit() and len(port) <= 5:
                parsed_port = int(port)
            else:
                raise ValueError('INVALID_PORT')
            if not 1 <= parsed_port <= 65535:
                raise ValueError('INVALID_PORT')
            targets.append((address, parsed_port))
        count += len(targets)
        if count > MAX_ENDPOINTS:
            raise ValueError('TOO_MANY_ENDPOINTS')
        result[identity] = {'role': role, 'targets': tuple(sorted(targets)),
                            'ready': entry.get('status') == 'active' and entry.get('healthy') is True}
    return result


def _selected(values, name, source=None):
    series = values.get(name, {})
    if source is not None:
        series = {labels: value for labels, value in series.items() if dict(labels).get('source') == source}
    return series


def _delta(current, previous, name, source=None):
    after, before = _selected(current, name, source), _selected(previous, name, source)
    if not after or set(after) != set(before):
        return None
    diffs = [after[key] - before[key] for key in after]
    if any(value < 0 or not math.isfinite(value) for value in diffs):
        return None
    value = sum(diffs)
    return value if math.isfinite(value) else None


def _sum_metric(values, name):
    series = values.get(name, {})
    if not series:
        return None
    result = sum(series.values())
    return result if math.isfinite(result) else None


def _sum_values(values):
    if not values:
        return None
    result = sum(values)
    return result if math.isfinite(result) else None


def _agreed(topologies):
    available = [top for top in topologies if top is not None]
    groups, ambiguous = {}, False
    if available:
        identities = set().union(*(set(top) for top in available))
        for identity in identities:
            entries = [top.get(identity) for top in available]
            if any(entry is None for entry in entries):
                ambiguous = True
                continue
            shape = {(entry['role'], entry['targets']) for entry in entries}
            if len(shape) != 1:
                ambiguous = True
                continue
            groups[identity] = {**entries[0], 'ready': all(entry['ready'] for entry in entries)}
    return groups, ambiguous


class MotorHistory:
    """Persist sampled rates in ten-minute buckets in a dedicated identified file."""
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
                connection.execute('''CREATE TABLE rate_buckets (
                    group_key TEXT NOT NULL, phase TEXT NOT NULL, bucket INTEGER NOT NULL,
                    rate_sum REAL NOT NULL, samples INTEGER NOT NULL, partial_samples INTEGER NOT NULL,
                    PRIMARY KEY (group_key, phase, bucket)) WITHOUT ROWID''')
            elif (connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or
                  connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
                raise ValueError('INVALID_MOTOR_HISTORY')
            connection.execute('PRAGMA journal_mode = WAL')

    def _connect(self):
        return sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=1)

    def record(self, group_key, phase, at, rate, *, partial=False):
        if (not isinstance(group_key, str) or not group_key or phase not in ('prefill', 'decode') or
                not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0 or
                not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0 or
                type(partial) is not bool):
            raise ValueError('INVALID_RATE_SAMPLE')
        bucket = int(at // BUCKET_SECONDS) * BUCKET_SECONDS
        with closing(self._connect()) as connection, connection:
            connection.execute('''INSERT INTO rate_buckets VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(group_key, phase, bucket) DO UPDATE SET
                rate_sum = rate_sum + excluded.rate_sum, samples = samples + 1,
                partial_samples = partial_samples + excluded.partial_samples''',
                               (group_key, phase, bucket, rate, int(partial)))

    def series(self, group_key, now):
        end = int(now // BUCKET_SECONDS) * BUCKET_SECONDS
        start = end - BUCKET_COUNT * BUCKET_SECONDS
        if start < 0:
            return None
        with closing(self._connect()) as connection:
            rows = connection.execute('''SELECT bucket, phase, rate_sum, samples, partial_samples FROM rate_buckets
                WHERE group_key = ? AND bucket >= ? AND bucket < ?''', (group_key, start, end)).fetchall()
        grouped = {(bucket, phase): (total, samples, partial) for bucket, phase, total, samples, partial in rows}
        points = []
        for bucket in range(start, end, BUCKET_SECONDS):
            point = {'start': bucket, 'end': bucket + BUCKET_SECONDS}
            for phase in ('prefill', 'decode'):
                total, samples, partial = grouped.get((bucket, phase), (0, 0, 0))
                point[phase + '_tps'] = total / samples if samples else None
                point[phase + '_samples'] = samples
                point[phase + '_partial'] = partial > 0
            points.append(point)
        return {'method': HISTORY_METHOD, 'bucket_seconds': BUCKET_SECONDS, 'points': points}


class MotorCollector:
    """Bounded passive scrapes; only same-process counter deltas produce rates."""
    def __init__(self, management_ports, *, fetch_topology=None, fetch_endpoint=None,
                 wall_time=time.time, monotonic=time.monotonic, interval=30, history=None):
        ports = tuple(dict.fromkeys(management_ports))
        if (not ports or len(ports) > 8 or any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
                or not 5 <= interval <= 300):
            raise ValueError('INVALID_MOTOR_CONFIG')
        self.ports = ports
        self.fetch_topology = fetch_topology or (lambda port: _get('127.0.0.1', port, '/instances'))
        self.fetch_endpoint = fetch_endpoint or (lambda host, port: _get(host, port, '/metrics'))
        self.wall_time, self.monotonic, self.interval = wall_time, monotonic, interval
        self.history = history
        self.previous = {}
        self.sequence = 0
        self.lock = threading.Lock()
        self.collect_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.value = {'schema': 1, 'kind': 'vllm-motor-telemetry', 'sampled_at': 0,
                      'nodes': [], 'history_available': False}
        self.worker = None

    def _read_topology(self, port):
        try:
            return _topology(self.fetch_topology(port))
        except Exception:
            return None

    def _read_endpoint(self, target):
        started = self.monotonic()
        try:
            values = parse_metrics(self.fetch_endpoint(*target))
            ended = self.monotonic()
            return values, ended, self.wall_time(), max(0, ended - started)
        except Exception:
            return None

    def collect(self):
        with self.collect_lock:
            return self._collect()

    def _collect(self):
        with ThreadPoolExecutor(max_workers=min(4, len(self.ports))) as executor:
            before = list(executor.map(self._read_topology, self.ports))
        before_groups, ambiguous = _agreed(before)
        before_addresses = Counter(target for group in before_groups.values() for target in group['targets'])
        targets = [target for target, count in before_addresses.items() if count == 1]
        with ThreadPoolExecutor(max_workers=4) as executor:
            readings = dict(zip(targets, executor.map(self._read_endpoint, targets)))
        with ThreadPoolExecutor(max_workers=min(4, len(self.ports))) as executor:
            after = list(executor.map(self._read_topology, self.ports))
        after_groups, after_ambiguous = _agreed(after)
        common_sources = sum(left is not None and right is not None for left, right in zip(before, after))
        ambiguous = ambiguous or after_ambiguous or not common_sources
        changed = {identity for identity in set(before_groups) | set(after_groups)
                   if before_groups.get(identity) != after_groups.get(identity)}
        if changed:
            ambiguous = True
        groups = {identity: value for identity, value in before_groups.items()
                  if common_sources and identity not in changed}
        after_addresses = Counter(target for group in after_groups.values() for target in group['targets'])
        ambiguous_targets = {target for target in set(before_addresses) | set(after_addresses)
                             if before_addresses[target] > 1 or after_addresses[target] > 1}
        for topology in before + after:
            if topology is not None:
                counts = Counter(target for group in topology.values() for target in group['targets'])
                ambiguous_targets.update(target for target, count in counts.items() if count > 1)
        for identity in changed:
            for group in (before_groups.get(identity), after_groups.get(identity)):
                if group is not None:
                    ambiguous_targets.update(group['targets'])
        if ambiguous_targets:
            ambiguous = True
        self.sequence += 1
        now = self.wall_time()
        now_monotonic = self.monotonic()
        new_previous, nodes = {}, []
        history_ok = self.history is not None
        for identity, group in sorted(groups.items()):
            role = group['role']
            group_key = _fingerprint(json.dumps(['motor', identity, role, group['targets']], separators=(',', ':')))
            # 48 bits stay exactly representable as an integer in the browser.
            public_id = int(group_key[:12], 16)
            node = {'label': ('Prefill ' if role == 'prefill' else 'Decode ') + group_key[:8],
                    'backend_port': public_id, 'role': role, 'received_at': now,
                    'source_ok': False, 'backend_ready': group['ready'], 'clock_ok': True,
                    'clock_uncertainty_seconds': 0, 'driver_ok': False,
                    'expected_endpoints': len(group['targets']),
                    'prefill': None, 'decode': None, 'hbm': None, 'phase_timers': None, 'history': None}
            rates, window_starts, window_ends, deadlines, observed_times, count_delta, seconds_delta = [], [], [], [], [], 0, 0.0
            metrics = {'running': [], 'waiting': [], 'input_total': [], 'hit_pairs': []}
            observed_endpoints = 0
            valid_timer_workers = 0
            for target in group['targets']:
                if target in ambiguous_targets or not group['ready']:
                    continue
                reading = readings.get(target)
                if reading is None:
                    continue
                values, stamp, sampled_at, uncertainty = reading
                age = now_monotonic - stamp
                earliest = sampled_at - uncertainty
                if (not 0 <= age + uncertainty < self.interval * 2 + 5 or
                        not 0 <= now - earliest < self.interval * 2 + 5 or uncertainty > 20):
                    continue
                observed_endpoints += 1
                observed_times.append(earliest)
                node['source_ok'] = True
                node['clock_uncertainty_seconds'] = max(node['clock_uncertainty_seconds'], uncertainty)
                for key, name in (('running', 'vllm:num_requests_running'),
                                  ('waiting', 'vllm:num_requests_waiting')):
                    value = _sum_metric(values, name)
                    if value is not None:
                        metrics[key].append(value)
                if role == 'prefill':
                    total = _sum_metric(values, 'vllm:prompt_tokens_total')
                    hits = _sum_metric(values, 'vllm:prefix_cache_hits_total')
                    queries = _sum_metric(values, 'vllm:prefix_cache_queries_total')
                    if total is not None:
                        metrics['input_total'].append(total)
                    if hits is not None and queries is not None and hits <= queries:
                        metrics['hit_pairs'].append((hits, queries))
                endpoint_key = (group_key, target)
                previous = self.previous.get(endpoint_key)
                new_previous[endpoint_key] = (values, stamp, sampled_at, uncertainty)
                if previous is None:
                    continue
                old, old_stamp, old_sampled_at, old_uncertainty = previous
                duration = stamp - old_stamp
                if (not 0 < duration <= self.interval * 1.5 + 5 or
                        sampled_at < old_sampled_at or
                        abs((sampled_at - old_sampled_at) - duration) > 5 + uncertainty + old_uncertainty or
                        values['process_start_time_seconds'] != old['process_start_time_seconds']):
                    continue
                name = 'vllm:prompt_tokens_by_source_total' if role == 'prefill' else 'vllm:generation_tokens_total'
                change = _delta(values, old, name, 'local_compute' if role == 'prefill' else None)
                if change is None:
                    continue
                rates.append(change / duration)
                window_starts.append(old_sampled_at - old_uncertainty)
                window_ends.append(sampled_at)
                deadlines.append(earliest + self.interval * 2 + 5)
                prefix = 'vllm:request_' + role + '_time_seconds_'
                count = _delta(values, old, prefix + 'count')
                seconds = _delta(values, old, prefix + 'sum')
                same_series = (set(_selected(values, prefix + 'count')) == set(_selected(values, prefix + 'sum'))
                               and set(_selected(old, prefix + 'count')) == set(_selected(old, prefix + 'sum')))
                if count is not None and seconds is not None and count > 0 and same_series:
                    count_delta += count
                    seconds_delta += seconds
                    valid_timer_workers += 1
            if rates:
                rate = sum(rates)
                span = max(window_ends) - min(window_starts)
                valid_until = min(deadlines)
                if math.isfinite(rate) and 0 <= span <= 60 and valid_until > now:
                    part = {'tokens_per_second': rate, 'log_at': max(window_ends), 'log_skew_seconds': span,
                            'valid_until': valid_until, 'step': self.sequence,
                            'valid_endpoints': len(rates), 'expected_endpoints': len(group['targets']),
                            'partial': len(rates) < len(group['targets']) or common_sources < len(self.ports) or ambiguous,
                            'mfu_estimate': None}
                    node[role] = part
                    if valid_timer_workers and count_delta and math.isfinite(count_delta) and math.isfinite(seconds_delta):
                        node['phase_timers'] = {'step': self.sequence, role: {
                            'mean_ms': seconds_delta * 1000 / count_delta,
                            'samples': int(count_delta), 'ranks': valid_timer_workers,
                            'expected_ranks': len(group['targets'])}}
                    if self.history is not None:
                        try:
                            self.history.record(group_key, role, part['log_at'], rate, partial=part['partial'])
                        except (OSError, sqlite3.Error, ValueError, OverflowError):
                            history_ok = False
            pairs = metrics['hit_pairs']
            hit_total = _sum_values([hit for hit, _ in pairs])
            query_total = _sum_values([query for _, query in pairs])
            node['received_at'] = min(observed_times) if observed_times else None
            node['engine_metrics'] = {
                'running': _sum_values(metrics['running']),
                'waiting': _sum_values(metrics['waiting']),
                'input_total': _sum_values(metrics['input_total']),
                'hit_rate': hit_total / query_total if hit_total is not None and query_total else None,
                'observed_endpoints': observed_endpoints, 'expected_endpoints': len(group['targets']),
                'sampled_at': min(observed_times), 'valid_until': min(observed_times) + self.interval * 2 + 5,
                'partial': (observed_endpoints < len(group['targets']) or common_sources < len(self.ports)
                            or ambiguous or len(metrics['running']) < observed_endpoints
                            or len(metrics['waiting']) < observed_endpoints or
                            (role == 'prefill' and (len(metrics['input_total']) < observed_endpoints or
                                                    len(metrics['hit_pairs']) < observed_endpoints))),
            } if observed_times else None
            if self.history is not None:
                try:
                    node['history'] = self.history.series(group_key, now)
                except (OSError, sqlite3.Error, ValueError, OverflowError):
                    history_ok = False
            nodes.append(node)
        self.previous = new_previous
        if len({node['backend_port'] for node in nodes}) != len(nodes):
            nodes, ambiguous = [], True
        result = {'schema': 1, 'kind': 'vllm-motor-telemetry', 'sampled_at': now, 'nodes': nodes,
                  'topology_sources': {'available': common_sources, 'total': len(self.ports)},
                  'topology_ambiguous': ambiguous, 'prefill_tps': None, 'decode_tps': None,
                  'prefill_mfu': None, 'hbm_percent': None, 'history_available': history_ok,
                  'history_dropped': 0}
        with self.lock:
            self.value = result
        return deepcopy(result)

    def snapshot(self):
        with self.lock:
            value = deepcopy(self.value)
        now = self.wall_time()
        age = now - value['sampled_at']
        if not 0 <= age <= self.interval * 2 + 20:
            return {'schema': 1, 'kind': 'vllm-motor-telemetry', 'sampled_at': 0,
                    'nodes': [], 'history_available': False}
        for node in value['nodes']:
            for phase in ('prefill', 'decode'):
                part = node.get(phase)
                if part is not None and (not 0 <= now - part['log_at'] or now >= part['valid_until']):
                    node[phase] = None
            timers = node.get('phase_timers')
            if isinstance(timers, dict) and not any(node.get(phase) and node[phase]['step'] == timers.get('step')
                                                    for phase in ('prefill', 'decode')):
                node['phase_timers'] = None
            metrics = node.get('engine_metrics')
            if metrics is not None and (not 0 <= now - metrics['sampled_at'] or now >= metrics['valid_until']):
                node['engine_metrics'] = None
        return value

    def start(self):
        if self.worker is not None:
            raise RuntimeError('COLLECTOR_ALREADY_STARTED')
        def run():
            while not self.stop_event.is_set():
                try:
                    self.collect()
                except Exception:
                    pass
                if self.stop_event.wait(self.interval):
                    break
        self.worker = threading.Thread(target=run, name='motor-metrics', daemon=True)
        self.worker.start()

    def close(self):
        self.stop_event.set()
        if self.worker is not None:
            self.worker.join(timeout=5)
