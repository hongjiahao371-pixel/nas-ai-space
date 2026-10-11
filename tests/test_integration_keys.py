from __future__ import annotations
import unittest
from unittest.mock import patch
from tests import test_improvements as fixtures
from app.services.integration_keys import allowed_request
from app.security import hash_password

class IntegrationKeyTests(unittest.TestCase):
    setUp = fixtures.ApiImprovementsTests.setUp
    tearDown = fixtures.ApiImprovementsTests.tearDown

    def key(self):
        response = self.client.post('/api/integrations/keys', headers=self.headers,
            json={'name':'Jarvis', 'library_ids':[self.lib['id']]})
        self.assertEqual(response.status_code,201,response.text)
        data=response.json()
        return data, {'Authorization':'Bearer '+data['token']}

    def test_database_and_list_never_persist_or_repeat_raw_secret(self):
        data, headers=self.key()
        rows=self.main.state.database.fetchall('SELECT * FROM integration_keys')
        self.assertNotIn(data['token'],str(rows))
        result=self.client.get('/api/integrations/keys',headers=self.headers)
        self.assertNotIn(data['token'],result.text)
        self.assertNotIn('token_hash',result.text)
        self.assertNotIn(data['token'],str(self.main.state.database.fetchall('SELECT * FROM audit_log')))
        capabilities=self.client.get('/api/integrations/capabilities',headers=headers)
        self.assertEqual(capabilities.status_code,200)
        self.assertTrue(capabilities.json()['credential']['readonly'])
        self.assertEqual(capabilities.json()['credential']['library_ids'],[self.lib['id']])
        self.assertIn('no-store',result.headers['cache-control'])

    def test_denies_administration_personal_data_and_all_media_mutations(self):
        data, headers=self.key()
        for path in ['/api/users','/api/operations/status','/api/integrations/keys','/api/conversations','/api/files','/api/auth/me','/api/libraries','/api/files/1/similar']:
            with self.subTest(path=path):self.assertEqual(self.client.get(path,headers=headers).status_code,403)
        for method,path,body in [('POST','/api/uploads',None),('DELETE','/api/files/1',None),('POST','/api/operations/backups',{}),('POST','/api/files/1/multimodal/retry',{}),('POST','/api/integrations/keys',{'name':'escalate','library_ids':[self.lib['id']]})]:
            with self.subTest(path=path):
                # Dependency authorization precedes route payload handling.
                response=self.client.request(method,path,headers=headers,json=body)
                self.assertIn(response.status_code,[403,404,405])
        self.assertEqual(self.client.post('/api/ask',headers=headers,json={'question':'查资料','conversation_id':1}).status_code,403)

    def test_library_scope_applies_to_detail_content_preview_search_and_ask(self):
        data, headers=self.key()
        self.assertEqual(self.client.get('/api/files/'+str(self.files[0]['id']),headers=headers).status_code,200)
        other=self.library/'private';other.mkdir()
        library=self.main.state.database.create_library('private',str(other))
        self.main.state.database.execute('UPDATE files SET library_id=? WHERE id=?',(library['id'],self.files[1]['id']))
        for suffix in ['', '/content','/thumbnail','/moments','/frame?time=0']:
            self.assertEqual(self.client.get('/api/files/'+str(self.files[1]['id'])+suffix,headers=headers).status_code,404)
        self.assertEqual(self.client.get('/api/search',headers=headers,params={'q':'图片','library_id':library['id']}).status_code,404)
        with patch.object(self.main.state.search,'search',return_value={'results':[]}) as search:
            before=self.main.state.database.fetchone('SELECT COUNT(*) n FROM conversations')['n']
            response=self.client.post('/api/ask',headers=headers,json={'question':'查找图片'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(search.call_args.args[3],[self.lib['id']])
            self.assertEqual(before,self.main.state.database.fetchone('SELECT COUNT(*) n FROM conversations')['n'])
        self.assertEqual(self.client.post('/api/ask',headers=headers,json={'question':'查找图片','file_ids':[self.files[1]['id']]}).status_code,404)
        self.main.state.database.execute('UPDATE libraries SET enabled=0 WHERE id=?',(self.lib['id'],))
        self.assertEqual(self.client.get('/api/files/'+str(self.files[0]['id']),headers=headers).status_code,404)

    def test_search_uses_native_scope_without_expanding_all_library_files(self):
        _,headers=self.key()
        with patch.object(self.main.state.search,'search',return_value={'results':[]}) as search:
            response=self.client.get('/api/search',headers=headers,params={'q':'红色雨伞','kind':'image','media_first':'true','precise':'false'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(search.call_args.args[3],[self.lib['id']])
            self.assertIsNone(search.call_args.kwargs['filter_sql'])
            self.assertTrue(search.call_args.kwargs['media_first'])
            response=self.client.get('/api/search',headers=headers,params={'q':'红色雨伞','date_from':'2026-01-01'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertIn('>= ?',search.call_args.kwargs['filter_sql'][0])

    def test_expiry_revocation_and_member_cannot_create_keys(self):
        data, headers=self.key()
        self.main.state.database.execute("UPDATE integration_keys SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",(data['id'],))
        self.assertEqual(self.client.get('/api/integrations/capabilities',headers=headers).status_code,401)
        data,headers=self.key()
        self.assertEqual(self.client.delete('/api/integrations/keys/'+str(data['id']),headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/api/integrations/capabilities',headers=headers).status_code,401)
        password='Member-password-2468'
        created=self.client.post('/api/users',headers=self.headers,json={'username':'member','display_name':'member','password':password,'role':'member','library_ids':[self.lib['id']]})
        self.assertEqual(created.status_code,201,created.text)
        login=self.client.post('/api/auth/login',json={'username':'member','password':password})
        member={'Authorization':'Bearer '+login.json()['token']}
        self.assertEqual(self.client.post('/api/integrations/keys',headers=member,json={'name':'bad','library_ids':[self.lib['id']]}).status_code,403)

    def test_empty_disabled_and_missing_library_cannot_be_authorized(self):
        for ids in [[],[999],[-1]]:
            response=self.client.post('/api/integrations/keys',headers=self.headers,json={'name':'test','library_ids':ids})
            self.assertIn(response.status_code,[400,422])
        self.main.state.database.execute('UPDATE libraries SET enabled=0 WHERE id=?',(self.lib['id'],))
        self.assertEqual(self.client.post('/api/integrations/keys',headers=self.headers,json={'name':'test','library_ids':[self.lib['id']]}).status_code,400)

    def test_allowlist_fails_closed_for_future_and_encoded_routes(self):
        for method,path in [('GET','/api/new-admin-function'),('DELETE','/api/files/1/content'),('POST','/api/search'),('GET','/api/files/1/../../users'),('GET','/api/files/0'),('GET','/api/files/1/ticket')]:
            self.assertFalse(allowed_request(method,path))
        for method,path in [('GET','/api/files/1/frame'),('POST','/api/integrations/vision'),('POST','/api/search/image')]:self.assertTrue(allowed_request(method,path))
