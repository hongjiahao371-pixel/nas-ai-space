"""Independent local media embeddings. Never rewrite the Qwen collection or source DB."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps

from app.config import Settings
from app.services.local_ai import LocalAIClient
from app.services.vectors import VectorStore

logger = logging.getLogger(__name__)
MODEL_REVISION = "embeddinggemma2-q8-v1"
DIMENSION = 768


def validate_embedding(value: Any) -> list[float]:
    if not isinstance(value, list) or len(value) != DIMENSION:
        raise ValueError("多模态向量必须为 768 维")
    vector = [float(x) for x in value]
    if not all(math.isfinite(x) for x in vector):
        raise ValueError("多模态向量包含非有限值")
    norm = math.sqrt(sum(x * x for x in vector))
    if norm < 1e-9:
        raise ValueError("多模态向量为空")
    return [x / norm for x in vector]


class MultimodalService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.multimodal_enabled and bool(settings.multimodal_base_url)
        self.vectors = VectorStore(replace(settings, qdrant_collection=settings.multimodal_collection))
        self._http = httpx.Client(timeout=httpx.Timeout(180, connect=5))
        self._gate = threading.Lock()
        self._cache: dict[str, tuple[float, list[float]]] = {}
        self.state_path = settings.data_dir / "multimodal-index.db"

    def embed(self, content: Any, timeout: float = 180) -> list[float]:
        endpoint = LocalAIClient(self.settings)._validate_endpoint(self.settings.multimodal_base_url)
        with self._gate:
            response = self._http.post(
                endpoint + "/v1/embeddings",
                json={"model": "embeddinggemma2", "input": [content], "encoding_format": "float"},
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()["data"]
            if len(data) != 1:
                raise ValueError("多模态服务返回数量不匹配")
            return validate_embedding(data[0]["embedding"])

    def query_embedding(self, query: str) -> list[float]:
        with self._gate:
            cached = self._cache.get(query)
            if cached and time.monotonic() - cached[0] < 600:
                return list(cached[1])
        vector = self.embed("task: search result | query: " + query, timeout=60)
        with self._gate:
            if len(self._cache) >= 128:
                self._cache.pop(next(iter(self._cache)))
            self._cache[query] = (time.monotonic(), vector)
        return vector

    def search(self, query: str, limit: int, kind: str = "", library_ids=None, file_ids=None) -> list[dict]:
        if not self.enabled or kind not in {"", "image", "video", "audio"}:
            return []
        if library_ids == [] or file_ids == []:
            return []
        vector = self.query_embedding(query)
        return [hit for hit in self.vectors.search(vector, limit, kind, library_ids, file_ids)
                if float(hit.get("score") or 0) >= self.settings.multimodal_min_score]

    def _state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.state_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS media_index (
            file_id INTEGER PRIMARY KEY, signature TEXT NOT NULL, status TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0, coverage TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL)""")
        db.execute("CREATE TABLE IF NOT EXISTS worker_status (id INTEGER PRIMARY KEY, heartbeat REAL, status TEXT)")
        return db

    def heartbeat(self, status="running"):
        with self._state() as db:
            db.execute("INSERT OR REPLACE INTO worker_status VALUES (1,?,?)", (time.time(), status))

    def status(self, library_ids=None) -> dict:
        result = {"enabled": self.enabled, "model": "EmbeddingGemma 2", "dimension": DIMENSION,
                  "indexed_files": 0, "indexed_points": 0, "total_media": 0, "errors": 0,
                  "video_coverage": "sampled_frames", "audio_coverage": "sampled_30s_segments"}
        if not self.state_path.exists() or library_ids == []:
            return result
        # Status counts only files visible to the caller, including library permissions.
        try:
            with sqlite3.connect(f"file:{self.state_path}?mode=ro", uri=True, timeout=5) as db:
                db.execute("ATTACH DATABASE ? AS source", (f"file:{self.settings.database_path}?mode=ro",))
                clause = ""
                params = []
                if library_ids is not None:
                    clause = " AND f.library_id IN (" + ",".join("?" for _ in library_ids) + ")"
                    params = list(library_ids)
                result["total_media"] = db.execute("""SELECT COUNT(*) FROM source.files f
                    JOIN source.libraries l ON l.id=f.library_id WHERE l.enabled=1
                    AND f.kind IN ('image','video','audio')""" + clause, params).fetchone()[0]
                row = db.execute("""SELECT COALESCE(SUM(m.status='ready'),0),
                    COALESCE(SUM(CASE WHEN m.status='ready' THEN m.points ELSE 0 END),0),
                    COALESCE(SUM(m.status='error'),0) FROM media_index m
                    JOIN source.files f ON f.id=m.file_id JOIN source.libraries l ON l.id=f.library_id
                    WHERE l.enabled=1""" + clause, params).fetchone()
                result.update(indexed_files=row[0], indexed_points=row[1], errors=row[2])
                worker = db.execute("SELECT heartbeat,status FROM worker_status WHERE id=1").fetchone()
                if worker:
                    result.update(worker_alive=time.time() - worker[0] < 900, worker_status=worker[1])
        except sqlite3.Error:
            logger.warning("多模态索引状态暂不可用", exc_info=True)
        return result

    def signature(self, file: dict) -> str:
        value = [MODEL_REVISION, file["mtime_ns"], file["size"], self.settings.multimodal_video_frames,
                 self.settings.multimodal_audio_segments]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

    def source_path(self, file: dict) -> Path:
        path = Path(file["path"]).resolve()
        roots = (*self.settings.scan_roots, self.settings.upload_root)
        if not any(path.is_relative_to(root.resolve()) for root in roots):
            raise ValueError("素材路径不在允许的资料目录内")
        stat = path.stat()
        if not path.is_file() or stat.st_mtime_ns != int(file["mtime_ns"]) or stat.st_size != int(file["size"]):
            raise ValueError("素材已变化，等待目录重新扫描")
        return path

    @staticmethod
    def image_input(path: Path) -> dict:
        from app.services.extractors import _convert_image
        with tempfile.TemporaryDirectory(prefix="gemma-image-") as tmp:
            try:
                image = Image.open(path)
            except (OSError, ValueError):
                converted = Path(tmp) / "source.jpg"
                _convert_image(path, converted)
                image = Image.open(converted)
            with image:
                prepared = ImageOps.exif_transpose(image).convert("RGB")
                prepared.thumbnail((960, 960))
                buffer = io.BytesIO()
                prepared.save(buffer, format="JPEG", quality=88)
        encoded = base64.b64encode(buffer.getvalue()).decode()
        return {"content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded}}]}

    def media_inputs(self, file: dict, path: Path, temporary: Path):
        kind = file["kind"]
        if kind == "image":
            yield self.image_input(path), "直接图片", None, None
            return
        from app.services.extractors import _extract_video_frames, _probe_media
        duration = float(file.get("duration") or _probe_media(path).get("duration") or 0)
        if kind == "video":
            frames = _extract_video_frames(path, temporary, duration, self.settings.multimodal_video_frames)
            for timestamp, frame in frames:
                yield self.image_input(frame), "直接视频画面", timestamp, timestamp
        elif kind == "audio" and duration > 0:
            count = min(self.settings.multimodal_audio_segments, max(1, math.ceil(duration / 30)))
            for index in range(count):
                start = index * 30 if duration <= count * 30 else (duration - 30) * index / max(1, count - 1)
                target = temporary / f"audio-{index}.wav"
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
                                "-i", str(path), "-t", "30", "-vn", "-ac", "1", "-ar", "16000",
                                "-c:a", "pcm_s16le", str(target)], check=True, capture_output=True, timeout=120)
                content = {"content": [{"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(target.read_bytes()).decode(), "format": "wav"}}]}
                yield content, "直接音频片段", start, min(duration, start + 30)

    def index_file(self, file: dict):
        path = self.source_path(file)
        signature = self.signature(file)
        points = []
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.settings.cache_dir, prefix="gemma-media-") as tmp:
            for index, (content, label, start, end) in enumerate(self.media_inputs(file, path, Path(tmp))):
                vector = self.embed(content)
                payload = {"file_id": int(file["id"]), "library_id": int(file["library_id"]),
                           "kind": file["kind"], "mtime_ns": int(file["mtime_ns"]), "size": int(file["size"]),
                           "signature": signature, "source_label": label, "start_time": start, "end_time": end,
                           "content": label + (f" · {start:.1f} 秒" if start is not None else ""),
                           "model_revision": MODEL_REVISION}
                points.append({"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"nas-ai:{file['id']}:{signature}:{index}")),
                               "vector": vector, "payload": payload})
        if not points:
            raise ValueError("未提取到可索引的素材内容")
        self.source_path(file)  # A file that changed during inference must not be published.
        self.vectors._ensure_collection(DIMENSION)
        url = f"{self.settings.qdrant_url}/collections/{self.settings.multimodal_collection}/points"
        response = self._http.put(url + "?wait=true", json={"points": points}, timeout=60)
        response.raise_for_status()
        # Publish first, then remove earlier generations. Qwen vectors are untouched.
        response = self._http.post(url + "/delete?wait=true", json={"filter": {
            "must": [{"key": "file_id", "match": {"value": int(file["id"])}}],
            "must_not": [{"key": "signature", "match": {"value": signature}}]}}, timeout=60)
        response.raise_for_status()
        coverage = "image" if file["kind"] == "image" else "sampled_frames" if file["kind"] == "video" else "sampled_audio"
        with self._state() as db:
            db.execute("INSERT OR REPLACE INTO media_index VALUES (?,?,?,?,?,?,?)",
                       (file["id"], signature, "ready", len(points), coverage, "", time.time()))
        return len(points)

    def run_batch(self) -> dict:
        self.heartbeat()
        with sqlite3.connect(f"file:{self.settings.database_path}?mode=ro", uri=True) as source:
            source.row_factory = sqlite3.Row
            files = [dict(row) for row in source.execute("""SELECT f.* FROM files f JOIN libraries l ON l.id=f.library_id
                WHERE l.enabled=1 AND f.kind IN ('image','video','audio') ORDER BY f.mtime_ns DESC, f.id DESC""")]
        with self._state() as db:
            indexed = {int(row["file_id"]): dict(row) for row in db.execute("SELECT * FROM media_index")}
        removed = set(indexed) - {int(file["id"]) for file in files}
        for file_id in removed:
            url = f"{self.settings.qdrant_url}/collections/{self.settings.multimodal_collection}/points/delete?wait=true"
            response = self._http.post(url, json={"filter": {"must": [
                {"key": "file_id", "match": {"value": file_id}}]}}, timeout=60)
            if response.status_code != 404:
                response.raise_for_status()
            with self._state() as db:
                db.execute("DELETE FROM media_index WHERE file_id=?", (file_id,))
        ready = errors = 0
        for file in files:
            previous = indexed.get(int(file["id"]))
            if previous and previous["signature"] == self.signature(file):
                if previous["status"] == "ready" or time.time() - previous["updated_at"] < 3600:
                    continue
            # Keep free memory for the existing NAS services; at most one inference at a time.
            from app.services.hardware import memory_runtime
            if memory_runtime().get("available_bytes", 0) < self.settings.multimodal_memory_floor_mb * 1024 * 1024:
                break
            try:
                points = self.index_file(file)
                ready += 1
                logger.info("多模态索引完成 file_id=%s kind=%s points=%s", file["id"], file["kind"], points)
            except Exception as exc:
                errors += 1
                logger.warning("多模态索引失败 file_id=%s: %s", file["id"], exc)
                with self._state() as db:
                    db.execute("INSERT OR REPLACE INTO media_index VALUES (?,?,?,?,?,?,?)",
                               (file["id"], self.signature(file), "error", 0, "", str(exc)[:500], time.time()))
            self.heartbeat()
            if ready + errors >= self.settings.multimodal_batch_size:
                break
        self.heartbeat("indexing" if ready + errors else "waiting")
        return {"ready": ready, "errors": errors, "total_media": len(files)}
