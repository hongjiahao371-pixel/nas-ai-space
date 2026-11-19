import io
import json
import time
import subprocess
import tempfile
from pathlib import Path
import unittest
import zipfile
from dataclasses import replace
from unittest.mock import patch
from fastapi.testclient import TestClient
from test_core import app, state
from app.main import require_auth, settings


class EditHandoffAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.TemporaryDirectory()
        cls.media_path = Path(cls.media_dir.name)/"fixture.mp4"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","color=c=blue:s=160x90:r=25","-t","12","-c:v","libx264","-threads","1","-pix_fmt","yuv420p",str(cls.media_path)],check=True)
        cls.context = TestClient(app)
        cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None,None,None)
        cls.media_dir.cleanup()

    def setUp(self):
        self.project = self.client.post('/api/projects',json={'name':f'edit-{time.time_ns()}'}).json()['id']
        self.addCleanup(state.database.execute,'DELETE FROM projects WHERE id=?',(self.project,))
        upload = self.client.post('/api/uploads',content=self.media_path.read_bytes(),headers={'X-Filename':f'edit-{time.time_ns()}.mp4'}).json()['file']
        self.file_id = upload['id']
        # Actual generated video, wait for the normal ingest task to finish.
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            source=state.database.get_file(self.file_id)
            if source.get('duration') and source.get('width'):
                break
            time.sleep(.05)
        self.assertTrue(source.get('duration'), source)
        asset = self.client.post(f'/api/projects/{self.project}/assets',json={'file_id':self.file_id,'title':'fixture'}).json()
        self.version = asset['versions'][0]['id']
        self.url=f'/api/projects/{self.project}/edit-handoff'
        self.payload={'frame_rate':'25','clips':[{'version_id':self.version,'start_time':1,'end_time':3,'media_path':'/Volumes/Edit/source.mp4'}]}

    def test_success_bundle_and_no_nas_path_disclosure(self):
        response=self.client.post(self.url,json=self.payload)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            self.assertEqual(set(z.namelist()),{'Timeline.fcpxmld/Info.fcpxml','timeline.fcpxml','manifest.json','README.txt'})
            self.assertIn(b'file:///Volumes/Edit/source.mp4',z.read('timeline.fcpxml'))
            self.assertEqual(z.read('Timeline.fcpxmld/Info.fcpxml'), z.read('timeline.fcpxml'))
            self.assertNotIn(state.database.get_file(self.file_id)['path'].encode(), b''.join(z.read(n) for n in z.namelist()))
            self.assertEqual(json.loads(z.read('manifest.json'))['timeline_duration'],'2s')

    def test_missing_mapping_wrong_version_cross_project(self):
        for changes,code in [({'version_id':987654321},404),({'media_path':''},422),({'media_path':'https://example.com/x'},422),({'start_time':4,'end_time':2},422),({'end_time':13},422)]:
            p={'frame_rate':'25','clips':[self.payload['clips'][0]|changes]}
            self.assertEqual(self.client.post(self.url,json=p).status_code,code)
        other=self.client.post('/api/projects',json={'name':f'other-{time.time_ns()}'}).json()['id']
        self.addCleanup(state.database.execute,'DELETE FROM projects WHERE id=?',(other,))
        self.assertEqual(self.client.post(f'/api/projects/{other}/edit-handoff',json=self.payload).status_code,404)

    def test_source_changed_and_unknown_rate(self):
        state.database.execute("UPDATE files SET metadata_json='{}' WHERE id=?",(self.file_id,))
        self.assertEqual(self.client.post(self.url,json=self.payload).status_code,422)
        state.database.execute('UPDATE files SET size=size+1 WHERE id=?',(self.file_id,))
        response=self.client.post(self.url,json=self.payload)
        self.assertEqual(response.status_code,409)
        self.assertNotIn(state.database.get_file(self.file_id)['path'],response.text)

    def test_disabled_library_and_intervening_rescan(self):
        library_id=state.database.get_file(self.file_id)['library_id']
        state.database.execute('UPDATE libraries SET enabled=0 WHERE id=?',(library_id,))
        try:
            self.assertEqual(self.client.post(self.url,json=self.payload).status_code,404)
        finally:
            state.database.execute('UPDATE libraries SET enabled=1 WHERE id=?',(library_id,))
        from app.services.edit_handoff import build_edit_handoff
        def rescan(*args,**kwargs):
            result=build_edit_handoff(*args,**kwargs)
            state.database.execute("UPDATE files SET metadata_json='{}' WHERE id=?",(self.file_id,))
            return result
        with patch('app.services.edit_handoff.build_edit_handoff',side_effect=rescan):
            self.assertEqual(self.client.post(self.url,json=self.payload).status_code,409)

    def test_malformed_nonfinite_oversize(self):
        for raw in [b'{',b'{"clips":[]}',json.dumps(self.payload).replace('"start_time": 1','"start_time": NaN').encode()]:
            self.assertEqual(self.client.post(self.url,content=raw).status_code,422)
        self.assertEqual(self.client.post(self.url,content=b'x'*(128*1024+1)).status_code,413)

    def test_auth_project_role_and_library_permissions(self):
        with patch('app.main.settings',replace(settings,api_token='fixture-secret')):
            self.assertEqual(self.client.post(self.url,json=self.payload).status_code,401)
        member={'user_id':999999,'username':'tester','role':'member','library_ids':[],'auth_type':'session'}
        app.dependency_overrides[require_auth]=lambda:member
        try:
            self.assertEqual(self.client.post(self.url,json=self.payload).status_code,404)
            with patch.object(state.workspaces,'access_role',return_value='viewer'):
                self.assertEqual(self.client.post(self.url,json=self.payload).status_code,403)
            with patch.object(state.workspaces,'access_role',return_value='editor'):
                self.assertEqual(self.client.post(self.url,json=self.payload).status_code,404)
        finally:
            app.dependency_overrides.pop(require_auth,None)
