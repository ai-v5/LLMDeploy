# SPDX-License-Identifier: AGPL-3.0-only
"""Validate this pinned overlay's inputs, or emit CPU-only installed fingerprints."""
import argparse
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parent


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check_source_archive(lock, path):
    if file_hash(path) != lock['raw_archive_sha256']:
        raise ValueError('SOURCE_ARCHIVE_HASH_MISMATCH')
    with tarfile.open(path) as archive:
        license_bytes = archive.extractfile('LICENSE').read()
    digest = hashlib.sha256(license_bytes).hexdigest()
    if digest != lock['license']['source_license_sha256']:
        raise ValueError('UPSTREAM_LICENSE_HASH_MISMATCH')
    return {'archive_sha256': file_hash(path), 'archive_bytes': path.stat().st_size,
            'source_license_sha256': digest}


def check_operator_files(names, lock):
    names = set(names)
    profiles = {operator: set() for operator in lock['expected_native_profiles']}
    for name in names:
        fields = name.split('/')
        if name.startswith('ops/_native/operators/') and '/variants/' in name:
            operator, variant = fields[3], fields[5]
            if operator not in profiles:
                raise ValueError('UNEXPECTED_NATIVE_OPERATOR')
            profiles[operator].add(variant)
    if {key: len(value) for key, value in profiles.items()} != lock['expected_native_profiles']:
        raise ValueError('NATIVE_PROFILE_COUNTS_MISMATCH')
    for operator, variants in profiles.items():
        for variant in variants:
            prefix = f'ops/_native/operators/{operator}/variants/{variant}/'
            if not all(prefix + name in names for name in ('provider.so', 'provider.so.meta.json', 'variant.json')):
                raise ValueError('INCOMPLETE_NATIVE_PROFILE')
    libraries = {Path(name).name for name in names if name.startswith('ops/_aicpu/') and name.endswith('.so')}
    if libraries != set(lock['expected_aicpu_libraries']):
        raise ValueError('AICPU_LIBRARIES_MISMATCH')
    if not all(f'ops/{name}.py' in names for name in lock['import_modules']):
        raise ValueError('OPERATOR_PYTHON_MODULE_MISSING')
    return {key: len(value) for key, value in profiles.items()}


def check_inputs(lock, framework_dir, operator_dir, license_file):
    framework = framework_dir / lock['framework_wheel']
    operator = operator_dir / lock['operator_wheel']
    if file_hash(framework) != lock['framework_wheel_sha256']:
        raise ValueError('FRAMEWORK_WHEEL_HASH_MISMATCH')
    if file_hash(license_file) != lock['license']['source_license_sha256']:
        raise ValueError('UPSTREAM_LICENSE_HASH_MISMATCH')
    with zipfile.ZipFile(operator) as wheel:
        profiles = check_operator_files(wheel.namelist(), lock)
    return framework, operator, {'framework_sha256': file_hash(framework), 'operator_sha256': file_hash(operator),
                                 'operator_hash_is_historical_reference': file_hash(operator) == lock['reference_operator_wheel_sha256'],
                                 'native_profiles': profiles}


def installed_fingerprints(lock):
    if list(Path('/dev').glob('davinci*')):
        raise ValueError('CPU_VALIDATION_MUST_HAVE_NO_NPU_DEVICES')
    for name in lock['import_modules']:
        importlib.import_module('ops.' + name)
    result = {}
    for package, version in [('cannbot-dsl', '0.7.1'), ('cannbot-arena-net', lock['operator_version'])]:
        distribution = importlib.metadata.distribution(package)
        if distribution.version != version:
            raise ValueError('INSTALLED_VERSION_MISMATCH')
        files = {}
        for relative in distribution.files or []:
            if str(relative).endswith('.pyc') or '__pycache__' in str(relative):
                continue
            path = Path(distribution.locate_file(relative))
            if path.is_file():
                files[str(relative)] = {'bytes': path.stat().st_size, 'sha256': file_hash(path)}
        if not files:
            raise ValueError('EMPTY_INSTALLED_DISTRIBUTION')
        if package == 'cannbot-arena-net':
            check_operator_files(files, lock)
        result[package] = {'version': version, 'files': files,
                           'sha256': hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=ROOT / 'dependencies.json')
    parser.add_argument('--install-only', action='store_true')
    parser.add_argument('--check-source-archive', type=Path)
    parser.add_argument('--framework-dir', type=Path, default=Path('/deps'))
    parser.add_argument('--operator-dir', type=Path, default=Path('/operator'))
    parser.add_argument('--license-file', type=Path, default=Path('/usr/local/share/licenses/cannbot-arena-net/LICENSE'))
    args = parser.parse_args()
    lock = json.loads(args.lock.read_text(encoding='utf-8'))
    if args.check_source_archive:
        if args.install_only:
            parser.error('source archive verification and installation are separate steps')
        print(json.dumps({'state': 'SOURCE_ARCHIVE_AND_LICENSE_IDENTITIES_VERIFIED_NOT_BUILT_NOT_NPU',
                          **check_source_archive(lock, args.check_source_archive)}))
        return
    if args.install_only:
        framework, operator, facts = check_inputs(lock, args.framework_dir, args.operator_dir, args.license_file)
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--no-deps', str(framework), str(operator)], check=True)
        (ROOT / 'installed-inputs.json').write_text(json.dumps(facts, indent=2), encoding='utf-8')
        print(json.dumps({'state': 'INSTALLED_ONLY_IMPORT_AND_NPU_VALIDATION_PENDING', **facts}))
    else:
        print(json.dumps({'state': 'CPU_IMPORT_FINGERPRINT_ONLY_NOT_NPU_VERIFIED',
                          'installed_distributions': installed_fingerprints(lock), 'npu_devices': [], 'inference_run': False}))


if __name__ == '__main__':
    main()
