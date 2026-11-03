"""Install an interactive SSH notice for a pinned vLLM PD container."""
import argparse
import json
import os
from pathlib import Path
import stat

from .ssh_notice import install_atomic, render_notice, verify_target

DESTINATION = Path('/etc/profile.d/llmdeploy-pd-notice.sh')
NOTICE = '当前机器正在运行 vLLM-Ascend P/D 推理服务，占用 NPU。'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--install', action='store_true', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('ROOT_REQUIRED')
    parent = DESTINATION.parent.stat()
    if parent.st_uid != 0 or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError('UNSAFE_PROFILE_DIRECTORY')
    config = json.loads(args.config.read_text(encoding='utf-8'))
    running = verify_target(config)
    install_atomic(DESTINATION, render_notice(config['container_id'], config['container_name'], notice=NOTICE))
    print(json.dumps({'state': 'INSTALLED', 'target_running': running}))


if __name__ == '__main__':
    main()
