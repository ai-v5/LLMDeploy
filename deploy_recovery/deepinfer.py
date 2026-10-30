"""Recover a pinned DeepInfer container after two hours of observed idleness."""
import argparse
from datetime import datetime
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
POLICY_VERSION = 2


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
    since = previous.get('idle_since')
    if samples:
        gap = now - samples[-1]['time']
        if gap <= 0 or gap >= IDLE_INTERVAL * 2 or any(s['value'] != 'IDLE' for s in samples):
            samples, since = [], None
    samples.append({'time': now, 'value': observation})
    if since is None and observation == 'IDLE':
        since = now
    samples = [s for s in samples if s['time'] >= now - WINDOW]
    ready = (since is not None and now - since >= WINDOW
             and len(samples) >= WINDOW // IDLE_INTERVAL)
    return {'samples': samples, 'idle_since': since, 'failure_ratio': 0.0}, ready


def command(args, timeout=20):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('COMMAND_FAILED')
    return result.stdout


def inspect_target(config):
    rows = json.loads(command([config['docker'], 'inspect', '--type', 'container', config['container_id']]))
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
    text = command([config['docker'], 'events', '--since', str(since), '--until', str(until),
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
        running = command([config['docker'], 'ps', '--no-trunc', '--format', '{{.ID}}']).splitlines()
        for container_id in running:
            if not re.fullmatch('[0-9a-f]{64}', container_id):
                return 'UNKNOWN'
            if container_id == config['container_id']:
                continue
            if container_id not in allowed:
                return 'BUSY'
            image = command([config['docker'], 'inspect', '--type', 'container', '--format',
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
        if not isinstance(last_check, (int, float)) or not math.isfinite(last_check):
            return False
        text = command(['systemctl', 'show', 'docker.service', '-p', 'ActiveState',
                        '-p', 'ExecMainStartTimestampMonotonic'])
        values = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        started = int(values['ExecMainStartTimestampMonotonic']) / 1_000_000
        if values['ActiveState'] != 'active' or started <= 0 or started > last_check:
            return False
        output = command([config['docker'], 'events', '--since', str(cursor - 1), '--until', str(until),
                          '--format', '{{.TimeNano}}']).splitlines()
        return len(output) < 256 and all(re.fullmatch(r'\d+', line) for line in output)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        return False


def rearm_stopped(config, previous, boot_id, *, inspect=inspect_target, events=docker_events,
                  clock=time.monotonic, wall=time.time):
    """Rearm one pinned stopped target after an explicit operator repair."""
    if (not isinstance(previous, dict) or previous.get('version') != 1
            or previous.get('target_id') != config['container_id']
            or previous.get('boot_id') != boot_id or previous.get('manual_hold') is not False):
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
                   rearm_finished_at=state['FinishedAt'], policy_version=POLICY_VERSION)
    return current


def prepare_previous(previous, config, boot_id, boot_epoch):
    """Reset the window and allow a verified pre-boot exit to be reconsidered."""
    valid = (isinstance(previous, dict) and previous.get('version') == 1
             and previous.get('target_id') == config['container_id']
             and isinstance(previous.get('boot_id'), str)
             and previous.get('policy_version') in (None, POLICY_VERSION))
    current = dict(previous) if valid else {}
    new_boot = valid and current['boot_id'] != boot_id
    legacy_boot_hold = (valid and not new_boot and current.get('policy_version') != POLICY_VERSION
                        and current.get('state') == 'MANUAL_HOLD' and current.get('armed') is False)
    # A host reboot starts a new recovery window; explicit stop actions after
    # that boot still block it. Missing or corrupt state must not silently
    # claim an existing stopped container.
    if ((new_boot and isinstance(current.get('manual_hold'), bool))
            or (legacy_boot_hold and current.get('manual_hold') is False)):
        current.update(boot_recovery_pending=True, boot_recovery=False, boot_finished_at=None,
                       boot_epoch=boot_epoch, event_cursor=boot_epoch, armed=False, manual_hold=False)
        current.pop('last_check', None)
    elif new_boot:
        current.update(boot_recovery_pending=False, boot_recovery=False, boot_finished_at=None,
                       event_cursor=boot_epoch, armed=False)
        current.pop('last_check', None)
    current.update(boot_id=boot_id, policy_version=POLICY_VERSION,
                   samples=[], idle_since=None, failure_ratio=None)
    return current


def finished_before_boot(finished_at, boot_epoch):
    if not isinstance(finished_at, str) or not isinstance(boot_epoch, (int, float)):
        raise ValueError('UNKNOWN_BOOT_TIME')
    ended = datetime.fromisoformat(finished_at.replace('Z', '+00:00'))
    if ended.tzinfo is None or ended.year < 1970 or not math.isfinite(boot_epoch):
        raise ValueError('UNKNOWN_FINISHED_TIME')
    return ended.timestamp() < boot_epoch


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
    current.update(version=1, epoch=wall(), last_check=now, target_id=config.get('container_id'))
    try:
        state = inspect(config)
        pending_boot = previous.get('boot_recovery_pending') is True
        cursor = previous.get('boot_epoch') if pending_boot else previous.get('event_cursor', current['epoch'] - 1)
        if not isinstance(cursor, (int, float)) or not math.isfinite(cursor) or cursor > current['epoch']:
            raise ValueError('UNKNOWN_EVENT_CURSOR')
        changes = events(config, cursor if pending_boot else cursor - 1, current['epoch'])
        # Longer planned waits still need proof that Docker control history is intact.
        elapsed = now - previous.get('last_check', now)
        gap = not pending_boot and bool(previous) and elapsed > INTERVAL * 2
        missed_check = elapsed > next_interval(previous) * 2
        manual = previous.get('manual_hold', False) or any(e['action'] in ('kill', 'stop', 'destroy') for e in changes)
        if state['Running'] and state['StartedAt'] != previous.get('started'):
            manual = False
        current.update(event_cursor=current['epoch'], started=state['StartedAt'], manual_hold=manual)
        if state['Running'] or state.get('Paused') or state.get('Restarting'):
            current.update(state='RUNNING', armed=not manual, boot_recovery_pending=False,
                           boot_recovery=False, boot_finished_at=None,
                           rearm_finished_at=None,
                           samples=[], idle_since=None, failure_ratio=None)
            persist(current)
            return current
        if state['Status'] != 'exited':
            raise ValueError('UNEXPECTED_CONTAINER_STATE')
        armed = previous.get('armed') is True
        boot_recovery = previous.get('boot_recovery') is True
        if pending_boot:
            eligible = (not manual and not any(e['action'] == 'start' for e in changes)
                        and finished_before_boot(state['FinishedAt'], previous.get('boot_epoch')))
            current['boot_recovery_pending'] = False
            if not eligible:
                current.update(state='MANUAL_HOLD', armed=False, boot_recovery=False,
                               samples=[], idle_since=None, failure_ratio=None)
                persist(current)
                return current
            armed, boot_recovery = True, True
            current.update(armed=True, boot_recovery=True, boot_finished_at=state['FinishedAt'])
        elif boot_recovery and state['FinishedAt'] != previous.get('boot_finished_at'):
            manual = True
            current['manual_hold'] = True
        if previous.get('rearm_finished_at') is not None and state['FinishedAt'] != previous['rearm_finished_at']:
            manual = True
            current['manual_hold'] = True
        if gap and armed and not manual and not any(e['action'] in ('start', 'restart') for e in changes):
            if control_history_complete(config, previous, cursor, current['epoch']):
                if missed_check:
                    previous = dict(previous, samples=[], idle_since=None, failure_ratio=None)
                gap = False
        if (manual or gap or not armed
                or (state['ExitCode'] == 0 and not boot_recovery and previous.get('rearm_finished_at') is None)):
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
                    or final_state['FinishedAt'] != state['FinishedAt']
                    or any(e['action'] in ('kill', 'stop', 'start', 'destroy') for e in final_events)):
                current.update(state='MANUAL_HOLD', armed=False, samples=[], idle_since=None, failure_ratio=None)
            else:
                final_sample = sample(config)
                if final_sample == 'IDLE':
                    # A slow NPU call must not hide an operator action during it.
                    after_probe = inspect(config)
                    after_events = events(config, cursor - 1, wall())
                    if (after_probe['Running'] or after_probe.get('Paused') or after_probe.get('Restarting')
                            or after_probe['StartedAt'] != state['StartedAt']
                            or after_probe['FinishedAt'] != state['FinishedAt']
                            or any(e['action'] in ('kill', 'stop', 'start', 'destroy') for e in after_events)):
                        current.update(state='MANUAL_HOLD', armed=False, samples=[], idle_since=None, failure_ratio=None)
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
        current.update(state='CONTROL_UNKNOWN', armed=previous.get('armed') is True,
                       samples=[], idle_since=None, failure_ratio=None)
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
        # A prompt observer restart can retain stop intent, never an idle window.
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        try:
            previous = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError, AttributeError):
            previous = {}
        if args.rearm_stopped:
            write_state(path, rearm_stopped(config, previous, boot))
            return
        previous = prepare_previous(previous, config, boot, linux_boot_epoch())
        while True:
            began = time.monotonic()
            previous = step(config, previous, persist=lambda s: write_state(path, s),
                            start=lambda c: command([c['docker'], 'start', c['container_id']], timeout=45))
            elapsed = time.monotonic() - began
            # No overlapping requests, no catch-up burst after a slow cycle.
            interval = next_interval(previous)
            time.sleep(interval - elapsed if elapsed < interval else interval)


if __name__ == '__main__':
    main()
