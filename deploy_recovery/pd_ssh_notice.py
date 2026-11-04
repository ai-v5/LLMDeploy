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
    parser.add_argument('--message-file', type=Path, help='Root-owned local notice text, read at login')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('ROOT_REQUIRED')
    parent = DESTINATION.parent.stat()
    if parent.st_uid != 0 or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError('UNSAFE_PROFILE_DIRECTORY')
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if args.message_file is not None:
        message_stat = args.message_file.stat()
        if (args.message_file.is_symlink() or not args.message_file.is_file() or message_stat.st_uid != 0
                or message_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or not message_stat.st_mode & stat.S_IROTH):
            raise PermissionError('UNSAFE_MESSAGE_FILE')
        if not args.message_file.read_text(encoding='utf-8').strip():
            raise ValueError('EMPTY_MESSAGE_FILE')
    running = verify_target(config)
    install_atomic(DESTINATION, render_notice(config['container_id'], config['container_name'], notice=NOTICE,
                                             message_file=args.message_file))
    print(json.dumps({'state': 'INSTALLED', 'target_running': running}))


if __name__ == '__main__':
    main()
