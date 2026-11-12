"""Inspect or explicitly start the fixed 910B ModelSlim runtime inside its container."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import socket
import subprocess
import sys

from verify_model import verify as verify_model

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017'
WORKSPACE = Path('/workspace')
NATIVE_PYTHON = '/usr/local/python3.12.13/bin/python3'


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path, *, normalize_lf=False):
    if normalize_lf:
        return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def relative_file(base, name, *, allowed_root=None):
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('INVALID_MANIFEST_PATH')
    path = (base / name).resolve()
    if not path.is_relative_to((allowed_root or base).resolve()) or not path.is_file():
        raise ValueError('MISSING_OR_OUTSIDE_RUNTIME_FILE')
    return path


def verify_source(source, manifest):
    paths = {name: relative_file(source, name) for name in manifest['after_sha256']}
    actual = {path.relative_to(source).as_posix() for path in source.rglob('*.py') if '.git' not in path.parts}
    if actual != set(paths):
        raise ValueError('SOURCE_FILE_SET_MISMATCH')
    for name, expected in manifest['after_sha256'].items():
        if digest(paths[name], normalize_lf=True) != expected:
            raise ValueError('SOURCE_SHA256_MISMATCH: ' + name)
    return len(manifest['after_sha256'])


def build_plan(recipe, source, api_python, port, master_port):
    for value in (source, api_python):
        path = PurePosixPath(value)
        if not path.is_relative_to('/workspace') or '..' in path.parts or any(c in value for c in '\r\n'):
            raise ValueError('USE_CONTAINER_WORKSPACE_PATHS')
    if any(type(value) is not int or not 1 <= value <= 65535 for value in (port, master_port)) or port == master_port:
        raise ValueError('INVALID_OR_DUPLICATE_PORT')
    env = {**recipe['env'], 'MASTER_ADDR': '127.0.0.1', 'MASTER_PORT': str(master_port), 'PYTHONPATH': source}
    worker = '/workspace/generated/deepinfer-a2-worker-' + hashlib.sha256(worker_script(source).encode()).hexdigest()[:16] + '.sh'
    argv = [api_python, source + '/examples/serve_recipe.py', '--model', '/model', '--worker-python', worker, '--port', str(port)]
    for key, value in recipe['server'].items():
        option = '--' + key.replace('_', '-')
        if isinstance(value, bool):
            if value:
                argv.append(option)
        else:
            argv += [option, str(value)]
    libraries = ['/workspace/' + value for value in recipe['library_paths']]
    libraries += ['/usr/local/Ascend/driver/lib64/driver', '/usr/local/Ascend/driver/lib64/common']
    return {'env': env, 'argv': argv, 'worker': worker, 'library_path_prefix': ':'.join(libraries)}


def worker_script(source):
    return ('#!/bin/bash\nset -euo pipefail\nexport PYTHONPATH=' + shlex.quote(source + ':/workspace/envs/python-site') +
            '\nexport PYTORCH_NPU_ALLOC_CONF=expandable_segments:True,pinned_reserve_segment_size_mb:64\nexec ' +
            NATIVE_PYTHON + ' "$@"\n')


def check_runtime(recipe, source, api_python, environment, full_model_hash):
    if sys.platform != 'linux' or platform.machine() != 'aarch64':
        raise ValueError('REQUIRE_LINUX_AARCH64_RUNTIME')
    if sys.version_info[:3] != (3, 12, 13):
        raise ValueError('USE_PINNED_NATIVE_PYTHON_3_12_13')
    if not source.is_relative_to(WORKSPACE.resolve()):
        raise ValueError('SOURCE_OUTSIDE_WORKSPACE')
    lock = load(PROFILE / recipe['runtime_lock'])
    count = verify_source(source, load(PROFILE / recipe['source']['manifest']))
    extension = lock['native_extension']
    path = relative_file(source, extension['path'], allowed_root=WORKSPACE)
    if path.stat().st_size != extension['size'] or digest(path) != extension['sha256']:
        raise ValueError('NATIVE_EXTENSION_IDENTITY_MISMATCH')
    for name, row in lock['workspace_artifacts'].items():
        path = relative_file(WORKSPACE, name)
        if path.stat().st_size != row['size'] or digest(path) != row['sha256']:
            raise ValueError('RUNTIME_ARTIFACT_IDENTITY_MISMATCH: ' + name)
    old_path = sys.path.copy()
    try:
        sys.path.insert(0, '/workspace/envs/python-site')
        native_versions = {name: metadata.version(name) for name in lock['native_packages']}
    finally:
        sys.path[:] = old_path
    if native_versions != lock['native_packages']:
        raise ValueError('NATIVE_PACKAGE_VERSION_MISMATCH')
    if Path(api_python).resolve() != Path(NATIVE_PYTHON).resolve():
        raise ValueError('API_VENV_MUST_USE_PINNED_IMAGE_INTERPRETER')
    for name, row in lock['api_artifacts'].items():
        path = relative_file(Path(api_python).parent.parent, name, allowed_root=WORKSPACE)
        if path.stat().st_size != row['size'] or digest(path) != row['sha256']:
            raise ValueError('API_PROTOCOL_ARTIFACT_IDENTITY_MISMATCH: ' + name)
    code = ('import importlib.metadata as m,json,sys; print(json.dumps({"packages":'
            '{name:m.version(name) for name in ' + repr(list(lock['api_packages'])) + '},"torch_imported":"torch" in sys.modules}))')
    result = subprocess.run([api_python, '-c', code], env=environment, capture_output=True, text=True, check=True)
    api_versions = json.loads(result.stdout)
    if api_versions['packages'] != lock['api_packages'] or api_versions['torch_imported']:
        raise ValueError('API_PACKAGE_OR_INTERPRETER_ISOLATION_MISMATCH')
    if 'torch' in sys.modules:
        raise ValueError('NATIVE_PRECHECK_IMPORTED_TORCH')
    files = verify_model(Path('/model'), load(PROFILE / recipe['model']['manifest']), full_model_hash)
    return {'state': 'PASS_RUNTIME_IDENTITY_ONLY_NOT_MODEL_ACCEPTANCE', 'python_sources': count,
            'runtime_artifacts': len(lock['workspace_artifacts']) + len(lock['api_artifacts']) + 1, 'model_files': files,
            'model_content_check': 'FULL_SHA256' if full_model_hash else 'SIZE_ONLY_NOT_CONTENT',
            'native_torch_imported': 'torch' in sys.modules, 'api_torch_imported': api_versions['torch_imported']}


def verify_idle_cards(info):
    cards = re.findall(r'\|\s*(\d+)\s+910B1\s*\|\s*OK\s*\|', info)
    if sorted(map(int, cards)) != list(range(8)) or info.count('No running processes found') != 8:
        raise ValueError('REQUIRE_EIGHT_HEALTHY_IDLE_910B1_CARDS')


def idle_preflight(port, master_port):
    verify_idle_cards(subprocess.check_output(['/usr/local/bin/npu-smi', 'info'], text=True))
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    if int(memory['MemAvailable'].split()[0]) * 1024 < 700 * 1024**3:
        raise ValueError('THIS_PROFILE_REQUIRES_700_GIB_AVAILABLE_HOST_MEMORY')
    for port_value in (port, master_port):
        with socket.socket() as candidate:
            candidate.bind(('127.0.0.1', port_value))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--plan', action='store_true', help='print fixed arguments only; no runtime reads or writes')
    action.add_argument('--check-only', action='store_true', help='check files and metadata; do not import Torch or start workers')
    action.add_argument('--serve', action='store_true', help='explicitly start the native API after identity and idle checks')
    parser.add_argument('--source', default='/workspace/src/deepinfer-runtime')
    parser.add_argument('--api-python', default='/workspace/envs/api/bin/python')
    parser.add_argument('--port', type=int, default=8900)
    parser.add_argument('--master-port', type=int, default=29500)
    parser.add_argument('--full-model-hash', action='store_true', help='read every model payload during identity checks')
    args = parser.parse_args(argv)
    recipe = load(PROFILE / 'recipe.json')
    plan = build_plan(recipe, args.source, args.api_python, args.port, args.master_port)
    if args.plan:
        print(json.dumps(plan, indent=2))
        return
    source = Path(args.source).resolve()
    plan = build_plan(recipe, str(source), args.api_python, args.port, args.master_port)
    environment = {**os.environ, **plan['env']}
    environment['LD_LIBRARY_PATH'] = plan['library_path_prefix'] + ':' + os.environ.get('LD_LIBRARY_PATH', '')
    result = check_runtime(recipe, source, args.api_python, environment, args.full_model_hash)
    print(json.dumps(result), flush=True)
    if not args.serve:
        return
    idle_preflight(args.port, args.master_port)
    for name in ('generated', 'home', 'tmp', 'cache/xdg', 'cache/hf', 'cache/jit/tilelang', 'cache/torch-extensions'):
        (WORKSPACE / name).mkdir(parents=True, exist_ok=True)
    worker = Path(plan['worker'])
    expected = worker_script(str(source))
    if worker.exists():
        if worker.read_text(encoding='utf-8') != expected or not os.access(worker, os.X_OK):
            raise ValueError('EXISTING_WORKER_SCRIPT_DIFFERS_OR_IS_NOT_EXECUTABLE')
    else:
        with worker.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(expected)
        worker.chmod(0o700)
    os.execvpe(args.api_python, plan['argv'], environment)


if __name__ == '__main__':
    main()
