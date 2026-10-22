"""Check the pinned model files; full mode streams checksums without loading NPU."""
import argparse
import hashlib
import json
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]

def verify(root,manifest,full):
    for row in manifest['files']:
        path=(root/row['path']).resolve()
        assert path.is_relative_to(root.resolve()) and path.is_file(),'MODEL_FILE_MISSING_OR_OUTSIDE_ROOT'
        assert path.stat().st_size==row['size'],'MODEL_SIZE_MISMATCH'
        if not full:continue
        if row.get('sha256'):
            h=hashlib.sha256()
            with path.open('rb') as stream:
                while block:=stream.read(8*1024*1024):h.update(block)
            actual=h.hexdigest()
            assert actual==row['sha256'],'MODEL_SHA256_MISMATCH'
        else:
            h=hashlib.sha1();h.update(('blob '+str(row['size'])+'\0').encode())
            with path.open('rb') as stream:
                while block:=stream.read(8*1024*1024):h.update(block)
            assert h.hexdigest()==row['git_blob_sha1'],'MODEL_GIT_BLOB_MISMATCH'
    return len(manifest['files'])

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--full',action='store_true')
    p.add_argument('--manifest',type=Path,default=REPO/'recipes/deepseek-v4.1-flash-1017/model-files.json',
                   help='Pinned file manifest; defaults to the historical original-precision checkpoint')
    a=p.parse_args()
    m=json.loads(a.manifest.read_text(encoding='utf-8'))
    print(json.dumps({'state':'PASS_FULL_HASH' if a.full else 'PASS_SIZE_ONLY_NOT_CONTENT','files':verify(a.root,m,a.full),'revision':m['revision']}))
