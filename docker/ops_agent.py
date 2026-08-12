"""容器资源代理（ops 边车）。

唯一挂载 /var/run/docker.sock 的特权容器，只在 compose 内网监听，不发布端口。
通过 unix socket 直连 Docker Engine API，对外暴露白名单 HTTP 接口：
- GET  /health
- GET  /containers                       白名单容器状态 / 内存占用 / 上限 / 重启次数
- POST /containers/{service}/memory      {"mb": N}，docker update 在线调整内存上限
- POST /containers/{service}/restart     重启容器

内存覆盖值持久化到 /data/overrides.json，启动时重放：
compose 重建容器后仍保持管理员设定的内存上限。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DOCKER_SOCKET = os.getenv("OPS_DOCKER_SOCKET", "/var/run/docker.sock")
OVERRIDES_PATH = Path(os.getenv("OPS_OVERRIDES_PATH", "/data/overrides.json"))
LISTEN_HOST = os.getenv("OPS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("OPS_LISTEN_PORT", "9100"))

# 服务名 → compose 容器名白名单；端点与持久化只接受这里的键
CONTAINERS = {
    "app": "nas-ai-space-app-1",
    "vision": "nas-ai-space-vision-1",
    "embedding": "nas-ai-space-embedding-1",
    "reranker": "nas-ai-space-reranker-1",
    "qdrant": "nas-ai-space-qdrant-1",
    "speech": "nas-ai-space-speech-1",
}
MIN_MEMORY_MB = 256
MAX_MEMORY_MB = 8192


class DockerError(RuntimeError):
    """Docker Engine API 调用失败（socket 不可达或引擎返回错误）。"""


class UnixHTTPConnection(http.client.HTTPConnection):
    """通过 unix socket 连接 Docker Engine API 的最小 HTTP 客户端。"""

    def __init__(self, socket_path: str, timeout: float = 30.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def docker_request(method: str, path: str, body: dict | None = None) -> object:
    """调用 Docker Engine API，非 2xx 统一抛 DockerError。"""
    payload = json.dumps(body).encode() if body is not None else None
    connection = UnixHTTPConnection(DOCKER_SOCKET)
    try:
        headers = {"Content-Type": "application/json"} if payload else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
    except OSError as exc:
        raise DockerError(f"无法连接 Docker：{exc}") from exc
    finally:
        connection.close()
    data = None
    if raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
    if status >= 400:
        message = data.get("message") if isinstance(data, dict) else None
        raise DockerError(f"Docker 返回 {status}：{message or raw[:200]!r}")
    return data


def validate_service(service: str) -> str:
    """校验服务白名单，返回对应的容器名。"""
    if service not in CONTAINERS:
        raise ValueError(f"不支持的容器服务：{service}")
    return CONTAINERS[service]


def validate_memory_mb(value: object) -> int:
    """内存上限强制整数 MB，范围 256-8192。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("内存上限必须是整数 MB")
    if not MIN_MEMORY_MB <= value <= MAX_MEMORY_MB:
        raise ValueError(f"内存上限需在 {MIN_MEMORY_MB}-{MAX_MEMORY_MB} MB 之间")
    return value


def load_overrides() -> dict[str, int]:
    """读取持久化的内存覆盖；文件缺失或损坏时按空处理。"""
    try:
        data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    overrides: dict[str, int] = {}
    for service, mb in data.items():
        try:
            validate_service(str(service))
            overrides[str(service)] = validate_memory_mb(mb)
        except ValueError:
            continue
    return overrides


def save_override(service: str, mb: int) -> None:
    """合并写入内存覆盖（先写临时文件再原子替换）。"""
    overrides = load_overrides()
    overrides[service] = mb
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OVERRIDES_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(OVERRIDES_PATH)


def _docker_update_memory(container: str, mb: int) -> None:
    docker_request("POST", f"/containers/{container}/update", {
        "Memory": mb * 1024 * 1024,
        "MemorySwap": mb * 2 * 1024 * 1024,
    })


def apply_overrides() -> None:
    """启动时重放持久化的内存覆盖；单个失败不影响其余容器。"""
    for service, mb in load_overrides().items():
        try:
            _docker_update_memory(CONTAINERS[service], mb)
        except DockerError:
            continue


def _container_snapshot(item: tuple[str, str]) -> dict:
    service, container = item
    info = docker_request("GET", f"/containers/{container}/json")
    state = info.get("State") or {}
    mem_usage = 0
    if state.get("Running"):
        stats = docker_request("GET", f"/containers/{container}/stats?stream=false")
        mem_usage = int((stats.get("memory_stats") or {}).get("usage") or 0)
    return {
        "name": str(info.get("Name") or "").lstrip("/") or container,
        "service": service,
        "status": state.get("Status", "unknown"),
        "mem_usage_bytes": mem_usage,
        "mem_limit_bytes": int((info.get("HostConfig") or {}).get("Memory") or 0),
        "memswap_limit_bytes": int((info.get("HostConfig") or {}).get("MemorySwap") or 0),
        "restart_count": int(info.get("RestartCount") or 0),
        "oom_killed": bool(state.get("OOMKilled")),
    }


def list_containers() -> list[dict]:
    with ThreadPoolExecutor(max_workers=len(CONTAINERS)) as executor:
        return list(executor.map(_container_snapshot, CONTAINERS.items()))


def set_memory(service: str, mb: object) -> dict:
    container = validate_service(service)
    mb = validate_memory_mb(mb)
    _docker_update_memory(container, mb)
    save_override(service, mb)
    return {"service": service, "mem_limit_mb": mb, "mem_limit_bytes": mb * 1024 * 1024}


def restart_container(service: str) -> dict:
    container = validate_service(service)
    docker_request("POST", f"/containers/{container}/restart")
    return {"service": service, "restarting": True}


class OpsHandler(BaseHTTPRequestHandler):
    server_version = "nas-ai-ops/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ValueError("请求体必须是 JSON") from None
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        if self.path == "/containers":
            try:
                self._send_json(200, {"containers": list_containers()})
            except DockerError as exc:
                self._send_json(502, {"detail": str(exc)})
            return
        self._send_json(404, {"detail": "路径不存在"})

    def do_POST(self) -> None:
        match = re.fullmatch(r"/containers/([a-z]+)/(memory|restart)", self.path)
        if not match:
            self._send_json(404, {"detail": "路径不存在"})
            return
        service, action = match.groups()
        try:
            if action == "memory":
                payload = self._read_json()
                self._send_json(200, set_memory(service, payload.get("mb")))
            else:
                self._send_json(200, restart_container(service))
        except ValueError as exc:
            self._send_json(400, {"detail": str(exc)})
        except DockerError as exc:
            self._send_json(502, {"detail": str(exc)})


def main() -> None:
    apply_overrides()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), OpsHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
