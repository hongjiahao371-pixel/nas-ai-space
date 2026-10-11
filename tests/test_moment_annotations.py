import unittest
from unittest.mock import patch
from tests import test_improvements as fixtures

class MomentAnnotationTests(unittest.TestCase):
    setUp=fixtures.ApiImprovementsTests.setUp
    tearDown=fixtures.ApiImprovementsTests.tearDown
    def setup_video(self):
        db=self.main.state.database;f=self.files[0]
        db.execute("UPDATE files SET kind='video',duration=12 WHERE id=?",(f['id'],))
        self.revision=f"{f['mtime_ns']}:{f['size']}";self.url=f"/api/files/{f['id']}/moments"
        return {'query':'四个按钮','time':4.862,'revision':self.revision}
    def test_confirm_prioritizes_only_same_query_and_revision_and_can_revoke(self):
        body=self.setup_video();service=self.main.state.video_moments
        with patch.object(service,'frame',return_value=b'actual-decoded-frame'):
            self.assertEqual(self.client.post(self.url+'/annotations',headers=self.headers,json=body).status_code,201)
        candidates={'moments':[{'time':1.62,'source_label':'直接视频画面'},{'time':4.862,'source_label':'直接视频画面'}],'indexed':True,'revision':self.revision}
        with patch.object(service,'candidates',return_value=candidates.copy()):
            result=self.client.get(self.url,headers=self.headers,params={'q':' 四个 按钮 '}).json()
            self.assertEqual(result['moments'][0]['time'],4.862)
            self.assertTrue(result['moments'][0]['human_confirmed']);self.assertEqual(result['unassisted_first_time'],1.62)
            other=self.client.get(self.url,headers=self.headers,params={'q':'两个按钮'}).json()
            self.assertEqual(other['moments'][0]['time'],1.62);self.assertNotIn('manual_confirmation',other)
        with patch.object(service,'candidates',return_value={**candidates,'revision':'changed'}):
            self.assertNotIn('manual_confirmation',self.client.get(self.url,headers=self.headers,params={'q':'四个按钮'}).json())
        annotation_id=result['moments'][0]['annotation_id']
        self.assertEqual(self.client.delete(self.url+'/annotations/'+str(annotation_id),headers=self.headers).status_code,200)
        self.assertEqual(self.main.state.database.fetchone('SELECT COUNT(*) n FROM video_moment_annotations')['n'],0)
    def test_stale_out_of_range_and_unreadable_frames_never_confirm(self):
        body=self.setup_video();service=self.main.state.video_moments
        with patch.object(service,'frame',return_value=b'frame') as decoder:
            self.assertEqual(self.client.post(self.url+'/annotations',headers=self.headers,json={**body,'revision':'stale'}).status_code,409)
            self.assertEqual(self.client.post(self.url+'/annotations',headers=self.headers,json={**body,'time':12}).status_code,409)
            decoder.assert_not_called()
        with patch.object(service,'frame',side_effect=ValueError('missing real frame')):
            self.assertEqual(self.client.post(self.url+'/annotations',headers=self.headers,json=body).status_code,409)
        self.assertEqual(self.main.state.database.fetchone('SELECT COUNT(*) n FROM video_moment_annotations')['n'],0)
    def test_machine_credentials_cannot_create_or_delete_annotations(self):
        body=self.setup_video()
        key=self.client.post('/api/integrations/keys',headers=self.headers,json={'name':'readonly','library_ids':[self.lib['id']]}).json()['token'];h={'Authorization':'Bearer '+key}
        self.assertEqual(self.client.post(self.url+'/annotations',headers=h,json=body).status_code,403)
        self.assertEqual(self.client.delete(self.url+'/annotations/1',headers=h).status_code,403)
