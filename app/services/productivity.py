from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import Database, utc_now
from app.services.local_ai import LocalAIClient


ARTIFACT_TYPES = {"report", "summary", "brief", "minutes", "script", "checklist"}
AGENT_ACTIONS = {"tag", "copy_to_output", "add_to_space", "generate_artifact"}
TRIGGER_TYPES = {"manual", "file_arrived", "schedule"}
CONDITION_FIELDS = {"kind", "extension", "library_id", "name_contains", "min_size", "max_size"}
EDIT_PROJECT_ROLES = {"owner", "manager", "editor"}


class ProductivityService:
    def __init__(self, database: Database, settings: Settings, ai: LocalAIClient):
        self.database = database
        self.settings = settings
        self.output_root = (settings.upload_root / "AI 工作成果").resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.ai = ai
        self._artifact_locks: dict[int, threading.Lock] = {}
        self._artifact_locks_guard = threading.Lock()

    @staticmethod
    def _loads(value: str | None, default: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, json.JSONDecodeError):
            return default

    @staticmethod
    def _slug(value: str, fallback: str = "output") -> str:
        cleaned = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", value, flags=re.UNICODE).strip("-_")
        return cleaned[:80] or fallback

    @staticmethod
    def _project_access(database: Database, project_id: int, user_id: int, admin: bool) -> bool:
        if admin:
            return bool(database.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,)))
        return bool(database.fetchone(
            "SELECT 1 AS found FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        ))

    def space_access(self, space_id: int, user_id: int, admin: bool = False) -> dict[str, Any] | None:
        space = self.database.fetchone("SELECT * FROM knowledge_spaces WHERE id = ?", (space_id,))
        if not space:
            return None
        if admin or int(space["owner_id"]) == user_id:
            return space
        project_id = space.get("project_id")
        if project_id and self._project_access(self.database, int(project_id), user_id, admin):
            return space
        return None

    def list_spaces(self, user_id: int, admin: bool = False) -> list[dict[str, Any]]:
        if admin:
            rows = self.database.fetchall("SELECT * FROM knowledge_spaces ORDER BY updated_at DESC")
        else:
            rows = self.database.fetchall(
                """SELECT DISTINCT s.* FROM knowledge_spaces s
                   LEFT JOIN project_members pm ON pm.project_id = s.project_id AND pm.user_id = ?
                   WHERE s.owner_id = ? OR pm.user_id IS NOT NULL ORDER BY s.updated_at DESC""",
                (user_id, user_id),
            )
        for row in rows:
            count = self.database.fetchone(
                "SELECT COUNT(*) AS count FROM knowledge_space_files WHERE space_id = ?", (row["id"],)
            )
            row["file_count"] = int((count or {}).get("count") or 0)
        return rows

    def create_space(
        self,
        name: str,
        description: str,
        user_id: int,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("知识空间名称不能为空")
        now = utc_now()
        space_id = self.database.execute(
            """INSERT INTO knowledge_spaces(name, description, project_id, owner_id, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (clean_name, description.strip(), project_id, user_id, user_id, now, now),
        )
        return self.space_access(space_id, user_id) or {}

    def update_space(self, space_id: int, name: str, description: str) -> None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("知识空间名称不能为空")
        self.database.execute(
            "UPDATE knowledge_spaces SET name = ?, description = ?, updated_at = ? WHERE id = ?",
            (clean_name, description.strip(), utc_now(), space_id),
        )

    def add_space_files(self, space_id: int, file_ids: list[int], user_id: int) -> int:
        now = utc_now()
        added = 0
        with self.database.transaction() as connection:
            for file_id in dict.fromkeys(file_ids):
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO knowledge_space_files(space_id, file_id, added_by, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (space_id, file_id, user_id, now),
                )
                added += max(0, int(cursor.rowcount or 0))
            connection.execute("UPDATE knowledge_spaces SET updated_at = ? WHERE id = ?", (now, space_id))
        return added

    def remove_space_file(self, space_id: int, file_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM knowledge_space_files WHERE space_id = ? AND file_id = ?", (space_id, file_id)
            )
            connection.execute(
                "UPDATE knowledge_spaces SET updated_at = ? WHERE id = ?", (utc_now(), space_id)
            )

    def space_files(
        self,
        space_id: int,
        user_id: int | None = None,
        admin: bool = False,
    ) -> list[dict[str, Any]]:
        rows = self.database.fetchall(
            """SELECT f.id, f.name, f.path, f.relative_path, f.kind, f.mime_type, f.size,
                      f.status, f.ai_caption, f.manual_caption, f.library_id, f.updated_at
               FROM knowledge_space_files sf JOIN files f ON f.id = sf.file_id
               WHERE sf.space_id = ? ORDER BY sf.created_at DESC""",
            (space_id,),
        )
        if user_id is None or admin:
            return rows
        user = self.database.get_user(user_id)
        if not user or not user.get("enabled"):
            return []
        if user.get("role") in {"owner", "admin"}:
            return rows
        allowed = {int(value) for value in user.get("library_ids", [])}
        return [row for row in rows if int(row["library_id"]) in allowed]

    def scope_file_ids(self, space_id: int | None = None, project_id: int | None = None) -> list[int] | None:
        if space_id is not None:
            return [
                int(row["file_id"])
                for row in self.database.fetchall(
                    "SELECT file_id FROM knowledge_space_files WHERE space_id = ?", (space_id,)
                )
            ]
        if project_id is not None:
            return [
                int(row["file_id"])
                for row in self.database.fetchall(
                    """SELECT DISTINCT v.file_id FROM assets a
                       JOIN asset_versions v ON v.asset_id = a.id
                       WHERE a.project_id = ? AND v.file_id IS NOT NULL""",
                    (project_id,),
                )
            ]
        return None

    def _artifact_access(self, artifact_id: int, user_id: int, admin: bool = False) -> dict[str, Any] | None:
        artifact = self.database.fetchone("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if not artifact:
            return None
        if admin or int(artifact["created_by"]) == user_id:
            return artifact
        if artifact.get("project_id") and self._project_access(
            self.database, int(artifact["project_id"]), user_id, admin
        ):
            return artifact
        if artifact.get("space_id") and self.space_access(int(artifact["space_id"]), user_id, admin):
            return artifact
        return None

    def list_artifacts(self, user_id: int, admin: bool = False) -> list[dict[str, Any]]:
        if admin:
            rows = self.database.fetchall("SELECT * FROM artifacts ORDER BY updated_at DESC")
        else:
            rows = self.database.fetchall(
                """SELECT DISTINCT a.* FROM artifacts a
                   LEFT JOIN project_members pm ON pm.project_id = a.project_id AND pm.user_id = ?
                   LEFT JOIN knowledge_spaces s ON s.id = a.space_id
                   LEFT JOIN project_members spm ON spm.project_id = s.project_id AND spm.user_id = ?
                   WHERE a.created_by = ? OR pm.user_id IS NOT NULL OR s.owner_id = ? OR spm.user_id IS NOT NULL
                   ORDER BY a.updated_at DESC""",
                (user_id, user_id, user_id, user_id),
            )
        return [self._with_latest_version(row) for row in rows]

    def artifact(self, artifact_id: int, user_id: int, admin: bool = False) -> dict[str, Any] | None:
        artifact = self._artifact_access(artifact_id, user_id, admin)
        if not artifact:
            return None
        artifact["versions"] = self.database.fetchall(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? ORDER BY version_number DESC",
            (artifact_id,),
        )
        for version in artifact["versions"]:
            version["sources"] = self._loads(version.pop("sources_json", "[]"), [])
        return artifact

    def _with_latest_version(self, artifact: dict[str, Any]) -> dict[str, Any]:
        version = self.database.fetchone(
            """SELECT id, version_number, format, created_at, sources_json
               FROM artifact_versions WHERE artifact_id = ? ORDER BY version_number DESC LIMIT 1""",
            (artifact["id"],),
        )
        artifact["latest_version"] = version
        if version:
            version["source_count"] = len(self._loads(version.pop("sources_json", "[]"), []))
        return artifact

    def create_artifact(
        self,
        title: str,
        artifact_type: str,
        user_id: int,
        project_id: int | None = None,
        space_id: int | None = None,
    ) -> dict[str, Any]:
        if artifact_type not in ARTIFACT_TYPES:
            raise ValueError("不支持的成果类型")
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("成果标题不能为空")
        now = utc_now()
        artifact_id = self.database.execute(
            """INSERT INTO artifacts(title, artifact_type, status, project_id, space_id, created_by, created_at, updated_at)
               VALUES (?, ?, 'processing', ?, ?, ?, ?, ?)""",
            (clean_title, artifact_type, project_id, space_id, user_id, now, now),
        )
        return self._artifact_access(artifact_id, user_id) or {}

    def _sources(self, file_ids: list[int]) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for file_id in dict.fromkeys(file_ids[:12]):
            file = self.database.get_file(int(file_id))
            if not file:
                continue
            chunks = self.database.fetchall(
                """SELECT content, source_label, start_time, end_time FROM content_chunks
                   WHERE file_id = ? ORDER BY chunk_index LIMIT 8""",
                (file_id,),
            )
            text = "\n".join(str(chunk.get("content") or "") for chunk in chunks).strip()
            if not text:
                text = str(file.get("manual_caption") or file.get("ai_caption") or file.get("extracted_text") or "")
            sources.append({
                "id": int(file_id),
                "name": file["name"],
                "path": file["relative_path"],
                "kind": file["kind"],
                "evidence": text[:6000],
                "source_labels": [chunk.get("source_label") for chunk in chunks if chunk.get("source_label")],
            })
        return sources

    def generate_artifact_version(
        self,
        artifact_id: int,
        prompt: str,
        file_ids: list[int],
        user_id: int,
    ) -> dict[str, Any]:
        with self._artifact_locks_guard:
            lock = self._artifact_locks.setdefault(artifact_id, threading.Lock())
        with lock:
            artifact = self._artifact_access(artifact_id, user_id)
            if not artifact:
                raise ValueError("成果不存在或无权访问")
            sources = self._sources(file_ids)
            if not sources:
                raise ValueError("没有可用于生成成果的资料")
            content = self.ai.generate_artifact(
                prompt.strip(), artifact["artifact_type"], artifact["title"], sources
            )
            previous = self.database.fetchone(
                "SELECT MAX(version_number) AS number FROM artifact_versions WHERE artifact_id = ?", (artifact_id,)
            )
            version_number = int((previous or {}).get("number") or 0) + 1
            directory = self.output_root / f"artifact-{artifact_id}"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"v{version_number:03d}-{self._slug(artifact['title'])}.md"
            temporary = path.with_suffix(".md.tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
            now = utc_now()
            try:
                with self.database.transaction() as connection:
                    cursor = connection.execute(
                        """INSERT INTO artifact_versions(
                               artifact_id, version_number, prompt, content, format, sources_json,
                               file_path, created_by, created_at
                           ) VALUES (?, ?, ?, ?, 'markdown', ?, ?, ?, ?)""",
                        (
                            artifact_id, version_number, prompt.strip(), content,
                            json.dumps(sources, ensure_ascii=False), str(path), user_id, now,
                        ),
                    )
                    version_id = int(cursor.lastrowid)
                    connection.execute(
                        "UPDATE artifacts SET status = 'ready', updated_at = ? WHERE id = ?", (now, artifact_id)
                    )
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return self.database.fetchone("SELECT * FROM artifact_versions WHERE id = ?", (version_id,)) or {}

    def fail_artifact(self, artifact_id: int) -> None:
        self.database.execute(
            "UPDATE artifacts SET status = 'error', updated_at = ? WHERE id = ?", (utc_now(), artifact_id)
        )

    def artifact_file(self, artifact_id: int, version_id: int, user_id: int, admin: bool = False) -> Path | None:
        if not self._artifact_access(artifact_id, user_id, admin):
            return None
        version = self.database.fetchone(
            "SELECT file_path FROM artifact_versions WHERE id = ? AND artifact_id = ?", (version_id, artifact_id)
        )
        if not version:
            return None
        path = Path(version["file_path"]).resolve()
        if not path.is_relative_to(self.output_root) or not path.is_file():
            return None
        return path

    def delete_artifact(self, artifact_id: int, user_id: int, admin: bool = False) -> bool:
        artifact = self.artifact(artifact_id, user_id, admin)
        if not artifact:
            return False
        for version in artifact["versions"]:
            path = Path(str(version.get("file_path") or "")).resolve()
            if path.is_relative_to(self.output_root) and path.is_file():
                path.unlink()
        directory = (self.output_root / f"artifact-{artifact_id}").resolve()
        if directory.is_relative_to(self.output_root) and directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass
        self.database.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
        return True

    @staticmethod
    def _safe_target(value: str) -> str:
        target = Path(value.strip().replace("\\", "/"))
        if target.is_absolute() or not value.strip() or any(part in {"", ".", ".."} for part in target.parts):
            raise ValueError("目标目录必须是成果空间内的相对路径")
        return str(target)

    def _normalize_action(self, action: dict[str, Any], default_file_ids: list[int]) -> dict[str, Any]:
        action_type = str(action.get("type") or "").strip()
        if action_type not in AGENT_ACTIONS:
            raise ValueError(f"不支持的 Agent 动作：{action_type or '空'}")
        file_ids = [int(value) for value in action.get("file_ids") or default_file_ids]
        if len(file_ids) > 500:
            raise ValueError("单次 Agent 任务最多处理 500 个文件")
        normalized: dict[str, Any] = {"type": action_type, "file_ids": list(dict.fromkeys(file_ids))}
        if action_type == "tag":
            tags = [str(value).strip()[:80] for value in action.get("tags") or [] if str(value).strip()]
            if not tags or len(tags) > 20:
                raise ValueError("标签动作需要 1-20 个标签")
            normalized["tags"] = list(dict.fromkeys(tags))
        elif action_type == "copy_to_output":
            normalized["target_folder"] = self._safe_target(str(action.get("target_folder") or ""))
        elif action_type == "add_to_space":
            normalized["space_id"] = int(action.get("space_id") or 0)
            if normalized["space_id"] <= 0:
                raise ValueError("加入知识空间动作缺少 space_id")
        elif action_type == "generate_artifact":
            if len(normalized["file_ids"]) > 12:
                raise ValueError("成果生成每次最多使用 12 份资料")
            normalized.update({
                "title": str(action.get("title") or "AI 工作成果").strip()[:160] or "AI 工作成果",
                "artifact_type": str(action.get("artifact_type") or "report"),
                "prompt": (
                    str(action.get("prompt") or "根据所选资料生成一份结构清晰的报告").strip()[:4000]
                    or "根据所选资料生成一份结构清晰的报告"
                ),
                "space_id": int(action["space_id"]) if action.get("space_id") else None,
                "project_id": int(action["project_id"]) if action.get("project_id") else None,
            })
            if normalized["artifact_type"] not in ARTIFACT_TYPES:
                raise ValueError("不支持的成果类型")
        return normalized

    def create_agent_run(
        self,
        user_id: int,
        prompt: str,
        actions: list[dict[str, Any]],
        file_ids: list[int],
        project_id: int | None = None,
        space_id: int | None = None,
    ) -> dict[str, Any]:
        if not actions or len(actions) > 20:
            raise ValueError("Agent 计划需要 1-20 个动作")
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("Agent 任务说明不能为空")
        plan = [self._normalize_action(action, file_ids) for action in actions]
        risk = "medium" if any(action["type"] == "copy_to_output" for action in plan) else "low"
        now = utc_now()
        run_id = self.database.execute(
            """INSERT INTO agent_runs(
                   user_id, project_id, space_id, prompt, status, risk_level, plan_json, created_at
               ) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?)""",
            (user_id, project_id, space_id, clean_prompt, risk, json.dumps(plan, ensure_ascii=False), now),
        )
        with self.database.transaction() as connection:
            for sequence, action in enumerate(plan, 1):
                connection.execute(
                    """INSERT INTO agent_actions(run_id, sequence, action_type, input_json, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (run_id, sequence, action["type"], json.dumps(action, ensure_ascii=False), now),
                )
        return self.agent_run(run_id, user_id) or {}

    def agent_run(self, run_id: int, user_id: int, admin: bool = False) -> dict[str, Any] | None:
        run = self.database.fetchone("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
        if not run or (not admin and int(run["user_id"]) != user_id):
            return None
        run["plan"] = self._loads(run.pop("plan_json", "[]"), [])
        run["undo"] = self._loads(run.pop("undo_json", "[]"), [])
        run["actions"] = self.database.fetchall(
            "SELECT * FROM agent_actions WHERE run_id = ? ORDER BY sequence", (run_id,)
        )
        for action in run["actions"]:
            action["input"] = self._loads(action.pop("input_json", "{}"), {})
            action["result"] = self._loads(action.pop("result_json", "{}"), {})
        run["confirmation"] = f"执行任务 {run_id}"
        return run

    def list_agent_runs(self, user_id: int, admin: bool = False) -> list[dict[str, Any]]:
        if admin:
            rows = self.database.fetchall("SELECT * FROM agent_runs ORDER BY id DESC LIMIT 200")
        else:
            rows = self.database.fetchall(
                "SELECT * FROM agent_runs WHERE user_id = ? ORDER BY id DESC LIMIT 200", (user_id,)
            )
        for row in rows:
            row["plan"] = self._loads(row.pop("plan_json", "[]"), [])
            row.pop("undo_json", None)
        return rows

    def approve_agent_run(self, run_id: int, user_id: int, confirmation: str) -> None:
        run = self.database.fetchone("SELECT * FROM agent_runs WHERE id = ? AND user_id = ?", (run_id, user_id))
        if not run:
            raise ValueError("Agent 任务不存在")
        if run["status"] != "draft":
            raise ValueError("只有待确认计划可以执行")
        if confirmation.strip() != f"执行任务 {run_id}":
            raise ValueError("确认文本不匹配")
        self.database.execute(
            "UPDATE agent_runs SET status = 'approved', approved_at = ? WHERE id = ?", (utc_now(), run_id)
        )

    def reset_agent_approval(self, run_id: int, user_id: int) -> None:
        self.database.execute(
            """UPDATE agent_runs SET status = 'draft', approved_at = NULL
               WHERE id = ? AND user_id = ? AND status = 'approved'""",
            (run_id, user_id),
        )

    def _project_edit_access(self, project_id: int, user_id: int, admin: bool) -> bool:
        if admin:
            return bool(self.database.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,)))
        row = self.database.fetchone(
            "SELECT role FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        )
        return bool(row and row["role"] in EDIT_PROJECT_ROLES)

    def _ensure_execution_access(self, user_id: int, run: dict[str, Any], plan: list[dict[str, Any]]) -> None:
        user = self.database.get_user(user_id)
        if not user or not user.get("enabled"):
            raise PermissionError("任务所属用户已停用")
        admin = user.get("role") in {"owner", "admin"}
        project_ids = {
            int(value) for value in [run.get("project_id"), *(action.get("project_id") for action in plan)]
            if value
        }
        space_ids = {
            int(value) for value in [run.get("space_id"), *(action.get("space_id") for action in plan)]
            if value
        }
        for project_id in project_ids:
            if not self._project_edit_access(project_id, user_id, admin):
                raise PermissionError("项目权限已变化，任务已停止")
        for space_id in space_ids:
            space = self.space_access(space_id, user_id, admin)
            if not space:
                raise PermissionError("知识空间权限已变化，任务已停止")
            if int(space["owner_id"]) != user_id and not admin:
                project_id = int(space.get("project_id") or 0)
                if not project_id or not self._project_edit_access(project_id, user_id, admin):
                    raise PermissionError("知识空间编辑权限已变化，任务已停止")
        if admin:
            return
        allowed_libraries = {int(value) for value in user.get("library_ids", [])}
        file_ids = {
            int(file_id) for action in plan for file_id in action.get("file_ids", [])
        }
        for file_id in file_ids:
            file = self.database.get_file(file_id)
            if not file or int(file["library_id"]) not in allowed_libraries:
                raise PermissionError("资料访问权限已变化，任务已停止")

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _unique_destination(self, directory: Path, name: str) -> Path:
        candidate = directory / name
        counter = 2
        while candidate.exists():
            candidate = directory / f"{Path(name).stem}-{counter}{Path(name).suffix}"
            counter += 1
        return candidate

    def _execute_action(self, action: dict[str, Any], user_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        action_type = action["type"]
        if action_type == "tag":
            previous: dict[str, list[str]] = {}
            applied: dict[str, list[str]] = {}
            for file_id in action["file_ids"]:
                previous[str(file_id)] = self.database.file_tag_names(user_id, file_id)
                applied[str(file_id)] = sorted(
                    dict.fromkeys([*previous[str(file_id)], *action["tags"]]), key=str.casefold
                )
            completed: list[int] = []
            try:
                for file_id in action["file_ids"]:
                    self.database.set_file_tags(user_id, file_id, applied[str(file_id)])
                    completed.append(file_id)
            except Exception:
                for file_id in completed:
                    self.database.set_file_tags(user_id, file_id, previous[str(file_id)])
                raise
            return {"updated": len(previous)}, {
                "type": "tag", "previous": previous, "applied": applied
            }
        if action_type == "copy_to_output":
            directory = (self.output_root / action["target_folder"]).resolve()
            if not directory.is_relative_to(self.output_root):
                raise ValueError("目标目录超出成果空间")
            directory.mkdir(parents=True, exist_ok=True)
            copied = []
            try:
                for file_id in action["file_ids"]:
                    file = self.database.get_file(file_id)
                    if not file:
                        raise ValueError(f"文件 {file_id} 不存在")
                    source = Path(file["path"])
                    if not source.is_file():
                        raise FileNotFoundError(f"文件已离线：{source.name}")
                    destination = self._unique_destination(directory, source.name)
                    shutil.copy2(source, destination)
                    copied.append({
                        "file_id": file_id,
                        "path": str(destination),
                        "size": destination.stat().st_size,
                        "sha256": self._digest(destination),
                    })
            except Exception:
                for record in copied:
                    Path(record["path"]).unlink(missing_ok=True)
                raise
            return {"copied": copied}, {"type": "copy_to_output", "files": copied}
        if action_type == "add_to_space":
            existing = {
                int(row["file_id"])
                for row in self.database.fetchall(
                    "SELECT file_id FROM knowledge_space_files WHERE space_id = ?", (action["space_id"],)
                )
            }
            added = [file_id for file_id in action["file_ids"] if file_id not in existing]
            self.add_space_files(action["space_id"], added, user_id)
            return {"added": len(added)}, {"type": "add_to_space", "space_id": action["space_id"], "file_ids": added}
        artifact = self.create_artifact(
            action["title"], action["artifact_type"], user_id, action.get("project_id"), action.get("space_id")
        )
        try:
            version = self.generate_artifact_version(
                int(artifact["id"]), action["prompt"], action["file_ids"], user_id
            )
        except Exception:
            self.delete_artifact(int(artifact["id"]), user_id)
            raise
        version_path = Path(version["file_path"])
        return {
            "artifact_id": artifact["id"], "version_id": version["id"]
        }, {
            "type": "generate_artifact",
            "artifact_id": artifact["id"],
            "version_id": version["id"],
            "file": {
                "path": str(version_path),
                "size": version_path.stat().st_size,
                "sha256": self._digest(version_path),
            },
        }

    def execute_agent_run(self, run_id: int) -> dict[str, Any]:
        run = self.database.fetchone("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
        if not run or run["status"] not in {"approved", "running"}:
            raise ValueError("Agent 任务未获得执行确认")
        plan = self._loads(run["plan_json"], [])
        try:
            self._ensure_execution_access(int(run["user_id"]), run, plan)
        except Exception as exc:
            self.database.execute(
                "UPDATE agent_runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (f"{type(exc).__name__}: {exc}"[:2000], utc_now(), run_id),
            )
            raise
        if run["status"] == "approved":
            self.database.execute(
                "UPDATE agent_runs SET status = 'running', started_at = ?, error = '' WHERE id = ?",
                (utc_now(), run_id),
            )
        undo: list[dict[str, Any]] = self._loads(run["undo_json"], [])
        try:
            for sequence, action in enumerate(plan, 1):
                action_state = self.database.fetchone(
                    "SELECT status FROM agent_actions WHERE run_id = ? AND sequence = ?",
                    (run_id, sequence),
                )
                if action_state and action_state["status"] == "completed":
                    continue
                if action_state and action_state["status"] == "running":
                    self.database.execute(
                        """UPDATE agent_actions SET status = 'failed', error = ?, finished_at = ?
                           WHERE run_id = ? AND sequence = ?""",
                        ("上次执行在动作中途终止", utc_now(), run_id, sequence),
                    )
                    raise RuntimeError("上次执行在动作中途终止；为避免重复写入，需先撤销或人工检查")
                self.database.execute(
                    "UPDATE agent_actions SET status = 'running', error = '' WHERE run_id = ? AND sequence = ?",
                    (run_id, sequence),
                )
                try:
                    result, inverse = self._execute_action(action, int(run["user_id"]))
                except Exception as exc:
                    self.database.execute(
                        """UPDATE agent_actions SET status = 'failed', error = ?, finished_at = ?
                           WHERE run_id = ? AND sequence = ?""",
                        (f"{type(exc).__name__}: {exc}"[:2000], utc_now(), run_id, sequence),
                    )
                    raise
                undo.append(inverse)
                with self.database.transaction() as connection:
                    connection.execute(
                        """UPDATE agent_actions SET status = 'completed', result_json = ?, finished_at = ?
                           WHERE run_id = ? AND sequence = ?""",
                        (json.dumps(result, ensure_ascii=False), utc_now(), run_id, sequence),
                    )
                    connection.execute(
                        "UPDATE agent_runs SET undo_json = ? WHERE id = ?",
                        (json.dumps(undo, ensure_ascii=False), run_id),
                    )
            self.database.execute(
                """UPDATE agent_runs SET status = 'completed', undo_json = ?, finished_at = ? WHERE id = ?""",
                (json.dumps(undo, ensure_ascii=False), utc_now(), run_id),
            )
        except Exception as exc:
            self.database.execute(
                "UPDATE agent_runs SET status = 'failed', undo_json = ?, error = ?, finished_at = ? WHERE id = ?",
                (json.dumps(undo, ensure_ascii=False), f"{type(exc).__name__}: {exc}"[:2000], utc_now(), run_id),
            )
            raise
        return self.agent_run(run_id, int(run["user_id"])) or {}

    def undo_agent_run(self, run_id: int, user_id: int) -> dict[str, Any]:
        run = self.database.fetchone("SELECT * FROM agent_runs WHERE id = ? AND user_id = ?", (run_id, user_id))
        if not run or run["status"] not in {"completed", "failed"} or not self._loads(run["undo_json"], []):
            raise ValueError("只有已产生可撤销变更的任务可以撤销")
        undo = self._loads(run["undo_json"], [])
        virtual_tags: dict[str, list[str]] = {}
        for inverse in reversed(undo):
            if inverse["type"] == "tag" and inverse.get("applied"):
                for file_id, applied in inverse["applied"].items():
                    current = virtual_tags.get(file_id)
                    if current is None:
                        current = self.database.file_tag_names(user_id, int(file_id))
                    if current != applied:
                        raise ValueError(f"文件 {file_id} 的标签已变化，拒绝覆盖人工修改")
                    virtual_tags[file_id] = inverse["previous"][file_id]
            elif inverse["type"] == "copy_to_output":
                for record in inverse["files"]:
                    path = Path(record["path"]).resolve()
                    if not path.is_relative_to(self.output_root) or not path.is_file():
                        continue
                    if path.stat().st_size != int(record["size"]) or self._digest(path) != record["sha256"]:
                        raise ValueError(f"成果文件已变化，拒绝删除：{path.name}")
            elif inverse["type"] == "generate_artifact" and inverse.get("file"):
                versions = self.database.fetchall(
                    "SELECT id FROM artifact_versions WHERE artifact_id = ? ORDER BY id",
                    (int(inverse["artifact_id"]),),
                )
                if [int(item["id"]) for item in versions] != [int(inverse["version_id"])]:
                    raise ValueError("成果已有新版本，拒绝撤销整个成果")
                record = inverse["file"]
                path = Path(record["path"]).resolve()
                if path.is_file() and (
                    not path.is_relative_to(self.output_root)
                    or path.stat().st_size != int(record["size"])
                    or self._digest(path) != record["sha256"]
                ):
                    raise ValueError(f"成果文件已变化，拒绝删除：{path.name}")
        for inverse in reversed(undo):
            if inverse["type"] == "tag":
                for file_id, tags in inverse["previous"].items():
                    self.database.set_file_tags(user_id, int(file_id), tags)
            elif inverse["type"] == "copy_to_output":
                for record in inverse["files"]:
                    path = Path(record["path"]).resolve()
                    if not path.is_relative_to(self.output_root) or not path.is_file():
                        continue
                    path.unlink()
            elif inverse["type"] == "add_to_space":
                for file_id in inverse["file_ids"]:
                    self.remove_space_file(int(inverse["space_id"]), int(file_id))
            elif inverse["type"] == "generate_artifact":
                self.delete_artifact(int(inverse["artifact_id"]), user_id)
        self.database.execute(
            "UPDATE agent_runs SET status = 'undone', undone_at = ? WHERE id = ?", (utc_now(), run_id)
        )
        return self.agent_run(run_id, user_id) or {}

    def create_workflow(
        self,
        user_id: int,
        name: str,
        description: str,
        trigger_type: str,
        trigger: dict[str, Any],
        conditions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        enabled: bool,
        project_id: int | None = None,
        space_id: int | None = None,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("自动化名称不能为空")
        if trigger_type not in TRIGGER_TYPES:
            raise ValueError("不支持的自动化触发器")
        normalized_actions = [self._normalize_action(action, []) for action in actions]
        if not normalized_actions or len(normalized_actions) > 20:
            raise ValueError("自动化需要 1-20 个动作")
        self._validate_workflow(trigger_type, trigger, conditions, normalized_actions, project_id, space_id)
        now = utc_now()
        workflow_id = self.database.execute(
            """INSERT INTO automation_workflows(
                   name, description, user_id, project_id, space_id, trigger_type, trigger_json,
                   conditions_json, actions_json, enabled, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                clean_name, description.strip(), user_id, project_id, space_id, trigger_type,
                json.dumps(trigger, ensure_ascii=False), json.dumps(conditions, ensure_ascii=False),
                json.dumps(normalized_actions, ensure_ascii=False), int(enabled), now, now,
            ),
        )
        return self.workflow(workflow_id, user_id) or {}

    def workflow(self, workflow_id: int, user_id: int, admin: bool = False) -> dict[str, Any] | None:
        row = self.database.fetchone("SELECT * FROM automation_workflows WHERE id = ?", (workflow_id,))
        if not row or (not admin and int(row["user_id"]) != user_id):
            return None
        row["trigger"] = self._loads(row.pop("trigger_json", "{}"), {})
        row["conditions"] = self._loads(row.pop("conditions_json", "[]"), [])
        row["actions"] = self._loads(row.pop("actions_json", "[]"), [])
        row["runs"] = self.database.fetchall(
            "SELECT * FROM automation_runs WHERE workflow_id = ? ORDER BY id DESC LIMIT 20", (workflow_id,)
        )
        return row

    def list_workflows(self, user_id: int, admin: bool = False) -> list[dict[str, Any]]:
        if admin:
            rows = self.database.fetchall("SELECT * FROM automation_workflows ORDER BY updated_at DESC")
        else:
            rows = self.database.fetchall(
                "SELECT * FROM automation_workflows WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)
            )
        for row in rows:
            row["trigger"] = self._loads(row.pop("trigger_json", "{}"), {})
            row["conditions"] = self._loads(row.pop("conditions_json", "[]"), [])
            row["actions"] = self._loads(row.pop("actions_json", "[]"), [])
        return rows

    def set_workflow_enabled(self, workflow_id: int, user_id: int, enabled: bool) -> None:
        row = self.database.fetchone(
            "SELECT id FROM automation_workflows WHERE id = ? AND user_id = ?", (workflow_id, user_id)
        )
        if not row:
            raise ValueError("自动化不存在")
        self.database.execute(
            "UPDATE automation_workflows SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), utc_now(), workflow_id),
        )

    def update_workflow(
        self,
        workflow_id: int,
        user_id: int,
        name: str,
        description: str,
        trigger_type: str,
        trigger: dict[str, Any],
        conditions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        enabled: bool,
        project_id: int | None = None,
        space_id: int | None = None,
    ) -> dict[str, Any]:
        if not self.database.fetchone(
            "SELECT id FROM automation_workflows WHERE id = ? AND user_id = ?", (workflow_id, user_id)
        ):
            raise ValueError("自动化不存在")
        if trigger_type not in TRIGGER_TYPES:
            raise ValueError("不支持的自动化触发器")
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("自动化名称不能为空")
        normalized_actions = [self._normalize_action(action, []) for action in actions]
        if not normalized_actions or len(normalized_actions) > 20:
            raise ValueError("自动化需要 1-20 个动作")
        self._validate_workflow(trigger_type, trigger, conditions, normalized_actions, project_id, space_id)
        self.database.execute(
            """UPDATE automation_workflows SET name = ?, description = ?, project_id = ?, space_id = ?,
               trigger_type = ?, trigger_json = ?, conditions_json = ?, actions_json = ?, enabled = ?,
               last_trigger_key = '', updated_at = ? WHERE id = ?""",
            (
                clean_name, description.strip(), project_id, space_id, trigger_type,
                json.dumps(trigger, ensure_ascii=False), json.dumps(conditions, ensure_ascii=False),
                json.dumps(normalized_actions, ensure_ascii=False), int(enabled), utc_now(), workflow_id,
            ),
        )
        return self.workflow(workflow_id, user_id) or {}

    @staticmethod
    def _validate_workflow(
        trigger_type: str,
        trigger: dict[str, Any],
        conditions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        project_id: int | None,
        space_id: int | None,
    ) -> None:
        if len(conditions) > 20:
            raise ValueError("自动化最多支持 20 个条件")
        for condition in conditions:
            field = str(condition.get("field") or "")
            if field not in CONDITION_FIELDS:
                raise ValueError(f"不支持的自动化条件：{field or '空'}")
            value = condition.get("value")
            if value is None or value == "" or (isinstance(value, str) and not value.strip()):
                raise ValueError("自动化条件缺少值")
            if field in {"library_id", "min_size", "max_size"}:
                try:
                    if int(condition["value"]) < 0:
                        raise ValueError
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"自动化条件 {field} 必须是非负整数") from exc
        if trigger_type != "schedule":
            return
        try:
            hour = int(trigger.get("hour", 0))
            minute = int(trigger.get("minute", 0))
            weekdays = [int(value) for value in trigger.get("weekdays") or range(7)]
        except (TypeError, ValueError) as exc:
            raise ValueError("定时触发配置无效") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not weekdays or any(
            value < 0 or value > 6 for value in weekdays
        ):
            raise ValueError("定时触发的时间或星期配置无效")
        if not project_id and not space_id and any(not action.get("file_ids") for action in actions):
            raise ValueError("定时自动化必须指定项目或知识空间作为工作范围")

    def create_automation_run(self, workflow_id: int, file_ids: list[int], source: str) -> int:
        workflow = self.database.fetchone("SELECT * FROM automation_workflows WHERE id = ?", (workflow_id,))
        if not workflow or not workflow["enabled"]:
            raise ValueError("自动化未启用")
        return self.database.execute(
            """INSERT INTO automation_runs(workflow_id, status, trigger_payload_json, created_at)
               VALUES (?, 'pending', ?, ?)""",
            (workflow_id, json.dumps({"file_ids": file_ids, "source": source}, ensure_ascii=False), utc_now()),
        )

    @staticmethod
    def _matches(file: dict[str, Any], conditions: list[dict[str, Any]]) -> bool:
        for condition in conditions:
            field = str(condition.get("field") or "")
            value = condition.get("value")
            if field == "kind" and str(file.get("kind")) != str(value):
                return False
            if field == "extension" and str(file.get("extension", "")).lower() != str(value).lower():
                return False
            if field == "library_id" and int(file.get("library_id") or 0) != int(value or 0):
                return False
            if field == "name_contains" and str(value).lower() not in str(file.get("name", "")).lower():
                return False
            if field == "min_size" and int(file.get("size") or 0) < int(value or 0):
                return False
            if field == "max_size" and int(file.get("size") or 0) > int(value or 0):
                return False
        return True

    def create_file_arrival_runs(self, file_ids: list[int]) -> list[int]:
        runs: list[int] = []
        workflows = self.database.fetchall(
            "SELECT * FROM automation_workflows WHERE enabled = 1 AND trigger_type = 'file_arrived'"
        )
        files = [self.database.get_file(file_id) for file_id in file_ids]
        for workflow in workflows:
            user = self.database.get_user(int(workflow["user_id"]))
            if not user or not user.get("enabled"):
                continue
            allowed_libraries = None if user and user.get("role") in {"owner", "admin"} else set(
                int(value) for value in (user or {}).get("library_ids", [])
            )
            conditions = self._loads(workflow["conditions_json"], [])
            matched = [
                int(file["id"]) for file in files
                if file
                and (allowed_libraries is None or int(file["library_id"]) in allowed_libraries)
                and not Path(file["path"]).resolve().is_relative_to(self.output_root)
                and self._matches(file, conditions)
            ]
            if matched:
                runs.append(self.create_automation_run(int(workflow["id"]), matched, "file_arrived"))
        return runs

    def create_due_schedule_runs(self, now: datetime | None = None) -> list[int]:
        current = now or datetime.now().astimezone()
        runs: list[int] = []
        workflows = self.database.fetchall(
            "SELECT * FROM automation_workflows WHERE enabled = 1 AND trigger_type = 'schedule'"
        )
        for workflow in workflows:
            user = self.database.get_user(int(workflow["user_id"]))
            if not user or not user.get("enabled"):
                continue
            trigger = self._loads(workflow["trigger_json"], {})
            weekdays = [int(value) for value in trigger.get("weekdays") or range(7)]
            if current.weekday() not in weekdays:
                continue
            if current.hour != int(trigger.get("hour", 0)) or current.minute != int(trigger.get("minute", 0)):
                continue
            trigger_key = current.strftime("%Y-%m-%dT%H:%M")
            if workflow["last_trigger_key"] == trigger_key:
                continue
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """UPDATE automation_workflows SET last_trigger_key = ?
                       WHERE id = ? AND last_trigger_key != ?""",
                    (trigger_key, workflow["id"], trigger_key),
                )
                if not cursor.rowcount:
                    continue
                run_cursor = connection.execute(
                    """INSERT INTO automation_runs(workflow_id, status, trigger_payload_json, created_at)
                       VALUES (?, 'pending', ?, ?)""",
                    (
                        workflow["id"],
                        json.dumps({"file_ids": [], "source": "schedule"}, ensure_ascii=False),
                        utc_now(),
                    ),
                )
                runs.append(int(run_cursor.lastrowid))
        return runs

    def execute_automation_run(self, automation_run_id: int) -> dict[str, Any]:
        run = self.database.fetchone("SELECT * FROM automation_runs WHERE id = ?", (automation_run_id,))
        if not run or run["status"] not in {"pending", "running"}:
            raise ValueError("自动化运行不存在或状态无效")
        workflow = self.database.fetchone(
            "SELECT * FROM automation_workflows WHERE id = ?", (run["workflow_id"],)
        )
        if not workflow or not workflow["enabled"]:
            self.database.execute(
                "UPDATE automation_runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                ("自动化已停用", utc_now(), automation_run_id),
            )
            raise ValueError("自动化已停用")
        user = self.database.get_user(int(workflow["user_id"]))
        if not user or not user.get("enabled"):
            self.database.execute(
                "UPDATE automation_runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                ("自动化所属用户已停用", utc_now(), automation_run_id),
            )
            raise PermissionError("自动化所属用户已停用")
        payload = self._loads(run["trigger_payload_json"], {})
        actions = self._loads(workflow["actions_json"], [])
        file_ids = [int(value) for value in payload.get("file_ids") or []]
        if not file_ids:
            file_ids = self.scope_file_ids(workflow.get("space_id"), workflow.get("project_id")) or []
        try:
            saved = self._loads(run.get("result_json"), {})
            agent_run_id = int(saved.get("agent_run_id") or 0)
            if not agent_run_id:
                self.database.execute(
                    "UPDATE automation_runs SET status = 'running', started_at = ? WHERE id = ?",
                    (utc_now(), automation_run_id),
                )
                agent = self.create_agent_run(
                    int(workflow["user_id"]), f"自动化：{workflow['name']}", actions, file_ids,
                    workflow.get("project_id"), workflow.get("space_id"),
                )
                agent_run_id = int(agent["id"])
                self.database.execute(
                    "UPDATE automation_runs SET result_json = ? WHERE id = ?",
                    (json.dumps({"agent_run_id": agent_run_id}), automation_run_id),
                )
            agent_state = self.database.fetchone("SELECT status FROM agent_runs WHERE id = ?", (agent_run_id,))
            if not agent_state:
                raise RuntimeError("自动化关联的 AI 任务不存在")
            if agent_state["status"] == "draft":
                self.database.execute(
                    "UPDATE agent_runs SET status = 'approved', approved_at = ? WHERE id = ?",
                    (utc_now(), agent_run_id),
                )
            if agent_state["status"] in {"completed", "undone"}:
                result = self.agent_run(agent_run_id, int(workflow["user_id"])) or {}
            elif agent_state["status"] == "failed":
                raise RuntimeError("自动化关联的 AI 任务执行失败")
            else:
                result = self.execute_agent_run(agent_run_id)
            summary = {"agent_run_id": agent_run_id, "status": result["status"]}
            now = utc_now()
            with self.database.transaction() as connection:
                connection.execute(
                    """UPDATE automation_runs SET status = 'completed', result_json = ?, finished_at = ?
                       WHERE id = ?""",
                    (json.dumps(summary, ensure_ascii=False), now, automation_run_id),
                )
                connection.execute(
                    "UPDATE automation_workflows SET last_run_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, workflow["id"]),
                )
            return summary
        except Exception as exc:
            self.database.execute(
                "UPDATE automation_runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (f"{type(exc).__name__}: {exc}"[:2000], utc_now(), automation_run_id),
            )
            raise
