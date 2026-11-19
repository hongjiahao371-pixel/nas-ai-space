import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import urllib.error

spec=importlib.util.spec_from_file_location('ollama_diagnostics',Path(__file__).resolve().parents[1]/'scripts/test-ollama-runtime.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class RuntimeFailureDiagnosticsTests(unittest.TestCase):
    def test_http_error_preserved_bounded_and_redacted_without_request_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            acceptance=module.OllamaAcceptance(SimpleNamespace(evidence=tmp))
            self.addCleanup(__import__('shutil').rmtree,acceptance.temporary,True)
            acceptance.token='synthetic-secret'
            exc=urllib.error.HTTPError('http://127.0.0.1/api/embed',500,'Internal Server Error',{},
                io.BytesIO(b'{"error":"synthetic-secret runner crashed"}'+b'x'*9000))
            acceptance.http=Mock();acceptance.http.open.side_effect=exc
            with self.assertRaises(urllib.error.HTTPError) as raised:
                acceptance.request('http://127.0.0.1','/api/embed?private=not-recorded',method='POST',
                    payload={'model':'qwen3-embedding:0.6b','input':['not-to-be-recorded']})
            self.assertIs(raised.exception,exc)
            evidence=json.loads((Path(tmp)/'runtime.json').read_text())
            failure=evidence['http_errors'][0]
            self.assertEqual(failure['status'],500)
            self.assertEqual(failure['model'],'qwen3-embedding:0.6b')
            self.assertTrue(failure['response_body_truncated'])
            self.assertLessEqual(len(failure['response_body']),8300)
            text=json.dumps(evidence)
            for forbidden in ['synthetic-secret','not-to-be-recorded','not-recorded']:
                self.assertNotIn(forbidden,text)
            self.assertNotEqual(evidence['status'],'passed')
            self.assertEqual(acceptance.http.open.call_count,1)

    def test_success_response_still_requires_the_existing_caller_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            acceptance=module.OllamaAcceptance(SimpleNamespace(evidence=tmp))
            self.addCleanup(__import__('shutil').rmtree,acceptance.temporary,True)
            acceptance.http=Mock()
            acceptance.http.open.return_value.__enter__=Mock(return_value=io.BytesIO(b'{"embeddings":[]}'))
            acceptance.http.open.return_value.__exit__=Mock(return_value=False)
            result=acceptance.request('http://127.0.0.1','/api/embed')
            self.assertEqual(result,{'embeddings':[]})
            self.assertNotIn('http_errors',acceptance.report)
            self.assertNotEqual(acceptance.report['status'],'passed')
            # The unchanged acceptance run checks count/dimension/nonzero vectors.
            self.assertEqual(acceptance.http.open.call_count,1)

    def test_cpu_summary_does_not_capture_serial_or_unrelated_proc_fields(self):
        result=module.host_cpu_summary('model name : Intel Test\nflags : avx2 amx_int8\nSerial : secret\nprocessor : 0\nmodel name : duplicate')
        self.assertEqual(result,{'model name':'Intel Test','flags':'avx2 amx_int8'})
