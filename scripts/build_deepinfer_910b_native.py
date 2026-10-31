"""Build pinned 910B indexer artifacts from separately supplied native sources.

This helper does not provide the source inputs, deploy a model, or validate a
device. Its own outputs use a new directory and partial outputs are retained.
Dependencies can write caches; use an isolated environment when needed.
"""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / 'recipes/a2/910b/deepinfer/w4a8-source/native-build-inputs.json'
KERNELS = {
    'weighted_gather128_r664': '_weighted_gather910b/gather128.o',
    'weighted_gather1_r664': '_weighted_gather910b/gather1.o',
    'indexer_schedule_rows_r1264': '_native_prepare910b/indexer_schedule_rows_r1264.o',
    'indexer_gather_r722': '_native_prepare910b/indexer_gather_r722.o',
    'indexer_select_r760': '_native_prepare910b/indexer_select_r760.o',
    'indexer_prefill_gather_r798': '_native_prefill_gather910b/prefill_gather.o',
}
BRIDGES = {
    'deepinfer_weighted_gather_r664': ('weighted_gather_binding_r664.cpp', '_weighted_gather910b/gather_bridge.so'),
    'deepinfer_indexer_prepare_rows_r1264': ('indexer_prepare_binding_rows_r1264.cpp', '_native_prepare910b/deepinfer_indexer_prepare_rows_r1264.so'),
    'deepinfer_prefill_gather_r827': ('indexer_prefill_gather_binding_r827.cpp', '_native_prefill_gather910b/prefill_gather_bridge.so'),
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_sources(source, expected):
    root = source.resolve(strict=True)
    for name, digest in expected.items():
        if Path(name).name != name or not isinstance(digest, str):
            raise ValueError('Invalid source manifest entry')
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise RuntimeError('Missing or unsafe source: ' + name)
        if sha(path) != digest:
            raise RuntimeError('Source checksum mismatch: ' + name)
    return root


def verify_cxx(expected):
    configured = os.environ.get('CXX')
    compiler = shutil.which(configured) if configured else None
    if not compiler or sha(Path(compiler).resolve(strict=True)) != expected:
        raise RuntimeError('CXX does not identify the pinned compiler')


def new_output(source, output):
    path = output.resolve()
    if path == source or path.is_relative_to(source):
        raise RuntimeError('Output must be outside the source directory')
    path.mkdir(parents=True, exist_ok=False)
    return path


def kernel_phase(source, output, toolchain):
    # Keep TBE compilation in a separate interpreter from Torch registration.
    from tbe.common.platform import set_current_compile_soc_info
    from tbe.common.buildcfg import build_config
    from tbe.tvm.contrib import ccec

    compiler = shutil.which('ccec')
    if (not compiler or sha(Path(compiler).resolve(strict=True)) != toolchain['ccec_sha256'] or
            sha(Path(ccec.__file__).resolve(strict=True)) != toolchain['tbe_ccec_module_sha256']):
        raise RuntimeError('CANN compiler does not match the pinned identities')
    set_current_compile_soc_info('Ascend910B1', 'VectorCore')
    rows = []
    for name, relative in KERNELS.items():
        dest = output / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or (output / (name + '-compiler.log')).exists():
            raise RuntimeError('Kernel output already exists: ' + name)
        with build_config(enable_vector_core=True):
            command = ccec._build_aicore_compile_cmd(str(source / (name + '.cce')), str(dest), name=name)
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        with (output / (name + '-compiler.log')).open('xb') as log:
            log.write(result.stdout)
        if result.returncode != 0 or not dest.read_bytes().startswith(b'\x7fELF'):
            raise RuntimeError('Kernel compilation failed: ' + name)
        rows.append({'name': name, 'source_sha256': sha(source / (name + '.cce')),
                     'output': relative, 'output_sha256': sha(dest), 'command': command})
    if 'torch' in sys.modules or 'torch_npu' in sys.modules:
        raise RuntimeError('Unexpected Torch import during kernel compilation')
    with (output / 'kernel-build.json').open('x', encoding='utf8', newline='\n') as stream:
        json.dump({'soc': 'Ascend910B1', 'core': 'VectorCore', 'kernels': rows}, stream, indent=2)


def bridge_phase(source, output, cann, toolchain):
    import torch
    import torch_npu
    from torch.utils.cpp_extension import load

    packages = {name: importlib.metadata.version(name) for name in ('torch', 'torch-npu')}
    if packages != {'torch': toolchain['torch'], 'torch-npu': toolchain['torch_npu']} or not torch.compiled_with_cxx11_abi():
        raise RuntimeError('Unexpected Torch, torch-npu or C++ ABI')
    if torch_npu.version.git_version != toolchain['torch_npu_git_version']:
        raise RuntimeError('Unexpected torch-npu source identity')
    if torch.npu.is_initialized():
        raise RuntimeError('NPU was initialized before bridge compilation')
    npu = Path(torch_npu.__file__).parent
    header = npu / 'include/torch_npu/csrc/core/npu/NPUGraphsUtils.h'
    if sha(header) != toolchain['capture_header_sha256']:
        raise RuntimeError('Unexpected torch-npu graph header')
    for name, (filename, relative) in BRIDGES.items():
        build = output / ('build-' + name)
        build.mkdir(exist_ok=False)
        load(name=name, sources=[str(source / filename)],
             extra_include_paths=[str(npu / 'include'), str(cann / 'include')],
             extra_cflags=['-O2', '-std=c++17', '-D_GLIBCXX_USE_CXX11_ABI=1'],
             extra_ldflags=['-L' + str(npu / 'lib'), '-ltorch_npu', '-L' + str(cann / 'lib64'), '-lacl_rt', '-lascendcl'],
             build_directory=str(build), with_cuda=False, is_python_module=False, verbose=True)
        dest = output / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open('xb') as stream:
            stream.write((build / (name + '.so')).read_bytes())
    if torch.npu.is_initialized():
        raise RuntimeError('NPU initialized during bridge compilation')
    return {'packages': packages, 'torch_npu_git_version': torch_npu.version.git_version,
            'capture_header_sha256': sha(header), 'cann_root': str(cann)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, required=True, help='Directory containing the nine pinned native source files')
    parser.add_argument('--output', type=Path, help='New output directory, outside the source directory')
    parser.add_argument('--check-inputs', action='store_true', help='Only check source identities; do not compile')
    parser.add_argument('--cann-root', type=Path, default=Path('/usr/local/Ascend/cann-9.1.0'))
    parser.add_argument('--internal-kernels', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_bytes())
    sources = {name + '.cce' for name in KERNELS} | {item[0] for item in BRIDGES.values()}
    outputs = set(KERNELS.values()) | {item[1] for item in BRIDGES.values()}
    if (manifest.get('scope') != 'NATIVE_INPUT_IDENTITIES_ONLY_NOT_A_DEPLOYMENT_RECIPE' or
            set(manifest['source_sha256']) != sources or set(manifest['artifact_sha256']) != outputs):
        raise RuntimeError('Native input manifest is incomplete')
    source = verify_sources(args.source_dir, manifest['source_sha256'])
    if args.check_inputs:
        if args.output or args.internal_kernels:
            raise RuntimeError('--check-inputs cannot create an output')
        print(json.dumps({'status': 'NATIVE_SOURCE_IDENTITIES_PASS_NOT_COMPILED', 'sources': len(sources)}))
        return 0
    if not args.output:
        parser.error('--output is required unless --check-inputs is used')
    verify_cxx(manifest['toolchain']['cxx_sha256'])
    if args.internal_kernels:
        output = args.output.resolve(strict=True)
        if not output.is_dir() or output == source or output.is_relative_to(source):
            raise RuntimeError('Kernel phase requires the new build directory')
        kernel_phase(source, output, manifest['toolchain'])
        verify_sources(source, manifest['source_sha256'])
        return 0
    output = new_output(source, args.output)
    result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--source-dir', str(source),
                             '--output', str(output), '--internal-kernels'])
    if result.returncode:
        raise RuntimeError('Kernel compilation failed; preserve partial output for inspection')
    verify_sources(source, manifest['source_sha256'])
    bridge = bridge_phase(source, output, args.cann_root.resolve(), manifest['toolchain'])
    verify_sources(source, manifest['source_sha256'])
    artifacts = {name: {'sha256': sha(output / name), 'matches_historical': sha(output / name) == expected}
                 for name, expected in manifest['artifact_sha256'].items()}
    matching = all(item['matches_historical'] for item in artifacts.values())
    report = {'status': 'CPU_BYTE_IDENTITIES_MATCH_NOT_DEVICE_ADMISSION' if matching else 'CPU_BUILD_IDENTITIES_DIFFER_NOT_ADMITTED',
              'bridge': bridge, 'kernel_build': json.loads((output / 'kernel-build.json').read_bytes()),
              'artifacts': artifacts, 'scope': 'Compilation and byte identities only; no model or device validation.'}
    with (output / 'build-result.json').open('x', encoding='utf8', newline='\n') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps({'status': report['status'], 'artifacts': len(artifacts)}))
    return 0 if matching else 1


if __name__ == '__main__':
    raise SystemExit(main())
