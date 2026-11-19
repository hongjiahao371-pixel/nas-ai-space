from __future__ import annotations

import json
import math
from collections.abc import Callable
from sqlite3 import Connection
from typing import Any

from app.database import Database, utc_now
from app.services.workspaces import WorkspaceService


MAX_ITEMS = 200
FileLookup = Callable[[int], dict[str, Any]]


class BasketFull(ValueError):
    pass


class InvalidClip(ValueError):
    pass


class ClipConflict(ValueError):
    pass


class ClipNotFound(ValueError):
    pass


def clip_times(start_time: float, end_time: float) -> tuple[float, float]:
    if not math.isfinite(start_time) or not math.isfinite(end_time) or not 0 <= start_time < end_time <= 31 * 86400:
        raise InvalidClip("片段时间无效，请确保出点晚于入点")
    start, end = round(start_time, 6), round(end_time, 6)
    if end <= start:
        raise InvalidClip("片段时间过短")
    return start, end


def validate_clip_file(file: dict[str, Any], end_time: float) -> None:
    duration = file.get("duration")
    if file.get("kind") != "video" or duration is None or not math.isfinite(duration) or duration <= 0:
        raise ClipConflict("只能加入已获取时长的视频片段")
    if end_time > round(duration, 6):
        raise ClipConflict("片段出点不能超过当前视频时长")


def clip_frame_rate(file: dict[str, Any]) -> float | None:
    try:
        rate = float(json.loads(file.get("metadata_json") or "{}").get("frame_rate") or 0)
        return rate if math.isfinite(rate) and rate > 0 else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


class MaterialBasket:
    def __init__(self, database: Database, workspaces: WorkspaceService):
        self.database = database
        self.workspaces = workspaces

    @staticmethod
    def _ids(connection: Connection, user_id: int) -> list[int]:
        return [row["file_id"] for row in connection.execute(
            "SELECT file_id FROM material_basket_items WHERE user_id = ? ORDER BY id", (user_id,),
        ).fetchall()]

    def ids(self, user_id: int) -> list[int]:
        with self.database.read_connection() as connection:
            return self._ids(connection, user_id)

    @staticmethod
    def _clips(connection: Connection, user_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute(
            """SELECT id, file_id, start_time, end_time, position FROM material_basket_clips
               WHERE user_id = ? ORDER BY position, id""", (user_id,),
        ).fetchall()]

    def snapshot(self, user_id: int) -> tuple[list[int], list[dict[str, Any]]]:
        with self.database.read_connection() as connection:
            connection.execute("BEGIN")
            try:
                return self._ids(connection, user_id), self._clips(connection, user_id)
            finally:
                connection.rollback()

    def add(self, user_id: int, file_ids: list[int], lookup: FileLookup) -> None:
        unique = list(dict.fromkeys(file_ids))
        with self.database.transaction() as connection:
            for file_id in unique:
                lookup(file_id)
            existing = set(self._ids(connection, user_id))
            new_ids = [file_id for file_id in unique if file_id not in existing]
            if new_ids and len(existing) + len(new_ids) + len(self._clips(connection, user_id)) > MAX_ITEMS:
                raise BasketFull(f"素材篮最多可保存 {MAX_ITEMS} 项")
            connection.executemany(
                "INSERT INTO material_basket_items(user_id, file_id) VALUES (?, ?)",
                ((user_id, file_id) for file_id in new_ids),
            )

    def remove(self, user_id: int, file_id: int) -> None:
        self.database.execute(
            "DELETE FROM material_basket_items WHERE user_id = ? AND file_id = ?", (user_id, file_id),
        )

    def clear(self, user_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM material_basket_items WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM material_basket_clips WHERE user_id = ?", (user_id,))

    def add_clip(self, user_id: int, file_id: int, start: float, end: float, lookup: FileLookup) -> None:
        start, end = clip_times(start, end)
        with self.database.transaction() as connection:
            validate_clip_file(lookup(file_id), end)
            clips = self._clips(connection, user_id)
            if any((clip["file_id"], clip["start_time"], clip["end_time"]) == (file_id, start, end) for clip in clips):
                return
            if len(clips) + len(self._ids(connection, user_id)) >= MAX_ITEMS:
                raise BasketFull(f"素材篮最多可保存 {MAX_ITEMS} 项")
            connection.execute(
                "INSERT INTO material_basket_clips(user_id,file_id,start_time,end_time,position) VALUES (?,?,?,?,?)",
                (user_id, file_id, start, end, len(clips)),
            )

    def edit_clip(self, user_id: int, clip_id: int, start: float, end: float, lookup: FileLookup) -> None:
        start, end = clip_times(start, end)
        with self.database.transaction() as connection:
            clips = self._clips(connection, user_id)
            clip = next((clip for clip in clips if clip["id"] == clip_id), None)
            if not clip:
                raise ClipNotFound("片段不存在")
            validate_clip_file(lookup(clip["file_id"]), end)
            if any(
                other["id"] != clip_id
                and (other["file_id"], other["start_time"], other["end_time"]) == (clip["file_id"], start, end)
                for other in clips
            ):
                raise ClipConflict("同一素材的这个时间区间已在素材篮")
            connection.execute(
                "UPDATE material_basket_clips SET start_time = ?,end_time = ? WHERE user_id = ? AND id = ?",
                (start, end, user_id, clip_id),
            )

    @staticmethod
    def _set_order(connection: Connection, user_id: int, clip_ids: list[int]) -> None:
        connection.executemany(
            "UPDATE material_basket_clips SET position = ? WHERE user_id = ? AND id = ?",
            ((position, user_id, clip_id) for position, clip_id in enumerate(clip_ids)),
        )

    def remove_clip(self, user_id: int, clip_id: int) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute("DELETE FROM material_basket_clips WHERE user_id = ? AND id = ?", (user_id, clip_id))
            if not cursor.rowcount:
                raise ClipNotFound("片段不存在")
            self._set_order(connection, user_id, [clip["id"] for clip in self._clips(connection, user_id)])

    def reorder_clips(self, user_id: int, clip_ids: list[int]) -> None:
        with self.database.transaction() as connection:
            current = {clip["id"] for clip in self._clips(connection, user_id)}
            if len(clip_ids) != len(current) or set(clip_ids) != current:
                raise ClipConflict("片段清单已变化，请刷新后重新排序")
            self._set_order(connection, user_id, clip_ids)

    def handoff(
        self,
        user_id: int,
        project_id: int,
        check_project: Callable[[], Any],
        lookup: FileLookup,
    ) -> dict[str, Any]:
        # A single writer locks the basket snapshot, project membership, dedup and inserts.
        # It also prevents simultaneous retries in separate worker processes from creating duplicates.
        with self.database.transaction() as connection:
            check_project()
            clips = self._clips(connection, user_id)
            file_ids = list(dict.fromkeys([*self._ids(connection, user_id), *(clip["file_id"] for clip in clips)]))
            if not file_ids:
                return {"project_id": project_id, "added": 0, "skipped": 0, "items": [], "clips": []}
            files = {file_id: lookup(file_id) for file_id in file_ids}
            for clip in clips:
                _, end = clip_times(clip["start_time"], clip["end_time"])
                validate_clip_file(files[clip["file_id"]], end)
            placeholders = ",".join("?" for _ in file_ids)
            rows = connection.execute(
                f"""SELECT a.id, a.title, v.file_id, v.id AS version_id, v.version_number
                    FROM assets a JOIN asset_versions v ON v.asset_id = a.id
                    WHERE a.project_id = ? AND v.file_id IN ({placeholders})
                    ORDER BY a.id, (v.id = a.cover_version_id) DESC, v.version_number DESC""",
                (project_id, *file_ids),
            ).fetchall()
            existing = {}
            for row in rows:
                existing.setdefault(row["file_id"], row)
            items = []
            resolved = {}
            added = 0
            for file_id, file in files.items():
                if file_id in existing:
                    row = existing[file_id]
                    resolved[file_id] = (row["id"], row["version_id"], row["version_number"], row["title"])
                    items.append({"id": row["id"], "file_id": file_id, "title": row["title"], "created": False})
                    continue
                title = file["name"]
                asset_id = self.workspaces.insert_asset(connection, project_id, file, None, title, user_id)
                version_id = connection.execute("SELECT cover_version_id FROM assets WHERE id = ?", (asset_id,)).fetchone()[0]
                resolved[file_id] = (asset_id, version_id, 1, title)
                items.append({"id": asset_id, "file_id": file_id, "title": title, "created": True})
                added += 1
            if added:
                connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now(), project_id))
            resolved_clips = []
            for clip in clips:
                file_id = clip["file_id"]
                asset_id, version_id, version_number, title = resolved[file_id]
                resolved_clips.append({
                    "id": clip["id"], "position": clip["position"],
                    "asset_id": asset_id, "version_id": version_id, "version_number": version_number,
                    "file_id": file_id, "title": title,
                    "start_time": clip["start_time"], "end_time": clip["end_time"],
                    "duration": round(clip["end_time"] - clip["start_time"], 6),
                    "source_duration": files[file_id]["duration"],
                    "frame_rate": clip_frame_rate(files[file_id]),
                })
            return {"project_id": project_id, "added": added, "skipped": len(items) - added,
                    "items": items, "clips": resolved_clips}
