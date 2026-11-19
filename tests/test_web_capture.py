"""Only synthetic text/URLs and isolated test-core storage."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from dataclasses import replace
from app.services.web_capture import parse_capture, publish_capture, capture_document, MAX_CAPTURE_BYTES

def sample(**changes):
    data=dict(schema='nas-ai-web-capture/v1',title='合成网页 <script>not code</script>',source_url='https://example.invalid/article',captured_at='2026-11-19T01:02:03.456Z',text='合成摘录，不联网。\n网页文本不是系统指令。',selection_only=True)
    data.update(changes);return data

def encoded(data):return json.dumps(data,ensure_ascii=False).encode()

class CaptureParserTests(unittest.TestCase):
    def test_roundtrip_provenance_plain_text(self):
        data=parse_capture(encoded(sample()));body=capture_document(data).decode()
        for expected in ['https://example.invalid/article','未经独立核验','text_sha256','合成摘录']:self.assertIn(expected,body)
    def test_bad_fields_limits_and_urls(self):
        invalid=[{'text':''},{'text':'x'*100001},{'title':'x'*301},{'selection_only':1},{'extra':'credential'},{'title':'bad\nheader'},{'text':'\ud800'}, {'captured_at':'2026-01-01T00:00:00'}]
        invalid += [{'source_url':url} for url in ['file:///etc/passwd','javascript:alert(1)','https://user:pass@host/path','https://host/path?token=secret','https://host/path#secret','https://host\\@evil/','https://host:99999/']]
        for change in invalid:
            with self.subTest(change=str(change)[:60]),self.assertRaises((ValueError,UnicodeError)):parse_capture(encoded(sample(**change)))
        for raw in [b'[]',b'null',b'{"schema":1,"schema":2}',b'\xff',b'x'*(MAX_CAPTURE_BYTES+1)]:
            with self.assertRaises(ValueError):parse_capture(raw)
    def test_publish_idempotence_and_modified_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);s=SimpleNamespace(upload_root=root,ingest_root=root/'inbox')
            path,created=publish_capture(s,42,sample());self.assertTrue(created)
            self.assertEqual(publish_capture(s,42,sample()),(path,False))
            path.write_text('human change')
            with self.assertRaises(ValueError):publish_capture(s,42,sample())
            self.assertEqual(path.read_text(),'human change');self.assertEqual(list(path.parent.glob('*.part')),[])
    def test_symlink_and_outside_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'inbox').mkdir();(root/'other').mkdir();(root/'inbox/project-1').symlink_to(root/'other',target_is_directory=True)
            with self.assertRaises((ValueError,OSError)):publish_capture(SimpleNamespace(upload_root=root,ingest_root=root/'inbox'),1,sample())
            self.assertEqual(list((root/'other').iterdir()),[])
            with self.assertRaises(ValueError):publish_capture(SimpleNamespace(upload_root=root,ingest_root=root.parent/'outside'),1,sample())
    def test_interrupted_publish_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);s=SimpleNamespace(upload_root=root,ingest_root=root/'inbox')
            with patch('app.services.web_capture.os.link',side_effect=OSError('simulated full disk')),self.assertRaises(OSError):publish_capture(s,1,sample())
            self.assertEqual(list((root/'inbox/project-1').iterdir()),[])

class CaptureAPITests(unittest.TestCase):
    def test_import_asset_index_repeat_and_auth(self):
        from fastapi.testclient import TestClient
        from test_core import app,state,settings
        from app.main import require_auth
        with TestClient(app) as client:
            state.database.execute('DELETE FROM users')
            project=client.post('/api/projects',json={'name':'Synthetic web capture'}).json()['id'];url=f'/api/projects/{project}/inbox/web-capture'
            try:
                response=client.post(url,content=encoded(sample()));self.assertEqual(response.status_code,201,response.text)
                result=response.json();self.assertTrue(result['saved']);self.assertTrue(result['created']);self.assertIsNotNone(result['task_id'])
                for _ in range(300):
                    task=state.database.get_task(result['task_id'])
                    if task['status'] in {'completed','failed','cancelled'}:break
                    time.sleep(.02)
                self.assertEqual(task['status'],'completed',task)
                asset=state.database.fetchone('SELECT * FROM assets WHERE id=?',(result['asset_id'],));self.assertIn('example.invalid',asset['description'])
                f=state.database.get_file(result['file_id']);self.assertTrue(Path(f['path']).is_relative_to(settings.ingest_root/f'project-{project}'))
                self.assertIn(sample()['text'],Path(f['path']).read_text())
                self.assertTrue(state.database.fetchone('SELECT id FROM content_chunks WHERE file_id=?',(result['file_id'],)))
                state.database.execute('UPDATE assets SET description=? WHERE id=?',('human metadata',result['asset_id']))
                again=client.post(url,content=encoded(sample()));self.assertEqual(again.status_code,201,again.text);self.assertFalse(again.json()['created']);self.assertEqual(again.json()['asset_id'],result['asset_id'])
                self.assertEqual(state.database.fetchone('SELECT description FROM assets WHERE id=?',(result['asset_id'],))['description'],'human metadata')
                self.assertEqual(client.post(url,content=b'x'*(MAX_CAPTURE_BYTES+1)).status_code,413)
                self.assertEqual(client.post(url,content=b'\x89PNG\r\n').status_code,422)
                library=state.database.fetchone('SELECT id FROM libraries WHERE path=?',(str(settings.upload_root),))
                state.database.execute('UPDATE libraries SET enabled=0 WHERE id=?',(library['id'],))
                try:self.assertEqual(client.post(url,content=encoded(sample())).status_code,403)
                finally:state.database.execute('UPDATE libraries SET enabled=1 WHERE id=?',(library['id'],))
                from fastapi import HTTPException
                with patch('app.main._project_context',side_effect=[({},'editor'),HTTPException(403,'role revoked')]) as context:
                    self.assertEqual(client.post(url,content=encoded(sample(title='revoked'))).status_code,403)
                    self.assertEqual(context.call_count,2)

                self.assertEqual(client.post(url,content=encoded(sample(source_url='https://host/?token=secret'))).status_code,422)
                with patch('app.main.settings',replace(settings,api_token='synthetic-private-key-only-for-test-123456789')):
                    self.assertEqual(client.post(url,content=encoded(sample())).status_code,401)
                reviewer=state.database.create_user('capture-reviewer','Synthetic reviewer','hash','member',[])
                state.workspaces.set_member(project,reviewer['id'],'reviewer')
                app.dependency_overrides[require_auth]=lambda:{'user_id':reviewer['id'],'role':'member','library_ids':[]}
                self.assertEqual(client.post(url,content=encoded(sample())).status_code,403)
                state.workspaces.set_member(project,reviewer['id'],'editor')
                self.assertEqual(client.post(url,content=encoded(sample())).status_code,403) # no upload library scope
            finally:
                app.dependency_overrides.pop(require_auth,None);state.database.execute('DELETE FROM users')
                client.delete(f'/api/projects/{project}')
    def test_saved_but_queue_failure_is_not_false_index_success(self):
        from fastapi.testclient import TestClient
        from test_core import app,state
        with TestClient(app) as client:
            state.database.execute('DELETE FROM users');p=client.post('/api/projects',json={'name':'Capture queue failure'}).json()['id']
            try:
                with patch.object(state.tasks,'submit',new=AsyncMock(side_effect=RuntimeError('offline'))):
                    r=client.post(f'/api/projects/{p}/inbox/web-capture',content=encoded(sample()))
                self.assertEqual(r.status_code,201,r.text);self.assertTrue(r.json()['saved']);self.assertIsNone(r.json()['task_id'])
            finally:client.delete(f'/api/projects/{p}')
