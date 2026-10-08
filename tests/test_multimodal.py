from __future__ import annotations

import hashlib
import math
import sqlite3
import tempfile
import unittest
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
