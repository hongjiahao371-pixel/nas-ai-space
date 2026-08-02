from __future__ import annotations

import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from app.database import Database


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif", ".raw", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".psd", ".psb", ".ai", ".eps", ".ttf", ".otf", ".ttc"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".mts", ".m2ts", ".flv", ".wmv"}
AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".ape"}
DOCUMENT_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".epub", ".log", ".srt", ".vtt"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}
# 3D 模型归 other：不生成缩略图、不抽取文本，预览由前端审阅页按扩展名处理
MODEL_EXTENSIONS = {".obj", ".ply", ".glb", ".gltf"}
IGNORED_NAMES = {"@eaDir", ".snapshot", ".recycle", "#recycle", "$RECYCLE.BIN", ".Trash", ".Trashes", ".AppleDouble"}


def file_kind(extension: str) -> str:
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in DOCUMENT_EXTENSIONS:
        return "document"
    if extension in ARCHIVE_EXTENSIONS:
        return "archive"
    if extension in MODEL_EXTENSIONS:
        return "other"
    return "other"


def is_ignored(path: Path) -> bool:
    return (
        path.name in IGNORED_NAMES
        or path.name.startswith(("._", ".nas-ai-import-"))
        or path.name == ".DS_Store"
    )


def scan_library(
    database: Database,
    library: dict,
    progress: Callable[[float, str], None],
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    root = Path(library["path"]).resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"目录不存在或不可访问：{root}")

    scan_token = uuid.uuid4().hex
    values_batch: list[dict[str, Any]] = []
    changed = 0
    unchanged = 0
    errors = 0
    scanned = 0
    stack = [root]

    def flush_batch(finalize: bool = False) -> None:
        nonlocal changed, unchanged, scanned
        results = database.upsert_files(values_batch, finalize=finalize)
        changed += sum(int(was_changed) for _, was_changed in results)
        unchanged += sum(int(not was_changed) for _, was_changed in results)
        scanned += len(results)
        values_batch.clear()
        if scanned and scanned % 500 == 0:
            progress(0.05, f"已扫描 {scanned:,} 个文件")

    while stack:
        if cancelled():
            raise InterruptedError("任务已取消")
        directory = stack.pop()
        try:
            iterator = os.scandir(directory)
        except (PermissionError, OSError):
            errors += 1
            continue
        with iterator:
            for entry in iterator:
                path = Path(entry.path)
                if is_ignored(path):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except (PermissionError, FileNotFoundError, OSError):
                    errors += 1
                    continue
                absolute = str(path.resolve())
                extension = path.suffix.lower()
                values_batch.append({
                    "library_id": library["id"],
                    "path": absolute,
                    "relative_path": str(path.relative_to(root)),
                    "name": path.name,
                    "extension": extension,
                    "kind": file_kind(extension),
                    "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "inode": getattr(stat, "st_ino", 0),
                    "scan_token": scan_token,
                })
                if len(values_batch) >= 500:
                    flush_batch()

    # 最后一次 flush 传 finalize=True：即使本批为空也会触发 similarity_groups 的一次性重算
    flush_batch(finalize=True)
    removed_ids = database.mark_missing_files(int(library["id"]), scan_token)
    database.update_library_stats(int(library["id"]))
    progress(0.1, f"扫描完成，新增或变化 {changed:,} 个文件")
    return {
        "scanned": scanned,
        "changed": changed,
        "unchanged": unchanged,
        "removed": len(removed_ids),
        "removed_file_ids": removed_ids,
        "errors": errors,
    }
