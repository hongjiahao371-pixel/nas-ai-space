import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('ab',Path(__file__).parents[1]/'scripts/test-ollama-ab.py')
ab=importlib.util.module_from_spec(spec);spec.loader.exec_module(ab)
class ComparisonTests(unittest.TestCase):
    def test_only_native_intel_amx_eligible(self):
        cpu={'vendor_id':'GenuineIntel','flags':'amx_tile amx_int8 avx2'}
        self.assertTrue(ab.eligible('Linux','x86_64',cpu))
        for system,machine,c in [('Darwin','x86_64',cpu),('Linux','aarch64',cpu),('Linux','x86_64',dict(cpu,vendor_id='AuthenticAMD')),('Linux','x86_64',dict(cpu,flags='avx2'))]:
            self.assertFalse(ab.eligible(system,machine,c))
    def test_model_identity_required(self):
        models=[{'name':n,'digest':'a'*64} for n in ab.MODELS]
        self.assertEqual(len(ab.identities(models)),2)
        for broken in [[],models[:1],models+models, [dict(m,digest='latest') for m in models]]:
            with self.assertRaises(ValueError):ab.identities(broken)
    def test_fail_closed_classification(self):
        old={'status':'failed','model_ids':{'x':'a'},'inference_failed':True,'known_embedding_crash':True}
        new=dict(old,status='passed')
        self.assertEqual(ab.conclusion([old,new]),'candidate_passes_baseline_inference_fails')
        self.assertEqual(ab.conclusion([new,new]),'inconclusive_baseline_not_reproduced')
        self.assertEqual(ab.conclusion([dict(old,inference_failed=False),new]),'inconclusive_setup')
        self.assertEqual(ab.conclusion([old,dict(new,model_ids={'x':'b'})]),'inconclusive_model_drift')
        self.assertEqual(ab.conclusion([old,old]),'candidate_failed')

    def test_known_crash_requires_exact_embedding_http_fingerprint(self):
        e=dict(method='POST',path='/api/embed',model=ab.MODELS[0],status=500,response_body='runner terminated: signal: segmentation fault')
        self.assertTrue(ab.known_embedding_crash([e]))
        self.assertTrue(ab.known_embedding_crash([dict(e,response_body='SIGSEGV')]))
        for key,value in [('path','/api/generate'),('model',ab.MODELS[1]),('status',504),('response_body','connection timeout'),('method','GET')]:
            self.assertFalse(ab.known_embedding_crash([dict(e,**{key:value})]))
        old=dict(status='failed',model_ids={'x':'a'},inference_failed=True,known_embedding_crash=False)
        self.assertEqual(ab.conclusion([old,dict(old,status='passed')]),'inconclusive_different_baseline_failure')

    def test_cleanup_failure_blocks_comparison_even_if_baseline_already_failed(self):
        old=dict(status='failed',model_ids={'x':'a'},inference_failed=True,known_embedding_crash=True)
        new=dict(old,status='passed')
        for phases in [[dict(old,cleanup_errors=['remove failed'])],
                       [dict(old,cleanup_errors=['remove failed']),new],
                       [old,dict(new,cleanup_errors=['collect failed'])]]:
            self.assertEqual(ab.conclusion(phases),'inconclusive_cleanup')

    def test_ps_projection_is_bounded_and_redacts_unexpected_data(self):
        secret='https://example.invalid/?token=SECRET'
        row=dict(name=ab.MODELS[0],model='registry.ollama.ai/library/'+ab.MODELS[0],digest='a'*64,
                 size=123,size_vram=522977280,context_length=4096,token=secret,url=secret,details={'secret':secret})
        result=ab.safe_process_sample({'models':[row]*9,'token':secret})
        self.assertEqual(len(result['models']),8)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['models'][0],{k:row[k] for k in ('name','model','digest','size','size_vram','context_length')})
        self.assertNotIn('SECRET',str(result))
        for key in ('name','model','digest','size','size_vram','context_length'):
            result=ab.safe_process_sample({'models':[dict(row,**{key:secret})]})
            self.assertNotIn('SECRET',str(result))
        self.assertEqual(ab.safe_process_sample({'models':None}),{'models_valid':False})
        self.assertEqual(ab.safe_process_sample({'models':[None]})['models'],[{'valid':False}])
        for value in (True,-1,2**63,1.5,None):
            self.assertEqual(ab.safe_process_sample({'models':[{'size_vram':value}]})['models'][0]['size_vram'],'[invalid]')

    def test_same_single_ps_sample_preserves_original_cpu_gate(self):
        from unittest.mock import patch
        trial=object.__new__(ab.LockedAcceptance)
        trial.report={'checks':{}}
        trial.version='0.30.8'
        trial.cpu_request=None
        trial.save=lambda:None
        cases=[([{'name':ab.MODELS[0],'size_vram':0}],True),
               ([{'name':ab.MODELS[0],'size_vram':522977280}],False),
               ([{'name':'different','size_vram':0}],False),
               ([],False),([{'name':ab.MODELS[0]}],False),
               ([{'name':ab.MODELS[0],'size_vram':0}]*2,False)]
        for models,passes in cases:
            response={'models':models}
            with patch.object(ab.original.OllamaAcceptance,'request',return_value=response) as request:
                if passes: trial.check_cpu('http://127.0.0.1',ab.MODELS[0])
                else:
                    with self.assertRaisesRegex(ValueError,'loaded entirely on CPU'):
                        trial.check_cpu('http://127.0.0.1',ab.MODELS[0])
                request.assert_called_once_with('http://127.0.0.1','/api/ps')
                self.assertIs(request.return_value,response)
        self.assertEqual(len(trial.report['cpu_process_samples']),2)
        self.assertEqual(trial.report['cpu_process_samples'][1]['models'][0]['size_vram'],522977280)

class CPUProofTests(unittest.TestCase):
    def setUp(self):
        self.info={'HostConfig':{'Privileged':False,'Runtime':'runc','PidMode':'','IpcMode':'private','UsernsMode':'','UTSMode':'','CgroupnsMode':'private','CapDrop':['ALL']},'Mounts':[{'Type':'volume'}]}
        self.boundary=ab.proof.container_cpu_boundary(self.info,'/dev/null\n/dev/zero')
        self.log='''msg="inference compute" id=cpu library=cpu
msg="starting llama-server" cmd="llama-server -ngl 0 --model synthetic"
load_tensors: AMX model buffer size = 498.75 MiB
load_tensors: CPU model buffer size = 157.62 MiB
llama_context: CPU compute buffer size = 12.0 MiB
'''
        self.response={'models':[{'name':ab.MODELS[0],'model':ab.MODELS[0],'digest':'a'*64,'size_vram':522977280}]}
    def run_proof(self,**overrides):
        args=dict(version='0.32.3',model=ab.MODELS[0],digest='a'*64,response=self.response,full_log=self.log,
                  request_log=self.log,isolation=self.boundary,num_gpu=0)
        args.update(overrides)
        return ab.proof.amx_cpu_proof(**args)
    def test_exact_amx_accounting_and_repeated_buffer_replacement(self):
        self.assertEqual(self.run_proof()['amx_bytes'],522977280)
        stamped=''.join('2026-11-19T12:00:00.123456789Z '+line+'\n' for line in self.log.splitlines())
        self.assertEqual(self.run_proof(request_log=stamped)['amx_bytes'],522977280)
        self.assertEqual(self.run_proof(request_log=self.log+self.log.splitlines()[2]+'\n')['amx_bytes'],522977280)
        log=self.log+'clip_model_load: AMX model buffer size = 1.0 MiB\n'
        response={'models':[dict(self.response['models'][0],size_vram=524025856)]}
        self.assertEqual(self.run_proof(request_log=log,response=response)['amx_bytes'],524025856)
    def test_identity_and_accounting_never_accept_arbitrary_vram(self):
        for key,value in [('name','wrong'),('model','wrong'),('digest','b'*64),('size_vram',0),('size_vram',1),('size_vram',True)]:
            with self.assertRaises(ValueError):self.run_proof(response={'models':[dict(self.response['models'][0],**{key:value})]})
        for options in [dict(version='0.30.8'),dict(num_gpu=-1),dict(num_gpu=False),dict(isolation=None),dict(response={'models':[]}),dict(response={'models':self.response['models']*2})]:
            with self.assertRaises(ValueError):self.run_proof(**options)
    def test_missing_unknown_gpu_and_multiple_runner_logs_rejected(self):
        logs=['',self.log.replace('-ngl 0','-ngl 1'),self.log+self.log,
              self.log.replace('AMX model','CUDA0 model'),self.log.replace('498.75','498.76'),
              self.log+'mystery: new buffer size = 5 MiB\n',self.log+'offloaded 1/28 layers to GPU\n',
              self.log+'using device CUDA0 (GPU)\n']
        for log in logs:
            with self.assertRaises(ValueError):self.run_proof(request_log=log)
        with self.assertRaises(ValueError):self.run_proof(full_log=self.log.replace('id=cpu library=cpu','id=0 library=cuda'))
    def test_container_boundary_rejects_gpu_and_privilege_escape_paths(self):
        for key,value in [('Privileged',True),('CapAdd',['SYS_ADMIN']),('IpcMode','host'),('UsernsMode','host'),('UTSMode','host'),('CgroupnsMode','host'),('Devices',''),('Runtime','nvidia'),('Devices',[{}]),('DeviceRequests',[{}]),
                          ('DeviceCgroupRules',['c *:* rwm']),('Binds',['/dev:/dev']),('VolumesFrom',['other']),('CapDrop',[]),('PidMode','host')]:
            info=dict(self.info,HostConfig=dict(self.info['HostConfig'],**{key:value}))
            with self.assertRaises(ValueError):ab.proof.container_cpu_boundary(info,'/dev/null')
        for nodes in ['', '/dev/null\n/dev/nvidia0','/dev/null\n/dev/dri/renderD128','/dev/null\n/dev/kfd']:
            with self.assertRaises(ValueError):ab.proof.container_cpu_boundary(self.info,nodes)
        with self.assertRaises(ValueError):ab.proof.container_cpu_boundary(dict(self.info,Mounts=[{'Type':'bind'}]),'/dev/null')

    def test_wrapper_proves_same_ps_sample_and_does_not_erase_nonzero_vram(self):
        from unittest.mock import patch
        trial=object.__new__(ab.LockedAcceptance)
        trial.version='0.32.3'; trial.model_ids={ab.MODELS[0]:'a'*64}
        trial.cpu_request={'model':ab.MODELS[0],'num_gpu':0}
        trial.cpu_isolation=self.boundary;trial.request_log_prefix=('','')
        trial.report={'checks':{}};trial.save=lambda:None
        trial.runner_logs=lambda:(self.log,'')
        trial.inspect_cpu_boundary=lambda:self.boundary
        with patch.object(ab.original.OllamaAcceptance,'request',return_value=self.response) as request:
            trial.check_cpu('http://127.0.0.1',ab.MODELS[0])
            request.assert_called_once_with('http://127.0.0.1','/api/ps')
        self.assertEqual(self.response['models'][0]['size_vram'],522977280)
        self.assertEqual(trial.report['checks']['cpu_qwen3-embedding']['method'],'amx_accounting_cpu_proof')
        trial.runner_logs=lambda:(self.log.replace('-ngl 0','-ngl 1'),'')
        with patch.object(ab.original.OllamaAcceptance,'request',return_value=self.response):
            with self.assertRaises(ValueError):trial.check_cpu('http://127.0.0.1',ab.MODELS[0])

    def test_both_versions_requests_explicit_cpu_and_preserve_vision_options(self):
        from unittest.mock import patch
        for version in ab.VERSIONS:
            trial=object.__new__(ab.LockedAcceptance)
            trial.version=version;trial.model_ids={ab.MODELS[0]:'a'*64}
            trial.report={'checks':{}};trial.save=lambda:None
            trial.inspect_cpu_boundary=lambda:self.boundary;trial.runner_logs=lambda:('','')
            payload={'model':ab.MODELS[0],'options':{'num_predict':32,'temperature':0}}
            with patch.object(ab.original.OllamaAcceptance,'request',return_value={}) as request:
                trial.request('http://127.0.0.1','/api/generate',payload=payload)
                self.assertEqual(request.call_args.kwargs['payload']['options'],{'num_predict':32,'temperature':0,'num_gpu':0})
                self.assertNotIn('num_gpu',payload['options'])
