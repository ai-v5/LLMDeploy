"""Assemble four 910B CCE sources from a separately generated TBE baseline.

The baseline and its generated metadata are not provided by this repository.
This helper does not compile, install, or validate a device or model.
"""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECIPE = ROOT / 'recipes/a2/910b/deepinfer/w4a8-source'
BODY_MANIFEST = RECIPE / 'native-body-inputs.json'
BUILD_MANIFEST = RECIPE / 'native-build-inputs.json'
BODIES = RECIPE / 'native-bodies'
MARKER = b'struct BaseTlv {'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def assemble(baseline, body_dir, body_manifest, expected_sources):
    if sha(baseline) != body_manifest['ara_base_sha256']:
        raise RuntimeError('TBE baseline checksum mismatch')
    if baseline.count(MARKER) != 1:
        raise RuntimeError('TBE baseline metadata marker mismatch')
    symbol = body_manifest['ara_base_symbol'].encode('ascii')
    footer = baseline[baseline.index(MARKER):]
    if footer.count(symbol) != 2:
        raise RuntimeError('TBE baseline metadata symbol mismatch')
    result = {}
    root = body_dir.resolve(strict=True)
    for output, item in body_manifest['bodies'].items():
        filename = item['file']
        if Path(output).name != output or not output.endswith('.cce') or Path(filename).name != filename:
            raise ValueError('Invalid native body path')
        path = root / filename
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise RuntimeError('Missing or unsafe body: ' + filename)
        body = path.read_bytes()
        if (sha(body) != item['sha256'] or not body.endswith(b'\n') or body.endswith(b'\n\n') or
                MARKER in body or b'.ascend.meta' in body):
            raise RuntimeError('Native body identity mismatch: ' + filename)
        new_symbol = output.removesuffix('.cce').encode('ascii') + b'__kernel0'
        generated = body + b'\n' + footer.replace(symbol, new_symbol)
        if sha(generated) != expected_sources[output]:
            raise RuntimeError('Assembled source checksum mismatch: ' + output)
        result[output] = generated
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tbe-ara-cce', type=Path, required=True, help='Pinned TBE-generated ARA baseline source')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check-only', action='store_true', help='Validate the four reconstructed identities without writing')
    group.add_argument('--output', type=Path, help='New output directory')
    args = parser.parse_args()
    body_manifest = json.loads(BODY_MANIFEST.read_bytes())
    build_manifest = json.loads(BUILD_MANIFEST.read_bytes())
    if body_manifest.get('scope') != 'FOUR_BODY_IDENTITIES_ONLY_NOT_A_DEPLOYMENT_RECIPE':
        raise RuntimeError('Unexpected body manifest scope')
    expected = build_manifest['source_sha256']
    required = {'indexer_schedule_rows_r1264.cce', 'indexer_gather_r722.cce',
                'indexer_select_r760.cce', 'indexer_prefill_gather_r798.cce'}
    if set(body_manifest['bodies']) != required or not required <= set(expected):
        raise RuntimeError('Body manifest is incomplete')
    baseline_path = args.tbe_ara_cce
    if baseline_path.is_symlink() or not baseline_path.is_file() or baseline_path.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeError('TBE baseline must be a bounded regular file')
    outputs = assemble(baseline_path.read_bytes(), BODIES, body_manifest, expected)
    if not args.check_only:
        if args.output.is_symlink() or args.output.exists():
            raise FileExistsError('Output already exists')
        output = args.output.resolve()
        body_root = BODIES.resolve()
        if output == body_root or output.is_relative_to(body_root):
            raise RuntimeError('Output must be outside the body directory')
        output.mkdir(parents=True, exist_ok=False)
        for name, raw in outputs.items():
            with (output / name).open('xb') as stream:
                stream.write(raw)
    print(json.dumps({'status': 'FOUR_CCE_IDENTITIES_MATCH_NOT_COMPILED_OR_DEVICE_ADMITTED',
                      'sources': len(outputs), 'written': not args.check_only}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
