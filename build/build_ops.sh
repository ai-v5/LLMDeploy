#!/usr/bin/env bash
# CPU-only container build. Host CANN and dependency inputs are mounted read-only.
set -eo pipefail
source "$ASCEND_TOOLKIT_HOME/set_env.sh"
python3 - <<'PY'
import hashlib,io,json,subprocess,tarfile
from pathlib import Path
meta=json.loads(Path('/deployment/build/dependencies.json').read_text())
wheel=Path('/deps')/meta['framework_wheel']
assert hashlib.file_digest(wheel.open('rb'),'sha256').hexdigest()==meta['framework_wheel_sha256']
archive=Path('/deps')/meta['raw_archive']
if archive.exists():
    assert hashlib.file_digest(archive.open('rb'),'sha256').hexdigest()==meta['raw_archive_sha256']
    stream=tarfile.open(archive)
else:
    source='/deps/source'
    data=subprocess.check_output(['git','-c','safe.directory='+source,'-C',source,'archive',meta['source_revision']])
    stream=tarfile.open(fileobj=io.BytesIO(data))
target=Path('/build/source');target.mkdir(exist_ok=False)
with stream:
    for member in stream.getmembers():
        assert member.isfile() or member.isdir(),'SOURCE_SPECIAL_ENTRY'
    stream.extractall(target,filter='data')
assert (target/'net/native_package/build.py').is_file()
Path('/build/input-facts.json').write_text(json.dumps({'source_revision':meta['source_revision'],'framework_wheel_sha256':meta['framework_wheel_sha256']}))
PY
python3 -m pip install --no-index --no-deps /deps/cannbot_dsl-0.7.1-cp312-cp312-manylinux_2_28_aarch64.whl
python3 /build/source/net/native_package/build.py \
  --operator mqsmla qli qsli --target dav-3510 \
  --output-root /build/output --work-root /build/work
set -- /build/output/operators/mqsmla__qli__qsli/wheels/*.whl
test "$#" = 1 && test -f "$1"
python3 -m pip install --no-index --no-deps "$1"
python3 - <<'PY'
import importlib,importlib.metadata,json
from pathlib import Path
names=['mixed_quant_sparse_flash_mla','mixed_quant_sparse_flash_mla_metadata','quant_lightning_indexer_dsl','quant_lightning_indexer_metadata_dsl','quant_sparse_lightning_indexer_dsl','quant_sparse_lightning_indexer_metadata_dsl']
assert importlib.metadata.version('cannbot-dsl')=='0.7.1'
assert importlib.metadata.version('cannbot-arena-net')=='0.3.0'
for name in names:importlib.import_module('ops.'+name)
Path('/build/result.json').write_text(json.dumps({'state':'PASS_BUILD_AND_IMPORT_ONLY','operators':names,'inference_run':False}))
print('PASS_BUILD_AND_IMPORT_ONLY')
PY
