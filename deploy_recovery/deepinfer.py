"""Recover a pinned DeepInfer container after two hours of observed idleness."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time

from .watchdog import load_config, npu_idle, write_state

INTERVAL = 60
IDLE_INTERVAL = 120
BUSY_INTERVAL = 1200
WINDOW = 7200
POLICY_VERSION = 4
MAX_SAMPLES = WINDOW // INTERVAL + 1


def next_interval(state):
    """Keep failure detection prompt; slow down only a stopped target's checks."""
    name = state.get('state')
    if name in ('BUSY', 'CONTROL_UNKNOWN'):
        return BUSY_INTERVAL
    if name in ('WAITING_IDLE', 'START_FAILED'):
        return IDLE_INTERVAL if state.get('observation') == 'IDLE' else BUSY_INTERVAL
    return INTERVAL


def sample_window(previous, now, observation):
    """Require two hours of successfully observed idle samples."""
    if observation != 'IDLE':
        return {'samples': [], 'idle_since': None, 'failure_ratio': None}, False
    samples = list(previous.get('samples', []))
    if samples:
        gap = now - samples[-1]['time']
        if gap <= 0 or gap >= IDLE_INTERVAL * 2 or any(s['value'] != 'IDLE' for s in samples):
            samples = []
    samples.append({'time': now, 'value': observation})
    # Only retained samples prove the elapsed time; a corrupt or stale
    # idle_since must not make a shorter observed window ready.
    samples = samples[-MAX_SAMPLES:]
    since = samples[0]['time']
    ready = now - since >= WINDOW and len(samples) >= WINDOW // IDLE_INTERVAL
    return {'samples': samples, 'idle_since': since, 'failure_ratio': 0.0}, ready


def command(args, timeout=20):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('COMMAND_FAILED')
    return result.stdout


def docker_command(config, args, timeout=20):
    """Do not connect to an inactive or socket-activated Docker daemon."""
    def unit(name):
        text = command(['systemctl', 'show', name, '-p', 'LoadState', '-p', 'ActiveState'])
        return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    service, socket = unit('docker.service'), unit('docker.socket')
    if (service.get('LoadState') != 'loaded' or service.get('ActiveState') != 'active'
            or socket.get('LoadState') not in ('loaded', 'not-found')
            or socket.get('ActiveState') not in ('inactive', 'failed')):
        raise RuntimeError('DOCKER_UNAVAILABLE_OR_SOCKET_ACTIVATED')
    return command([config['docker'], *args], timeout=timeout)


def target_fingerprint(config):
    fields = ('container_id', 'container_name', 'image_id', 'spec_sha256')
    data = {key: config.get(key) for key in fields}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inspect_target(config):
    rows = json.loads(docker_command(config, ['inspect', '--type', 'container', config['container_id']]))
    if len(rows) != 1:
        raise ValueError('TARGET_CHANGED')
    row = rows[0]
    digest = hashlib.sha256(json.dumps({'config': row['Config'], 'hostconfig': row['HostConfig']}, sort_keys=True).encode()).hexdigest()
    if (row['Id'] != config['container_id'] or row['Name'] != '/' + config['container_name']
            or row['Image'] != config['image_id'] or digest != config['spec_sha256']
            or row['HostConfig']['RestartPolicy']['Name'] != 'no'):
        raise ValueError('TARGET_CHANGED')
    return row['State']


def docker_events(config, since, until):
    text = docker_command(config, ['events', '--since', str(since), '--until', str(until),
                                   '--filter', 'container=' + config['container_id'], '--format', '{{json .}}'])
    events = []
    for line in text.splitlines():
        e = json.loads(line)
        events.append({'action': e.get('Action'), 'epoch': e.get('timeNano', e.get('time', 0) * 10**9) / 10**9})
    return events


def npu_observation(config):
    try:
        return 'IDLE' if npu_idle(command([config['npu_smi'], 'info'])) else 'BUSY'
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return 'UNKNOWN'


def load_deepinfer_config(path):
    config = load_config(path, optional_fields=('non_workload_containers',))
    exemptions = config.get('non_workload_containers', [])
    if not isinstance(exemptions, list):
        raise ValueError('INVALID_INFRASTRUCTURE')
    seen = set()
    for row in exemptions:
        if (not isinstance(row, dict) or set(row) != {'container_id', 'image_id'}
                or not isinstance(row['container_id'], str)
                or not re.fullmatch('[0-9a-f]{64}', row['container_id'])
                or not isinstance(row['image_id'], str)
                or not re.fullmatch('sha256:[0-9a-f]{64}', row['image_id'])
                or row['container_id'] == config['container_id'] or row['container_id'] in seen):
            raise ValueError('INVALID_INFRASTRUCTURE')
        seen.add(row['container_id'])
    return config


def machine_observation(config):
    """Do not share a machine with another running workload container."""
    observation = npu_observation(config)
    if observation != 'IDLE':
        return observation
    try:
        allowed = {row['container_id']: row['image_id'] for row in config.get('non_workload_containers', [])}
        running = docker_command(config, ['ps', '--no-trunc', '--format', '{{.ID}}']).splitlines()
        for container_id in running:
            if not re.fullmatch('[0-9a-f]{64}', container_id):
                return 'UNKNOWN'
            if container_id == config['container_id']:
                continue
            if container_id not in allowed:
                return 'BUSY'
            image = docker_command(config, ['inspect', '--type', 'container', '--format',
                                            '{{.Image}}', container_id]).strip()
            if image != allowed[container_id]:
                return 'BUSY'
        return 'IDLE'
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        return 'UNKNOWN'


def control_history_complete(config, previous, cursor, until):
    """Accept an observer gap only when the Docker event history is bounded."""
    try:
        last_check = previous['last_check']
        if type(last_check) not in (int, float) or not math.isfinite(last_check):
            return False
        def daemon_start():
            text = command(['systemctl', 'show', 'docker.service', '-p', 'ActiveState',
                            '-p', 'ExecMainStartTimestampMonotonic'])
            values = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
            if values['ActiveState'] != 'active':
                raise ValueError('DOCKER_NOT_ACTIVE')
            return int(values['ExecMainStartTimestampMonotonic']) / 1_000_000
        started = daemon_start()
        if started <= 0 or started > last_check:
            return False
        output = docker_command(config, ['events', '--since', str(cursor - 1), '--until', str(until),
                                         '--format', '{{.TimeNano}}']).splitlines()
        if len(output) >= 256 or not all(re.fullmatch(r'\d+', line) for line in output):
            return False
        return daemon_start() == started
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        return False


def rearm_stopped(config, previous, boot_id, *, inspect=inspect_target, events=docker_events,
                  clock=time.monotonic, wall=time.time):
    """Rearm one pinned stopped target after an explicit operator repair."""
    if (not isinstance(previous, dict) or previous.get('version') != 1
            or previous.get('target_id') != config['container_id']
            or previous.get('boot_id') != boot_id or not isinstance(previous.get('manual_hold'), bool)):
        raise ValueError('REARM_REQUIRES_VERIFIED_STATE')
    state = inspect(config)
    if (state.get('Running') or state.get('Paused') or state.get('Restarting')
            or state.get('Status') != 'exited' or state.get('StartedAt') != previous.get('started')):
        raise ValueError('REARM_TARGET_CHANGED')
    cursor = previous.get('event_cursor')
    now = wall()
    if not isinstance(cursor, (int, float)) or not math.isfinite(cursor) or cursor > now:
        raise ValueError('REARM_UNKNOWN_CONTROL_HISTORY')
    if any(e['action'] in ('start', 'stop', 'kill', 'destroy', 'restart')
           for e in events(config, cursor - 1, now)):
        raise ValueError('REARM_CONTROL_CHANGED')
    current = dict(previous)
    current.update(state='REARMED', armed=True, manual_hold=False, event_cursor=now,
                   epoch=now, last_check=clock(), samples=[], idle_since=None, failure_ratio=None,
                   boot_recovery_pending=False, boot_recovery=False, boot_finished_at=None,
                   rearm_finished_at=state['FinishedAt'], policy_version=POLICY_VERSION,
                   trusted_fingerprint=target_fingerprint(config))
    return current


def valid_idle_window(previous):
    samples = previous.get('samples')
    since, last = previous.get('idle_since'), previous.get('last_check')
    if (not isinstance(samples, list) or not 0 < len(samples) <= MAX_SAMPLES
            or type(since) not in (int, float) or not math.isfinite(since)
            or type(last) not in (int, float) or not math.isfinite(last)):
        return False
    times = []
    for sample in samples:
        if (not isinstance(sample, dict) or sample.get('value') != 'IDLE'
                or type(sample.get('time')) not in (int, float) or not math.isfinite(sample['time'])):
            return False
        times.append(sample['time'])
    # last_check is captured before the potentially slow NPU/Docker probes;
    # the successful sample timestamp can therefore be later than last_check.
    return (0 <= since <= times[0] <= times[-1] and 0 <= last <= times[-1]
            and all(0 < b - a < IDLE_INTERVAL * 2 for a, b in zip(times, times[1:])))


def prepare_previous(previous, config, boot_id, boot_epoch):
    """Recover prior intent without trusting unverified control history."""
    valid = (isinstance(previous, dict) and previous.get('version') == 1
             and previous.get('target_id') == config['container_id']
             and isinstance(previous.get('boot_id'), str)
             and previous.get('policy_version') in (None, 2, 3, POLICY_VERSION))
    current = dict(previous) if valid else {}
    new_boot = valid and current['boot_id'] != boot_id
    cursor = current.get('event_cursor')
    last = current.get('last_check')
    valid_states = ('RUNNING', 'WAITING_IDLE', 'BUSY', 'CONTROL_UNKNOWN', 'MANUAL_HOLD',
                    'REARMED', 'START_PENDING', 'START_REQUESTED', 'START_FAILED')
    trusted = (valid and isinstance(current.get('manual_hold'), bool)
               and isinstance(current.get('armed'), bool) and current.get('state') in valid_states
               and type(cursor) in (int, float) and math.isfinite(cursor)
               and type(last) in (int, float) and math.isfinite(last) and last >= 0
               and current.get('policy_version') == POLICY_VERSION
               and current.get('trusted_fingerprint') == target_fingerprint(config))
    current.pop('legacy_hold_pending', None)
    if not trusted:
        # Legacy stopped states cannot prove that the prior watcher ever
        # observed this exact container identity running.
        current.update(armed=False, trusted_fingerprint=None)
    # A host reboot starts a new recovery window. Missing or corrupt state
    # must not silently claim an existing stopped container.
    can_resume = (current.get('armed') is True or
                  (current.get('state') == 'CONTROL_UNKNOWN' and current.get('boot_recovery_pending') is True))
    if new_boot and trusted and can_resume:
        current.update(boot_recovery_pending=True, boot_recovery=False, boot_finished_at=None,
                       boot_epoch=boot_epoch, event_cursor=boot_epoch, armed=False, manual_hold=False)
        current.pop('last_check', None)
    elif new_boot:
        current.update(boot_recovery_pending=False, boot_recovery=False, boot_finished_at=None,
                       event_cursor=boot_epoch, armed=False)
        current.pop('last_check', None)
    same_policy_boot = valid and not new_boot and trusted
    preserve_idle = (same_policy_boot and current.get('state') == 'WAITING_IDLE'
                     and current.get('observation') == 'IDLE' and current.get('armed') is True
                     and current.get('manual_hold') is False and valid_idle_window(current))
    if not preserve_idle:
        current.update(samples=[], idle_since=None, failure_ratio=None)
    if same_policy_boot and current.get('armed') is True and current.get('manual_hold') is False:
        # A quick process restart can still hide a Docker daemon restart or a
        # lost operator stop, even when the sampling gap is under two minutes.
        current['restart_history_pending'] = True
    else:
        current.pop('restart_history_pending', None)
    current.update(boot_id=boot_id, policy_version=POLICY_VERSION)
    return current


def restart_delay(previous, config, boot_id, now):
    """Keep the last stopped-target check's deadline after an observer restart."""
    if (not isinstance(previous, dict) or previous.get('version') != 1
            or previous.get('target_id') != config.get('container_id')
            or previous.get('boot_id') != boot_id or previous.get('policy_version') not in (2, 3, POLICY_VERSION)
            or previous.get('armed') is not True or previous.get('manual_hold') is not False
            or previous.get('state') not in ('BUSY', 'WAITING_IDLE', 'START_FAILED', 'CONTROL_UNKNOWN')):
        return 0
    last = previous.get('last_attempt', previous.get('last_check'))
    if (not isinstance(last, (int, float)) or not math.isfinite(last)
            or not isinstance(now, (int, float)) or not math.isfinite(now) or now < last):
        return 0
    return max(0, next_interval(previous) - (now - last))


def linux_boot_epoch():
    for line in Path('/proc/stat').read_text().splitlines():
        if line.startswith('btime '):
            return int(line.split()[1])
    raise ValueError('UNKNOWN_BOOT_TIME')


def step(config, previous, *, inspect=inspect_target, events=docker_events,
         sample=machine_observation, start=None, persist=None, clock=time.monotonic, wall=time.time):
    """Single synchronous sample; never stop or kill a container or workload."""
    now = clock()
    current = dict(previous)
    current.pop('restart_history_pending', None)
    current.update(version=1, epoch=wall(), last_check=now, last_attempt=now, target_id=config.get('container_id'))
    try:
        state = inspect(config)
        pending_boot = previous.get('boot_recovery_pending') is True
        if (not pending_boot and previous.get('armed') is True and not state['Running']
                and ('event_cursor' not in previous or type(previous.get('last_check')) not in (int, float)
                     or not math.isfinite(previous['last_check']) or not 0 <= previous['last_check'] <= now)):
            # A stopped target cannot inherit a window when its control
            # history's starting point is missing from the saved state.
            raise ValueError('MISSING_EVENT_CURSOR')
        cursor = previous.get('boot_epoch') if pending_boot else previous.get('event_cursor', current['epoch'] - 1)
        if not isinstance(cursor, (int, float)) or not math.isfinite(cursor) or cursor > current['epoch']:
            raise ValueError('UNKNOWN_EVENT_CURSOR')
        changes = events(config, cursor if pending_boot else cursor - 1, current['epoch'])
        # Longer planned waits still need proof that Docker control history is intact.
        elapsed = now - previous.get('last_check', now)
        gap = not pending_boot and bool(previous) and previous.get('armed') is True
        missed_check = elapsed > next_interval(previous) * 2
        destroyed = any(e['action'] == 'destroy' for e in changes)
        trusted = previous.get('trusted_fingerprint') == target_fingerprint(config)
        manual = previous.get('manual_hold', False) or destroyed
        if state['Running'] and not state.get('Paused') and not state.get('Restarting') and not destroyed:
            trusted = True
            manual = False
        current.update(event_cursor=current['epoch'], started=state['StartedAt'],
                       finished=state['FinishedAt'], manual_hold=manual,
                       trusted_fingerprint=target_fingerprint(config) if trusted and not destroyed else None)
        if state['Running'] or state.get('Paused') or state.get('Restarting'):
            current.update(state='RUNNING', armed=trusted and not manual, boot_recovery_pending=False,
                           boot_recovery=False, boot_finished_at=None,
                           rearm_finished_at=None,
                           samples=[], idle_since=None, failure_ratio=None)
            persist(current)
            return current
        if state['Status'] != 'exited':
            raise ValueError('UNEXPECTED_CONTAINER_STATE')
        armed = previous.get('armed') is True and trusted
        boot_recovery = previous.get('boot_recovery') is True
        if pending_boot:
            current['boot_recovery_pending'] = False
            if manual or not trusted:
                current.update(state='MANUAL_HOLD', armed=False, boot_recovery=False,
                               samples=[], idle_since=None, failure_ratio=None)
                persist(current)
                return current
            armed, boot_recovery = True, True
            current.update(armed=True, boot_recovery=True, boot_finished_at=state['FinishedAt'])
        # A new container run, a changed finish time, or an old state without a
        # recorded finish invalidates any accumulated idle samples. Stop/kill
        # events themselves no longer cancel the authorized recovery policy.
        target_changed = not pending_boot and (state['StartedAt'] != previous.get('started')
                          or (previous.get('finished') is not None
                              and state['FinishedAt'] != previous['finished'])
                          or (previous.get('samples') and previous.get('finished') is None)
                          or (boot_recovery and state['FinishedAt'] != previous.get('boot_finished_at'))
                          or (previous.get('rearm_finished_at') is not None
                              and state['FinishedAt'] != previous['rearm_finished_at'])
                          or any(e['action'] in ('start', 'restart')
                                 or (e['action'] in ('stop', 'kill') and e.get('epoch', cursor) >= cursor)
                                 for e in changes))
        if target_changed:
            previous = dict(previous, samples=[], idle_since=None, failure_ratio=None)
            current.update(boot_recovery=False, boot_finished_at=None, rearm_finished_at=None)
        if gap and armed and not manual:
            complete = control_history_complete(config, previous, cursor, current['epoch'])
            if not complete:
                # No old sample survives an unverified gap. Establish the new
                # baseline only while the exact stopped target remains stable.
                fresh = inspect(config)
                recent = events(config, current['epoch'] - 1, wall())
                if (fresh['Running'] or fresh.get('Paused') or fresh.get('Restarting')
                        or fresh['Status'] != 'exited' or fresh['StartedAt'] != state['StartedAt']
                        or fresh['FinishedAt'] != state['FinishedAt']
                        or any(e['action'] in ('start', 'stop', 'kill', 'restart', 'destroy') for e in recent)):
                    raise ValueError('CONTROL_CHANGED_DURING_REBASE')
            if not complete or missed_check:
                previous = dict(previous, samples=[], idle_since=None, failure_ratio=None)
            gap = False
        if manual or gap or not armed:
            current.update(state='MANUAL_HOLD', armed=False, samples=[], idle_since=None, failure_ratio=None)
            persist(current)
            return current
        observation = sample(config)
        window, ready = sample_window(previous, clock(), observation)
        current.update(window)
        current.update(state='BUSY' if observation == 'BUSY' else 'WAITING_IDLE', observation=observation)
        if ready:
            # Re-read control history and identity before the final fresh NPU probe.
            final_state = inspect(config)
            final_events = events(config, cursor - 1, wall())
            if (final_state['Running'] or final_state.get('Paused') or final_state.get('Restarting')
                    or final_state['StartedAt'] != state['StartedAt']
                    or final_state['FinishedAt'] != state['FinishedAt']
                    or any(e['action'] in ('kill', 'stop', 'start', 'restart', 'destroy') for e in final_events)):
                destroyed = any(e['action'] == 'destroy' for e in final_events)
                current.update(state='MANUAL_HOLD' if destroyed else 'WAITING_IDLE',
                               armed=not destroyed, manual_hold=destroyed, observation='UNKNOWN',
                               samples=[], idle_since=None, failure_ratio=None)
            else:
                final_sample = sample(config)
                if final_sample == 'IDLE':
                    # A slow NPU call must not hide an operator action during it.
                    after_probe = inspect(config)
                    after_events = events(config, cursor - 1, wall())
                    if (after_probe['Running'] or after_probe.get('Paused') or after_probe.get('Restarting')
                            or after_probe['StartedAt'] != state['StartedAt']
                            or after_probe['FinishedAt'] != state['FinishedAt']
                            or any(e['action'] in ('kill', 'stop', 'start', 'restart', 'destroy') for e in after_events)):
                        destroyed = any(e['action'] == 'destroy' for e in after_events)
                        current.update(state='MANUAL_HOLD' if destroyed else 'WAITING_IDLE',
                                       armed=not destroyed, manual_hold=destroyed, observation='UNKNOWN',
                                       samples=[], idle_since=None, failure_ratio=None)
                        persist(current)
                        return current
                    # Reset durably before requesting start; failure waits a new window.
                    current.update(state='START_PENDING', samples=[], idle_since=None, failure_ratio=None)
                    persist(current)
                    try:
                        start(config)
                        current['state'] = 'START_REQUESTED'
                    except (OSError, RuntimeError, subprocess.SubprocessError):
                        current['state'] = 'START_FAILED'
                elif final_sample == 'BUSY':
                    current.update(state='BUSY', samples=[], idle_since=None, failure_ratio=None)
                else:
                    current.update(samples=[], idle_since=None, failure_ratio=None, observation='UNKNOWN')
        persist(current)
        return current
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        current.update(state='CONTROL_UNKNOWN', armed=(previous.get('armed') is True
                       and previous.get('trusted_fingerprint') == target_fingerprint(config)),
                       samples=[], idle_since=None, failure_ratio=None)
        if previous.get('restart_history_pending') is True:
            current['restart_history_pending'] = True
        if 'event_cursor' in previous:
            current['event_cursor'] = previous['event_cursor']
        if 'last_check' in previous:
            current['last_check'] = previous['last_check']
        persist(current)
        return current


def main():
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--rearm-stopped', action='store_true')
    args = parser.parse_args()
    config = load_deepinfer_config(args.config)
    path = Path(config['state_file'])
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('RECOVERY_ALREADY_RUNNING')
        # Same-boot observer restarts retain only a validated candidate window;
        # the next stopped-target check must verify control history first.
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        try:
            previous = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError, AttributeError):
            previous = {}
        if args.rearm_stopped:
            write_state(path, rearm_stopped(config, previous, boot))
            return
        delay = restart_delay(previous, config, boot, time.monotonic())
        previous = prepare_previous(previous, config, boot, linux_boot_epoch())
        if delay:
            time.sleep(delay)
        while True:
            began = time.monotonic()
            previous = step(config, previous, persist=lambda s: write_state(path, s),
                            start=lambda c: docker_command(c, ['start', c['container_id']], timeout=45))
            elapsed = time.monotonic() - began
            # No overlapping requests, no catch-up burst after a slow cycle.
            interval = next_interval(previous)
            time.sleep(interval - elapsed if elapsed < interval else interval)


if __name__ == '__main__':
    main()
