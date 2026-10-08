from __future__ import annotations
import hashlib, io, math, shutil, subprocess, unittest
from unittest.mock import Mock, patch
from PIL import Image
from app.services.video_moments import VideoMoments
from tests import test_improvements as fixtures

class VideoMomentTests(unittest.TestCase):
    setUp = fixtures.ImprovementsTests.setUp
    tearDown = fixtures.ImprovementsTests.tearDown

    def service(self):
        media = Mock(enabled=True, settings=self.cfg)
        media.source_path.return_value = self.library / 'test.mp4'
        media.signature.return_value = 'current'
        media.query_embedding.return_value = [1., 0.]
        self.file = {**self.files[0], 'kind': 'video', 'duration': 10}
        return VideoMoments(media)

    def hit(self, stamp, **overrides):
        f = self.file
        return {'payload': {'file_id': f['id'], 'library_id': f['library_id'], 'kind': 'video',
                            'signature': 'current', 'source_label': '直接视频画面',
                            'mtime_ns': f['mtime_ns'], 'size': f['size'], 'start_time': stamp, **overrides}}

    def response(self, service, hits):
        response = Mock(status_code=200)
        response.json.return_value = {'result': {'points': hits}}
        service.media._http.post.return_value = response

    def test_query_preserves_candidate_order_and_requires_current_visual_generation(self):
        service = self.service()
        self.response(service, [self.hit(4.862), self.hit(1.62), self.hit(4.8621),
            self.hit(8, signature='old'), self.hit(3, source_label='视频音轨'),
            self.hit(9, file_id=-1), self.hit(-1), self.hit(math.nan), self.hit(10)])
        result = service.candidates(self.file, '导入菜单')
        self.assertEqual([x['time'] for x in result['moments']], [4.862, 1.62])
        url, = service.media._http.post.call_args.args
        body = service.media._http.post.call_args.kwargs['json']
        self.assertTrue(url.endswith('/points/query'))
        self.assertEqual(body['query'], [1., 0.])
        self.assertIn({'key': 'signature', 'match': {'value': 'current'}}, body['filter']['must'])
        service.media.vectors._ensure_collection.assert_not_called()
        service.media._http.put.assert_not_called()

    def test_browse_is_chronological_and_does_not_call_model(self):
        service = self.service()
        self.response(service, [self.hit(6), self.hit(2), self.hit(4)])
        self.assertEqual([x['time'] for x in service.candidates(self.file)['moments']], [2, 4, 6])
        service.media.query_embedding.assert_not_called()
        self.assertTrue(service.media._http.post.call_args.args[0].endswith('/points/scroll'))

    def test_disabled_or_missing_collection_is_empty_without_creation(self):
        service = self.service()
        service.media.enabled = False
        self.assertFalse(service.candidates(self.file)['indexed'])
        service.media._http.post.assert_not_called()
        service.media.enabled = True
        service.media._http.post.return_value.status_code = 404
        self.assertFalse(service.candidates(self.file)['indexed'])
        service.media._http.put.assert_not_called()

    def test_source_change_after_query_discards_result(self):
        service = self.service()
        self.response(service, [self.hit(4)])
        service.media.source_path.side_effect = [self.library/'test.mp4', ValueError('source changed')]
        with self.assertRaises(ValueError): service.candidates(self.file)

    def test_invalid_kind_or_time_does_not_decode(self):
        service = self.service()
        for value in [-1, 10, math.inf, math.nan, 'bad']:
            with self.assertRaises(ValueError): service.frame(self.file, value)
        with self.assertRaises(ValueError): service.frame({**self.file, 'kind': 'image'}, 1)

    def test_frame_cache_revalidates_source_and_has_bounded_memory(self):
        service = self.service()
        output = io.BytesIO(); Image.new('RGB', (20, 30), 'red').save(output, format='JPEG')
        with patch('app.services.video_moments.subprocess.run', return_value=Mock(returncode=0, stdout=output.getvalue())) as decode:
            self.assertEqual(service.frame(self.file, 1.6204), output.getvalue())
            self.assertEqual(service.frame(self.file, 1.6203), output.getvalue())
            decode.assert_called_once()
            service.media.source_path.side_effect = ValueError('changed')
            with self.assertRaises(ValueError): service.frame(self.file, 1.6203)
        self.assertLessEqual(sum(map(len, service._frames.values())), 8*1024*1024)

    def test_timeout_and_invalid_decoder_output_are_not_cached(self):
        service = self.service()
        with patch('app.services.video_moments.subprocess.run', side_effect=subprocess.TimeoutExpired('ffmpeg', 20)):
            with self.assertRaises(TimeoutError): service.frame(self.file, 1)
        with patch('app.services.video_moments.subprocess.run', return_value=Mock(returncode=0, stdout=b'bad image')):
            with self.assertRaises(OSError): service.frame(self.file, 1)
        self.assertFalse(service._frames)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_actual_frame_read_preserves_original_and_makes_no_disk_cache(self):
        source = self.library/'test.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=128x128:d=2','-c:v','libx264',str(source)], check=True)
        service = self.service(); service.media.source_path.return_value = source
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        frame = service.frame(self.file, .5)
        with Image.open(io.BytesIO(frame)) as image:
            self.assertEqual(image.width, 480)
            self.assertGreater(image.convert('RGB').getpixel((20,20))[0], 200)
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertFalse((self.cfg.cache_dir/'video-moments').exists())

class VideoMomentApiTests(unittest.TestCase):
    setUp = fixtures.ApiImprovementsTests.setUp
    tearDown = fixtures.ApiImprovementsTests.tearDown

    def test_auth_non_video_disabled_library_and_limits(self):
        file_id = self.files[0]['id']; url = f'/api/files/{file_id}/moments'
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(url, headers=self.headers).status_code, 400)
        self.db.execute("UPDATE files SET kind='video',duration=10 WHERE id=?", (file_id,))
        self.assertEqual(self.client.get(url+'?q='+('x'*501), headers=self.headers).status_code, 422)
        self.db.execute('UPDATE libraries SET enabled=0 WHERE id=?', (self.lib['id'],))
        self.assertEqual(self.client.get(url, headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(f'/api/files/{file_id}/frame?time=1', headers=self.headers).status_code, 404)

    def test_member_cannot_read_other_library_moments_or_frames(self):
        from app.security import hash_password
        self.db.create_user('moment-member','moment-member',hash_password('TestPassword123'),'member',[])
        login = self.client.post('/api/auth/login', json={'username':'moment-member','password':'TestPassword123'})
        headers = {'Authorization':'Bearer '+login.json()['token']}
        self.db.execute("UPDATE files SET kind='video',duration=10 WHERE id=?", (self.files[0]['id'],))
        for suffix in ['moments','frame?time=1']:
            self.assertEqual(self.client.get(f"/api/files/{self.files[0]['id']}/{suffix}", headers=headers).status_code, 404)

    def test_authenticated_frame_is_private_and_query_does_not_mutate_index(self):
        file_id=self.files[0]['id'];self.db.execute("UPDATE files SET kind='video',duration=10 WHERE id=?", (file_id,))
        with patch.object(self.main.state.video_moments, 'candidates', return_value={'moments':[{'time':4.862}], 'indexed':True}) as query:
            result=self.client.get(f'/api/files/{file_id}/moments?q=menu',headers=self.headers)
            self.assertEqual(result.status_code,200);self.assertEqual(result.json()['moments'][0]['time'],4.862)
            self.assertEqual(query.call_args.args[1], 'menu')
        with patch.object(self.main.state.video_moments,'frame',return_value=b'jpeg'):
            result=self.client.get(f'/api/files/{file_id}/frame?time=4.862',headers=self.headers)
            self.assertEqual(result.content,b'jpeg');self.assertEqual(result.headers['cache-control'],'private, no-store')
        self.assertEqual(self.client.get(f'/api/files/{file_id}/frame?time=-1',headers=self.headers).status_code,422)
        self.assertEqual(self.client.get(f'/api/files/{file_id}/frame?time=1&revision=old',headers=self.headers).status_code,409)
