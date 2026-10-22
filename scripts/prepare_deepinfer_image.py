"""Create a new public-source overlay build context; never build or run Docker."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy_deepinfer.config import RECIPE, ROOT, load


def prepare(source, output):
    recipe = load(RECIPE)
    files = {}
    for name, expected in recipe['source']['runtime_files_sha256'].items():
        result = subprocess.run(['git', '-C', str(source), 'show', recipe['source']['commit'] + ':' + name], capture_output=True)
        if result.returncode or hashlib.sha256(result.stdout).hexdigest() != expected:
            raise ValueError('PINNED_PUBLIC_SOURCE_UNAVAILABLE:' + name)
        files[name] = result.stdout
    license_file = subprocess.run(['git', '-C', str(source), 'show', recipe['source']['commit'] + ':LICENSE'], capture_output=True, check=True).stdout
    output.mkdir(parents=True, exist_ok=False)
    for name, raw in files.items():
        path = output / 'source' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (output / 'LICENSE.DeepInfer').write_bytes(license_file)
    (output / 'Dockerfile').write_bytes((ROOT / 'images/deepinfer/Dockerfile').read_bytes())
    return output


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    print('PREPARED_ONLY: ' + str(prepare(a.source, a.output)))
