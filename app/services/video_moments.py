"""Read-only, file-scoped candidate moments and bounded source-frame previews."""
from __future__ import annotations

import hashlib
import io
import math
import subprocess
import threading
from collections import OrderedDict

from PIL import Image


class VideoMoments:
    def __init__(self, media):
        self.media = media
        self._frames = OrderedDict()
        self._lock = threading.Lock()
        self._decoder = threading.BoundedSemaphore(2)

    @staticmethod
    def valid_time(file, value):
        try:
            stamp = float(value)
            duration = float(file.get("duration") or 0)
        except (ValueError, TypeError):
            raise ValueError("视频时间点无效") from None
        if file.get("kind") != "video" or not math.isfinite(duration) or not math.isfinite(stamp) or not 0 <= stamp < duration:
            raise ValueError("请选择视频时长以内的时间点")
        return stamp

    def candidates(self, file, query="", limit=12):
        self.valid_time(file, 0)
        self.media.source_path(file)
        if not self.media.enabled:
            return {"moments": [], "indexed": False, "query": query}
        signature = self.media.signature(file)
        conditions = {"file_id": int(file["id"]), "library_id": int(file["library_id"]),
                      "kind": "video", "signature": signature, "source_label": "直接视频画面",
                      "mtime_ns": int(file["mtime_ns"]), "size": int(file["size"])}
        body = {"filter": {"must": [{"key": key, "match": {"value": value}} for key, value in conditions.items()]},
                "limit": 64, "with_payload": True, "with_vector": False}
        base = f"{self.media.settings.qdrant_url}/collections/{self.media.settings.multimodal_collection}/points"
        if query:
            body["query"] = self.media.query_embedding(query)
            # Do not create a missing collection or rebuild a stale generation.
            response = self.media._http.post(base + "/query", json=body, timeout=30)
        else:
            response = self.media._http.post(base + "/scroll", json=body, timeout=30)
        if response.status_code == 404:
            return {"moments": [], "indexed": False, "query": query}
        response.raise_for_status()
        result = response.json().get("result") or {}
        hits = result if isinstance(result, list) else result.get("points", [])
        moments = []
        seen = set()
        for hit in hits:
            payload = hit.get("payload") or {}
            if any(payload.get(key) != value for key, value in conditions.items()):
                continue
            try:
                stamp = self.valid_time(file, payload.get("start_time"))
            except ValueError:
                continue
            milliseconds = round(stamp * 1000)
            if milliseconds in seen:
                continue
            seen.add(milliseconds)
            moments.append({"time": stamp, "source_label": "直接视频画面"})
        if not query:
            moments.sort(key=lambda item: item["time"])
        self.media.source_path(file)
        return {"moments": moments[:max(1, min(12, limit))], "indexed": bool(moments), "query": query,
                "revision": f"{file['mtime_ns']}:{file['size']}",
                "sampled": True}

    def frame(self, file, stamp):
        stamp = self.valid_time(file, stamp)
        source = self.media.source_path(file)
        # Same seek precision is used for the cache key and actual decoder.
        seek = f"{stamp:.3f}"
        if float(seek) >= float(file["duration"]):
            raise ValueError("时间点已超出可预览范围")
        key = hashlib.sha256(f"{source}:{file['mtime_ns']}:{file['size']}:{seek}".encode()).hexdigest()
        with self._lock:
            if key in self._frames:
                self._frames.move_to_end(key)
                return self._frames[key]
        if not self._decoder.acquire(timeout=10):
            raise TimeoutError("画面正在读取，请稍后重试")
        try:
            try:
                result = subprocess.run(["ffmpeg", "-v", "error", "-threads", "1", "-ss", seek,
                                         "-i", str(source), "-an", "-frames:v", "1", "-vf", "scale=480:-2",
                                         "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-threads", "1", "pipe:1"],
                                        capture_output=True, timeout=20)
            except subprocess.TimeoutExpired:
                raise TimeoutError("画面读取超时") from None
            if result.returncode or not result.stdout or len(result.stdout) > 2 * 1024 * 1024:
                raise ValueError("该时间点的画面暂时无法读取")
            with Image.open(io.BytesIO(result.stdout)) as image:
                image.verify()
            self.media.source_path(file)
            with self._lock:
                self._frames[key] = result.stdout
                while len(self._frames) > 128 or sum(map(len, self._frames.values())) > 8 * 1024 * 1024:
                    self._frames.popitem(last=False)
            return result.stdout
        finally:
            self._decoder.release()
