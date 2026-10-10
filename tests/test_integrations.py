from __future__ import annotations
import base64, io, json, unittest
from dataclasses import replace
from unittest.mock import Mock, patch
import httpx
from PIL import Image
from tests import test_improvements as fixtures
from app.services.local_ai import LocalAIClient
from integrations.jarvis.ai_space_client import AISpaceClient, AISpaceError, execute_ai_space, TOOLS

class VisionIntegrationTests(unittest.TestCase):
    setUp = fixtures.ImprovementsTests.setUp
    tearDown = fixtures.ImprovementsTests.tearDown

    def test_image_preparation_strips_metadata_and_calls_interactive_gate(self):
        ai = LocalAIClient(replace(self.cfg, vision_model='vision'))
        image = Image.new('RGB', (2000, 1000), 'red'); data = io.BytesIO(); image.save(data, 'PNG')
        response = Mock(); response.json.return_value = {'choices':[{'message':{'content':'红色画面'}}]}
        with patch.object(ai, '_validate_endpoint', return_value='http://127.0.0.1:1'), patch.object(ai, '_post_json', return_value=response) as post:
            self.assertEqual(ai.analyze_image(data.getvalue(), '什么颜色？'), '红色画面')
            payload=post.call_args.args[1]
            raw=base64.b64decode(payload['messages'][1]['content'][1]['image_url']['url'].split(',')[1])
            with Image.open(io.BytesIO(raw)) as prepared:
                self.assertEqual(prepared.size, (1280,640)); self.assertFalse(prepared.getexif())
        self.assertEqual(list(self.cfg.cache_dir.iterdir()), [])

    def test_invalid_images_never_call_model(self):
        ai=LocalAIClient(replace(self.cfg, vision_model='vision'))
        gif=io.BytesIO(); Image.new('RGB',(10,10)).save(gif,'GIF')
        with patch.object(ai, '_post_json') as model:
            for data in [b'', b'bad', b'x'*(8*1024*1024+1), gif.getvalue()]:
                with self.assertRaises(ValueError): ai.analyze_image(data,'分析图片')
            model.assert_not_called()

class IntegrationApiTests(unittest.TestCase):
    setUp = fixtures.ApiImprovementsTests.setUp
    tearDown = fixtures.ApiImprovementsTests.tearDown

    def test_auth_and_capabilities_no_secret(self):
        self.assertEqual(self.client.get('/api/integrations/capabilities').status_code,401)
        result=self.client.get('/api/integrations/capabilities',headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertNotIn(self.cfg.api_token,result.text)
        self.assertFalse(result.json()['vision']['enabled'])
        self.assertEqual(self.client.post('/api/integrations/vision',content=b'bad').status_code,401)

    def test_vision_returns_without_media_or_task_mutation(self):
        before=self.db.fetchone('SELECT COUNT(*) n FROM files')['n']
        with patch.object(self.main.state.ai,'analyze_image',return_value='红色图片') as model:
            result=self.client.post('/api/integrations/vision?question=什么颜色',content=b'raw',headers=self.headers)
            self.assertEqual(result.status_code,200,result.text)
            self.assertFalse(result.json()['stored']); model.assert_called_once_with(b'raw','什么颜色')
        self.assertEqual(self.db.fetchone('SELECT COUNT(*) n FROM files')['n'],before)
        self.assertEqual(self.db.fetchone('SELECT COUNT(*) n FROM tasks')['n'],0)

    def test_body_question_and_model_errors_are_bounded(self):
        with patch.object(self.main.state.ai,'analyze_image',side_effect=ValueError('bad')):
            self.assertEqual(self.client.post('/api/integrations/vision',content=b'bad',headers=self.headers).status_code,400)
        with patch.object(self.main.state.ai,'analyze_image',side_effect=RuntimeError('private backend detail')):
            r=self.client.post('/api/integrations/vision',content=b'raw',headers=self.headers)
            self.assertEqual(r.status_code,503); self.assertNotIn('private backend',r.text)
        with patch.object(self.main.state.ai,'analyze_image') as model:
            self.assertEqual(self.client.post('/api/integrations/vision',content=b'x'*(8*1024*1024+1),headers=self.headers).status_code,413)
            self.assertEqual(self.client.post('/api/integrations/vision?question='+('x'*1001),content=b'raw',headers=self.headers).status_code,422)
            model.assert_not_called()
        self.main._integration_vision_slot.acquire()
        try:
            r=self.client.post('/api/integrations/vision',content=b'raw',headers=self.headers)
            self.assertEqual(r.status_code,503); self.assertEqual(r.headers['retry-after'],'5')
        finally:self.main._integration_vision_slot.release()

class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_format_no_token_in_output_and_zero_timestamp(self):
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(200,json={'results':[{'id':3,'name':'视频','kind':'video','match_time':0,'confidence':.99,'evidence':'x'*3000}]})
        client=AISpaceClient(token='secret',transport=httpx.MockTransport(handler))
        try:
            result=await execute_ai_space(client,TOOLS[0]['name'],{'query':'红色汽车'})
            self.assertEqual(result['results'][0]['match_time'],0)
            self.assertEqual(len(result['results'][0]['evidence']),600)
            self.assertNotIn('secret',json.dumps(result)); self.assertNotIn('confidence',result['results'][0])
            self.assertEqual(seen[0].headers['authorization'],'Bearer secret')
            self.assertEqual(seen[0].url.params['precise'],'false')
            for args in [{'query':'图片','url':'http://other'}, {'query':'图片','limit':True}]:
                with self.assertRaises(ValueError): await execute_ai_space(client,TOOLS[0]['name'],args)
        finally: await client.close()

    async def test_auth_failure_has_no_false_not_found_or_credentials(self):
        client=AISpaceClient(token='secret',transport=httpx.MockTransport(lambda _:httpx.Response(401,text='private error')))
        try:
            with self.assertRaises(AISpaceError) as result: await client.search('照片')
            self.assertIn('凭据',str(result.exception)); self.assertNotIn('secret',str(result.exception))
        finally: await client.close()

    async def test_ask_and_vision_contracts(self):
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200,json={'answer':'有依据的回答','sources':[{'id':1,'name':'资料','kind':'document'}],'stored':False})
        client=AISpaceClient(token='secret',transport=httpx.MockTransport(handler))
        try:
            answer=await client.ask('资料说明什么',[1],kind='document')
            self.assertEqual(answer['sources'][0]['file_id'],1)
            self.assertEqual(json.loads(requests[0].content)['file_ids'],[1])
            vision=await client.analyze_image(b'image','图上是什么')
            self.assertFalse(vision['stored']); self.assertEqual(requests[1].content,b'image')
            with self.assertRaises(ValueError): await client.ask('问题',[True])
        finally: await client.close()
