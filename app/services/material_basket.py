from __future__ import annotations

from collections.abc import Callable
from sqlite3 import Connection
from typing import Any

from app.database import Database, utc_now
from app.services.workspaces import WorkspaceService


MAX_ITEMS = 200
FileLookup = Callable[[int], dict[str, Any]]


class BasketFull(ValueError):
    pass


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

    def add(self, user_id: int, file_ids: list[int], lookup: FileLookup) -> None:
        unique = list(dict.fromkeys(file_ids))
        with self.database.transaction() as connection:
            for file_id in unique:
                lookup(file_id)
            existing = set(self._ids(connection, user_id))
            new_ids = [file_id for file_id in unique if file_id not in existing]
            if len(existing) + len(new_ids) > MAX_ITEMS:
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
        self.database.execute("DELETE FROM material_basket_items WHERE user_id = ?", (user_id,))

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
            file_ids = self._ids(connection, user_id)
            files = [lookup(file_id) for file_id in file_ids]
            if not files:
                return {"project_id": project_id, "added": 0, "skipped": 0, "items": []}
            placeholders = ",".join("?" for _ in file_ids)
            rows = connection.execute(
                f"""SELECT a.id, a.title, v.file_id FROM assets a JOIN asset_versions v ON v.asset_id = a.id
                    WHERE a.project_id = ? AND v.file_id IN ({placeholders}) ORDER BY a.id""",
                (project_id, *file_ids),
            ).fetchall()
            existing = {}
            for row in rows:
                existing.setdefault(row["file_id"], row)
            items = []
            added = 0
            for file in files:
                file_id = int(file["id"])
                if file_id in existing:
                    row = existing[file_id]
                    items.append({"id": row["id"], "file_id": file_id, "title": row["title"], "created": False})
                    continue
                title = file["name"]
                asset_id = self.workspaces.insert_asset(connection, project_id, file, None, title, user_id)
                items.append({"id": asset_id, "file_id": file_id, "title": title, "created": True})
                added += 1
            if added:
                connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now(), project_id))
            return {"project_id": project_id, "added": added, "skipped": len(items) - added, "items": items}
