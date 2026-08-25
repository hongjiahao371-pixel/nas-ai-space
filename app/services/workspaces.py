from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.database import Database, utc_now
from app.security import token_digest


PROJECT_ROLES = {"owner", "manager", "editor", "reviewer", "viewer"}
EDIT_ROLES = {"owner", "manager", "editor"}
COMMENT_ROLES = {"owner", "manager", "editor", "reviewer"}
MANAGE_ROLES = {"owner", "manager"}


class WorkspaceService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _loads(value: str, default: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, json.JSONDecodeError):
            return default

    def access_role(self, project_id: int, user_id: int | None, admin: bool = False) -> str:
        if admin:
            return "owner"
        if user_id is None:
            return ""
        row = self.database.fetchone(
            """SELECT CASE WHEN p.owner_id = ? THEN 'owner' ELSE pm.role END AS role
               FROM projects p LEFT JOIN project_members pm
               ON pm.project_id = p.id AND pm.user_id = ?
               WHERE p.id = ?""",
            (user_id, user_id, project_id),
        )
        return str(row["role"] or "") if row else ""

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        row = self.database.fetchone(
            """SELECT p.*, u.display_name AS owner_name,
               (SELECT COUNT(*) FROM assets a WHERE a.project_id = p.id) AS asset_count,
               (SELECT COUNT(*) FROM project_members pm WHERE pm.project_id = p.id) AS member_count,
               (SELECT COUNT(*) FROM review_comments rc JOIN assets a ON a.id = rc.asset_id
                WHERE a.project_id = p.id AND rc.resolved = 0) AS open_comment_count
               FROM projects p LEFT JOIN users u ON u.id = p.owner_id WHERE p.id = ?""",
            (project_id,),
        )
        return row

    def list_projects(self, user_id: int | None, admin: bool = False) -> list[dict[str, Any]]:
        membership = "" if admin else "JOIN project_members mine ON mine.project_id = p.id AND mine.user_id = ?"
        params: list[Any] = [] if admin else [user_id]
        rows = self.database.fetchall(
            f"""SELECT p.*, u.display_name AS owner_name,
                {("'owner'" if admin else "mine.role")} AS access_role,
                COUNT(DISTINCT a.id) AS asset_count,
                COUNT(DISTINCT pm.user_id) AS member_count,
                COUNT(DISTINCT CASE WHEN rc.resolved = 0 THEN rc.id END) AS open_comment_count,
                MAX(COALESCE(a.updated_at, p.updated_at)) AS activity_at
                FROM projects p {membership}
                LEFT JOIN users u ON u.id = p.owner_id
                LEFT JOIN assets a ON a.project_id = p.id
                LEFT JOIN project_members pm ON pm.project_id = p.id
                LEFT JOIN review_comments rc ON rc.asset_id = a.id
                GROUP BY p.id ORDER BY activity_at DESC, p.id DESC""",
            params,
        )
        return rows

    def create_project(
        self,
        name: str,
        description: str,
        color: str,
        user_id: int | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO projects(name, description, color, owner_id, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name.strip(), description.strip(), color, user_id, user_id, now, now),
            )
            project_id = int(cursor.lastrowid)
            if user_id is not None:
                connection.execute(
                    """INSERT INTO project_members(project_id, user_id, role, created_at)
                       VALUES (?, ?, 'owner', ?)""",
                    (project_id, user_id, now),
                )
            connection.executemany(
                """INSERT INTO project_statuses(project_id, key, name, color, sort_order, is_terminal)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (project_id, "draft", "待整理", "#7f8997", 10, 0),
                    (project_id, "in_progress", "制作中", "#7c8cff", 20, 0),
                    (project_id, "review", "待审阅", "#efb65c", 30, 0),
                    (project_id, "approved", "已通过", "#55d6a7", 40, 0),
                    (project_id, "delivered", "已交付", "#5be0c0", 50, 1),
                ],
            )
        return self.get_project(project_id) or {}

    def update_project(
        self,
        project_id: int,
        name: str,
        description: str,
        color: str,
        status: str,
    ) -> dict[str, Any]:
        self.database.execute(
            """UPDATE projects SET name = ?, description = ?, color = ?, status = ?, updated_at = ?
               WHERE id = ?""",
            (name.strip(), description.strip(), color, status, utc_now(), project_id),
        )
        return self.get_project(project_id) or {}

    def delete_project(self, project_id: int) -> None:
        self.database.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def list_members(self, project_id: int) -> list[dict[str, Any]]:
        return self.database.fetchall(
            """SELECT u.id, u.username, u.display_name, u.enabled, pm.role, pm.created_at
               FROM project_members pm JOIN users u ON u.id = pm.user_id
               WHERE pm.project_id = ? ORDER BY
               CASE pm.role WHEN 'owner' THEN 0 WHEN 'manager' THEN 1 WHEN 'editor' THEN 2
                    WHEN 'reviewer' THEN 3 ELSE 4 END, u.display_name COLLATE NOCASE""",
            (project_id,),
        )

    def set_member(self, project_id: int, user_id: int, role: str) -> None:
        if role not in PROJECT_ROLES:
            raise ValueError("不支持的项目角色")
        project = self.database.fetchone("SELECT owner_id FROM projects WHERE id = ?", (project_id,))
        if not project:
            raise ValueError("项目不存在")
        if int(project["owner_id"] or 0) == int(user_id):
            if role != "owner":
                raise ValueError("不能修改项目所有者的角色")
        elif role == "owner":
            raise ValueError("不能通过成员角色转移项目所有权")
        now = utc_now()
        self.database.execute(
            """INSERT INTO project_members(project_id, user_id, role, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(project_id, user_id) DO UPDATE SET role = excluded.role""",
            (project_id, user_id, role, now),
        )

    def remove_member(self, project_id: int, user_id: int) -> None:
        project = self.database.fetchone("SELECT owner_id FROM projects WHERE id = ?", (project_id,))
        if project and project["owner_id"] == user_id:
            raise ValueError("不能移除项目所有者")
        self.database.execute(
            "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        )

    def list_folders(self, project_id: int) -> list[dict[str, Any]]:
        return self.database.fetchall(
            """SELECT f.*, COUNT(a.id) AS asset_count FROM project_folders f
               LEFT JOIN assets a ON a.folder_id = f.id WHERE f.project_id = ?
               GROUP BY f.id ORDER BY f.parent_id, f.sort_order, f.name COLLATE NOCASE""",
            (project_id,),
        )

    def create_folder(self, project_id: int, name: str, parent_id: int | None) -> dict[str, Any]:
        if parent_id is not None:
            parent = self.database.fetchone(
                "SELECT id FROM project_folders WHERE id = ? AND project_id = ?",
                (parent_id, project_id),
            )
            if not parent:
                raise ValueError("上级文件夹不存在")
        duplicate = self.database.fetchone(
            """SELECT id FROM project_folders WHERE project_id = ? AND
               ((parent_id = ?) OR (parent_id IS NULL AND ? IS NULL)) AND name = ? COLLATE NOCASE""",
            (project_id, parent_id, parent_id, name.strip()),
        )
        if duplicate:
            raise ValueError("同级文件夹名称已存在")
        now = utc_now()
        folder_id = self.database.execute(
            """INSERT INTO project_folders(project_id, parent_id, name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (project_id, parent_id, name.strip(), now, now),
        )
        return self.database.fetchone("SELECT * FROM project_folders WHERE id = ?", (folder_id,)) or {}

    def delete_folder(self, project_id: int, folder_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE assets SET folder_id = NULL, updated_at = ? WHERE project_id = ? AND folder_id = ?",
                (utc_now(), project_id, folder_id),
            )
            connection.execute(
                "DELETE FROM project_folders WHERE id = ? AND project_id = ?",
                (folder_id, project_id),
            )

    def list_statuses(self, project_id: int) -> list[dict[str, Any]]:
        return self.database.fetchall(
            "SELECT * FROM project_statuses WHERE project_id = ? ORDER BY sort_order, id",
            (project_id,),
        )

    def set_statuses(self, project_id: int, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[tuple[int, str, str, str, int, int]] = []
        for index, value in enumerate(values[:20]):
            key = str(value.get("key") or "").strip().lower().replace(" ", "_")[:40]
            name = str(value.get("name") or "").strip()[:50]
            if not key or not name:
                continue
            normalized.append((
                project_id,
                key,
                name,
                str(value.get("color") or "#7f8997")[:20],
                index * 10,
                int(bool(value.get("is_terminal"))),
            ))
        if not normalized:
            raise ValueError("至少保留一个有效状态")
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM project_statuses WHERE project_id = ?", (project_id,))
            connection.executemany(
                """INSERT INTO project_statuses(project_id, key, name, color, sort_order, is_terminal)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                normalized,
            )
            placeholders = ",".join("?" for _ in normalized)
            connection.execute(
                f"""UPDATE assets SET status = ?, updated_at = ? WHERE project_id = ?
                    AND status NOT IN ({placeholders})""",
                (normalized[0][1], utc_now(), project_id, *[item[1] for item in normalized]),
            )
        return self.list_statuses(project_id)

    @staticmethod
    def _version_snapshot(file: dict[str, Any]) -> tuple[Any, ...]:
        return (
            int(file["id"]),
            str(file.get("name") or ""),
            str(file.get("mime_type") or ""),
            str(file.get("kind") or "other"),
            int(file.get("size") or 0),
            file.get("duration"),
            file.get("width"),
            file.get("height"),
        )

    def create_asset(
        self,
        project_id: int,
        file: dict[str, Any],
        folder_id: int | None,
        title: str,
        user_id: int | None,
    ) -> dict[str, Any]:
        if folder_id is not None and not self.database.fetchone(
            "SELECT id FROM project_folders WHERE id = ? AND project_id = ?",
            (folder_id, project_id),
        ):
            raise ValueError("项目文件夹不存在")
        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO assets(project_id, folder_id, title, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (project_id, folder_id, title.strip() or file["name"], user_id, now, now),
            )
            asset_id = int(cursor.lastrowid)
            version = connection.execute(
                """INSERT INTO asset_versions(
                   asset_id, file_id, version_number, label, file_name, mime_type, kind, size,
                   duration, width, height, created_by, created_at)
                   VALUES (?, ?, 1, '初始版本', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (asset_id, *self._version_snapshot(file), user_id, now),
            )
            connection.execute(
                "UPDATE assets SET cover_version_id = ? WHERE id = ?",
                (int(version.lastrowid), asset_id),
            )
        return self.asset_detail(asset_id) or {}

    def add_version(
        self,
        asset_id: int,
        file: dict[str, Any],
        label: str,
        notes: str,
        user_id: int | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.transaction() as connection:
            asset = connection.execute("SELECT project_id FROM assets WHERE id = ?", (asset_id,)).fetchone()
            if not asset:
                raise ValueError("素材不存在")
            current = connection.execute(
                "SELECT COALESCE(MAX(version_number), 0) AS number FROM asset_versions WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            version_number = int(current["number"] or 0) + 1
            cursor = connection.execute(
                """INSERT INTO asset_versions(
                   asset_id, file_id, version_number, label, notes, file_name, mime_type, kind, size,
                   duration, width, height, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    asset_id,
                    *self._version_snapshot(file)[:1],
                    version_number,
                    label.strip() or f"版本 {version_number}",
                    notes.strip(),
                    *self._version_snapshot(file)[1:],
                    user_id,
                    now,
                ),
            )
            version_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE assets SET cover_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, now, asset_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (now, int(asset["project_id"])),
            )
        return self.version(version_id) or {}

    def version(self, version_id: int) -> dict[str, Any] | None:
        version = self.database.fetchone(
            """SELECT av.*, f.library_id, f.relative_path, f.status AS index_status, f.metadata_json,
               COALESCE(NULLIF(f.manual_caption, ''), f.ai_caption, '') AS caption
               FROM asset_versions av LEFT JOIN files f ON f.id = av.file_id WHERE av.id = ?""",
            (version_id,),
        )
        if not version:
            return None
        metadata = self._loads(version.pop("metadata_json", "{}"), {})
        version["frame_rate"] = float(metadata.get("frame_rate") or 0) or None
        return version

    def list_assets(
        self,
        project_id: int,
        folder_id: int | None = None,
        status: str = "",
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        clauses = ["a.project_id = ?"]
        params: list[Any] = [project_id]
        if folder_id is not None:
            clauses.append("a.folder_id = ?")
            params.append(folder_id)
        if status:
            clauses.append("a.status = ?")
            params.append(status)
        if query.strip():
            clauses.append(
                """(a.title LIKE ? ESCAPE '\\' OR a.description LIKE ? ESCAPE '\\'
                   OR av.file_name LIKE ? ESCAPE '\\' OR COALESCE(f.ai_caption, '') LIKE ? ESCAPE '\\')"""
            )
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%"] * 4)
        where = " AND ".join(clauses)
        total = self.database.fetchone(
            f"""SELECT COUNT(DISTINCT a.id) AS count FROM assets a
                LEFT JOIN asset_versions av ON av.id = COALESCE(
                  a.cover_version_id, (SELECT id FROM asset_versions WHERE asset_id = a.id ORDER BY version_number DESC LIMIT 1))
                LEFT JOIN files f ON f.id = av.file_id WHERE {where}""",
            params,
        ) or {"count": 0}
        rows = self.database.fetchall(
            f"""SELECT a.*, av.id AS version_id, av.version_number, av.file_id, av.file_name,
                av.mime_type, av.kind, av.size, av.duration, av.width, av.height, av.proxy_status,
                f.library_id, f.relative_path, f.status AS index_status,
                COALESCE(NULLIF(f.manual_caption, ''), f.ai_caption, '') AS caption,
                u.display_name AS assignee_name,
                (SELECT COUNT(*) FROM asset_versions allv WHERE allv.asset_id = a.id) AS version_count,
                (SELECT COUNT(*) FROM review_comments rc WHERE rc.asset_id = a.id AND rc.resolved = 0) AS open_comment_count
                FROM assets a
                LEFT JOIN asset_versions av ON av.id = COALESCE(
                  a.cover_version_id, (SELECT id FROM asset_versions WHERE asset_id = a.id ORDER BY version_number DESC LIMIT 1))
                LEFT JOIN files f ON f.id = av.file_id
                LEFT JOIN users u ON u.id = a.assignee_id
                WHERE {where} ORDER BY a.updated_at DESC, a.id DESC LIMIT ? OFFSET ?""",
            [*params, max(1, min(200, limit)), max(0, offset)],
        )
        return {"total": int(total["count"]), "items": rows}

    def asset_detail(self, asset_id: int) -> dict[str, Any] | None:
        asset = self.database.fetchone(
            """SELECT a.*, p.name AS project_name, u.display_name AS assignee_name,
               creator.display_name AS creator_name
               FROM assets a JOIN projects p ON p.id = a.project_id
               LEFT JOIN users u ON u.id = a.assignee_id
               LEFT JOIN users creator ON creator.id = a.created_by WHERE a.id = ?""",
            (asset_id,),
        )
        if not asset:
            return None
        versions = self.database.fetchall(
            """SELECT av.*, f.library_id, f.relative_path, f.status AS index_status,
               f.metadata_json,
               COALESCE(NULLIF(f.manual_caption, ''), f.ai_caption, '') AS caption
               FROM asset_versions av LEFT JOIN files f ON f.id = av.file_id
               WHERE av.asset_id = ? ORDER BY av.version_number DESC""",
            (asset_id,),
        )
        for version in versions:
            metadata = self._loads(version.pop("metadata_json", "{}"), {})
            version["frame_rate"] = float(metadata.get("frame_rate") or 0) or None
        comments = self.database.fetchall(
            """SELECT rc.*, u.display_name, u.username FROM review_comments rc
               LEFT JOIN users u ON u.id = rc.user_id WHERE rc.asset_id = ?
               ORDER BY rc.created_at, rc.id""",
            (asset_id,),
        )
        self._with_attachments(comments)
        for comment in comments:
            comment["drawing"] = self._loads(comment.pop("drawing_json", "[]"), [])
        asset["versions"] = versions
        asset["comments"] = comments
        return asset

    def _with_attachments(self, comments: list[dict[str, Any]]) -> None:
        comment_ids = [int(comment["id"]) for comment in comments]
        if not comment_ids:
            return
        placeholders = ",".join("?" for _ in comment_ids)
        rows = self.database.fetchall(
            f"""SELECT comment_id, name, original_name, mime, size_bytes, created_at
                FROM comment_attachments WHERE comment_id IN ({placeholders})
                ORDER BY created_at, name""",
            comment_ids,
        )
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(int(row["comment_id"]), []).append(row)
        for comment in comments:
            comment["attachments"] = grouped.get(int(comment["id"]), [])

    def add_comment_attachment(
        self,
        comment_id: int,
        name: str,
        original_name: str,
        mime: str,
        size_bytes: int,
    ) -> dict[str, Any]:
        row = self.database.fetchone(
            "SELECT COUNT(*) AS count FROM comment_attachments WHERE comment_id = ?",
            (comment_id,),
        )
        if row and int(row["count"]) >= settings.comment_attachment_max_per_comment:
            raise ValueError(f"单条评论最多允许 {settings.comment_attachment_max_per_comment} 个附件")
        self.database.execute(
            """INSERT INTO comment_attachments(comment_id, name, original_name, mime, size_bytes, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (comment_id, name, original_name[:240], mime, int(size_bytes), utc_now()),
        )
        return {
            "comment_id": comment_id,
            "name": name,
            "original_name": original_name,
            "mime": mime,
            "size_bytes": int(size_bytes),
        }

    def comment_attachment_names(self, comment_id: int) -> list[str]:
        rows = self.database.fetchall(
            "SELECT name FROM comment_attachments WHERE comment_id = ?",
            (comment_id,),
        )
        return [str(row["name"]) for row in rows]

    def project_attachment_names(self, project_id: int) -> list[str]:
        rows = self.database.fetchall(
            """SELECT ca.name FROM comment_attachments ca
               JOIN review_comments rc ON rc.id = ca.comment_id
               JOIN assets a ON a.id = rc.asset_id WHERE a.project_id = ?""",
            (project_id,),
        )
        return [str(row["name"]) for row in rows]

    def delete_comment(self, comment_id: int) -> list[str]:
        names = self.comment_attachment_names(comment_id)
        self.database.execute("DELETE FROM review_comments WHERE id = ?", (comment_id,))
        return names

    def update_asset(self, asset_id: int, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "title": str(values.get("title") or "").strip()[:240],
            "description": str(values.get("description") or "").strip()[:4000],
            "status": str(values.get("status") or "draft")[:40],
            "rating": max(0, min(5, int(values.get("rating") or 0))),
            "folder_id": int(values["folder_id"]) if values.get("folder_id") is not None else None,
            "assignee_id": int(values["assignee_id"]) if values.get("assignee_id") is not None else None,
        }
        asset = self.database.fetchone("SELECT project_id FROM assets WHERE id = ?", (asset_id,))
        if not asset:
            raise ValueError("素材不存在")
        if allowed["folder_id"] is not None and not self.database.fetchone(
            "SELECT id FROM project_folders WHERE id = ? AND project_id = ?",
            (allowed["folder_id"], asset["project_id"]),
        ):
            raise ValueError("项目文件夹不存在")
        if not self.database.fetchone(
            "SELECT id FROM project_statuses WHERE project_id = ? AND key = ?",
            (asset["project_id"], allowed["status"]),
        ):
            raise ValueError("素材状态不存在")
        self.database.execute(
            """UPDATE assets SET title = ?, description = ?, status = ?, rating = ?,
               folder_id = ?, assignee_id = ?, updated_at = ? WHERE id = ?""",
            (
                allowed["title"],
                allowed["description"],
                allowed["status"],
                allowed["rating"],
                allowed["folder_id"],
                allowed["assignee_id"],
                utc_now(),
                asset_id,
            ),
        )
        return self.asset_detail(asset_id) or {}

    def delete_asset(self, asset_id: int) -> None:
        self.database.execute("DELETE FROM assets WHERE id = ?", (asset_id,))

    def create_review_session(
        self,
        project_id: int,
        name: str,
        user_id: int | None,
    ) -> dict[str, Any]:
        session_id = self.database.execute(
            """INSERT INTO review_sessions(project_id, name, created_by, created_at)
               VALUES (?, ?, ?, ?)""",
            (project_id, name.strip(), user_id, utc_now()),
        )
        return self.database.fetchone("SELECT * FROM review_sessions WHERE id = ?", (session_id,)) or {}

    def list_review_sessions(self, project_id: int) -> list[dict[str, Any]]:
        return self.database.fetchall(
            """SELECT rs.*, u.display_name AS creator_name,
               (SELECT COUNT(*) FROM review_comments rc WHERE rc.review_session_id = rs.id) AS comment_count
               FROM review_sessions rs LEFT JOIN users u ON u.id = rs.created_by
               WHERE rs.project_id = ? ORDER BY rs.id DESC""",
            (project_id,),
        )

    def close_review_session(self, session_id: int) -> None:
        self.database.execute(
            "UPDATE review_sessions SET status = 'closed', closed_at = ? WHERE id = ?",
            (utc_now(), session_id),
        )

    def add_comment(
        self,
        asset_id: int,
        version_id: int | None,
        body: str,
        comment_type: str,
        time_start: float | None,
        time_end: float | None,
        x: float | None,
        y: float | None,
        drawing: list[dict[str, Any]],
        visibility: str,
        user_id: int | None,
        guest_name: str = "",
        review_session_id: int | None = None,
    ) -> dict[str, Any]:
        asset = self.database.fetchone("SELECT project_id, title FROM assets WHERE id = ?", (asset_id,))
        if not asset:
            raise ValueError("素材不存在")
        if version_id is not None and not self.database.fetchone(
            "SELECT id FROM asset_versions WHERE id = ? AND asset_id = ?",
            (version_id, asset_id),
        ):
            raise ValueError("素材版本不存在")
        if review_session_id is not None and not self.database.fetchone(
            "SELECT id FROM review_sessions WHERE id = ? AND project_id = ?",
            (review_session_id, asset["project_id"]),
        ):
            raise ValueError("审阅会话不存在")
        now = utc_now()
        comment_id = self.database.execute(
            """INSERT INTO review_comments(
               asset_id, version_id, review_session_id, user_id, guest_name, body, comment_type,
               time_start, time_end, x, y, drawing_json, visibility, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                asset_id,
                version_id,
                review_session_id,
                user_id,
                guest_name.strip()[:80],
                body.strip(),
                comment_type,
                time_start,
                time_end,
                x,
                y,
                json.dumps(drawing[:200], ensure_ascii=False),
                visibility,
                now,
                now,
            ),
        )
        self.database.execute("UPDATE assets SET updated_at = ? WHERE id = ?", (now, asset_id))
        self.database.execute(
            "UPDATE projects SET updated_at = ? WHERE id = ?",
            (now, asset["project_id"]),
        )
        if user_id is not None:
            members = self.database.fetchall(
                "SELECT user_id FROM project_members WHERE project_id = ? AND user_id != ?",
                (asset["project_id"], user_id),
            )
            actor = self.database.fetchone("SELECT display_name FROM users WHERE id = ?", (user_id,))
            actor_name = actor["display_name"] if actor else "成员"
        else:
            members = self.database.fetchall(
                "SELECT user_id FROM project_members WHERE project_id = ?",
                (asset["project_id"],),
            )
            actor_name = guest_name.strip()[:80] or "外部访客"
        title = f"{actor_name} 评论了 {asset['title']}"
        with self.database.transaction() as connection:
            connection.executemany(
                """INSERT INTO notifications(user_id, type, title, body, target_type, target_id, created_at)
                   VALUES (?, 'review.comment', ?, ?, 'asset', ?, ?)""",
                [
                    (int(member["user_id"]), title, body.strip()[:240], str(asset_id), now)
                    for member in members
                ],
            )
        return self.comment(comment_id) or {}

    def comment(self, comment_id: int) -> dict[str, Any] | None:
        row = self.database.fetchone(
            """SELECT rc.*, u.display_name, u.username FROM review_comments rc
               LEFT JOIN users u ON u.id = rc.user_id WHERE rc.id = ?""",
            (comment_id,),
        )
        if row:
            self._with_attachments([row])
            row["drawing"] = self._loads(row.pop("drawing_json", "[]"), [])
        return row

    def resolve_comment(self, comment_id: int, user_id: int | None, resolved: bool) -> dict[str, Any]:
        self.database.execute(
            """UPDATE review_comments SET resolved = ?, resolved_by = ?, resolved_at = ?, updated_at = ?
               WHERE id = ?""",
            (
                int(resolved),
                user_id if resolved else None,
                utc_now() if resolved else None,
                utc_now(),
                comment_id,
            ),
        )
        return self.comment(comment_id) or {}

    def create_share(
        self,
        project_id: int,
        asset_id: int | None,
        name: str,
        access_code_hash: str,
        expires_at: str | None,
        can_download: bool,
        can_comment: bool,
        can_view_versions: bool,
        watermark_text: str,
        brand_name: str,
        user_id: int | None,
    ) -> tuple[dict[str, Any], str]:
        if asset_id is not None and not self.database.fetchone(
            "SELECT id FROM assets WHERE id = ? AND project_id = ?",
            (asset_id, project_id),
        ):
            raise ValueError("分享素材不属于该项目")
        token = secrets.token_urlsafe(32)
        share_id = self.database.execute(
            """INSERT INTO share_links(
               project_id, asset_id, token_hash, name, access_code_hash, expires_at,
               can_download, can_comment, can_view_versions, watermark_text, brand_name,
               created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                asset_id,
                token_digest(token),
                name.strip(),
                access_code_hash,
                expires_at,
                int(can_download),
                int(can_comment),
                int(can_view_versions),
                watermark_text.strip()[:100],
                brand_name.strip()[:100] or "NAS AI Space",
                user_id,
                utc_now(),
            ),
        )
        return self.share(share_id) or {}, token

    def share(self, share_id: int) -> dict[str, Any] | None:
        row = self.database.fetchone(
            """SELECT sl.id, sl.project_id, sl.asset_id, sl.name, sl.expires_at,
               sl.can_download, sl.can_comment, sl.can_view_versions, sl.watermark_text,
               sl.brand_name, sl.enabled, sl.created_by, sl.created_at, sl.last_access_at,
               p.name AS project_name, a.title AS asset_title,
               CASE WHEN sl.access_code_hash != '' THEN 1 ELSE 0 END AS access_code_required
               FROM share_links sl JOIN projects p ON p.id = sl.project_id
               LEFT JOIN assets a ON a.id = sl.asset_id WHERE sl.id = ?""",
            (share_id,),
        )
        return row

    def list_shares(self, project_id: int) -> list[dict[str, Any]]:
        return self.database.fetchall(
            """SELECT sl.id, sl.project_id, sl.asset_id, sl.name, sl.expires_at,
               sl.can_download, sl.can_comment, sl.can_view_versions, sl.watermark_text,
               sl.brand_name, sl.enabled, sl.created_by, sl.created_at, sl.last_access_at,
               a.title AS asset_title,
               CASE WHEN sl.access_code_hash != '' THEN 1 ELSE 0 END AS access_code_required
               FROM share_links sl LEFT JOIN assets a ON a.id = sl.asset_id
               WHERE sl.project_id = ? ORDER BY sl.id DESC""",
            (project_id,),
        )

    def share_by_token(self, token: str) -> dict[str, Any] | None:
        row = self.database.fetchone(
            """SELECT sl.*, p.name AS project_name, p.description AS project_description,
               a.title AS asset_title FROM share_links sl JOIN projects p ON p.id = sl.project_id
               LEFT JOIN assets a ON a.id = sl.asset_id
               WHERE sl.token_hash = ? AND sl.enabled = 1""",
            (token_digest(token),),
        )
        if not row:
            return None
        if row.get("expires_at"):
            try:
                expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
                if expires.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                    return None
            except ValueError:
                return None
        self.database.execute(
            "UPDATE share_links SET last_access_at = ? WHERE id = ?",
            (utc_now(), row["id"]),
        )
        return row

    def set_share_enabled(self, share_id: int, enabled: bool) -> None:
        self.database.execute(
            "UPDATE share_links SET enabled = ? WHERE id = ?",
            (int(enabled), share_id),
        )

    def list_notifications(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self.database.fetchall(
            "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, max(1, min(500, limit))),
        )

    def unread_notifications(self, user_id: int) -> int:
        row = self.database.fetchone(
            "SELECT COUNT(*) AS count FROM notifications WHERE user_id = ? AND read_at IS NULL",
            (user_id,),
        )
        return int(row["count"]) if row else 0

    def read_notifications(self, user_id: int, notification_id: int | None = None) -> None:
        if notification_id is None:
            self.database.execute(
                "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
                (utc_now(), user_id),
            )
        else:
            self.database.execute(
                "UPDATE notifications SET read_at = ? WHERE id = ? AND user_id = ?",
                (utc_now(), notification_id, user_id),
            )
