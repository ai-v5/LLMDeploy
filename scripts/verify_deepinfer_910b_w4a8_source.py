"""Check the source-only 910B W4A8 patch set and normalized Python files."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / 'recipes/a2/910b/deepinfer/w4a8-source/source-files-w4a8.json'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def checked_relative(name):
    path = PurePosixPath(name)
    if '\\' in name or ':' in name or path.is_absolute() or not path.parts or any(part in ('', '.', '..') for part in path.parts):
        raise ValueError('Invalid manifest path')
    return path


def verify_python(source, expected):
    root = source.resolve(strict=True)
    issues = []
    actual = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name != '.git' and not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if name.endswith('.py'):
                actual.add(path.relative_to(root).as_posix())
    for name in sorted(expected):
        relative = checked_relative(name)
        path = root.joinpath(*relative.parts)
        if name not in actual or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            issues.append('missing_or_unsafe:' + name)
            continue
        if sha(path.read_bytes().replace(b'\r\n', b'\n')) != expected[name]:
            issues.append('sha256:' + name)
    issues.extend('unexpected:' + name for name in sorted(actual - set(expected)))
    return issues


def verify_bundle(repo, manifest):
    issues = []
    if manifest.get('scope') != 'SOURCE_ONLY_NOT_A_DEPLOYMENT_RECIPE':
        issues.append('scope')
    for item in [manifest['baseline_manifest'], *manifest['patches']]:
        relative = checked_relative(item['path'])
        path = repo.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(repo.resolve()) or sha(path.read_bytes()) != item['sha256']:
            issues.append('bundle_sha256:' + item['path'])
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Upstream source after applying the three patches')
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_bytes())
    issues = verify_bundle(REPO, manifest) + verify_python(args.source, manifest['python_sha256_lf'])
    print(json.dumps({'status': 'SOURCE_ONLY_PYTHON_HASHES_PASS' if not issues else 'SOURCE_ONLY_PYTHON_HASHES_FAIL',
                      'python_files': len(manifest['python_sha256_lf']), 'issues': issues,
                      'scope': 'Normalized Python and patch hashes only; native builds and model inference are not checked.'}))
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
