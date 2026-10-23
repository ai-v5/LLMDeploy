"""No stop/kill operations; inspection failures always reset the idle window."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

INTERVAL = 600
IDLE_SECONDS = 3600
MAX_GAP = 900


def npu_idle(payload):
    cards, covered, pending, processes = {}, set(), None, None
    for line in payload.splitlines():
        if 'NPU ID' in line and 'Process id' in line:
            if processes is not None:
                raise ValueError('DUPLICATE_PROCESS_SECTION')
            processes, pending = 0, None
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if processes is not None:
            empty = re.search(r'No running processes found in NPU\s+(\d+)', line, re.I)
            if empty:
                covered.add(int(empty[1]))
            if len(cells) >= 3 and cells[0].isdigit():
                if not cells[1].isdigit():
                    raise ValueError('INVALID_PROCESS_ROW')
                covered.add(int(cells[0]))
                processes += 1
            continue
        if len(cells) >= 4 and cells[0].isdigit() and cells[1] == 'Ascend950DT':
            index = int(cells[0])
            if index in cards:
                raise ValueError('DUPLICATE_CARD')
            cards[index] = {'health': cells[2], 'util': None}
            pending = index
        elif pending is not None and len(cells) >= 4 and not cells[0]:
            fields = cells[3].split()
            if not fields:
                raise ValueError('MISSING_UTILIZATION')
            util = float(fields[0])
            if not math.isfinite(util) or not 0 <= util <= 100:
                raise ValueError('INVALID_UTILIZATION')
            cards[pending]['util'] = util
            pending = None
    if set(cards) != set(range(8)) or covered != set(range(8)) or processes is None:
        raise ValueError('INCOMPLETE_NPU_SNAPSHOT')
    if any(c['util'] is None or c['health'] not in ('OK', 'Alarm') for c in cards.values()):
        raise ValueError('UNHEALTHY_OR_UNKNOWN_CARD')
    return processes == 0 and all(c['util'] == 0 for c in cards.values())


def advance(previous, now, boot_id, observation):
    """Seven idle observations spanning at least one hour, with no long gap."""
    current = {'version': 1, 'boot_id': boot_id, 'last_check': now,
               'idle_since': None, 'idle_checks': 0, 'state': observation}
    if observation != 'IDLE':
        return current, False
    valid = (previous.get('version') == 1 and previous.get('boot_id') == boot_id
             and isinstance(previous.get('last_check'), (int, float))
             and 0 < now - previous['last_check'] <= MAX_GAP
             and isinstance(previous.get('idle_since'), (int, float))
             and 0 <= previous['idle_since'] <= previous['last_check']
             and type(previous.get('idle_checks')) is int and previous['idle_checks'] > 0)
    current['idle_since'] = previous['idle_since'] if valid else now
    current['idle_checks'] = previous['idle_checks'] + 1 if valid else 1
    ready = current['idle_checks'] >= 7 and now - current['idle_since'] >= IDLE_SECONDS
    current['state'] = 'START_PENDING' if ready else 'WAITING_IDLE'
    return current, ready


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('COMMAND_FAILED')
    return result.stdout


def inspect(config):
    data = json.loads(command([config['docker'], 'inspect', '--type', 'container', config['container_id']]))
    if len(data) != 1:
        raise ValueError('UNEXPECTED_CONTAINER')
    data = data[0]
    fingerprint = hashlib.sha256(json.dumps({'config': data['Config'], 'hostconfig': data['HostConfig']}, sort_keys=True).encode()).hexdigest()
    if (data['Id'] != config['container_id'] or data['Name'] != '/' + config['container_name']
            or data['Image'] != config['image_id'] or fingerprint != config['spec_sha256']):
        raise ValueError('TARGET_CHANGED')
    state = data['State']
    if state['Running'] or state.get('Restarting') or state.get('Paused'):
        return 'RUNNING'
    if state['Status'] not in ('created', 'exited', 'dead'):
        raise ValueError('UNKNOWN_CONTAINER_STATE')
    return 'STOPPED'


def probe(config):
    if inspect(config) == 'RUNNING':
        return 'RUNNING'
    return 'IDLE' if npu_idle(command([config['npu_smi'], 'info'])) else 'BUSY'


def write_state(path, state):
    temp = path.with_suffix('.new')
    with temp.open('w', encoding='utf-8') as handle:
        json.dump(state, handle, separators=(',', ':'))
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def check(config, previous, now, boot_id, *, observe=probe, start=None, persist=None, clock=None):
    """Persist the reset before starting, and recheck the target and devices."""
    try:
        observation = observe(config)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
        observation = 'UNKNOWN'
    now = clock() if clock is not None else now
    current, ready = advance(previous, now, boot_id, observation)
    if ready:
        try:
            observation = observe(config)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
            observation = 'UNKNOWN'
        now = clock() if clock is not None else now
        current['last_check'] = now
        if observation != 'IDLE':
            current, ready = advance({}, now, boot_id, observation)
    if ready:
        current.update(idle_since=None, idle_checks=0, state='START_PENDING')
    persist(current)
    if ready:
        try:
            start(config)
            current['state'] = 'START_REQUESTED'
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            current['state'] = 'START_FAILED'
        persist(current)
    return current


def load_config(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if set(value) != {'container_id', 'container_name', 'image_id', 'spec_sha256', 'docker', 'npu_smi', 'state_file'}:
        raise ValueError('INVALID_CONFIG')
    if not re.fullmatch('[0-9a-f]{64}', value['container_id']) or not re.fullmatch('sha256:[0-9a-f]{64}', value['image_id']):
        raise ValueError('INVALID_TARGET')
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value['container_name']) or not re.fullmatch('[0-9a-f]{64}', value['spec_sha256']):
        raise ValueError('INVALID_TARGET')
    paths = [path, Path(value['state_file']), Path(value['state_file']).with_suffix('.new'), Path(value['state_file']).with_suffix('.lock')]
    if len({p.resolve() for p in paths}) != len(paths) or any(not Path(value[k]).is_absolute() for k in ('docker', 'npu_smi', 'state_file')):
        raise ValueError('INVALID_PATHS')
    return value


def main():
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    path = Path(config['state_file'])
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            previous = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(previous, dict):
                previous = {}
        except (OSError, ValueError):
            previous = {}
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        state = check(config, previous, None, boot, clock=time.monotonic,
                      start=lambda c: command([c['docker'], 'start', c['container_id']]),
                      persist=lambda s: write_state(path, s))
        print(json.dumps({'state': state['state'], 'idle_checks': state['idle_checks']}))


if __name__ == '__main__':
    main()
