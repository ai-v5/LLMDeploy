"""Render an explicit Linux Docker start command; never execute a deployment."""
import argparse
import glob
import json
from pathlib import Path,PurePosixPath
import re
import shlex

REPO=Path(__file__).resolve().parents[1]
RECIPE=REPO/'recipes/deepseek-v4.1-flash-1017/recipe.json'

def load(path):return json.loads(path.read_text(encoding='utf-8'))

def validate(host,recipe):
    expected={'schema','container_name','model_dir','runtime_dir','artifacts_dir','cann_root','port','devices','image_id'}
    assert set(host)==expected,'UNKNOWN_OR_MISSING_HOST_FIELDS'
    assert host['schema']==recipe['schema']==1
    assert re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]+',host['container_name'])
    assert re.fullmatch(r'sha256:[0-9a-f]{64}',host['image_id'])
    assert isinstance(host['port'],int) and not isinstance(host['port'],bool) and 1024<=host['port']<=65535
    assert host['devices']==list(range(8)),'RECIPE_REQUIRES_PHYSICAL_DEVICES_0_TO_7'
    for key in ['model_dir','runtime_dir','artifacts_dir','cann_root']:
        value=host[key];p=PurePosixPath(value)
        assert p.is_absolute() and '..' not in p.parts and value!='/' and ':' not in value and '\n' not in value and '\r' not in value
    dirs=[PurePosixPath(host[k]) for k in ['model_dir','runtime_dir','artifacts_dir']]
    assert len(set(dirs))==3 and not any(a.is_relative_to(b) for a in dirs for b in dirs if a!=b),'HOST_DIRS_OVERLAP'

def command(host,recipe,repo_path=None,optional_devices=None,optional_mounts=None):
    validate(host,recipe)
    root=str(repo_path or REPO.resolve())
    assert root.startswith('/') and ':' not in root and '\n' not in root and '\r' not in root,'RENDER_ON_TARGET_LINUX_HOST'
    env={**recipe['env'],'ASCEND_RT_VISIBLE_DEVICES':','.join(map(str,host['devices'])),
         'HF_HOME':'/runtime/huggingface','XDG_CACHE_HOME':'/runtime/cache','TORCHINDUCTOR_CACHE_DIR':'/runtime/torchinductor','VLLM_NO_USAGE_STATS':'1','DO_NOT_TRACK':'1'}
    old=env['ASCEND_TOOLKIT_HOME']
    for k in ['ASCEND_TOOLKIT_HOME','ASCEND_HOME_PATH','ASCEND_OPP_PATH','LD_LIBRARY_PATH']:
        env[k]=env[k].replace(old,host['cann_root'])
    args=['docker','run','--detach','--name',host['container_name'],'--label','llmdeploy.recipe='+recipe['id'],
          '--network','host','--shm-size','256g','--ulimit','memlock=-1:-1','--cap-add','IPC_LOCK','--entrypoint','/bin/bash']
    mounts=[(root,'/deployment','ro'),(host['model_dir'],'/model','ro'),(host['runtime_dir'],'/runtime',None),(host['artifacts_dir'],'/artifacts',None),(host['cann_root'],host['cann_root'],'ro')]
    for path in ['/usr/local/dcmi','/usr/local/Ascend/driver/lib64','/usr/local/Ascend/driver/topo','/usr/local/Ascend/driver/version.info','/etc/ascend_install.info','/usr/local/Ascend/driver/tools/hccn_tool','/usr/local/bin/npu-smi']:
        mounts.append((path,path,'ro'))
    if optional_mounts is None:optional_mounts=[p for p in ['/etc/hixlep','/lib/route.conf','/etc/hccl_rootinfo.json'] if Path(p).exists()]
    for p in optional_mounts:mounts.append((p,p,'ro'))
    for source,target,mode in mounts:args+=['-v',source+':'+target+(':'+mode if mode else '')]
    for k,v in env.items():args+=['-e',k+'='+v]
    devices=['/dev/davinci'+str(i) for i in host['devices']]+['/dev/davinci_manager','/dev/hisi_hdc','/dev/ummu/tid']
    if optional_devices is None:optional_devices=glob.glob('/dev/uburma/*')+(['/dev/devmm_svm'] if Path('/dev/devmm_svm').exists() else [])
    for p in devices+sorted(optional_devices):args+=['--device',p]
    vllm=recipe['vllm_args'].copy();vllm[vllm.index('--port')+1]=str(host['port'])
    script='source "$ASCEND_TOOLKIT_HOME/set_env.sh" && python3 /deployment/scripts/apply_patches.py && exec '+shlex.join(['vllm',*vllm])
    return args+[host['image_id'],'--noprofile','--norc','-c',script]

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();host=load(a.host);cmd=command(host,load(RECIPE))
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x',encoding='utf-8',newline='\n') as f:
        f.write('#!/usr/bin/env bash\nset -eo pipefail\n# Explicit operator action: this starts a new container; it never stops an existing one.\n')
        f.write('mkdir -p -- '+shlex.join([host['runtime_dir'],host['artifacts_dir']])+'\n')
        f.write(shlex.join(cmd)+'\n')
    print(json.dumps({'state':'RENDERED_ONLY','output':str(a.output)}))
