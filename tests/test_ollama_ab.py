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
        old={'status':'failed','model_ids':{'x':'a'},'inference_failed':True}
        new=dict(old,status='passed')
        self.assertEqual(ab.conclusion([old,new]),'candidate_passes_baseline_inference_fails')
        self.assertEqual(ab.conclusion([new,new]),'inconclusive_baseline_not_reproduced')
        self.assertEqual(ab.conclusion([dict(old,inference_failed=False),new]),'inconclusive_setup')
        self.assertEqual(ab.conclusion([old,dict(new,model_ids={'x':'b'})]),'inconclusive_model_drift')
        self.assertEqual(ab.conclusion([old,old]),'candidate_failed')
