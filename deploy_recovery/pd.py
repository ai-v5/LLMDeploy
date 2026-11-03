"""Recover a pinned vLLM PD container after two hours of NPU idleness."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from . import deepinfer
from .watchdog import load_config, npu_idle


def load_pd_config(path):
    fields = ('launcher_file', 'launcher_sha256', 'fault_receipt', 'fault_receipt_sha256')
    config = load_config(path, optional_fields=fields)
    if not all(k in config for k in fields):
        raise ValueError('INCOMPLETE_PD_CONFIG')
    if (not all(isinstance(config[k], str) and Path(config[k]).is_absolute()
                for k in ('launcher_file', 'fault_receipt'))
            or not all(isinstance(config[k], str) and re.fullmatch('[0-9a-f]{64}', config[k])
                       for k in ('launcher_sha256', 'fault_receipt_sha256'))):
        raise ValueError('INVALID_PD_CONFIG')
    return config


def verify_launcher(config):
    path = Path(config['launcher_file'])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != config['launcher_sha256']:
        raise ValueError('LAUNCHER_CHANGED')


def fault_baseline(config):
    data = Path(config['fault_receipt']).read_bytes()
    if hashlib.sha256(data).hexdigest() != config['fault_receipt_sha256']:
        raise ValueError('FAULT_BASELINE_CHANGED')
    receipt = json.loads(data)
    if receipt.get('failure') is not None or receipt.get('image_id') != config['image_id']:
        raise ValueError('INVALID_FAULT_BASELINE')
    return receipt


def observe(config):
    """Require all eight cards idle and the accepted device/fault baseline unchanged."""
    try:
        verify_launcher(config)
        receipt = fault_baseline(config)
        output = deepinfer.command([config['npu_smi'], 'info'])
        if not npu_idle(output):
            return 'BUSY'
        health = {}
        for line in output.splitlines():
            match = re.match(r'\|\s*(\d+)\s*\|\s*Ascend950DT\s*\|\s*(OK|Alarm)\s*\|', line)
            if match:
                health[str(int(match.group(1)))] = match.group(2)
        if health != receipt['health_before']:
            return 'UNKNOWN'
        faults = {}
        for device in range(8):
            text = deepinfer.command([config['npu_smi'], 'info', '-t', 'current-fault-event',
                                      '-i', str(device), '-p', '0'])
            faults[str(device)] = [list(x) for x in sorted(re.findall(
                r'event id=(0x[0-9A-Fa-f]+),\s*severity=(\d+)', text))]
        return 'IDLE' if faults == receipt['faults_before'] else 'UNKNOWN'
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        return 'UNKNOWN'


def start(config):
    verify_launcher(config)
    fault_baseline(config)
    deepinfer.docker_command(config, ['start', config['container_id']], timeout=45)


def main():
    deepinfer.main(config_loader=load_pd_config, sample=observe, start=start)


if __name__ == '__main__':
    main()
