"""The public serving profile, separate from the experimental P/D work."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / 'recipes/a5/950dt/deepinfer/deepseek-v4.1-flash-1017/recipe.json'


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def validate(host, recipe):
    fields = {'schema', 'container_name', 'model_dir', 'artifacts_dir', 'warmup_file',
              'image_id', 'port', 'master_port', 'devices'}
    if set(host) != fields or host['schema'] != 1:
        raise ValueError('UNKNOWN_OR_MISSING_HOST_FIELDS')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', host['container_name']):
        raise ValueError('INVALID_CONTAINER_NAME')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', host['image_id']):
        raise ValueError('EXACT_LOCAL_IMAGE_ID_REQUIRED')
    if host['devices'] != list(range(8)) or any(type(device) is not int for device in host['devices']):
        raise ValueError('REQUIRES_PHYSICAL_DEVICES_0_TO_7')
    for name in ('port', 'master_port'):
        if type(host[name]) is not int or not 1024 <= host[name] <= 65535:
            raise ValueError('INVALID_PORT')
    if host['port'] == host['master_port']:
        raise ValueError('PORTS_OVERLAP')
    paths = []
    for name in ('model_dir', 'artifacts_dir', 'warmup_file'):
        value = host[name]
        path = PurePosixPath(value)
        if not path.is_absolute() or path == PurePosixPath('/') or '..' in path.parts or any(c in value for c in ':,\n\r'):
            raise ValueError('INVALID_HOST_PATH')
        paths.append(path)
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise ValueError('HOST_PATHS_OVERLAP')
    if recipe['engine'] != 'deepinfer' or recipe['topology'] != {'DP': 8, 'EP': 8, 'CP': 1, 'max_num_seqs_per_rank': 4}:
        raise ValueError('WRONG_SERVING_PROFILE')


def server_argv(recipe, port):
    profile = recipe['serving']
    return [recipe['paths']['api_python'], '-u', recipe['paths']['source'] + '/examples/serve_recipe.py',
            '--model', '/model', '--worker-python', recipe['paths']['worker_python'],
            '--served-model-name', recipe['model']['served_name'], '--model-alias', recipe['model']['alias'],
            '--port', str(port), '--max-model-len', str(profile['context_tokens']),
            '--max-output-tokens', str(profile['output_tokens']), '--max-images-per-request', str(profile['images']),
            '--max-num-batched-tokens', str(profile['batch_tokens']),
            '--gpu-memory-utilization', str(profile['memory_fraction']),
            '--prefill-any-interval', str(profile['prefill_interval']), '--eplb-interval', str(profile['eplb_interval']),
            '--dspark' if profile['dspark'] else '--no-dspark', '--warmup-workload', '/config/warmup.json']


def verify_runtime(recipe, source_root=None, native_path=None):
    source = Path(source_root or recipe['paths']['source'])
    for name, expected in recipe['source']['runtime_files_sha256'].items():
        # The historical image packaged CRLF source; Git and new overlay
        # contexts use LF. Accept that representation difference only.
        raw = (source / name).read_bytes().replace(b'\r\n', b'\n')
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('RUNTIME_SOURCE_MISMATCH:' + name)
    native = Path(native_path or source / recipe['source']['native_path'])
    if hashlib.sha256(native.read_bytes()).hexdigest() != recipe['source']['native_sha256']:
        raise ValueError('NATIVE_BINARY_MISMATCH_REVALIDATION_REQUIRED')
