import importlib.util
import unittest
from pathlib import Path
import httpx

spec=importlib.util.spec_from_file_location('vector_memory',Path(__file__).parents[1]/'scripts/vector-memory.py')
memory=importlib.util.module_from_spec(spec);spec.loader.exec_module(memory)

class VectorMemoryTests(unittest.TestCase):
    def test_fingerprint_covers_every_page_payload_and_vector(self):
        rows=[{'id':1,'vector':[0.1,0.2],'payload':{'file_id':3}},
              {'id':2,'vector':[0.3,0.4],'payload':{'file_id':4}}]
        def handler(request):
            import json
            body=json.loads(request.content)
            self.assertTrue(body['with_payload']);self.assertTrue(body['with_vector'])
            offset=body.get('offset')
            return httpx.Response(200,json={'result':{'points':[rows[1 if offset else 0]],'next_page_offset':None if offset else 2}})
        with httpx.Client(base_url='http://qdrant',transport=httpx.MockTransport(handler)) as client:
            baseline=memory.fingerprint(client,'test')
            self.assertEqual(baseline['points'],2)
            self.assertEqual(memory.fingerprint(client,'test'),baseline)
            rows[1]['payload']['file_id']=5
            self.assertNotEqual(memory.fingerprint(client,'test'),baseline)
            rows[1]['payload']['file_id']=4;rows[0]['vector'][0]=0.9
            self.assertNotEqual(memory.fingerprint(client,'test'),baseline)

    def test_failed_scroll_never_produces_partial_success(self):
        with httpx.Client(base_url='http://qdrant',transport=httpx.MockTransport(lambda r:httpx.Response(503))) as client:
            with self.assertRaises(httpx.HTTPStatusError):memory.fingerprint(client,'test')

    def test_rollback_preserves_mixed_original_storage_settings(self):
        info={'config':{'params':{'vectors':{'on_disk':True},'on_disk_payload':False},'hnsw_config':{'on_disk':True}}}
        self.assertEqual(memory.rollback_patch(info),{'vectors':{'':{'on_disk':True}},'hnsw_config':{'on_disk':True},'params':{'on_disk_payload':False}})
        self.assertNotIn('size',memory.ram_patch()['vectors'][''])
        self.assertNotIn('distance',memory.ram_patch()['vectors'][''])

if __name__=='__main__':unittest.main()
