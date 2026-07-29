from __future__ import annotations

import hashlib
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps

from app.config import Settings
from app.database import Database
from app.services.extractors import _convert_image, quick_hash


Progress = Callable[[float, str], None]
Cancelled = Callable[[], bool]


def _bit_count(value: int) -> int:
    return value.bit_count() if hasattr(value, "bit_count") else bin(value).count("1")


def _perceptual_hash(path: Path, settings: Settings) -> str:
    temporary: tempfile.TemporaryDirectory[str] | None = None
    prepared = path
    try:
        try:
            image = Image.open(prepared)
        except (OSError, ValueError):
            temporary = tempfile.TemporaryDirectory(dir=settings.cache_dir, prefix="similar-")
            prepared = Path(temporary.name) / "converted.jpg"
            _convert_image(path, prepared, 1024)
            image = Image.open(prepared)
        with image:
            image.draft("RGB", (128, 128))
            image.thumbnail((128, 128), Image.Resampling.LANCZOS)
            oriented = ImageOps.exif_transpose(image).convert("RGB")
            resized = oriented.convert("L").resize((9, 6), Image.Resampling.LANCZOS)
            pixels = resized.load()
            difference = 0
            for y in range(6):
                for x in range(8):
                    difference = (difference << 1) | int(pixels[x, y] > pixels[x + 1, y])
            red, green, blue = oriented.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
            color = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
            value = (difference << 16) | color
            return f"{value:016x}"
    finally:
        if temporary:
            temporary.cleanup()


def analyze_duplicates(
    database: Database,
    _: Settings,
    progress: Progress,
    cancelled: Cancelled,
) -> dict[str, int]:
    rows = database.fetchall(
        """SELECT id, path, size, quick_hash FROM files WHERE size > 0 AND size IN (
           SELECT size FROM files WHERE size > 0 GROUP BY size HAVING COUNT(*) > 1)
           ORDER BY size, id"""
    )
    total = len(rows)
    updates: list[tuple[str, str, int]] = []
    hashed = 0
    missing = 0
    for index, row in enumerate(rows, 1):
        if cancelled():
            raise InterruptedError("任务已取消")
        if not row["quick_hash"]:
            path = Path(row["path"])
            if not path.is_file():
                missing += 1
            else:
                try:
                    updates.append((quick_hash(path, int(row["size"])), "", int(row["id"])))
                    hashed += 1
                    if len(updates) >= 100:
                        database.update_file_hashes(updates)
                        updates.clear()
                except OSError:
                    missing += 1
        if index % 50 == 0 or index == total:
            progress(0.7 * index / max(1, total), f"正在核对重复文件 {index:,}/{total:,}")
    database.update_file_hashes(updates)
    exact_candidates = database.fetchall(
        """SELECT id, path, size, content_hash FROM files WHERE quick_hash != '' AND (quick_hash, size) IN (
           SELECT quick_hash, size FROM files WHERE quick_hash != '' GROUP BY quick_hash, size HAVING COUNT(*) > 1)
           ORDER BY size, quick_hash, id"""
    )
    full_updates: list[tuple[str, int]] = []
    verified = 0
    for index, row in enumerate(exact_candidates, 1):
        if cancelled():
            raise InterruptedError("任务已取消")
        if not row["content_hash"]:
            path = Path(row["path"])
            if not path.is_file():
                missing += 1
            else:
                try:
                    digest = hashlib.blake2b(digest_size=32)
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                            if cancelled():
                                raise InterruptedError("任务已取消")
                            digest.update(chunk)
                    full_updates.append((digest.hexdigest(), int(row["id"])))
                    verified += 1
                    if len(full_updates) >= 50:
                        database.update_content_hashes(full_updates)
                        full_updates.clear()
                except OSError:
                    missing += 1
        if index % 10 == 0 or index == len(exact_candidates):
            progress(0.7 + 0.3 * index / max(1, len(exact_candidates)), f"正在完整校验 {index:,}/{len(exact_candidates):,}")
    database.update_content_hashes(full_updates)
    result = database.duplicate_groups(1, 0)
    progress(1, f"发现 {int(result['total']):,} 组重复文件")
    return {"candidates": total, "hashed": hashed, "verified": verified, "missing": missing, "groups": int(result["total"])}


class _DisjointSet:
    def __init__(self, size: int):
        self.parents = list(range(size))

    def find(self, value: int) -> int:
        while self.parents[value] != value:
            self.parents[value] = self.parents[self.parents[value]]
            value = self.parents[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parents[right_root] = left_root


def _similar_components(rows: list[dict[str, Any]], max_distance: int) -> list[list[tuple[int, int]]]:
    if not rows:
        return []
    values = [int(row["perceptual_hash"], 16) for row in rows]
    sets = _DisjointSet(len(rows))
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, value in enumerate(values):
        candidates: set[int] = set()
        bit_offset = 0
        for band, width in enumerate((7, 7, 7, 7, 7, 7, 7, 7, 8)):
            key = (band, (value >> bit_offset) & ((1 << width) - 1))
            candidates.update(buckets[key])
            buckets[key].append(index)
            bit_offset += width
        for candidate in candidates:
            if _bit_count(value ^ values[candidate]) <= max_distance:
                sets.union(candidate, index)
    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(rows)):
        components[sets.find(index)].append(index)
    groups: list[list[tuple[int, int]]] = []
    for indexes in components.values():
        if len(indexes) < 2:
            continue
        representative = values[indexes[0]]
        groups.append([
            (int(rows[index]["id"]), _bit_count(representative ^ values[index]))
            for index in indexes
        ])
    return sorted(groups, key=len, reverse=True)


def analyze_similar(
    database: Database,
    settings: Settings,
    progress: Progress,
    cancelled: Cancelled,
    max_distance: int = 8,
) -> dict[str, int]:
    rows = database.fetchall(
        "SELECT id, path, perceptual_hash FROM files WHERE kind = 'image' ORDER BY id"
    )
    total = len(rows)
    updates: list[tuple[str, str, int]] = []
    analyzed = 0
    failed = 0
    for index, row in enumerate(rows, 1):
        if cancelled():
            raise InterruptedError("任务已取消")
        if not row["perceptual_hash"]:
            try:
                value = _perceptual_hash(Path(row["path"]), settings)
                row["perceptual_hash"] = value
                updates.append(("", value, int(row["id"])))
                analyzed += 1
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                failed += 1
        if len(updates) >= 100:
            database.update_file_hashes(updates)
            updates.clear()
        if index % 25 == 0 or index == total:
            progress(index / max(1, total) * 0.9, f"正在生成图片指纹 {index:,}/{total:,}")
    database.update_file_hashes(updates)
    hashed_rows = [row for row in rows if row.get("perceptual_hash")]
    progress(0.94, "正在聚合相似照片")
    groups = _similar_components(hashed_rows, max_distance)
    database.replace_similarity_groups(groups)
    progress(1, f"发现 {len(groups):,} 组相似照片")
    return {"files": total, "analyzed": analyzed, "failed": failed, "groups": len(groups)}
