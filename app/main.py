from __future__ import annotations

import hashlib
import hmac
import html
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
import csv
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Optional
from urllib.parse import quote, unquote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app.database import Database, configure_database
from app.security import hash_password, session_token, token_digest, verify_password
from app.services.extractors import create_thumbnail
from app.services.hardware import detect_hardware, memory_runtime, runtime_metrics
from app.services.ingest import project_inbox
from app.services.local_ai import LocalAIClient
from app.services.recycle import RecycleBin
from app.services.search import SearchService
from app.services.watcher import LibraryWatcher
from app.services.tasks import TaskManager
from app.services.vectors import VectorStore
from app.services.scanner import file_kind
from app.services.workspaces import COMMENT_ROLES, EDIT_ROLES, MANAGE_ROLES, WorkspaceService


logger = logging.getLogger(__name__)


class LibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=2048)


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    kind: str = ""
    conversation_id: Optional[int] = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=512)


class BootstrapRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=512)


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=512)
    role: str = "member"
    library_ids: list[int] = []


class UserUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(default="", max_length=512)
    role: str = "member"
    enabled: bool = True
    library_ids: list[int] = []


class PersonUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class AlbumUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class RecycleRequest(BaseModel):
    file_ids: list[int] = Field(min_length=1, max_length=100)


class SnapshotRestoreRequest(BaseModel):
    confirm: str = Field(min_length=1, max_length=200)


class OpsMemoryRequest(BaseModel):
    mb: int = Field(ge=256, le=8192)


class CaptionUpgradeRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=100000)


class CaptionUpdate(BaseModel):
    caption: str = Field(default="", max_length=4000)


class FeedbackUpdate(BaseModel):
    query: str = Field(default="", max_length=1000)
    verdict: str
    note: str = Field(default="", max_length=1000)


class TagsUpdate(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=20)


class SmartAlbumCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    query: str = Field(default="", max_length=1000)
    kind: str = ""
    filters: dict[str, Any] = Field(default_factory=dict)


class PersonMerge(BaseModel):
    target_id: int
    source_ids: list[int] = Field(min_length=1, max_length=100)


class PersonSplit(BaseModel):
    face_ids: list[int] = Field(min_length=1, max_length=500)
    name: str = Field(default="", max_length=80)


class CoverUpdate(BaseModel):
    item_id: int


class EventMerge(BaseModel):
    target_id: int
    source_ids: list[int] = Field(min_length=1, max_length=100)


class EventSplit(BaseModel):
    file_ids: list[int] = Field(min_length=1, max_length=1000)
    name: str = Field(min_length=1, max_length=100)


class IndexRequest(BaseModel):
    limit: int = Field(default=200, ge=1, le=100000)
    library_id: Optional[int] = None
    kind: str = ""
    order: str = "balanced"


class IndexPolicyUpdate(BaseModel):
    enabled: bool = False
    start_hour: int = Field(default=0, ge=0, le=23)
    end_hour: int = Field(default=7, ge=0, le=23)
    batch_size: int = Field(default=200, ge=1, le=10000)
    library_id: Optional[int] = None
    kind: str = ""
    order: str = "balanced"


class IndexControllerReport(BaseModel):
    state: str = Field(default="idle", max_length=40)
    message: str = Field(default="", max_length=500)
    task_id: Optional[int] = None
    repairable: int = Field(default=0, ge=0)
    retry_waiting: int = Field(default=0, ge=0)
    terminal_failures: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)
    caption_pending: int = Field(default=0, ge=0)
    available_memory_bytes: int = Field(default=0, ge=0)
    free_swap_bytes: int = Field(default=0, ge=0)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    color: str = Field(default="#7c8cff", pattern=r"^#[0-9A-Fa-f]{6}$")


class ProjectUpdate(ProjectCreate):
    status: str = Field(default="active", pattern=r"^(active|archived)$")


class ProjectMemberUpdate(BaseModel):
    user_id: int
    role: str = Field(pattern=r"^(manager|editor|reviewer|viewer)$")


class ProjectFolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    parent_id: Optional[int] = None


class ProjectStatusesUpdate(BaseModel):
    items: list[dict[str, Any]] = Field(min_length=1, max_length=20)


class AssetCreate(BaseModel):
    file_id: int
    folder_id: Optional[int] = None
    title: str = Field(default="", max_length=240)


class AssetUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=4000)
    status: str = Field(default="draft", max_length=40)
    rating: int = Field(default=0, ge=0, le=5)
    folder_id: Optional[int] = None
    assignee_id: Optional[int] = None


class AssetVersionCreate(BaseModel):
    file_id: int
    label: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=2000)


class LookPreviewCreate(BaseModel):
    lut_file_id: int


class ReviewSessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ReviewCommentCreate(BaseModel):
    version_id: Optional[int] = None
    review_session_id: Optional[int] = None
    body: str = Field(min_length=1, max_length=4000)
    comment_type: str = Field(default="text", pattern=r"^(text|point|range|drawing)$")
    time_start: Optional[float] = Field(default=None, ge=0)
    time_end: Optional[float] = Field(default=None, ge=0)
    x: Optional[float] = Field(default=None, ge=0, le=1)
    y: Optional[float] = Field(default=None, ge=0, le=1)
    drawing: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    visibility: str = Field(default="team", pattern=r"^(team|external)$")


class CommentResolve(BaseModel):
    resolved: bool = True


class ShareCreate(BaseModel):
    asset_id: Optional[int] = None
    name: str = Field(min_length=1, max_length=120)
    access_code: str = Field(default="", max_length=120)
    expires_at: Optional[str] = None
    can_download: bool = False
    can_comment: bool = True
    can_view_versions: bool = False
    watermark_text: str = Field(default="", max_length=100)
    brand_name: str = Field(default="NAS AI Space", max_length=100)


class PublicShareAccess(BaseModel):
    access_code: str = Field(default="", max_length=120)


class PublicReviewComment(BaseModel):
    access_code: str = Field(default="", max_length=120)
    asset_id: int
    version_id: Optional[int] = None
    guest_name: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=4000)
    time_start: Optional[float] = Field(default=None, ge=0)
    time_end: Optional[float] = Field(default=None, ge=0)


class AppState:
    database: Database
    ai: LocalAIClient
    vectors: VectorStore
    search: SearchService
    tasks: TaskManager
    watcher: LibraryWatcher
    recycle: RecycleBin
    workspaces: WorkspaceService
    media_tickets: dict[str, tuple[int, float]]
    workspace_tickets: dict[str, tuple[str, str, str, float, bool, str]]


state = AppState()
INDEX_KINDS = {"", "image", "video", "audio", "document", "archive", "other"}
INDEX_ORDERS = {"balanced", "newest", "oldest", "smallest"}
# 容器资源面板可操作的服务白名单（与 ops 边车各自独立校验）
OPS_SERVICES = {"app", "vision", "embedding", "qdrant", "speech"}
COMMENT_ATTACHMENT_MIMES = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/avif",
    "video/mp4", "video/webm", "video/quicktime", "video/x-matroska",
}
# 可在同源执行脚本的活跃内容类型，下载/预览一律强制 attachment
ACTIVE_CONTENT_MIMES = {
    "text/html", "image/svg+xml", "text/javascript", "application/javascript",
    "text/xml", "application/xhtml+xml",
}
LOGIN_FAILURES: dict[str, list[float]] = {}
PUBLIC_ACCESS_FAILURES: dict[str, list[float]] = {}
PUBLIC_COMMENT_ATTEMPTS: dict[str, list[float]] = {}
PUBLIC_ATTACHMENT_ATTEMPTS: dict[str, list[float]] = {}
AUTH_ATTACHMENT_ATTEMPTS: dict[str, list[float]] = {}
BOOTSTRAP_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_FAILURES_LOCK = threading.Lock()
PUBLIC_RATE_LIMIT_LOCK = threading.Lock()
MEDIA_TICKETS_LOCK = threading.Lock()
WORKSPACE_TICKETS_LOCK = threading.Lock()
RATE_LIMIT_BUCKETS = 4096


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.umask(0o077)
    settings.prepare()
    state.database = configure_database(settings.database_path)
    if not state.database.fetchone("SELECT id FROM libraries WHERE path = ?", (str(settings.upload_root),)):
        state.database.create_library("上传空间", str(settings.upload_root))
    state.ai = LocalAIClient(settings)
    state.ai.set_max_concurrency(detect_hardware().plan.inference_workers)
    state.vectors = VectorStore(settings)
    state.search = SearchService(state.database, state.ai, state.vectors)
    state.tasks = TaskManager(state.database, settings, state.ai, state.vectors)
    state.recycle = RecycleBin(state.database, settings, state.vectors)
    state.workspaces = WorkspaceService(state.database)
    state.media_tickets = {}
    state.workspace_tickets = {}
    await state.tasks.start()
    state.watcher = LibraryWatcher(state.database, settings, state.tasks)
    await state.watcher.start()
    yield
    await state.watcher.stop()
    await state.tasks.stop()


app = FastAPI(
    title="NAS AI Space",
    version="1.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def production_headers(request: Request, call_next: Any) -> Any:
    request_id = request.headers.get("x-request-id", "").strip()[:128] or secrets.token_hex(12)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
        "frame-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
    )
    if request.url.path.startswith("/api/") and not request.url.path.startswith(("/api/media/", "/api/files/")):
        response.headers["Cache-Control"] = "no-store"
    return response


def require_auth(
    authorization: Annotated[Optional[str], Header()] = None,
    x_api_token: Annotated[Optional[str], Header()] = None,
    token: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    candidate = x_api_token or (token if settings.allow_query_token else None) or ""
    if authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    if settings.api_token and candidate and hmac.compare_digest(candidate, settings.api_token):
        return {
            "user_id": None,
            "username": "system",
            "display_name": "系统管理员",
            "role": "owner",
            "library_ids": None,
            "auth_type": "api_token",
        }
    if candidate:
        user = state.database.resolve_session(token_digest(candidate))
        if user:
            return {
                "user_id": int(user["id"]),
                "username": user["username"],
                "display_name": user["display_name"],
                "role": user["role"],
                "library_ids": user["library_ids"],
                "auth_type": "session",
            }
    # 匿名 owner 兜底只放行纯首次启动窗口（users 表为空）；bootstrap 完成后无凭据一律 401
    if not settings.api_token and state.database.user_count() == 0:
        return {
            "user_id": None,
            "username": "system",
            "display_name": "系统管理员",
            "role": "owner",
            "library_ids": None,
            "auth_type": "local",
        }
    raise HTTPException(status_code=401, detail="需要有效的访问令牌或账号会话")


Auth = Annotated[dict[str, Any], Depends(require_auth)]


def _library_ids(principal: dict[str, Any]) -> list[int] | None:
    if principal["role"] in {"owner", "admin"}:
        return None
    return [int(value) for value in principal.get("library_ids") or []]


def _is_admin(principal: dict[str, Any]) -> bool:
    return principal["role"] in {"owner", "admin"}


def _require_admin(principal: dict[str, Any]) -> None:
    if not _is_admin(principal):
        raise HTTPException(status_code=403, detail="需要管理员权限")


def _personal_user_id(principal: dict[str, Any]) -> int:
    user_id = principal.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=409, detail="请使用本地账号登录后使用个人收藏、标签和对话")
    return int(user_id)


def _actor(principal: dict[str, Any]) -> str:
    return str(principal.get("username") or "system")


def _audit(
    principal: dict[str, Any],
    action: str,
    target_type: str = "",
    target_id: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    state.database.audit(
        action,
        _actor(principal),
        principal.get("user_id"),
        target_type,
        target_id,
        detail,
    )


def _allowed_library(principal: dict[str, Any], library_id: int) -> bool:
    allowed = _library_ids(principal)
    return allowed is None or library_id in allowed


def _visible_file(file_id: int, principal: dict[str, Any]) -> dict[str, Any]:
    row = state.database.get_file(file_id)
    if not row or not _allowed_library(principal, int(row["library_id"])):
        raise HTTPException(status_code=404, detail="文件不存在")
    return row


def _visible_library(library_id: int, principal: dict[str, Any]) -> dict[str, Any]:
    row = state.database.get_library(library_id)
    if not row or not _allowed_library(principal, library_id):
        raise HTTPException(status_code=404, detail="媒体库不存在")
    return row


def _project_context(
    project_id: int,
    principal: dict[str, Any],
    roles: set[str] | None = None,
) -> tuple[dict[str, Any], str]:
    project = state.workspaces.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    role = state.workspaces.access_role(
        project_id,
        int(principal["user_id"]) if principal.get("user_id") is not None else None,
        _is_admin(principal),
    )
    if not role:
        raise HTTPException(status_code=404, detail="项目不存在")
    if roles is not None and role not in roles:
        raise HTTPException(status_code=403, detail="当前项目角色没有此操作权限")
    return project, role


def _asset_context(
    asset_id: int,
    principal: dict[str, Any],
    roles: set[str] | None = None,
) -> tuple[dict[str, Any], str]:
    asset = state.workspaces.asset_detail(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="素材不存在")
    _, role = _project_context(int(asset["project_id"]), principal, roles)
    return asset, role


def _version_context(
    version_id: int,
    principal: dict[str, Any],
    roles: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    version = state.workspaces.version(version_id)
    if not version:
        raise HTTPException(status_code=404, detail="素材版本不存在")
    asset, role = _asset_context(int(version["asset_id"]), principal, roles)
    return version, asset, role


def _is_active_content(mime_type: Any) -> bool:
    return str(mime_type or "").split(";")[0].strip().lower() in ACTIVE_CONTENT_MIMES


def _workspace_ticket(
    path: str,
    mime_type: str,
    filename: str,
    download: bool = False,
    lifetime: int = 3600,
    share_token: str = "",
) -> str:
    source = Path(path)
    if not source.is_file():
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    now = time.monotonic()
    ticket = secrets.token_urlsafe(32)
    with WORKSPACE_TICKETS_LOCK:
        state.workspace_tickets = {
            key: value for key, value in state.workspace_tickets.items() if value[3] > now
        }
        if len(state.workspace_tickets) >= 4096:
            oldest = min(state.workspace_tickets, key=lambda key: state.workspace_tickets[key][3])
            state.workspace_tickets.pop(oldest, None)
        state.workspace_tickets[ticket] = (
            str(source),
            mime_type or "application/octet-stream",
            filename,
            now + max(60, min(86400, lifetime)),
            download,
            share_token,
        )
    return f"/api/workspace-media/{ticket}"


def _comment_attachment_path(name: str) -> Path:
    safe_name = Path(str(name)).name
    if not safe_name or safe_name != str(name):
        raise HTTPException(status_code=404, detail="评论附件不存在")
    return settings.data_dir / "comment-attachments" / safe_name


def _comment_attachment_payload(attachment: dict[str, Any], share_token: str = "") -> dict[str, Any] | None:
    try:
        path = _comment_attachment_path(attachment["name"])
    except HTTPException:
        return None
    if not path.is_file():
        return None
    return {
        "name": attachment["name"],
        "original_name": attachment["original_name"],
        "mime": attachment["mime"],
        "size_bytes": attachment["size_bytes"],
        "url": _workspace_ticket(
            str(path),
            str(attachment["mime"] or ""),
            str(attachment["original_name"] or attachment["name"]),
            share_token=share_token,
        ),
    }


def _remove_comment_attachment_files(names: list[str]) -> None:
    for name in names:
        try:
            _comment_attachment_path(name).unlink(missing_ok=True)
        except (HTTPException, OSError):
            continue


async def _save_comment_attachment(request: Request, comment_id: int, x_filename: str) -> dict[str, Any]:
    filename = Path(unquote((x_filename or "").replace("\x00", ""))).name.strip()
    if not filename or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="缺少有效文件名")
    mime = mimetypes.guess_type(filename)[0] or ""
    if mime not in COMMENT_ATTACHMENT_MIMES:
        raise HTTPException(status_code=400, detail="仅支持常见图片或视频附件")
    content_length = request.headers.get("content-length")
    try:
        declared_bytes = int(content_length) if content_length else 0
    except ValueError:
        declared_bytes = 0
    if declared_bytes > settings.comment_attachment_max_bytes:
        raise HTTPException(status_code=413, detail="附件超过大小限制")
    directory = settings.data_dir / "comment-attachments"
    directory.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(directory).free
    if declared_bytes and declared_bytes + 256 * 1024 * 1024 > free_bytes:
        raise HTTPException(status_code=507, detail="NAS 可用空间不足")
    stored_name = f"{secrets.token_hex(8)}{Path(filename).suffix.lower()}"
    destination = directory / stored_name
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.part")
    written = 0
    last_space_check = 0
    try:
        with temporary.open("xb") as handle:
            async for chunk in request.stream():
                written += len(chunk)
                if written > settings.comment_attachment_max_bytes:
                    raise HTTPException(status_code=413, detail="附件超过大小限制")
                if written - last_space_check >= 64 * 1024 * 1024:
                    last_space_check = written
                    if shutil.disk_usage(directory).free < 256 * 1024 * 1024:
                        raise HTTPException(status_code=507, detail="NAS 可用空间不足")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if not written:
            raise HTTPException(status_code=400, detail="不能上传空文件")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    try:
        return state.workspaces.add_comment_attachment(comment_id, stored_name, filename, mime, written)
    except ValueError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _share_context(token: str, access_code: str = "") -> dict[str, Any]:
    share = state.workspaces.share_by_token(token)
    if not share:
        raise HTTPException(status_code=404, detail="分享不存在、已关闭或已过期")
    if share.get("access_code_hash") and not verify_password(access_code, str(share["access_code_hash"])):
        raise HTTPException(status_code=401, detail="访问码错误")
    # 记下来源 token，后续签发的 workspace ticket 消费时回查分享仍有效
    share["token"] = token
    return share


def _public_asset_payload(asset_id: int, share: dict[str, Any]) -> dict[str, Any]:
    asset = state.workspaces.asset_detail(asset_id)
    if not asset or int(asset["project_id"]) != int(share["project_id"]):
        raise HTTPException(status_code=404, detail="分享素材不存在")
    versions = asset["versions"] if share["can_view_versions"] else [
        next(
            (item for item in asset["versions"] if int(item["id"]) == int(asset.get("cover_version_id") or 0)),
            asset["versions"][0] if asset["versions"] else None,
        )
    ]
    public_versions = []
    share_token = str(share.get("token") or "")
    for version in (item for item in versions if item):
        media_path = str(version.get("proxy_path") or "")
        media_type = "video/mp4" if version["kind"] == "video" else (
            "audio/mp4" if version["kind"] == "audio" else str(version.get("mime_type") or "")
        )
        if not media_path or not Path(media_path).is_file():
            file = state.database.get_file(int(version["file_id"])) if version.get("file_id") else None
            media_path = str(file.get("path") or "") if file else ""
            media_type = str(file.get("mime_type") or version.get("mime_type") or "") if file else media_type
        media_url = (
            _workspace_ticket(media_path, media_type, str(version["file_name"]), share_token=share_token)
            if media_path else ""
        )
        download_url = ""
        if share["can_download"] and version.get("file_id"):
            file = state.database.get_file(int(version["file_id"]))
            if file and Path(file["path"]).is_file():
                download_url = _workspace_ticket(
                    str(file["path"]),
                    str(file.get("mime_type") or version.get("mime_type") or ""),
                    str(file["name"]),
                    True,
                    share_token=share_token,
                )
        public_versions.append({
            key: version.get(key)
            for key in (
                "id", "version_number", "label", "notes", "file_name", "mime_type", "kind",
                "size", "duration", "width", "height", "proxy_status", "caption", "created_at",
            )
        } | {"media_url": media_url, "download_url": download_url})
    comments = [
        {
            key: comment.get(key)
            for key in (
                "id", "version_id", "guest_name", "display_name", "body", "comment_type",
                "time_start", "time_end", "x", "y", "drawing", "resolved", "created_at",
            )
        } | {
            "attachments": [
                payload
                for attachment in comment.get("attachments", [])
                if (payload := _comment_attachment_payload(attachment, share_token))
            ]
        }
        for comment in asset["comments"]
        if comment.get("visibility") == "external"
    ]
    return {
        key: asset.get(key)
        for key in ("id", "title", "description", "status", "rating", "created_at", "updated_at")
    } | {"versions": public_versions, "comments": comments}


def _safe_library_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    for root in settings.scan_roots:
        try:
            path.relative_to(root)
            break
        except ValueError:
            continue
    else:
        allowed = "、".join(str(root) for root in settings.scan_roots)
        raise HTTPException(status_code=400, detail=f"目录必须位于 {allowed} 内")
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=400, detail="目录不存在或不是文件夹")
    return path


def _validate_index_options(library_id: int | None, kind: str, order: str, principal: dict[str, Any]) -> None:
    if library_id is not None:
        _visible_library(library_id, principal)
    if kind not in INDEX_KINDS:
        raise HTTPException(status_code=400, detail="不支持的文件类型")
    if order not in INDEX_ORDERS:
        raise HTTPException(status_code=400, detail="不支持的索引顺序")


def _public_file(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: row.get(key)
        for key in (
            "id", "library_id", "relative_path", "name", "extension", "kind", "mime_type", "size", "mtime_ns",
            "width", "height", "duration", "captured_at", "latitude", "longitude", "ai_caption", "manual_caption",
            "status", "error", "metadata_status", "vision_status", "transcription_status", "embedding_status",
            "vision_error", "transcription_error", "embedding_error", "indexed_at",
            "retry_count", "last_attempt_at", "next_retry_at", "terminal_error",
        )
    }
    try:
        result["metadata"] = json.loads(row.get("metadata_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        result["metadata"] = {}
    return result


def _login_failure_key(request: Request, username: str) -> str:
    client = request.client.host if request.client else "unknown"
    return f"{client}\0{username.strip().casefold()}"


def _prune_rate_limits(
    buckets: dict[str, list[float]],
    now: float,
    window: int,
) -> None:
    for key, values in list(buckets.items()):
        active = [value for value in values if now - value < window]
        if active:
            buckets[key] = active
        else:
            buckets.pop(key, None)
    if len(buckets) > RATE_LIMIT_BUCKETS:
        oldest = sorted(buckets, key=lambda key: buckets[key][-1])
        for key in oldest[:len(buckets) - RATE_LIMIT_BUCKETS]:
            buckets.pop(key, None)


def _login_retry_after(key: str) -> int:
    now = time.monotonic()
    with LOGIN_FAILURES_LOCK:
        _prune_rate_limits(LOGIN_FAILURES, now, 900)
        client_key = f"client:{key.split(chr(0), 1)[0]}"
        # 纯账号名全局桶：跨 IP 计数，堵住多 IP 密码喷洒
        account_key = f"account:{key.split(chr(0), 1)[1]}"
        failures = LOGIN_FAILURES.get(key, [])
        client_failures = LOGIN_FAILURES.get(client_key, [])
        account_failures = LOGIN_FAILURES.get(account_key, [])
        if len(failures) < 5 and len(client_failures) < 20 and len(account_failures) < 30:
            return 0
        thresholds = []
        if len(failures) >= 5:
            thresholds.append(failures[-5])
        if len(client_failures) >= 20:
            thresholds.append(client_failures[-20])
        if len(account_failures) >= 30:
            thresholds.append(account_failures[-30])
        # Retry-After 取各超限桶中的最大等待时长（即最早脱离窗口的那次失败）
        return max(1, round(900 - (now - min(thresholds))))


def _register_login_failure(key: str) -> None:
    now = time.monotonic()
    with LOGIN_FAILURES_LOCK:
        _prune_rate_limits(LOGIN_FAILURES, now, 900)
        client_key = f"client:{key.split(chr(0), 1)[0]}"
        account_key = f"account:{key.split(chr(0), 1)[1]}"
        LOGIN_FAILURES[key] = [*LOGIN_FAILURES.get(key, []), now][-20:]
        LOGIN_FAILURES[client_key] = [*LOGIN_FAILURES.get(client_key, []), now][-40:]
        LOGIN_FAILURES[account_key] = [*LOGIN_FAILURES.get(account_key, []), now][-60:]


def _public_rate_key(request: Request, token: str) -> str:
    client = request.client.host if request.client else "unknown"
    return f"{client}:{token_digest(token)[:20]}"


def _rate_retry_after(
    buckets: dict[str, list[float]],
    lock: threading.Lock,
    key: str,
    maximum: int,
    window: int,
) -> int:
    now = time.monotonic()
    with lock:
        _prune_rate_limits(buckets, now, window)
        attempts = buckets.get(key, [])
        if len(attempts) < maximum:
            return 0
        return max(1, round(window - (now - attempts[-maximum])))


def _record_rate_attempt(
    buckets: dict[str, list[float]],
    lock: threading.Lock,
    key: str,
    maximum: int,
    window: int,
) -> None:
    now = time.monotonic()
    with lock:
        _prune_rate_limits(buckets, now, window)
        buckets[key] = [*buckets.get(key, []), now][-maximum:]


def _clear_rate_attempts(
    buckets: dict[str, list[float]],
    lock: threading.Lock,
    key: str,
) -> None:
    with lock:
        buckets.pop(key, None)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": app.version,
        "auth_enabled": bool(settings.api_token),
        "bootstrap_required": state.database.bootstrap_required(),
    }


def _controller_status() -> dict[str, Any]:
    report = state.database.get_setting("index_controller_report", {})
    if not isinstance(report, dict) or not report:
        return {"state": "unknown", "message": "尚未收到外部调度器状态", "stale": True, "age_seconds": None}
    reported_at = str(report.get("reported_at") or "")
    try:
        timestamp = datetime.fromisoformat(reported_at.replace("Z", "+00:00"))
        age = max(0, round((datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        age = None
    return {**report, "stale": age is None or age > 180, "age_seconds": age}


def _index_overview(files: dict[str, Any] | None = None, stages: dict[str, Any] | None = None) -> dict[str, Any]:
    values = files or state.database.dashboard()["files"]
    total = int(values.get("total") or 0)
    semantic_ready = int(values.get("semantic_ready") or 0)
    ready = int(values.get("ready") or 0)
    # stages 可由调用方预算一次传入（如 /api/index/status），避免同一请求内重复聚合
    stages = stages or state.database.index_stage_summary()
    pending = int(values.get("pending") or 0)
    repairable = int(stages.get("repairable") or 0)
    retry_waiting = int(stages.get("retry_waiting") or 0)
    terminal_failures = int(stages.get("terminal_failures") or 0)
    caption_pending = state.database.caption_upgrade_count()
    active = state.database.active_index_task()
    if active:
        status = "running" if active["status"] == "running" else "queued"
    elif pending or repairable or caption_pending:
        status = "queued"
    elif retry_waiting:
        status = "backoff"
    elif terminal_failures:
        status = "degraded"
    else:
        status = "complete"
    return {
        "status": status,
        "total": total,
        "ready": ready,
        "semantic_ready": semantic_ready,
        "semantic_percent": round(semantic_ready / total * 100, 2) if total else 0.0,
        "pipeline_percent": round(ready / total * 100, 2) if total else 0.0,
        "pending": pending,
        "repairable": repairable,
        "retry_waiting": retry_waiting,
        "terminal_failures": terminal_failures,
        "caption_pending": caption_pending,
        "active": active,
        "runtime": state.database.index_runtime_summary(),
        "controller": _controller_status(),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _production_readiness() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, level: str, detail: str, critical: bool = False) -> None:
        checks.append({"name": name, "level": level, "detail": detail, "critical": critical})

    database_check = state.database.probe()
    add("database", "ok" if database_check == "ok" else "error", database_check, True)
    vector = state.vectors.health()
    add(
        "vector_store",
        "ok" if vector.get("reachable") else "error",
        "Qdrant 可用" if vector.get("reachable") else str(vector.get("error") or vector.get("status") or "不可达"),
        True,
    )
    local_ai = state.ai.health()
    add(
        "local_ai",
        "ok" if local_ai.get("reachable") else "error",
        "本地模型端点可用" if local_ai.get("reachable") else "一个或多个本地模型端点不可达",
        True,
    )
    token_ok = len(settings.api_token) >= 32
    add(
        "authentication",
        "ok" if token_ok else "error",
        "访问控制已启用" if token_ok else "API Token 未设置或长度不足 32 位",
        True,
    )
    bootstrap_required = state.database.bootstrap_required()
    add(
        "bootstrap",
        "error" if bootstrap_required else "ok",
        "等待用户完成首次管理员设置" if bootstrap_required else "管理员账号已初始化",
        True,
    )
    disk = shutil.disk_usage(settings.data_dir)
    disk_ok = disk.free >= 2 * 1024**3 and (not disk.total or disk.free / disk.total >= 0.02)
    add("storage", "ok" if disk_ok else "error", f"可用 {disk.free / 1024**3:.1f} GiB", True)
    backup_directory = settings.data_dir / "backups"
    backups = sorted(backup_directory.glob("nas-ai-space-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    backup_age = time.time() - backups[0].stat().st_mtime if backups else None
    backup_ok = backup_age is not None and backup_age <= max(48, settings.automatic_backup_interval_hours * 2) * 3600
    add(
        "backup",
        "ok" if backup_ok else "warning",
        backups[0].name if backup_ok else "缺少 48 小时内的 SQLite 备份",
    )
    sensitive_paths = [
        settings.data_dir,
        backup_directory,
        settings.vector_backup_dir,
        settings.database_path,
        *(backups[:1]),
        *[
            settings.vector_backup_dir / item["name"]
            for item in state.vectors.list_snapshots()[:1]
        ],
    ]
    insecure_paths = [
        path.name or str(path)
        for path in sensitive_paths
        if path.exists() and path.stat().st_mode & 0o077
    ]
    add(
        "sensitive_permissions",
        "error" if insecure_paths else "ok",
        f"权限过宽：{', '.join(insecure_paths)}" if insecure_paths else "数据库与灾备文件仅限所有者访问",
        True,
    )
    if backups:
        marker = backups[0].with_suffix(backups[0].suffix + ".verified")
        marker_ok = marker.is_file() and marker.stat().st_mtime >= backups[0].stat().st_mtime
        add(
            "backup_verification",
            "ok" if marker_ok else "warning",
            "最新备份已通过 quick_check 与外键检查" if marker_ok else "最新备份来自旧版流程，尚无自动校验标记",
        )
    overview = _index_overview()
    if overview["terminal_failures"]:
        add("index_failures", "warning", f"{overview['terminal_failures']} 个文件已停止自动重试")
    else:
        add("index_failures", "ok", "没有终止重试的文件")
    if overview["caption_pending"]:
        add("caption_upgrades", "warning", f"{overview['caption_pending']} 张图片等待新版描述")
    else:
        add("caption_upgrades", "ok", "图片描述均为当前版本")
    controller = overview["controller"]
    controller_required = bool(
        overview["pending"] or overview["repairable"] or overview["retry_waiting"] or overview["caption_pending"]
    )
    add(
        "index_controller",
        "warning" if controller_required and controller.get("stale") else "ok",
        str(controller.get("message") or controller.get("state") or "状态未知"),
    )
    active = state.database.active_index_task()
    if active and active.get("heartbeat_at"):
        try:
            heartbeat = datetime.fromisoformat(str(active["heartbeat_at"]).replace("Z", "+00:00"))
            stale_task = (datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds() > 900
        except (TypeError, ValueError):
            stale_task = True
        add("task_heartbeat", "error" if stale_task else "ok", "任务心跳超过 15 分钟" if stale_task else "任务心跳正常", True)
    else:
        add("task_heartbeat", "ok", "当前没有运行中的索引任务")
    return {
        "ready": not any(item["critical"] and item["level"] == "error" for item in checks),
        "checks": checks,
        "version": app.version,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@app.get("/api/ready")
def readiness() -> JSONResponse:
    report = _production_readiness()
    return JSONResponse(report, status_code=200 if report["ready"] else 503)


@app.get("/api/auth/bootstrap")
def bootstrap_status() -> dict[str, bool]:
    return {"required": state.database.bootstrap_required()}


@app.post("/api/auth/bootstrap", status_code=201)
def bootstrap_owner(payload: BootstrapRequest, request: Request) -> dict[str, Any]:
    rate_key = _public_rate_key(request, "bootstrap")
    retry_after = _rate_retry_after(BOOTSTRAP_ATTEMPTS, PUBLIC_RATE_LIMIT_LOCK, rate_key, 5, 600)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"初始化尝试过于频繁，请在 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )
    _record_rate_attempt(BOOTSTRAP_ATTEMPTS, PUBLIC_RATE_LIMIT_LOCK, rate_key, 5, 600)
    username = payload.username.strip()
    if not username or any(character.isspace() for character in username):
        raise HTTPException(status_code=400, detail="用户名不能包含空格")
    try:
        user = state.database.complete_initial_setup(
            username,
            payload.display_name.strip(),
            hash_password(payload.password),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    token = session_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
    state.database.create_session(int(user["id"]), token_digest(token), expires_at)
    state.database.audit("auth.bootstrap", username, int(user["id"]), "user", str(user["id"]))
    return {**user, "token": token, "expires_at": expires_at, "user": user}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request) -> dict[str, Any]:
    key = _login_failure_key(request, payload.username.strip())
    retry_after = _login_retry_after(key)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请在 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )
    credentials = state.database.user_credentials(payload.username.strip())
    if (
        not credentials
        or not credentials["enabled"]
        or credentials["password_setup_required"]
        or not verify_password(payload.password, credentials["password_hash"])
    ):
        _register_login_failure(key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    with LOGIN_FAILURES_LOCK:
        LOGIN_FAILURES.pop(key, None)
    token = session_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
    state.database.create_session(int(credentials["id"]), token_digest(token), expires_at)
    state.database.audit("auth.login", credentials["username"], int(credentials["id"]), "user", str(credentials["id"]))
    user = state.database.get_user(int(credentials["id"])) or {}
    return {"token": token, "expires_at": expires_at, "user": user}


@app.get("/api/auth/me")
def auth_me(principal: Auth) -> dict[str, Any]:
    return {
        "id": principal["user_id"],
        "username": principal["username"],
        "display_name": principal["display_name"],
        "role": principal["role"],
        "library_ids": principal["library_ids"],
        "auth_type": principal["auth_type"],
    }


@app.post("/api/auth/logout")
def logout(
    principal: Auth,
    authorization: Annotated[Optional[str], Header()] = None,
) -> dict[str, bool]:
    if principal["auth_type"] == "session" and authorization and authorization.lower().startswith("bearer "):
        state.database.delete_session(token_digest(authorization[7:].strip()))
    return {"ok": True}


@app.get("/api/users")
def list_users(principal: Auth) -> list[dict[str, Any]]:
    _require_admin(principal)
    return state.database.list_users()


@app.post("/api/users", status_code=201)
def create_user(payload: UserCreate, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    role = payload.role if payload.role in {"admin", "member"} else "member"
    username = payload.username.strip()
    if not username or any(character.isspace() for character in username):
        raise HTTPException(status_code=400, detail="用户名不能包含空格")
    try:
        user = state.database.create_user(
            username,
            payload.display_name.strip(),
            hash_password(payload.password),
            role,
            payload.library_ids,
        )
    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(status_code=409, detail="用户名已存在") from exc
        raise
    _audit(principal, "user.create", "user", str(user["id"]), {"username": username, "role": role})
    return user


@app.put("/api/users/{user_id}")
def update_user(user_id: int, payload: UserUpdate, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    existing = state.database.get_user(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="用户不存在")
    if existing["role"] == "owner":
        if principal.get("user_id") not in {None, user_id}:
            raise HTTPException(status_code=403, detail="只有系统所有者可以修改自己的账号")
        if not payload.enabled:
            raise HTTPException(status_code=409, detail="不能停用系统所有者")
        role = "owner"
    else:
        role = payload.role if payload.role in {"admin", "member"} else "member"
    if principal.get("user_id") == user_id and (not payload.enabled or role != principal["role"]):
        raise HTTPException(status_code=409, detail="不能停用自己或修改自己的管理员角色")
    if payload.password and len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="密码至少 8 位")
    state.database.set_user(
        user_id,
        payload.display_name.strip(),
        role,
        payload.enabled,
        payload.library_ids,
        hash_password(payload.password) if payload.password else "",
    )
    _audit(principal, "user.update", "user", str(user_id), {"role": role, "enabled": payload.enabled})
    return state.database.get_user(user_id) or {}


@app.get("/api/system")
def system(principal: Auth) -> dict[str, Any]:
    result = {
        "hardware": detect_hardware().as_dict(),
        "metrics": runtime_metrics(),
        "local_ai": state.ai.health(),
        "vector_store": state.vectors.health(),
        "configuration": {
            "scan_root": str(settings.scan_root),
            "scan_roots": [str(root) for root in settings.scan_roots],
            "upload_root": str(settings.upload_root),
            "recycle_root": str(settings.recycle_root),
            "auth_enabled": bool(settings.api_token),
            "embedding_model": settings.embedding_model,
            "vision_model": settings.vision_model,
            "chat_model": settings.chat_model,
            "transcription_model": settings.transcription_model,
            "indexing": {
                "task_workers": len(state.tasks.workers),
                "index_workers": settings.index_workers or detect_hardware().plan.index_workers,
                "default_batch_size": settings.index_batch_size,
                "min_available_memory_bytes": settings.min_available_memory_bytes,
                "min_free_swap_bytes": settings.min_free_swap_bytes,
                "retry_max_attempts": settings.index_retry_max_attempts,
                "retry_base_seconds": settings.index_retry_base_seconds,
            },
            "maintenance": {
                "automatic_backup_enabled": settings.automatic_backup_enabled,
                "automatic_backup_interval_hours": settings.automatic_backup_interval_hours,
                "automatic_backup_retention": settings.automatic_backup_retention,
                "task_retention_days": settings.task_retention_days,
                "task_retention_count": settings.task_retention_count,
            },
            "people": {
                "available": settings.face_detection_model.is_file() and settings.face_recognition_model.is_file(),
                "backend": "OpenCV DNN",
                "match_threshold": settings.face_match_threshold,
            },
            "watcher": state.watcher.status(),
            "model_endpoints": {
                "embedding": settings.embedding_base_url,
                "vision": settings.vision_base_url,
                "chat": settings.chat_base_url,
                "transcription": settings.transcription_base_url,
            },
        },
    }
    if not _is_admin(principal):
        # 非管理员不暴露 NAS 绝对路径与内部模型端点等拓扑信息（前端算力卡片不依赖这些字段）
        configuration = result["configuration"]
        for key in ("scan_root", "scan_roots", "upload_root", "recycle_root", "model_endpoints"):
            configuration.pop(key, None)
        result["local_ai"].pop("endpoints", None)
    return result


@app.get("/api/system/metrics")
def system_metrics(_: Auth) -> dict[str, Any]:
    return runtime_metrics()


@app.get("/api/system/watcher")
def watcher_status(principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    return state.watcher.status()


@app.get("/api/dashboard")
def dashboard(principal: Auth) -> dict[str, Any]:
    result = state.database.dashboard(_library_ids(principal), None if _is_admin(principal) else principal["user_id"])
    if _is_admin(principal):
        result["indexing"] = _index_overview(result["files"])
    else:
        total = int(result["files"].get("total") or 0)
        semantic_ready = int(result["files"].get("semantic_ready") or 0)
        result["indexing"] = {
            "total": total,
            "semantic_ready": semantic_ready,
            "semantic_percent": round(semantic_ready / total * 100, 2) if total else 0.0,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    return result


@app.get("/api/projects")
def list_projects(principal: Auth) -> list[dict[str, Any]]:
    return state.workspaces.list_projects(
        int(principal["user_id"]) if principal.get("user_id") is not None else None,
        _is_admin(principal),
    )


@app.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreate, principal: Auth) -> dict[str, Any]:
    if principal.get("user_id") is None and principal.get("auth_type") != "local":
        raise HTTPException(status_code=409, detail="请使用本地账号创建项目")
    project = state.workspaces.create_project(
        payload.name,
        payload.description,
        payload.color,
        int(principal["user_id"]) if principal.get("user_id") is not None else None,
    )
    _audit(principal, "project.create", "project", str(project["id"]), {"name": project["name"]})
    return project


@app.get("/api/projects/{project_id}")
def project_details(project_id: int, principal: Auth) -> dict[str, Any]:
    project, role = _project_context(project_id, principal)
    return {
        "project": project,
        "access_role": role,
        "folders": state.workspaces.list_folders(project_id),
        "members": state.workspaces.list_members(project_id),
        "statuses": state.workspaces.list_statuses(project_id),
        "review_sessions": state.workspaces.list_review_sessions(project_id),
        "shares": state.workspaces.list_shares(project_id) if role in MANAGE_ROLES else [],
    }


@app.put("/api/projects/{project_id}")
def update_project(project_id: int, payload: ProjectUpdate, principal: Auth) -> dict[str, Any]:
    _project_context(project_id, principal, MANAGE_ROLES)
    project = state.workspaces.update_project(
        project_id,
        payload.name,
        payload.description,
        payload.color,
        payload.status,
    )
    _audit(principal, "project.update", "project", str(project_id), {"status": payload.status})
    return project


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: int, principal: Auth) -> dict[str, bool]:
    _project_context(project_id, principal, {"owner"})
    attachment_names = state.workspaces.project_attachment_names(project_id)
    state.workspaces.delete_project(project_id)
    _remove_comment_attachment_files(attachment_names)
    _audit(principal, "project.delete", "project", str(project_id))
    return {"ok": True}


@app.put("/api/projects/{project_id}/members")
def set_project_member(
    project_id: int,
    payload: ProjectMemberUpdate,
    principal: Auth,
) -> dict[str, Any]:
    _project_context(project_id, principal, MANAGE_ROLES)
    if not state.database.get_user(payload.user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    try:
        state.workspaces.set_member(project_id, payload.user_id, payload.role)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(
        principal,
        "project.member.set",
        "project",
        str(project_id),
        {"user_id": payload.user_id, "role": payload.role},
    )
    return {"items": state.workspaces.list_members(project_id)}


@app.delete("/api/projects/{project_id}/members/{user_id}")
def remove_project_member(project_id: int, user_id: int, principal: Auth) -> dict[str, bool]:
    _project_context(project_id, principal, MANAGE_ROLES)
    try:
        state.workspaces.remove_member(project_id, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(principal, "project.member.remove", "project", str(project_id), {"user_id": user_id})
    return {"ok": True}


@app.post("/api/projects/{project_id}/folders", status_code=201)
def create_project_folder(
    project_id: int,
    payload: ProjectFolderCreate,
    principal: Auth,
) -> dict[str, Any]:
    _project_context(project_id, principal, EDIT_ROLES)
    try:
        folder = state.workspaces.create_folder(project_id, payload.name, payload.parent_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(principal, "project.folder.create", "folder", str(folder["id"]), {"project_id": project_id})
    return folder


@app.delete("/api/projects/{project_id}/folders/{folder_id}")
def delete_project_folder(project_id: int, folder_id: int, principal: Auth) -> dict[str, bool]:
    _project_context(project_id, principal, EDIT_ROLES)
    state.workspaces.delete_folder(project_id, folder_id)
    _audit(principal, "project.folder.delete", "folder", str(folder_id), {"project_id": project_id})
    return {"ok": True}


@app.get("/api/projects/{project_id}/inbox")
def project_inbox_status(project_id: int, principal: Auth) -> dict[str, Any]:
    _project_context(project_id, principal)
    try:
        inbox = project_inbox(settings, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    prefix = str(inbox) + "/"
    discovered = state.database.fetchone(
        "SELECT COUNT(*) AS count FROM files WHERE path LIKE ?",
        (f"{prefix}%",),
    ) or {"count": 0}
    collected = state.database.fetchone(
        """SELECT COUNT(DISTINCT av.file_id) AS count FROM asset_versions av
           JOIN assets a ON a.id = av.asset_id JOIN files f ON f.id = av.file_id
           WHERE a.project_id = ? AND f.path LIKE ?""",
        (project_id, f"{prefix}%"),
    ) or {"count": 0}
    active = state.database.fetchone(
        """SELECT id, status, progress, message FROM tasks WHERE type = 'collect_project_inbox'
           AND status IN ('pending', 'running')
           AND json_extract(payload_json, '$.project_id') = ?
           ORDER BY id DESC LIMIT 1""",
        (project_id,),
    )
    return {
        "relative_path": f"inbox/project-{project_id}",
        "container_path": str(inbox),
        "discovered_files": int(discovered["count"]),
        "collected_files": int(collected["count"]),
        "active_task": active,
    }


@app.post("/api/projects/{project_id}/inbox/collect", status_code=202)
async def collect_project_inbox_now(project_id: int, principal: Auth) -> dict[str, Any]:
    _project_context(project_id, principal, EDIT_ROLES)
    try:
        project_inbox(settings, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    existing = state.database.fetchone(
        """SELECT id FROM tasks WHERE type = 'collect_project_inbox'
           AND status IN ('pending', 'running')
           AND json_extract(payload_json, '$.project_id') = ?
           ORDER BY id DESC LIMIT 1""",
        (project_id,),
    )
    if existing:
        return {"task_id": int(existing["id"]), "existing": True}
    task_id = await state.tasks.submit(
        "collect_project_inbox",
        {
            "project_id": project_id,
            "user_id": int(principal["user_id"]) if principal.get("user_id") is not None else None,
        },
        priority=9,
        user_id=principal.get("user_id"),
    )
    _audit(principal, "project.inbox.collect", "project", str(project_id), {"task_id": task_id})
    return {"task_id": task_id, "existing": False}


@app.put("/api/projects/{project_id}/statuses")
def update_project_statuses(
    project_id: int,
    payload: ProjectStatusesUpdate,
    principal: Auth,
) -> dict[str, Any]:
    _project_context(project_id, principal, MANAGE_ROLES)
    try:
        items = state.workspaces.set_statuses(project_id, payload.items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(principal, "project.statuses.update", "project", str(project_id), {"count": len(items)})
    return {"items": items}


@app.get("/api/projects/{project_id}/assets")
def list_project_assets(
    project_id: int,
    principal: Auth,
    folder_id: Optional[int] = None,
    status: str = "",
    q: str = "",
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    _project_context(project_id, principal)
    return state.workspaces.list_assets(project_id, folder_id, status, q, limit, offset)


@app.post("/api/projects/{project_id}/assets", status_code=201)
def create_project_asset(
    project_id: int,
    payload: AssetCreate,
    principal: Auth,
) -> dict[str, Any]:
    _project_context(project_id, principal, EDIT_ROLES)
    file = _visible_file(payload.file_id, principal)
    try:
        asset = state.workspaces.create_asset(
            project_id,
            file,
            payload.folder_id,
            payload.title,
            int(principal["user_id"]) if principal.get("user_id") is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(
        principal,
        "asset.create",
        "asset",
        str(asset["id"]),
        {"project_id": project_id, "file_id": payload.file_id},
    )
    return asset


@app.get("/api/assets/{asset_id}")
def asset_details(asset_id: int, principal: Auth) -> dict[str, Any]:
    asset, role = _asset_context(asset_id, principal)
    for comment in asset["comments"]:
        comment["attachments"] = [
            payload
            for attachment in comment.get("attachments", [])
            if (payload := _comment_attachment_payload(attachment))
        ]
    asset["access_role"] = role
    asset["statuses"] = state.workspaces.list_statuses(int(asset["project_id"]))
    asset["members"] = state.workspaces.list_members(int(asset["project_id"]))
    return asset


@app.put("/api/assets/{asset_id}")
def update_asset(asset_id: int, payload: AssetUpdate, principal: Auth) -> dict[str, Any]:
    asset, _ = _asset_context(asset_id, principal, EDIT_ROLES)
    if payload.assignee_id is not None and not state.database.fetchone(
        "SELECT 1 AS value FROM project_members WHERE project_id = ? AND user_id = ?",
        (asset["project_id"], payload.assignee_id),
    ):
        raise HTTPException(status_code=409, detail="负责人不是项目成员")
    try:
        updated = state.workspaces.update_asset(asset_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(principal, "asset.update", "asset", str(asset_id), {"status": payload.status})
    return updated


@app.delete("/api/assets/{asset_id}")
def delete_asset(asset_id: int, principal: Auth) -> dict[str, bool]:
    asset, _ = _asset_context(asset_id, principal, EDIT_ROLES)
    attachment_names = [
        str(attachment["name"])
        for comment in asset["comments"]
        for attachment in comment.get("attachments", [])
    ]
    state.workspaces.delete_asset(asset_id)
    _remove_comment_attachment_files(attachment_names)
    _audit(principal, "asset.delete", "asset", str(asset_id))
    return {"ok": True}


@app.post("/api/assets/{asset_id}/versions", status_code=201)
def add_asset_version(
    asset_id: int,
    payload: AssetVersionCreate,
    principal: Auth,
) -> dict[str, Any]:
    _asset_context(asset_id, principal, EDIT_ROLES)
    file = _visible_file(payload.file_id, principal)
    try:
        version = state.workspaces.add_version(
            asset_id,
            file,
            payload.label,
            payload.notes,
            int(principal["user_id"]) if principal.get("user_id") is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(
        principal,
        "asset.version.create",
        "asset_version",
        str(version["id"]),
        {"asset_id": asset_id, "file_id": payload.file_id},
    )
    return version


@app.post("/api/asset-versions/{version_id}/proxy", status_code=202)
async def create_asset_proxy(version_id: int, principal: Auth) -> dict[str, Any]:
    version, _, _ = _version_context(version_id, principal, EDIT_ROLES)
    if version["kind"] not in {"video", "audio"}:
        raise HTTPException(status_code=409, detail="该素材类型不需要代理媒体")
    if version["proxy_status"] == "processing":
        existing = state.database.fetchone(
            """SELECT id FROM tasks WHERE type = 'generate_proxy' AND status IN ('pending', 'running')
               AND json_extract(payload_json, '$.version_id') = ? ORDER BY id DESC LIMIT 1""",
            (version_id,),
        )
        return {"task_id": int(existing["id"]) if existing else None, "existing": True}
    task_id = await state.tasks.submit(
        "generate_proxy",
        {"version_id": version_id},
        priority=8,
        user_id=principal.get("user_id"),
    )
    state.database.execute(
        "UPDATE asset_versions SET proxy_status = 'processing', proxy_error = '' WHERE id = ?",
        (version_id,),
    )
    _audit(principal, "asset.proxy.submit", "asset_version", str(version_id), {"task_id": task_id})
    return {"task_id": task_id, "existing": False}


@app.post("/api/asset-versions/{version_id}/look-preview", status_code=202)
async def create_asset_look_preview(
    version_id: int,
    payload: LookPreviewCreate,
    principal: Auth,
) -> dict[str, Any]:
    version, _, _ = _version_context(version_id, principal, EDIT_ROLES)
    if version["kind"] not in {"image", "video"}:
        raise HTTPException(status_code=409, detail="LUT 预览仅支持图片和视频")
    lut_file = _visible_file(payload.lut_file_id, principal)
    if str(lut_file.get("extension") or "").lower() != ".cube":
        raise HTTPException(status_code=409, detail="请选择 .cube 格式的 3D LUT 文件")
    existing = state.database.fetchone(
        """SELECT id FROM tasks WHERE type = 'generate_look_preview'
           AND status IN ('pending', 'running')
           AND json_extract(payload_json, '$.version_id') = ?
           ORDER BY id DESC LIMIT 1""",
        (version_id,),
    )
    if existing:
        return {"task_id": int(existing["id"]), "existing": True}
    task_id = await state.tasks.submit(
        "generate_look_preview",
        {"version_id": version_id, "lut_file_id": payload.lut_file_id},
        priority=8,
        user_id=principal.get("user_id"),
    )
    state.database.execute(
        """UPDATE asset_versions SET look_status = 'processing', look_name = ?,
           look_error = '' WHERE id = ?""",
        (str(lut_file["name"])[:240], version_id),
    )
    _audit(
        principal,
        "asset.look.submit",
        "asset_version",
        str(version_id),
        {"task_id": task_id, "lut_file_id": payload.lut_file_id},
    )
    return {"task_id": task_id, "existing": False}


@app.post("/api/asset-versions/{version_id}/ticket")
def asset_version_ticket(
    version_id: int,
    principal: Auth,
    variant: str = Query(default="best", pattern=r"^(best|original|proxy|poster|filmstrip|waveform|look)$"),
    download: bool = False,
) -> dict[str, Any]:
    version, _, _ = _version_context(version_id, principal)
    file = state.database.get_file(int(version["file_id"])) if version.get("file_id") else None
    paths = {
        "proxy": str(version.get("proxy_path") or ""),
        "poster": str(version.get("poster_path") or ""),
        "filmstrip": str(version.get("filmstrip_path") or ""),
        "waveform": str(version.get("waveform_path") or ""),
        "look": str(version.get("look_path") or ""),
        "original": str(file.get("path") or "") if file else "",
    }
    selected = variant
    if variant == "best":
        selected = "proxy" if paths["proxy"] and Path(paths["proxy"]).is_file() else "original"
    path = paths.get(selected, "")
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="所选素材文件尚未生成或已经离线")
    mime = {
        "proxy": "video/mp4" if version["kind"] == "video" else "audio/mp4",
        "poster": "image/jpeg",
        "filmstrip": "image/jpeg",
        "waveform": "image/png",
        "look": "video/mp4" if version["kind"] == "video" else "image/jpeg",
    }.get(selected, str(file.get("mime_type") or version.get("mime_type") or "") if file else "")
    filename = Path(path).name if selected != "original" else str(version["file_name"])
    return {
        "url": _workspace_ticket(path, mime, filename, download and selected == "original"),
        "variant": selected,
        "expires_in": 3600,
    }


@app.post("/api/assets/{asset_id}/comments", status_code=201)
def create_review_comment(
    asset_id: int,
    payload: ReviewCommentCreate,
    principal: Auth,
) -> dict[str, Any]:
    _asset_context(asset_id, principal, COMMENT_ROLES)
    if payload.time_end is not None and payload.time_start is not None and payload.time_end < payload.time_start:
        raise HTTPException(status_code=400, detail="结束时间不能早于开始时间")
    try:
        comment = state.workspaces.add_comment(
            asset_id,
            payload.version_id,
            payload.body,
            payload.comment_type,
            payload.time_start,
            payload.time_end,
            payload.x,
            payload.y,
            payload.drawing,
            payload.visibility,
            int(principal["user_id"]) if principal.get("user_id") is not None else None,
            review_session_id=payload.review_session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(principal, "review.comment.create", "comment", str(comment["id"]), {"asset_id": asset_id})
    return comment


@app.put("/api/comments/{comment_id}/resolve")
def resolve_review_comment(
    comment_id: int,
    payload: CommentResolve,
    principal: Auth,
) -> dict[str, Any]:
    comment = state.workspaces.comment(comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="审阅意见不存在")
    _asset_context(int(comment["asset_id"]), principal, COMMENT_ROLES)
    resolved = state.workspaces.resolve_comment(
        comment_id,
        int(principal["user_id"]) if principal.get("user_id") is not None else None,
        payload.resolved,
    )
    _audit(principal, "review.comment.resolve", "comment", str(comment_id), {"resolved": payload.resolved})
    return resolved


@app.post("/api/comments/{comment_id}/attachments", status_code=201)
async def upload_comment_attachment(
    comment_id: int,
    request: Request,
    principal: Auth,
    x_filename: Annotated[Optional[str], Header()] = None,
) -> dict[str, Any]:
    comment = state.workspaces.comment(comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="审阅意见不存在")
    _, role = _asset_context(int(comment["asset_id"]), principal, COMMENT_ROLES)
    user_id = int(principal["user_id"]) if principal.get("user_id") is not None else None
    if role not in MANAGE_ROLES and (user_id is None or comment.get("user_id") != user_id):
        raise HTTPException(status_code=403, detail="只能为自己发表的审阅意见添加附件")
    rate_key = f"attachment:{user_id if user_id is not None else _actor(principal)}"
    retry_after = _rate_retry_after(AUTH_ATTACHMENT_ATTEMPTS, PUBLIC_RATE_LIMIT_LOCK, rate_key, 120, 3600)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"附件上传过于频繁，请在 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )
    _record_rate_attempt(AUTH_ATTACHMENT_ATTEMPTS, PUBLIC_RATE_LIMIT_LOCK, rate_key, 120, 3600)
    attachment = await _save_comment_attachment(request, comment_id, x_filename or "")
    _audit(
        principal,
        "review.comment.attachment",
        "comment",
        str(comment_id),
        {"name": attachment["original_name"], "size": attachment["size_bytes"]},
    )
    return attachment


@app.delete("/api/comments/{comment_id}")
def delete_review_comment(comment_id: int, principal: Auth) -> dict[str, bool]:
    comment = state.workspaces.comment(comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="审阅意见不存在")
    _, role = _asset_context(int(comment["asset_id"]), principal, COMMENT_ROLES)
    user_id = int(principal["user_id"]) if principal.get("user_id") is not None else None
    if role not in MANAGE_ROLES and (user_id is None or comment.get("user_id") != user_id):
        raise HTTPException(status_code=403, detail="只能删除自己发表的审阅意见")
    attachment_names = state.workspaces.delete_comment(comment_id)
    _remove_comment_attachment_files(attachment_names)
    _audit(principal, "review.comment.delete", "comment", str(comment_id))
    return {"ok": True}


@app.get("/api/projects/{project_id}/review-tasks")
def project_review_tasks(project_id: int, principal: Auth) -> dict[str, Any]:
    _project_context(project_id, principal)
    rows = state.database.fetchall(
        """SELECT rc.id, rc.asset_id, rc.version_id, rc.body, rc.comment_type, rc.time_start,
           rc.time_end, rc.created_at, a.title AS asset_title,
           COALESCE(u.display_name, rc.guest_name, '访客') AS author
           FROM review_comments rc JOIN assets a ON a.id = rc.asset_id
           LEFT JOIN users u ON u.id = rc.user_id
           WHERE a.project_id = ? AND rc.resolved = 0 ORDER BY a.updated_at DESC, rc.created_at""",
        (project_id,),
    )
    return {"total": len(rows), "items": rows}


@app.post("/api/projects/{project_id}/review-sessions", status_code=201)
def create_review_session(
    project_id: int,
    payload: ReviewSessionCreate,
    principal: Auth,
) -> dict[str, Any]:
    _project_context(project_id, principal, COMMENT_ROLES)
    session = state.workspaces.create_review_session(
        project_id,
        payload.name,
        int(principal["user_id"]) if principal.get("user_id") is not None else None,
    )
    _audit(principal, "review.session.create", "review_session", str(session["id"]))
    return session


@app.post("/api/review-sessions/{session_id}/close")
def close_review_session(session_id: int, principal: Auth) -> dict[str, bool]:
    session = state.database.fetchone("SELECT * FROM review_sessions WHERE id = ?", (session_id,))
    if not session:
        raise HTTPException(status_code=404, detail="审阅会话不存在")
    _project_context(int(session["project_id"]), principal, MANAGE_ROLES)
    state.workspaces.close_review_session(session_id)
    _audit(principal, "review.session.close", "review_session", str(session_id))
    return {"ok": True}


@app.get("/api/assets/{asset_id}/qc")
def asset_quality_control(asset_id: int, principal: Auth) -> dict[str, Any]:
    asset, _ = _asset_context(asset_id, principal)
    current = next(
        (item for item in asset["versions"] if int(item["id"]) == int(asset.get("cover_version_id") or 0)),
        asset["versions"][0] if asset["versions"] else None,
    )
    checks: list[dict[str, str]] = []

    def add(name: str, level: str, detail: str) -> None:
        checks.append({"name": name, "level": level, "detail": detail})

    if not current:
        add("版本", "error", "素材没有任何版本")
    else:
        add("原文件", "ok" if current.get("file_id") else "error", "文件在线" if current.get("file_id") else "原文件离线")
        add(
            "AI 索引",
            "ok" if current.get("index_status") == "ready" else "warning",
            f"索引状态：{current.get('index_status') or '未知'}",
        )
        if current["kind"] in {"video", "audio"}:
            add(
                "代理媒体",
                "ok" if current.get("proxy_status") == "ready" else "warning",
                "代理媒体可用于流畅审阅" if current.get("proxy_status") == "ready" else "尚未生成代理媒体",
            )
        if current["kind"] == "video":
            add(
                "媒体时长",
                "ok" if float(current.get("duration") or 0) > 0 else "error",
                f"{float(current.get('duration') or 0):.2f} 秒" if current.get("duration") else "无法读取视频时长",
            )
            pixels = int(current.get("width") or 0) * int(current.get("height") or 0)
            add(
                "画面尺寸",
                "ok" if pixels >= 1280 * 720 else "warning",
                f"{current.get('width') or 0} × {current.get('height') or 0}",
            )
        add(
            "内容描述",
            "ok" if str(current.get("caption") or "").strip() else "warning",
            "已有可检索描述" if current.get("caption") else "缺少内容描述",
        )
    open_comments = [item for item in asset["comments"] if not item["resolved"]]
    add(
        "未解决意见",
        "ok" if not open_comments else "warning",
        "没有待处理意见" if not open_comments else f"{len(open_comments)} 条意见尚未解决",
    )
    return {
        "passed": not any(item["level"] == "error" for item in checks),
        "checks": checks,
        "open_comments": len(open_comments),
    }


@app.get("/api/assets/{asset_id}/review-brief")
def asset_review_brief(asset_id: int, principal: Auth) -> dict[str, Any]:
    asset, _ = _asset_context(asset_id, principal)
    sources: list[dict[str, Any]] = []
    for version in asset["versions"][:8]:
        evidence = str(version.get("caption") or version.get("notes") or "").strip()
        if evidence:
            sources.append({
                "path": f"{asset['title']} / V{version['version_number']}",
                "confidence": 1,
                "evidence": evidence,
            })
    for comment in asset["comments"][-30:]:
        sources.append({
            "path": f"审阅意见 / {comment.get('display_name') or comment.get('guest_name') or '成员'}",
            "confidence": 1,
            "evidence": str(comment["body"]),
        })
    if not sources:
        return {"brief": "当前素材还没有足够的版本描述或审阅意见。", "source_count": 0}
    question = (
        "请生成简洁的素材审阅摘要：先概括当前内容，再按未解决问题、版本变化、下一步修改清单分组。"
        "只依据证据，不要猜测。"
    )
    try:
        brief = state.ai.answer(question, sources)
    except RuntimeError:
        unresolved = [item["body"] for item in asset["comments"] if not item["resolved"]]
        brief = (
            f"当前素材共有 {len(asset['versions'])} 个版本、{len(unresolved)} 条未解决意见。"
            + (f"\n修改清单：\n- " + "\n- ".join(unresolved[:12]) if unresolved else "\n当前没有待处理审阅意见。")
        )
    return {"brief": brief, "source_count": len(sources)}


@app.get("/api/projects/{project_id}/review-export")
def export_project_review(
    project_id: int,
    principal: Auth,
    format: str = Query(default="csv", pattern=r"^(csv|fcpxml)$"),
    scope: str = Query(default="all", pattern=r"^(all|external|team)$"),
) -> Response:
    project, _ = _project_context(project_id, principal)
    clauses = ["a.project_id = ?"]
    params: list[Any] = [project_id]
    if scope != "all":
        clauses.append("rc.visibility = ?")
        params.append(scope)
    rows = state.database.fetchall(
        f"""SELECT a.title, av.file_name, av.version_number, rc.body, rc.time_start, rc.time_end,
           rc.resolved, rc.visibility, COALESCE(u.display_name, rc.guest_name, '访客') AS author, rc.created_at
           FROM review_comments rc JOIN assets a ON a.id = rc.asset_id
           LEFT JOIN asset_versions av ON av.id = rc.version_id
           LEFT JOIN users u ON u.id = rc.user_id
           WHERE {" AND ".join(clauses)} ORDER BY a.id, rc.time_start, rc.id""",
        params,
    )
    safe_name = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", str(project["name"])).strip("-") or "project"
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["素材", "文件", "版本", "开始秒", "结束秒", "意见", "作者", "可见范围", "已解决", "创建时间"])
        for row in rows:
            writer.writerow([
                row["title"], row["file_name"] or "", row["version_number"] or "",
                row["time_start"] if row["time_start"] is not None else "",
                row["time_end"] if row["time_end"] is not None else "",
                row["body"], row["author"],
                "外部可见" if row["visibility"] == "external" else "团队内部",
                "是" if row["resolved"] else "否", row["created_at"],
            ])
        return Response(
            "\ufeff" + output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=project-review.csv; "
                    f"filename*=UTF-8''{quote(f'{safe_name}-review.csv')}"
                )
            },
        )
    markers = []
    for row in rows:
        start = max(0.0, float(row["time_start"] or 0))
        duration = max(0.04, float(row["time_end"] or start) - start)
        prefix = "" if row["visibility"] == "external" else "【团队内部】"
        value = html.escape(f"{prefix}{row['title']} · {row['author']}：{row['body']}", quote=True)
        markers.append(f'<marker start="{start:.3f}s" duration="{duration:.3f}s" value="{value}"/>')
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<fcpxml version="1.10"><resources/>'
        f'<library><event name="{html.escape(str(project["name"]), quote=True)}">'
        f'<project name="{html.escape(str(project["name"]), quote=True)}"><sequence duration="0s"><spine>'
        f'<gap name="Review Markers" offset="0s" start="0s" duration="0s">{"".join(markers)}</gap>'
        '</spine></sequence></project></event></library></fcpxml>'
    )
    return Response(
        xml,
        media_type="application/xml",
        headers={
            "Content-Disposition": (
                f"attachment; filename=project-review.fcpxml; "
                f"filename*=UTF-8''{quote(f'{safe_name}-review.fcpxml')}"
            )
        },
    )


@app.post("/api/projects/{project_id}/shares", status_code=201)
def create_share_link(
    project_id: int,
    payload: ShareCreate,
    principal: Auth,
) -> dict[str, Any]:
    _project_context(project_id, principal, MANAGE_ROLES)
    if payload.access_code and len(payload.access_code) < 6:
        raise HTTPException(status_code=400, detail="访问码至少 6 位，留空则不启用访问码")
    expires_at = payload.expires_at
    if expires_at:
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                raise ValueError
            expires_at = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="分享过期时间无效") from exc
    try:
        share, token = state.workspaces.create_share(
            project_id,
            payload.asset_id,
            payload.name,
            hash_password(payload.access_code) if payload.access_code else "",
            expires_at,
            payload.can_download,
            payload.can_comment,
            payload.can_view_versions,
            payload.watermark_text,
            payload.brand_name,
            int(principal["user_id"]) if principal.get("user_id") is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(principal, "share.create", "share", str(share["id"]), {"project_id": project_id})
    return {**share, "url": f"/share/{token}", "token": token}


@app.put("/api/shares/{share_id}/enabled")
def set_share_enabled(share_id: int, enabled: bool, principal: Auth) -> dict[str, bool]:
    share = state.workspaces.share(share_id)
    if not share:
        raise HTTPException(status_code=404, detail="分享不存在")
    _project_context(int(share["project_id"]), principal, MANAGE_ROLES)
    state.workspaces.set_share_enabled(share_id, enabled)
    _audit(principal, "share.enabled", "share", str(share_id), {"enabled": enabled})
    return {"ok": True}


@app.post("/api/public/shares/{token}")
def public_share(token: str, payload: PublicShareAccess, request: Request) -> dict[str, Any]:
    rate_key = _public_rate_key(request, token)
    retry_after = _rate_retry_after(
        PUBLIC_ACCESS_FAILURES,
        PUBLIC_RATE_LIMIT_LOCK,
        rate_key,
        10,
        900,
    )
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"访问码尝试次数过多，请在 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        share = _share_context(token, payload.access_code)
    except HTTPException as exc:
        if exc.status_code == 401:
            _record_rate_attempt(
                PUBLIC_ACCESS_FAILURES,
                PUBLIC_RATE_LIMIT_LOCK,
                rate_key,
                10,
                900,
            )
        raise
    _clear_rate_attempts(PUBLIC_ACCESS_FAILURES, PUBLIC_RATE_LIMIT_LOCK, rate_key)
    if share.get("asset_id"):
        assets = [_public_asset_payload(int(share["asset_id"]), share)]
    else:
        items = state.workspaces.list_assets(int(share["project_id"]), limit=200)["items"]
        assets = [_public_asset_payload(int(item["id"]), share) for item in items]
    return {
        "share": {
            key: share.get(key)
            for key in (
                "id", "name", "project_name", "project_description", "asset_id",
                "can_download", "can_comment", "can_view_versions", "watermark_text",
                "brand_name", "expires_at",
            )
        },
        "assets": assets,
    }


@app.post("/api/public/shares/{token}/comments", status_code=201)
def public_share_comment(
    token: str,
    payload: PublicReviewComment,
    request: Request,
) -> dict[str, Any]:
    rate_key = _public_rate_key(request, token)
    retry_after = _rate_retry_after(
        PUBLIC_COMMENT_ATTEMPTS,
        PUBLIC_RATE_LIMIT_LOCK,
        rate_key,
        30,
        3600,
    )
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"外部评论提交过于频繁，请在 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        share = _share_context(token, payload.access_code)
    except HTTPException as exc:
        if exc.status_code == 401:
            _record_rate_attempt(
                PUBLIC_ACCESS_FAILURES,
                PUBLIC_RATE_LIMIT_LOCK,
                rate_key,
                10,
                900,
            )
        raise
    if not share["can_comment"]:
        raise HTTPException(status_code=403, detail="该分享不允许评论")
    if share.get("asset_id") and int(share["asset_id"]) != payload.asset_id:
        raise HTTPException(status_code=404, detail="分享素材不存在")
    asset = state.workspaces.asset_detail(payload.asset_id)
    if not asset or int(asset["project_id"]) != int(share["project_id"]):
        raise HTTPException(status_code=404, detail="分享素材不存在")
    _record_rate_attempt(
        PUBLIC_COMMENT_ATTEMPTS,
        PUBLIC_RATE_LIMIT_LOCK,
        rate_key,
        30,
        3600,
    )
    try:
        return state.workspaces.add_comment(
            payload.asset_id,
            payload.version_id,
            payload.body,
            "range" if payload.time_end is not None else "point" if payload.time_start is not None else "text",
            payload.time_start,
            payload.time_end,
            None,
            None,
            [],
            "external",
            None,
            payload.guest_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/public/shares/{token}/comments/{comment_id}/attachments", status_code=201)
async def public_share_comment_attachment(
    token: str,
    comment_id: int,
    request: Request,
    access_code: str = Query(default="", max_length=120),
    x_filename: Annotated[Optional[str], Header()] = None,
) -> dict[str, Any]:
    rate_key = _public_rate_key(request, token)
    retry_after = _rate_retry_after(
        PUBLIC_ATTACHMENT_ATTEMPTS,
        PUBLIC_RATE_LIMIT_LOCK,
        rate_key,
        60,
        3600,
    )
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"附件上传过于频繁，请在 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        share = _share_context(token, access_code)
    except HTTPException as exc:
        if exc.status_code == 401:
            _record_rate_attempt(
                PUBLIC_ACCESS_FAILURES,
                PUBLIC_RATE_LIMIT_LOCK,
                rate_key,
                10,
                900,
            )
        raise
    if not share["can_comment"]:
        raise HTTPException(status_code=403, detail="该分享不允许评论")
    comment = state.workspaces.comment(comment_id)
    if (
        not comment
        or comment.get("visibility") != "external"
        or comment.get("user_id") is not None
        or not str(comment.get("guest_name") or "").strip()
    ):
        raise HTTPException(status_code=404, detail="审阅意见不存在")
    asset = state.database.fetchone(
        "SELECT project_id FROM assets WHERE id = ?",
        (int(comment["asset_id"]),),
    )
    if not asset or int(asset["project_id"]) != int(share["project_id"]):
        raise HTTPException(status_code=404, detail="审阅意见不存在")
    if share.get("asset_id") and int(share["asset_id"]) != int(comment["asset_id"]):
        raise HTTPException(status_code=404, detail="审阅意见不存在")
    _record_rate_attempt(
        PUBLIC_ATTACHMENT_ATTEMPTS,
        PUBLIC_RATE_LIMIT_LOCK,
        rate_key,
        60,
        3600,
    )
    return await _save_comment_attachment(request, comment_id, x_filename or "")


@app.get("/api/notifications")
def list_notifications(
    principal: Auth,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    user_id = _personal_user_id(principal)
    items = state.workspaces.list_notifications(user_id, limit)
    return {"unread": state.workspaces.unread_notifications(user_id), "items": items}


@app.post("/api/notifications/read")
def read_notifications(
    principal: Auth,
    notification_id: Optional[int] = None,
) -> dict[str, bool]:
    state.workspaces.read_notifications(_personal_user_id(principal), notification_id)
    return {"ok": True}


@app.get("/api/workspace-media/{ticket}")
def workspace_media(ticket: str) -> FileResponse:
    with WORKSPACE_TICKETS_LOCK:
        entry = state.workspace_tickets.get(ticket)
        if not entry or entry[3] <= time.monotonic():
            state.workspace_tickets.pop(ticket, None)
            entry = None
    if not entry:
        raise HTTPException(status_code=404, detail="媒体访问链接已失效")
    # 兼容旧的 5 元组（无分享来源），新票据为 6 元组
    path, mime_type, filename, _, download = entry[:5]
    share_token = str(entry[5]) if len(entry) > 5 else ""
    if share_token and not state.workspaces.share_by_token(share_token):
        raise HTTPException(status_code=403, detail="分享已关闭或过期")
    if not Path(path).is_file():
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    return FileResponse(
        path,
        media_type=mime_type,
        filename=filename,
        content_disposition_type="attachment" if download else "inline",
    )


@app.get("/api/libraries")
def list_libraries(principal: Auth) -> list[dict[str, Any]]:
    rows = state.database.list_libraries()
    return [row for row in rows if _allowed_library(principal, int(row["id"]))]


@app.post("/api/libraries", status_code=201)
def create_library(payload: LibraryCreate, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    path = _safe_library_path(payload.path)
    try:
        library = state.database.create_library(payload.name.strip(), str(path))
        state.watcher.refresh()
        _audit(principal, "library.create", "library", str(library["id"]), {"path": str(path)})
        return library
    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(status_code=409, detail="该目录已经添加") from exc
        raise


@app.delete("/api/libraries/{library_id}")
def delete_library(library_id: int, principal: Auth) -> dict[str, bool]:
    _require_admin(principal)
    library = _visible_library(library_id, principal)
    if Path(library["path"]) == settings.upload_root:
        raise HTTPException(status_code=409, detail="上传空间不能删除")
    try:
        state.vectors.delete_library(library_id)
    except Exception as exc:
        logger.warning("删除媒体库 %s 的向量数据失败：%s", library_id, exc)
    state.database.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
    state.watcher.refresh()
    _audit(principal, "library.delete", "library", str(library_id))
    return {"ok": True}


@app.post("/api/libraries/{library_id}/scan", status_code=202)
async def scan_library(library_id: int, principal: Auth) -> dict[str, int]:
    _require_admin(principal)
    _visible_library(library_id, principal)
    task_id = await state.tasks.submit(
        "scan_library", {"library_id": library_id}, priority=10, user_id=principal["user_id"]
    )
    _audit(principal, "library.scan", "library", str(library_id), {"task_id": task_id})
    return {"task_id": task_id}


@app.post("/api/libraries/{library_id}/discover", status_code=202)
async def discover_library(library_id: int, principal: Auth) -> dict[str, int]:
    _require_admin(principal)
    _visible_library(library_id, principal)
    task_id = await state.tasks.submit(
        "scan_only", {"library_id": library_id}, priority=10, user_id=principal["user_id"]
    )
    _audit(principal, "library.discover", "library", str(library_id), {"task_id": task_id})
    return {"task_id": task_id}


@app.get("/api/index/status")
def index_status(principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    memory = memory_runtime()
    # stages 与 caption_pending 复用 overview 已算结果，同一请求不再重复聚合
    stages = state.database.index_stage_summary()
    overview = _index_overview(stages=stages)
    return {
        "pending": state.database.pending_summary(),
        "stages": stages,
        "caption_upgrades": {
            "version": 3,
            "pending": overview["caption_pending"],
        },
        "policy": state.tasks.index_policy(),
        "active": overview["active"],
        "active_tasks": state.database.active_task_count(),
        "overview": overview,
        "resources": {
            "available_memory_bytes": memory["available_bytes"],
            "minimum_memory_bytes": settings.min_available_memory_bytes,
            "free_swap_bytes": max(0, memory["swap_total_bytes"] - memory["swap_used_bytes"]),
            "minimum_free_swap_bytes": settings.min_free_swap_bytes,
            "index_workers": settings.index_workers or detect_hardware().plan.index_workers,
            "default_batch_size": settings.index_batch_size,
        },
    }


@app.post("/api/index/controller/report")
def update_index_controller(report: IndexControllerReport, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    if report.state not in {"idle", "running", "recycling", "waiting", "complete", "degraded", "error"}:
        raise HTTPException(status_code=400, detail="不支持的调度器状态")
    values = {
        **report.model_dump(),
        "reported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    state.database.set_setting("index_controller_report", values)
    return values


@app.post("/api/index", status_code=202)
async def index_pending(principal: Auth, payload: Optional[IndexRequest] = None) -> dict[str, Any]:
    _require_admin(principal)
    options = payload or IndexRequest(limit=settings.index_batch_size)
    _validate_index_options(options.library_id, options.kind, options.order, principal)
    task_id, existing = await state.tasks.submit_unique(
        "index_pending",
        options.model_dump(),
        priority=5,
        user_id=principal["user_id"],
    )
    _audit(principal, "index.submit", "task", str(task_id), {**options.model_dump(), "existing": existing})
    return {"task_id": task_id, "existing": existing}


@app.post("/api/index/repair", status_code=202)
async def repair_index(principal: Auth, payload: Optional[CaptionUpgradeRequest] = None) -> dict[str, Any]:
    _require_admin(principal)
    options = payload or CaptionUpgradeRequest(limit=50)
    task_id, existing = await state.tasks.submit_unique(
        "repair_index",
        {"limit": options.limit, "source": "manual"},
        priority=7,
        user_id=principal["user_id"],
    )
    _audit(principal, "index.repair", "task", str(task_id), {"limit": options.limit, "existing": existing})
    return {"task_id": task_id, "existing": existing}


@app.put("/api/index/policy")
def update_index_policy(payload: IndexPolicyUpdate, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    values = payload.model_dump()
    _validate_index_options(payload.library_id, payload.kind, payload.order, principal)
    policy = state.tasks.set_index_policy(values)
    _audit(principal, "index.policy", "settings", "index_policy", policy)
    return policy


@app.post("/api/reindex", status_code=202)
async def reindex_all(principal: Auth) -> dict[str, int]:
    _require_admin(principal)
    state.database.execute(
        """UPDATE files SET status = 'pending', error = '', retry_count = 0, last_attempt_at = NULL,
           next_retry_at = NULL, terminal_error = 0, last_error_fingerprint = ''"""
    )
    task_id, _ = await state.tasks.submit_unique(
        "index_pending",
        {"limit": settings.index_batch_size, "order": "balanced"},
        priority=5,
        user_id=principal["user_id"],
    )
    return {"task_id": task_id}


@app.post("/api/vision/upgrade", status_code=202)
async def upgrade_vision_captions(payload: CaptionUpgradeRequest, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    task_id, existing = await state.tasks.submit_unique(
        "upgrade_captions",
        {"limit": payload.limit},
        priority=3,
        user_id=principal["user_id"],
    )
    _audit(
        principal,
        "vision.upgrade",
        "task",
        str(task_id),
        {"limit": payload.limit, "existing": existing},
    )
    return {"task_id": task_id, "existing": existing}


@app.get("/api/tasks")
def list_tasks(principal: Auth, limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    return state.database.list_tasks(limit, None if _is_admin(principal) else principal["user_id"])


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: int, principal: Auth) -> dict[str, bool]:
    task = state.database.get_task(task_id)
    if not task or (not _is_admin(principal) and task.get("user_id") != principal["user_id"]):
        raise HTTPException(status_code=404, detail="任务不存在")
    state.database.cancel_task(task_id)
    _audit(principal, "task.cancel", "task", str(task_id))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/retry", status_code=202)
async def retry_task(task_id: int, principal: Auth) -> dict[str, int]:
    task = state.database.get_task(task_id)
    if not task or (not _is_admin(principal) and task.get("user_id") != principal["user_id"]):
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="只有失败或已取消的任务可以重试")
    await state.tasks.retry(task_id)
    return {"task_id": task_id}


@app.get("/api/files")
def list_files(
    principal: Auth,
    kind: str = "",
    status: str = "",
    library_id: Optional[int] = None,
    date_from: str = "",
    date_to: str = "",
    min_size: Optional[int] = Query(default=None, ge=0),
    max_size: Optional[int] = Query(default=None, ge=0),
    favorite: bool = False,
    tag: str = "",
    sort: str = "newest",
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if library_id is not None:
        if not _allowed_library(principal, library_id):
            raise HTTPException(status_code=404, detail="媒体库不存在")
        clauses.append("library_id = ?")
        params.append(library_id)
    else:
        allowed = _library_ids(principal)
        if allowed is not None:
            if not allowed:
                return {"total": 0, "items": []}
            clauses.append(f"library_id IN ({','.join('?' for _ in allowed)})")
            params.extend(allowed)
    time_value = "COALESCE(NULLIF(captured_at, ''), datetime(mtime_ns / 1000000000.0, 'unixepoch'))"
    if date_from:
        clauses.append(f"{time_value} >= ?")
        params.append(date_from)
    if date_to:
        clauses.append(f"{time_value} < datetime(?, '+1 day')")
        params.append(date_to)
    if min_size is not None:
        clauses.append("size >= ?")
        params.append(min_size)
    if max_size is not None:
        clauses.append("size <= ?")
        params.append(max_size)
    personal_user_id: int | None = None
    if favorite or tag:
        personal_user_id = _personal_user_id(principal)
    if favorite:
        clauses.append("id IN (SELECT file_id FROM favorites WHERE user_id = ?)")
        params.append(personal_user_id)
    if tag:
        clauses.append(
            """id IN (SELECT ft.file_id FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
               WHERE t.user_id = ? AND t.name = ? COLLATE NOCASE)"""
        )
        params.extend([personal_user_id, tag.strip()])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    order = {
        "newest": f"{time_value} DESC, id DESC",
        "oldest": f"{time_value}, id",
        "largest": "size DESC, id",
        "name": "name COLLATE NOCASE, id",
    }.get(sort, f"{time_value} DESC, id DESC")
    total = state.database.fetchone(f"SELECT COUNT(*) AS count FROM files{where}", params) or {"count": 0}
    rows = state.database.fetchall(
        f"SELECT * FROM files{where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    items = [_public_file(row) for row in rows]
    if principal.get("user_id") is not None:
        user_id = int(principal["user_id"])
        for item in items:
            item["favorite"] = state.database.is_favorite(user_id, int(item["id"]))
            item["tags"] = state.database.file_tag_names(user_id, int(item["id"]))
    return {"total": total["count"], "items": items}


@app.get("/api/timeline")
def timeline(
    principal: Auth,
    year: Optional[int] = Query(default=None, ge=1970, le=2200),
    month: Optional[int] = Query(default=None, ge=1, le=12),
    kind: str = "",
    limit: int = Query(default=120, ge=1, le=240),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    time_value = "COALESCE(NULLIF(captured_at, ''), datetime(mtime_ns / 1000000000.0, 'unixepoch'))"
    clauses: list[str] = []
    params: list[Any] = []
    if year is not None:
        clauses.append(f"CAST(strftime('%Y', {time_value}) AS INTEGER) = ?")
        params.append(year)
    if month is not None:
        clauses.append(f"CAST(strftime('%m', {time_value}) AS INTEGER) = ?")
        params.append(month)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    allowed = _library_ids(principal)
    if allowed is not None:
        if not allowed:
            return {"total": 0, "years": [], "items": []}
        clauses.append(f"library_id IN ({','.join('?' for _ in allowed)})")
        params.extend(allowed)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = state.database.fetchone(f"SELECT COUNT(*) AS count FROM files{where}", params) or {"count": 0}
    rows = state.database.fetchall(
        f"SELECT *, date({time_value}) AS timeline_date FROM files{where} ORDER BY {time_value} DESC, id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    years_where = " WHERE " + " AND ".join(
        clause for clause in clauses if not clause.startswith("CAST(strftime('%Y'") and not clause.startswith("CAST(strftime('%m'")
    ) if any(
        not clause.startswith("CAST(strftime('%Y'") and not clause.startswith("CAST(strftime('%m'") for clause in clauses
    ) else ""
    years_params: list[Any] = []
    if kind:
        years_params.append(kind)
    if allowed is not None:
        years_params.extend(allowed)
    years = state.database.fetchall(
        f"""SELECT CAST(strftime('%Y', {time_value}) AS INTEGER) AS year, COUNT(*) AS count
            FROM files{years_where} GROUP BY year HAVING year IS NOT NULL ORDER BY year DESC""",
        years_params,
    )
    items = []
    for row in rows:
        item = _public_file(row)
        item["timeline_date"] = row["timeline_date"]
        items.append(item)
    return {"total": total["count"], "years": years, "items": items}


@app.get("/api/organizer/duplicates")
def duplicates(principal: Auth, limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    result = state.database.duplicate_groups(limit, offset, _library_ids(principal))
    groups = []
    for group in result["groups"]:
        def keeper_score(row: dict[str, Any]) -> tuple[Any, ...]:
            lowered = f"{row.get('name', '')} {row.get('relative_path', '')}".lower()
            copy_markers = ("副本", "拷贝", "copy", "(1)", "_1")
            return (
                -int(any(marker in lowered for marker in copy_markers)),
                int(bool(row.get("captured_at"))),
                int(row.get("width") or 0) * int(row.get("height") or 0),
                -len(str(row.get("relative_path") or "")),
                -int(row.get("mtime_ns") or 0),
            )
        keep_id = int(max(group["items"], key=keeper_score)["id"])
        groups.append({
            "size": group["size"],
            "member_count": group["member_count"],
            "reclaimable_bytes": group["reclaimable_bytes"],
            "recommended_keep_id": keep_id,
            "items": [{**_public_file(row), "recommended_keep": int(row["id"]) == keep_id} for row in group["items"]],
        })
    result["groups"] = groups
    return result


@app.get("/api/organizer/similar")
def similar(principal: Auth, limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    result = state.database.similarity_groups(limit, offset, _library_ids(principal))
    result["groups"] = [
        {**group, "items": [{**_public_file(row), "distance": row["distance"]} for row in group["items"]]}
        for group in result["groups"]
    ]
    return result


@app.post("/api/organizer/analyze/{mode}", status_code=202)
async def analyze_organizer(mode: str, principal: Auth) -> dict[str, int]:
    _require_admin(principal)
    task_types = {"duplicates": "analyze_duplicates", "similar": "analyze_similar"}
    if mode not in task_types:
        raise HTTPException(status_code=404, detail="未知整理模式")
    task_type = task_types[mode]
    existing = state.database.fetchone(
        "SELECT id FROM tasks WHERE type = ? AND status IN ('pending', 'running') ORDER BY id DESC LIMIT 1",
        (task_type,),
    )
    if existing:
        return {"task_id": int(existing["id"])}
    task_id = await state.tasks.submit(task_type, {}, priority=2, user_id=principal["user_id"])
    _audit(principal, "organizer.analyze", "task", str(task_id), {"mode": mode})
    return {"task_id": task_id}


def _album_permission(
    principal: dict[str, Any],
    file_alias: str = "f",
) -> tuple[str, list[Any]]:
    allowed = _library_ids(principal)
    if allowed is None:
        return "", []
    if not allowed:
        return " AND 0", []
    return f" AND {file_alias}.library_id IN ({','.join('?' for _ in allowed)})", list(allowed)


@app.post("/api/places/analyze", status_code=202)
async def analyze_place_albums(principal: Auth) -> dict[str, int]:
    _require_admin(principal)
    task_id, _ = await state.tasks.submit_unique(
        "analyze_places", {}, priority=2, user_id=principal["user_id"]
    )
    _audit(principal, "places.analyze", "task", str(task_id))
    return {"task_id": task_id}


@app.get("/api/places")
def list_places(principal: Auth) -> dict[str, Any]:
    permission_sql, params = _album_permission(principal)
    rows = state.database.fetchall(
        """SELECT p.id, p.name, p.is_named, p.latitude, p.longitude, p.radius_m,
           COUNT(pf.file_id) AS file_count,
           (SELECT pf2.file_id FROM place_files pf2 JOIN files f2 ON f2.id = pf2.file_id
            WHERE pf2.place_id = p.id""" + permission_sql.replace("f.", "f2.") + """
            ORDER BY f2.mtime_ns DESC LIMIT 1) AS cover_file_id
           FROM places p JOIN place_files pf ON pf.place_id = p.id JOIN files f ON f.id = pf.file_id
           WHERE 1=1""" + permission_sql + """ GROUP BY p.id HAVING COUNT(pf.file_id) > 0
           ORDER BY file_count DESC, p.id""",
        [*params, *params],
    )
    return {"total": len(rows), "files": sum(int(row["file_count"]) for row in rows), "items": rows}


@app.get("/api/places/{place_id}")
def place_details(place_id: int, principal: Auth) -> dict[str, Any]:
    place = state.database.fetchone("SELECT * FROM places WHERE id = ?", (place_id,))
    if not place:
        raise HTTPException(status_code=404, detail="地点不存在")
    permission_sql, params = _album_permission(principal)
    files = state.database.fetchall(
        """SELECT f.*, pf.distance_m FROM place_files pf JOIN files f ON f.id = pf.file_id
           WHERE pf.place_id = ?""" + permission_sql + " ORDER BY f.mtime_ns DESC LIMIT 1000",
        [place_id, *params],
    )
    if not files:
        raise HTTPException(status_code=404, detail="地点不存在")
    return {"place": place, "files": [{**_public_file(row), "distance_m": row["distance_m"]} for row in files]}


@app.put("/api/places/{place_id}")
def rename_place(place_id: int, payload: AlbumUpdate, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    if not state.database.fetchone("SELECT id FROM places WHERE id = ?", (place_id,)):
        raise HTTPException(status_code=404, detail="地点不存在")
    state.database.execute(
        "UPDATE places SET name = ?, is_named = 1, updated_at = ? WHERE id = ?",
        (payload.name.strip(), datetime.now(timezone.utc).isoformat(timespec="seconds"), place_id),
    )
    _audit(principal, "places.rename", "place", str(place_id), {"name": payload.name.strip()})
    return state.database.fetchone("SELECT * FROM places WHERE id = ?", (place_id,)) or {}


@app.post("/api/events/analyze", status_code=202)
async def analyze_event_albums(principal: Auth) -> dict[str, int]:
    _require_admin(principal)
    task_id, _ = await state.tasks.submit_unique(
        "analyze_events", {}, priority=2, user_id=principal["user_id"]
    )
    _audit(principal, "events.analyze", "task", str(task_id))
    return {"task_id": task_id}


@app.get("/api/events")
def list_events(
    principal: Auth,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    permission_sql, params = _album_permission(principal)
    rows = state.database.fetchall(
        """SELECT e.id, e.name, e.is_named, e.start_at, e.end_at, e.latitude, e.longitude,
           COUNT(ef.file_id) AS file_count,
           (SELECT ef2.file_id FROM event_files ef2 JOIN files f2 ON f2.id = ef2.file_id
            WHERE ef2.event_id = e.id""" + permission_sql.replace("f.", "f2.") + """
            ORDER BY f2.mtime_ns DESC LIMIT 1) AS cover_file_id
           FROM events e JOIN event_files ef ON ef.event_id = e.id JOIN files f ON f.id = ef.file_id
           WHERE e.hidden = 0""" + permission_sql + """ GROUP BY e.id HAVING COUNT(ef.file_id) > 0
           ORDER BY e.start_at DESC""",
        [*params, *params],
    )
    return {
        "total": len(rows),
        "files": sum(int(row["file_count"]) for row in rows),
        "items": rows[offset:offset + limit],
        "offset": offset,
        "has_more": offset + limit < len(rows),
    }


@app.get("/api/events/{event_id}")
def event_details(event_id: int, principal: Auth) -> dict[str, Any]:
    event = state.database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    if not event:
        raise HTTPException(status_code=404, detail="事件不存在")
    permission_sql, params = _album_permission(principal)
    files = state.database.fetchall(
        """SELECT f.* FROM event_files ef JOIN files f ON f.id = ef.file_id
           WHERE ef.event_id = ?""" + permission_sql + """
           ORDER BY COALESCE(f.captured_at, datetime(f.mtime_ns / 1000000000, 'unixepoch')), f.id LIMIT 2000""",
        [event_id, *params],
    )
    if not files:
        raise HTTPException(status_code=404, detail="事件不存在")
    return {"event": event, "files": [_public_file(row) for row in files]}


@app.put("/api/events/{event_id}")
def rename_event(event_id: int, payload: AlbumUpdate, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    if not state.database.fetchone("SELECT id FROM events WHERE id = ?", (event_id,)):
        raise HTTPException(status_code=404, detail="事件不存在")
    state.database.execute(
        "UPDATE events SET name = ?, is_named = 1, updated_at = ? WHERE id = ?",
        (payload.name.strip(), datetime.now(timezone.utc).isoformat(timespec="seconds"), event_id),
    )
    _audit(principal, "events.rename", "event", str(event_id), {"name": payload.name.strip()})
    return state.database.fetchone("SELECT * FROM events WHERE id = ?", (event_id,)) or {}


def _refresh_event(connection: Any, event_id: int, now: str) -> None:
    row = connection.execute(
        """SELECT MIN(COALESCE(f.captured_at, datetime(f.mtime_ns / 1000000000, 'unixepoch'))) AS start_at,
           MAX(COALESCE(f.captured_at, datetime(f.mtime_ns / 1000000000, 'unixepoch'))) AS end_at,
           COUNT(*) AS count FROM event_files ef JOIN files f ON f.id = ef.file_id WHERE ef.event_id = ?""",
        (event_id,),
    ).fetchone()
    connection.execute(
        """UPDATE events SET start_at = COALESCE(?, start_at), end_at = COALESCE(?, end_at),
           file_count = ?, cover_file_id = COALESCE(cover_file_id, (
             SELECT f.id FROM event_files ef JOIN files f ON f.id = ef.file_id
             WHERE ef.event_id = ? ORDER BY COALESCE(f.width, 0) * COALESCE(f.height, 0) DESC LIMIT 1
           )), updated_at = ? WHERE id = ?""",
        (row["start_at"], row["end_at"], row["count"], event_id, now, event_id),
    )


@app.post("/api/events/merge")
def merge_events(payload: EventMerge, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    source_ids = sorted({int(value) for value in payload.source_ids if int(value) != payload.target_id})
    if not source_ids or not state.database.fetchone("SELECT id FROM events WHERE id = ?", (payload.target_id,)):
        raise HTTPException(status_code=404, detail="事件不存在")
    placeholders = ",".join("?" for _ in source_ids)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with state.database.transaction() as connection:
        connection.execute(
            f"""INSERT OR IGNORE INTO event_files(event_id, file_id)
                SELECT ?, file_id FROM event_files WHERE event_id IN ({placeholders})""",
            [payload.target_id, *source_ids],
        )
        connection.execute("UPDATE events SET is_named = 1 WHERE id = ?", (payload.target_id,))
        connection.execute(f"DELETE FROM events WHERE id IN ({placeholders})", source_ids)
        _refresh_event(connection, payload.target_id, now)
    _audit(principal, "events.merge", "event", str(payload.target_id), {"sources": source_ids})
    return state.database.fetchone("SELECT * FROM events WHERE id = ?", (payload.target_id,)) or {}


@app.post("/api/events/{event_id}/split", status_code=201)
def split_event(event_id: int, payload: EventSplit, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    file_ids = sorted({int(value) for value in payload.file_ids})
    placeholders = ",".join("?" for _ in file_ids)
    rows = state.database.fetchall(
        f"SELECT file_id FROM event_files WHERE event_id = ? AND file_id IN ({placeholders})",
        [event_id, *file_ids],
    )
    if len(rows) != len(file_ids):
        raise HTTPException(status_code=409, detail="选择的文件不属于该事件")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with state.database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO events(event_key, name, is_named, start_at, end_at, file_count, cover_file_id, created_at, updated_at)
               VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)""",
            (f"manual:{secrets.token_hex(12)}", payload.name.strip(), now, now, len(file_ids), file_ids[0], now, now),
        )
        new_id = int(cursor.lastrowid)
        connection.executemany(
            "INSERT INTO event_files(event_id, file_id) VALUES (?, ?)",
            [(new_id, file_id) for file_id in file_ids],
        )
        connection.execute(
            f"DELETE FROM event_files WHERE event_id = ? AND file_id IN ({placeholders})",
            [event_id, *file_ids],
        )
        connection.execute("UPDATE events SET is_named = 1 WHERE id = ?", (event_id,))
        _refresh_event(connection, new_id, now)
        _refresh_event(connection, event_id, now)
        connection.execute(
            "DELETE FROM events WHERE id = ? AND NOT EXISTS (SELECT 1 FROM event_files WHERE event_id = ?)",
            (event_id, event_id),
        )
    _audit(principal, "events.split", "event", str(event_id), {"new_id": new_id, "files": file_ids})
    return state.database.fetchone("SELECT * FROM events WHERE id = ?", (new_id,)) or {}


@app.put("/api/events/{event_id}/cover")
def update_event_cover(event_id: int, payload: CoverUpdate, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    if not state.database.fetchone(
        "SELECT 1 AS value FROM event_files WHERE event_id = ? AND file_id = ?",
        (event_id, payload.item_id),
    ):
        raise HTTPException(status_code=409, detail="封面文件不属于该事件")
    state.database.execute(
        "UPDATE events SET cover_file_id = ?, is_named = 1, updated_at = ? WHERE id = ?",
        (payload.item_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), event_id),
    )
    return {"cover_file_id": payload.item_id}


@app.delete("/api/events/{event_id}")
def hide_event(event_id: int, principal: Auth) -> dict[str, bool]:
    _require_admin(principal)
    state.database.execute(
        "UPDATE events SET hidden = 1, is_named = 1, updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), event_id),
    )
    return {"ok": True}


@app.get("/api/recycle")
def list_recycle(principal: Auth, limit: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
    _require_admin(principal)
    items = state.database.list_trash(limit)
    return {
        "total": len(items),
        "bytes": sum(int(item["size"]) for item in items),
        "items": items,
    }


@app.post("/api/recycle", status_code=201)
def recycle_duplicates(payload: RecycleRequest, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    try:
        result = state.recycle.move_duplicates(payload.file_ids, _actor(principal))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(principal, "recycle.move", "trash", ",".join(map(str, result["items"])), result)
    return result


@app.post("/api/recycle/{item_id}/restore")
async def restore_recycle(item_id: int, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    try:
        result = state.recycle.restore(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileExistsError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    task_id = await state.tasks.submit(
        "restore_file",
        {"library_id": result["library_id"], "path": result["path"]},
        priority=10,
        user_id=principal["user_id"],
    )
    _audit(principal, "recycle.restore", "trash", str(item_id), {**result, "task_id": task_id})
    return {**result, "task_id": task_id}


@app.delete("/api/recycle/{item_id}")
def purge_recycle(item_id: int, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    try:
        result = state.recycle.purge(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit(principal, "recycle.purge", "trash", str(item_id), result)
    return result


@app.get("/api/files/{file_id}")
def file_details(file_id: int, principal: Auth) -> dict[str, Any]:
    row = _visible_file(file_id, principal)
    result = _public_file(row)
    result["chunks"] = state.database.fetchall(
        """SELECT chunk_index, content, start_offset, end_offset, start_time, end_time, source_label
           FROM content_chunks WHERE file_id = ? ORDER BY chunk_index LIMIT 100""",
        (file_id,),
    )
    if principal.get("user_id") is not None:
        user_id = int(principal["user_id"])
        result["favorite"] = state.database.is_favorite(user_id, file_id)
        result["tags"] = state.database.file_tag_names(user_id, file_id)
    else:
        result["favorite"] = False
        result["tags"] = []
    return result


@app.get("/api/files/{file_id}/similar")
def similar_files(
    file_id: int,
    principal: Auth,
    kind: str = "",
    limit: int = Query(default=20, ge=1, le=60),
) -> dict[str, Any]:
    _visible_file(file_id, principal)
    result = state.search.similar(file_id, kind, limit, _library_ids(principal))
    if principal.get("user_id") is not None:
        user_id = int(principal["user_id"])
        for item in result["results"]:
            item["favorite"] = state.database.is_favorite(user_id, int(item["id"]))
            item["tags"] = state.database.file_tag_names(user_id, int(item["id"]))
    return result


@app.post("/api/files/{file_id}/reindex", status_code=202)
async def reindex_file(file_id: int, principal: Auth) -> dict[str, Any]:
    _visible_file(file_id, principal)
    state.database.reset_file_retry(file_id, pending=True)
    # 按文件去重：同一文件的排队/进行中的重索引任务直接复用，避免重复提交堆积重资源任务
    task_id, existing = await state.tasks.submit_unique_file(
        "index_files", file_id, {"file_ids": [file_id]}, priority=8, user_id=principal["user_id"]
    )
    return {"task_id": task_id, "existing": existing}


@app.put("/api/files/{file_id}/caption", status_code=202)
async def update_file_caption(file_id: int, payload: CaptionUpdate, principal: Auth) -> dict[str, Any]:
    _visible_file(file_id, principal)
    state.database.set_manual_caption(file_id, payload.caption)
    # 手动描述变更触发的重建与重索引同属 index_files，按同一文件去重合并
    task_id, existing = await state.tasks.submit_unique_file(
        "index_files", file_id, {"file_ids": [file_id], "source": "manual_caption"}, priority=9,
        user_id=principal.get("user_id"),
    )
    _audit(principal, "file.caption", "file", str(file_id), {"manual": bool(payload.caption.strip())})
    return {"task_id": task_id, "existing": existing, "manual": bool(payload.caption.strip())}


@app.post("/api/files/{file_id}/feedback")
def update_file_feedback(file_id: int, payload: FeedbackUpdate, principal: Auth) -> dict[str, bool]:
    _visible_file(file_id, principal)
    if payload.verdict not in {"relevant", "irrelevant", "caption_wrong"}:
        raise HTTPException(status_code=400, detail="不支持的反馈类型")
    state.database.save_feedback(
        principal.get("user_id"), file_id, payload.query, payload.verdict, payload.note
    )
    _audit(principal, "search.feedback", "file", str(file_id), {"verdict": payload.verdict})
    return {"ok": True}


@app.put("/api/files/{file_id}/favorite")
def update_favorite(file_id: int, enabled: bool, principal: Auth) -> dict[str, bool]:
    _visible_file(file_id, principal)
    user_id = _personal_user_id(principal)
    state.database.set_favorite(user_id, file_id, enabled)
    return {"favorite": enabled}


@app.put("/api/files/{file_id}/tags")
def update_file_tags(file_id: int, payload: TagsUpdate, principal: Auth) -> dict[str, Any]:
    _visible_file(file_id, principal)
    user_id = _personal_user_id(principal)
    tags = state.database.set_file_tags(user_id, file_id, payload.tags)
    return {"tags": tags}


@app.get("/api/tags")
def list_tags(principal: Auth) -> list[dict[str, Any]]:
    return state.database.list_tags(_personal_user_id(principal))


@app.get("/api/smart-albums")
def list_smart_albums(principal: Auth) -> list[dict[str, Any]]:
    return state.database.list_smart_albums(_personal_user_id(principal))


@app.post("/api/smart-albums", status_code=201)
def create_smart_album(payload: SmartAlbumCreate, principal: Auth) -> dict[str, Any]:
    if payload.kind not in INDEX_KINDS:
        raise HTTPException(status_code=400, detail="不支持的文件类型")
    if not payload.query.strip():
        raise HTTPException(status_code=400, detail="智能相册需要保存一个搜索条件")
    return state.database.create_smart_album(
        _personal_user_id(principal),
        payload.name,
        payload.query,
        payload.kind,
        payload.filters,
    )


@app.get("/api/smart-albums/{album_id}/items")
def smart_album_items(
    album_id: int,
    principal: Auth,
    limit: int = Query(default=40, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000),
) -> dict[str, Any]:
    album = state.database.get_smart_album(album_id, _personal_user_id(principal))
    if not album:
        raise HTTPException(status_code=404, detail="智能相册不存在")
    filters = album.get("filters") or {}
    filter_sql = _search_filter_sql(
        principal,
        filters.get("library_id"),
        str(filters.get("date_from") or ""),
        str(filters.get("date_to") or ""),
        filters.get("person_id"),
        filters.get("place_id"),
        filters.get("event_id"),
        bool(filters.get("favorite")),
        str(filters.get("tag") or ""),
    )
    result = state.search.search(
        str(album["query"]), str(album["kind"]), limit, _library_ids(principal), False, None, offset,
        filter_sql=filter_sql,
    )
    result["album"] = album
    return result


@app.delete("/api/smart-albums/{album_id}")
def delete_smart_album(album_id: int, principal: Auth) -> dict[str, bool]:
    state.database.delete_smart_album(album_id, _personal_user_id(principal))
    return {"ok": True}


@app.get("/api/files/{file_id}/content")
def file_content(file_id: int, principal: Auth) -> FileResponse:
    row = _visible_file(file_id, principal)
    if not Path(row["path"]).is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    disposition = "attachment" if _is_active_content(row["mime_type"]) else "inline"
    return FileResponse(
        row["path"],
        media_type=row["mime_type"],
        filename=row["name"],
        content_disposition_type=disposition,
    )


@app.post("/api/files/{file_id}/ticket")
def media_ticket(file_id: int, principal: Auth) -> dict[str, Any]:
    row = _visible_file(file_id, principal)
    if not Path(row["path"]).is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    now = time.monotonic()
    ticket = secrets.token_urlsafe(32)
    with MEDIA_TICKETS_LOCK:
        state.media_tickets = {key: value for key, value in state.media_tickets.items() if value[1] > now}
        if len(state.media_tickets) >= 2048:
            state.media_tickets.pop(min(state.media_tickets, key=lambda key: state.media_tickets[key][1]), None)
        state.media_tickets[ticket] = (file_id, now + 3600)
    return {"url": f"/api/media/{ticket}", "expires_in": 3600}


@app.get("/api/media/{ticket}")
def ticketed_media(ticket: str) -> FileResponse:
    with MEDIA_TICKETS_LOCK:
        entry = state.media_tickets.get(ticket)
        if not entry or entry[1] <= time.monotonic():
            state.media_tickets.pop(ticket, None)
            entry = None
    if not entry:
        raise HTTPException(status_code=404, detail="媒体访问链接已失效")
    row = state.database.get_file(entry[0])
    if not row or not Path(row["path"]).is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    disposition = "attachment" if _is_active_content(row["mime_type"]) else "inline"
    return FileResponse(
        row["path"],
        media_type=row["mime_type"],
        filename=row["name"],
        content_disposition_type=disposition,
    )


@app.get("/api/files/{file_id}/thumbnail")
def file_thumbnail(file_id: int, principal: Auth) -> FileResponse:
    row = _visible_file(file_id, principal)
    if not Path(row["path"]).is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    key = hashlib.sha256(f"{row['path']}:{row['mtime_ns']}:{settings.thumbnail_size}".encode()).hexdigest()
    destination = settings.cache_dir / "thumbnails" / key[:2] / f"{key}.jpg"
    if not destination.exists():
        try:
            create_thumbnail(Path(row["path"]), destination, row["kind"], settings.thumbnail_size)
        except Exception as exc:
            raise HTTPException(status_code=415, detail=f"无法生成缩略图：{exc}") from exc
    return FileResponse(destination, media_type="image/jpeg")


def _search_filter_sql(
    principal: dict[str, Any],
    library_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    person_id: int | None = None,
    place_id: int | None = None,
    event_id: int | None = None,
    favorite: bool = False,
    tag: str = "",
) -> tuple[str, list[Any]] | None:
    # 过滤条件以 SQL 片段（作用于 files 别名 f）返回，由搜索侧下推为 JOIN/子查询，
    # 不再物化全量 id 拼巨型 IN 列表。
    clauses: list[str] = []
    params: list[Any] = []
    allowed = _library_ids(principal)
    if library_id is not None:
        _visible_library(library_id, principal)
        clauses.append("f.library_id = ?")
        params.append(library_id)
    elif allowed is not None:
        if not allowed:
            return ("1 = 0", [])
        clauses.append(f"f.library_id IN ({','.join('?' for _ in allowed)})")
        params.extend(allowed)
    time_value = "COALESCE(NULLIF(f.captured_at, ''), datetime(f.mtime_ns / 1000000000.0, 'unixepoch'))"
    if date_from:
        clauses.append(f"{time_value} >= ?")
        params.append(date_from)
    if date_to:
        clauses.append(f"{time_value} < datetime(?, '+1 day')")
        params.append(date_to)
    if person_id is not None:
        clauses.append("f.id IN (SELECT file_id FROM faces WHERE person_id = ?)")
        params.append(person_id)
    if place_id is not None:
        clauses.append("f.id IN (SELECT file_id FROM place_files WHERE place_id = ?)")
        params.append(place_id)
    if event_id is not None:
        clauses.append("f.id IN (SELECT file_id FROM event_files WHERE event_id = ?)")
        params.append(event_id)
    if favorite or tag:
        user_id = _personal_user_id(principal)
        if favorite:
            clauses.append("f.id IN (SELECT file_id FROM favorites WHERE user_id = ?)")
            params.append(user_id)
        if tag:
            clauses.append(
                """f.id IN (SELECT ft.file_id FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
                   WHERE t.user_id = ? AND t.name = ? COLLATE NOCASE)"""
            )
            params.extend([user_id, tag.strip()])
    if not clauses:
        return None
    return " AND ".join(clauses), params


@app.get("/api/search")
def search(
    principal: Auth,
    q: str = Query(min_length=1, max_length=1000),
    kind: str = "",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000),
    precise: bool = True,
    semantic: bool = True,
    library_id: Optional[int] = None,
    date_from: str = "",
    date_to: str = "",
    person_id: Optional[int] = None,
    place_id: Optional[int] = None,
    event_id: Optional[int] = None,
    favorite: bool = False,
    tag: str = "",
) -> dict[str, Any]:
    filter_sql = _search_filter_sql(
        principal, library_id, date_from, date_to, person_id, place_id, event_id, favorite, tag
    )
    result = state.search.search(
        q.strip(), kind, limit, _library_ids(principal), precise, None, offset, semantic, filter_sql=filter_sql
    )
    if principal.get("user_id") is not None:
        user_id = int(principal["user_id"])
        for item in result["results"]:
            item["favorite"] = state.database.is_favorite(user_id, int(item["id"]))
            item["tags"] = state.database.file_tag_names(user_id, int(item["id"]))
    return result


def _select_answer_sources(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    reranked = [
        source for source in candidates
        if source.get("rerank_reason") or "精准重排" in source.get("sources", [])
    ]
    if len(reranked) >= 2:
        candidates = reranked
    best_coverage = max(float(source.get("coverage") or 0) for source in candidates)
    if best_coverage > 0:
        minimum_coverage = 0.5 if best_coverage >= 0.75 else max(0.35, best_coverage - 0.2)
        relevant = [
            source for source in candidates
            if float(source.get("confidence") or 0) >= 0.3
            and float(source.get("coverage") or 0) >= minimum_coverage
        ]
        if relevant:
            return relevant[:8]
    fallback = [source for source in candidates if float(source.get("confidence") or 0) >= 0.3]
    return (fallback or candidates)[:4]


def _focus_answer_evidence(source: dict[str, Any]) -> dict[str, Any]:
    evidence = str(source.get("evidence") or source.get("snippet") or source.get("caption") or "").strip()
    terms = [str(term).lower() for term in source.get("matched_terms", []) if str(term).strip()]
    if not evidence or not terms:
        return source
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[。！？!?；;])|\n+", evidence) if sentence.strip()]
    matched = {
        index for index, sentence in enumerate(sentences)
        if any(term in sentence.lower() for term in terms)
    }
    selected = matched | {index - 1 for index in matched if index > 0}
    if len(matched) == 1:
        index = next(iter(matched))
        if index + 1 < len(sentences):
            selected.add(index + 1)
    focused = [sentence for index, sentence in enumerate(sentences) if index in selected]
    if not focused:
        return source
    return {**source, "evidence": "".join(focused)[:1000]}


@app.post("/api/ask")
def ask(payload: AskRequest, principal: Auth) -> dict[str, Any]:
    question = payload.question.strip()
    conversation_id = payload.conversation_id
    history: list[dict[str, Any]] = []
    if conversation_id is not None:
        user_id = _personal_user_id(principal)
        conversation = state.database.conversation(conversation_id, user_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="对话不存在")
        history = conversation["messages"]
    elif principal.get("user_id") is not None:
        conversation = state.database.create_conversation(int(principal["user_id"]), question[:40])
        conversation_id = int(conversation["id"])
    retrieval_question = question
    followup_markers = ("这些", "那些", "其中", "刚才", "上面", "它们", "这个", "还有", "谁")
    previous_user = next(
        (str(message.get("content") or "") for message in reversed(history) if message.get("role") == "user"),
        "",
    )
    if previous_user and (len(question) <= 30 or any(marker in question for marker in followup_markers)):
        retrieval_question = f"{previous_user}；追问：{question}"
    search_result = state.search.search(
        retrieval_question, payload.kind, 24, _library_ids(principal), True
    )
    candidates = search_result["results"]
    sources = [_focus_answer_evidence(source) for source in _select_answer_sources(candidates)]
    if not sources:
        answer_text = "当前索引中没有找到足够相关的资料。"
        if conversation_id is not None:
            state.database.add_conversation_message(conversation_id, "user", question)
            state.database.add_conversation_message(conversation_id, "assistant", answer_text)
        return {"answer": answer_text, "sources": [], "conversation_id": conversation_id}
    try:
        answer_text = state.ai.answer(question, sources, history)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if conversation_id is not None:
        state.database.add_conversation_message(conversation_id, "user", question)
        saved_sources = [
            {
                key: source.get(key)
                for key in ("id", "name", "path", "source_label", "match_time", "confidence", "evidence")
            }
            for source in sources[:8]
        ]
        state.database.add_conversation_message(conversation_id, "assistant", answer_text, saved_sources)
    return {"answer": answer_text, "sources": sources, "conversation_id": conversation_id}


@app.get("/api/conversations")
def list_conversations(principal: Auth) -> list[dict[str, Any]]:
    return state.database.list_conversations(_personal_user_id(principal))


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: int, principal: Auth) -> dict[str, Any]:
    conversation = state.database.conversation(conversation_id, _personal_user_id(principal))
    if not conversation:
        raise HTTPException(status_code=404, detail="对话不存在")
    return conversation


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, principal: Auth) -> None:
    state.database.delete_conversation(conversation_id, _personal_user_id(principal))


@app.post("/api/uploads", status_code=201)
async def upload_file(
    request: Request,
    principal: Auth,
    x_filename: Annotated[Optional[str], Header()] = None,
) -> dict[str, Any]:
    filename = Path(unquote((x_filename or "").replace("\x00", ""))).name.strip()
    if not filename or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="缺少有效文件名")
    content_length = request.headers.get("content-length")
    try:
        declared_bytes = int(content_length) if content_length else 0
    except ValueError:
        declared_bytes = 0
    if declared_bytes > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="文件超过上传大小限制")
    library = state.database.fetchone("SELECT * FROM libraries WHERE path = ?", (str(settings.upload_root),))
    if not library:
        raise HTTPException(status_code=503, detail="上传空间尚未就绪")
    if not _allowed_library(principal, int(library["id"])):
        raise HTTPException(status_code=403, detail="没有上传空间权限")
    free_bytes = shutil.disk_usage(settings.upload_root).free
    if declared_bytes and declared_bytes + 256 * 1024 * 1024 > free_bytes:
        raise HTTPException(status_code=507, detail="NAS 可用空间不足")
    relative_directory = Path(datetime.now().strftime("%Y/%m"))
    destination_directory = settings.upload_root / relative_directory
    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = destination_directory / filename
    if destination.exists():
        destination = destination.with_name(f"{destination.stem}-{secrets.token_hex(4)}{destination.suffix}")
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.part")
    written = 0
    last_space_check = 0
    try:
        with temporary.open("xb") as handle:
            async for chunk in request.stream():
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="文件超过上传大小限制")
                if written - last_space_check >= 64 * 1024 * 1024:
                    last_space_check = written
                    if shutil.disk_usage(settings.upload_root).free < 256 * 1024 * 1024:
                        raise HTTPException(status_code=507, detail="NAS 可用空间不足")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if not written:
            raise HTTPException(status_code=400, detail="不能上传空文件")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    stat = destination.stat()
    relative_path = str(destination.relative_to(settings.upload_root))
    extension = destination.suffix.lower()
    file_id, _ = state.database.upsert_file({
        "library_id": int(library["id"]),
        "path": str(destination),
        "relative_path": relative_path,
        "name": destination.name,
        "extension": extension,
        "kind": file_kind(extension),
        "mime_type": mimetypes.guess_type(destination.name)[0] or "application/octet-stream",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "inode": getattr(stat, "st_ino", 0),
        "scan_token": "upload",
    })
    state.database.update_library_stats(int(library["id"]))
    task_id = await state.tasks.submit(
        "index_files", {"file_ids": [file_id]}, priority=9, user_id=principal["user_id"]
    )
    _audit(
        principal,
        "file.upload",
        "file",
        str(file_id),
        {"name": destination.name, "size": stat.st_size, "task_id": task_id},
    )
    return {"file": _public_file(state.database.get_file(file_id) or {}), "task_id": task_id}


@app.post("/api/people/analyze", status_code=202)
async def analyze_faces(principal: Auth) -> dict[str, int]:
    _require_admin(principal)
    existing = state.database.fetchone(
        "SELECT id FROM tasks WHERE type = 'analyze_people' AND status IN ('pending', 'running') ORDER BY id DESC LIMIT 1"
    )
    if existing:
        return {"task_id": int(existing["id"])}
    task_id = await state.tasks.submit("analyze_people", {}, priority=1, user_id=principal["user_id"])
    _audit(principal, "people.analyze", "task", str(task_id))
    return {"task_id": task_id}


@app.get("/api/people")
def list_people(
    principal: Auth,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    allowed = _library_ids(principal)
    permission_sql = ""
    params: list[Any] = []
    if allowed is not None:
        if not allowed:
            return {"total": 0, "faces": 0, "items": []}
        permission_sql = f" AND f.library_id IN ({','.join('?' for _ in allowed)})"
        params.extend(allowed)
    subquery_params = list(allowed or []) if allowed is not None else []
    rows = state.database.fetchall(
        """SELECT p.id, p.name, p.is_named, COUNT(fa.id) AS face_count,
           COUNT(DISTINCT f.id) AS file_count,
           (SELECT fa2.id FROM faces fa2 JOIN files f2 ON f2.id = fa2.file_id
            WHERE fa2.person_id = p.id""" + (
                f" AND f2.library_id IN ({','.join('?' for _ in allowed)})" if allowed is not None else ""
            ) + """ ORDER BY fa2.confidence * fa2.width * fa2.height DESC LIMIT 1) AS cover_face_id
           FROM people p JOIN faces fa ON fa.person_id = p.id JOIN files f ON f.id = fa.file_id
           WHERE p.hidden = 0""" + permission_sql + """ GROUP BY p.id HAVING COUNT(fa.id) > 0
           ORDER BY face_count DESC, p.id""",
        [*subquery_params, *params],
    )
    face_total = sum(int(row["face_count"]) for row in rows)
    return {
        "total": len(rows),
        "faces": face_total,
        "items": rows[offset:offset + limit],
        "offset": offset,
        "has_more": offset + limit < len(rows),
    }


@app.get("/api/people/{person_id}")
def person_details(person_id: int, principal: Auth) -> dict[str, Any]:
    person = state.database.fetchone("SELECT * FROM people WHERE id = ?", (person_id,))
    if not person:
        raise HTTPException(status_code=404, detail="人物不存在")
    allowed = _library_ids(principal)
    permission_sql = ""
    params: list[Any] = [person_id]
    if allowed is not None:
        if not allowed:
            raise HTTPException(status_code=404, detail="人物不存在")
        permission_sql = f" AND f.library_id IN ({','.join('?' for _ in allowed)})"
        params.extend(allowed)
    files = state.database.fetchall(
        """SELECT f.*, MIN(fa.id) AS face_id FROM faces fa JOIN files f ON f.id = fa.file_id
           WHERE fa.person_id = ?""" + permission_sql + " GROUP BY f.id ORDER BY f.mtime_ns DESC LIMIT 500",
        params,
    )
    if not files:
        raise HTTPException(status_code=404, detail="人物不存在")
    return {"person": person, "files": [{**_public_file(row), "face_id": row["face_id"]} for row in files]}


@app.put("/api/people/{person_id}")
def rename_person(person_id: int, payload: PersonUpdate, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    if not state.database.fetchone("SELECT id FROM people WHERE id = ?", (person_id,)):
        raise HTTPException(status_code=404, detail="人物不存在")
    state.database.execute(
        "UPDATE people SET name = ?, is_named = 1, updated_at = ? WHERE id = ?",
        (payload.name.strip(), datetime.now(timezone.utc).isoformat(timespec="seconds"), person_id),
    )
    _audit(principal, "people.rename", "person", str(person_id), {"name": payload.name.strip()})
    return state.database.fetchone("SELECT * FROM people WHERE id = ?", (person_id,)) or {}


@app.post("/api/people/merge")
def merge_people(payload: PersonMerge, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    source_ids = sorted({int(value) for value in payload.source_ids if int(value) != payload.target_id})
    if not source_ids or not state.database.fetchone("SELECT id FROM people WHERE id = ?", (payload.target_id,)):
        raise HTTPException(status_code=404, detail="人物不存在")
    placeholders = ",".join("?" for _ in source_ids)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with state.database.transaction() as connection:
        connection.execute(
            f"UPDATE faces SET person_id = ? WHERE person_id IN ({placeholders})",
            [payload.target_id, *source_ids],
        )
        connection.execute(f"DELETE FROM people WHERE id IN ({placeholders})", source_ids)
        connection.execute(
            """UPDATE people SET face_count = (SELECT COUNT(*) FROM faces WHERE person_id = ?),
               cover_face_id = COALESCE(cover_face_id, (
                 SELECT id FROM faces WHERE person_id = ? ORDER BY confidence * width * height DESC LIMIT 1
               )), updated_at = ? WHERE id = ?""",
            (payload.target_id, payload.target_id, now, payload.target_id),
        )
    _audit(principal, "people.merge", "person", str(payload.target_id), {"sources": source_ids})
    return state.database.fetchone("SELECT * FROM people WHERE id = ?", (payload.target_id,)) or {}


@app.post("/api/people/{person_id}/split", status_code=201)
def split_person(person_id: int, payload: PersonSplit, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    face_ids = sorted({int(value) for value in payload.face_ids})
    placeholders = ",".join("?" for _ in face_ids)
    rows = state.database.fetchall(
        f"SELECT id FROM faces WHERE person_id = ? AND id IN ({placeholders})",
        [person_id, *face_ids],
    )
    if len(rows) != len(face_ids):
        raise HTTPException(status_code=409, detail="选择的人脸不属于该人物")
    with state.database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO people(name, is_named, face_count, cover_face_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                payload.name.strip() or "待命名人物",
                int(bool(payload.name.strip())),
                len(face_ids),
                face_ids[0],
                now,
                now,
            ),
        )
        new_id = int(cursor.lastrowid)
        connection.execute(
            f"UPDATE faces SET person_id = ? WHERE id IN ({placeholders})",
            [new_id, *face_ids],
        )
        connection.execute(
            """UPDATE people SET face_count = (SELECT COUNT(*) FROM faces WHERE person_id = ?),
               cover_face_id = (SELECT id FROM faces WHERE person_id = ?
                 ORDER BY confidence * width * height DESC LIMIT 1), updated_at = ? WHERE id = ?""",
            (person_id, person_id, now, person_id),
        )
        connection.execute(
            "DELETE FROM people WHERE id = ? AND NOT EXISTS (SELECT 1 FROM faces WHERE person_id = ?)",
            (person_id, person_id),
        )
    _audit(principal, "people.split", "person", str(person_id), {"new_id": new_id, "faces": face_ids})
    return state.database.fetchone("SELECT * FROM people WHERE id = ?", (new_id,)) or {}


@app.put("/api/people/{person_id}/cover")
def update_person_cover(person_id: int, payload: CoverUpdate, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    face = state.database.fetchone(
        "SELECT id FROM faces WHERE id = ? AND person_id = ?",
        (payload.item_id, person_id),
    )
    if not face:
        raise HTTPException(status_code=409, detail="封面人脸不属于该人物")
    state.database.execute(
        "UPDATE people SET cover_face_id = ?, updated_at = ? WHERE id = ?",
        (payload.item_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), person_id),
    )
    return {"cover_face_id": payload.item_id}


@app.delete("/api/people/{person_id}")
def hide_person(person_id: int, principal: Auth) -> dict[str, bool]:
    _require_admin(principal)
    state.database.execute(
        "UPDATE people SET hidden = 1, updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), person_id),
    )
    return {"ok": True}


@app.get("/api/faces/{face_id}/thumbnail")
def face_thumbnail(face_id: int, principal: Auth) -> FileResponse:
    row = state.database.fetchone(
        """SELECT fa.*, f.path, f.mtime_ns, f.library_id FROM faces fa
           JOIN files f ON f.id = fa.file_id WHERE fa.id = ?""",
        (face_id,),
    )
    if not row or not _allowed_library(principal, int(row["library_id"])) or not Path(row["path"]).is_file():
        raise HTTPException(status_code=404, detail="人脸不存在")
    key = hashlib.sha256(f"{face_id}:{row['mtime_ns']}".encode()).hexdigest()
    destination = settings.cache_dir / "faces" / key[:2] / f"{key}.jpg"
    if not destination.exists():
        try:
            from PIL import Image, ImageOps

            destination.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(row["path"]) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                padding = max(float(row["width"]), float(row["height"])) * 0.3
                box = (
                    max(0, int(float(row["x"]) - padding)),
                    max(0, int(float(row["y"]) - padding)),
                    min(image.width, int(float(row["x"]) + float(row["width"]) + padding)),
                    min(image.height, int(float(row["y"]) + float(row["height"]) + padding)),
                )
                cropped = image.crop(box)
                cropped.thumbnail((360, 360))
                cropped.save(destination, "JPEG", quality=88, optimize=True)
        except Exception as exc:
            raise HTTPException(status_code=415, detail=f"无法生成人脸缩略图：{exc}") from exc
    return FileResponse(destination, media_type="image/jpeg")


@app.get("/api/operations/status")
def operations_status(principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    backups = settings.data_dir / "backups"
    backup_files = sorted(backups.glob("nas-ai-space-*.db"), reverse=True) if backups.exists() else []
    database_bytes = sum(
        path.stat().st_size for path in (
            settings.database_path,
            settings.database_path.with_name(settings.database_path.name + "-wal"),
            settings.database_path.with_name(settings.database_path.name + "-shm"),
        ) if path.exists()
    )
    task_stats = state.database.fetchall("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status")
    faces = state.database.fetchone("SELECT COUNT(*) AS faces, COUNT(DISTINCT person_id) AS people FROM faces") or {}
    trash = state.database.fetchone(
        "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM trash_items WHERE status = 'trashed'"
    ) or {"count": 0, "bytes": 0}
    vector_snapshots = state.vectors.list_snapshots()
    disk = shutil.disk_usage(settings.data_dir)
    return {
        "database": {
            "quick_check": state.database.probe(),
            "check_type": "connection_probe",
            "bytes": database_bytes,
        },
        "backups": {
            "count": len(backup_files),
            "latest": backup_files[0].name if backup_files else "",
            "latest_bytes": backup_files[0].stat().st_size if backup_files else 0,
        },
        "storage": {"total": disk.total, "used": disk.used, "free": disk.free},
        "tasks": task_stats,
        "indexing": {
            "pending": state.database.pending_summary(),
            "stages": state.database.index_stage_summary(),
            "policy": state.tasks.index_policy(),
        },
        "runtime": runtime_metrics(),
        "people": faces,
        "vision_captions": {"version": 3, "pending_upgrade": state.database.caption_upgrade_count()},
        "feedback": state.database.feedback_counts(),
        "watcher": state.watcher.status(),
        "maintenance": state.tasks.maintenance_status(),
        "production": _production_readiness(),
        "recycle": trash,
        "vector_snapshots": {
            "count": len(vector_snapshots),
            "latest": vector_snapshots[0]["name"] if vector_snapshots else "",
            "latest_bytes": vector_snapshots[0]["bytes"] if vector_snapshots else 0,
        },
        "uploads": {"path": str(settings.upload_root), "max_file_bytes": settings.max_upload_bytes},
    }


@app.post("/api/operations/backups", status_code=201)
def create_backup(principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    backup_directory = settings.data_dir / "backups"
    filename = f"nas-ai-space-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    destination = backup_directory / filename
    state.database.backup(destination)
    old_backups = sorted(
        backup_directory.glob("nas-ai-space-*.db"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[settings.automatic_backup_retention:]
    for path in old_backups:
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".verified").unlink(missing_ok=True)
    _audit(principal, "backup.create", "backup", filename, {"bytes": destination.stat().st_size})
    return {
        "name": filename,
        "bytes": destination.stat().st_size,
        "retained": settings.automatic_backup_retention,
    }


@app.get("/api/operations/vector-snapshots")
def list_vector_snapshots(principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    items = state.vectors.list_snapshots()
    return {"total": len(items), "items": items, "collection": settings.qdrant_collection}


@app.post("/api/operations/vector-snapshots", status_code=201)
def create_vector_snapshot(principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    if state.database.active_task_count():
        raise HTTPException(status_code=409, detail="请等待当前任务完成后再创建向量快照")
    try:
        result = state.vectors.create_snapshot(3)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(principal, "vector_snapshot.create", "snapshot", result["name"], result)
    return result


@app.post("/api/operations/vector-snapshots/{name}/restore")
def restore_vector_snapshot(name: str, payload: SnapshotRestoreRequest, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    if payload.confirm.strip() != settings.qdrant_collection:
        raise HTTPException(status_code=409, detail=f"请输入向量集合名 {settings.qdrant_collection} 以确认恢复")
    if state.database.active_task_count():
        raise HTTPException(status_code=409, detail="请等待当前任务完成后再恢复向量快照")
    backup_directory = settings.data_dir / "backups"
    backup_name = f"pre-vector-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    state.database.backup(backup_directory / backup_name)
    try:
        result = state.vectors.restore_snapshot(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(
        principal,
        "vector_snapshot.restore",
        "snapshot",
        name,
        {"database_backup": backup_name, **result},
    )
    return {"database_backup": backup_name, **result}


@app.delete("/api/operations/vector-snapshots/{name}")
def delete_vector_snapshot(name: str, principal: Auth) -> dict[str, bool]:
    _require_admin(principal)
    try:
        state.vectors.delete_snapshot(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(principal, "vector_snapshot.delete", "snapshot", name)
    return {"ok": True}


_ops_http_lock = threading.Lock()
_ops_http_client: httpx.Client | None = None


def _ops_http() -> httpx.Client:
    # 复用同一 httpx.Client（连接池），避免每次操作重建 TCP 连接；
    # httpx.Client 线程安全，懒初始化（参考 VectorStore._http）
    global _ops_http_client
    if _ops_http_client is None:
        with _ops_http_lock:
            if _ops_http_client is None:
                _ops_http_client = httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0))
    return _ops_http_client


def _ops_proxy(method: str, path: str, payload: dict[str, Any] | None = None) -> httpx.Response:
    if not settings.ops_url:
        raise HTTPException(status_code=503, detail="资源代理不可用：未配置 NAS_AI_OPS_URL")
    try:
        return _ops_http().request(method, f"{settings.ops_url}{path}", json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"资源代理不可用：{exc.__class__.__name__}") from exc


def _ops_require_ok(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        try:
            detail = str(response.json().get("detail") or "")
        except ValueError:
            detail = ""
        raise HTTPException(status_code=502, detail=f"资源代理异常：{detail or f'HTTP {response.status_code}'}")
    return response.json()


def _ops_service_or_404(service: str) -> str:
    if service not in OPS_SERVICES:
        raise HTTPException(status_code=404, detail="不支持的容器服务")
    return service


@app.get("/api/ops/containers")
def ops_containers(principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    return _ops_require_ok(_ops_proxy("GET", "/containers"))


@app.post("/api/ops/containers/{service}/memory")
def ops_set_memory(service: str, payload: OpsMemoryRequest, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    _ops_service_or_404(service)
    result = _ops_require_ok(_ops_proxy("POST", f"/containers/{service}/memory", {"mb": payload.mb}))
    _audit(principal, "ops.container.memory", "container", service, {"mb": payload.mb})
    return result


@app.post("/api/ops/containers/{service}/restart")
def ops_restart_container(service: str, principal: Auth) -> dict[str, Any]:
    _require_admin(principal)
    _ops_service_or_404(service)
    result = _ops_require_ok(_ops_proxy("POST", f"/containers/{service}/restart"))
    result["restarting"] = True
    if service == "app":
        result["notice"] = "正在重启应用容器，页面将短暂断开"
    _audit(principal, "ops.container.restart", "container", service)
    return result


@app.get("/api/audit")
def audit_log(principal: Auth, limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    _require_admin(principal)
    return state.database.list_audit(limit)


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str = "") -> FileResponse:
    candidate = (STATIC_DIR / path).resolve()
    if path and candidate.is_relative_to(STATIC_DIR) and candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(STATIC_DIR / "index.html")
