from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import rawpy
from PIL import ExifTags, Image, ImageDraw, ImageFont

from app.config import Settings, settings
from app.services.hardware import ffmpeg_input_args
from app.services.local_ai import LocalAIClient


TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".log", ".srt", ".vtt"}
RAW_IMAGE_EXTENSIONS = {".raw", ".dng", ".cr2", ".cr3", ".nef", ".arw"}
PSD_EXTENSIONS = {".psd", ".psb"}
VECTOR_DESIGN_EXTENSIONS = {".ai", ".eps"}
FONT_EXTENSIONS = {".ttf", ".otf", ".ttc"}


logger = logging.getLogger(__name__)


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
    frame_rate = 0.0
    rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    try:
        numerator, separator, denominator = rate.partition("/")
        frame_rate = float(numerator) / float(denominator) if separator else float(numerator)
        if not math.isfinite(frame_rate) or frame_rate <= 0:
            frame_rate = 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        frame_rate = 0.0
    return {
        "width": video.get("width"),
        "height": video.get("height"),
        "duration": float(format_data.get("duration", 0) or 0),
        "captured_at": _normalize_datetime(tags.get("creation_time")),
        "metadata": {
            "video_codec": video.get("codec_name"),
            "frame_rate": round(frame_rate, 6) if frame_rate else None,
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
    count = max(1, min(12, int(count)))
    timestamps = [max(0.0, duration * (index + 1) / (count + 1)) for index in range(count)]
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


def _render_psd_preview(path: Path, destination: Path, size: int) -> None:
    # PSD/PSB 体积可能很大，合成前先做大小检查，避免单文件耗尽内存
    max_bytes = settings.max_psd_bytes
    if path.stat().st_size > max_bytes:
        raise ValueError(f"PSD 文件超过 {max_bytes // 1024 // 1024} MB，跳过合成预览")
    try:
        from psd_tools import PSDImage
    except ImportError as exc:
        raise RuntimeError("psd-tools 不可用，无法预览 PSD") from exc
    # 自行持有文件句柄并用 with 确保关闭；psd-tools 传路径时也会内部关闭
    with path.open("rb") as handle:
        psd = PSDImage.open(handle)
        # 像素量是内存炸弹的真正来源：超大画布在 composite 时按 width*height 分配
        pixels = int(getattr(psd, "width", 0) or 0) * int(getattr(psd, "height", 0) or 0)
        if pixels > settings.max_psd_pixels:
            raise ValueError(f"PSD 画布超过 {settings.max_psd_pixels} 像素，跳过合成预览")
        image = psd.composite()
    if image is None:
        raise ValueError("PSD 没有可用的合成图像")
    image.thumbnail((size, size))
    if image.mode in {"RGBA", "LA", "PA"}:
        # 透明区域垫白底，避免转 JPEG 后变成黑块
        background = Image.new("RGB", image.size, "#ffffff")
        background.paste(image, mask=image.split()[-1])
        image = background
    elif image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    image.save(destination, "JPEG", quality=82, optimize=True)


def _render_font_preview(path: Path, destination: Path, size: int) -> None:
    # 字体文件也可能异常巨大（TTC 合集），解析前先限制体积
    max_bytes = settings.max_font_bytes
    if path.stat().st_size > max_bytes:
        raise ValueError(f"字体文件超过 {max_bytes // 1024 // 1024} MB，跳过预览")
    try:
        from fontTools.ttLib import TTFont
    except ImportError as exc:
        raise RuntimeError("fontTools 不可用，无法预览字体") from exc
    try:
        with path.open("rb") as handle:
            font = TTFont(handle, fontNumber=0, lazy=True)
            records = font["name"].names
            family = next((record.toUnicode() for record in records if record.nameID == 1), "")
            full_name = next((record.toUnicode() for record in records if record.nameID == 4), "")
    except Exception as exc:
        raise ValueError(f"字体文件无法解析：{exc}") from exc
    title = full_name or family or path.stem
    try:
        title_font = ImageFont.truetype(str(path), max(12, size // 22), index=0)
        sample_font = ImageFont.truetype(str(path), max(16, size * 3 // 20), index=0)
    except OSError as exc:
        raise ValueError(f"字体无法渲染：{exc}") from exc
    canvas = Image.new("RGB", (size, size * 3 // 4), "#f5f5f2")
    draw = ImageDraw.Draw(canvas)
    draw.text((size // 16, size // 20), title, fill="#333333", font=title_font)
    draw.text((size // 16, size // 5), "永 字体预览 AaBbCc 123", fill="#111111", font=sample_font)
    draw.text((size // 16, size * 2 // 5), "敏捷的棕色狐狸 0123456789", fill="#555555", font=sample_font)
    canvas.save(destination, "JPEG", quality=88, optimize=True)


def _render_eps_preview(path: Path, destination: Path, size: int) -> None:
    # EPS 依赖系统 Ghostscript，未安装时明确记为不支持而不是静默失败
    if not shutil.which("gs"):
        raise ValueError("EPS 预览需要 Ghostscript，当前系统未安装，跳过该文件")
    command = [
        "gs", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dEPSCrop",
        "-dFirstPage=1", "-dLastPage=1", "-sDEVICE=jpeg", "-r150",
        f"-sOutputFile={destination}", str(path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=120)
    except subprocess.CalledProcessError as exc:
        # gs 失败可能留下残缺的输出文件；错误消息只保留退出码与 stderr 末尾，避免刷屏/注入
        destination.unlink(missing_ok=True)
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        raise ValueError(f"EPS 渲染失败（退出码 {exc.returncode}）：{stderr.strip()[-200:]}") from exc
    with Image.open(destination) as image:
        image.thumbnail((size, size))
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        image.save(destination, "JPEG", quality=82, optimize=True)


def _convert_image(path: Path, destination: Path, size: int = 2048) -> str:
    extension = path.suffix.lower()
    if extension in PSD_EXTENSIONS:
        _render_psd_preview(path, destination, size)
        return "psd-tools"
    if extension == ".ai":
        # 现代 .ai 本质是 PDF 兼容格式，复用 pdftoppm 渲染首页
        if _render_pdf_page(path, 1, destination):
            return "poppler"
        raise ValueError("AI 文件不含可渲染的 PDF 页面（可能不是 PDF 兼容格式）")
    if extension == ".eps":
        _render_eps_preview(path, destination, size)
        return "ghostscript"
    if extension in FONT_EXTENSIONS:
        _render_font_preview(path, destination, size)
        return "fonttools"
    if extension in RAW_IMAGE_EXTENSIONS:
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


def upgrade_image_caption(
    file: dict[str, Any],
    settings: Settings,
    ai: LocalAIClient,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    if str(file.get("kind") or "") != "image":
        raise ValueError("只有图片支持描述升级")
    path = Path(str(file["path"]))
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        metadata = json.loads(str(file.get("metadata_json") or "{}"))
        if not isinstance(metadata, dict):
            metadata = {}
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    manual_caption = str(file.get("manual_caption") or "").strip()
    if manual_caption:
        caption = manual_caption
        source = "manual"
    else:
        prepared_path = path
        temporary: tempfile.TemporaryDirectory[str] | None = None
        try:
            try:
                _image_info(path)
            except (OSError, ValueError):
                temporary = tempfile.TemporaryDirectory(dir=settings.cache_dir, prefix="caption-upgrade-")
                prepared_path = Path(temporary.name) / "converted.jpg"
                _convert_image(path, prepared_path)
            if int(file.get("size") or 0) > 24 * 1024 * 1024 and prepared_path == path:
                temporary = tempfile.TemporaryDirectory(dir=settings.cache_dir, prefix="caption-upgrade-large-")
                prepared_path = Path(temporary.name) / "resized.jpg"
                _convert_image(path, prepared_path)
            caption = ai.caption_image(prepared_path).strip()
        finally:
            if temporary:
                temporary.cleanup()
        source = "ai"
    if not caption:
        raise RuntimeError("视觉模型未返回描述")
    chunks = [{**chunk, "source_label": "画面描述"} for chunk in split_chunks(caption)]
    if not chunks:
        raise RuntimeError("新版描述无法切分")
    for start in range(0, len(chunks), 32):
        batch = chunks[start:start + 32]
        embeddings = ai.embeddings([chunk["content"] for chunk in batch])
        if len(embeddings) != len(batch):
            raise RuntimeError("Embedding 返回数量不完整")
        for chunk, embedding in zip(batch, embeddings):
            if not embedding:
                raise RuntimeError("Embedding 返回空向量")
            chunk["embedding"] = embedding
    errors = metadata.get("ai_errors")
    if isinstance(errors, list):
        metadata["ai_errors"] = [
            str(error) for error in errors
            if not str(error).startswith(("vision:", "embedding:"))
        ]
    metadata.update({"caption_version": 4, "caption_source": source})
    return caption, metadata, chunks


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
    manual_transcript = str(file.get("manual_transcript") or "").strip()

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
                if extension in PSD_EXTENSIONS:
                    # 索引阶段已经完成一次全分辨率合成，直接铺好缩略图缓存，
                    # 避免首次缩略图请求再合成一次
                    _cache_psd_thumbnail(path, prepared_path, int(file.get("mtime_ns") or 0))
            if file["size"] > 24 * 1024 * 1024 and prepared_path == path:
                temporary = tempfile.TemporaryDirectory(dir=settings.cache_dir, prefix="image-large-")
                prepared_path = Path(temporary.name) / "resized.jpg"
                _convert_image(path, prepared_path)
            if manual_caption:
                result["caption"] = manual_caption
                result["metadata"].update({"caption_version": 4, "caption_source": "manual"})
                result["stages"]["vision"] = {"status": "ready", "error": ""}
            elif settings.vision_base_url and settings.vision_model:
                try:
                    result["caption"] = ai.caption_image(prepared_path)
                    result["metadata"]["caption_version"] = 4
                    result["metadata"]["caption_source"] = "ai"
                    result["stages"]["vision"] = {
                        "status": "ready" if result["caption"] else "missing",
                        "error": "" if result["caption"] else "vision: 模型未返回描述",
                    }
                except Exception as exc:
                    error = f"vision: {exc}"
                    result["metadata"].setdefault("ai_errors", []).append(error)
                    result["stages"]["vision"] = {"status": "error", "error": error}
        finally:
            if temporary:
                temporary.cleanup()
    elif kind in {"video", "audio"}:
        result.update(_probe_media(path))
        with tempfile.TemporaryDirectory(dir=settings.cache_dir, prefix="media-") as temporary:
            temporary_path = Path(temporary)
            if kind == "video" and manual_caption:
                result["caption"] = manual_caption
                result["metadata"].update({"caption_version": 4, "caption_source": "manual"})
                result["stages"]["vision"] = {"status": "ready", "error": ""}
            elif kind == "video" and settings.vision_base_url and settings.vision_model:
                descriptions = []
                duration = float(result.get("duration") or 0)
                frame_count = min(settings.video_frame_count, max(3, math.ceil(duration / 180)))
                visual_segments = []
                for timestamp, frame in _extract_video_frames(path, temporary_path, duration, frame_count):
                    try:
                        description = ai.caption_image(frame).strip()
                        if description:
                            descriptions.append(f"[{timestamp:.1f}秒] {description}")
                            visual_segments.append({"timestamp": timestamp, "content": description})
                    except Exception as exc:
                        error = f"vision: {exc}"
                        result["metadata"].setdefault("ai_errors", []).append(error)
                result["caption"] = "\n".join(descriptions)
                if descriptions:
                    result["visual_segments"] = visual_segments
                    result["metadata"]["visual_frame_count"] = len(visual_segments)
                    result["metadata"].update({"caption_version": 4, "caption_source": "ai"})
                    result["stages"]["vision"] = {"status": "ready", "error": ""}
                else:
                    error = next(
                        (value for value in result["metadata"].get("ai_errors", []) if value.startswith("vision:")),
                        "vision: 未提取到可识别视频帧",
                    )
                    result["stages"]["vision"] = {"status": "error", "error": error}
            audio_codec = str(result.get("metadata", {}).get("audio_codec") or "")
            has_audio = kind == "audio" or bool(audio_codec)
            if manual_transcript:
                result["text"] = manual_transcript
                result["metadata"]["transcription_source"] = "manual"
                result["stages"]["transcription"] = {"status": "ready", "error": ""}
                structured_chunks = [
                    {**chunk, "source_label": "人工转写"}
                    for chunk in split_chunks(manual_transcript)
                ]
            elif has_audio and settings.transcription_base_url and settings.transcription_model:
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
    visual_segments = result.pop("visual_segments", [])
    for segment in visual_segments:
        timestamp = float(segment.get("timestamp") or 0)
        chunks.append({
            "content": str(segment.get("content") or "").strip(),
            "start_time": timestamp,
            "source_label": f"{timestamp:.1f} 秒画面",
        })
    if result["caption"] and not visual_segments:
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


def _cache_psd_thumbnail(source: Path, prepared: Path, mtime_ns: int) -> None:
    # 与 /api/files/{id}/thumbnail 的缓存键规则保持一致（path:mtime_ns:size 的 sha256），
    # 让索引阶段的 PSD 合成结果直接命中首次缩略图请求
    try:
        if not mtime_ns:
            mtime_ns = source.stat().st_mtime_ns
        key = hashlib.sha256(f"{source}:{mtime_ns}:{settings.thumbnail_size}".encode()).hexdigest()
        destination = settings.cache_dir / "thumbnails" / key[:2] / f"{key}.jpg"
        if destination.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(prepared) as image:
            image.thumbnail((settings.thumbnail_size, settings.thumbnail_size))
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            temporary = destination.with_suffix(".tmp.jpg")
            image.save(temporary, "JPEG", quality=82, optimize=True)
            temporary.replace(destination)
    except Exception as exc:
        logger.warning("写入 PSD 缩略图缓存失败（%s）：%s", source, exc)


def create_thumbnail(path: Path, destination: Path, kind: str, size: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.jpg")
    if kind == "image":
        if path.suffix.lower() in PSD_EXTENSIONS | VECTOR_DESIGN_EXTENSIONS | FONT_EXTENSIONS:
            # PSD/AI/EPS/字体等设计文件走各自的专用渲染路径
            _convert_image(path, temporary, size)
            temporary.replace(destination)
            return
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
