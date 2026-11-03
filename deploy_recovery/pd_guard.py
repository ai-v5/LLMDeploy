"""Keep a pinned PD container from sharing cards with another NPU workload."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time

from . import deepinfer, pd
from .watchdog import write_state


def npu_processes(payload):
    health, processes, empty = {}, [], set()
    for line in payload.splitlines():
        match = re.match(r'\|\s*(\d+)\s*\|\s*Ascend950DT\s*\|\s*(OK|Alarm)\s*\|', line)
        if match:
            health[str(int(match.group(1)))] = match.group(2)
        match = re.match(r'\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+)\|', line)
        if match:
            processes.append((int(match.group(1)), int(match.group(2))))
        match = re.search(r'No running processes found in NPU\s+(\d+)', line)
        if match:
            empty.add(int(match.group(1)))
    if set(health) != {str(i) for i in range(8)} or empty | {d for d, _ in processes} != set(range(8)):
        raise ValueError('INCOMPLETE_NPU_SNAPSHOT')
    return health, processes


def probe(config, *, check_faults=True):
    state = deepinfer.inspect_target(config)
    if not state['Running']:
        return {'state': 'STOPPED'}
    pd.verify_launcher(config)
    receipt = pd.fault_baseline(config)
    top = deepinfer.docker_command(config, ['top', config['container_id'], '-o', 'pid'])
    known = {int(line.strip()) for line in top.splitlines() if line.strip().isdigit()}
    health, processes = npu_processes(deepinfer.command([config['npu_smi'], 'info']))
    if health != receipt['health_before']:
        return {'state': 'HEALTH_CHANGED'}
    if any(pid not in known for _, pid in processes):
        return {'state': 'NPU_CONFLICT'}
    if check_faults:
        faults = {}
        for device in range(8):
            result = deepinfer.command([config['npu_smi'], 'info', '-t', 'current-fault-event',
                                        '-i', str(device), '-p', '0'])
            faults[str(device)] = [list(x) for x in sorted(re.findall(
                r'event id=(0x[0-9A-Fa-f]+),\s*severity=(\d+)', result))]
        if faults != receipt['faults_before']:
            return {'state': 'FAULT_CHANGED'}
    return {'state': 'MONITORING', 'owned_npu_processes': len(processes)}


def stop_owned(config):
    """Revalidate the fixed identity immediately before stopping that container."""
    if deepinfer.inspect_target(config)['Running']:
        deepinfer.docker_command(config, ['stop', '--time', '5', config['container_id']], timeout=40)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = pd.load_pd_config(args.config)
    status = Path(config['state_file']).with_name('guard.json')
    last_fault = 0.0
    while True:
        try:
            check_faults = time.monotonic() - last_fault >= 60
            result = probe(config, check_faults=check_faults)
            if check_faults and result['state'] == 'MONITORING':
                last_fault = time.monotonic()
            if result['state'] == 'NPU_CONFLICT':
                time.sleep(1)
                result = probe(config, check_faults=False)
            if result['state'] in ('NPU_CONFLICT', 'HEALTH_CHANGED', 'FAULT_CHANGED'):
                reason = result['state']
                stop_owned(config)
                result = {'state': 'STOPPED_BY_GUARD', 'reason': reason}
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
            result = {'state': 'UNKNOWN'}
        result['epoch'] = time.time()
        try:
            write_state(status, result)
        except OSError:
            pass
        time.sleep(3 if result['state'] == 'MONITORING' else 30)


if __name__ == '__main__':
    main()
