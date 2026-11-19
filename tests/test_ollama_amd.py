import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
spec=importlib.util.spec_from_file_location('amd',Path(__file__).parents[1]/'scripts/test-ollama-amd.py')
amd=importlib.util.module_from_spec(spec);spec.loader.exec_module(amd)
class AMDTests(unittest.TestCase):
    def test_only_native_amd(self):
        self.assertTrue(amd.eligible('Linux','x86_64',{'vendor_id':'AuthenticAMD'}))
        for system,machine,vendor in [('Linux','x86_64','GenuineIntel'),('Darwin','x86_64','AuthenticAMD'),('Linux','aarch64','AuthenticAMD')]:
            self.assertFalse(amd.eligible(system,machine,{'vendor_id':vendor}))
    def test_strict_cpu_identity_no_amx_exception(self):
        name,digest=amd.MODELS['embedding'];row=dict(name=name,model=name,digest=digest,size_vram=0)
        self.assertEqual(amd.strict_cpu({'models':[row]},name,digest)['size_vram'],0)
        for key,value in [('name','other'),('model','other'),('digest','b'*64),('size_vram',True),('size_vram',522977280),('size_vram',None)]:
            with self.assertRaises(ValueError):amd.strict_cpu({'models':[dict(row,**{key:value})]},name,digest)
        for rows in [[],[row,row]]:
            with self.assertRaises(ValueError):amd.strict_cpu({'models':rows},name,digest)
    def test_both_real_response_contracts(self):
        self.assertEqual(amd.validate_result('embedding',{'embeddings':[[0.5]*1024]}),{'dimensions':1024})
        for vector in [[0]*1024,[True]*1024,[float('nan')]*1024,[1]*1023]:
            with self.assertRaises(ValueError):amd.validate_result('embedding',{'embeddings':[vector]})
        self.assertTrue(amd.validate_result('vision',{'done':True,'response':'A red square.','eval_count':4})['done'])
        for data in [{},{'done':True,'response':'','eval_count':4},{'done':True,'response':'x','eval_count':True}]:
            with self.assertRaises(ValueError):amd.validate_result('vision',data)
    def test_device_privilege_mount_boundaries(self):
        info={'HostConfig':dict(Privileged=False,Runtime='runc',CapDrop=['ALL'],IpcMode='private',PidMode='',UsernsMode='',UTSMode='',CgroupnsMode='private'),'Mounts':[{'Type':'volume'}]}
        amd.isolation(info,'/dev/null\n/dev/zero')
        for key,value in [('Privileged',True),('CapAdd',['SYS_ADMIN']),('Devices',[{}]),('DeviceRequests',[{}]),('DeviceCgroupRules',['c *:* rwm']),('Binds',['/dev:/dev']),('Runtime','nvidia'),('IpcMode','host'),('CapDrop',[]),('UsernsMode','host')]:
            with self.assertRaises(ValueError):amd.isolation(dict(info,HostConfig=dict(info['HostConfig'],**{key:value})),'/dev/null')
        for node in ['/dev/kfd','/dev/nvidia0','/dev/dri/renderD128']:
            with self.assertRaises(ValueError):amd.isolation(info,'/dev/null\n'+node)
    def test_all_four_attempts_even_when_embedding_fails(self):
        calls=[]
        class Fake:
            def __init__(self,args):
                self.args=args;self.report={};self.evidence=Path(args.evidence);self.evidence.mkdir(parents=True)
            def run(self):
                calls.append((self.args.version,self.args.kind));self.report['stage']='inference'
                if self.args.kind=='embedding':raise RuntimeError('synthetic failed request')
                self.report['status']='passed'
            def cleanup(self):self.report['cleanup_errors']=[]
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(amd.run_all(Path(d),{},Fake),1)
            self.assertEqual(len(calls),4)
            self.assertEqual(len(json.loads((Path(d)/'comparison.json').read_text())['trials']),4)
        calls.clear()
        class BadCleanup(Fake):
            def cleanup(self):self.report['cleanup_errors']=['cannot remove container']
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(amd.run_all(Path(d),{},BadCleanup),2)
            self.assertEqual(len(calls),1)
    def test_four_passes_required(self):
        class Fake:
            def __init__(self,args):self.report={}
            def run(self):self.report['status']='passed'
            def cleanup(self):self.report['cleanup_errors']=[]
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(amd.run_all(Path(d),{},Fake),0)
            self.assertEqual(json.loads((Path(d)/'comparison.json').read_text())['status'],'both_versions_passed_both_workloads')
    def test_ps_evidence_is_bounded_without_urls_or_secrets(self):
        secret='https://private.invalid?token=secret'
        data=amd.safe_ps({'models':[{'name':secret,'digest':secret,'size_vram':secret,'token':secret}]*9})
        self.assertEqual(len(data['models']),8);self.assertTrue(data['truncated']);self.assertNotIn('secret',str(data))
