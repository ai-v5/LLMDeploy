"""Read bounded DeepInfer log summaries and Ascend device metrics on a host.

This file is streamed to the remote Python interpreter; raw container logs and
container configuration are not returned or written by the probe.
"""
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import selectors
import subprocess
import sys
import time


SOURCES = {'stats': '/opt/deepinfer-build/source/deepinfer/engine/stats_logger.py',
           'backend': '/opt/deepinfer-build/source/examples/recipe_api/backend.py',
           'parallel': '/opt/deepinfer-build/source/deepinfer/config.py',
           'engine': '/opt/deepinfer-build/source/deepinfer/engine/llm_engine.py',
           'sampler': '/opt/deepinfer-build/source/deepinfer/model_executor/layers/sampler.py',
           'native': '/opt/deepinfer-build/source/deepinfer/_so/ascend-aarch64/_C.cpython-312-aarch64-linux-gnu.so'}
LOG_LIMIT = 8 * 1024 * 1024
DRIVER = '/usr/local/Ascend/driver/lib64/driver/libdcmi.so'
CONTAINER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
HEADER = re.compile(r'RANK\s+(\d+),\s*GPU\s+(\d+),\s*step\s+(\d+),\s*\[(stats|cpu)\]')
STAMP = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\s')
NUMBER = r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
TIMER = re.compile(r'engine_step:(prefill|decode)\s+cur(' + NUMBER + r')ms/mean(' + NUMBER +
                   r')ms/max(' + NUMBER + r')ms/(\d+)calls/')


def _number(line, name):
    match = re.search(r'(?:^|\|\s*)' + re.escape(name) + r'\s+(' + NUMBER + r')(?=\s|\||\))', line)
    if not match:
        return None
    value = float(match.group(1))
    return value if math.isfinite(value) and value >= 0 else None


def parse_log(data):
    """Extract numerical log fields only; invalid fields do not erase good ones."""
    rows = []
    for line in data.decode('utf-8', errors='replace').splitlines():
        stamp, head = STAMP.match(line), HEADER.search(line)
        if not stamp or not head:
            continue
        try:
            at = datetime.fromisoformat(stamp.group(1).replace('Z', '+00:00')).timestamp()
            rank, device, step = (int(head.group(i)) for i in (1, 2, 3))
        except (ValueError, OverflowError):
            continue
        if rank > 255 or device > 255 or step > 2**63 - 1:
            continue
        row = {'at': at, 'rank': rank, 'device': device, 'step': step, 'kind': head.group(4)}
        if row['kind'] == 'stats':
            phase = re.search(r'\bis_prompt\s+(True|False)(?=\s|\||\))', line)
            row.update(is_prompt=(phase.group(1) == 'True') if phase else None,
                       current_tps=_number(line, 'cur_TPS'), current_sps=_number(line, 'cur_SPS'),
                       prompt_tps=_number(line, 'prompt_TPS'), decode_tps=_number(line, 'completion_TPS'),
                       mixed_mfu=_number(line, 'MFU'))
            share = re.search(r'\bprefill/decode\s+(' + NUMBER + r')%/(' + NUMBER + r')%', line)
            if share:
                p, d = (float(share.group(i)) for i in (1, 2))
                row['phase_share'] = [p, d] if math.isfinite(p) and math.isfinite(d) and p <= 100 and d <= 100 else None
            else:
                row['phase_share'] = None
        else:
            timers = {}
            for match in TIMER.finditer(line):
                phase = match.group(1)
                if phase in timers:
                    timers[phase] = None
                    continue
                cur, mean, maximum = (float(match.group(i)) for i in (2, 3, 4))
                calls = int(match.group(5))
                timers[phase] = ({'mean_ms': mean, 'calls': calls} if calls > 0 and calls < 10**9 and
                                 all(math.isfinite(x) and x >= 0 for x in (cur, mean, maximum)) else None)
            row['timers'] = timers
        rows.append(row)
    return rows


def bounded_command(command, timeout, limit):
    """Collect finite output without leaving a blocked or unbounded log reader."""
    if command and command[0] == 'docker' and not docker_ready():
        raise ValueError('docker is not safely available')
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    output = bytearray()
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    try:
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stdout, selectors.EVENT_READ)
        while selector.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('probe timeout')
            for key, _ in selector.select(min(left, .25)):
                chunk = os.read(key.fd, min(65536, limit + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise ValueError('probe output too large')
        if proc.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
            raise ValueError('probe command failed')
        return bytes(output)
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        proc.stdout.close()


def docker_ready():
    """Do not trigger Docker socket activation while observing an idle host."""
    try:
        service = subprocess.run(['systemctl', 'is-active', 'docker.service'], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=3, check=False)
        socket = subprocess.run(['systemctl', 'is-active', 'docker.socket'], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=3, check=False)
        return service.returncode == 0 and service.stdout.strip() == b'active' and socket.stdout.strip() == b'inactive'
    except (OSError, subprocess.SubprocessError):
        return False


class HbmInfo(ctypes.Structure):
    _fields_ = [('memory_size', ctypes.c_ulonglong), ('freq', ctypes.c_uint),
                ('memory_usage', ctypes.c_ulonglong), ('temp', ctypes.c_int),
                ('bandwidth', ctypes.c_uint)]


def sample_hbm():
    """Read device-wide driver percentages; failures remain unknown, never zero."""
    lib = ctypes.CDLL(DRIVER)
    lib.dcmiv2_init.argtypes = []
    lib.dcmiv2_init.restype = ctypes.c_int
    lib.dcmiv2_get_device_list.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    lib.dcmiv2_get_device_list.restype = ctypes.c_int
    lib.dcmiv2_get_device_hbm_info.argtypes = [ctypes.c_int, ctypes.POINTER(HbmInfo)]
    lib.dcmiv2_get_device_hbm_info.restype = ctypes.c_int
    if lib.dcmiv2_init() != 0:
        return None
    ids, count = (ctypes.c_int * 64)(), ctypes.c_int()
    if lib.dcmiv2_get_device_list(ids, ctypes.byref(count), 64) != 0 or not 0 < count.value <= 64:
        return None
    results = []
    for dev in list(ids)[:count.value]:
        started = time.time()
        value = HbmInfo()
        status = lib.dcmiv2_get_device_hbm_info(dev, ctypes.byref(value))
        ended = time.time()
        valid = status == 0 and 0 < value.memory_size and value.bandwidth <= 100
        results.append({'id': dev, 'started_at': started, 'finished_at': ended,
                        'hbm_bandwidth_percent': value.bandwidth if valid else None})
    return results


def source_predates(mtimes, started_at):
    return (len(mtimes) == len(SOURCES) and math.isfinite(started_at) and all(
        part.isascii() and part.isdigit() and int(part) < math.floor(started_at) for part in mtimes))


def driver_digest():
    if not 0 < os.stat(DRIVER).st_size <= 16 * 1024 * 1024:
        raise ValueError('driver size invalid')
    digest = hashlib.sha256()
    with open(DRIVER, 'rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def probe(container):
    if not CONTAINER.fullmatch(container):
        raise ValueError('invalid container')
    cpu_start = time.process_time()
    try:
        import resource
        child_start = resource.getrusage(resource.RUSAGE_CHILDREN)
    except ImportError:
        resource, child_start = None, None
    result = {'schema': 1, 'started_at': time.time(), 'source_id': None, 'host_fingerprint': None,
              'image_fingerprint': None, 'driver_sha256': None, 'source_started_at': None,
              'source_before_start': False, 'source_sha256': {},
              'engine_rows': [], 'hbm': None}
    identity = None
    identity_cmd = ['docker', 'inspect', '--format',
                    '{{.Id}} {{.Image}} {{.State.StartedAt}} {{.State.Running}}', container]
    try:
        identity = bounded_command(identity_cmd, 5, 1024).decode().strip()
        fields = identity.split()
        if len(fields) != 4 or fields[-1] != 'true':
            raise ValueError('container unavailable')
        machine_id = open('/etc/machine-id', encoding='ascii').read().strip()
        if not re.fullmatch(r'[0-9a-f]{32}', machine_id) or machine_id == '0' * 32:
            raise ValueError('host identity unavailable')
        result['host_fingerprint'] = hashlib.sha256(machine_id.encode()).hexdigest()
        result['image_fingerprint'] = hashlib.sha256(fields[1].encode()).hexdigest()
        result['source_id'] = hashlib.sha256(identity.encode()).hexdigest()
        result['source_started_at'] = datetime.fromisoformat(fields[2].replace('Z', '+00:00')).timestamp()
        for key, path in SOURCES.items():
            digest = bounded_command(['docker', 'exec', container, 'sha256sum', path], 5, 1024).decode().split()[0]
            if not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise ValueError('source unavailable')
            result['source_sha256'][key] = digest
        mtimes = bounded_command(['docker', 'exec', container, 'stat', '-L', '-c', '%Y', *SOURCES.values()],
                                 5, 1024).decode().split()
        result['source_before_start'] = source_predates(mtimes, result['source_started_at'])
        if not result['source_before_start']:
            raise ValueError('source changed after container start')
        raw = bounded_command(['docker', 'logs', '--timestamps', '--since', '120s', '--tail', '3000', container],
                              8, LOG_LIMIT)
        result['engine_rows'] = parse_log(raw)[-1024:]
    except (OSError, ValueError, TimeoutError, UnicodeError, IndexError, subprocess.SubprocessError):
        pass
    try:
        result['driver_sha256'] = driver_digest()
        result['hbm'] = sample_hbm()
        if driver_digest() != result['driver_sha256']:
            result['driver_sha256'], result['hbm'] = None, None
    except (OSError, ValueError, AttributeError):
        result['driver_sha256'], result['hbm'] = None, None
    if identity is not None:
        try:
            if bounded_command(identity_cmd, 5, 1024).decode().strip() != identity:
                raise ValueError('container changed')
        except (OSError, ValueError, TimeoutError, UnicodeError, subprocess.SubprocessError):
            result.update(source_id=None, host_fingerprint=None, image_fingerprint=None, driver_sha256=None,
                          source_before_start=False, source_sha256={}, engine_rows=[], hbm=None)
    result['finished_at'] = time.time()
    result['probe_cpu_seconds'] = time.process_time() - cpu_start
    if resource is not None:
        child_end = resource.getrusage(resource.RUSAGE_CHILDREN)
        result['probe_cpu_seconds'] += (child_end.ru_utime + child_end.ru_stime -
                                        child_start.ru_utime - child_start.ru_stime)
    return result


if __name__ == '__main__':
    if len(sys.argv) != 2 or not CONTAINER.fullmatch(sys.argv[1]):
        raise SystemExit(2)
    print(json.dumps(probe(sys.argv[1]), allow_nan=False, separators=(',', ':')))
