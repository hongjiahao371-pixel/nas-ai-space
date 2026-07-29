from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from app.config import Settings
from app.database import Database
from app.services.hardware import detect_hardware, ffmpeg_input_args


def _run(command: list[str], timeout: int = 7200) -> None:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or "FFmpeg failed").strip()
        raise RuntimeError(detail[-2000:])


def _video_command(source: Path, destination: Path, hardware: bool) -> list[str]:
    backend = detect_hardware().plan.media_backend if hardware else "cpu"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if hardware:
        command.extend(ffmpeg_input_args())
    command.extend([
        "-i", str(source),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-vf", "scale='min(1920,iw)':-2",
    ])
    if backend == "cuda":
        command.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"])
    elif backend == "qsv":
        command.extend(["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "24"])
    elif backend == "vaapi":
        command.extend(["-c:v", "h264_vaapi", "-qp", "24"])
    else:
        command.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])
    command.extend([
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(destination),
    ])
    return command


def _look_video_command(source: Path, lut: Path, destination: Path, hardware: bool) -> list[str]:
    backend = detect_hardware().plan.media_backend if hardware else "cpu"
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-t", "30",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-vf", f"lut3d=file={lut},scale='min(1280,iw)':-2",
    ]
    if backend == "cuda":
        command.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22"])
    else:
        command.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"])
    command.extend([
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(destination),
    ])
    return command


def generate_proxy(
    database: Database,
    settings: Settings,
    version_id: int,
    progress: Callable[[float, str], None],
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    version = database.fetchone(
        """SELECT av.*, f.path, f.mtime_ns FROM asset_versions av
           LEFT JOIN files f ON f.id = av.file_id WHERE av.id = ?""",
        (version_id,),
    )
    if not version or not version.get("path"):
        raise FileNotFoundError("素材版本的原文件不存在")
    source = Path(version["path"])
    if not source.is_file():
        raise FileNotFoundError("素材版本的原文件已离线")
    if version["kind"] not in {"video", "audio"}:
        raise ValueError("只有视频和音频需要生成代理媒体")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg 不可用")

    target = settings.cache_dir / "proxies" / str(version["asset_id"]) / str(version_id)
    target.mkdir(parents=True, exist_ok=True)
    proxy = target / ("proxy.mp4" if version["kind"] == "video" else "proxy.m4a")
    poster = target / "poster.jpg"
    filmstrip = target / "filmstrip.jpg"
    waveform = target / "waveform.png"
    database.execute(
        """UPDATE asset_versions SET proxy_status = 'processing', proxy_error = '' WHERE id = ?""",
        (version_id,),
    )
    try:
        progress(0.05, "正在准备代理媒体")
        if version["kind"] == "video":
            try:
                _run(_video_command(source, proxy, hardware=True))
            except Exception:
                proxy.unlink(missing_ok=True)
                _run(_video_command(source, proxy, hardware=False))
            if cancelled():
                raise InterruptedError
            progress(0.7, "正在生成封面与胶片条")
            _run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", "1", "-i", str(source), "-frames:v", "1",
                "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(poster),
            ])
            try:
                duration = max(0.2, float(version.get("duration") or 0))
                filmstrip_rate = f"{5 / duration:.6f}" if version.get("duration") else "0.1"
                _run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(source), "-frames:v", "1",
                    "-vf", (
                        f"fps={filmstrip_rate},scale=200:-2,"
                        "tile=5x1:padding=2:margin=2"
                    ),
                    "-q:v", "4", str(filmstrip),
                ])
            except Exception:
                filmstrip.unlink(missing_ok=True)
        else:
            _run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-vn", "-c:a", "aac", "-b:a", "160k", str(proxy),
            ])
        if cancelled():
            raise InterruptedError
        progress(0.88, "正在生成音频波形")
        try:
            _run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-filter_complex",
                "aformat=channel_layouts=mono,showwavespic=s=1200x160:colors=7c8cff",
                "-frames:v", "1", str(waveform),
            ])
        except Exception:
            waveform.unlink(missing_ok=True)
        database.execute(
            """UPDATE asset_versions SET proxy_status = 'ready', proxy_path = ?, poster_path = ?,
               filmstrip_path = ?, waveform_path = ?, proxy_error = '' WHERE id = ?""",
            (
                str(proxy),
                str(poster) if poster.is_file() else "",
                str(filmstrip) if filmstrip.is_file() else "",
                str(waveform) if waveform.is_file() else "",
                version_id,
            ),
        )
        progress(1, "代理媒体已就绪")
        return {
            "version_id": version_id,
            "proxy_path": str(proxy),
            "poster_path": str(poster) if poster.is_file() else "",
            "filmstrip_path": str(filmstrip) if filmstrip.is_file() else "",
            "waveform_path": str(waveform) if waveform.is_file() else "",
        }
    except InterruptedError:
        database.execute(
            "UPDATE asset_versions SET proxy_status = 'not_requested', proxy_error = '' WHERE id = ?",
            (version_id,),
        )
        raise
    except Exception as exc:
        database.execute(
            "UPDATE asset_versions SET proxy_status = 'error', proxy_error = ? WHERE id = ?",
            (f"{type(exc).__name__}: {exc}"[:2000], version_id),
        )
        raise


def generate_look_preview(
    database: Database,
    settings: Settings,
    version_id: int,
    lut_file_id: int,
    progress: Callable[[float, str], None],
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    version = database.fetchone(
        """SELECT av.*, f.path FROM asset_versions av
           LEFT JOIN files f ON f.id = av.file_id WHERE av.id = ?""",
        (version_id,),
    )
    lut_file = database.get_file(lut_file_id)
    if not version or not version.get("path"):
        raise FileNotFoundError("素材版本的原文件不存在")
    if not lut_file or not lut_file.get("path"):
        raise FileNotFoundError("LUT 文件不存在")
    source = Path(version["path"])
    lut_source = Path(lut_file["path"])
    if not source.is_file() or not lut_source.is_file():
        raise FileNotFoundError("素材或 LUT 文件已离线")
    if lut_source.suffix.lower() != ".cube":
        raise ValueError("当前仅支持 .cube 3D LUT")
    if version["kind"] not in {"image", "video"}:
        raise ValueError("LUT 预览仅支持图片和视频")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg 不可用")

    target = settings.cache_dir / "proxies" / str(version["asset_id"]) / str(version_id) / "looks"
    target.mkdir(parents=True, exist_ok=True)
    lut = target / "active.cube"
    preview = target / ("preview.mp4" if version["kind"] == "video" else "preview.jpg")
    shutil.copy2(lut_source, lut)
    database.execute(
        """UPDATE asset_versions SET look_status = 'processing', look_name = ?,
           look_error = '' WHERE id = ?""",
        (str(lut_file["name"])[:240], version_id),
    )
    try:
        progress(0.08, f"正在应用 {lut_file['name']}")
        if version["kind"] == "video":
            try:
                _run(_look_video_command(source, lut, preview, hardware=True))
            except Exception:
                preview.unlink(missing_ok=True)
                _run(_look_video_command(source, lut, preview, hardware=False))
        else:
            _run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf", f"lut3d=file={lut},scale='min(1600,iw)':-2",
                "-frames:v", "1",
                "-q:v", "2",
                str(preview),
            ])
        if cancelled():
            raise InterruptedError
        database.execute(
            """UPDATE asset_versions SET look_status = 'ready', look_path = ?,
               look_name = ?, look_error = '' WHERE id = ?""",
            (str(preview), str(lut_file["name"])[:240], version_id),
        )
        progress(1, "LUT 预览已就绪")
        return {"version_id": version_id, "look_path": str(preview), "look_name": lut_file["name"]}
    except InterruptedError:
        database.execute(
            """UPDATE asset_versions SET look_status = 'not_requested', look_error = ''
               WHERE id = ?""",
            (version_id,),
        )
        raise
    except Exception as exc:
        database.execute(
            """UPDATE asset_versions SET look_status = 'error', look_error = ? WHERE id = ?""",
            (f"{type(exc).__name__}: {exc}"[:2000], version_id),
        )
        raise
