import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import render
import verify_model
import apply_patches

class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.host=render.load(ROOT/'hosts/950dt8.example.json')
        self.recipe=render.load(render.RECIPE)

    def test_exact_launch_contract(self):
        args=render.command(self.host,self.recipe,repo_path='/srv/LLMDeploy',optional_devices=['/dev/uburma/urma0'],optional_mounts=[])
        self.assertEqual(args[:3],['docker','run','--detach'])
        self.assertNotIn('--privileged',args)
        self.assertEqual(args.count('--device'),12)
        self.assertIn('ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7',args)
        script=args[-1]
        for token in ['--max-model-len 1048576','--data-parallel-size 8','--tensor-parallel-size 1','--enable-expert-parallel','--enable-prefix-caching','--swa-bounded-replay','--enable-prompt-tokens-details','apply_patches.py']:
            self.assertIn(token,script)
        context_index=self.recipe['vllm_args'].index('--max-model-len')+1
        self.assertEqual(self.recipe['client_limits']['contextWindow'],int(self.recipe['vllm_args'][context_index]))
        self.assertEqual(self.recipe['client_limits']['maxTokens'],32768)
        limit_index=self.recipe['vllm_args'].index('--limit-mm-per-prompt')+1
        self.assertEqual(json.loads(self.recipe['vllm_args'][limit_index]),{'image':640})
        self.assertIn('--limit-mm-per-prompt',script)
        self.assertIn('{"image":640}',script)

    def test_machine_path_and_port_reconfiguration(self):
        self.host.update(cann_root='/opt/Ascend/cann-9.2.0',port=9890,model_dir='/data/model')
        args=render.command(self.host,self.recipe,repo_path='/srv/LLMDeploy',optional_devices=[],optional_mounts=[])
        self.assertIn('/data/model:/model:ro',args)
        self.assertIn('ASCEND_OPP_PATH=/opt/Ascend/cann-9.2.0/opp',args)
        self.assertIn('--port 9890',args[-1])

    def test_reject_bad_host_contract(self):
        for update in [{'devices':[0,1,2,3]},{'model_dir':'/'},{'model_dir':'/data/../model'},{'runtime_dir':self.host['model_dir']},{'image_id':'latest'},{'port':True},{'unknown':'value'}]:
            with self.subTest(update=update):
                host={**self.host,**update}
                with self.assertRaises(AssertionError):render.validate(host,self.recipe)

    def test_patch_artifacts_are_frozen(self):
        m=json.loads((ROOT/'patches/manifest.json').read_text())
        self.assertEqual(len(m['patches']),2)
        for p in m['patches']:
            self.assertEqual(hashlib.sha256((ROOT/'patches'/p['patch']).read_bytes()).hexdigest(),p['patch_sha256'])

    def test_patch_refuses_wrong_source_before_writing(self):
        m=json.loads((ROOT/'patches/manifest.json').read_text())
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for row in m['patches']:
                target=root/row['tree']/row['path'];target.parent.mkdir(parents=True);target.write_text('wrong version\n')
            with self.assertRaises(AssertionError):apply_patches.apply(root,m)
            self.assertEqual((root/m['patches'][0]['tree']/m['patches'][0]['path']).read_text(),'wrong version\n')

    def test_model_rejects_same_size_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'weights').write_bytes(b'good')
            m={'files':[{'path':'weights','size':4,'sha256':hashlib.sha256(b'good').hexdigest()}]}
            self.assertEqual(verify_model.verify(root,m,True),1)
            (root/'weights').write_bytes(b'evil')
            with self.assertRaises(AssertionError):verify_model.verify(root,m,True)

    def test_model_manifest_pinned(self):
        m=json.loads((ROOT/'recipes/deepseek-v4.1-flash-1017/model-files.json').read_text())
        self.assertEqual(m['revision'],self.recipe['model']['revision'])
        self.assertEqual(len(m['files']),83)
        self.assertEqual(sum(r['path'].endswith('.safetensors') for r in m['files']),48)

    def test_model_cli_uses_explicit_manifest_and_checks_content(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'weights').write_bytes(b'good')
            manifest=root/'manifest.json'
            manifest.write_text(json.dumps({'revision':'test-revision','files':[
                {'path':'weights','size':4,'sha256':hashlib.sha256(b'good').hexdigest()}
            ]}),encoding='utf-8')
            command=[sys.executable,str(ROOT/'scripts/verify_model.py'),'--root',str(root),
                     '--manifest',str(manifest),'--full']
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {'state':'PASS_FULL_HASH','files':1,'revision':'test-revision'})
            (root/'weights').write_bytes(b'evil')
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('MODEL_SHA256_MISMATCH',result.stderr)

    def test_publishable_record_and_project_license(self):
        record=json.loads((ROOT/'records/950dt8-1017.json').read_text())
        for field in ['host_reference','active_container_reference','configuration_source_sha256']:
            self.assertNotIn(field,record)
        license=(ROOT/'LICENSE').read_text(encoding='utf-8')
        self.assertIn('GNU AFFERO GENERAL PUBLIC LICENSE',license)
        self.assertIn('Version 3, 19 November 2007',license)
        self.assertIn('AGPL-3.0-only',(ROOT/'README.md').read_text(encoding='utf-8'))
        self.assertIn('Apache License',(ROOT/'licenses/Apache-2.0.txt').read_text(encoding='utf-8'))

if __name__=='__main__':unittest.main()
