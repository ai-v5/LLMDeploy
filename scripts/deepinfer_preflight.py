"""Read-only admission checks; do not stop a container or kill an NPU process."""
import argparse
import json
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy_deepinfer.config import RECIPE, load, validate
from deploy_deepinfer.render import DEVICES, MOUNTS
from deploy_recovery.watchdog import npu_idle


def complete_empty(card, result):
    if result.returncode != 0 or re.findall(r'NPU ID\s*:\s*(\d+)', result.stdout) != [str(card)]:
        raise ValueError('UNKNOWN_DEVICE_PROCESS_STATE')
    if 'No process in device.' not in result.stdout or 'Process id:' in result.stdout:
        raise ValueError('NPU_IN_USE_OR_UNKNOWN')


def check(host):
    validate(host, load(RECIPE))
    if platform.system() != 'Linux' or platform.machine() not in ('aarch64', 'arm64'):
        raise ValueError('LINUX_AARCH64_REQUIRED')
    for name in DEVICES + MOUNTS + [host['model_dir'], host['warmup_file']]:
        if not Path(name).exists():
            raise ValueError('REQUIRED_LOCAL_PATH_MISSING:' + name)
    rows = subprocess.run(['npu-smi', 'info'], capture_output=True, text=True, timeout=30)
    if rows.returncode != 0 or not npu_idle(rows.stdout):
        raise ValueError('EIGHT_HEALTHY_IDLE_CARDS_REQUIRED')
    for card in range(8):
        result = subprocess.run(['npu-smi', 'info', '-t', 'proc-mem', '-i', str(card), '-p', '0'],
                                capture_output=True, text=True, timeout=10)
        complete_empty(card, result)
    for port in (host['port'], host['master_port']):
        with socket.socket() as probe:
            probe.bind(('0.0.0.0', port))
    result = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', host['image_id']],
                            capture_output=True, text=True, timeout=15)
    if result.returncode != 0 or result.stdout.strip() != host['image_id']:
        raise ValueError('PINNED_IMAGE_NOT_IMPORTED')
    result = subprocess.run(['docker', 'ps', '-aq', '--filter', 'name=^/' + host['container_name'] + '$'],
                            capture_output=True, text=True, timeout=15)
    if result.returncode != 0 or result.stdout.strip():
        raise ValueError('CONTAINER_NAME_EXISTS_OR_UNKNOWN')
    warmup = load(host['warmup_file'])
    if not warmup.get('groups'):
        raise ValueError('EXPLICIT_SYNTHETIC_WARMUP_REQUIRED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=Path)
    args = parser.parse_args()
    check(load(args.host))
    print(json.dumps({'state': 'READ_ONLY_PREFLIGHT_PASS_NOT_MODEL_ACCEPTANCE'}))


if __name__ == '__main__':
    main()
