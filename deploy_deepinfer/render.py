"""Generate an explicit Linux Docker command; never execute it."""
import argparse
import glob
from pathlib import Path
import shlex
import sys

from deploy_deepinfer.config import RECIPE, ROOT, load, validate

MOUNTS = ['/usr/local/Ascend/driver/lib64', '/usr/local/Ascend/driver/version.info',
          '/usr/local/Ascend/driver/topo', '/usr/local/dcmi', '/etc/ascend_install.info', '/usr/local/bin/npu-smi']
DEVICES = ['/dev/davinci' + str(i) for i in range(8)] + ['/dev/davinci_manager', '/dev/hisi_hdc', '/dev/ummu/tid', '/dev/ubmempfd', '/dev/ubagg']


def command(host, recipe, repo_path, optional_devices=None):
    validate(host, recipe)
    if not repo_path.startswith('/') or any(c in repo_path for c in ':,\n\r'):
        raise ValueError('RENDER_ON_TARGET_LINUX_HOST')
    if optional_devices is None:
        optional_devices = sorted(glob.glob('/dev/uburma/*') + glob.glob('/dev/ubcore/*'))
    if any(not (p.startswith('/dev/uburma/') or p.startswith('/dev/ubcore/')) or any(c in p for c in '\n\r') for p in optional_devices):
        raise ValueError('INVALID_OPTIONAL_DEVICE')
    args = ['docker', 'run', '--detach', '--name', host['container_name'], '--label', 'llmdeploy.recipe=' + recipe['id'],
            '--network', 'host', '--ipc', 'shareable', '--shm-size', '256g', '--ulimit', 'memlock=-1:-1', '--cap-add', 'IPC_LOCK',
            '--restart', 'no', '--entrypoint', '/opt/llmdeploy/tini', '--workdir', '/opt/deepinfer-build/source']
    mounts = [(repo_path, '/deployment', True), (host['model_dir'], '/model', True),
              (host['artifacts_dir'], '/artifacts', False), (host['warmup_file'], '/config/warmup.json', True)]
    mounts += [(p, p, True) for p in MOUNTS]
    for source, target, readonly in mounts:
        args += ['--mount', 'type=bind,src=' + source + ',dst=' + target + (',readonly' if readonly else '')]
    for device in DEVICES + optional_devices:
        args += ['--device', device]
    env = {**recipe['env'], 'MASTER_ADDR': '127.0.0.1', 'MASTER_PORT': str(host['master_port']),
           'LLMDEPLOY_API_PORT': str(host['port']), 'ASCEND_RT_VISIBLE_DEVICES': '0,1,2,3,4,5,6,7'}
    for key, value in env.items():
        args += ['--env', key + '=' + value]
    # Source only the audited native environment from the pinned image, then use
    # the repository module without replacing the native worker's PYTHONPATH.
    script = ('source /opt/llmdeploy/runtime_env.sh && '
              'export PYTHONPATH=/deployment:/opt/deepinfer-build/source && exec '
              '/opt/deepinfer-api-venv/bin/python -m deploy_deepinfer.entrypoint')
    return args + [host['image_id'], '--', '/bin/bash', '--noprofile', '--norc', '-c', script]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if sys.platform != 'linux':
        parser.error('Render on the target Linux host to discover its own RDMA devices.')
    host = load(args.host)
    cmd = command(host, load(RECIPE), str(ROOT.resolve()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8', newline='\n') as out:
        out.write('#!/usr/bin/env bash\nset -euo pipefail\n')
        out.write('python3 ' + shlex.quote(str(ROOT / 'scripts/deepinfer_preflight.py')) + ' --host ' + shlex.quote(str(args.host.resolve())) + '\n')
        out.write('mkdir -p -- ' + shlex.quote(host['artifacts_dir']) + '\n')
        out.write(shlex.join(cmd) + '\n')
    print('RENDERED_ONLY: ' + str(args.output))


if __name__ == '__main__':
    main()
