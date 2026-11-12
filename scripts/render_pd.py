"""Render one role of the isolated A5 P/D candidate; never start a service."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shlex

RECIPE = Path(__file__).resolve().parents[1] / 'recipes/deepseek-v4.1-flash-1017-pd/recipe.json'


def vllm_command(recipe, role, port, kv_port, rpc_port, node_ip, api_host='127.0.0.1'):
    if role not in ('prefill', 'decode'):
        raise ValueError('INVALID_PD_ROLE')
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in (port, kv_port, rpc_port)):
        raise ValueError('INVALID_PORT')
    if len({port, kv_port, rpc_port}) != 3:
        raise ValueError('OVERLAPPING_PORTS')
    for layout in (recipe['topology'][name] for name in ('prefill', 'decode')):
        if layout['TP'] * layout['DP'] != 8 or layout['PP'] != 1 or layout['CP'] != 1:
            raise ValueError('PD_REQUIRES_EIGHT_DEVICES_WITH_PP1_CP1')
    if recipe['topology']['prefill']['TP'] < recipe['topology']['decode']['TP']:
        raise ValueError('MOONCAKE_REQUIRES_PREFILL_TP_AT_LEAST_DECODE_TP')
    layout = recipe['topology'][role]
    transfer = {
        'kv_connector': recipe['connector'],
        'kv_role': 'kv_producer' if role == 'prefill' else 'kv_consumer',
        'kv_port': kv_port,
        'engine_id': 'pd-' + role,
        'kv_connector_extra_config': {
            name: {'dp_size': recipe['topology'][name]['DP'], 'tp_size': recipe['topology'][name]['TP']}
            for name in ('prefill', 'decode')
        },
    }
    common = recipe['common_args'].copy()
    common[common.index('--host') + 1] = api_host
    return ['vllm', *common, *recipe[role + '_args'],
            '--tensor-parallel-size', str(layout['TP']),
            '--data-parallel-size', str(layout['DP']),
            '--data-parallel-size-local', str(layout['DP']),
            '--port', str(port), '--data-parallel-address', node_ip,
            '--data-parallel-rpc-port', str(rpc_port),
            '--kv-transfer-config', json.dumps(transfer, separators=(',', ':'))]


def command(recipe, role, *, port, kv_port, rpc_port, node_ip, interface, model_dir, runtime_dir, name,
            optional_devices=(), optional_mounts=(), api_host='127.0.0.1'):
    for value in (model_dir, runtime_dir):
        path = PurePosixPath(value)
        if not path.is_absolute() or '..' in path.parts or value == '/' or any(c in value for c in ':\n\r'):
            raise ValueError('INVALID_LOCAL_PATH')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]+', name):
        raise ValueError('INVALID_CONTAINER_NAME')
    if PurePosixPath(runtime_dir).is_relative_to(PurePosixPath(model_dir)) or PurePosixPath(model_dir).is_relative_to(PurePosixPath(runtime_dir)):
        raise ValueError('OVERLAPPING_MODEL_RUNTIME')
    env = {**recipe['env'], 'ASCEND_RT_VISIBLE_DEVICES': '0,1,2,3,4,5,6,7',
           'VLLM_HOST_IP': node_ip, 'HCCL_IF_IP': node_ip,
           'GLOO_SOCKET_IFNAME': interface, 'HCCL_SOCKET_IFNAME': interface,
           'TP_SOCKET_IFNAME': interface, 'XDG_CACHE_HOME': '/runtime/cache',
           'HOME': '/runtime/home', 'HF_HOME': '/runtime/huggingface',
           'CANNBOTDSL_CACHE_DIR': '/runtime/cannbot-dsl'}
    args = ['docker', 'run', '--detach', '--name', name,
            '--label', 'llmdeploy.recipe=' + recipe['id'], '--network', 'host',
            '--shm-size', '256g', '--ulimit', 'memlock=-1:-1', '--cap-add', 'IPC_LOCK',
            '--entrypoint', '/bin/bash', '-v', model_dir + ':/model:ro',
            '-v', runtime_dir + ':/runtime', '-e', 'VLLM_API_KEY']
    for path in ['/usr/local/dcmi', '/usr/local/Ascend/driver/lib64', '/usr/local/Ascend/driver/topo',
                 '/usr/local/Ascend/driver/version.info', '/etc/ascend_install.info']:
        args += ['-v', path + ':' + path + ':ro']
    for path in ['/dev/davinci' + str(i) for i in range(8)] + ['/dev/davinci_manager', '/dev/hisi_hdc', '/dev/ummu/tid']:
        args += ['--device', path]
    for path in optional_devices:
        args += ['--device', path]
    for path in optional_mounts:
        args += ['-v', path + ':' + path + ':ro']
    for k, v in env.items():
        args += ['-e', k + '=' + v]
    # The Docker client inherits the independent key by name; no value is rendered.
    script = ('test -n "${VLLM_API_KEY:-}" && '
              'mkdir -p /runtime/home /runtime/cache /runtime/cannbot-dsl && '
              'export LD_LIBRARY_PATH="/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common:'
              '/usr/local/Ascend/driver/lib64:/usr/lib64:/usr/lib64/urma:/usr/local/lib:${LD_LIBRARY_PATH:-}" && '
              'source /usr/local/Ascend/ascend-toolkit/set_env.sh && exec ' +
              shlex.join(vllm_command(recipe, role, port, kv_port, rpc_port, node_ip, api_host)))
    image = (recipe['image']['runtime_image_ids'].get(role, '')
             if 'runtime_image_ids' in recipe['image'] else recipe['image']['image_id'])
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('PINNED_RUNTIME_IMAGE_REQUIRED')
    return args + [image, '--noprofile', '--norc', '-c', script]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    recipe = json.loads(RECIPE.read_text(encoding='utf-8'))
    host = json.loads(args.host.read_text(encoding='utf-8'))
    rendered = command(recipe, **host)
    with args.output.open('x', encoding='utf-8', newline='\n') as f:
        f.write('#!/usr/bin/env bash\nset -euo pipefail\n' + shlex.join(rendered) + '\n')
    print(json.dumps({'state': 'RENDERED_CANDIDATE_ONLY', 'output': str(args.output)}))
