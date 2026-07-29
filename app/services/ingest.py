from __future__ import annotations

import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable

from app.config import Settings
from app.database import Database
from app.services.scanner import scan_library
from app.services.workspaces import WorkspaceService


def project_inbox(settings: Settings, project_id: int) -> Path:
    root = (settings.ingest_root / f"project-{project_id}").resolve()
    try:
        root.relative_to(settings.upload_root)
    except ValueError as exc:
        raise ValueError("入库箱必须位于上传空间内") from exc
    root.mkdir(parents=True, exist_ok=True)
    return root


def _extract_packages(inbox: Path, maximum_bytes: int) -> set[Path]:
    extracted: set[Path] = set()
    for package in inbox.rglob("*"):
        if not package.is_file() or package.suffix.lower() not in {".zip", ".eaglepack"}:
            continue
        if not zipfile.is_zipfile(package):
            continue
        destination = package.with_name(f"{package.stem}-import")
        if destination.is_dir():
            extracted.add(package.resolve())
            continue
        temporary = Path(tempfile.mkdtemp(prefix=".nas-ai-import-", dir=package.parent))
        try:
            with zipfile.ZipFile(package) as archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
                if len(members) > 10000:
                    raise ValueError(f"{package.name} 内文件数量超过 10,000")
                total = sum(max(0, int(item.file_size)) for item in members)
                if total > maximum_bytes:
                    raise ValueError(f"{package.name} 解压后超过入库大小限制")
                for member in members:
                    if member.flag_bits & 0x1:
                        raise ValueError(f"{package.name} 包含加密文件")
                    mode = member.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise ValueError(f"{package.name} 包含不允许的符号链接")
                    target = (temporary / member.filename).resolve()
                    try:
                        target.relative_to(temporary)
                    except ValueError as exc:
                        raise ValueError(f"{package.name} 包含不安全路径") from exc
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
            temporary.replace(destination)
            extracted.add(package.resolve())
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    return extracted


def collect_project_inbox(
    database: Database,
    settings: Settings,
    project_id: int,
    user_id: int | None,
    progress: Callable[[float, str], None],
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    if not database.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,)):
        raise ValueError("项目不存在")
    inbox = project_inbox(settings, project_id)
    extracted_packages = _extract_packages(inbox, settings.max_upload_bytes)
    library = database.fetchone("SELECT * FROM libraries WHERE path = ?", (str(settings.upload_root),))
    if not library:
        raise ValueError("上传空间媒体库不存在")
    progress(0.02, "正在扫描 NAS 入库箱")
    scan_library(
        database,
        library,
        lambda value, message: progress(min(0.25, 0.02 + value * 2), message),
        cancelled,
    )
    if cancelled():
        raise InterruptedError
    prefix = str(inbox) + "/"
    files = database.fetchall(
        """SELECT * FROM files WHERE library_id = ? AND (path = ? OR path LIKE ?)
           ORDER BY relative_path COLLATE NOCASE LIMIT 10000""",
        (library["id"], str(inbox), f"{prefix}%"),
    )
    existing_rows = database.fetchall(
        """SELECT DISTINCT av.file_id FROM asset_versions av JOIN assets a ON a.id = av.asset_id
           WHERE a.project_id = ? AND av.file_id IS NOT NULL""",
        (project_id,),
    )
    existing = {int(row["file_id"]) for row in existing_rows}
    workspaces = WorkspaceService(database)
    added = 0
    for index, file in enumerate(files):
        if cancelled():
            raise InterruptedError
        file_id = int(file["id"])
        if Path(file["path"]).resolve() in extracted_packages:
            continue
        if file_id not in existing:
            workspaces.create_asset(project_id, file, None, str(file["name"]), user_id)
            existing.add(file_id)
            added += 1
        if index and index % 100 == 0:
            progress(0.25 + 0.15 * index / max(1, len(files)), f"已整理 {index:,} 个入库文件")
    pending_ids = [
        int(file["id"])
        for file in files
        if file["status"] in {"pending", "partial", "error"} and not file["terminal_error"]
    ]
    progress(0.4, f"已加入 {added:,} 个项目素材")
    return {
        "project_id": project_id,
        "inbox": str(inbox),
        "files": len(files),
        "assets_added": added,
        "packages_extracted": len(extracted_packages),
        "pending_file_ids": pending_ids,
    }
