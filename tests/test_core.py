from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

import numpy as np

TEST_ROOT = Path(tempfile.mkdtemp(prefix="nas-ai-space-tests-"))
SCAN_ROOT = TEST_ROOT / "library"
DATA_ROOT = TEST_ROOT / "data"
SCAN_ROOT.mkdir()
DATA_ROOT.mkdir()
os.environ["NAS_AI_SCAN_ROOT"] = str(SCAN_ROOT)
os.environ["NAS_AI_DATA_DIR"] = str(DATA_ROOT)
os.environ["NAS_AI_LOCAL_AI_URL"] = ""
os.environ["NAS_AI_EMBEDDING_MODEL"] = ""
os.environ["NAS_AI_VISION_MODEL"] = ""
os.environ["NAS_AI_CHAT_MODEL"] = ""

from fastapi.testclient import TestClient
from PIL import Image, UnidentifiedImageError

from app.config import settings
from app.database import Database
from app.main import (
    LOGIN_FAILURES,
    PUBLIC_ACCESS_FAILURES,
    _focus_answer_evidence,
    _select_answer_sources,
    app,
    state,
)
from app.services.albums import analyze_events, analyze_places
from app.services.extractors import (
    _convert_image,
    _extract_video_frames,
    create_thumbnail,
    index_file,
    split_chunks,
)
from app.services.hardware import GPU, _make_plan
from app.services.local_ai import (
    LocalAIClient,
    _PriorityGate,
    _parse_json_object,
    _parse_rerank_values,
    _render_common_answer,
)
from app.services.organizer import _similar_components, analyze_duplicates, analyze_similar
from app.services.proxy import generate_look_preview, generate_proxy
from app.services.recycle import RecycleBin
from app.services.scanner import scan_library
from app.services.search import SearchService, _partial_coverage_cap, _query_groups
from app.services.workspaces import WorkspaceService


class NullVectors:
    def search(self, vector, limit, kind="", library_ids=None, file_ids=None):
        return []

    def delete_files(self, file_ids):
        return None


class SemanticVectors:
    def __init__(self, file_id: int):
        self.file_id = file_id

    def search(self, vector, limit, kind="", library_ids=None, file_ids=None):
        return [{
            "score": 0.92,
            "payload": {"file_id": self.file_id, "content": "时间轴里的关键词", "start_time": 12.5},
        }]


class RankedVectors:
    def __init__(self, hits):
        self.hits = hits

    def search(self, vector, limit, kind="", library_ids=None, file_ids=None):
        return self.hits[:limit]


class SimilarVectors(RankedVectors):
    def representative_vector(self, file_id):
        return [0.1, 0.2]


class SemanticAI:
    def __init__(self, local_settings):
        self.settings = replace(local_settings, embedding_base_url="http://local", embedding_model="test")

    def embeddings(self, values, interactive=False):
        return [[0.1, 0.2] for _ in values]

    @staticmethod
    def embedding_query(query):
        return f"Instruct: test\nQuery: {query}"


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="nas-ai-core-")
        root = Path(self.temp.name)
        self.library_path = root / "library"
        self.library_path.mkdir()
        self.database = Database(root / "index.db")
        self.database.initialize()
        self.library = self.database.create_library("测试资料", str(self.library_path))
        self.local_settings = replace(
            settings,
            data_dir=root / "data",
            cache_dir=root / "cache",
            database_path=root / "index.db",
            scan_root=self.library_path,
            local_ai_base_url="",
            embedding_model="",
            vision_model="",
            chat_model="",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_incremental_scan_and_search_chinese(self) -> None:
        document = self.library_path / "装修记录.txt"
        document.write_text("厨房装修总费用一万二千元，包含橱柜和灯具。", encoding="utf-8")
        first = scan_library(self.database, self.library, lambda *_: None, lambda: False)
        self.assertEqual(first["changed"], 1)

        file_id = self.database.pending_file_ids()[0]
        file = self.database.get_file(file_id)
        result, chunks = index_file(file, self.local_settings, LocalAIClient(self.local_settings))
        self.database.finish_file_index(file_id, result, chunks)

        search = SearchService(self.database, LocalAIClient(self.local_settings), NullVectors())
        response = search.search("厨房装修")
        self.assertEqual(response["results"][0]["name"], "装修记录.txt")

        second = scan_library(self.database, self.library, lambda *_: None, lambda: False)
        self.assertEqual(second["changed"], 0)
        self.assertEqual(second["unchanged"], 1)

    def test_deleted_file_is_removed_from_index(self) -> None:
        document = self.library_path / "remove-me.txt"
        document.write_text("temporary", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        document.unlink()
        result = scan_library(self.database, self.library, lambda *_: None, lambda: False)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self.database.dashboard()["files"]["total"], 0)

    def test_empty_dashboard_counts_are_zero(self) -> None:
        files = self.database.dashboard()["files"]
        self.assertEqual(
            {key: files[key] for key in ("total", "bytes", "ready", "pending", "errors")},
            {"total": 0, "bytes": 0, "ready": 0, "pending": 0, "errors": 0},
        )
        self.assertEqual(self.database.probe(), "ok")

    def test_image_metadata_and_thumbnail(self) -> None:
        image_path = self.library_path / "green.png"
        Image.new("RGB", (1200, 800), "#45d6a7").save(image_path)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file = self.database.get_file(self.database.pending_file_ids()[0])
        result, _ = index_file(file, self.local_settings, LocalAIClient(self.local_settings))
        self.assertEqual((result["width"], result["height"]), (1200, 800))
        thumbnail = Path(self.temp.name) / "thumb.jpg"
        create_thumbnail(image_path, thumbnail, "image", 320)
        with Image.open(thumbnail) as output:
            self.assertEqual(output.size, (320, 213))

    def test_image_capture_time_is_extracted(self) -> None:
        image_path = self.library_path / "capture.jpg"
        exif = Image.Exif()
        exif[36867] = "2024:06:18 09:30:45"
        Image.new("RGB", (320, 240), "#5577cc").save(image_path, exif=exif)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file = self.database.get_file(self.database.pending_file_ids()[0])
        result, _ = index_file(file, self.local_settings, LocalAIClient(self.local_settings))
        self.assertEqual(result["captured_at"], "2024-06-18T09:30:45")

    def test_raw_image_uses_libraw_decoder(self) -> None:
        raw_path = self.library_path / "photo.dng"
        raw_path.write_bytes(b"fake-raw")
        destination = Path(self.temp.name) / "converted.jpg"

        class FakeRaw:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            @staticmethod
            def postprocess(**_):
                return np.zeros((240, 320, 3), dtype=np.uint8)

        with patch("app.services.extractors.rawpy.imread", return_value=FakeRaw()):
            decoder = _convert_image(raw_path, destination, 160)
        self.assertEqual(decoder, "libraw")
        with Image.open(destination) as output:
            self.assertEqual(output.size, (160, 120))

    def test_video_frame_extraction_falls_back_to_first_frame(self) -> None:
        video_path = self.library_path / "truncated.mkv"
        video_path.write_bytes(b"fake-video")
        frame_dir = Path(self.temp.name) / "frames"
        frame_dir.mkdir()

        def fake_run(command, **_):
            timestamp = float(command[command.index("-ss") + 1])
            if timestamp == 0:
                Path(command[-1]).write_bytes(b"frame")
                return subprocess.CompletedProcess(command, 0)
            return subprocess.CompletedProcess(command, 1)

        with (
            patch("app.services.extractors.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("app.services.extractors.ffmpeg_input_args", return_value=[]),
            patch("app.services.extractors.subprocess.run", side_effect=fake_run),
        ):
            frames = _extract_video_frames(video_path, frame_dir, 10_000)
        self.assertEqual([timestamp for timestamp, _ in frames], [0.0])

    def test_empty_transcript_is_not_counted_as_ready(self) -> None:
        video_path = self.library_path / "silent.mp4"
        video_path.write_bytes(b"fake-video")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file = self.database.get_file(self.database.pending_file_ids()[0])
        speech_settings = replace(
            self.local_settings,
            transcription_base_url="http://speech",
            transcription_model="test",
        )
        speech_settings.cache_dir.mkdir(parents=True, exist_ok=True)

        class EmptySpeechAI:
            @staticmethod
            def transcribe(_: Path) -> dict:
                return {"text": "", "segments": []}

        with (
            patch(
                "app.services.extractors._probe_media",
                return_value={"duration": 5.0, "metadata": {"audio_codec": "aac"}},
            ),
            patch("app.services.extractors._extract_audio"),
        ):
            result, _ = index_file(file, speech_settings, EmptySpeechAI())
        self.assertEqual(result["stages"]["transcription"]["status"], "not_applicable")
        self.assertTrue(result["metadata"]["transcription_empty"])

    def test_duplicate_analysis_verifies_entire_file(self) -> None:
        prefix = b"a" * (1024 * 1024)
        suffix = b"z" * (1024 * 1024)
        original = prefix + b"b" * (1024 * 1024) + suffix
        different_middle = prefix + b"c" * (1024 * 1024) + suffix
        (self.library_path / "one.bin").write_bytes(original)
        (self.library_path / "two.bin").write_bytes(original)
        (self.library_path / "three.bin").write_bytes(different_middle)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        result = analyze_duplicates(self.database, self.local_settings, lambda *_: None, lambda: False)
        groups = self.database.duplicate_groups()
        self.assertEqual(result["groups"], 1)
        self.assertEqual(groups["groups"][0]["member_count"], 2)
        self.assertEqual({item["name"] for item in groups["groups"][0]["items"]}, {"one.bin", "two.bin"})

    def test_similar_photo_analysis_and_complete_banding(self) -> None:
        first = self.library_path / "first.jpg"
        second = self.library_path / "second.jpg"
        third = self.library_path / "third.jpg"
        Image.new("RGB", (320, 240), "#4968ba").save(first)
        shutil.copyfile(first, second)
        Image.new("RGB", (320, 240), "#e45b42").save(third)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        result = analyze_similar(self.database, self.local_settings, lambda *_: None, lambda: False)
        self.assertEqual(result["groups"], 1)
        group = self.database.similarity_groups()["groups"][0]
        self.assertEqual({item["name"] for item in group["items"]}, {"first.jpg", "second.jpg"})

        changed_bands = sum(1 << offset for offset in range(0, 56, 7))
        components = _similar_components([
            {"id": 1, "perceptual_hash": "0000000000000000"},
            {"id": 2, "perceptual_hash": f"{changed_bands:016x}"},
        ], 8)
        self.assertEqual(len(components), 1)

    def test_place_and_event_album_analysis(self) -> None:
        files = []
        for index in range(6):
            path = self.library_path / f"album-{index}.jpg"
            Image.new("RGB", (320 + index, 240), "#4968ba").save(path)
            files.append(path)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        rows = self.database.fetchall("SELECT id, name FROM files ORDER BY name")
        values = [
            ("2024-05-01T09:00:00", 31.2304, 121.4737),
            ("2024-05-01T10:00:00", 31.2310, 121.4740),
            ("2024-05-01T11:00:00", 31.2320, 121.4750),
            ("2024-06-15T09:00:00", 39.9042, 116.4074),
            ("2024-06-15T10:00:00", 39.9050, 116.4080),
            ("2024-06-15T11:00:00", 39.9060, 116.4090),
        ]
        for row, value in zip(rows, values):
            self.database.execute(
                "UPDATE files SET captured_at = ?, latitude = ?, longitude = ?, width = 320, height = 240 WHERE id = ?",
                (*value, row["id"]),
            )
        place_result = analyze_places(self.database, lambda *_: None, lambda: False)
        event_result = analyze_events(self.database, lambda *_: None, lambda: False)
        self.assertEqual(place_result["places"], 2)
        self.assertEqual(event_result["events"], 2)
        self.assertEqual(self.database.fetchone("SELECT COUNT(*) AS count FROM place_files")["count"], 6)
        self.assertEqual(self.database.fetchone("SELECT COUNT(*) AS count FROM event_files")["count"], 6)

    def test_duplicate_recycle_and_restore(self) -> None:
        first = self.library_path / "keep.bin"
        second = self.library_path / "remove.bin"
        first.write_bytes(b"same-content")
        second.write_bytes(b"same-content")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        recycle_root = Path(self.temp.name) / "recycle"
        recycle_root.mkdir()
        local_settings = replace(self.local_settings, recycle_root=recycle_root)
        analyze_duplicates(self.database, local_settings, lambda *_: None, lambda: False)
        remove = self.database.fetchone("SELECT * FROM files WHERE name = 'remove.bin'")
        result = RecycleBin(self.database, local_settings, NullVectors()).move_duplicates([remove["id"]], "tester")
        self.assertEqual(result["moved"], 1)
        self.assertFalse(second.exists())
        self.assertEqual(self.database.fetchone("SELECT COUNT(*) AS count FROM files")["count"], 1)
        restored = RecycleBin(self.database, local_settings, NullVectors()).restore(result["items"][0])
        self.assertTrue(second.exists())
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        self.assertIsNotNone(self.database.fetchone("SELECT id FROM files WHERE path = ?", (restored["path"],)))
        self.assertEqual(self.database.get_trash_item(result["items"][0])["status"], "restored")

    def test_recycle_refuses_last_copy(self) -> None:
        only = self.library_path / "only.bin"
        only.write_bytes(b"unique")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        row = self.database.fetchone("SELECT * FROM files WHERE name = 'only.bin'")
        self.database.execute("UPDATE files SET content_hash = 'verified' WHERE id = ?", (row["id"],))
        recycle_root = Path(self.temp.name) / "recycle"
        recycle_root.mkdir()
        with self.assertRaises(ValueError):
            RecycleBin(
                self.database,
                replace(self.local_settings, recycle_root=recycle_root),
                NullVectors(),
            ).move_duplicates([row["id"]], "tester")

    def test_semantic_video_result_contains_match_time(self) -> None:
        video = self.library_path / "meeting.mp4"
        video.write_bytes(b"not-a-real-video")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file_id = self.database.pending_file_ids()[0]
        search = SearchService(self.database, SemanticAI(self.local_settings), SemanticVectors(file_id))
        response = search.search("关键词")
        self.assertEqual(response["results"][0]["match_time"], 12.5)

    def test_fast_search_can_skip_semantic_model(self) -> None:
        document = self.library_path / "快速搜索.txt"
        document.write_text("猫咪坐在花盆旁边", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file_id = self.database.pending_file_ids()[0]
        file = self.database.get_file(file_id)
        result, chunks = index_file(file, self.local_settings, LocalAIClient(self.local_settings))
        self.database.finish_file_index(file_id, result, chunks)
        response = SearchService(self.database, SemanticAI(self.local_settings), NullVectors()).search(
            "猫咪 花盆", semantic=False
        )
        self.assertFalse(response["semantic"])
        self.assertEqual(response["results"][0]["name"], "快速搜索.txt")

    def test_similar_search_excludes_source_and_deduplicates_files(self) -> None:
        for name in ("source.jpg", "near.jpg", "other.jpg"):
            Image.new("RGB", (80, 80), "#777777").save(self.library_path / name)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        source = self.database.fetchone("SELECT * FROM files WHERE name = 'source.jpg'")
        near = self.database.fetchone("SELECT * FROM files WHERE name = 'near.jpg'")
        other = self.database.fetchone("SELECT * FROM files WHERE name = 'other.jpg'")
        vectors = SimilarVectors([
            {"score": 1.0, "payload": {"file_id": source["id"], "content": "原图"}},
            {"score": 0.91, "payload": {"file_id": near["id"], "content": "相似场景"}},
            {"score": 0.88, "payload": {"file_id": near["id"], "content": "重复分块"}},
            {"score": 0.72, "payload": {"file_id": other["id"], "content": "相关场景"}},
        ])
        response = SearchService(self.database, LocalAIClient(self.local_settings), vectors).similar(source["id"])
        self.assertEqual([item["name"] for item in response["results"]], ["near.jpg", "other.jpg"])
        self.assertEqual(response["results"][0]["sources"], ["相似内容"])

    def test_multi_condition_search_boosts_full_coverage(self) -> None:
        for name in ("cat.jpg", "cat-and-pot.jpg"):
            Image.new("RGB", (80, 80), "#777777").save(self.library_path / name)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        cat = self.database.fetchone("SELECT * FROM files WHERE name = 'cat.jpg'")
        combined = self.database.fetchone("SELECT * FROM files WHERE name = 'cat-and-pot.jpg'")
        self.database.finish_file_index(cat["id"], {
            "caption": "一只灰色猫咪趴在床上",
            "text": "",
            "quick_hash": "a",
            "metadata": {},
        }, [{"content": "一只灰色猫咪趴在床上"}])
        self.database.finish_file_index(combined["id"], {
            "caption": "一只灰色猫咪坐在黑色花盆旁边",
            "text": "",
            "quick_hash": "b",
            "metadata": {},
        }, [{"content": "一只灰色猫咪坐在黑色花盆旁边"}])
        vectors = RankedVectors([
            {"score": 0.82, "payload": {"file_id": cat["id"], "content": "一只灰色猫咪趴在床上"}},
            {"score": 0.76, "payload": {"file_id": combined["id"], "content": "一只灰色猫咪坐在黑色花盆旁边"}},
        ])
        response = SearchService(self.database, SemanticAI(self.local_settings), vectors).search("灰色猫咪 花盆")
        self.assertEqual(response["results"][0]["name"], "cat-and-pot.jpg")
        self.assertEqual(response["terms"], ["灰色", "猫咪", "花盆"])

    def test_object_search_prefers_photo_over_screenshot_avatar(self) -> None:
        for name in ("cat.jpg", "Screenshot_cat.jpg"):
            Image.new("RGB", (80, 80), "#777777").save(self.library_path / name)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        photo = self.database.fetchone("SELECT * FROM files WHERE name = 'cat.jpg'")
        screenshot = self.database.fetchone("SELECT * FROM files WHERE name = 'Screenshot_cat.jpg'")
        self.database.finish_file_index(photo["id"], {
            "caption": "一只灰色猫咪趴在床上",
            "text": "",
            "quick_hash": "a",
            "metadata": {},
        }, [{"content": "一只灰色猫咪趴在床上"}])
        self.database.finish_file_index(screenshot["id"], {
            "caption": "微信聊天界面，右侧头像是一只灰色猫咪",
            "text": "",
            "quick_hash": "b",
            "metadata": {},
        }, [{"content": "微信聊天界面，右侧头像是一只灰色猫咪"}])
        response = SearchService(self.database, LocalAIClient(self.local_settings), NullVectors()).search("灰色猫咪")
        self.assertEqual(response["results"][0]["name"], "cat.jpg")

    def test_rerank_parser_accepts_python_style_json(self) -> None:
        values = _parse_rerank_values("[{'id': 1, 'score': 88, 'reason': '直接相关'}]")
        self.assertEqual(values[0]["score"], 88)

    def test_rerank_parser_keeps_complete_items_from_truncated_array(self) -> None:
        values = _parse_rerank_values(
            '[{"id":1,"score":88,"reason":"相关"},{"id":2,"score":42,"reason":"部分相关"},'
        )
        self.assertEqual([item["id"] for item in values], [1, 2])

    def test_common_answer_is_structured_and_source_checked(self) -> None:
        payload = _parse_json_object(
            '```json\n{"common":['
            '{"text":"蛋糕","sources":[1,2,3]},'
            '{"text":"男子身穿西装","sources":[1,2]},'
            '{"text":"香槟","sources":[1]},'
            '{"text":"无效来源","sources":[1,99]}]}\n```'
        )
        answer = _render_common_answer(payload, [{}, {}, {}], "有哪些共同的布置元素？")
        self.assertIn("蛋糕 [1][2][3]", answer)
        self.assertNotIn("西装", answer)
        self.assertNotIn("香槟", answer)
        self.assertNotIn("无效来源", answer)

    def test_common_answer_rejects_schema_placeholders(self) -> None:
        answer = _render_common_answer(
            {"common": [{"text": "共同事实", "sources": [1, 2]}]}, [{}, {}], "共同点是什么？"
        )
        self.assertTrue(answer.startswith("现有证据不足"))

    def test_partial_query_coverage_caps_confidence(self) -> None:
        self.assertEqual(_partial_coverage_cap(0.5), 0.58)
        self.assertEqual(_partial_coverage_cap(2 / 3), 0.68)
        self.assertEqual(_partial_coverage_cap(0), 0.3)

    def test_comparison_words_do_not_become_search_constraints(self) -> None:
        groups = _query_groups("生日聚会照片中有哪些共同的布置元素？")
        self.assertEqual([group[0] for group in groups], ["生日", "聚会"])
        groups = _query_groups("这些生日聚会照片里共同出现了哪些布置和物品？")
        self.assertEqual([group[0] for group in groups], ["生日", "聚会"])
        groups = _query_groups("这些生日聚会有哪些共同布置？")
        self.assertEqual([group[0] for group in groups], ["生日", "聚会"])
        groups = _query_groups("有哪些照片同时拍到了灰色猫咪和花盆？")
        self.assertEqual([group[0] for group in groups], ["灰色", "猫咪", "花盆"])

    def test_answer_sources_exclude_low_coverage_distractors(self) -> None:
        sources = _select_answer_sources([
            {"name": "party.jpg", "confidence": 0.45, "coverage": 1.0, "sources": ["精准重排"]},
            {"name": "party-two.jpg", "confidence": 0.4, "coverage": 2 / 3, "rerank_reason": "直接相关"},
            {"name": "desk.jpg", "confidence": 0.8, "coverage": 1 / 3},
        ])
        self.assertEqual([source["name"] for source in sources], ["party.jpg", "party-two.jpg"])

    def test_answer_sources_recover_when_reranker_returns_one_item(self) -> None:
        sources = _select_answer_sources([
            {"name": "party.jpg", "confidence": 0.8, "coverage": 1.0, "sources": ["精准重排"]},
            {"name": "party-two.jpg", "confidence": 0.7, "coverage": 1.0},
            {"name": "desk.jpg", "confidence": 0.9, "coverage": 0.25},
        ])
        self.assertEqual([source["name"] for source in sources], ["party.jpg", "party-two.jpg"])

    def test_answer_evidence_keeps_only_matched_sentences(self) -> None:
        source = _focus_answer_evidence({
            "matched_terms": ["birthday", "桌前"],
            "evidence": (
                "男子坐在桌前，桌上有泰迪熊和蛋糕。"
                "身后有HAPPY BIRTHDAY气球。"
                "房间另一侧有酒瓶和玻璃杯。"
            ),
        })
        self.assertIn("泰迪熊和蛋糕", source["evidence"])
        self.assertIn("HAPPY BIRTHDAY", source["evidence"])
        self.assertNotIn("酒瓶和玻璃杯", source["evidence"])

    def test_answer_evidence_keeps_following_context_for_single_match(self) -> None:
        source = _focus_answer_evidence({
            "matched_terms": ["birthday"],
            "evidence": (
                "男女站在室内拍照。"
                "身后有HAPPY BIRTHDAY气球。"
                "桌上摆放着蛋糕和香槟。"
            ),
        })
        self.assertIn("HAPPY BIRTHDAY", source["evidence"])
        self.assertIn("蛋糕和香槟", source["evidence"])

    def test_schema_migrates_existing_database(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """CREATE TABLE files (
                   id INTEGER PRIMARY KEY, library_id INTEGER, path TEXT, kind TEXT, status TEXT,
                   size INTEGER, mtime_ns INTEGER, quick_hash TEXT, scan_token TEXT)"""
            )
        migrated = Database(legacy_path)
        migrated.initialize()
        with migrated.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(files)")}
            task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        self.assertTrue({
            "captured_at", "latitude", "longitude", "perceptual_hash", "content_hash",
            "manual_caption", "metadata_status", "vision_status", "transcription_status", "embedding_status",
            "retry_count", "last_attempt_at", "next_retry_at", "terminal_error", "last_error_fingerprint",
        }.issubset(columns))
        self.assertTrue({"work_total", "work_done", "heartbeat_at"}.issubset(task_columns))

    def test_partial_index_stages_are_repairable(self) -> None:
        document = self.library_path / "partial.txt"
        document.write_text("需要修复的测试资料", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file_id = self.database.pending_file_ids()[0]
        result = {
            "caption": "",
            "text": "需要修复的测试资料",
            "quick_hash": "partial",
            "metadata": {"ai_errors": ["embedding: temporary"]},
            "stages": {
                "metadata": {"status": "ready", "error": ""},
                "vision": {"status": "not_applicable", "error": ""},
                "transcription": {"status": "not_applicable", "error": ""},
                "embedding": {"status": "error", "error": "embedding: temporary"},
            },
        }
        chunks = [{"content": "需要修复的测试资料", "source_label": "全文"}]
        status = self.database.finish_file_index(
            file_id, result, chunks, retry_max_attempts=3, retry_base_seconds=30
        )
        self.assertEqual(status, "partial")
        row = self.database.get_file(file_id)
        self.assertEqual(row["retry_count"], 1)
        self.assertIsNotNone(row["next_retry_at"])
        self.assertEqual(row["terminal_error"], 0)
        pending = self.library_path / "not-started.jpg"
        Image.new("RGB", (80, 80), "#333333").save(pending)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        pending_row = self.database.fetchone("SELECT * FROM files WHERE name = ?", (pending.name,))
        self.assertEqual(pending_row["status"], "pending")
        self.assertNotIn(pending_row["id"], self.database.repair_file_ids())
        self.assertEqual(self.database.repair_file_ids(), [])
        self.assertEqual(self.database.retry_waiting_count(), 1)
        self.database.execute(
            "UPDATE files SET next_retry_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds"), file_id),
        )
        self.assertEqual(self.database.repair_file_ids(), [file_id])
        stages = self.database.index_stage_summary()
        self.assertEqual(stages["repairable"], 1)
        self.assertEqual(stages["embedding"]["error"], 1)
        chunk = self.database.fetchone("SELECT source_label FROM content_chunks WHERE file_id = ?", (file_id,))
        self.assertEqual(chunk["source_label"], "全文")

        for expected_attempt in (2, 3):
            self.database.finish_file_index(
                file_id, result, chunks, retry_max_attempts=3, retry_base_seconds=30
            )
            row = self.database.get_file(file_id)
            self.assertEqual(row["retry_count"], expected_attempt)
            if expected_attempt < 3:
                self.database.execute(
                    "UPDATE files SET next_retry_at = ? WHERE id = ?",
                    ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds"), file_id),
                )
        self.assertEqual(row["terminal_error"], 1)
        self.assertIsNone(row["next_retry_at"])
        self.assertEqual(self.database.terminal_failure_count(), 1)
        self.assertEqual(self.database.repair_file_ids(), [])
        self.database.reset_file_retry(file_id, pending=True)
        reset = self.database.get_file(file_id)
        self.assertEqual((reset["status"], reset["retry_count"], reset["terminal_error"]), ("pending", 0, 0))

    def test_retry_limit_applies_even_when_error_text_changes(self) -> None:
        document = self.library_path / "unstable.txt"
        document.write_text("unstable", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file_id = self.database.pending_file_ids()[0]
        for attempt in range(1, 4):
            self.database.fail_file_index(
                file_id,
                f"temporary error {attempt}",
                retry_max_attempts=3,
                retry_base_seconds=30,
            )
            row = self.database.get_file(file_id)
            self.assertEqual(row["retry_count"], attempt)
        self.assertEqual(row["terminal_error"], 1)
        self.assertEqual(self.database.terminal_failure_count(), 1)

    def test_task_telemetry_runtime_and_retention(self) -> None:
        document = self.library_path / "remaining.txt"
        document.write_text("pending", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        task_id = self.database.create_task("index_pending", {"limit": 10})
        self.database.start_task(task_id)
        self.database.update_task(task_id, 1, "完成", 10, 10)
        self.database.finish_task(task_id)
        self.database.execute(
            "UPDATE tasks SET started_at = ?, finished_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:10+00:00", task_id),
        )
        runtime = self.database.index_runtime_summary()
        self.assertEqual(runtime["sample_items"], 10)
        self.assertEqual(runtime["items_per_minute"], 60.0)
        self.assertEqual(runtime["remaining_items"], 1)
        self.assertEqual(runtime["eta_seconds"], 1)

        for _ in range(101):
            terminal_task = self.database.create_task("analyze_events", {})
            self.database.finish_task(terminal_task)
        self.assertEqual(self.database.prune_tasks(retain_count=100, retain_days=30), 2)
        terminal_count = self.database.fetchone(
            "SELECT COUNT(*) AS count FROM tasks WHERE status NOT IN ('pending', 'running')"
        )
        self.assertEqual(terminal_count["count"], 100)

    def test_manual_caption_tags_favorites_smart_albums_and_conversations(self) -> None:
        image_path = self.library_path / "manual.jpg"
        Image.new("RGB", (160, 120), "#5577cc").save(image_path)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file_id = self.database.pending_file_ids()[0]
        self.database.set_manual_caption(file_id, "蓝色背景上的测试物体")
        file = self.database.get_file(file_id)
        result, chunks = index_file(file, self.local_settings, LocalAIClient(self.local_settings))
        self.database.finish_file_index(file_id, result, chunks)
        indexed = self.database.get_file(file_id)
        self.assertEqual(indexed["ai_caption"], "蓝色背景上的测试物体")
        self.assertEqual(indexed["metadata_status"], "ready")

        user = self.database.create_user("tester", "测试用户", "hash", "member", [self.library["id"]])
        user_id = int(user["id"])
        self.database.set_favorite(user_id, file_id, True)
        self.assertTrue(self.database.is_favorite(user_id, file_id))
        self.assertEqual(self.database.set_file_tags(user_id, file_id, ["重要", "测试", "重要"]), ["重要", "测试"])
        self.assertEqual(self.database.file_tag_names(user_id, file_id), ["测试", "重要"])
        album = self.database.create_smart_album(user_id, "蓝色物体", "蓝色物体", "image", {"favorite": True})
        self.assertEqual(self.database.get_smart_album(album["id"], user_id)["filters"]["favorite"], True)
        conversation = self.database.create_conversation(user_id, "测试问答")
        self.database.add_conversation_message(conversation["id"], "user", "这是什么？")
        self.database.add_conversation_message(
            conversation["id"], "assistant", "这是测试物体。", [{"id": file_id, "source_label": "画面描述"}]
        )
        saved = self.database.conversation(conversation["id"], user_id)
        self.assertEqual(saved["messages"][1]["sources"][0]["source_label"], "画面描述")

    def test_feedback_changes_exact_query_order(self) -> None:
        for name in ("first.txt", "second.txt"):
            (self.library_path / name).write_text("灰色猫咪和花盆", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        for row in self.database.fetchall("SELECT * FROM files ORDER BY id"):
            result, chunks = index_file(row, self.local_settings, LocalAIClient(self.local_settings))
            self.database.finish_file_index(row["id"], result, chunks)
        rows = self.database.fetchall("SELECT id, name FROM files ORDER BY id")
        self.database.save_feedback(None, rows[0]["id"], "灰色猫咪 花盆", "irrelevant")
        self.database.save_feedback(None, rows[1]["id"], "灰色猫咪 花盆", "relevant")
        response = SearchService(self.database, LocalAIClient(self.local_settings), NullVectors()).search("灰色猫咪 花盆")
        self.assertEqual(response["results"][0]["name"], "second.txt")

    def test_office_document_chunks_keep_source_labels(self) -> None:
        document = self.library_path / "slides.pptx"
        with zipfile.ZipFile(document, "w") as archive:
            archive.writestr(
                "ppt/slides/slide1.xml",
                '<p:sld xmlns:p="p" xmlns:a="a"><a:t>第一页项目总结</a:t></p:sld>',
            )
            archive.writestr(
                "ppt/slides/slide2.xml",
                '<p:sld xmlns:p="p" xmlns:a="a"><a:t>第二页待办事项</a:t></p:sld>',
            )
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file = self.database.get_file(self.database.pending_file_ids()[0])
        _, chunks = index_file(file, self.local_settings, LocalAIClient(self.local_settings))
        self.assertEqual([chunk["source_label"] for chunk in chunks], ["第 1 页", "第 2 页"])

    @unittest.skipUnless(shutil.which("pdftoppm") and find_spec("pypdf"), "PDF OCR dependencies are unavailable")
    def test_scanned_pdf_uses_page_ocr_and_source_labels(self) -> None:
        document = self.library_path / "scanned.pdf"
        Image.new("RGB", (900, 1200), "white").save(document, "PDF", resolution=150)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file = self.database.get_file(self.database.pending_file_ids()[0])
        ocr_settings = replace(
            self.local_settings,
            vision_base_url="http://local",
            vision_model="test",
            pdf_ocr_pages=2,
        )

        class OCRAI:
            @staticmethod
            def ocr_document_page(path: Path, page_number: int) -> str:
                return f"扫描页测试发票金额一百元，第 {page_number} 页。"

        result, chunks = index_file(file, ocr_settings, OCRAI())
        self.assertEqual(result["metadata"]["ocr_pages"], 1, (result, chunks))
        self.assertEqual(chunks[0]["source_label"], "第 1 页")
        self.assertIn("发票金额一百元", chunks[0]["content"])

    def test_failed_task_can_be_reset(self) -> None:
        task_id = self.database.create_task("analyze_duplicates", {})
        self.database.fail_task(task_id, "temporary")
        self.database.cancel_task(task_id)
        self.database.reset_task(task_id)
        task = self.database.get_task(task_id)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["error"], "")
        self.assertEqual(task["cancel_requested"], 0)

    def test_cancelled_task_is_finalized_during_recovery(self) -> None:
        task_id = self.database.create_task("analyze_duplicates", {})
        self.database.cancel_task(task_id)
        recovered = self.database.recover_tasks()
        self.assertNotIn(task_id, recovered)
        self.assertEqual(self.database.get_task(task_id)["status"], "cancelled")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_thumbnail_uses_ffmpeg_for_unsupported_image(self) -> None:
        image_path = self.library_path / "fallback.png"
        Image.new("RGB", (640, 480), "#5d6fe8").save(image_path)
        thumbnail = Path(self.temp.name) / "fallback.jpg"
        with patch("app.services.extractors.Image.open", side_effect=UnidentifiedImageError("unsupported")):
            create_thumbnail(image_path, thumbnail, "image", 160)
        with Image.open(thumbnail) as output:
            self.assertEqual(output.size, (160, 120))

    def test_chunking_has_overlap_without_looping(self) -> None:
        text = "第一段。" * 1000
        chunks = split_chunks(text, max_chars=300, overlap=30)
        self.assertGreater(len(chunks), 5)
        self.assertEqual(chunks[0]["start_offset"], 0)
        self.assertEqual(chunks[-1]["end_offset"], len(text))

    def test_interactive_inference_has_queue_priority(self) -> None:
        gate = _PriorityGate(1)
        holder_ready = threading.Event()
        release_holder = threading.Event()
        order: list[str] = []

        def holder() -> None:
            with gate.slot():
                holder_ready.set()
                release_holder.wait(2)

        def waiter(name: str, interactive: bool) -> None:
            with gate.slot(interactive):
                order.append(name)

        holder_thread = threading.Thread(target=holder)
        background_thread = threading.Thread(target=waiter, args=("background", False))
        interactive_thread = threading.Thread(target=waiter, args=("interactive", True))
        holder_thread.start()
        self.assertTrue(holder_ready.wait(1))
        background_thread.start()
        interactive_thread.start()
        deadline = time.time() + 1
        while gate.interactive_waiters < 1 and time.time() < deadline:
            time.sleep(0.005)
        release_holder.set()
        for thread in (holder_thread, background_thread, interactive_thread):
            thread.join(2)
        self.assertEqual(order, ["interactive", "background"])

    def test_hardware_plans(self) -> None:
        nvidia = _make_plan(
            [GPU("nvidia", "RTX", "discrete", 24 * 1024**3)], 16, 32 * 1024**3,
            ["CUDAExecutionProvider"], {"hwaccels": ["cuda"], "encoders": [], "decoders": []}, [], False,
        )
        self.assertEqual((nvidia.inference_backend, nvidia.media_backend), ("cuda", "cuda"))
        intel = _make_plan(
            [GPU("intel", "UHD", "integrated")], 8, 16 * 1024**3,
            ["OpenVINOExecutionProvider"], {"hwaccels": ["qsv", "vaapi"], "encoders": [], "decoders": []}, ["/dev/dri/renderD128"], False,
        )
        self.assertEqual((intel.inference_backend, intel.media_backend), ("openvino", "qsv"))
        amd = _make_plan(
            [GPU("amd", "Radeon", "integrated")], 8, 16 * 1024**3,
            ["ROCMExecutionProvider"], {"hwaccels": ["vaapi"], "encoders": [], "decoders": []}, ["/dev/dri/renderD128"], True,
        )
        self.assertEqual((amd.inference_backend, amd.media_backend), ("rocm", "vaapi"))

    def test_project_asset_versions_comments_and_share_storage(self) -> None:
        image_path = self.library_path / "workspace.jpg"
        Image.new("RGB", (640, 480), "#4865a8").save(image_path)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file = self.database.fetchone("SELECT * FROM files WHERE name = 'workspace.jpg'")
        user = self.database.create_user("workspace-owner", "项目所有者", "hash", "owner", [])
        reviewer = self.database.create_user("workspace-reviewer", "项目审阅者", "hash", "member", [])
        workspace = WorkspaceService(self.database)
        project = workspace.create_project("宣传片", "测试项目", "#7c8cff", user["id"])
        workspace.set_member(project["id"], reviewer["id"], "reviewer")
        folder = workspace.create_folder(project["id"], "成片", None)
        asset = workspace.create_asset(project["id"], file, folder["id"], "主视觉", user["id"])
        version = workspace.add_version(asset["id"], file, "修改版", "调整颜色", user["id"])
        comment = workspace.add_comment(
            asset["id"], version["id"], "第 3 秒调整字幕", "point", 3.0, None,
            0.5, 0.5, [{"points": [{"x": 0.4, "y": 0.4}, {"x": 0.6, "y": 0.6}]}],
            "external", user["id"],
        )
        workspace.resolve_comment(comment["id"], user["id"], True)
        share, token = workspace.create_share(
            project["id"], asset["id"], "客户审阅", "", None,
            False, True, True, "仅供审阅", "测试品牌", user["id"],
        )
        detail = workspace.asset_detail(asset["id"])
        self.assertEqual(len(detail["versions"]), 2)
        self.assertTrue(detail["comments"][0]["resolved"])
        self.assertEqual(workspace.share_by_token(token)["id"], share["id"])
        workspace.add_comment(
            asset["id"], version["id"], "外部客户的新意见", "text", None, None,
            None, None, [], "external", None, "客户代表",
        )
        guest_notifications = self.database.fetchall(
            "SELECT * FROM notifications WHERE title LIKE '客户代表%' ORDER BY user_id"
        )
        self.assertEqual(
            {item["user_id"] for item in guest_notifications},
            {user["id"], reviewer["id"]},
        )
        self.assertEqual(self.database.quick_check(), "ok")

    def test_project_owner_role_invariant_and_migration_repair(self) -> None:
        owner = self.database.create_user("role-owner", "项目所有者", "hash", "owner", [])
        reviewer = self.database.create_user("role-reviewer", "项目成员", "hash", "member", [])
        workspace = WorkspaceService(self.database)
        project = workspace.create_project("权限项目", "", "#7c8cff", owner["id"])
        workspace.set_member(project["id"], reviewer["id"], "reviewer")

        with self.assertRaisesRegex(ValueError, "不能修改项目所有者"):
            workspace.set_member(project["id"], owner["id"], "manager")
        with self.assertRaisesRegex(ValueError, "不能通过成员角色转移"):
            workspace.set_member(project["id"], reviewer["id"], "owner")

        self.database.execute(
            "UPDATE project_members SET role = 'manager' WHERE project_id = ? AND user_id = ?",
            (project["id"], owner["id"]),
        )
        self.database.execute(
            "UPDATE project_members SET role = 'owner' WHERE project_id = ? AND user_id = ?",
            (project["id"], reviewer["id"]),
        )
        self.database.initialize()
        roles = {
            item["id"]: item["role"]
            for item in workspace.list_members(project["id"])
        }
        self.assertEqual(roles[owner["id"]], "owner")
        self.assertEqual(roles[reviewer["id"]], "manager")
        self.assertEqual(workspace.access_role(project["id"], owner["id"]), "owner")

    def test_backup_is_verified_and_owner_only(self) -> None:
        destination = Path(self.temp.name) / "backups" / "verified.db"
        self.database.backup(destination)
        marker = destination.with_suffix(".db.verified")
        self.assertEqual(destination.stat().st_mode & 0o077, 0)
        self.assertEqual(destination.parent.stat().st_mode & 0o077, 0)
        self.assertEqual(marker.stat().st_mode & 0o077, 0)
        self.assertEqual(Database.verify_backup(destination), "ok")
        self.assertIn('"quick_check": "ok"', marker.read_text(encoding="utf-8"))

        corrupt = destination.with_name("corrupt.db")
        corrupt.write_bytes(b"not a sqlite database")
        with self.assertRaises(Exception):
            Database.verify_backup(corrupt)

    def test_lut_preview_is_tracked_without_modifying_source(self) -> None:
        image_path = self.library_path / "look-source.jpg"
        lut_path = self.library_path / "cinema.cube"
        Image.new("RGB", (640, 360), "#557799").save(image_path)
        lut_path.write_text(
            'TITLE "Identity"\nLUT_3D_SIZE 2\n0 0 0\n0 0 1\n0 1 0\n0 1 1\n1 0 0\n1 0 1\n1 1 0\n1 1 1\n',
            encoding="utf-8",
        )
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        image = self.database.fetchone("SELECT * FROM files WHERE name = ?", (image_path.name,))
        lut = self.database.fetchone("SELECT * FROM files WHERE name = ?", (lut_path.name,))
        user = self.database.create_user("look-owner", "调色用户", "hash", "owner", [])
        workspace = WorkspaceService(self.database)
        project = workspace.create_project("调色项目", "", "#7c8cff", user["id"])
        asset = workspace.create_asset(project["id"], image, None, "调色素材", user["id"])
        version_id = asset["versions"][0]["id"]

        def fake_run(command, timeout=7200):
            Path(command[-1]).write_bytes(b"preview")

        with (
            patch("app.services.proxy.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("app.services.proxy._run", side_effect=fake_run),
        ):
            result = generate_look_preview(
                self.database,
                self.local_settings,
                version_id,
                lut["id"],
                lambda *_: None,
                lambda: False,
            )
        version = workspace.version(version_id)
        self.assertEqual(version["look_status"], "ready")
        self.assertEqual(version["look_name"], lut_path.name)
        self.assertTrue(Path(result["look_path"]).is_file())
        self.assertTrue(image_path.is_file())

    def test_short_video_proxy_generates_horizontal_filmstrip(self) -> None:
        video_path = self.library_path / "short-review.mp4"
        video_path.write_bytes(b"video fixture")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file = self.database.fetchone("SELECT * FROM files WHERE name = ?", (video_path.name,))
        self.database.execute(
            "UPDATE files SET duration = ?, mime_type = ? WHERE id = ?",
            (4.35, "video/mp4", file["id"]),
        )
        file = self.database.get_file(file["id"])
        user = self.database.create_user("proxy-owner", "代理用户", "hash", "owner", [])
        workspace = WorkspaceService(self.database)
        project = workspace.create_project("代理项目", "", "#7c8cff", user["id"])
        asset = workspace.create_asset(project["id"], file, None, "短视频", user["id"])
        version_id = asset["versions"][0]["id"]
        commands = []

        def fake_run(command, timeout=7200):
            commands.append(command)
            Path(command[-1]).write_bytes(b"preview")

        with (
            patch("app.services.proxy.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("app.services.proxy._run", side_effect=fake_run),
        ):
            result = generate_proxy(
                self.database,
                self.local_settings,
                version_id,
                lambda *_: None,
                lambda: False,
            )
        self.assertTrue(Path(result["filmstrip_path"]).is_file())
        filmstrip_command = next(command for command in commands if command[-1].endswith("filmstrip.jpg"))
        self.assertIn("tile=5x1", next(value for value in filmstrip_command if "tile=" in value))


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)

    def test_health_and_static_frontend(self) -> None:
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])
        homepage = self.client.get("/")
        self.assertEqual(homepage.status_code, 200)
        self.assertIn("NAS AI Space", homepage.text)
        script = self.client.get("/assets/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertNotIn("?token=", script.text)
        self.assertIn("authenticatedBlobUrl", script.text)
        self.assertIn("openFileViewer", script.text)
        self.assertIn("/ticket", script.text)
        self.assertIn("data-organizer-mode", homepage.text)
        self.assertIn("view-timeline", homepage.text)
        self.assertIn("indexBatchForm", homepage.text)
        self.assertIn("indexPolicyForm", homepage.text)
        self.assertIn("view-places", homepage.text)
        self.assertIn("view-events", homepage.text)
        self.assertIn("view-recycle", homepage.text)
        self.assertIn("createVectorSnapshot", homepage.text)
        self.assertIn("view-library", homepage.text)
        self.assertIn("view-albums", homepage.text)
        self.assertIn("saveSmartAlbum", homepage.text)
        self.assertIn("repairIndex", homepage.text)
        self.assertIn("bootstrapForm", homepage.text)
        self.assertIn('name="password_confirm"', homepage.text)
        self.assertIn("完成设置并进入空间", homepage.text)
        self.assertIn("response.token", script.text)
        self.assertNotIn("new URLSearchParams(location.search).get('token')", script.text)
        self.assertIn("searchParams.delete('token')", script.text)
        self.assertIn("auth_type === 'api_token'", script.text)
        self.assertIn("authReady", script.text)
        self.assertIn("publicMode", script.text)
        self.assertIn('value="owner" hidden', homepage.text)
        self.assertIn("thumbnailObserver", script.text)
        self.assertIn("conversation_id", script.text)
        self.assertIn("indexHealthMeta", homepage.text)
        self.assertIn("productionChecks", homepage.text)
        self.assertIn("projectInboxButton", homepage.text)
        self.assertIn("lookModal", homepage.text)
        model_viewer = self.client.get("/assets/model-viewer.js")
        self.assertEqual(model_viewer.status_code, 200)
        self.assertIn("mountModelViewer", model_viewer.text)
        self.assertIn("loadDashboard(true)", script.text)
        self.assertEqual(health.headers["x-content-type-options"], "nosniff")
        self.assertEqual(health.headers["x-frame-options"], "SAMEORIGIN")
        self.assertIn("default-src 'self'", health.headers["content-security-policy"])
        self.assertTrue(health.headers["x-request-id"])
        self.assertNotIn("uvicorn", health.headers.get("server", "").lower())
        self.assertNotIn("swagger-ui", self.client.get("/docs").text.lower())
        self.assertEqual(self.client.get("/openapi.json").headers["content-type"].split(";")[0], "text/html")

    def test_discover_and_batched_index_api(self) -> None:
        folder = SCAN_ROOT / f"batch-{time.time_ns()}"
        folder.mkdir()
        for index in range(3):
            (folder / f"note-{index}.txt").write_text(f"批量索引资料 {index}", encoding="utf-8")
        created = self.client.post("/api/libraries", json={"name": folder.name, "path": str(folder)})
        library_id = created.json()["id"]
        task_id = self.client.post(f"/api/libraries/{library_id}/discover").json()["task_id"]
        for _ in range(100):
            current = state.database.get_task(task_id)
            if current["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.03)
        counts = state.database.fetchone(
            """SELECT SUM(status = 'ready') AS ready, SUM(status = 'pending') AS pending
               FROM files WHERE library_id = ?""",
            (library_id,),
        )
        self.assertEqual((counts["ready"], counts["pending"]), (0, 3))

        response = self.client.post("/api/index", json={
            "library_id": library_id,
            "limit": 2,
            "kind": "document",
            "order": "smallest",
        })
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        for _ in range(100):
            current = state.database.get_task(task_id)
            if current["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.03)
        counts = state.database.fetchone(
            """SELECT SUM(status = 'ready') AS ready, SUM(status = 'pending') AS pending
               FROM files WHERE library_id = ?""",
            (library_id,),
        )
        self.assertEqual((counts["ready"], counts["pending"]), (2, 1))

    def test_index_policy_and_runtime_metrics_api(self) -> None:
        status = self.client.get("/api/index/status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("pending", status.json())
        self.assertIn("overview", status.json())
        self.assertIn("active_tasks", status.json())
        self.assertIn("caption_upgrades", status.json())
        self.assertIn("pending", status.json()["caption_upgrades"])
        self.assertIn("semantic_percent", status.json()["overview"])
        self.assertIn("eta_seconds", status.json()["overview"]["runtime"])
        updated = self.client.put("/api/index/policy", json={
            "enabled": True,
            "start_hour": 1,
            "end_hour": 6,
            "batch_size": 123,
            "library_id": None,
            "kind": "image",
            "order": "newest",
        })
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["batch_size"], 123)
        metrics = self.client.get("/api/system/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("memory", metrics.json())
        self.client.put("/api/index/policy", json={
            "enabled": False,
            "start_hour": 0,
            "end_hour": 7,
            "batch_size": 200,
            "library_id": None,
            "kind": "",
            "order": "balanced",
        })

    def test_production_readiness_api(self) -> None:
        production_settings = replace(settings, api_token="production-test-token-at-least-32-bytes")
        with (
            patch("app.main.settings", production_settings),
            patch.object(state.database, "user_count", return_value=1),
            patch.object(state.database, "bootstrap_required", return_value=False),
            patch.object(state.database, "probe", return_value="ok"),
            patch.object(state.vectors, "health", return_value={"reachable": True}),
            patch.object(state.ai, "health", return_value={"reachable": True}),
        ):
            response = self.client.get("/api/ready")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ready"])
        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(checks["database"]["level"], "ok")
        self.assertEqual(checks["authentication"]["level"], "ok")
        self.assertEqual(checks["local_ai"]["level"], "ok")
        self.assertEqual(checks["vector_store"]["level"], "ok")

    def test_login_failures_are_rate_limited(self) -> None:
        username = f"rate-limit-{time.time_ns()}"
        state.database.create_user(
            username,
            "限流测试",
            "scrypt$16384$8$1$invalid$invalid",
            "member",
            [],
        )
        LOGIN_FAILURES.clear()
        try:
            for _ in range(5):
                response = self.client.post(
                    "/api/auth/login",
                    json={"username": username, "password": "wrong-password"},
                )
                self.assertEqual(response.status_code, 401)
            blocked = self.client.post(
                "/api/auth/login",
                json={"username": username, "password": "wrong-password"},
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)
        finally:
            LOGIN_FAILURES.clear()

    def test_login_rate_limit_cannot_be_bypassed_by_rotating_usernames(self) -> None:
        LOGIN_FAILURES.clear()
        try:
            for index in range(20):
                response = self.client.post(
                    "/api/auth/login",
                    json={"username": f"missing-{index}", "password": "wrong-password"},
                )
                self.assertEqual(response.status_code, 401)
            blocked = self.client.post(
                "/api/auth/login",
                json={"username": "another-missing-user", "password": "wrong-password"},
            )
            self.assertEqual(blocked.status_code, 429)
        finally:
            LOGIN_FAILURES.clear()

    def test_first_start_creates_owner_and_logs_in(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nas-ai-bootstrap-") as directory:
            database = Database(Path(directory) / "index.db")
            database.initialize()
            with patch.object(state, "database", database):
                status = self.client.get("/api/auth/bootstrap")
                self.assertEqual(status.json(), {"required": True})
                created = self.client.post("/api/auth/bootstrap", json={
                    "username": "first-owner",
                    "display_name": "首位管理员",
                    "password": "owner-password",
                })
                self.assertEqual(created.status_code, 201)
                payload = created.json()
                self.assertTrue(payload["token"])
                self.assertEqual(payload["user"]["username"], "first-owner")
                self.assertEqual(payload["user"]["role"], "owner")
                self.assertNotIn("password_hash", payload)
                authenticated = self.client.get(
                    "/api/auth/me",
                    headers={"Authorization": f"Bearer {payload['token']}"},
                )
                self.assertEqual(authenticated.status_code, 200)
                self.assertEqual(authenticated.json()["username"], "first-owner")
                duplicate = self.client.post("/api/auth/bootstrap", json={
                    "username": "another-owner",
                    "display_name": "另一个管理员",
                    "password": "another-password",
                })
                self.assertEqual(duplicate.status_code, 409)

    def test_first_start_can_claim_an_operator_armed_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nas-ai-owner-claim-") as directory:
            database = Database(Path(directory) / "index.db")
            database.initialize()
            existing = database.create_user(
                "placeholder-admin",
                "待设置管理员",
                "scrypt$16384$8$1$invalid$invalid",
                "owner",
                [],
            )
            database.execute(
                "UPDATE users SET password_setup_required = 1 WHERE id = ?",
                (existing["id"],),
            )
            with patch.object(state, "database", database):
                self.assertEqual(
                    self.client.get("/api/auth/bootstrap").json(),
                    {"required": True},
                )
                blocked = self.client.post("/api/auth/login", json={
                    "username": "placeholder-admin",
                    "password": "placeholder-password",
                })
                self.assertEqual(blocked.status_code, 401)
                claimed = self.client.post("/api/auth/bootstrap", json={
                    "username": "chosen-owner",
                    "display_name": "自定义管理员",
                    "password": "chosen-password",
                })
                self.assertEqual(claimed.status_code, 201)
                self.assertEqual(claimed.json()["user"]["id"], existing["id"])
                self.assertEqual(database.user_count(), 1)
                self.assertFalse(database.bootstrap_required())

    def test_owner_can_update_profile_without_losing_role(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nas-ai-owner-update-") as directory:
            database = Database(Path(directory) / "index.db")
            database.initialize()
            secured = replace(settings, api_token="owner-update-system-token")
            with patch.object(state, "database", database), patch("app.main.settings", secured):
                created = self.client.post("/api/auth/bootstrap", json={
                    "username": "profile-owner",
                    "display_name": "原显示名称",
                    "password": "profile-password",
                })
                self.assertEqual(created.status_code, 201)
                token = created.json()["token"]
                user_id = created.json()["user"]["id"]
                updated = self.client.put(
                    f"/api/users/{user_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "display_name": "新显示名称",
                        "password": "",
                        "role": "owner",
                        "enabled": True,
                        "library_ids": [],
                    },
                )
                self.assertEqual(updated.status_code, 200)
                self.assertEqual(updated.json()["role"], "owner")
                self.assertEqual(updated.json()["display_name"], "新显示名称")
                disabled = self.client.put(
                    f"/api/users/{user_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "display_name": "新显示名称",
                        "password": "",
                        "role": "owner",
                        "enabled": False,
                        "library_ids": [],
                    },
                )
                self.assertEqual(disabled.status_code, 409)

    def test_api_token_cannot_create_ownerless_project_or_assign_owner_role(self) -> None:
        secured = replace(settings, api_token="project-system-token")
        headers = {"Authorization": "Bearer project-system-token"}
        with patch("app.main.settings", secured):
            project = self.client.post(
                "/api/projects",
                headers=headers,
                json={"name": "无所有者项目", "description": "", "color": "#7c8cff"},
            )
            self.assertEqual(project.status_code, 409)
            role = self.client.put(
                "/api/projects/1/members",
                headers=headers,
                json={"user_id": 1, "role": "owner"},
            )
            self.assertEqual(role.status_code, 422)

    def test_library_scan_api(self) -> None:
        folder = SCAN_ROOT / f"api-{time.time_ns()}"
        folder.mkdir()
        (folder / "note.txt").write_text("极空间测试资料", encoding="utf-8")
        created = self.client.post("/api/libraries", json={"name": folder.name, "path": str(folder)})
        self.assertEqual(created.status_code, 201)
        task = self.client.post(f"/api/libraries/{created.json()['id']}/scan")
        self.assertEqual(task.status_code, 202)
        task_id = task.json()["task_id"]
        status = "pending"
        for _ in range(80):
            tasks = self.client.get("/api/tasks").json()
            current = next(item for item in tasks if item["id"] == task_id)
            status = current["status"]
            if status in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        self.assertEqual(status, "completed")
        search = self.client.get("/api/search", params={"q": "极空间"})
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()["results"][0]["name"], "note.txt")

    def test_concurrent_scans_keep_all_library_files(self) -> None:
        folder = SCAN_ROOT / f"concurrent-{time.time_ns()}"
        folder.mkdir()
        for index in range(40):
            (folder / f"file-{index}.txt").write_text(str(index), encoding="utf-8")
        created = self.client.post("/api/libraries", json={"name": folder.name, "path": str(folder)})
        library_id = created.json()["id"]
        task_ids = [
            self.client.post(f"/api/libraries/{library_id}/discover").json()["task_id"]
            for _ in range(2)
        ]
        for _ in range(150):
            statuses = [state.database.get_task(task_id)["status"] for task_id in task_ids]
            if all(status in {"completed", "failed", "cancelled"} for status in statuses):
                break
            time.sleep(0.03)
        self.assertEqual(statuses, ["completed", "completed"])
        count = state.database.fetchone(
            "SELECT COUNT(*) AS count FROM files WHERE library_id = ?", (library_id,)
        )
        self.assertEqual(count["count"], 40)

    def test_timeline_filters_and_ticketed_range_media(self) -> None:
        folder = SCAN_ROOT / f"timeline-{time.time_ns()}"
        folder.mkdir()
        media = folder / "sample.mp4"
        media.write_bytes(b"0123456789abcdef")
        created = self.client.post("/api/libraries", json={"name": folder.name, "path": str(folder)})
        task_id = self.client.post(f"/api/libraries/{created.json()['id']}/scan").json()["task_id"]
        for _ in range(100):
            current = next(item for item in self.client.get("/api/tasks").json() if item["id"] == task_id)
            if current["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.03)
        file_row = state.database.fetchone("SELECT * FROM files WHERE name = ?", (media.name,))
        state.database.execute("UPDATE files SET captured_at = ? WHERE id = ?", ("2023-08-09T10:11:12", file_row["id"]))

        timeline = self.client.get("/api/timeline", params={"year": 2023, "month": 8, "kind": "video"})
        self.assertEqual(timeline.status_code, 200)
        self.assertTrue(any(item["id"] == file_row["id"] for item in timeline.json()["items"]))

        ticket = self.client.post(f"/api/files/{file_row['id']}/ticket")
        self.assertEqual(ticket.status_code, 200)
        self.assertNotIn("token=", ticket.json()["url"])
        ranged = self.client.get(ticket.json()["url"], headers={"Range": "bytes=2-5"})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, b"2345")
        self.assertEqual(ranged.headers["content-range"], "bytes 2-5/16")

    def test_retry_endpoint_runs_cancelled_task(self) -> None:
        task_id = state.database.create_task("analyze_duplicates", {})
        state.database.mark_task_cancelled(task_id)
        response = self.client.post(f"/api/tasks/{task_id}/retry")
        self.assertEqual(response.status_code, 202)
        status = "pending"
        for _ in range(100):
            current = state.database.get_task(task_id)
            status = current["status"]
            if status in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.03)
        self.assertEqual(status, "completed")

    def test_upload_is_streamed_into_separate_library(self) -> None:
        response = self.client.post(
            "/api/uploads",
            content=b"local upload content",
            headers={"X-Filename": "upload-note.txt"},
        )
        self.assertEqual(response.status_code, 201)
        uploaded = response.json()["file"]
        row = state.database.get_file(uploaded["id"])
        self.assertTrue(Path(row["path"]).is_relative_to(settings.upload_root))
        self.assertEqual(Path(row["path"]).read_bytes(), b"local upload content")

    def test_local_user_login_and_library_permissions(self) -> None:
        username = f"member-{time.time_ns()}"
        libraries = self.client.get("/api/libraries").json()
        visible_library = libraries[0]["id"]
        created = self.client.post("/api/users", json={
            "username": username,
            "display_name": "测试成员",
            "password": "strong-password",
            "role": "member",
            "library_ids": [visible_library],
        })
        self.assertEqual(created.status_code, 201)
        login = self.client.post("/api/auth/login", json={"username": username, "password": "strong-password"})
        self.assertEqual(login.status_code, 200)
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        visible = self.client.get("/api/libraries", headers=headers)
        self.assertEqual([item["id"] for item in visible.json()], [visible_library])
        self.assertEqual(self.client.get("/api/users", headers=headers).status_code, 403)

    def test_operations_backup_and_people_endpoints(self) -> None:
        status = self.client.get("/api/operations/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["database"]["quick_check"], "ok")
        backup = self.client.post("/api/operations/backups")
        self.assertEqual(backup.status_code, 201)
        backup_path = settings.data_dir / "backups" / backup.json()["name"]
        self.assertTrue(backup_path.is_file())
        self.assertTrue(backup_path.with_suffix(".db.verified").is_file())
        self.assertEqual(backup_path.stat().st_mode & 0o077, 0)
        people = self.client.get("/api/people")
        self.assertEqual(people.status_code, 200)
        self.assertIn("items", people.json())
        watcher = self.client.get("/api/system/watcher")
        self.assertEqual(watcher.status_code, 200)
        snapshots = self.client.get("/api/operations/vector-snapshots")
        self.assertEqual(snapshots.status_code, 200)
        self.assertIn("collection", snapshots.json())

    def test_personal_library_search_album_and_conversation_endpoints(self) -> None:
        file_row = state.database.fetchone("SELECT * FROM files ORDER BY id LIMIT 1")
        self.assertIsNotNone(file_row)
        username = f"personal-{time.time_ns()}"
        created = self.client.post("/api/users", json={
            "username": username,
            "display_name": "个人功能测试",
            "password": "strong-password",
            "role": "member",
            "library_ids": [file_row["library_id"]],
        })
        self.assertEqual(created.status_code, 201)
        login = self.client.post("/api/auth/login", json={"username": username, "password": "strong-password"})
        headers = {"Authorization": f"Bearer {login.json()['token']}"}

        favorite = self.client.put(f"/api/files/{file_row['id']}/favorite", params={"enabled": True}, headers=headers)
        self.assertEqual(favorite.status_code, 200)
        tags = self.client.put(
            f"/api/files/{file_row['id']}/tags",
            json={"tags": ["重要", "测试"]},
            headers=headers,
        )
        self.assertEqual(tags.json()["tags"], ["重要", "测试"])
        filtered = self.client.get("/api/files", params={"favorite": True, "tag": "重要"}, headers=headers)
        self.assertTrue(any(item["id"] == file_row["id"] for item in filtered.json()["items"]))

        album = self.client.post("/api/smart-albums", json={
            "name": "重要资料",
            "query": file_row["name"],
            "kind": file_row["kind"],
            "filters": {"favorite": True, "tag": "重要"},
        }, headers=headers)
        self.assertEqual(album.status_code, 201)
        album_items = self.client.get(f"/api/smart-albums/{album.json()['id']}/items", headers=headers)
        self.assertEqual(album_items.status_code, 200)
        self.assertEqual(album_items.json()["album"]["name"], "重要资料")

        with patch.object(state.ai, "answer", return_value="这是本地资料中的测试答案。"):
            answer = self.client.post("/api/ask", json={"question": file_row["name"]}, headers=headers)
        self.assertEqual(answer.status_code, 200)
        conversation_id = answer.json()["conversation_id"]
        self.assertIsNotNone(conversation_id)
        conversation = self.client.get(f"/api/conversations/{conversation_id}", headers=headers)
        self.assertEqual(conversation.status_code, 200)
        self.assertEqual(len(conversation.json()["messages"]), 2)

    def test_project_review_share_and_export_endpoints(self) -> None:
        upload = self.client.post(
            "/api/uploads",
            content=b"workspace media",
            headers={"X-Filename": "workspace-media.txt"},
        )
        self.assertEqual(upload.status_code, 201)
        file_id = upload.json()["file"]["id"]
        project = self.client.post("/api/projects", json={
            "name": "端到端项目",
            "description": "项目审阅测试",
            "color": "#7c8cff",
        })
        self.assertEqual(project.status_code, 201)
        project_id = project.json()["id"]
        folder = self.client.post(f"/api/projects/{project_id}/folders", json={
            "name": "交付",
            "parent_id": None,
        })
        self.assertEqual(folder.status_code, 201)
        asset = self.client.post(f"/api/projects/{project_id}/assets", json={
            "file_id": file_id,
            "folder_id": folder.json()["id"],
            "title": "测试素材",
        })
        self.assertEqual(asset.status_code, 201)
        asset_id = asset.json()["id"]
        version_id = asset.json()["versions"][0]["id"]
        comment = self.client.post(f"/api/assets/{asset_id}/comments", json={
            "version_id": version_id,
            "body": "请修改这里",
            "comment_type": "point",
            "time_start": 1.5,
            "drawing": [{"points": [{"x": 0.1, "y": 0.2}, {"x": 0.3, "y": 0.4}]}],
            "visibility": "external",
        })
        self.assertEqual(comment.status_code, 201)
        tasks = self.client.get(f"/api/projects/{project_id}/review-tasks")
        self.assertEqual(tasks.json()["total"], 1)
        qc = self.client.get(f"/api/assets/{asset_id}/qc")
        self.assertEqual(qc.status_code, 200)
        share = self.client.post(f"/api/projects/{project_id}/shares", json={
            "asset_id": asset_id,
            "name": "客户审阅",
            "can_comment": True,
            "can_view_versions": True,
            "watermark_text": "仅供审阅",
            "brand_name": "测试品牌",
        })
        self.assertEqual(share.status_code, 201)
        token = share.json()["token"]
        public = self.client.post(f"/api/public/shares/{token}", json={"access_code": ""})
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.json()["assets"][0]["title"], "测试素材")
        guest = self.client.post(f"/api/public/shares/{token}/comments", json={
            "asset_id": asset_id,
            "version_id": version_id,
            "guest_name": "客户",
            "body": "可以交付",
            "access_code": "",
        })
        self.assertEqual(guest.status_code, 201)
        csv_export = self.client.get(f"/api/projects/{project_id}/review-export?format=csv")
        self.assertEqual(csv_export.status_code, 200)
        self.assertIn("请修改这里", csv_export.text)
        xml_export = self.client.get(f"/api/projects/{project_id}/review-export?format=fcpxml")
        self.assertEqual(xml_export.status_code, 200)
        self.assertIn("<fcpxml", xml_export.text)

        protected_share = self.client.post(f"/api/projects/{project_id}/shares", json={
            "asset_id": asset_id,
            "name": "口令审阅",
            "access_code": "correct-access-code",
            "can_comment": True,
        })
        protected_token = protected_share.json()["token"]
        PUBLIC_ACCESS_FAILURES.clear()
        try:
            for _ in range(10):
                rejected = self.client.post(
                    f"/api/public/shares/{protected_token}",
                    json={"access_code": "wrong-access-code"},
                )
                self.assertEqual(rejected.status_code, 401)
            blocked = self.client.post(
                f"/api/public/shares/{protected_token}",
                json={"access_code": "wrong-access-code"},
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertIn("retry-after", blocked.headers)
        finally:
            PUBLIC_ACCESS_FAILURES.clear()

    def test_project_inbox_collects_and_indexes_dropped_files(self) -> None:
        project = self.client.post("/api/projects", json={
            "name": f"入库项目-{time.time_ns()}",
            "description": "FTP 与 SMB 入库测试",
            "color": "#55d6a7",
        })
        self.assertEqual(project.status_code, 201)
        project_id = project.json()["id"]
        inbox = settings.ingest_root / f"project-{project_id}"
        inbox.mkdir(parents=True, exist_ok=True)
        dropped = inbox / "camera-drop.txt"
        dropped.write_text("从 NAS 入库箱投递的制作资料", encoding="utf-8")
        package = inbox / "eagle-library.eaglepack"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("nested/eagle-note.txt", "从 Eagle 素材包导入的资料")
        try:
            status = self.client.get(f"/api/projects/{project_id}/inbox")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["relative_path"], f"inbox/project-{project_id}")
            submitted = self.client.post(f"/api/projects/{project_id}/inbox/collect")
            self.assertEqual(submitted.status_code, 202)
            task_id = submitted.json()["task_id"]
            task_status = "pending"
            for _ in range(200):
                task = state.database.get_task(task_id)
                task_status = task["status"]
                if task_status in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.03)
            self.assertEqual(task_status, "completed")
            assets = self.client.get(f"/api/projects/{project_id}/assets").json()["items"]
            self.assertTrue(any(item["file_name"] == dropped.name for item in assets))
            self.assertTrue(any(item["file_name"] == "eagle-note.txt" for item in assets))
            self.assertFalse(any(item["file_name"] == package.name for item in assets))
            indexed = state.database.fetchone("SELECT status FROM files WHERE path = ?", (str(dropped),))
            self.assertEqual(indexed["status"], "ready")
        finally:
            dropped.unlink(missing_ok=True)
            package.unlink(missing_ok=True)
            shutil.rmtree(inbox, ignore_errors=True)
            self.client.delete(f"/api/projects/{project_id}")


if __name__ == "__main__":
    unittest.main()
