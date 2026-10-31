"""Spawn-safe entry for the fixed EP16 API coordinator or eight-worker agent."""
import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=('coordinator', 'worker'), required=True)
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    if args.role == 'worker':
        from agent import run_agent
        from resident_worker import resident_worker
        run_agent((os.environ['DEEPINFER_CONTROL_BIND'], int(os.environ['DEEPINFER_CONTROL_PORT'])),
                  os.environ['DEEPINFER_CONTROL_RUN_ID'], sys.executable, worker_target=resident_worker)
        return
    import api_entry
    profile = Path(__file__).resolve().parents[2] / 'recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017-ep16/recipe.json'
    config = json.loads(profile.read_text(encoding='utf-8'))
    sys.argv = ['api_entry', '--model', '/model', '--port', str(args.port)]
    for key, value in config['server'].items():
        option = '--' + key.replace('_', '-')
        if isinstance(value, bool):
            if value:
                sys.argv.append(option)
        else:
            sys.argv += [option, str(value)]
    api_entry.main()


if __name__ == '__main__':
    main()
