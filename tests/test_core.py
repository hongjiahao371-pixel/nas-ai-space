from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec, module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock, patch

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
from app.services.watcher import LibraryWatcher
from app.services.workspaces import WorkspaceService

# ops 边车是纯 stdlib 脚本（docker/ 不是包），按文件路径加载以单测其校验与持久化逻辑
_OPS_AGENT_SPEC = spec_from_file_location(
    "ops_agent", Path(__file__).resolve().parent.parent / "docker" / "ops_agent.py"
)
ops_agent = module_from_spec(_OPS_AGENT_SPEC)
_OPS_AGENT_SPEC.loader.exec_module(ops_agent)


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


def _make_test_font(path: Path) -> None:
    # 用 fontTools 现场生成一个最小可用 TTF，避免测试依赖系统字体
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    builder = FontBuilder(1000)
    builder.setupGlyphOrder([".notdef", "A"])
    builder.setupCharacterMap({ord("A"): "A"})
    pen = TTGlyphPen(None)
    pen.moveTo((50, 0))
    pen.lineTo((250, 700))
    pen.lineTo((450, 0))
    pen.closePath()
    builder.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "A": pen.glyph()})
    builder.setupHorizontalMetrics({".notdef": (500, 0), "A": (500, 0)})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "TestFont", "styleName": "Regular", "fullName": "TestFont Regular"})
    builder.setupOS2()
    builder.setupPost()
    builder.save(path)


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

    def test_design_files_are_scanned_as_images(self) -> None:
        for name in ("poster.psd", "big.psb", "vector.ai", "logo.eps", "brand.ttf", "type.otf", "pack.ttc"):
            (self.library_path / name).write_bytes(b"fake")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        rows = self.database.fetchall("SELECT name, kind FROM files")
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(row["kind"] == "image" for row in rows))

    def test_psd_preview_uses_psd_tools(self) -> None:
        psd_path = self.library_path / "poster.psd"
        psd_path.write_bytes(b"fake-psd")
        thumbnail = Path(self.temp.name) / "psd.jpg"

        class FakePsd:
            @staticmethod
            def composite():
                return Image.new("RGB", (800, 400), "#336699")

        with patch("psd_tools.PSDImage.open", return_value=FakePsd()):
            create_thumbnail(psd_path, thumbnail, "image", 320)
        with Image.open(thumbnail) as output:
            self.assertEqual(output.size, (320, 160))

    def test_psd_oversize_is_skipped(self) -> None:
        psd_path = self.library_path / "huge.psd"
        psd_path.write_bytes(b"fake-psd")
        with patch("app.services.extractors.settings", replace(settings, max_psd_bytes=1)):
            with self.assertRaises(ValueError):
                create_thumbnail(psd_path, Path(self.temp.name) / "huge.jpg", "image", 320)

    def test_psd_index_falls_back_to_psd_tools(self) -> None:
        psd_path = self.library_path / "art.psd"
        psd_path.write_bytes(b"fake-psd")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file = self.database.get_file(self.database.pending_file_ids()[0])
        self.local_settings.cache_dir.mkdir(parents=True, exist_ok=True)

        class FakePsd:
            @staticmethod
            def composite():
                return Image.new("RGB", (1024, 512), "#7a3bd0")

        with patch("psd_tools.PSDImage.open", return_value=FakePsd()):
            result, _ = index_file(file, self.local_settings, LocalAIClient(self.local_settings))
        self.assertEqual((result["width"], result["height"]), (1024, 512))
        self.assertEqual(result["metadata"]["decoder"], "psd-tools")

    def test_ai_preview_uses_poppler_pdf_path(self) -> None:
        ai_path = self.library_path / "vector.ai"
        ai_path.write_bytes(b"%PDF-1.5 fake")
        thumbnail = Path(self.temp.name) / "ai.jpg"

        def fake_render(path, page, destination):
            Image.new("RGB", (640, 480), "#cccccc").save(destination)
            return True

        with patch("app.services.extractors._render_pdf_page", side_effect=fake_render):
            create_thumbnail(ai_path, thumbnail, "image", 320)
        with Image.open(thumbnail) as output:
            self.assertEqual(output.size, (640, 480))

    def test_ai_without_pdf_page_is_rejected(self) -> None:
        ai_path = self.library_path / "legacy.ai"
        ai_path.write_bytes(b"fake-ai")
        with patch("app.services.extractors._render_pdf_page", return_value=False):
            with self.assertRaises(ValueError):
                create_thumbnail(ai_path, Path(self.temp.name) / "legacy.jpg", "image", 320)

    def test_eps_without_ghostscript_is_marked_unsupported(self) -> None:
        eps_path = self.library_path / "logo.eps"
        eps_path.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0")
        with patch("app.services.extractors.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Ghostscript"):
                create_thumbnail(eps_path, Path(self.temp.name) / "eps.jpg", "image", 320)

    def test_font_preview_renders_sample_sheet(self) -> None:
        font_path = self.library_path / "brand.ttf"
        _make_test_font(font_path)
        thumbnail = Path(self.temp.name) / "font.jpg"
        create_thumbnail(font_path, thumbnail, "image", 320)
        with Image.open(thumbnail) as output:
            self.assertEqual(output.size, (320, 240))

    def test_broken_font_degrades_with_error(self) -> None:
        font_path = self.library_path / "broken.ttf"
        font_path.write_bytes(b"not-a-font")
        with self.assertRaises(ValueError):
            create_thumbnail(font_path, Path(self.temp.name) / "broken.jpg", "image", 320)

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

    def tearDown(self) -> None:
        # 匿名 owner 兜底只在 users 表为空时生效，每个用例结束后清掉测试账号避免相互影响
        state.database.execute("DELETE FROM users")

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

    def test_comment_attachments_visibility_scope_and_notifications(self) -> None:
        upload = self.client.post(
            "/api/uploads",
            content=b"attachment media",
            headers={"X-Filename": "attachment-media.txt"},
        )
        self.assertEqual(upload.status_code, 201)
        file_id = upload.json()["file"]["id"]
        project = self.client.post("/api/projects", json={
            "name": "附件项目",
            "description": "评论附件测试",
            "color": "#7c8cff",
        })
        self.assertEqual(project.status_code, 201)
        project_id = project.json()["id"]
        asset = self.client.post(f"/api/projects/{project_id}/assets", json={
            "file_id": file_id,
            "title": "附件素材",
        })
        self.assertEqual(asset.status_code, 201)
        asset_id = asset.json()["id"]
        internal = self.client.post(f"/api/assets/{asset_id}/comments", json={
            "body": "团队内部意见",
            "visibility": "team",
        })
        self.assertEqual(internal.status_code, 201)
        internal_id = internal.json()["id"]
        external = self.client.post(f"/api/assets/{asset_id}/comments", json={
            "body": "外部可见意见",
            "visibility": "external",
        })
        self.assertEqual(external.status_code, 201)
        external_id = external.json()["id"]

        png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        attachment = self.client.post(
            f"/api/comments/{external_id}/attachments",
            content=png,
            headers={"X-Filename": "%E6%88%AA%E5%9B%BE.png"},
        )
        self.assertEqual(attachment.status_code, 201)
        self.assertEqual(attachment.json()["original_name"], "截图.png")
        stored = settings.data_dir / "comment-attachments" / attachment.json()["name"]
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.read_bytes(), png)
        self.assertNotIn("/", attachment.json()["name"])
        bad_type = self.client.post(
            f"/api/comments/{external_id}/attachments",
            content=b"payload",
            headers={"X-Filename": "evil.exe"},
        )
        self.assertEqual(bad_type.status_code, 400)
        traversal = self.client.post(
            f"/api/comments/{external_id}/attachments",
            content=png,
            headers={"X-Filename": "../escape.png"},
        )
        self.assertEqual(traversal.status_code, 201)
        self.assertTrue((settings.data_dir / "comment-attachments" / traversal.json()["name"]).is_file())

        detail = self.client.get(f"/api/assets/{asset_id}")
        comments = {item["id"]: item for item in detail.json()["comments"]}
        self.assertEqual(len(comments[external_id]["attachments"]), 2)
        ticket_url = comments[external_id]["attachments"][0]["url"]
        downloaded = self.client.get(ticket_url)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, png)

        share = self.client.post(f"/api/projects/{project_id}/shares", json={
            "asset_id": asset_id,
            "name": "附件审阅",
            "can_comment": True,
        })
        self.assertEqual(share.status_code, 201)
        token = share.json()["token"]
        public = self.client.post(f"/api/public/shares/{token}", json={"access_code": ""})
        self.assertEqual(public.status_code, 200)
        public_comments = public.json()["assets"][0]["comments"]
        self.assertEqual({item["body"] for item in public_comments}, {"外部可见意见"})
        public_attachment = public_comments[0]["attachments"][0]
        served = self.client.get(public_attachment["url"])
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.content, png)

        guest = self.client.post(f"/api/public/shares/{token}/comments", json={
            "asset_id": asset_id,
            "guest_name": "客户",
            "body": "外部附件意见",
            "access_code": "",
        })
        self.assertEqual(guest.status_code, 201)
        guest_attachment = self.client.post(
            f"/api/public/shares/{token}/comments/{guest.json()['id']}/attachments?access_code=",
            content=png,
            headers={"X-Filename": "guest.png"},
        )
        self.assertEqual(guest_attachment.status_code, 201)
        guest_stored = settings.data_dir / "comment-attachments" / guest_attachment.json()["name"]
        member_comment_attach = self.client.post(
            f"/api/public/shares/{token}/comments/{external_id}/attachments?access_code=",
            content=png,
            headers={"X-Filename": "guest.png"},
        )
        self.assertEqual(member_comment_attach.status_code, 404)
        internal_comment_attach = self.client.post(
            f"/api/public/shares/{token}/comments/{internal_id}/attachments?access_code=",
            content=png,
            headers={"X-Filename": "guest.png"},
        )
        self.assertEqual(internal_comment_attach.status_code, 404)

        csv_all = self.client.get(f"/api/projects/{project_id}/review-export?format=csv")
        self.assertIn("可见范围", csv_all.text)
        self.assertIn("团队内部意见", csv_all.text)
        csv_external = self.client.get(f"/api/projects/{project_id}/review-export?format=csv&scope=external")
        self.assertIn("外部可见意见", csv_external.text)
        self.assertNotIn("团队内部意见", csv_external.text)
        xml_team = self.client.get(f"/api/projects/{project_id}/review-export?format=fcpxml&scope=team")
        self.assertIn("【团队内部】", xml_team.text)
        self.assertNotIn("外部可见意见", xml_team.text)

        username = f"notify-{time.time_ns()}"
        created = self.client.post("/api/users", json={
            "username": username,
            "display_name": "通知用户",
            "password": "notify-password-1",
            "role": "admin",
        })
        self.assertEqual(created.status_code, 201)
        user_id = created.json()["id"]
        # users 表已有账号后匿名 owner 兜底失效，后续管理操作一律携带该管理员的会话
        login = self.client.post("/api/auth/login", json={
            "username": username,
            "password": "notify-password-1",
        })
        self.assertEqual(login.status_code, 200)
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        membership = self.client.put(f"/api/projects/{project_id}/members", json={
            "user_id": user_id,
            "role": "reviewer",
        }, headers=headers)
        self.assertEqual(membership.status_code, 200)
        guest_two = self.client.post(f"/api/public/shares/{token}/comments", json={
            "asset_id": asset_id,
            "guest_name": "客户",
            "body": "第二条外部意见",
            "access_code": "",
        })
        self.assertEqual(guest_two.status_code, 201)
        notifications = self.client.get("/api/notifications", headers=headers)
        self.assertEqual(notifications.status_code, 200)
        self.assertGreaterEqual(notifications.json()["unread"], 1)
        unread_id = next(
            item["id"] for item in notifications.json()["items"] if not item["read_at"]
        )
        read_one = self.client.post(
            f"/api/notifications/read?notification_id={unread_id}",
            headers=headers,
        )
        self.assertEqual(read_one.status_code, 200)
        read_all = self.client.post("/api/notifications/read", headers=headers)
        self.assertEqual(read_all.status_code, 200)
        self.assertEqual(
            self.client.get("/api/notifications", headers=headers).json()["unread"],
            0,
        )

        task_id = state.database.create_task("analyze_duplicates", {}, user_id=user_id)
        state.database.fail_task(task_id, "测试失败")
        retry = self.client.post(f"/api/tasks/{task_id}/retry", headers=headers)
        self.assertEqual(retry.status_code, 202)
        status = "pending"
        for _ in range(100):
            status = state.database.get_task(task_id)["status"]
            if status in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.03)
        self.assertEqual(status, "completed")
        task_notifications = state.database.fetchall(
            "SELECT * FROM notifications WHERE user_id = ? AND type = 'task.finished' AND target_id = ?",
            (user_id, str(task_id)),
        )
        self.assertEqual(len(task_notifications), 1)
        self.assertIn("完成", task_notifications[0]["title"])

        deleted = self.client.delete(f"/api/comments/{external_id}", headers=headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(stored.exists())
        self.assertFalse((settings.data_dir / "comment-attachments" / traversal.json()["name"]).exists())
        removed = self.client.delete(f"/api/projects/{project_id}", headers=headers)
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(guest_stored.exists())

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


class SecurityHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)

    def tearDown(self) -> None:
        # 匿名 owner 兜底只在 users 表为空时生效，每个用例结束后清掉测试账号避免相互影响
        state.database.execute("DELETE FROM users")

    def _shared_asset(self, name: str) -> tuple[int, int]:
        upload = self.client.post(
            "/api/uploads",
            content=f"{name} media".encode(),
            headers={"X-Filename": f"security-{name}.txt"},
        )
        self.assertEqual(upload.status_code, 201)
        file_id = upload.json()["file"]["id"]
        project = self.client.post("/api/projects", json={
            "name": f"{name}-{time.time_ns()}",
            "color": "#7c8cff",
        })
        self.assertEqual(project.status_code, 201)
        project_id = project.json()["id"]
        asset = self.client.post(f"/api/projects/{project_id}/assets", json={
            "file_id": file_id,
            "title": f"{name}素材",
        })
        self.assertEqual(asset.status_code, 201)
        return project_id, asset.json()["id"]

    def test_public_comment_wrong_access_code_counts_toward_rate_limit(self) -> None:
        from app.main import PUBLIC_ATTACHMENT_ATTEMPTS, PUBLIC_COMMENT_ATTEMPTS

        project_id, asset_id = self._shared_asset("rate-limit")
        share = self.client.post(f"/api/projects/{project_id}/shares", json={
            "asset_id": asset_id,
            "name": "口令限流分享",
            "access_code": "correct-code",
            "can_comment": True,
        })
        self.assertEqual(share.status_code, 201)
        token = share.json()["token"]
        PUBLIC_ACCESS_FAILURES.clear()
        PUBLIC_COMMENT_ATTEMPTS.clear()
        try:
            missing = self.client.post("/api/public/shares/not-a-real-token/comments", json={
                "asset_id": asset_id,
                "guest_name": "访客",
                "body": "不存在的分享",
                "access_code": "whatever",
            })
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(PUBLIC_ACCESS_FAILURES, {})
            for _ in range(10):
                rejected = self.client.post(f"/api/public/shares/{token}/comments", json={
                    "asset_id": asset_id,
                    "guest_name": "访客",
                    "body": "爆破尝试",
                    "access_code": "wrong-code",
                })
                self.assertEqual(rejected.status_code, 401)
            blocked = self.client.post(f"/api/public/shares/{token}", json={"access_code": "correct-code"})
            self.assertEqual(blocked.status_code, 429)
            self.assertIn("retry-after", blocked.headers)
        finally:
            PUBLIC_ACCESS_FAILURES.clear()
            PUBLIC_COMMENT_ATTEMPTS.clear()
            PUBLIC_ATTACHMENT_ATTEMPTS.clear()

    def test_share_access_code_min_length(self) -> None:
        project_id, _ = self._shared_asset("access-code")
        short = self.client.post(f"/api/projects/{project_id}/shares", json={
            "name": "短码分享",
            "access_code": "12345",
        })
        self.assertEqual(short.status_code, 400)
        empty = self.client.post(f"/api/projects/{project_id}/shares", json={
            "name": "无码分享",
            "access_code": "",
        })
        self.assertEqual(empty.status_code, 201)
        valid = self.client.post(f"/api/projects/{project_id}/shares", json={
            "name": "正常分享",
            "access_code": "123456",
        })
        self.assertEqual(valid.status_code, 201)

    def test_password_change_invalidates_sessions(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="nas-ai-session-")
        self.addCleanup(temp.cleanup)
        database = Database(Path(temp.name) / "index.db")
        database.initialize()
        user = database.create_user("会话用户", "会话用户", "old-hash", "member", [])
        user_id = int(user["id"])
        token_hash = "session-token-hash"
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
        database.create_session(user_id, token_hash, expires_at)
        self.assertIsNotNone(database.resolve_session(token_hash))
        database.set_user(user_id, "会话用户", "member", True, [])
        self.assertIsNotNone(database.resolve_session(token_hash))
        database.set_user(user_id, "会话用户", "member", True, [], "new-hash")
        self.assertIsNone(database.resolve_session(token_hash))

    def test_workspace_tickets_thread_safety(self) -> None:
        from app.main import WORKSPACE_TICKETS_LOCK, _workspace_ticket

        temp = tempfile.TemporaryDirectory(prefix="nas-ai-tickets-")
        self.addCleanup(temp.cleanup)
        source = Path(temp.name) / "ticket-media.txt"
        source.write_text("ticket media", encoding="utf-8")
        now = time.monotonic()
        state.workspace_tickets = {
            f"seed-{index}": (str(source), "text/plain", "seed.txt", now + 3600, False)
            for index in range(4096)
        }
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(50):
                    url = _workspace_ticket(str(source), "text/plain", "ticket-media.txt")
                    ticket = url.rsplit("/", 1)[-1]
                    with WORKSPACE_TICKETS_LOCK:
                        state.workspace_tickets.pop(ticket, None)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertLessEqual(len(state.workspace_tickets), 4096)
        finally:
            state.workspace_tickets = {}


class _RecordingVectors:
    def __init__(self):
        self.filters = []

    def search(self, vector, limit, kind="", library_ids=None, file_ids=None):
        self.filters.append(None if file_ids is None else list(file_ids))
        return []


class _RecordingConnection:
    def __init__(self, connection, statements):
        self._connection = connection
        self._statements = statements

    def execute(self, sql, parameters=()):
        self._statements.append(sql)
        return self._connection.execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        batch = list(seq_of_parameters)
        self._statements.append(f"{sql} [x{len(batch)}]")
        return self._connection.executemany(sql, batch)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _WatcherDatabase:
    def __init__(self, path):
        self.path = path

    def list_libraries(self):
        return [{"id": 1, "path": str(self.path), "enabled": 1}]


class PerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="nas-ai-perf-")
        root = Path(self.temp.name)
        self.library_path = root / "library"
        self.library_path.mkdir()
        self.database = Database(root / "index.db")
        self.database.initialize()
        self.library = self.database.create_library("性能资料", str(self.library_path))
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

    def _record_statements(self):
        statements = []
        original_transaction = self.database.transaction

        @contextmanager
        def recording_transaction():
            with original_transaction() as connection:
                yield _RecordingConnection(connection, statements)

        return statements, patch.object(self.database, "transaction", recording_transaction)

    def test_like_fallback_runs_once_and_scans_only_path_columns(self) -> None:
        (self.library_path / "无关文件.txt").write_text("没有任何匹配词", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        statements = []
        original_fetchall = self.database.fetchall

        def recording_fetchall(query, params=()):
            statements.append(query)
            return original_fetchall(query, params)

        search = SearchService(self.database, LocalAIClient(self.local_settings), NullVectors())
        with patch.object(self.database, "fetchall", recording_fetchall):
            # 词长 >= 3：单次 LIKE 兜底只扫 name/relative_path 两个短列
            search.search("report summary")
            like_queries = [sql for sql in statements if " LIKE " in sql]
            self.assertEqual(len(like_queries), 1)
            self.assertIn("f.name LIKE ? OR f.relative_path LIKE ?", like_queries[0])
            self.assertNotIn("extracted_text LIKE", like_queries[0])
            self.assertNotIn("ai_caption LIKE", like_queries[0])
            # 词长 < 3（双字中文）超出 trigram 最小匹配长度，单次兜底必须覆盖大文本列，
            # 但仍然整个搜索只执行一次，不再每组一次
            statements.clear()
            search.search("猫咪 花盆")
            like_queries = [sql for sql in statements if " LIKE " in sql]
            self.assertEqual(len(like_queries), 1)
            self.assertIn("extracted_text LIKE", like_queries[0])

    def test_fts_syntax_error_retries_with_cleaned_query(self) -> None:
        document = self.library_path / "weekly-report.txt"
        document.write_text("quarterly report summary", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file_id = self.database.pending_file_ids()[0]
        file = self.database.get_file(file_id)
        result, chunks = index_file(file, self.local_settings, LocalAIClient(self.local_settings))
        self.database.finish_file_index(file_id, result, chunks)

        statements = []
        original_fetchall = self.database.fetchall

        def recording_fetchall(query, params=()):
            statements.append(query)
            return original_fetchall(query, params)

        search = SearchService(self.database, LocalAIClient(self.local_settings), NullVectors())
        with patch.object(self.database, "fetchall", recording_fetchall):
            response = search.search('report "')
        # 未闭合引号触发 FTS OperationalError 后，先用清理后的表达式重试命中，不再退 LIKE
        like_queries = [sql for sql in statements if " LIKE " in sql]
        self.assertEqual(like_queries, [])
        self.assertEqual(response["results"][0]["name"], "weekly-report.txt")

    def test_upsert_files_preloads_and_batches_unchanged_updates(self) -> None:
        for index in range(3):
            (self.library_path / f"file-{index}.txt").write_text(f"内容 {index}", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        token_before = self.database.fetchone("SELECT scan_token FROM files LIMIT 1")["scan_token"]

        statements, patcher = self._record_statements()
        with patcher:
            result = scan_library(self.database, self.library, lambda *_: None, lambda: False)
        self.assertEqual(result["unchanged"], 3)
        # 预载按本批 path 一次查询（files.path 全局 UNIQUE，不限 library_id，重叠库命中已有行走 UPDATE），
        # 既不是每文件一条 SELECT 点查，也不是不带 path 限定的全库 SELECT
        preload_selects = [sql for sql in statements if sql.startswith("SELECT id, path, size, mtime_ns FROM files")]
        self.assertTrue(preload_selects)
        self.assertTrue(all("WHERE path IN" in sql for sql in preload_selects))
        # 未变文件只刷 scan_token，合并成一次 executemany，没有逐行 UPDATE
        token_updates = [
            sql for sql in statements
            if sql.startswith("UPDATE files SET library_id = ?, relative_path = ?, scan_token")
        ]
        self.assertEqual(token_updates, ["UPDATE files SET library_id = ?, relative_path = ?, scan_token = ? WHERE id = ? [x3]"])
        # similarity_groups 全量重算只在扫描结束时执行一次
        self.assertEqual(sum(1 for sql in statements if "UPDATE similarity_groups" in sql), 1)
        tokens = {row["scan_token"] for row in self.database.fetchall("SELECT scan_token FROM files")}
        self.assertEqual(len(tokens), 1)
        self.assertNotEqual(tokens.pop(), token_before)

    def test_upsert_files_finalize_controls_group_recompute(self) -> None:
        (self.library_path / "a.txt").write_text("内容", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        row = self.database.fetchone("SELECT * FROM files LIMIT 1")
        values = dict(row)
        values["scan_token"] = "new-token"

        statements, patcher = self._record_statements()
        with patcher:
            result = self.database.upsert_files([values], finalize=False)
            self.assertEqual(result, [(int(row["id"]), False)])
            self.assertFalse(any("similarity_groups" in sql for sql in statements))
            statements.clear()
            self.assertEqual(self.database.upsert_files([], finalize=True), [])
            self.assertTrue(any("UPDATE similarity_groups" in sql for sql in statements))
        self.assertEqual(
            self.database.fetchone("SELECT scan_token FROM files WHERE id = ?", (row["id"],))["scan_token"],
            "new-token",
        )

    def test_filter_sql_pushdown_matches_legacy_file_id_results(self) -> None:
        (self.library_path / "周报甲.txt").write_text("季度报告：甲图书馆的统计数据", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        other_path = Path(self.temp.name) / "other"
        other_path.mkdir()
        other_library = self.database.create_library("其他库", str(other_path))
        (other_path / "周报乙.txt").write_text("季度报告：乙图书馆的统计数据", encoding="utf-8")
        scan_library(self.database, other_library, lambda *_: None, lambda: False)
        for file_id in self.database.pending_file_ids():
            file = self.database.get_file(file_id)
            result, chunks = index_file(file, self.local_settings, LocalAIClient(self.local_settings))
            self.database.finish_file_index(file_id, result, chunks)

        search = SearchService(self.database, LocalAIClient(self.local_settings), NullVectors())
        unfiltered = search.search("季度报告")
        self.assertEqual(len(unfiltered["results"]), 2)
        id_other = int(self.database.fetchone("SELECT id FROM files WHERE name = ?", ("周报乙.txt",))["id"])
        legacy = search.search("季度报告", file_ids=[id_other])
        pushed = search.search("季度报告", filter_sql=("f.library_id = ?", [int(other_library["id"])]))
        self.assertEqual([item["id"] for item in legacy["results"]], [id_other])
        self.assertEqual([item["id"] for item in pushed["results"]], [id_other])

    def test_vector_filter_batches_large_id_sets(self) -> None:
        vectors = _RecordingVectors()
        search = SearchService(self.database, SemanticAI(self.local_settings), vectors)
        search.search("测试", file_ids=list(range(1, 4501)))
        self.assertEqual([len(chunk) for chunk in vectors.filters], [2000, 2000, 500])

        # filter_sql 路径同样把过滤后的 id 分批传给向量检索，不再静默放弃过滤
        (self.library_path / "v.txt").write_text("内容", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file_id = int(self.database.fetchone("SELECT id FROM files LIMIT 1")["id"])
        vectors_sql = _RecordingVectors()
        search_sql = SearchService(self.database, SemanticAI(self.local_settings), vectors_sql)
        search_sql.search("测试", filter_sql=("f.library_id = ?", [int(self.library["id"])]))
        self.assertEqual(vectors_sql.filters, [[file_id]])

    def test_watcher_shallow_probe_skips_unchanged_deep_walk(self) -> None:
        nested = self.library_path / "dir1"
        nested.mkdir()
        (nested / "file1.txt").write_text("一", encoding="utf-8")
        (self.library_path / "file2.txt").write_text("二", encoding="utf-8")
        watcher = LibraryWatcher(_WatcherDatabase(self.library_path), self.local_settings, None)
        watcher.mode = "hybrid"
        deep_calls = 0
        original_deep = watcher._deep_signature

        def counting_deep(path):
            nonlocal deep_calls
            deep_calls += 1
            return original_deep(path)

        watcher._deep_signature = counting_deep
        stamp = time.time_ns() + 5_000_000_000

        first = watcher._signatures()
        self.assertEqual(deep_calls, 1)
        second = watcher._signatures()
        self.assertEqual(deep_calls, 1)
        self.assertEqual(first, second)

        # 顶层文件直接修改：文件自身 mtime 进入浅层探测 → 深入递归
        (self.library_path / "file2.txt").write_text("二改", encoding="utf-8")
        stamp += 5_000_000_000
        os.utime(self.library_path / "file2.txt", ns=(stamp, stamp))
        third = watcher._signatures()
        self.assertEqual(deep_calls, 2)
        self.assertNotEqual(first, third)

        # 顶层目录内新增文件：目录 mtime 刷新 → 深入递归
        (nested / "file3.txt").write_text("三", encoding="utf-8")
        stamp += 5_000_000_000
        os.utime(nested, ns=(stamp, stamp))
        watcher._signatures()
        self.assertEqual(deep_calls, 3)

        # 取舍：更深层已有文件的纯内容修改不刷新顶层 mtime，浅层探测会跳过，
        # hybrid 模式靠 inotify 实时事件覆盖这类变化（见 watcher._signatures 注释）
        (nested / "file1.txt").write_text("一改", encoding="utf-8")
        stamp += 5_000_000_000
        os.utime(nested / "file1.txt", ns=(stamp, stamp))
        watcher._signatures()
        self.assertEqual(deep_calls, 3)

        # polling 模式没有 inotify 兜底，每次都必须全量递归
        watcher.mode = "polling"
        watcher._signatures()
        self.assertEqual(deep_calls, 4)

    def test_watcher_signature_interval_follows_mode(self) -> None:
        watcher = LibraryWatcher(_WatcherDatabase(self.library_path), self.local_settings, None)
        watcher.mode = "hybrid"
        self.assertEqual(watcher._signature_interval(), self.local_settings.watch_signature_seconds)
        watcher.mode = "polling"
        self.assertEqual(watcher._signature_interval(), self.local_settings.watch_poll_seconds)
        self.assertEqual(settings.watch_signature_seconds, 1800)


if __name__ == "__main__":
    unittest.main()


class PreviewSecurityTests(unittest.TestCase):
    """安全修复与预览管线的回归测试（附件/上传/内容服务/分享区域）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)

    def tearDown(self) -> None:
        # 匿名 owner 兜底只在 users 表为空时生效，每个用例结束后清掉测试账号避免相互影响
        state.database.execute("DELETE FROM users")

    def _temp_db(self, prefix: str):
        temp = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        library_path = root / "library"
        library_path.mkdir()
        database = Database(root / "index.db")
        database.initialize()
        library = database.create_library("测试库", str(library_path))
        return root, library_path, database, library

    def _shared_asset(self, name: str) -> tuple[int, int]:
        upload = self.client.post(
            "/api/uploads",
            content=f"{name} media".encode(),
            headers={"X-Filename": f"preview-{name}.txt"},
        )
        self.assertEqual(upload.status_code, 201)
        file_id = upload.json()["file"]["id"]
        project = self.client.post("/api/projects", json={
            "name": f"{name}-{time.time_ns()}",
            "color": "#7c8cff",
        })
        self.assertEqual(project.status_code, 201)
        project_id = project.json()["id"]
        asset = self.client.post(f"/api/projects/{project_id}/assets", json={
            "file_id": file_id,
            "title": f"{name}素材",
        })
        self.assertEqual(asset.status_code, 201)
        return project_id, asset.json()["id"]

    def _session_header(self, username: str) -> tuple[dict[str, str], int]:
        from app.security import token_digest

        user = state.database.create_user(username, username, "scrypt$16384$8$1$invalid$invalid", "member", [])
        raw_token = f"token-{username}-{time.time_ns()}"
        expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
        state.database.create_session(int(user["id"]), token_digest(raw_token), expires_at)
        return {"Authorization": f"Bearer {raw_token}"}, int(user["id"])

    def test_active_content_forced_to_attachment(self) -> None:
        upload = self.client.post(
            "/api/uploads",
            content=b"<html><script>alert(1)</script></html>",
            headers={"X-Filename": f"xss-{time.time_ns()}.html"},
        )
        self.assertEqual(upload.status_code, 201)
        file_id = upload.json()["file"]["id"]
        content = self.client.get(f"/api/files/{file_id}/content")
        self.assertEqual(content.status_code, 200)
        self.assertTrue(content.headers["content-disposition"].startswith("attachment"))
        ticket = self.client.post(f"/api/files/{file_id}/ticket").json()["url"]
        media = self.client.get(ticket)
        self.assertEqual(media.status_code, 200)
        self.assertTrue(media.headers["content-disposition"].startswith("attachment"))

        image = self.client.post(
            "/api/uploads",
            content=b"\x89PNG\r\n\x1a\nfake-png",
            headers={"X-Filename": f"safe-{time.time_ns()}.png"},
        )
        self.assertEqual(image.status_code, 201)
        safe = self.client.get(f"/api/files/{image.json()['file']['id']}/content")
        self.assertEqual(safe.status_code, 200)
        self.assertTrue(safe.headers["content-disposition"].startswith("inline"))

    def test_comment_attachment_per_comment_limit(self) -> None:
        _, asset_id = self._shared_asset("attach-limit")
        comment = self.client.post(f"/api/assets/{asset_id}/comments", json={"body": "附件上限"})
        self.assertEqual(comment.status_code, 201)
        comment_id = comment.json()["id"]
        for index in range(settings.comment_attachment_max_per_comment):
            response = self.client.post(
                f"/api/comments/{comment_id}/attachments",
                content=b"\x89PNG\r\n\x1a\nfake-png",
                headers={"X-Filename": f"proof-{index}.png"},
            )
            self.assertEqual(response.status_code, 201)
        overflow = self.client.post(
            f"/api/comments/{comment_id}/attachments",
            content=b"\x89PNG\r\n\x1a\nfake-png",
            headers={"X-Filename": "proof-overflow.png"},
        )
        self.assertEqual(overflow.status_code, 409)

    def test_comment_attachment_disk_guard(self) -> None:
        _, asset_id = self._shared_asset("attach-disk")
        comment = self.client.post(f"/api/assets/{asset_id}/comments", json={"body": "磁盘余量"})
        comment_id = comment.json()["id"]
        usage = type("Usage", (), {"total": 0, "used": 0, "free": 0})
        with patch("app.main.shutil.disk_usage", lambda *_: usage()):
            response = self.client.post(
                f"/api/comments/{comment_id}/attachments",
                content=b"\x89PNG\r\n\x1a\nfake-png",
                headers={"X-Filename": "disk-full.png"},
            )
        self.assertEqual(response.status_code, 507)

    def test_comment_attachment_ownership_boundary(self) -> None:
        project_id, asset_id = self._shared_asset("attach-owner")
        author_headers, author_id = self._session_header(f"author-{time.time_ns()}")
        peer_headers, peer_id = self._session_header(f"peer-{time.time_ns()}")
        state.workspaces.set_member(project_id, author_id, "reviewer")
        state.workspaces.set_member(project_id, peer_id, "reviewer")
        comment = self.client.post(
            f"/api/assets/{asset_id}/comments",
            json={"body": "归属边界"},
            headers=author_headers,
        )
        self.assertEqual(comment.status_code, 201)
        comment_id = comment.json()["id"]
        forbidden = self.client.post(
            f"/api/comments/{comment_id}/attachments",
            content=b"\x89PNG\r\n\x1a\nfake-png",
            headers={**peer_headers, "X-Filename": "peer.png"},
        )
        self.assertEqual(forbidden.status_code, 403)
        allowed = self.client.post(
            f"/api/comments/{comment_id}/attachments",
            content=b"\x89PNG\r\n\x1a\nfake-png",
            headers={**author_headers, "X-Filename": "author.png"},
        )
        self.assertEqual(allowed.status_code, 201)
        state.workspaces.set_member(project_id, peer_id, "manager")
        manager = self.client.post(
            f"/api/comments/{comment_id}/attachments",
            content=b"\x89PNG\r\n\x1a\nfake-png",
            headers={**peer_headers, "X-Filename": "manager.png"},
        )
        self.assertEqual(manager.status_code, 201)

    def test_share_ticket_rejected_after_share_disabled(self) -> None:
        project_id, asset_id = self._shared_asset("ticket-revoke")
        share = self.client.post(f"/api/projects/{project_id}/shares", json={
            "asset_id": asset_id,
            "name": "可撤销分享",
            "can_download": True,
        })
        self.assertEqual(share.status_code, 201)
        payload = self.client.post(f"/api/public/shares/{share.json()['token']}", json={})
        self.assertEqual(payload.status_code, 200)
        media_url = payload.json()["assets"][0]["versions"][0]["media_url"]
        self.assertTrue(media_url)
        self.assertEqual(self.client.get(media_url).status_code, 200)
        disabled = self.client.put(f"/api/shares/{share.json()['id']}/enabled", params={"enabled": "false"})
        self.assertEqual(disabled.status_code, 200)
        revoked = self.client.get(media_url)
        self.assertEqual(revoked.status_code, 403)

    def test_bootstrap_is_rate_limited(self) -> None:
        from app.main import BOOTSTRAP_ATTEMPTS

        BOOTSTRAP_ATTEMPTS.clear()
        try:
            for index in range(5):
                response = self.client.post("/api/auth/bootstrap", json={
                    "username": f"bootstrap-{index}",
                    "display_name": "初始化",
                    "password": "bootstrap-password",
                })
                self.assertNotEqual(response.status_code, 429)
            blocked = self.client.post("/api/auth/bootstrap", json={
                "username": "bootstrap-blocked",
                "display_name": "初始化",
                "password": "bootstrap-password",
            })
            self.assertEqual(blocked.status_code, 429)
            self.assertIn("retry-after", blocked.headers)
        finally:
            BOOTSTRAP_ATTEMPTS.clear()

    def test_delete_library_vector_failure_is_logged(self) -> None:
        folder = SCAN_ROOT / f"vector-fail-{time.time_ns()}"
        folder.mkdir()
        created = self.client.post("/api/libraries", json={"name": folder.name, "path": str(folder)})
        self.assertEqual(created.status_code, 201)
        library_id = created.json()["id"]
        with patch.object(state.vectors, "delete_library", side_effect=RuntimeError("qdrant down")):
            with self.assertLogs("app.main", level="WARNING") as captured:
                response = self.client.delete(f"/api/libraries/{library_id}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("向量数据失败" in line for line in captured.output))

    def test_recycle_vector_cleanup_failure_is_logged(self) -> None:
        _, library_path, database, library = self._temp_db("nas-ai-recycle-log-")
        (library_path / "first.txt").write_text("identical-content", encoding="utf-8")
        (library_path / "second.txt").write_text("identical-content", encoding="utf-8")
        scan_library(database, library, lambda *_: None, lambda: False)
        database.execute("UPDATE files SET content_hash = 'hash-x'")
        file_ids = database.pending_file_ids()
        self.assertEqual(len(file_ids), 2)

        class FailingVectors:
            @staticmethod
            def delete_files(file_ids):
                raise RuntimeError("qdrant down")

        recycle = RecycleBin(database, settings, FailingVectors())
        with self.assertLogs("app.services.recycle", level="WARNING") as captured:
            result = recycle.move_duplicates([file_ids[0]], "tester")
        self.assertEqual(result["moved"], 1)
        self.assertTrue(any("向量清理失败" in line and "1 个文件" in line for line in captured.output))

    def test_scan_vector_cleanup_failure_is_logged(self) -> None:
        import asyncio

        from app.services.tasks import TaskManager

        _, _, database, library = self._temp_db("nas-ai-scan-log-")
        task_id = database.create_task("scan_library", {"library_id": int(library["id"])})

        class FailingVectors:
            @staticmethod
            def delete_files(file_ids):
                raise RuntimeError("qdrant down")

        manager = TaskManager(database, settings, LocalAIClient(settings), FailingVectors())
        with self.assertLogs("app.services.tasks", level="WARNING") as captured:
            asyncio.run(manager._scan(task_id, int(library["id"])))
        self.assertTrue(any("向量清理失败" in line for line in captured.output))

    def test_psd_pixel_limit_rejects_bomb(self) -> None:
        _, library_path, _, _ = self._temp_db("nas-ai-psd-pixels-")
        psd_path = library_path / "bomb.psd"
        psd_path.write_bytes(b"fake-psd")

        class FakePsd:
            width = 20000
            height = 20000

            @staticmethod
            def composite():
                raise AssertionError("像素超限时不应触发合成")

        with patch("psd_tools.PSDImage.open", return_value=FakePsd()):
            with self.assertRaises(ValueError) as context:
                create_thumbnail(psd_path, library_path / "bomb.jpg", "image", 320)
        self.assertIn("像素", str(context.exception))

    def test_font_size_limit(self) -> None:
        _, library_path, _, _ = self._temp_db("nas-ai-font-limit-")
        font_path = library_path / "big.ttf"
        font_path.write_bytes(b"fake-font-bytes")
        with patch("app.services.extractors.settings", replace(settings, max_font_bytes=1)):
            with self.assertRaises(ValueError) as context:
                create_thumbnail(font_path, library_path / "big.jpg", "image", 320)
        self.assertIn("字体文件超过", str(context.exception))

    def test_eps_failure_cleans_tmp_and_truncates_error(self) -> None:
        _, library_path, _, _ = self._temp_db("nas-ai-eps-clean-")
        eps_path = library_path / "figure.eps"
        eps_path.write_bytes(b"%!PS-Adobe-3.0 fake")
        destination = library_path / "figure.tmp.jpg"
        destination.write_bytes(b"partial-output")
        failure = subprocess.CalledProcessError(3, ["gs"], stderr=b"noise\n" + b"x" * 1000)
        with (
            patch("app.services.extractors.shutil.which", return_value="/usr/bin/gs"),
            patch("app.services.extractors.subprocess.run", side_effect=failure),
        ):
            from app.services.extractors import _render_eps_preview

            with self.assertRaises(ValueError) as context:
                _render_eps_preview(eps_path, destination, 320)
        message = str(context.exception)
        self.assertIn("退出码 3", message)
        self.assertLessEqual(len(message), 300)
        self.assertFalse(destination.exists())

    def test_psd_index_prefills_thumbnail_cache(self) -> None:
        import hashlib

        _, library_path, database, library = self._temp_db("nas-ai-psd-cache-")
        psd_path = library_path / "art.psd"
        psd_path.write_bytes(b"fake-psd")
        scan_library(database, library, lambda *_: None, lambda: False)
        file = database.get_file(database.pending_file_ids()[0])

        class FakePsd:
            width = 1024
            height = 512

            @staticmethod
            def composite():
                return Image.new("RGB", (1024, 512), "#7a3bd0")

        key = hashlib.sha256(
            f"{file['path']}:{file['mtime_ns']}:{settings.thumbnail_size}".encode()
        ).hexdigest()
        cache_file = settings.cache_dir / "thumbnails" / key[:2] / f"{key}.jpg"
        cache_file.unlink(missing_ok=True)
        self.addCleanup(lambda: cache_file.unlink(missing_ok=True))
        local_settings = replace(settings, cache_dir=Path(tempfile.mkdtemp(prefix="nas-ai-psd-cache-dir-")))
        with patch("psd_tools.PSDImage.open", return_value=FakePsd()):
            result, _ = index_file(file, local_settings, LocalAIClient(local_settings))
        self.assertEqual(result["metadata"]["decoder"], "psd-tools")
        self.assertTrue(cache_file.is_file())
        with Image.open(cache_file) as cached:
            self.assertLessEqual(max(cached.size), settings.thumbnail_size)


class BackendRegressionPerfTests(unittest.TestCase):
    """本轮回归与性能修复的配套测试（见各项修复说明）。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="nas-ai-backend-perf-")
        root = Path(self.temp.name)
        self.library_path = root / "library"
        self.library_path.mkdir()
        self.database = Database(root / "index.db")
        self.database.initialize()
        self.library = self.database.create_library("回归资料", str(self.library_path))
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

    def _file_values(self, library_id: int, path: str, size: int = 100, mtime_ns: int = 1) -> dict:
        return {
            "library_id": library_id,
            "path": path,
            "relative_path": Path(path).name,
            "name": Path(path).name,
            "extension": Path(path).suffix,
            "kind": "image",
            "mime_type": "image/jpeg",
            "size": size,
            "mtime_ns": mtime_ns,
            "inode": 1,
            "scan_token": "token-1",
        }

    def test_upsert_files_overlapping_libraries_hit_update_branch(self) -> None:
        # files.path 全局 UNIQUE：两个库根目录重叠（/photos 与 /photos/2024）时，
        # 同 path 出现在另一库的批次里应命中已有行走 UPDATE，而不是 INSERT 冲突整批回滚
        nested_path = self.library_path / "2024"
        nested_path.mkdir()
        nested_library = self.database.create_library("嵌套库", str(nested_path))
        shared = str(nested_path / "photo.jpg")

        first = self.database.upsert_files([self._file_values(int(self.library["id"]), shared)])
        file_id = first[0][0]
        second = self.database.upsert_files([self._file_values(int(nested_library["id"]), shared, size=200, mtime_ns=2)])
        self.assertEqual(second, [(file_id, True)])
        row = self.database.get_file(file_id)
        self.assertEqual(int(row["library_id"]), int(nested_library["id"]))
        self.assertEqual(int(self.database.fetchone("SELECT COUNT(*) AS c FROM files")["c"]), 1)
        # 内容未变再走一次：命中 unchanged 分支（仅刷 token），同样不报错
        third = self.database.upsert_files([self._file_values(int(nested_library["id"]), shared, size=200, mtime_ns=2)])
        self.assertEqual(third, [(file_id, False)])
        self.assertEqual(int(self.database.fetchone("SELECT COUNT(*) AS c FROM files")["c"]), 1)

    def test_index_stage_summary_single_scan_matches_group_by(self) -> None:
        for index in range(3):
            (self.library_path / f"f{index}.txt").write_text(f"内容 {index}", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        ids = self.database.pending_file_ids()
        self.database.execute(
            """UPDATE files SET status = 'ready', vision_status = 'ready',
               transcription_status = 'not_applicable', embedding_status = 'ready',
               extracted_text = '正文' WHERE id = ?""",
            (ids[0],),
        )
        self.database.execute(
            "UPDATE files SET status = 'error', vision_status = 'error', terminal_error = 1 WHERE id = ?",
            (ids[1],),
        )
        calls: list[str] = []
        original_fetchone = self.database.fetchone

        def recording_fetchone(query, params=()):
            calls.append(query)
            return original_fetchone(query, params)

        with patch.object(self.database, "fetchone", recording_fetchone):
            summary = self.database.index_stage_summary()
        # 三个状态列 + 三个修复计数合并为一次条件聚合单扫
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["terminal_failures"], 1)
        self.assertEqual(summary["repairable"], 0)
        self.assertEqual(summary["retry_waiting"], 0)
        # 数值口径与旧 GROUP BY 实现完全一致（含“不出现的键即 0”）
        for column, name in (
            ("vision_status", "vision"),
            ("transcription_status", "transcription"),
            ("embedding_status", "embedding"),
        ):
            rows = self.database.fetchall(f"SELECT {column} AS s, COUNT(*) AS c FROM files GROUP BY {column}")
            self.assertEqual(summary[name], {str(row["s"]): int(row["c"]) for row in rows})

    def test_embedding_json_not_written_and_dashboard_coverage_from_stages(self) -> None:
        (self.library_path / "doc.txt").write_text("一些正文内容", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file_id = self.database.pending_file_ids()[0]
        self.database.finish_file_index(
            file_id,
            {"caption": "", "text": "一些正文内容", "quick_hash": "q", "metadata": {}},
            [{"content": "一些正文内容", "embedding": [0.1, 0.2]}],
        )
        chunk = self.database.fetchone("SELECT embedding_json FROM content_chunks WHERE file_id = ?", (file_id,))
        # embedding_json 已弃用：索引完成后为 NULL，向量只在 Qdrant
        self.assertIsNone(chunk["embedding_json"])
        files = self.database.dashboard()["files"]
        # 覆盖率统计切换到 files 表口径后仍然正确
        self.assertEqual(files["semantic_ready"], 1)
        self.assertEqual(files["content_ready"], 1)

    def test_semantic_hits_are_batch_fetched_without_get_file(self) -> None:
        video = self.library_path / "meeting.mp4"
        video.write_bytes(b"not-a-real-video")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file_id = self.database.pending_file_ids()[0]
        search = SearchService(self.database, SemanticAI(self.local_settings), SemanticVectors(file_id))
        with patch.object(Database, "get_file", side_effect=AssertionError("语义候选不应逐条 get_file")):
            response = search.search("关键词")
        self.assertEqual(response["results"][0]["match_time"], 12.5)

    def test_similar_candidates_are_batch_fetched_without_get_file(self) -> None:
        for name in ("source.jpg", "near.jpg"):
            Image.new("RGB", (80, 80), "#777777").save(self.library_path / name)
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        source = self.database.fetchone("SELECT * FROM files WHERE name = 'source.jpg'")
        near = self.database.fetchone("SELECT * FROM files WHERE name = 'near.jpg'")
        vectors = SimilarVectors([
            {"score": 0.91, "payload": {"file_id": near["id"], "content": "相似场景"}},
        ])
        search = SearchService(self.database, LocalAIClient(self.local_settings), vectors)
        with patch.object(Database, "get_file", side_effect=AssertionError("相似候选不应逐条 get_file")):
            response = search.similar(int(source["id"]))
        self.assertEqual([item["name"] for item in response["results"]], ["near.jpg"])

    def test_lexical_queries_select_narrow_columns_but_profile_uses_full_text(self) -> None:
        document = self.library_path / "笔记.txt"
        document.write_text("猫咪坐在花盆旁边", encoding="utf-8")
        scan_library(self.database, self.library, lambda *_: None, lambda: False)
        file_id = self.database.pending_file_ids()[0]
        file = self.database.get_file(file_id)
        result, chunks = index_file(file, self.local_settings, LocalAIClient(self.local_settings))
        self.database.finish_file_index(file_id, result, chunks)

        statements: list[str] = []
        original_fetchall = self.database.fetchall

        def recording_fetchall(query, params=()):
            statements.append(query)
            return original_fetchall(query, params)

        search = SearchService(self.database, LocalAIClient(self.local_settings), NullVectors())
        with patch.object(self.database, "fetchall", recording_fetchall):
            response = search.search("猫咪 花盆", semantic=False)
        candidate_queries = [sql for sql in statements if "FROM files_fts" in sql or " LIKE " in sql]
        self.assertTrue(candidate_queries)
        # 候选查询不再 SELECT f.* 物化整篇 extracted_text
        self.assertFalse(any("f.*" in sql for sql in candidate_queries))
        top = response["results"][0]
        self.assertEqual(top["name"], "笔记.txt")
        # profile 仍基于回填的完整文本计算，匹配词与 snippet 行为不变
        self.assertEqual(top["matched_terms"], ["猫咪", "花盆"])
        self.assertEqual(top["coverage"], 1.0)
        self.assertIn("猫咪", top["snippet"])

    def test_vector_store_reuses_client_and_writes_wait_false(self) -> None:
        from app.services.vectors import VectorStore

        store = VectorStore(self.local_settings)
        self.assertIs(store._http(), store._http())
        urls: list[str] = []

        class FakeResponse:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def post(self, url, **_kwargs):
                urls.append(url)
                return FakeResponse()

            def put(self, url, **_kwargs):
                urls.append(url)
                return FakeResponse()

        store._dimension = 3  # 跳过 _ensure_collection
        store._client = FakeClient()
        store.replace_file(
            {"id": 7, "library_id": 1, "kind": "document", "relative_path": "a.txt"},
            [{"content": "正文", "embedding": [0.1, 0.2, 0.3]}],
        )
        self.assertEqual(len(urls), 2)
        # 批量索引路径 delete+upsert 均为 wait=false，不再双 wait=true 同步刷盘
        self.assertTrue(all("wait=false" in url for url in urls))
        store.delete_files([7])
        self.assertIn("wait=false", urls[-1])

    def test_local_ai_reuses_http_client(self) -> None:
        client = LocalAIClient(self.local_settings)
        self.assertIsNone(client._client)
        self.assertIs(client._http(), client._http())

    def test_album_refresh_is_throttled_until_queue_drains(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock

        from app.services.tasks import TaskManager

        manager = TaskManager(
            self.database, self.local_settings, LocalAIClient(self.local_settings), NullVectors()
        )

        async def scenario() -> None:
            with patch.object(
                manager,
                "submit_unique",
                new=AsyncMock(side_effect=[(1, False), (2, False), (3, False), (4, False)]),
            ) as submit:
                # 首次：允许排队并记录时间
                await manager._queue_album_refresh()
                self.assertEqual(submit.await_count, 2)
                # 距上次不足 1 小时且任务队列未排空：跳过，不重复全库重算
                manager.queue.put_nowait((0, 999))
                await manager._queue_album_refresh()
                self.assertEqual(submit.await_count, 2)
                # 队列排空后：允许立即补刷一次，相册最终仍会更新
                manager.queue.get_nowait()
                await manager._queue_album_refresh()
                self.assertEqual(submit.await_count, 4)

        asyncio.run(scenario())


class OpsAgentTests(unittest.TestCase):
    def test_validate_service_whitelist(self) -> None:
        self.assertEqual(ops_agent.validate_service("app"), "nas-ai-space-app-1")
        self.assertEqual(ops_agent.validate_service("speech"), "nas-ai-space-speech-1")
        for bad in ("nginx", "", "ops", "app1", "../app"):
            with self.assertRaises(ValueError):
                ops_agent.validate_service(bad)

    def test_validate_memory_mb_bounds_and_type(self) -> None:
        self.assertEqual(ops_agent.validate_memory_mb(256), 256)
        self.assertEqual(ops_agent.validate_memory_mb(8192), 8192)
        for bad in (0, 255, 8193, -1):
            with self.assertRaises(ValueError):
                ops_agent.validate_memory_mb(bad)
        for bad in (True, 512.0, "512", None):
            with self.assertRaises(ValueError):
                ops_agent.validate_memory_mb(bad)

    def test_set_memory_rejects_before_docker_call(self) -> None:
        calls = []
        with patch.object(ops_agent, "docker_request", side_effect=lambda *args: calls.append(args)):
            with self.assertRaises(ValueError):
                ops_agent.set_memory("vision", 100)
            with self.assertRaises(ValueError):
                ops_agent.set_memory("nginx", 512)
        self.assertEqual(calls, [])

    def test_overrides_roundtrip_and_reapply(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nas-ai-ops-") as directory:
            path = Path(directory) / "overrides.json"
            calls = []

            def fake_docker(method, url, body=None):
                calls.append((method, url, body))
                return {}

            with patch.object(ops_agent, "OVERRIDES_PATH", path), \
                    patch.object(ops_agent, "docker_request", side_effect=fake_docker):
                result = ops_agent.set_memory("vision", 3072)
                self.assertEqual(result["mem_limit_mb"], 3072)
                self.assertEqual(result["mem_limit_bytes"], 3072 * 1024 * 1024)
                self.assertEqual(calls[-1], (
                    "POST",
                    "/containers/nas-ai-space-vision-1/update",
                    {"Memory": 3072 * 1024 * 1024, "MemorySwap": -1},
                ))
                ops_agent.set_memory("speech", 512)
                self.assertEqual(ops_agent.load_overrides(), {"vision": 3072, "speech": 512})
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["vision"], 3072)
                # 启动重放：compose 重建容器后保持管理员设定
                calls.clear()
                ops_agent.apply_overrides()
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(call[0] == "POST" and call[1].endswith("/update") for call in calls))
            # 覆盖文件损坏时按空处理，不阻塞启动
            path.write_text("{bad json", encoding="utf-8")
            with patch.object(ops_agent, "OVERRIDES_PATH", path):
                self.assertEqual(ops_agent.load_overrides(), {})

    def test_list_containers_parses_engine_payload(self) -> None:
        payloads = {
            ("GET", "/containers/nas-ai-space-app-1/json"): {
                "Name": "/nas-ai-space-app-1",
                "RestartCount": 2,
                "State": {"Status": "running", "Running": True, "OOMKilled": False},
                "HostConfig": {"Memory": 1610612736},
            },
            ("GET", "/containers/nas-ai-space-app-1/stats?stream=false"): {
                "memory_stats": {"usage": 805306368},
            },
        }

        def fake_docker(method, url, body=None):
            if (method, url) in payloads:
                return payloads[(method, url)]
            return {
                "Name": "",
                "RestartCount": 0,
                "State": {"Status": "exited", "Running": False, "OOMKilled": True},
                "HostConfig": {"Memory": 0},
            }

        with patch.object(ops_agent, "docker_request", side_effect=fake_docker):
            items = ops_agent.list_containers()
        self.assertEqual(len(items), 5)
        app_item = items[0]
        self.assertEqual(app_item["service"], "app")
        self.assertEqual(app_item["name"], "nas-ai-space-app-1")
        self.assertEqual(app_item["status"], "running")
        self.assertEqual(app_item["mem_usage_bytes"], 805306368)
        self.assertEqual(app_item["mem_limit_bytes"], 1610612736)
        self.assertEqual(app_item["restart_count"], 2)
        self.assertFalse(app_item["oom_killed"])
        qdrant_item = next(item for item in items if item["service"] == "qdrant")
        self.assertEqual(qdrant_item["status"], "exited")
        self.assertEqual(qdrant_item["mem_usage_bytes"], 0)
        self.assertTrue(qdrant_item["oom_killed"])


class _FakeOpsResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class OpsProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)

    def tearDown(self) -> None:
        # 匿名 owner 兜底只在 users 表为空时生效，每个用例结束后清掉测试账号避免相互影响
        state.database.execute("DELETE FROM users")

    def test_non_admin_forbidden_and_anonymous_unauthorized(self) -> None:
        username = f"ops-member-{time.time_ns()}"
        created = self.client.post("/api/users", json={
            "username": username,
            "display_name": "运维成员",
            "password": "strong-password",
            "role": "member",
            "library_ids": [],
        })
        self.assertEqual(created.status_code, 201)
        login = self.client.post("/api/auth/login", json={"username": username, "password": "strong-password"})
        self.assertEqual(login.status_code, 200)
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        self.assertEqual(self.client.get("/api/ops/containers", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/api/ops/containers/app/restart", headers=headers).status_code, 403)
        self.assertEqual(
            self.client.post("/api/ops/containers/app/memory", json={"mb": 512}, headers=headers).status_code,
            403,
        )
        secured = replace(settings, api_token="ops-proxy-token")
        with patch("app.main.settings", secured):
            self.assertEqual(self.client.get("/api/ops/containers").status_code, 401)

    def test_service_whitelist_and_memory_range_rejected(self) -> None:
        self.assertEqual(self.client.post("/api/ops/containers/nginx/memory", json={"mb": 512}).status_code, 404)
        self.assertEqual(self.client.post("/api/ops/containers/nginx/restart").status_code, 404)
        self.assertEqual(self.client.post("/api/ops/containers/app/memory", json={"mb": 100}).status_code, 422)
        self.assertEqual(self.client.post("/api/ops/containers/app/memory", json={"mb": 99999}).status_code, 422)

    def test_ops_unreachable_returns_503(self) -> None:
        import httpx

        broken = Mock()
        broken.request.side_effect = httpx.ConnectError("connection refused")
        with patch("app.main._ops_http", return_value=broken):
            response = self.client.get("/api/ops/containers")
            restart = self.client.post("/api/ops/containers/vision/restart")
        self.assertEqual(response.status_code, 503)
        self.assertIn("资源代理不可用", response.json()["detail"])
        self.assertEqual(restart.status_code, 503)

    def test_proxy_success_payloads(self) -> None:
        containers = {"containers": [{
            "name": "nas-ai-space-app-1",
            "service": "app",
            "status": "running",
            "mem_usage_bytes": 805306368,
            "mem_limit_bytes": 1610612736,
            "restart_count": 0,
            "oom_killed": False,
        }]}
        client = Mock()
        client.request.return_value = _FakeOpsResponse(200, containers)
        with patch("app.main._ops_http", return_value=client):
            response = self.client.get("/api/ops/containers")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["containers"][0]["service"], "app")

        client.request.return_value = _FakeOpsResponse(200, {
            "service": "vision",
            "mem_limit_mb": 3072,
            "mem_limit_bytes": 3072 * 1024 * 1024,
        })
        with patch("app.main._ops_http", return_value=client):
            response = self.client.post("/api/ops/containers/vision/memory", json={"mb": 3072})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mem_limit_mb"], 3072)
        client.request.assert_called_with(
            "POST", f"{settings.ops_url}/containers/vision/memory", json={"mb": 3072},
        )

        client.request.return_value = _FakeOpsResponse(200, {"service": "app", "restarting": True})
        with patch("app.main._ops_http", return_value=client):
            response = self.client.post("/api/ops/containers/app/restart")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["restarting"])
        self.assertIn("短暂断开", response.json()["notice"])

    def test_ops_error_detail_is_forwarded(self) -> None:
        client = Mock()
        client.request.return_value = _FakeOpsResponse(400, {"detail": "内存上限需在 256-8192 MB 之间"})
        with patch("app.main._ops_http", return_value=client):
            response = self.client.post("/api/ops/containers/vision/memory", json={"mb": 512})
        self.assertEqual(response.status_code, 502)
        self.assertIn("资源代理异常", response.json()["detail"])
        self.assertIn("256-8192", response.json()["detail"])


class AccountSecurityReviewTests(unittest.TestCase):
    """账号体系设计审查修复的配套测试（匿名兜底/会话节流/登录限流/任务去重/系统信息脱敏）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)

    def tearDown(self) -> None:
        state.database.execute("DELETE FROM users")
        LOGIN_FAILURES.clear()

    def _temp_database(self, prefix: str) -> Database:
        temp = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(temp.cleanup)
        database = Database(Path(temp.name) / "index.db")
        database.initialize()
        return database

    def test_anonymous_owner_fallback_only_before_bootstrap(self) -> None:
        database = self._temp_database("nas-ai-anon-")
        with patch.object(state, "database", database):
            # 纯首次启动窗口（users 表为空）：匿名请求仍按本地 owner 放行
            self.assertEqual(self.client.get("/api/system").status_code, 200)
        database.create_user("occupied-admin", "已初始化管理员", "scrypt$16384$8$1$invalid$invalid", "owner", [])
        with patch.object(state, "database", database):
            # bootstrap 完成后无有效 token/会话一律 401
            self.assertEqual(self.client.get("/api/system").status_code, 401)
            # 公开端点不受影响
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            self.assertEqual(self.client.get("/api/auth/bootstrap").status_code, 200)
            login = self.client.post(
                "/api/auth/login",
                json={"username": "occupied-admin", "password": "wrong-password"},
            )
            self.assertEqual(login.status_code, 401)

    def test_resolve_session_throttles_last_seen_writes(self) -> None:
        from app.security import token_digest

        database = self._temp_database("nas-ai-throttle-")
        user = database.create_user("节流用户", "节流用户", "hash", "member", [])
        token_hash = token_digest("throttle-token")
        expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
        database.create_session(int(user["id"]), token_hash, expires_at)
        writes = 0
        original_execute = database.execute

        def counting_execute(query, params=()):
            nonlocal writes
            if "last_seen_at" in query:
                writes += 1
            return original_execute(query, params)

        with patch.object(database, "execute", side_effect=counting_execute):
            self.assertIsNotNone(database.resolve_session(token_hash))
            self.assertIsNotNone(database.resolve_session(token_hash))
        self.assertEqual(writes, 1)
        # 距上次写入超过 600 秒后再次刷新
        session_id = next(iter(database._session_seen_at))
        database._session_seen_at[session_id] -= 601
        with patch.object(database, "execute", side_effect=counting_execute):
            self.assertIsNotNone(database.resolve_session(token_hash))
        self.assertEqual(writes, 2)

    def test_login_rate_limit_account_bucket_spans_ips(self) -> None:
        from app.main import _login_failure_key, _login_retry_after, _register_login_failure

        request = Mock()
        request.client.host = "10.9.9.9"
        # 限流 key 对账号名做大小写/空白规范化
        self.assertEqual(_login_failure_key(request, "  Victim  "), "10.9.9.9\0victim")

        LOGIN_FAILURES.clear()
        try:
            for index in range(30):
                _register_login_failure(f"10.0.0.{index}\0victim")
            # 第 31 个全新 IP 也命中同一账号的跨 IP 桶
            self.assertGreaterEqual(_login_retry_after("192.168.1.1\0victim"), 1)
            # 其它账号不受影响
            self.assertEqual(_login_retry_after("192.168.1.1\0other-account"), 0)
        finally:
            LOGIN_FAILURES.clear()

    def test_reindex_and_caption_submissions_dedupe_per_file(self) -> None:
        library = state.database.create_library(f"去重库-{time.time_ns()}", str(SCAN_ROOT))
        file_id = state.database.upsert_files([{
            "library_id": int(library["id"]),
            "path": str(SCAN_ROOT / f"dedup-{time.time_ns()}.txt"),
            "relative_path": "dedup.txt",
            "name": "dedup.txt",
            "extension": ".txt",
            "kind": "document",
            "mime_type": "text/plain",
            "size": 10,
            "mtime_ns": 1,
            "inode": 1,
            "scan_token": "token-1",
        }])[0][0]
        # 同一文件已有排队中的 index_files 任务时，重复提交（含 caption 重建）自动合并
        pending = state.database.create_task("index_files", {"file_ids": [file_id]}, 8)
        reindex = self.client.post(f"/api/files/{file_id}/reindex")
        self.assertEqual(reindex.status_code, 202)
        self.assertEqual(reindex.json(), {"task_id": pending, "existing": True})
        caption = self.client.put(f"/api/files/{file_id}/caption", json={"caption": "手动描述"})
        self.assertEqual(caption.status_code, 202)
        self.assertEqual(caption.json()["task_id"], pending)
        self.assertTrue(caption.json()["existing"])
        # 任务结束后再次提交会创建新任务
        state.database.mark_task_cancelled(pending)
        fresh = self.client.post(f"/api/files/{file_id}/reindex")
        self.assertEqual(fresh.status_code, 202)
        self.assertFalse(fresh.json()["existing"])
        state.database.mark_task_cancelled(fresh.json()["task_id"])

    def test_system_endpoint_masks_topology_for_members(self) -> None:
        from app.security import token_digest

        database = self._temp_database("nas-ai-mask-")
        expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
        member = database.create_user("普通成员", "普通成员", "hash", "member", [])
        member_token = "mask-member-token"
        database.create_session(int(member["id"]), token_digest(member_token), expires_at)
        admin = database.create_user("管理员", "管理员", "hash", "admin", [])
        admin_token = "mask-admin-token"
        database.create_session(int(admin["id"]), token_digest(admin_token), expires_at)
        with patch.object(state, "database", database):
            member_view = self.client.get(
                "/api/system",
                headers={"Authorization": f"Bearer {member_token}"},
            )
            self.assertEqual(member_view.status_code, 200)
            member_config = member_view.json()["configuration"]
            for key in ("scan_root", "scan_roots", "upload_root", "recycle_root", "model_endpoints"):
                self.assertNotIn(key, member_config)
            # 前端 member 首页算力卡片依赖的字段保留
            self.assertIn("indexing", member_config)
            self.assertIn("hardware", member_view.json())
            self.assertIn("metrics", member_view.json())
            self.assertIn("configured", member_view.json()["local_ai"])
            admin_view = self.client.get(
                "/api/system",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(admin_view.status_code, 200)
            admin_config = admin_view.json()["configuration"]
            self.assertIn("scan_root", admin_config)
            self.assertIn("upload_root", admin_config)
            self.assertIn("model_endpoints", admin_config)
