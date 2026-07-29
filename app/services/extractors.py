from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import rawpy
from PIL import ExifTags, Image

from app.config import Settings
from app.services.hardware import ffmpeg_input_args
from app.services.local_ai import LocalAIClient


TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".log", ".srt", ".vtt"}
RAW_IMAGE_EXTENSIONS = {".raw", ".dng", ".cr2", ".cr3", ".nef", ".arw"}


def _normalize_datetime(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], pattern).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _gps_coordinate(values: Any, reference: Any) -> float | None:
    try:
        degrees, minutes, seconds = (float(item) for item in values)
        coordinate = degrees + minutes / 60 + seconds / 3600
        return -coordinate if str(reference).upper() in {"S", "W"} else coordinate
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def quick_hash(path: Path, size: int) -> str:
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            handle.seek(max(0, size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    return digest.hexdigest()


def _decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _clean_markup(text: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _xml_text(data: bytes) -> str:
    return _clean_markup(_decode_bytes(data))


def _extract_office_sections(path: Path, extension: str, limit: int) -> list[dict[str, Any]]:
    patterns = {
        ".docx": ("word/document.xml",),
        ".pptx": ("ppt/slides/",),
        ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/"),
    }[extension]
    sections: list[dict[str, Any]] = []
    total = 0
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if not any(name == pattern or name.startswith(pattern) for pattern in patterns):
                continue
            data = archive.read(name)
            value = _xml_text(data)
            if value:
                if extension == ".pptx":
                    match = re.search(r"slide(\d+)\.xml$", name)
                    label = f"第 {match.group(1)} 页" if match else "幻灯片"
                elif extension == ".xlsx":
                    match = re.search(r"sheet(\d+)\.xml$", name)
                    label = f"工作表 {match.group(1)}" if match else "共享文本"
                else:
                    label = "正文"
                for chunk in split_chunks(value):
                    sections.append({**chunk, "source_label": label})
                total += len(value.encode("utf-8"))
            if total >= limit:
                break
    return sections


def _extract_epub(path: Path, limit: int) -> str:
    pieces: list[str] = []
    total = 0
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if not name.lower().endswith((".html", ".xhtml", ".htm")):
                continue
            value = _clean_markup(_decode_bytes(archive.read(name)))
            pieces.append(value)
            total += len(value.encode("utf-8"))
            if total >= limit:
                break
    return "\n".join(pieces)[:limit]


def _render_pdf_page(path: Path, page_number: int, destination: Path) -> bool:
    if not shutil.which("pdftoppm"):
        return False
    prefix = destination.with_suffix("")
    result = subprocess.run(
        [
            "pdftoppm", "-f", str(page_number), "-l", str(page_number), "-singlefile",
            "-jpeg", "-scale-to", "1600", str(path), str(prefix),
        ],
        check=False,
        capture_output=True,
        timeout=180,
    )
    generated = prefix.with_suffix(".jpg")
    if result.returncode == 0 and generated.exists():
        generated.replace(destination)
        return True
    return False


def _extract_pdf_sections(
    path: Path,
    limit: int,
    settings: Settings,
    ai: LocalAIClient,
) -> tuple[list[dict[str, Any]], list[str], int]:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return [], [], 0
    sections: list[dict[str, Any]] = []
    errors: list[str] = []
    total = 0
    ocr_pages = 0
    reader = PdfReader(str(path))
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.cache_dir, prefix="pdf-ocr-") as temporary:
        directory = Path(temporary)
        for page_number, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            if (
                len(text) < 30
                and ocr_pages < settings.pdf_ocr_pages
                and settings.vision_base_url
                and settings.vision_model
            ):
                rendered = directory / f"page-{page_number}.jpg"
                if _render_pdf_page(path, page_number, rendered):
                    try:
                        text = ai.ocr_document_page(rendered, page_number).strip()
                        ocr_pages += 1
                    except Exception as exc:
                        errors.append(f"ocr: 第{page_number}页 {exc}")
            if not text:
                continue
            for chunk in split_chunks(text):
                sections.append({**chunk, "source_label": f"第 {page_number} 页"})
            total += len(text.encode("utf-8"))
            if total >= limit:
                break
    return sections, errors, ocr_pages


def _probe_media(path: Path) -> dict[str, Any]:
    if not shutil.which("ffprobe"):
        return {}
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    video = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"), {})
    audio = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"), {})
    format_data = payload.get("format", {})
    tags = format_data.get("tags", {})
    return {
        "width": video.get("width"),
        "height": video.get("height"),
        "duration": float(format_data.get("duration", 0) or 0),
        "captured_at": _normalize_datetime(tags.get("creation_time")),
        "metadata": {
            "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name"),
            "bit_rate": int(format_data.get("bit_rate", 0) or 0),
            "format": format_data.get("format_name"),
            "tags": tags,
        },
    }


def _extract_audio(path: Path, destination: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg 不可用")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k", str(destination),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=3600)


def _extract_video_frames(path: Path, directory: Path, duration: float, count: int = 3) -> list[tuple[float, Path]]:
    if not shutil.which("ffmpeg") or duration <= 0:
        return []
    timestamps = [max(0.0, duration * ratio) for ratio in (0.15, 0.5, 0.85)[:count]]
    fallback_timestamps = [0.0, min(1.0, duration)]
    output: list[tuple[float, Path]] = []
    for index, timestamp in enumerate(timestamps + fallback_timestamps):
        if len(output) >= count:
            break
        if any(abs(timestamp - existing) < 0.001 for existing, _ in output):
            continue
        destination = directory / f"frame-{index}.jpg"
        base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        command = base + ffmpeg_input_args() + [
            "-ss", f"{timestamp:.3f}", "-i", str(path), "-frames:v", "1",
            "-vf", "scale='min(960,iw)':-2", "-q:v", "3", str(destination),
        ]
        result = subprocess.run(command, check=False, capture_output=True, timeout=120)
        if result.returncode != 0:
            fallback = base + [
                "-ss", f"{timestamp:.3f}", "-i", str(path), "-frames:v", "1",
                "-vf", "scale='min(960,iw)':-2", "-q:v", "3", str(destination),
            ]
            result = subprocess.run(fallback, check=False, capture_output=True, timeout=120)
        if result.returncode == 0 and destination.exists():
            output.append((timestamp, destination))
    return output


def _image_info(path: Path) -> dict[str, Any]:
    with Image.open(path) as image:
        metadata: dict[str, Any] = {"format": image.format, "mode": image.mode}
        captured_at = None
        latitude = None
        longitude = None
        try:
            exif = image.getexif()
            captured_at = _normalize_datetime(exif.get(36867) or exif.get(306))
            gps = exif.get_ifd(34853)
            if gps:
                latitude = _gps_coordinate(gps.get(2), gps.get(1))
                longitude = _gps_coordinate(gps.get(4), gps.get(3))
            metadata["exif"] = {
                ExifTags.TAGS.get(key, str(key)): str(value)[:500]
                for key, value in exif.items()
                if ExifTags.TAGS.get(key, str(key)) in {"DateTimeOriginal", "Make", "Model", "LensModel", "Orientation", "GPSInfo"}
            }
        except (AttributeError, ValueError, TypeError):
            pass
        return {
            "width": image.width,
            "height": image.height,
            "captured_at": captured_at,
            "latitude": latitude,
            "longitude": longitude,
            "metadata": metadata,
        }


def _convert_raw_image(path: Path, destination: Path, size: int) -> None:
    with rawpy.imread(str(path)) as raw:
        pixels = raw.postprocess(use_camera_wb=True, half_size=True, output_bps=8)
    with Image.fromarray(pixels) as image:
        image.thumbnail((size, size))
        image.save(destination, "JPEG", quality=88, optimize=True)


def _convert_image(path: Path, destination: Path, size: int = 2048) -> str:
    if path.suffix.lower() in RAW_IMAGE_EXTENSIONS:
        try:
            _convert_raw_image(path, destination, size)
            return "libraw"
        except (rawpy.LibRawError, OSError, ValueError):
            pass
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg 不可用，无法转换该图片格式")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-frames:v", "1", "-vf", f"scale='min({size},iw)':-2", "-q:v", "3", str(destination),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=180)
    return "ffmpeg"


def split_chunks(text: str, max_chars: int = 1200, overlap: int = 120) -> list[dict[str, Any]]:
    clean = text.strip()
    if not clean:
        return []
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(clean):
        target = min(len(clean), start + max_chars)
        end = target
        if target < len(clean):
            candidates = [clean.rfind(mark, start + max_chars // 2, target) for mark in ("\n", "。", "！", "？", ". ")]
            best = max(candidates)
            if best > start:
                end = best + 1
        chunks.append({"content": clean[start:end], "start_offset": start, "end_offset": end})
        if end >= len(clean):
            break
        start = max(start + 1, end - overlap)
    return chunks


def index_file(file: dict[str, Any], settings: Settings, ai: LocalAIClient) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = Path(file["path"])
    if not path.exists():
        raise FileNotFoundError(path)
    extension = file["extension"]
    kind = file["kind"]
    result: dict[str, Any] = {
        "quick_hash": quick_hash(path, file["size"]),
        "text": "",
        "caption": "",
        "metadata": {},
        "stages": {
            "metadata": {"status": "ready", "error": ""},
            "vision": {"status": "not_applicable", "error": ""},
            "transcription": {"status": "not_applicable", "error": ""},
            "embedding": {"status": "not_applicable", "error": ""},
        },
    }
    structured_chunks: list[dict[str, Any]] = []
    manual_caption = str(file.get("manual_caption") or "").strip()

    if extension in TEXT_EXTENSIONS:
        raw = path.read_bytes()[:settings.max_extract_bytes]
        text = _decode_bytes(raw)
        result["text"] = _clean_markup(text) if extension in {".html", ".htm", ".xml"} else text
        structured_chunks = [{**chunk, "source_label": "全文"} for chunk in split_chunks(result["text"])]
    elif extension in {".docx", ".pptx", ".xlsx"}:
        structured_chunks = _extract_office_sections(path, extension, settings.max_extract_bytes)
        result["text"] = "\n".join(chunk["content"] for chunk in structured_chunks)
    elif extension == ".epub":
        result["text"] = _extract_epub(path, settings.max_extract_bytes)
        structured_chunks = [{**chunk, "source_label": "正文"} for chunk in split_chunks(result["text"])]
    elif extension == ".pdf":
        structured_chunks, ocr_errors, ocr_pages = _extract_pdf_sections(
            path, settings.max_extract_bytes, settings, ai
        )
        result["text"] = "\n".join(chunk["content"] for chunk in structured_chunks)
        result["metadata"]["ocr_pages"] = ocr_pages
        if ocr_errors:
            result["metadata"].setdefault("ai_errors", []).extend(ocr_errors)
    elif kind == "image":
        prepared_path = path
        temporary: tempfile.TemporaryDirectory[str] | None = None
        try:
            result.update(_image_info(path))
        except (OSError, ValueError):
            temporary = tempfile.TemporaryDirectory(dir=settings.cache_dir, prefix="image-")
            prepared_path = Path(temporary.name) / "converted.jpg"
            decoder = _convert_image(path, prepared_path)
            result.update(_image_info(prepared_path))
            result["metadata"].update({"source_format": extension.lstrip("."), "decoder": decoder})
            source_probe = _probe_media(path)
            result["captured_at"] = source_probe.get("captured_at") or result.get("captured_at")
        if file["size"] > 24 * 1024 * 1024 and prepared_path == path:
            temporary = tempfile.TemporaryDirectory(dir=settings.cache_dir, prefix="image-large-")
            prepared_path = Path(temporary.name) / "resized.jpg"
            _convert_image(path, prepared_path)
        if manual_caption:
            result["caption"] = manual_caption
            result["metadata"].update({"caption_version": 3, "caption_source": "manual"})
            result["stages"]["vision"] = {"status": "ready", "error": ""}
        elif settings.vision_base_url and settings.vision_model:
            try:
                result["caption"] = ai.caption_image(prepared_path)
                result["metadata"]["caption_version"] = 3
                result["metadata"]["caption_source"] = "ai"
                result["stages"]["vision"] = {
                    "status": "ready" if result["caption"] else "missing",
                    "error": "" if result["caption"] else "vision: 模型未返回描述",
                }
            except Exception as exc:
                error = f"vision: {exc}"
                result["metadata"].setdefault("ai_errors", []).append(error)
                result["stages"]["vision"] = {"status": "error", "error": error}
        if temporary:
            temporary.cleanup()
    elif kind in {"video", "audio"}:
        result.update(_probe_media(path))
        with tempfile.TemporaryDirectory(dir=settings.cache_dir, prefix="media-") as temporary:
            temporary_path = Path(temporary)
            if kind == "video" and manual_caption:
                result["caption"] = manual_caption
                result["metadata"].update({"caption_version": 3, "caption_source": "manual"})
                result["stages"]["vision"] = {"status": "ready", "error": ""}
            elif kind == "video" and settings.vision_base_url and settings.vision_model:
                descriptions = []
                for timestamp, frame in _extract_video_frames(path, temporary_path, result.get("duration") or 0):
                    try:
                        descriptions.append(f"[{timestamp:.1f}秒] {ai.caption_image(frame)}")
                    except Exception as exc:
                        error = f"vision: {exc}"
                        result["metadata"].setdefault("ai_errors", []).append(error)
                result["caption"] = "\n".join(descriptions)
                if descriptions:
                    result["metadata"].update({"caption_version": 3, "caption_source": "ai"})
                    result["stages"]["vision"] = {"status": "ready", "error": ""}
                else:
                    error = next(
                        (value for value in result["metadata"].get("ai_errors", []) if value.startswith("vision:")),
                        "vision: 未提取到可识别视频帧",
                    )
                    result["stages"]["vision"] = {"status": "error", "error": error}
            audio_codec = str(result.get("metadata", {}).get("audio_codec") or "")
            has_audio = kind == "audio" or bool(audio_codec)
            if has_audio and settings.transcription_base_url and settings.transcription_model:
                try:
                    audio_path = temporary_path / "audio.mp3"
                    _extract_audio(path, audio_path)
                    transcript = ai.transcribe(audio_path)
                    result["text"] = transcript.get("text", "")
                    result["transcript_segments"] = transcript.get("segments", [])
                    has_transcript = bool(
                        str(result["text"]).strip()
                        or any(str(segment.get("text") or "").strip() for segment in result["transcript_segments"])
                    )
                    result["stages"]["transcription"] = {
                        "status": "ready" if has_transcript else "not_applicable",
                        "error": "",
                    }
                    if not has_transcript:
                        result["metadata"]["transcription_empty"] = True
                except Exception as exc:
                    error = f"transcription: {exc}"
                    result["metadata"].setdefault("ai_errors", []).append(error)
                    result["stages"]["transcription"] = {"status": "error", "error": error}

    chunks: list[dict[str, Any]] = list(structured_chunks)
    for segment in result.pop("transcript_segments", []):
        if segment.get("text"):
            start_time = segment.get("start")
            chunks.append({
                "content": segment["text"].strip(),
                "start_time": start_time,
                "end_time": segment.get("end"),
                "source_label": f"{float(start_time or 0):.1f} 秒",
            })
    if result["caption"]:
        chunks.extend({**chunk, "source_label": "画面描述"} for chunk in split_chunks(result["caption"]))
    if not chunks and result["text"]:
        chunks = [{**chunk, "source_label": "全文"} for chunk in split_chunks(result["text"])]
    if chunks and settings.embedding_base_url and settings.embedding_model:
        result["stages"]["embedding"] = {"status": "pending", "error": ""}
        for start in range(0, len(chunks), 32):
            batch = chunks[start:start + 32]
            try:
                embeddings = ai.embeddings([chunk["content"] for chunk in batch])
                for chunk, embedding in zip(batch, embeddings):
                    chunk["embedding"] = embedding
            except Exception as exc:
                error = f"embedding: {exc}"
                result["metadata"].setdefault("ai_errors", []).append(error)
                result["stages"]["embedding"] = {"status": "error", "error": error}
                break
        else:
            result["stages"]["embedding"] = {"status": "ready", "error": ""}
    elif not chunks and any(
        result["stages"][name]["status"] in {"error", "missing"}
        for name in ("vision", "transcription")
    ):
        result["stages"]["embedding"] = {"status": "blocked", "error": "embedding: 上游内容提取未完成"}
    return result, chunks


def create_thumbnail(path: Path, destination: Path, kind: str, size: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.jpg")
    if kind == "image":
        try:
            with Image.open(path) as image:
                image.thumbnail((size, size))
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                image.save(temporary, "JPEG", quality=82, optimize=True)
        except (OSError, ValueError):
            _convert_image(path, temporary, size)
    elif kind == "video" and shutil.which("ffmpeg"):
        base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        command = base + ffmpeg_input_args() + ["-ss", "00:00:01", "-i", str(path), "-frames:v", "1", "-vf", f"scale='min({size},iw)':-2", "-q:v", "3", str(temporary)]
        result = subprocess.run(command, check=False, capture_output=True, timeout=45)
        if result.returncode != 0:
            fallback = base + ["-ss", "00:00:01", "-i", str(path), "-frames:v", "1", "-vf", f"scale='min({size},iw)':-2", "-q:v", "3", str(temporary)]
            subprocess.run(fallback, check=True, capture_output=True, timeout=45)
    else:
        raise ValueError("该文件类型不支持缩略图")
    temporary.replace(destination)
