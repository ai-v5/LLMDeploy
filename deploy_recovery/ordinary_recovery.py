"""Recover one pinned ordinary model after a sampled idle window.

This observer never stops containers. It does not reserve devices against other
users; the final checks and Docker start are not atomic.
"""
import argparse
from datetime import datetime
import glob
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time

from . import deepinfer, device_holders
from .watchdog import write_state

POLICY = 1
MAX_IDLE_SAMPLE_GAP = deepinfer.IDLE_INTERVAL + 30
CONTROL_ACTIONS = frozenset(('start', 'restart', 'stop', 'kill', 'destroy'))
ELIGIBLE = frozenset(('WAITING_IDLE', 'BUSY', 'CONTROL_UNKNOWN',
                      'START_PENDING', 'START_REQUESTED', 'START_FAILED'))


def prepare(previous, config, boot_id, boot_epoch):
    """Reset samples on every observer start and retain only pinned intent."""
    valid = (isinstance(previous, dict) and previous.get('policy') == POLICY
             and previous.get('target_id') == config.get('container_id')
             and previous.get('trusted_fingerprint') == deepinfer.target_fingerprint(config)
             and isinstance(previous.get('boot_id'), str)
             and (previous.get('armed') is True or previous.get('state') == 'MANUAL_HOLD'))
    if not valid:
        return {'policy': POLICY, 'target_id': config.get('container_id'),
                'boot_id': boot_id, 'state': 'UNCLAIMED', 'armed': False,
                'samples': [], 'idle_since': None}
    state = dict(previous, boot_id=boot_id, samples=[], idle_since=None, failure_ratio=None)
    if previous['boot_id'] != boot_id:
        prior_phase = previous.get('previous_phase') if previous.get('state') == 'CONTROL_UNKNOWN' else previous.get('state')
        if prior_phase == 'RUNNING' and previous.get('armed') is True:
            state.update(state='BOOT_PENDING', boot_epoch=boot_epoch, event_cursor=boot_epoch,
                         reason='HOST_REBOOT', finished=None)
        else:
            state.update(state='MANUAL_HOLD', armed=False, reason='BOOT_INTENT_UNKNOWN')
    return state


def docker_command(config, args, timeout=20):
    """Avoid connecting through an active socket-activation unit."""
    return deepinfer.docker_command(config, args, timeout=timeout)


def inspect_target(config):
    return deepinfer.inspect_target(config, docker=docker_command)


def docker_events(config, since, until):
    return deepinfer.docker_events(config, since, until, docker=docker_command)


def start_target(config):
    return docker_command(config, ['start', config['container_id']], timeout=45)


def history_complete(config, previous, cursor, until):
    """Reject missing Docker event history before accepting a stopped target."""
    if previous.get('state') == 'BOOT_PENDING':
        # The old boot's monotonic clock is not comparable with this one.
        previous = dict(previous, last_check=time.monotonic())
    return deepinfer.control_history_complete(config, previous, cursor, until, docker=docker_command)


def device_paths(config):
    patterns = config.get('device_patterns')
    if not isinstance(patterns, list) or not patterns:
        raise ValueError('DEVICE_PATTERNS_REQUIRED')
    devices = []
    for item in patterns:
        if (not isinstance(item, dict) or set(item) != {'pattern', 'count'}
                or not isinstance(item['pattern'], str)
                or not item['pattern'].startswith('/dev/')
                or '..' in PurePosixPath(item['pattern']).parts
                or type(item['count']) is not int or not 0 < item['count'] <= 1024):
            raise ValueError('DEVICE_PATTERN_INVALID')
        matches = glob.glob(item['pattern'])
        if len(matches) != item['count']:
            raise ValueError('DEVICE_LAYOUT_CHANGED')
        devices.extend(matches)
    required = {'/dev/davinci' + str(i) for i in range(8)} | {'/dev/davinci_manager'}
    if len(set(devices)) != len(devices) or not required <= set(devices):
        raise ValueError('DEVICE_LAYOUT_INCOMPLETE')
    return devices


def load_config(path):
    config = deepinfer.load_deepinfer_config(
        path, extra_fields=('device_patterns', 'non_workload_processes'))
    pins = config.get('non_workload_processes', [])
    if not isinstance(pins, list):
        raise ValueError('INVALID_SYSTEM_PROCESSES')
    seen = set()
    for pin in pins:
        if (not isinstance(pin, dict)
                or set(pin) != {'service', 'exe_path', 'exe_sha256', 'cmdline_sha256', 'environ_sha256'}
                or not isinstance(pin['service'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\.service', pin['service'])
                or not isinstance(pin['exe_path'], str) or not pin['exe_path'].startswith('/')
                or '..' in PurePosixPath(pin['exe_path']).parts
                or not isinstance(pin['exe_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', pin['exe_sha256'])
                or not isinstance(pin['cmdline_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', pin['cmdline_sha256'])
                or not isinstance(pin['environ_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', pin['environ_sha256'])
                or pin['service'] in seen):
            raise ValueError('INVALID_SYSTEM_PROCESSES')
        seen.add(pin['service'])
    return config


def _systemd_main(service):
    args = ['systemctl', 'show', service]
    for field in ('LoadState', 'ActiveState', 'MainPID', 'ControlGroup'):
        args.extend(('-p', field))
    return dict(line.split('=', 1) for line in deepinfer.command(args).splitlines() if '=' in line)


def _process_identity(row):
    proc = Path('/proc') / str(row['pid'])
    ticks = device_holders._start_ticks((proc / 'stat').read_bytes())
    if ticks != row['start_ticks']:
        raise ValueError('PROCESS_GENERATION_CHANGED')
    exe_path = os.readlink(proc / 'exe')
    with (proc / 'exe').open('rb') as executable:
        before = os.fstat(executable.fileno())
        hasher = hashlib.sha256()
        for chunk in iter(lambda: executable.read(1024 * 1024), b''):
            hasher.update(chunk)
        digest = hasher.hexdigest()
        after = os.fstat(executable.fileno())
        current = os.stat(proc / 'exe')
    key = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if key(before) != key(after) or key(after) != key(current):
        raise ValueError('PROCESS_EXECUTABLE_CHANGED')
    cmdline = (proc / 'cmdline').read_bytes()
    environ = (proc / 'environ').read_bytes()
    cgroup = (proc / 'cgroup').read_text()
    status = (proc / 'status').read_text()
    uids = next(line for line in status.splitlines() if line.startswith('Uid:')).split()[1:]
    if (len(uids) != 4 or device_holders._start_ticks((proc / 'stat').read_bytes()) != ticks
            or os.readlink(proc / 'exe') != exe_path or (proc / 'cmdline').read_bytes() != cmdline
            or (proc / 'environ').read_bytes() != environ):
        raise ValueError('PROCESS_IDENTITY_CHANGED')
    return {'root_uid': all(uid == '0' for uid in uids), 'start_ticks': ticks,
            'exe_path': exe_path, 'exe_sha256': digest, 'cmdline_sha256': hashlib.sha256(cmdline).hexdigest(),
            'environ_sha256': hashlib.sha256(environ).hexdigest(),
            'cgroups': [line.split(':', 2)[2] for line in cgroup.splitlines() if line.count(':') >= 2]}


def system_process_matches(row, pin):
    """Exempt only one root service's exact, stable main process."""
    try:
        before = _systemd_main(pin['service'])
        if (before.get('LoadState') != 'loaded' or before.get('ActiveState') != 'active'
                or before.get('MainPID') != str(row['pid']) or not before.get('ControlGroup')):
            return False
        identity = _process_identity(row)
        after = _systemd_main(pin['service'])
        identity_after = _process_identity(row)
        return (before == after and identity == identity_after and identity['root_uid'] is True
                and identity['start_ticks'] == row['start_ticks']
                and identity['exe_path'] == pin['exe_path']
                and identity['exe_sha256'] == pin['exe_sha256']
                and identity['cmdline_sha256'] == pin['cmdline_sha256']
                and identity['environ_sha256'] == pin['environ_sha256']
                and before['ControlGroup'] in identity['cgroups'])
    except (OSError, ValueError, KeyError, IndexError, StopIteration, RuntimeError, subprocess.SubprocessError):
        return False


def machine_observation(config):
    """Require both the usual NPU/container sample and an empty holder scan."""
    observation = deepinfer.machine_observation(config, docker=docker_command)
    if observation != 'IDLE':
        return observation
    try:
        devices = device_paths(config)
        pins = config.get('non_workload_processes', [])
        def precheck(pid, ticks, name):
            row = {'pid': pid, 'start_ticks': ticks}
            return any(name == Path(pin['exe_path']).name.encode()
                       and system_process_matches(row, pin) for pin in pins)
        holders = device_holders.inventory_device_holders(devices, config['container_id'], precheck=precheck)
    except (device_holders.InventoryUnknown, OSError, ValueError):
        return 'UNKNOWN'
    return 'BUSY' if any(not holder.get('preverified')
                         or not any(system_process_matches(holder, pin) for pin in pins) for holder in holders) else 'IDLE'


def _hold(current, persist, reason):
    current.update(state='MANUAL_HOLD', reason=reason, armed=False,
                   samples=[], idle_since=None, failure_ratio=None)
    persist(current)
    return current


def _wait_unknown(current, persist, reason):
    current.update(state='WAITING_IDLE', observation='UNKNOWN', waiting_reason=reason,
                   samples=[], idle_since=None, failure_ratio=None)
    persist(current)
    return current


def _control_events(events, config, cursor, until, *, own_start_after=None):
    rows = events(config, cursor - 1, until)
    if not isinstance(rows, list):
        raise ValueError('CONTROL_EVENTS_UNKNOWN')
    own_starts = 0
    for row in rows:
        action, event_epoch = row.get('action'), row.get('epoch')
        if action not in CONTROL_ACTIONS:
            continue
        # The query deliberately overlaps by one second. A clock adjustment
        # can place an actual stop just before the previous wall-time cursor.
        if action == 'start' and isinstance(event_epoch, (int, float)) and event_epoch < cursor:
            continue
        if (action == 'start' and own_start_after is not None
                and isinstance(event_epoch, (int, float)) and event_epoch >= own_start_after):
            own_starts += 1
            if own_starts == 1:
                continue
        return True
    return False


def _same_stopped(state, reference):
    return (not state['Running'] and not state.get('Paused') and not state.get('Restarting')
            and state['Status'] == 'exited' and state['StartedAt'] == reference['StartedAt']
            and state['FinishedAt'] == reference['FinishedAt'])


def _valid_finished(value, now):
    """Reject malformed or future timestamps; ordering across boots is uncertain."""
    try:
        finished = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if finished.tzinfo is None:
            return False
        stamp = finished.timestamp()
        return 0 < stamp <= now
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return False


def _own_started_generation(previous, target, now):
    phase = previous.get('previous_phase') if previous.get('state') == 'CONTROL_UNKNOWN' else previous.get('state')
    if phase not in ('START_PENDING', 'START_REQUESTED', 'START_FAILED') or target.get('StartedAt') == previous.get('started'):
        return False
    requested = previous.get('request_epoch')
    if type(requested) not in (int, float):
        return False
    try:
        started = datetime.fromisoformat(target['StartedAt'].replace('Z', '+00:00'))
        return started.tzinfo is not None and requested - 1 <= started.timestamp() <= now + 1
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError):
        return False


def step(config, previous, *, inspect=inspect_target, events=docker_events,
         history_complete=history_complete, sample=machine_observation, start=None, persist=None,
         clock=time.monotonic, wall=time.time):
    """Observe one generation and optionally request start after two idle hours."""
    now, epoch = clock(), wall()
    current = dict(previous)
    current.pop('previous_phase', None)
    current.pop('waiting_reason', None)
    current.update(policy=POLICY, target_id=config.get('container_id'))
    try:
        target = inspect(config)
        own_generation = _own_started_generation(previous, target, epoch)
        own_start_after = previous['request_epoch'] - 1 if own_generation else None
        if target['Running'] and not target.get('Paused') and not target.get('Restarting'):
            same_generation = target['StartedAt'] == previous.get('started')
            if previous.get('state') == 'MANUAL_HOLD' and same_generation:
                return _hold(current, persist, previous.get('reason', 'MANUAL_HOLD'))
            if previous.get('armed') is True and same_generation:
                cursor = previous.get('event_cursor')
                if type(cursor) not in (int, float) or cursor > epoch:
                    return _hold(current, persist, 'CONTROL_CURSOR_UNKNOWN')
                complete = history_complete(config, previous, cursor, epoch)
                if _control_events(events, config, cursor, epoch):
                    return _hold(current, persist, 'OPERATOR_OR_CONTROL_ACTION')
                if not complete:
                    fresh = inspect(config)
                    if (not fresh['Running'] or fresh.get('Paused') or fresh.get('Restarting')
                            or fresh['StartedAt'] != target['StartedAt']
                            or _control_events(events, config, epoch - 1, wall())):
                        return _hold(current, persist, 'CONTROL_CHANGED_DURING_REBASE')
            if own_generation and _control_events(events, config, previous['event_cursor'], epoch,
                                                   own_start_after=own_start_after):
                return _hold(current, persist, 'OPERATOR_OR_CONTROL_ACTION')
            current.update(state='RUNNING', reason='TARGET_RUNNING', armed=True,
                           trusted_fingerprint=deepinfer.target_fingerprint(config),
                           started=target['StartedAt'], finished=target['FinishedAt'],
                           oom_seen=target.get('OOMKilled'),
                           event_cursor=epoch, last_check=now, samples=[], idle_since=None)
            persist(current)
            return current
        if target['Running'] or target.get('Paused') or target.get('Restarting'):
            return _hold(current, persist, 'TARGET_TRANSITION_UNKNOWN')
        if target.get('Status') != 'exited':
            return _hold(current, persist, 'TARGET_STATE_UNKNOWN')
        old_state = previous.get('state')
        if old_state == 'CONTROL_UNKNOWN':
            old_state = previous.get('previous_phase', 'CONTROL_UNKNOWN')
        if old_state not in ELIGIBLE | {'RUNNING', 'BOOT_PENDING'} or previous.get('armed') is not True:
            return _hold(current, persist, previous.get('reason', 'NO_TRUSTED_RUNNING_INTENT'))
        cursor = previous.get('event_cursor')
        if type(cursor) not in (int, float) or cursor > epoch:
            return _hold(current, persist, 'CONTROL_CURSOR_UNKNOWN')
        history_previous = dict(previous, state=old_state)
        complete = history_complete(config, history_previous, cursor, epoch)
        if _control_events(events, config, cursor, epoch, own_start_after=own_start_after):
            return _hold(current, persist, 'OPERATOR_OR_CONTROL_ACTION')
        if not complete:
            fresh = inspect(config)
            if (not _same_stopped(fresh, target)
                    or _control_events(events, config, epoch - 1, wall(), own_start_after=own_start_after)):
                return _hold(current, persist, 'CONTROL_CHANGED_DURING_REBASE')
            previous = dict(previous, samples=[], idle_since=None, failure_ratio=None)
        if target['StartedAt'] != previous.get('started') and not own_generation:
            return _hold(current, persist, 'TARGET_GENERATION_CHANGED')
        if old_state == 'RUNNING' or own_generation:
            # A nonzero code is a best-effort signal, not proof of intent.
            code = target.get('ExitCode')
            if type(code) is not int or code <= 0:
                return _hold(current, persist, 'EXIT_CAUSE_UNKNOWN')
            reason = 'ABNORMAL_EXIT'
        elif old_state == 'BOOT_PENDING':
            if not _valid_finished(target['FinishedAt'], epoch):
                return _hold(current, persist, 'BOOT_EXIT_TIME_UNKNOWN')
            reason = 'HOST_REBOOT'
        else:
            if target['FinishedAt'] != previous.get('finished'):
                return _hold(current, persist, 'TARGET_GENERATION_CHANGED')
            reason = previous.get('reason')
            if reason not in ('ABNORMAL_EXIT', 'HOST_REBOOT'):
                return _hold(current, persist, 'EXIT_CAUSE_UNKNOWN')
        observation = sample(config)
        sampled_at = clock()
        samples = previous.get('samples', [])
        if samples and sampled_at - samples[-1]['time'] > MAX_IDLE_SAMPLE_GAP:
            previous = dict(previous, samples=[], idle_since=None, failure_ratio=None)
        window, ready = deepinfer.sample_window(previous, sampled_at, observation)
        current.update(window)
        current.update(state='BUSY' if observation == 'BUSY' else 'WAITING_IDLE',
                       reason=reason, observation=observation, armed=True,
                       started=target['StartedAt'], finished=target['FinishedAt'],
                       event_cursor=epoch, last_check=now)
        if observation != 'IDLE':
            current['waiting_reason'] = 'MACHINE_BUSY' if observation == 'BUSY' else 'OBSERVATION_UNKNOWN'
        if ready:
            final = inspect(config)
            if not _same_stopped(final, target) or _control_events(events, config, cursor, wall()):
                return _hold(current, persist, 'FINAL_CONTROL_CHANGED')
            if not history_complete(config, previous, cursor, wall()):
                return _wait_unknown(current, persist, 'CONTROL_HISTORY_INCOMPLETE')
            final_observation = sample(config)
            final = inspect(config)
            if not _same_stopped(final, target) or _control_events(events, config, cursor, wall()):
                return _hold(current, persist, 'FINAL_CONTROL_CHANGED')
            if not history_complete(config, previous, cursor, wall()):
                return _wait_unknown(current, persist, 'CONTROL_HISTORY_INCOMPLETE')
            if final_observation != 'IDLE':
                current.update(state='BUSY' if final_observation == 'BUSY' else 'WAITING_IDLE',
                               observation=final_observation, samples=[], idle_since=None,
                               waiting_reason='MACHINE_BUSY' if final_observation == 'BUSY' else 'OBSERVATION_UNKNOWN')
            else:
                current.update(state='START_PENDING', samples=[], idle_since=None, request_epoch=wall())
                persist(current)
                try:
                    start(config)
                    current['state'] = 'START_REQUESTED'
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    current['state'] = 'START_FAILED'
        persist(current)
        return current
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        current.update(state='CONTROL_UNKNOWN', samples=[], idle_since=None,
                       waiting_reason='CONTROL_UNAVAILABLE',
                       previous_phase=(previous.get('previous_phase') if previous.get('state') == 'CONTROL_UNKNOWN'
                                       else previous.get('state')),
                       reason=previous.get('reason'), armed=previous.get('armed') is True)
        persist(current)
        return current


def main():
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    device_paths(config)
    path = Path(config['state_file'])
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('RECOVERY_ALREADY_RUNNING')
        try:
            previous = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError, AttributeError):
            previous = {}
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        previous = prepare(previous, config, boot_id, deepinfer.linux_boot_epoch())
        while True:
            began = time.monotonic()
            previous = step(config, previous, persist=lambda s: write_state(path, s),
                            start=start_target)
            elapsed = time.monotonic() - began
            interval = deepinfer.next_interval(previous)
            time.sleep(interval - elapsed if elapsed < interval else interval)


if __name__ == '__main__':
    main()
