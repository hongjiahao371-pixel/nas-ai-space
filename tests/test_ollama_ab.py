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
