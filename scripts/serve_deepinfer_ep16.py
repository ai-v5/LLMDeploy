"""Inspect or explicitly start the fixed two-host, resident-expert 910B profile."""
import argparse
import importlib.metadata as metadata
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import socket
import subprocess
import sys

from serve_deepinfer_a2 import digest, load, relative_file, verify_idle_cards, verify_source
from verify_model import verify as verify_model

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017-ep16'
BRIDGE = ROOT / 'scripts/deepinfer_ep16'
WORKSPACE = Path('/workspace')
NATIVE_PYTHON = '/usr/local/python3.12.13/bin/python3'


def build_plan(recipe, source, role, coordinator, peer, interface, run_id,
               port=8900, master_port=29550, control_port=29570):
    path = PurePosixPath(source)
    if not path.is_relative_to('/workspace') or '..' in path.parts or any(c in source for c in '\r\n'):
        raise ValueError('USE_CONTAINER_WORKSPACE_SOURCE')
    if role not in ('coordinator', 'worker'):
        raise ValueError('INVALID_NODE_ROLE')
    addresses = [ipaddress.IPv4Address(value) for value in (coordinator, peer)]
    if addresses[0] == addresses[1] or any(a.is_unspecified or a.is_multicast for a in addresses):
        raise ValueError('REQUIRE_DISTINCT_UNICAST_NODE_ADDRESSES')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,63}', interface):
        raise ValueError('INVALID_INTERFACE')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,64}', run_id):
        raise ValueError('INVALID_RUN_ID')
    ports = (port, master_port, control_port)
    if any(type(v) is not int or not 1 <= v <= 65535 for v in ports) or master_port <= 50:
        raise ValueError('INVALID_PORT')
    if len({*ports, master_port - 50}) != 4:
        raise ValueError('DUPLICATE_API_CONTROL_OR_DISTRIBUTED_PORT')
    if recipe['topology']['ep'] != 16 or recipe['topology']['dp'] != 16:
        raise ValueError('REQUIRE_FIXED_EP16_DP16_PROFILE')
    env = {**recipe['env'], 'MASTER_ADDR': coordinator, 'MASTER_PORT': str(master_port),
           'GLOO_SOCKET_IFNAME': interface, 'TASK_NODE_RANK': '0' if role == 'coordinator' else '1',
           'DEEPINFER_CONTROL_BIND': coordinator, 'DEEPINFER_CONTROL_PEER': peer,
           'DEEPINFER_CONTROL_PORT': str(control_port), 'DEEPINFER_CONTROL_RUN_ID': run_id,
           'PYTHONPYCACHEPREFIX': '/workspace/cache/pyc', 'ASCEND_WORK_PATH': '/workspace',
           'ASCEND_PROCESS_LOG_PATH': '/workspace/logs/' + run_id,
           'PYTHONPATH': ':'.join((BRIDGE.as_posix(), source + '/examples', source,
                                   '/workspace/envs/native-extra-site', '/workspace/envs/runtime-site'))}
    libraries = ['/workspace/' + name for name in recipe['library_paths']]
    libraries += ['/usr/local/Ascend/driver/lib64/driver', '/usr/local/Ascend/driver/lib64/common']
    argv = [NATIVE_PYTHON, str(BRIDGE / 'supervisor.py'), str(BRIDGE / 'runtime_entry.py'),
            '--role', role, '--port', str(port)]
    return {'env': env, 'argv': argv, 'library_path_prefix': ':'.join(libraries),
            'node_role': role, 'distributed_store_port': master_port - 50,
            'scope': 'FIXED_EP16_PLAN_ONLY_NOT_RUNTIME_OR_CONTEXT_ACCEPTANCE'}


def check_runtime(recipe, source, full_model_hash=False):
    if sys.platform != 'linux' or platform.machine() != 'aarch64' or sys.version_info[:3] != (3, 12, 13):
        raise ValueError('REQUIRE_PINNED_LINUX_AARCH64_PYTHON_3_12_13')
    if not ROOT.is_relative_to(WORKSPACE) or not source.is_relative_to(WORKSPACE):
        raise ValueError('DEPLOY_REPOSITORY_AND_SOURCE_MUST_BE_INSIDE_WORKSPACE')
    if Path(sys.executable).resolve() != Path(NATIVE_PYTHON).resolve():
        raise ValueError('USE_PINNED_IMAGE_INTERPRETER')
    if 'torch' in sys.modules or 'torch_npu' in sys.modules:
        raise ValueError('RUNTIME_CHECK_MUST_NOT_INITIALIZE_ACCELERATOR')
    count = verify_source(source, load(PROFILE / recipe['source']['manifest']))
    lock = load(PROFILE / recipe['runtime_lock'])
    native = lock['native_extension']
    files = [(relative_file(source, native['path'], allowed_root=WORKSPACE), native)]
    files += [(relative_file(WORKSPACE, name), row) for name, row in lock['workspace_artifacts'].items()]
    for path, row in files:
        if path.stat().st_size != row['size'] or digest(path) != row['sha256']:
            raise ValueError('RUNTIME_ARTIFACT_IDENTITY_MISMATCH: ' + path.name)
    old_path = sys.path.copy()
    try:
        sys.path[:0] = ['/workspace/envs/native-extra-site', '/workspace/envs/runtime-site']
        versions = {name: metadata.version(name) for name in lock['packages']}
    finally:
        sys.path[:] = old_path
    if versions != lock['packages']:
        raise ValueError('PACKAGE_VERSION_MISMATCH')
    control = load(PROFILE / 'control-files.json')['files']
    actual = {p.name for p in BRIDGE.glob('*.py')}
    if actual != set(control):
        raise ValueError('CONTROL_SOURCE_FILE_SET_MISMATCH')
    for name, sha in control.items():
        if digest(relative_file(BRIDGE, name), normalize_lf=True) != sha:
            raise ValueError('CONTROL_SOURCE_IDENTITY_MISMATCH: ' + name)
    model = verify_model(Path('/model'), load(PROFILE / recipe['model']['manifest']), full_model_hash)
    if 'torch' in sys.modules or 'torch_npu' in sys.modules:
        raise ValueError('RUNTIME_CHECK_IMPORTED_ACCELERATOR')
    return {'state': 'PASS_EP16_LOCAL_IDENTITY_ONLY_NOT_WORLD_OR_MODEL_ACCEPTANCE',
            'python_sources': count, 'control_sources': len(control), 'runtime_artifacts': len(files),
            'model_files': model, 'model_content_check': 'FULL_SHA256' if full_model_hash else 'SIZE_ONLY_NOT_CONTENT',
            'torch_imported': False}


def idle_preflight(plan):
    verify_idle_cards(subprocess.check_output(['/usr/local/sbin/npu-smi', 'info'], text=True))
    if not (Path('/sys/class/net') / plan['env']['GLOO_SOCKET_IFNAME']).exists():
        raise ValueError('GLOO_INTERFACE_NOT_PRESENT')
    role = plan['node_role']
    if role == 'coordinator':
        reservations = [(plan['env']['DEEPINFER_CONTROL_BIND'], int(plan['env']['DEEPINFER_CONTROL_PORT'])),
                        (plan['env']['MASTER_ADDR'], plan['distributed_store_port']),
                        ('127.0.0.1', int(plan['argv'][-1]))]
        for address, port in reservations:
            with socket.socket() as candidate:
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                candidate.bind((address, port))
    run_id = plan['env']['DEEPINFER_CONTROL_RUN_ID']
    if any((WORKSPACE / 'receipts').glob(run_id + '-resident-*.json')):
        raise ValueError('RUN_ID_ALREADY_HAS_RESIDENCY_RECEIPTS_USE_NEW_RUN_ID')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--plan', action='store_true')
    action.add_argument('--check-only', action='store_true')
    action.add_argument('--serve', action='store_true', help='explicitly start this node after identity and idle checks')
    parser.add_argument('--source', default='/workspace/src/deepinfer-runtime')
    parser.add_argument('--role', choices=('coordinator', 'worker'), required=True)
    parser.add_argument('--coordinator', required=True, help='coordinator IPv4 address on the trusted control network')
    parser.add_argument('--peer', required=True, help='worker IPv4 address on the same trusted network')
    parser.add_argument('--interface', required=True, help='this node Gloo network interface')
    parser.add_argument('--run-id', required=True, help='same new run name on both nodes, never an authentication secret')
    parser.add_argument('--port', type=int, default=8900)
    parser.add_argument('--master-port', type=int, default=29550)
    parser.add_argument('--control-port', type=int, default=29570)
    parser.add_argument('--full-model-hash', action='store_true')
    args = parser.parse_args(argv)
    recipe = load(PROFILE / 'recipe.json')
    plan = build_plan(recipe, args.source, args.role, args.coordinator, args.peer, args.interface,
                      args.run_id, args.port, args.master_port, args.control_port)
    if args.plan:
        print(json.dumps(plan, indent=2))
        return
    source = Path(args.source).resolve()
    check = check_runtime(recipe, source, args.full_model_hash)
    print(json.dumps(check), flush=True)
    if not args.serve:
        return
    idle_preflight(plan)
    for name in ('receipts', 'home', 'tmp', 'cache/xdg', 'cache/hf', 'cache/jit/tilelang',
                 'cache/torch-extensions', 'cache/pyc', 'logs/' + args.run_id):
        (WORKSPACE / name).mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, **plan['env']}
    environment['LD_LIBRARY_PATH'] = plan['library_path_prefix'] + ':' + os.environ.get('LD_LIBRARY_PATH', '')
    os.execvpe(NATIVE_PYTHON, plan['argv'], environment)


if __name__ == '__main__':
    main()
