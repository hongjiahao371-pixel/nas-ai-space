from __future__ import annotations

import gc
import os
import tempfile
import threading
import time
from pathlib import Path

from faster_whisper import WhisperModel
from flask import Flask, jsonify, request


app = Flask(__name__)
model_name = os.getenv("SPEECH_MODEL", "Systran/faster-whisper-tiny")
model_device = os.getenv("SPEECH_DEVICE", "cpu").strip().lower() or "cpu"
idle_unload_seconds = max(0, int(os.getenv("SPEECH_IDLE_UNLOAD_SECONDS", "600")))
model_lock = threading.RLock()
model: WhisperModel | None = None
last_used_at = 0.0


def get_model() -> WhisperModel:
    global last_used_at, model
    if model is not None:
        last_used_at = time.monotonic()
        return model
    with model_lock:
        if model is None:
            model = WhisperModel(
                model_name,
                device=model_device,
                compute_type=os.getenv("SPEECH_COMPUTE_TYPE", "int8"),
                cpu_threads=int(os.getenv("SPEECH_CPU_THREADS", "6")),
                num_workers=1,
                local_files_only=os.getenv("SPEECH_LOCAL_FILES_ONLY", "false").lower() in {"1", "true", "yes", "on"},
            )
        last_used_at = time.monotonic()
    return model


def unload_idle_model() -> None:
    global model
    if not idle_unload_seconds:
        return
    while True:
        time.sleep(min(60, max(5, idle_unload_seconds // 2)))
        with model_lock:
            if model is not None and time.monotonic() - last_used_at >= idle_unload_seconds:
                model = None
                gc.collect()


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "model": model_name,
        "device": model_device,
        "loaded": model is not None,
        "idle_unload_seconds": idle_unload_seconds,
    })


@app.get("/v1/models")
def models():
    return jsonify({"object": "list", "data": [{"id": model_name, "object": "model"}]})


@app.post("/v1/audio/transcriptions")
def transcriptions():
    global last_used_at
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": {"message": "file is required"}}), 400

    suffix = Path(upload.filename or "audio.wav").suffix[:16]
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary:
            temporary_path = temporary.name
            upload.save(temporary)

        with model_lock:
            segments_iter, info = get_model().transcribe(
                temporary_path,
                beam_size=1,
                vad_filter=True,
                word_timestamps=False,
                condition_on_previous_text=True,
            )
            segments = [
                {
                    "id": index,
                    "start": float(segment.start),
                    "end": float(segment.end),
                    "text": segment.text.strip(),
                }
                for index, segment in enumerate(segments_iter)
            ]
            last_used_at = time.monotonic()
        text = " ".join(segment["text"] for segment in segments).strip()
        duration = float(segments[-1]["end"]) if segments else float(getattr(info, "duration", 0.0))
        return jsonify({
            "task": "transcribe",
            "language": getattr(info, "language", ""),
            "duration": duration,
            "text": text,
            "segments": segments,
        })
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


threading.Thread(target=unload_idle_model, name="speech-idle-unload", daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
