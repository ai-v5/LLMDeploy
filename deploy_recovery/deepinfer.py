"""Recover a pinned DeepInfer container after two hours of observed idleness."""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time

from .watchdog import load_config, npu_idle, write_state

INTERVAL = 60
WINDOW = 7200
MAX_FAILURE_RATIO = 0.10
POLICY_VERSION = 2


def sample_window(previous, now, observation):
    """Unknown samples consume the failure budget; only busy resets idleness."""
    if observation == 'BUSY':
        return {'samples': [], 'idle_since': None, 'failure_ratio': None}, False
    samples = list(previous.get('samples', []))
    since = previous.get('idle_since')
    if samples:
        gap = now - samples[-1]['time']
        if gap <= 0:
            samples, since = [], None
        else:
            # Missed whole sampling slots must not disappear from the denominator.
            for i in range(1, max(1, math.floor(gap / INTERVAL))):
                samples.append({'time': samples[-1]['time'] + INTERVAL, 'value': 'UNKNOWN'})
    samples.append({'time': now, 'value': observation})
    if since is None and observation == 'IDLE':
        since = now
    samples = [s for s in samples if s['time'] >= now - WINDOW]
    failed = sum(s['value'] != 'IDLE' for s in samples)
    ratio = failed / len(samples)
    ready = (since is not None and now - since >= WINDOW
             and observation == 'IDLE' and ratio <= MAX_FAILURE_RATIO
             and len(samples) >= WINDOW // INTERVAL)
    return {'samples': samples, 'idle_since': since, 'failure_ratio': ratio}, ready


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
         sample=npu_observation, start=None, persist=None, clock=time.monotonic, wall=time.time):
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
        # If the control history has a gap, a stopped target needs human re-arming.
        gap = not pending_boot and bool(previous) and now - previous.get('last_check', now) > INTERVAL * 2
        manual = previous.get('manual_hold', False) or any(e['action'] in ('kill', 'stop', 'destroy') for e in changes)
        if state['Running'] and state['StartedAt'] != previous.get('started'):
            manual = False
        current.update(event_cursor=current['epoch'], started=state['StartedAt'], manual_hold=manual)
        if state['Running'] or state.get('Paused') or state.get('Restarting'):
            current.update(state='RUNNING', armed=not manual, boot_recovery_pending=False,
                           boot_recovery=False, boot_finished_at=None,
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
        if manual or gap or not armed or (state['ExitCode'] == 0 and not boot_recovery):
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
                    # One failed collection is one failed minute, including the final check.
                    current['samples'][-1]['value'] = 'UNKNOWN'
                    current['failure_ratio'] = sum(s['value'] != 'IDLE' for s in current['samples']) / len(current['samples'])
                    current['observation'] = 'UNKNOWN'
        persist(current)
        return current
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        current.update(state='CONTROL_UNKNOWN', armed=False, samples=[], idle_since=None, failure_ratio=None)
        persist(current)
        return current


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
            raise SystemExit('RECOVERY_ALREADY_RUNNING')
        # A prompt observer restart can retain stop intent, never an idle window.
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        try:
            previous = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError, AttributeError):
            previous = {}
        previous = prepare_previous(previous, config, boot, linux_boot_epoch())
        while True:
            began = time.monotonic()
            previous = step(config, previous, persist=lambda s: write_state(path, s),
                            start=lambda c: command([c['docker'], 'start', c['container_id']], timeout=45))
            elapsed = time.monotonic() - began
            # No overlapping requests, no catch-up burst after a slow cycle.
            time.sleep(INTERVAL - elapsed if elapsed < INTERVAL else INTERVAL)


if __name__ == '__main__':
    main()
