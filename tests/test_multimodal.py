from __future__ import annotations

import hashlib
import math
import sqlite3
import tempfile
import unittest
import wave
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from app.config import settings
from app.database import Database
from app.services.local_ai import LocalAIClient
from app.services.multimodal import DIMENSION, MultimodalService, validate_embedding
from app.services.scanner import scan_library
from app.services.search import SearchService


class NoVectors:
    def search(self, *args, **kwargs):
        return []


class MediaSearch:
    enabled = True

    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def search(self, *args):
        self.calls.append(args)
        return self.hits


class MultimodalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library_path = self.root / "library"
        self.library_path.mkdir()
        self.db = Database(self.root / "source.db")
        self.db.initialize()
        self.library = self.db.create_library("素材", str(self.library_path))
        self.cfg = replace(settings, database_path=self.root / "source.db", data_dir=self.root / "data",
                           cache_dir=self.root / "cache", scan_root=self.library_path,
                           scan_roots=(self.library_path,), upload_root=self.root / "uploads",
                           vector_backup_dir=self.root / 'snapshots',
                           embedding_base_url="", embedding_model="", multimodal_enabled=True,
                           multimodal_base_url="http://127.0.0.1:8080")
        path = self.library_path / "IMG_001.jpg"
        Image.new("RGB", (40, 20), "red").save(path)
        scan_library(self.db, self.library, lambda *_: None, lambda: False)
        self.file = self.db.get_file(self.db.pending_file_ids()[0])

    def tearDown(self):
        self.tmp.cleanup()

    def hit(self, **overrides):
        payload = {"file_id": self.file["id"], "library_id": self.library["id"],
                   "mtime_ns": self.file["mtime_ns"], "size": self.file["size"],
                   "source_label": "直接图片", "content": "直接图片"}
        payload.update(overrides)
        return {"score": 0.4, "payload": payload}

    def search(self, hits):
        service = SearchService(self.db, LocalAIClient(self.cfg), NoVectors())
        service.multimodal = MediaSearch(hits)
        return service

    def test_media_recall_works_without_caption_or_lexical_hit(self):
        result = self.search([self.hit()]).search("红色雨伞", precise=True)
        self.assertEqual([row["id"] for row in result["results"]], [self.file["id"]])
        self.assertTrue(result["multimodal"])
        self.assertIn("素材语义", result["results"][0]["sources"])
        self.assertEqual(result["results"][0]["multimodal_score"], 0.4)
        self.assertFalse(result["precise"])

    def test_permissions_and_filters_are_checked_after_media_recall(self):
        search = self.search([self.hit()])
        self.assertFalse(search.search("红色雨伞", library_ids=[]) ["results"])
        self.assertFalse(search.search("红色雨伞", file_ids=[]) ["results"])
        self.assertFalse(search.search("红色雨伞", kind="video") ["results"])
        self.assertFalse(search.search("红色雨伞", filter_sql=("f.id = ?", [-1])) ["results"])

    def test_media_first_skips_qwen_for_valid_media_hits(self):
        search=self.search([self.hit()])
        search.ai.settings=replace(self.cfg,embedding_base_url='http://qwen',embedding_model='qwen')
        search.ai.embeddings=Mock(side_effect=AssertionError('Qwen must not delay valid media'))
        result=search.search('红色雨伞',kind='image',library_ids=[self.library['id']],media_first=True)
        self.assertEqual(result['retrieval_strategy'],'media_first')
        self.assertEqual([r['id'] for r in result['results']],[self.file['id']])
        search.ai.embeddings.assert_not_called()

    def test_media_first_falls_back_to_qwen_on_empty_or_stale_media(self):
        for hits in [[],[self.hit(mtime_ns=1)]]:
            search=self.search(hits)
            search.ai.settings=replace(self.cfg,embedding_base_url='http://qwen',embedding_model='qwen')
            search.ai.embeddings=Mock(return_value=[[0.1]])
            result=search.search('红色雨伞',kind='image',media_first=True)
            self.assertEqual(result['retrieval_strategy'],'hybrid_fallback')
            search.ai.embeddings.assert_called_once()

    def test_media_first_respects_scope_and_disabled_semantic(self):
        search=self.search([self.hit()]);search.ai.embeddings=Mock()
        self.assertFalse(search.search('红色雨伞',kind='image',library_ids=[],media_first=True)['results'])
        search.multimodal.calls.clear()
        search.search('红色雨伞',kind='image',semantic=False,media_first=True)
        self.assertFalse(search.multimodal.calls)
        search.ai.embeddings.assert_not_called()

    def test_stale_media_vectors_are_rejected(self):
        self.assertFalse(self.search([self.hit(mtime_ns=1)]).search("红色雨伞")["results"])
        self.assertFalse(self.search([self.hit(size=1)]).search("红色雨伞")["results"])

    def test_media_failure_preserves_lexical_search(self):
        search = self.search([])
        search.multimodal.search = Mock(side_effect=RuntimeError("offline"))
        result = search.search("IMG_001")
        self.assertEqual(result["results"][0]["id"], self.file["id"])
        self.assertFalse(result["multimodal"])

    def test_fast_mode_does_not_contact_media_model(self):
        search = self.search([self.hit()])
        result = search.search("红色雨伞", semantic=False)
        self.assertFalse(result["results"])
        self.assertTrue(result["multimodal_available"])
        self.assertFalse(search.multimodal.calls)

    def test_embeddings_require_correct_finite_normalized_vectors(self):
        for bad in ([], [0.] * DIMENSION, [math.nan] * DIMENSION, [math.inf] * DIMENSION):
            with self.assertRaises(ValueError):
                validate_embedding(bad)
        self.assertAlmostEqual(sum(x*x for x in validate_embedding([1.] * DIMENSION)), 1)

    def test_index_writes_only_new_collection_and_preserves_source(self):
        service = MultimodalService(self.cfg)
        before = hashlib.sha256(self.cfg.database_path.read_bytes()).hexdigest()
        service.embed = Mock(return_value=validate_embedding([1.] * DIMENSION))
        service.vectors._ensure_collection = Mock()
        service._http = Mock()
        count = service.index_file(self.file)
        self.assertEqual(count, 1)
        self.assertIn(self.cfg.multimodal_collection, service._http.put.call_args.args[0])
        self.assertNotIn("nas_ai_chunks", service._http.put.call_args.args[0])
        self.assertEqual(before, hashlib.sha256(self.cfg.database_path.read_bytes()).hexdigest())
        self.assertEqual(service.status([self.library["id"]])["indexed_files"], 1)
        self.assertEqual(service.status([self.library["id"]])["total_media"], 1)
        self.assertEqual(service.status([])["indexed_files"], 0)
        self.assertEqual(service.status([-1])["indexed_files"], 0)
        self.assertEqual(service.status([-1])["total_media"], 0)

    def test_changed_source_during_inference_is_not_published(self):
        service = MultimodalService(self.cfg)
        service._http = Mock()
        def changed(_):
            Path(self.file["path"]).write_bytes(b"changed")
            return validate_embedding([1.] * DIMENSION)
        service.embed = changed
        with self.assertRaises(ValueError):
            service.index_file(self.file)
        service._http.put.assert_not_called()

    def test_terminal_failure_is_scoped_and_not_a_processable_queue_or_eta(self):
        service=MultimodalService(self.cfg)
        with service._state() as db:
            db.execute('INSERT INTO media_index VALUES (?,?,?,?,?,?,?)',
                       (self.file['id'],service.signature(self.file),'error',0,'','truncated',1))
            db.execute('INSERT INTO retry_state VALUES (?,?,?,?)',(self.file['id'],3,0,1))
            db.execute('INSERT INTO retry_state VALUES (?,?,?,?)',(9999,3,0,1))
        scoped=service.status([self.library['id']])
        self.assertEqual(scoped['terminal_errors'],1)
        self.assertEqual(scoped['processable_pending_files'],0)
        self.assertTrue(scoped['queue_complete']);self.assertFalse(scoped['fully_indexed'])
        self.assertIsNone(scoped['eta_seconds'])
        self.assertEqual(service.status([-1])['terminal_errors'],0)

    def test_changed_source_does_not_inherit_terminal_failure_status(self):
        service=MultimodalService(self.cfg)
        with service._state() as db:
            db.execute('INSERT INTO media_index VALUES (?,?,?,?,?,?,?)',
                       (self.file['id'],'old-signature','error',0,'','truncated',1))
            db.execute('INSERT INTO retry_state VALUES (?,?,?,?)',(self.file['id'],3,0,1))
        status=service.status()
        self.assertEqual(status['errors'],0);self.assertEqual(status['terminal_errors'],0)
        self.assertEqual(status['processable_pending_files'],1)
        self.assertFalse(status['queue_complete']);self.assertFalse(status['error_files'])

    def test_source_path_cannot_escape_library(self):
        service = MultimodalService(self.cfg)
        outside = self.root / "outside.jpg"
        Image.new("RGB", (5, 5)).save(outside)
        with self.assertRaises(ValueError):
            service.source_path({**self.file, "path": str(outside)})

    def test_media_model_uses_search_prefix_and_validates_reply(self):
        service = MultimodalService(self.cfg)
        service._http = Mock()
        service._http.post.return_value.json.return_value = {"data": [{"embedding": [1.] * DIMENSION}]}
        vector = service.query_embedding("红伞")
        request = service._http.post.call_args.kwargs["json"]
        self.assertEqual(request["input"], ["task: search result | query: 红伞"])
        self.assertEqual(len(vector), DIMENSION)
        service.query_embedding("红伞")
        self.assertEqual(service._http.post.call_count, 1)

    def test_empty_and_truncated_pcm_are_rejected_before_embedding(self):
        from app.services.media_sampling import validated_audio
        target = self.root / 'sample.wav'
        def wav(count):
            with wave.open(str(target), 'wb') as f:
                f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
                f.writeframes(b'\0\0' * count)
        wav(0)
        with self.assertRaisesRegex(ValueError, '过短'): validated_audio(target)
        wav(16000)
        self.assertGreater(len(validated_audio(target)), 32000)
        target.write_bytes(target.read_bytes()[:-2000])
        with self.assertRaisesRegex(ValueError, '不完整'): validated_audio(target)

    def test_audio_end_uses_decoded_length_instead_of_requested_window(self):
        service=MultimodalService(self.cfg)
        def decode(command,**kwargs):
            with wave.open(command[-1], 'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
                audio.writeframes(b'\0\0'*16000)
            return Mock(returncode=0)
        with patch('app.services.multimodal.subprocess.run',side_effect=decode):
            segments=list(service.media_inputs({**self.file,'kind':'audio','duration':90},Path(self.file['path']),self.root))
        self.assertEqual([(start,end) for _,_,start,end in segments],[(0,1),(30,31),(60,61)])

    def test_optional_video_audio_failure_publishes_visual_points_with_warning(self):
        service = MultimodalService(self.cfg)
        video = {**self.file, 'kind':'video'}
        service.media_inputs = Mock(return_value=iter([
            ({'frame':'first'}, '直接视频画面', .1, .1),
            ({'audio':'bad'}, '视频音轨', 0, 30),
        ]))
        service.embed = Mock(side_effect=[validate_embedding([1.] * DIMENSION), ValueError('audio bad')])
        service.vectors._ensure_collection = Mock(); service._http = Mock()
        self.db.execute("UPDATE files SET kind='video' WHERE id=?", (self.file['id'],))
        count = service.index_file(video)
        self.assertEqual(count, 1)
        with service._state() as db:
            self.assertEqual(db.execute('SELECT status,points FROM media_index').fetchone()[:], ('ready',1))
            self.assertIn('音轨向量生成失败', db.execute('SELECT message FROM media_warnings').fetchone()[0])
        status=service.status([self.library['id']])
        self.assertEqual(status['partial_files'],1)
        self.assertEqual(status['errors'],0)
        # Warnings are scoped to visible libraries, not leaked to other members.
        self.assertEqual(service.status([-1]).get('warning_files'), [])
        body=service._http.put.call_args.kwargs['json']
        self.assertEqual(body['points'][0]['payload']['sampling_revision'],'boundary-v3')
        cleanup=service._http.post.call_args.kwargs['json']['filter']
        self.assertEqual(cleanup['must_not'],[{'has_id':[p['id'] for p in body['points']]}])

    def test_image_failure_is_not_silently_promoted_to_ready(self):
        service=MultimodalService(self.cfg)
        service.embed=Mock(side_effect=ValueError('invalid image'))
        service._http=Mock()
        with self.assertRaises(ValueError):service.index_file(self.file)
        service._http.put.assert_not_called()

    def test_boundary_sampling_includes_actual_final_scene(self):
        from app.services.media_sampling import boundary_times
        stamps=boundary_times(34.4344)
        self.assertAlmostEqual(stamps[-1],34.1344)
        for duration in [.01,.1,1,34.4344]:
            self.assertTrue(all(0 <= t < duration for t in boundary_times(duration)))
        self.assertFalse(boundary_times(math.nan))

    def test_video_sampling_stops_at_total_budget(self):
        from app.services.media_sampling import sample_video_frames
        with patch('app.services.media_sampling.time.monotonic',side_effect=[0,0,61]), \
             patch('app.services.media_sampling.subprocess.run',return_value=Mock(returncode=1)) as decode:
            self.assertFalse(sample_video_frames(self.library_path/'video.mp4',self.root,100))
            self.assertEqual(decode.call_count,1)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_actual_sampling_reads_opening_and_closing_without_touching_source(self):
        from app.services.media_sampling import sample_video_frames
        source=self.library_path/'two-scenes.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=128x128:d=1',
                        '-f','lavfi','-i','color=c=blue:s=128x128:d=1',
                        '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]',
                        '-map','[v]','-c:v','libx264',str(source)],check=True)
        before=hashlib.sha256(source.read_bytes()).hexdigest()
        frames=sample_video_frames(source,self.root,2,count=2)
        self.assertEqual(len(frames),4)
        with Image.open(frames[0][1]) as img:self.assertGreater(img.getpixel((20,20))[0],200)
        with Image.open(frames[1][1]) as img:self.assertGreater(img.getpixel((20,20))[2],200)
        self.assertEqual(before,hashlib.sha256(source.read_bytes()).hexdigest())

    def test_interrupted_snapshot_does_not_publish_or_expire_recovery_files(self):
        from contextlib import contextmanager
        from app.services.vectors import VectorStore
        store=VectorStore(self.cfg);client=Mock();store._http=Mock(return_value=client)
        client.get.return_value.status_code=200
        client.post.return_value.json.return_value={'result':{'name':'new.snapshot'}}
        @contextmanager
        def stream(*args,**kwargs):
            response=Mock()
            def chunks():
                yield b'partial'
                raise OSError('connection dropped')
            response.iter_bytes.side_effect=chunks
            yield response
        client.stream.side_effect=stream
        directory=self.cfg.vector_backup_dir;directory.mkdir(parents=True,exist_ok=True)
        old=directory/'good.snapshot';old.write_bytes(b'old verified backup')
        with self.assertRaises(OSError):store.create_snapshot(retain=1)
        self.assertEqual(old.read_bytes(),b'old verified backup')
        self.assertFalse((directory/'new.snapshot').exists())
        self.assertFalse((directory/'new.snapshot.part').exists())
        client.delete.assert_not_called()
