from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    cache_dir: Path
    database_path: Path
    api_token: str
    scan_root: Path
    scan_roots: tuple[Path, ...]
    upload_root: Path
    ingest_root: Path
    recycle_root: Path
    vector_backup_dir: Path
    mutation_roots: tuple[tuple[Path, Path], ...]
    max_upload_bytes: int
    comment_attachment_max_bytes: int
    comment_attachment_max_per_comment: int
    max_extract_bytes: int
    max_psd_bytes: int
    max_psd_pixels: int
    max_font_bytes: int
    pdf_ocr_pages: int
    video_frame_count: int
    thumbnail_size: int
    task_workers: int
    index_workers: int
    index_batch_size: int
    min_available_memory_bytes: int
    min_free_swap_bytes: int
    index_retry_max_attempts: int
    index_retry_base_seconds: int
    auto_index_enabled: bool
    auto_index_start_hour: int
    auto_index_end_hour: int
    auto_index_batch_size: int
    task_retention_days: int
    task_retention_count: int
    album_refresh_interval_seconds: int
    automatic_backup_enabled: bool
    automatic_backup_interval_hours: int
    automatic_backup_retention: int
    watch_enabled: bool
    watch_debounce_seconds: int
    watch_poll_seconds: int
    watch_signature_seconds: int
    watch_fallback_seconds: int
    face_workers: int
    local_ai_base_url: str
    local_ai_api_key: str
    embedding_base_url: str
    vision_base_url: str
    chat_base_url: str
    rerank_base_url: str
    embedding_model: str
    vision_model: str
    chat_model: str
    rerank_model: str
    transcription_base_url: str
    transcription_model: str
    allow_cloud_endpoints: bool
    allow_query_token: bool
    qdrant_url: str
    qdrant_collection: str
    vectors_on_disk: bool
    ops_url: str
    face_detection_model: Path
    face_recognition_model: Path
    face_match_threshold: float

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("NAS_AI_DATA_DIR", "/app/data")).expanduser().resolve()
        cache_dir = Path(os.getenv("NAS_AI_CACHE_DIR", str(data_dir / "cache"))).expanduser().resolve()
        model_dir = Path(__file__).resolve().parent.parent / "models"
        local_ai_base_url = os.getenv("NAS_AI_LOCAL_AI_URL", "").strip().rstrip("/")
        scan_root = Path(os.getenv("NAS_AI_SCAN_ROOT", "/library")).expanduser().resolve()
        configured_scan_roots = tuple(
            Path(value.strip()).expanduser().resolve()
            for value in os.getenv("NAS_AI_SCAN_ROOTS", "").split(",")
            if value.strip()
        )
        scan_roots = configured_scan_roots or (scan_root,)
        if scan_root not in scan_roots:
            scan_roots = (scan_root, *scan_roots)
        mutation_roots: list[tuple[Path, Path]] = []
        for value in os.getenv("NAS_AI_MUTATION_ROOTS", "").split(","):
            source, separator, destination = value.partition("=")
            if separator and source.strip() and destination.strip():
                mutation_roots.append((
                    Path(source.strip()).expanduser().resolve(),
                    Path(destination.strip()).expanduser().resolve(),
                ))
        upload_root = Path(
            os.getenv("NAS_AI_UPLOAD_ROOT", str(data_dir / "uploads"))
        ).expanduser().resolve()
        return cls(
            data_dir=data_dir,
            cache_dir=cache_dir,
            database_path=data_dir / "nas-ai-space.db",
            api_token=os.getenv("NAS_AI_API_TOKEN", "").strip(),
            scan_root=scan_root,
            scan_roots=scan_roots,
            upload_root=upload_root,
            ingest_root=Path(
                os.getenv("NAS_AI_INGEST_ROOT", str(upload_root / "inbox"))
            ).expanduser().resolve(),
            recycle_root=Path(os.getenv("NAS_AI_RECYCLE_ROOT", str(data_dir / "recycle"))).expanduser().resolve(),
            vector_backup_dir=Path(
                os.getenv("NAS_AI_VECTOR_BACKUP_DIR", str(data_dir / "vector-backups"))
            ).expanduser().resolve(),
            mutation_roots=tuple(mutation_roots),
            max_upload_bytes=_int_env("NAS_AI_MAX_UPLOAD_GB", 20, 1, 1024) * 1024 * 1024 * 1024,
            comment_attachment_max_bytes=_int_env("NAS_AI_COMMENT_ATTACHMENT_MB", 200, 1, 2048) * 1024 * 1024,
            comment_attachment_max_per_comment=_int_env("NAS_AI_COMMENT_ATTACHMENT_MAX_PER_COMMENT", 10, 1, 1000),
            max_extract_bytes=_int_env("NAS_AI_MAX_EXTRACT_MB", 16, 1, 256) * 1024 * 1024,
            max_psd_bytes=_int_env("NAS_AI_MAX_PSD_MB", 500, 1, 10240) * 1024 * 1024,
            max_psd_pixels=_int_env("NAS_AI_MAX_PSD_PIXELS", 100_000_000, 1_000_000, 10_000_000_000),
            max_font_bytes=_int_env("NAS_AI_MAX_FONT_MB", 100, 1, 2048) * 1024 * 1024,
            pdf_ocr_pages=_int_env("NAS_AI_PDF_OCR_PAGES", 20, 0, 200),
            video_frame_count=_int_env("NAS_AI_VIDEO_FRAME_COUNT", 6, 1, 12),
            thumbnail_size=_int_env("NAS_AI_THUMBNAIL_SIZE", 640, 160, 2048),
            task_workers=_int_env("NAS_AI_TASK_WORKERS", 0, 0, 32),
            index_workers=_int_env("NAS_AI_INDEX_WORKERS", 0, 0, 16),
            index_batch_size=_int_env("NAS_AI_INDEX_BATCH_SIZE", 200, 1, 10000),
            min_available_memory_bytes=_int_env("NAS_AI_MIN_AVAILABLE_MEMORY_MB", 1024, 0, 65536) * 1024 * 1024,
            min_free_swap_bytes=_int_env("NAS_AI_MIN_FREE_SWAP_MB", 512, 0, 65536) * 1024 * 1024,
            index_retry_max_attempts=_int_env("NAS_AI_INDEX_RETRY_MAX_ATTEMPTS", 3, 1, 20),
            index_retry_base_seconds=_int_env("NAS_AI_INDEX_RETRY_BASE_SECONDS", 300, 30, 86400),
            auto_index_enabled=_bool_env("NAS_AI_AUTO_INDEX_ENABLED", False),
            auto_index_start_hour=_int_env("NAS_AI_AUTO_INDEX_START_HOUR", 0, 0, 23),
            auto_index_end_hour=_int_env("NAS_AI_AUTO_INDEX_END_HOUR", 7, 0, 23),
            auto_index_batch_size=_int_env("NAS_AI_AUTO_INDEX_BATCH_SIZE", 200, 1, 10000),
            task_retention_days=_int_env("NAS_AI_TASK_RETENTION_DAYS", 30, 1, 3650),
            task_retention_count=_int_env("NAS_AI_TASK_RETENTION_COUNT", 2000, 100, 100000),
            album_refresh_interval_seconds=_int_env("NAS_AI_ALBUM_REFRESH_INTERVAL_SECONDS", 3600, 0, 86400),
            automatic_backup_enabled=_bool_env("NAS_AI_AUTOMATIC_BACKUP_ENABLED", True),
            automatic_backup_interval_hours=_int_env("NAS_AI_AUTOMATIC_BACKUP_INTERVAL_HOURS", 24, 1, 720),
            automatic_backup_retention=_int_env("NAS_AI_AUTOMATIC_BACKUP_RETENTION", 7, 1, 100),
            watch_enabled=_bool_env("NAS_AI_WATCH_ENABLED", True),
            watch_debounce_seconds=_int_env("NAS_AI_WATCH_DEBOUNCE_SECONDS", 5, 1, 120),
            watch_poll_seconds=_int_env("NAS_AI_WATCH_POLL_SECONDS", 30, 5, 3600),
            watch_signature_seconds=_int_env("NAS_AI_WATCH_SIGNATURE_SECONDS", 1800, 30, 86400),
            watch_fallback_seconds=_int_env("NAS_AI_WATCH_FALLBACK_SECONDS", 900, 60, 86400),
            face_workers=_int_env("NAS_AI_FACE_WORKERS", 0, 0, 16),
            local_ai_base_url=local_ai_base_url,
            local_ai_api_key=os.getenv("NAS_AI_LOCAL_AI_KEY", "").strip(),
            embedding_base_url=os.getenv("NAS_AI_EMBEDDING_URL", local_ai_base_url).strip().rstrip("/"),
            vision_base_url=os.getenv("NAS_AI_VISION_URL", local_ai_base_url).strip().rstrip("/"),
            chat_base_url=os.getenv("NAS_AI_CHAT_URL", local_ai_base_url).strip().rstrip("/"),
            rerank_base_url=os.getenv(
                "NAS_AI_RERANK_URL",
                os.getenv("NAS_AI_CHAT_URL", local_ai_base_url),
            ).strip().rstrip("/"),
            embedding_model=os.getenv("NAS_AI_EMBEDDING_MODEL", "").strip(),
            vision_model=os.getenv("NAS_AI_VISION_MODEL", "").strip(),
            chat_model=os.getenv("NAS_AI_CHAT_MODEL", "").strip(),
            rerank_model=os.getenv(
                "NAS_AI_RERANK_MODEL",
                os.getenv("NAS_AI_CHAT_MODEL", ""),
            ).strip(),
            transcription_base_url=os.getenv("NAS_AI_TRANSCRIPTION_URL", "").strip().rstrip("/"),
            transcription_model=os.getenv("NAS_AI_TRANSCRIPTION_MODEL", "").strip(),
            allow_cloud_endpoints=_bool_env("NAS_AI_ALLOW_CLOUD_ENDPOINTS", False),
            allow_query_token=_bool_env("NAS_AI_ALLOW_QUERY_TOKEN", False),
            qdrant_url=os.getenv("NAS_AI_QDRANT_URL", "http://qdrant:6333").strip().rstrip("/"),
            qdrant_collection=os.getenv("NAS_AI_QDRANT_COLLECTION", "nas_ai_chunks").strip(),
            vectors_on_disk=_bool_env("NAS_AI_VECTORS_ON_DISK", True),
            ops_url=os.getenv("NAS_AI_OPS_URL", "http://ops:9100").strip().rstrip("/"),
            face_detection_model=Path(os.getenv(
                "NAS_AI_FACE_DETECTION_MODEL",
                str(model_dir / "face_detection_yunet_2023mar.onnx"),
            )).expanduser().resolve(),
            face_recognition_model=Path(os.getenv(
                "NAS_AI_FACE_RECOGNITION_MODEL",
                str(model_dir / "face_recognition_sface_2021dec.onnx"),
            )).expanduser().resolve(),
            face_match_threshold=_float_env("NAS_AI_FACE_MATCH_THRESHOLD", 0.45, 0.2, 0.9),
        )

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "thumbnails").mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "faces").mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "proxies").mkdir(parents=True, exist_ok=True)
        self.upload_root.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "comment-attachments").mkdir(parents=True, exist_ok=True)
        self.ingest_root.mkdir(parents=True, exist_ok=True)
        self.recycle_root.mkdir(parents=True, exist_ok=True)
        self.vector_backup_dir.mkdir(parents=True, exist_ok=True)
        backup_dir = self.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for directory in (self.data_dir, backup_dir, self.vector_backup_dir):
            try:
                directory.chmod(0o700)
            except PermissionError:
                pass
        sensitive_files = [
            self.database_path,
            self.database_path.with_name(self.database_path.name + "-wal"),
            self.database_path.with_name(self.database_path.name + "-shm"),
            *backup_dir.glob("*.db*"),
            *backup_dir.glob("*.verified"),
            *self.vector_backup_dir.glob("*.snapshot"),
        ]
        for path in sensitive_files:
            if path.is_file():
                try:
                    path.chmod(0o600)
                except PermissionError:
                    pass


settings = Settings.from_env()
