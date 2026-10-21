"""Yield a failed DeepInfer container until an hour of mostly observed idleness."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time

from .watchdog import load_config, npu_idle, write_state

INTERVAL = 60
WINDOW = 3600
MAX_FAILURE_RATIO = 0.10


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
             and len(samples) >= 60)
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


def step(config, previous, *, inspect=inspect_target, events=docker_events,
         sample=npu_observation, start=None, persist=None, clock=time.monotonic, wall=time.time):
    """Single synchronous sample; never stop or kill a container or workload."""
    now = clock()
    current = dict(previous)
    current.update(version=1, epoch=wall(), last_check=now, target_id=config.get('container_id'))
    try:
        state = inspect(config)
        cursor = previous.get('event_cursor', current['epoch'] - 1)
        changes = events(config, cursor - 1, current['epoch'])
        # If the control history has a gap, a stopped target needs human re-arming.
        gap = bool(previous) and now - previous.get('last_check', now) > INTERVAL * 2
        manual = previous.get('manual_hold', False) or any(e['action'] in ('kill', 'stop', 'destroy') for e in changes)
        if state['Running'] and state['StartedAt'] != previous.get('started'):
            manual = False
        current.update(event_cursor=current['epoch'], started=state['StartedAt'], manual_hold=manual)
        if state['Running'] or state.get('Paused') or state.get('Restarting'):
            current.update(state='RUNNING', armed=not manual, samples=[], idle_since=None, failure_ratio=None)
            persist(current)
            return current
        if state['Status'] != 'exited':
            raise ValueError('UNEXPECTED_CONTAINER_STATE')
        if manual or gap or not previous.get('armed') or state['ExitCode'] == 0:
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
                    # Reset durably before requesting start; a failed start waits a new hour.
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
            if (previous.get('version') != 1 or previous.get('boot_id') != boot
                    or previous.get('target_id') != config['container_id']):
                previous = {}
        except (OSError, ValueError, AttributeError):
            previous = {}
        previous.update(boot_id=boot, samples=[], idle_since=None, failure_ratio=None)
        while True:
            began = time.monotonic()
            previous = step(config, previous, persist=lambda s: write_state(path, s),
                            start=lambda c: command([c['docker'], 'start', c['container_id']], timeout=45))
            elapsed = time.monotonic() - began
            # No overlapping requests, no catch-up burst after a slow cycle.
            time.sleep(INTERVAL - elapsed if elapsed < INTERVAL else INTERVAL)


if __name__ == '__main__':
    main()
