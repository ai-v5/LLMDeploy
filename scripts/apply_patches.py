"""Apply hash-locked patches to the disposable container layer, never host CANN."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

REPO=Path(__file__).resolve().parents[1]

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def apply(root,manifest):
    operations=[]
    for row in manifest['patches']:
        tree=(root/row['tree']).resolve()
        target=(tree/row['path']).resolve()
        assert tree.is_relative_to(root.resolve()) and target.is_relative_to(tree),'PATCH_PATH_ESCAPE'
        patch=REPO/'patches'/row['patch']
        assert sha(patch)==row['patch_sha256'],'PATCH_BYTES_CHANGED'
        current=sha(target)
        assert current in [row['before_sha256'],row['after_sha256']],'SOURCE_VERSION_MISMATCH'
        if current==row['before_sha256']:
            subprocess.run(['git','-c','core.autocrlf=false','-c','core.eol=lf','-C',str(tree),'apply','--check',str(patch)],check=True)
            operations.append((tree,target,patch,row))
    for tree,target,patch,row in operations:
        subprocess.run(['git','-c','core.autocrlf=false','-c','core.eol=lf','-C',str(tree),'apply',str(patch)],check=True)
        assert sha(target)==row['after_sha256'],'PATCH_RESULT_MISMATCH'
    return len(operations)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('/vllm-workspace'))
    args=p.parse_args();m=json.loads((REPO/'patches/manifest.json').read_text(encoding='utf-8'))
    print(json.dumps({'state':'PASS_PATCHES','applied':apply(args.root,m)}))
