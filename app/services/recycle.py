from __future__ import annotations

import logging
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import Database
from app.services.vectors import VectorStore


logger = logging.getLogger(__name__)


class RecycleBin:
    def __init__(self, database: Database, settings: Settings, vectors: VectorStore):
        self.database = database
        self.settings = settings
        self.vectors = vectors

    def _physical_path(self, logical_path: str, library_path: str) -> Path:
        logical = Path(logical_path).resolve()
        upload_root = self.settings.upload_root.resolve()
        if logical.is_relative_to(upload_root):
            return logical
        for source, destination in self.settings.mutation_roots:
            if logical.is_relative_to(source):
                mapped = (destination / logical.relative_to(source)).resolve()
                if not mapped.is_relative_to(destination):
                    break
                return mapped
        library_root = Path(library_path).resolve()
        if logical.is_relative_to(library_root) and os.access(library_root, os.W_OK):
            return logical
        raise PermissionError("该媒体库未配置可写维护挂载，不能移动原文件")

    def _safe_recycle_path(self, value: str) -> Path:
        path = Path(value).resolve()
        if not path.is_relative_to(self.settings.recycle_root.resolve()):
            raise PermissionError("回收站路径无效")
        return path

    def move_duplicates(self, file_ids: list[int], actor: str) -> dict[str, Any]:
        selected = sorted(set(int(value) for value in file_ids))
        if not selected:
            raise ValueError("请选择要移入回收站的重复文件")
        rows = [self.database.get_file(file_id) for file_id in selected]
        if any(row is None for row in rows):
            raise FileNotFoundError("部分文件已不存在")
        files = [row for row in rows if row]
        placeholders = ",".join("?" for _ in selected)
        for file in files:
            if not file.get("content_hash"):
                raise ValueError(f"{file['name']} 尚未完成完整内容校验")
            remaining = self.database.fetchone(
                f"""SELECT COUNT(*) AS count FROM files WHERE content_hash = ? AND size = ?
                    AND id NOT IN ({placeholders})""",
                [file["content_hash"], file["size"], *selected],
            )
            if not remaining or int(remaining["count"]) < 1:
                raise ValueError(f"{file['name']} 没有未选中的安全保留副本")

        plans: list[dict[str, Any]] = []
        for file in files:
            library = self.database.get_library(int(file["library_id"]))
            if not library:
                raise FileNotFoundError("媒体库不存在")
            source = self._physical_path(file["path"], library["path"])
            if not source.is_file():
                raise FileNotFoundError(f"原文件不存在：{file['name']}")
            directory = self.settings.recycle_root / datetime.now().strftime("%Y/%m")
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / f"{secrets.token_hex(12)}-{Path(file['name']).name}"
            plans.append({"file": file, "source": source, "destination": destination})

        moved: list[dict[str, Any]] = []
        try:
            for plan in plans:
                shutil.move(str(plan["source"]), str(plan["destination"]))
                moved.append(plan)
            item_ids = self.database.trash_files(
                [(plan["file"], str(plan["destination"])) for plan in moved],
                actor,
            )
        except Exception:
            for item in reversed(moved):
                if item["destination"].exists() and not item["source"].exists():
                    item["source"].parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(item["destination"]), str(item["source"]))
            raise

        try:
            self.vectors.delete_files([int(item["file"]["id"]) for item in moved])
        except Exception as exc:
            logger.warning("回收站向量清理失败（%d 个文件）：%s", len(moved), exc)
        for library_id in {int(item["file"]["library_id"]) for item in moved}:
            self.database.update_library_stats(library_id)
        return {
            "moved": len(moved),
            "bytes": sum(int(file["size"]) for file in files),
            "items": item_ids,
        }

    def restore(self, item_id: int) -> dict[str, Any]:
        item = self.database.get_trash_item(item_id)
        if not item or item["status"] != "trashed":
            raise FileNotFoundError("回收站项目不存在")
        library = self.database.get_library(int(item["library_id"]))
        if not library:
            raise FileNotFoundError("原媒体库不存在")
        source = self._safe_recycle_path(item["recycle_path"])
        destination = self._physical_path(item["original_path"], library["path"])
        if not source.is_file():
            raise FileNotFoundError("回收站文件已不存在")
        if destination.exists():
            raise FileExistsError("原路径已有同名文件，无法自动恢复")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        try:
            self.database.mark_trash_restored(item_id)
        except Exception:
            shutil.move(str(destination), str(source))
            raise
        return {
            "library_id": int(item["library_id"]),
            "path": item["original_path"],
            "name": item["name"],
        }

    def purge(self, item_id: int) -> dict[str, Any]:
        item = self.database.get_trash_item(item_id)
        if not item or item["status"] != "trashed":
            raise FileNotFoundError("回收站项目不存在")
        path = self._safe_recycle_path(item["recycle_path"])
        path.unlink(missing_ok=True)
        self.database.mark_trash_purged(item_id)
        return {"purged": True, "bytes": int(item["size"])}
