"""Bounded boundary samples and validated PCM audio; originals stay read-only."""
from __future__ import annotations

import math
import io
import subprocess
import time
import wave
from pathlib import Path


def boundary_times(duration: float) -> list[float]:
    if not math.isfinite(duration) or duration <= 0:
        return []
    # Stay inside the final frame rather than seeking exactly to EOF.
    margin = min(.3, duration / 4)
    return [min(.1, duration / 4), max(0., duration - margin)]


def boundary_frames(source: Path, temporary: Path, duration: float):
    return sample_video_frames(source, temporary, duration, count=0)


def sample_video_frames(source: Path, temporary: Path, duration: float, count=6):
    """Decode boundary-first samples with one CPU thread and a total budget."""
    frames = []
    started = time.monotonic()
    stamps = boundary_times(duration)
    if not stamps:
        return frames
    count = max(0, min(12, int(count)))
    stamps += [duration * (index+1)/(count+1) for index in range(count)]
    for index, stamp in enumerate(stamps):
        remaining = 60 - (time.monotonic()-started)
        if remaining <= 0:
            break
        target = temporary / f"sample-{index}.jpg"
        try:
            result = subprocess.run([
                "ffmpeg", "-v", "error", "-threads", "1", "-filter_threads", "1", "-ss", f"{stamp:.3f}",
                "-i", str(source), "-an", "-frames:v", "1", "-vf", "scale='min(960,iw)':-2",
                "-q:v", "3", "-threads", "1", "-y", str(target),
            ], capture_output=True, timeout=min(20, remaining))
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0 and target.exists() and target.stat().st_size:
            frames.append((stamp, target))
    return frames


def validated_audio(path: Path) -> bytes:
    """Reject empty/truncated WAVs before sending them to the embedding server."""
    try:
        with wave.open(str(path), "rb") as audio:
            count, rate = audio.getnframes(), audio.getframerate()
            width, channels = audio.getsampwidth(), audio.getnchannels()
            if rate != 16000 or channels != 1 or width != 2 or audio.getcomptype() != "NONE":
                raise ValueError("音轨格式不是16kHz单声道PCM")
            if count < rate // 5 or count > rate * 31:
                raise ValueError("抽样音轨为空、过短或时长异常")
            pcm = audio.readframes(count)
            if len(pcm) != count * width * channels:
                raise ValueError("抽样音轨数据不完整")
    except (wave.Error, EOFError, OSError) as exc:
        raise ValueError("抽样音轨无法解码") from exc
    # Serialize a minimal, sized RIFF header instead of forwarding FFmpeg's
    # optional metadata chunks or streaming header to model-specific decoders.
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(channels); audio.setsampwidth(width); audio.setframerate(rate)
        audio.writeframes(pcm)
    return output.getvalue()
