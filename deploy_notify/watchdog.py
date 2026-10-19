"""Keep the notification observer alive without touching inference services."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from .worker import load_config, singleton, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    if load_config(config_path).get('backend') is None:
        return
    root = config_path.parent
    lock = singleton(root, 'watchdog')
    if lock is None:
        return
    state = {'watchdog_pid': os.getpid(), 'restarts': 0}
    while True:
        try:
            child = subprocess.Popen(
                [sys.executable, '-m', 'deploy_notify', '--config', str(config_path)],
                cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            state.update(state='RUNNING', worker_pid=child.pid, worker_started_epoch=time.time())
            write_json(root / 'watchdog-state.json', state)
            code = child.wait()
            state.update(state='BACKOFF', last_exit_code=code, last_exit_epoch=time.time())
        except OSError as error:
            state.update(state='SPAWN_FAILED', error_type=type(error).__name__)
        state['restarts'] += 1
        write_json(root / 'watchdog-state.json', state)
        time.sleep(10)


if __name__ == '__main__':
    main()
