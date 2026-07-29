from __future__ import annotations

import glob
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


def _run(command: list[str], timeout: float = 2.0) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or result.stderr).strip()


def _memory_bytes() -> int:
    if Path("/proc/meminfo").exists():
        text = Path("/proc/meminfo").read_text(errors="ignore")
        match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.MULTILINE)
        if match:
            return int(match.group(1)) * 1024
    if platform.system() == "Darwin":
        value = _run(["sysctl", "-n", "hw.memsize"])
        if value.isdigit():
            return int(value)
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def _memory_runtime() -> dict[str, int]:
    values = {
        "total_bytes": 0,
        "available_bytes": 0,
        "swap_total_bytes": 0,
        "swap_used_bytes": 0,
    }
    if not Path("/proc/meminfo").exists():
        values["total_bytes"] = _memory_bytes()
        return values
    parsed: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(errors="ignore").splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        match = re.search(r"(\d+)", raw)
        if match:
            parsed[key] = int(match.group(1)) * 1024
    values["total_bytes"] = parsed.get("MemTotal", 0)
    values["available_bytes"] = parsed.get("MemAvailable", parsed.get("MemFree", 0))
    values["swap_total_bytes"] = parsed.get("SwapTotal", 0)
    values["swap_used_bytes"] = max(0, parsed.get("SwapTotal", 0) - parsed.get("SwapFree", 0))
    return values


def _cpu_name() -> str:
    if Path("/proc/cpuinfo").exists():
        text = Path("/proc/cpuinfo").read_text(errors="ignore")
        match = re.search(r"^(?:model name|Hardware)\s*:\s*(.+)$", text, re.MULTILINE)
        if match:
            return match.group(1).strip()
    if platform.system() == "Darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor()
    return platform.processor() or platform.machine()


def _ffmpeg_capabilities() -> dict[str, list[str]]:
    if not shutil.which("ffmpeg"):
        return {"hwaccels": [], "encoders": [], "decoders": []}
    hwaccels = [
        line.strip()
        for line in _run(["ffmpeg", "-hide_banner", "-hwaccels"], 4).splitlines()
        if line.strip() and not line.lower().startswith("hardware")
    ]
    encoders_text = _run(["ffmpeg", "-hide_banner", "-encoders"], 5)
    decoders_text = _run(["ffmpeg", "-hide_banner", "-decoders"], 5)
    interesting = ("qsv", "vaapi", "nvenc", "cuda", "videotoolbox", "vulkan")
    encoders = sorted({word for word in re.findall(r"\b[\w-]+\b", encoders_text) if any(x in word for x in interesting)})
    decoders = sorted({word for word in re.findall(r"\b[\w-]+\b", decoders_text) if any(x in word for x in interesting)})
    return {"hwaccels": hwaccels, "encoders": encoders, "decoders": decoders}


def _onnx_providers() -> list[str]:
    try:
        import onnxruntime  # type: ignore

        return list(onnxruntime.get_available_providers())
    except (ImportError, OSError):
        return []


@dataclass(frozen=True)
class GPU:
    vendor: str
    name: str
    kind: str
    memory_bytes: int = 0
    device: str = ""


@dataclass(frozen=True)
class AccelerationPlan:
    inference_backend: str
    media_backend: str
    index_workers: int
    media_workers: int
    inference_workers: int
    cpu_threads_per_worker: int
    reasons: list[str]


@dataclass(frozen=True)
class HardwareInventory:
    os: str
    arch: str
    cpu: str
    logical_cpus: int
    memory_bytes: int
    gpus: list[GPU]
    dri_devices: list[str]
    nvidia_devices: list[str]
    kfd_available: bool
    onnx_providers: list[str]
    ffmpeg: dict[str, list[str]]
    plan: AccelerationPlan

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _detect_gpus() -> list[GPU]:
    gpus: list[GPU] = []
    nvidia = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,pci.bus_id",
        "--format=csv,noheader,nounits",
    ]) if shutil.which("nvidia-smi") else ""
    for line in nvidia.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 3:
            try:
                memory = int(parts[1]) * 1024 * 1024
            except ValueError:
                memory = 0
            gpus.append(GPU("nvidia", parts[0], "discrete", memory, parts[2]))

    lspci = _run(["lspci", "-nn"], 3) if shutil.which("lspci") else ""
    for line in lspci.splitlines():
        lowered = line.lower()
        if "vga compatible controller" not in lowered and "3d controller" not in lowered and "display controller" not in lowered:
            continue
        device = line.split()[0]
        if "nvidia" in lowered and any(
            gpu.vendor == "nvidia" and gpu.device.lower().endswith(device.lower()) for gpu in gpus
        ):
            continue
        if "[8086:" in lowered or re.search(r"\bintel\b", lowered):
            vendor = "intel"
        elif "[1002:" in lowered or re.search(r"\b(?:amd|ati)\b", lowered):
            vendor = "amd"
        elif "[10de:" in lowered or re.search(r"\bnvidia\b", lowered):
            vendor = "nvidia"
        else:
            vendor = "unknown"
        kind = "integrated" if vendor in {"intel", "amd"} and "integrated" in lowered else "unknown"
        gpus.append(GPU(vendor, line.split(": ", 1)[-1].strip(), kind, device=device))

    if not gpus:
        vendor_names = {"0x8086": "intel", "0x1002": "amd", "0x10de": "nvidia"}
        for card_path in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
            vendor_path = card_path / "device" / "vendor"
            device_path = card_path / "device" / "device"
            try:
                vendor_id = vendor_path.read_text().strip().lower()
                device_id = device_path.read_text().strip().lower()
            except OSError:
                continue
            vendor = vendor_names.get(vendor_id, "unknown")
            kind = "integrated" if vendor in {"intel", "amd"} else "unknown"
            name = f"{vendor.title()} DRM GPU ({device_id})"
            gpus.append(GPU(vendor, name, kind, device=f"/dev/dri/{card_path.name}"))

    if platform.system() == "Darwin" and not gpus:
        profiler = _run(["system_profiler", "SPDisplaysDataType", "-json"], 5)
        try:
            displays = json.loads(profiler).get("SPDisplaysDataType", [])
        except (json.JSONDecodeError, AttributeError):
            displays = []
        for display in displays:
            name = display.get("sppci_model", "Apple GPU")
            vendor = "apple" if "apple" in name.lower() else "unknown"
            gpus.append(GPU(vendor, name, "integrated"))
    return gpus


def _make_plan(
    gpus: list[GPU],
    cpus: int,
    memory: int,
    providers: list[str],
    ffmpeg: dict[str, list[str]],
    dri: list[str],
    kfd: bool,
) -> AccelerationPlan:
    vendors = {gpu.vendor for gpu in gpus}
    accelerators = set(ffmpeg["hwaccels"])
    provider_text = " ".join(providers).lower()
    reasons: list[str] = []

    forced_inference = os.getenv("NAS_AI_INFERENCE_BACKEND", "").strip().lower()
    if forced_inference in {"cuda", "rocm", "openvino", "vulkan", "coreml", "cpu"}:
        inference = forced_inference
        reasons.append(f"部署配置指定 {forced_inference.upper()} 推理后端")
    elif "nvidia" in vendors and ("cuda" in provider_text or Path("/dev/nvidia0").exists()):
        inference = "cuda"
        reasons.append("检测到 NVIDIA GPU，推理优先使用 CUDA")
    elif "amd" in vendors and kfd and "rocm" in provider_text:
        inference = "rocm"
        reasons.append("检测到 AMD ROCm 执行环境")
    elif "intel" in vendors and ("openvino" in provider_text or dri):
        inference = "openvino"
        reasons.append("检测到 Intel 核显，推理优先使用 OpenVINO")
    elif "apple" in vendors and "coreml" in provider_text:
        inference = "coreml"
        reasons.append("检测到 Apple CoreML 执行环境")
    else:
        inference = "cpu"
        reasons.append("未发现可用 GPU 推理 Provider，使用 CPU")

    if "nvidia" in vendors and "cuda" in accelerators:
        media = "cuda"
        reasons.append("媒体解码使用 NVDEC/CUDA")
    elif "intel" in vendors and "qsv" in accelerators and dri:
        media = "qsv"
        reasons.append("媒体解码使用 Intel Quick Sync")
    elif dri and "vaapi" in accelerators:
        media = "vaapi"
        reasons.append("媒体解码使用 VA-API")
    elif platform.system() == "Darwin" and "videotoolbox" in accelerators:
        media = "videotoolbox"
        reasons.append("媒体解码使用 VideoToolbox")
    else:
        media = "cpu"
        reasons.append("媒体处理使用 CPU")

    ram_gib = memory / (1024**3) if memory else 4
    index_workers = max(1, min(8, cpus // 3 or 1, int(ram_gib // 2) or 1))
    media_workers = 1 if media == "cpu" else max(1, min(3, len(gpus) + 1))
    inference_workers = 1
    if inference in {"cuda", "rocm"} and any(gpu.memory_bytes >= 16 * 1024**3 for gpu in gpus):
        inference_workers = 2
    cpu_threads = max(1, min(8, cpus // max(1, index_workers)))
    return AccelerationPlan(inference, media, index_workers, media_workers, inference_workers, cpu_threads, reasons)


@lru_cache(maxsize=1)
def detect_hardware() -> HardwareInventory:
    cpus = os.cpu_count() or 1
    memory = _memory_bytes()
    gpus = _detect_gpus()
    dri = sorted(glob.glob("/dev/dri/renderD*") + glob.glob("/dev/dri/card*"))
    nvidia = sorted(glob.glob("/dev/nvidia*"))
    kfd = Path("/dev/kfd").exists()
    providers = _onnx_providers()
    ffmpeg = _ffmpeg_capabilities()
    plan = _make_plan(gpus, cpus, memory, providers, ffmpeg, dri, kfd)
    return HardwareInventory(
        os=platform.platform(),
        arch=platform.machine(),
        cpu=_cpu_name(),
        logical_cpus=cpus,
        memory_bytes=memory,
        gpus=gpus,
        dri_devices=dri,
        nvidia_devices=nvidia,
        kfd_available=kfd,
        onnx_providers=providers,
        ffmpeg=ffmpeg,
        plan=plan,
    )


def runtime_metrics() -> dict[str, Any]:
    memory = _memory_runtime()
    cpus = os.cpu_count() or 1
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except (AttributeError, OSError):
        load_1m = load_5m = load_15m = 0.0
    gpus = []
    output = _run([
        "nvidia-smi",
        "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit",
        "--format=csv,noheader,nounits",
    ], 4) if shutil.which("nvidia-smi") else ""
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 8:
            continue
        try:
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "utilization_percent": float(parts[2]),
                "memory_used_bytes": int(float(parts[3])) * 1024 * 1024,
                "memory_total_bytes": int(float(parts[4])) * 1024 * 1024,
                "temperature_c": float(parts[5]),
                "power_watts": float(parts[6]),
                "power_limit_watts": float(parts[7]),
            })
        except ValueError:
            continue
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cpu": {
            "load_1m": round(load_1m, 2),
            "load_5m": round(load_5m, 2),
            "load_15m": round(load_15m, 2),
            "load_percent": round(min(100.0, load_1m / cpus * 100), 1),
        },
        "memory": memory,
        "gpus": gpus,
    }


def available_memory_bytes() -> int:
    return _memory_runtime()["available_bytes"]


def memory_runtime() -> dict[str, int]:
    return _memory_runtime()


def ffmpeg_input_args() -> list[str]:
    backend = detect_hardware().plan.media_backend
    if backend == "cuda":
        return ["-hwaccel", "cuda"]
    if backend == "qsv":
        return ["-hwaccel", "qsv"]
    if backend == "vaapi":
        device = next((path for path in detect_hardware().dri_devices if "renderD" in path), "")
        return ["-hwaccel", "vaapi", "-vaapi_device", device] if device else []
    if backend == "videotoolbox":
        return ["-hwaccel", "videotoolbox"]
    return []
