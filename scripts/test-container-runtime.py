#!/usr/bin/env python3
"""Native Linux CPU acceptance using only disposable, synthetic data.

Build nas-ai-space:test and nas-ai-space:test-media first, then run this script.
No host Python packages are required. The production media entrypoint downloads
and verifies the pinned models/runtime; no inference server is mocked. Evidence
is saved even on failure, and all uniquely named Docker resources are removed.

This is a small image/text retrieval smoke test, not a NAS, performance, video,
audio, Qwen/Ollama, or model-quality acceptance test.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib


REPO = Path(__file__).resolve().parents[1]
COLLECTION = "nas_ai_media_gemma2_q8_v1"
QUERY = "a bright red square centered on a plain white background"


def architecture(value: str) -> str:
    aliases = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    if value.lower() not in aliases:
        raise ValueError(f"Unsupported architecture: {value}")
    return aliases[value.lower()]


def require_native(host_system: str, host_machine: str, daemon: dict, images: list[dict]) -> str:
    expected = architecture(host_machine)
    if host_system != "Linux" or daemon["OSType"] != "linux":
        raise ValueError("This acceptance requires a native Linux host and Linux Docker daemon")
    if architecture(daemon["Architecture"]) != expected:
        raise ValueError("Docker daemon and host architectures differ; emulation is not acceptance")
    for image in images:
        if image["Os"] != "linux" or architecture(image["Architecture"]) != expected:
            raise ValueError("Container image does not match the native host architecture")
    return expected


def vector_summary(vector: object) -> dict:
    if not isinstance(vector, list) or len(vector) != 768:
        raise ValueError("Expected exactly one 768-dimensional embedding")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
        raise ValueError("Embedding must contain only finite numbers")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm <= 1e-9:
        raise ValueError("Embedding is empty/zero")
    return {"dimension": len(vector), "norm": norm,
            "sha256": hashlib.sha256(json.dumps(vector, separators=(",", ":")).encode()).hexdigest()}


def require_media_hit(response: dict, file_id: int, *, media_first: bool = False) -> dict:
    if response.get("multimodal") is not True:
        raise ValueError("Search fell back without using the real media vectors")
    if media_first and response.get("retrieval_strategy") != "media_first":
        raise ValueError("Search did not complete the media-first route")
    for row in response.get("results", []):
        if row.get("id") == file_id:
            score = row.get("multimodal_score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise ValueError("Retrieved file lacks a finite media similarity score")
            return {"file_id": file_id, "multimodal_score": score, "sources": row.get("sources", [])}
    raise ValueError("Synthetic image was not returned by the media search")


def synthetic_png() -> bytes:
    """Produce a new simple RGB fixture without any private media or dependencies."""
    side = 256
    raw = b"".join(b"\x00" + b"".join(
        b"\xe6\x14\x14" if 48 <= x < 208 and 48 <= y < 208 else b"\xff\xff\xff"
        for x in range(side)) for y in range(side))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.evidence = Path(args.evidence).resolve()
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.prefix = "nas-ai-native-" + uuid.uuid4().hex[:12]
        self.network = self.prefix + "-net"
        self.containers: dict[str, str] = {}
        self.volumes: list[str] = []
        self.network_created = False
        self.temporary = Path(tempfile.mkdtemp(prefix=self.prefix + "-"))
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.token = ""
        self.report = {"status": "running", "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "scope": ["native Linux containers", "application bootstrap and upload",
                                 "SQLite runtime and FULL write connections", "keyword content search",
                                 "real CPU text and image embeddings", "Qdrant media indexing",
                                 "application media-first text search", "application image search"],
                       "not_covered": ["physical NAS hardware", "production data", "Qwen/Ollama",
                                       "GPU", "video/audio", "ranking quality", "load/latency guarantees"],
                       "checks": {}}

    def record(self, name: str, value: object):
        self.report["checks"][name] = value
        self.save()
        print(f"PASS {name}", flush=True)

    def save(self):
        (self.evidence / "runtime.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def command(*args: str, timeout: int = 120) -> str:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr[-6000:]}")
        return result.stdout.strip()

    def docker(self, *args: str, timeout: int = 120) -> str:
        return self.command("docker", *args, timeout=timeout)

    def image_info(self, image: str) -> dict:
        return json.loads(self.docker("image", "inspect", "--format", "{{json .}}", image))

    def start(self, service: str, image: str, options: list[str], command: list[str] | None = None):
        name = self.prefix + "-" + service
        self.containers[service] = name  # Track failed creations, too.
        self.docker("run", "--detach", "--name", name, "--network", self.network,
                    "--network-alias", service, "--label", "nas-ai.runtime-smoke=" + self.prefix,
                    "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL",
                    *options, image, *(command or []), timeout=180)

    def port(self, service: str, port: int) -> str:
        value = self.docker("port", self.containers[service], str(port) + "/tcp").splitlines()[0]
        if not value.startswith("127.0.0.1:"):
            raise ValueError("Smoke-test ports must bind only to loopback")
        return "http://" + value

    def request(self, base: str, path: str, *, method="GET", payload=None, raw=None,
                headers=None, timeout=15, decode=True):
        request_headers = dict(headers or {})
        if base == getattr(self, "app_url", None) and self.token:
            request_headers["Authorization"] = "Bearer " + self.token
        if payload is not None:
            raw = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + path, data=raw, headers=request_headers, method=method)
        with self.http.open(request, timeout=timeout) as response:
            body = response.read()
            return json.loads(body) if decode else body

    def running(self):
        for service, name in self.containers.items():
            state = json.loads(self.docker("inspect", "--format", "{{json .State}}", name, timeout=15))
            if not state["Running"]:
                raise RuntimeError(f"{service} stopped (exit={state['ExitCode']}, OOM={state['OOMKilled']})")

    def wait(self, label: str, callback, timeout: int):
        deadline, next_progress, last_error = time.monotonic() + timeout, 0.0, "not ready"
        while time.monotonic() < deadline:
            self.running()
            try:
                value = callback()
                if value:
                    return value
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = str(exc)
            if time.monotonic() >= next_progress:
                print(f"Waiting for {label} ({max(0, int(deadline - time.monotonic()))} s left)", flush=True)
                next_progress = time.monotonic() + 30
            time.sleep(2)
        raise TimeoutError(f"{label} timed out after {timeout} s; last response: {last_error}")

    def python(self, service: str, code: str, *, timeout=60):
        executable = "python3" if service in {"multimodal", "multimodal-query"} else "python"
        return json.loads(self.docker("exec", self.containers[service], executable, "-c", code, timeout=timeout))

    def check_container(self, service: str):
        result = self.python(service, "import json,platform; print(json.dumps({'system':platform.system(),'machine':platform.machine()}))")
        if result["system"] != "Linux" or architecture(result["machine"]) != self.arch:
            raise ValueError(f"{service} does not run the expected native architecture")
        self.record(service + "_architecture", result)

    def run(self):
        manifest = json.loads((REPO / "deploy/embeddinggemma2-manifest.json").read_text())
        (self.evidence / "model-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        daemon = json.loads(self.docker("info", "--format", "{{json .}}"))
        images = [self.image_info(self.args.app_image), self.image_info(self.args.media_image)]
        self.arch = require_native(platform.system(), platform.machine(), daemon, images)
        self.record("native_platform", {"uname": list(platform.uname()), "architecture": self.arch,
                                       "docker_architecture": daemon["Architecture"],
                                       "docker_version": daemon["ServerVersion"],
                                       "images": [{k: image[k] for k in ("Id", "Os", "Architecture")} for image in images]})
        # Qdrant is the public Compose dependency; never point this test at an existing database.
        self.docker("pull", self.args.qdrant_image, timeout=300)
        qdrant_image = self.image_info(self.args.qdrant_image)
        require_native(platform.system(), platform.machine(), daemon, [qdrant_image])
        self.record("qdrant_image", {k: qdrant_image[k] for k in ("Id", "Os", "Architecture")})
        self.docker("network", "create", self.network)
        self.network_created = True
        for suffix in ("models", "qdrant"):
            name = self.prefix + "-" + suffix
            self.docker("volume", "create", name)
            self.volumes.append(name)
        model_volume, qdrant_volume = self.volumes
        self.start("qdrant", self.args.qdrant_image, ["--publish", "127.0.0.1::6333", "--memory", "768m",
                   "--mount", f"type=volume,src={qdrant_volume},dst=/qdrant/storage"])
        self.qdrant_url = self.port("qdrant", 6333)
        self.wait("Qdrant", lambda: self.request(self.qdrant_url, "/readyz", decode=False), 90)
        self.start("multimodal", self.args.media_image, ["--publish", "127.0.0.1::8080", "--memory", "3g",
                   "--cpus", "2", "--mount", f"type=volume,src={model_volume},dst=/models"])
        self.media_url = self.port("multimodal", 8080)
        health = self.wait("pinned media model download and CPU startup",
                           lambda: self.request(self.media_url, "/health"), self.args.model_timeout)
        if health.get("status") != "ok":
            raise ValueError(f"Unexpected model health: {health}")
        self.record("multimodal_health", health)
        self.start("multimodal-query", self.args.media_image, ["--publish", "127.0.0.1::8080", "--memory", "768m",
                   "--memory-swap", "768m", "--cpus", "2", "--read-only", "--tmpfs", "/tmp:size=64m",
                   "--ulimit", "memlock=536870912:536870912", "--env", "NAS_AI_MEDIA_MODE=query",
                   "--mount", f"type=volume,src={model_volume},dst=/models,readonly"])
        self.query_url = self.port("multimodal-query", 8080)
        health = self.wait("CPU text-query service", lambda: self.request(self.query_url, "/health"), 180)
        if health.get("status") != "ok":
            raise ValueError(f"Unexpected query health: {health}")
        self.record("query_health", health)
        for service in ("multimodal", "multimodal-query"):
            self.check_container(service)
        # Inspect the actually executing process and independently rehash every model.
        runtime = self.python("multimodal", """
import hashlib,json,os,platform
from pathlib import Path
manifest=json.loads(Path('/opt/manifest.json').read_text())
files={}
for name in manifest['files']:
    path=Path('/models')/name
    with path.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    files[name]={'size':path.stat().st_size,'sha256':digest}
exe=Path('/proc/1/exe')
header=exe.open('rb').read(20)
print(json.dumps({'files':files,'executable':os.readlink(exe),'elf_machine':int.from_bytes(header[18:20],'little'),
                  'command':Path('/proc/1/cmdline').read_bytes().replace(b'\\0',b' ').decode()}))
""", timeout=180)
        if runtime["files"] != manifest["files"]:
            raise ValueError("Downloaded model bytes differ from the pinned manifest")
        if runtime["elf_machine"] != {"amd64": 62, "arm64": 183}[self.arch]:
            raise ValueError("Running llama-server ELF architecture differs from the host")
        expected_executable = f"/models/runtime-{self.arch}-{manifest['runtimes'][self.arch]['sha256']}/llama-server"
        if runtime["executable"] != expected_executable:
            raise ValueError("Running media executable is not from the pinned architecture-specific runtime")
        if "--n-gpu-layers 0" not in runtime["command"] or "--no-mmproj-offload" not in runtime["command"]:
            raise ValueError("Media runtime is not using the CPU-only configuration")
        self.record("verified_models_and_runtime", runtime)
        query_runtime = self.python("multimodal-query", """
import json,os
from pathlib import Path
header=Path('/proc/1/exe').open('rb').read(20)
print(json.dumps({'executable':os.readlink('/proc/1/exe'),'elf_machine':int.from_bytes(header[18:20],'little'),
                  'command':Path('/proc/1/cmdline').read_bytes().replace(b'\\0',b' ').decode()}))
""")
        if (query_runtime["elf_machine"] != runtime["elf_machine"]
                or query_runtime["executable"] != expected_executable
                or "--n-gpu-layers 0" not in query_runtime["command"]):
            raise ValueError("Query runtime is not the expected native CPU executable")
        self.record("query_cpu_runtime", query_runtime)
        for directory in ("data", "library", "uploads", "recycle"):
            (self.temporary / directory).mkdir()
        environment = {
            "NAS_AI_DATA_DIR": "/app/data", "NAS_AI_CACHE_DIR": "/app/data/cache",
            "NAS_AI_SCAN_ROOT": "/library", "NAS_AI_UPLOAD_ROOT": "/uploads", "NAS_AI_RECYCLE_ROOT": "/recycle",
            "NAS_AI_WATCH_ENABLED": "false", "NAS_AI_AUTO_INDEX_ENABLED": "false",
            "NAS_AI_AUTOMATIC_BACKUP_ENABLED": "false", "NAS_AI_TASK_WORKERS": "1", "NAS_AI_INDEX_WORKERS": "1",
            "NAS_AI_MIN_FREE_SWAP_MB": "0", "NAS_AI_MULTIMODAL_MEMORY_FLOOR_MB": "512",
            "NAS_AI_MULTIMODAL_ENABLED": "true", "NAS_AI_MULTIMODAL_URL": "http://multimodal:8080",
            "NAS_AI_MULTIMODAL_QUERY_URL": "http://multimodal-query:8080", "NAS_AI_MULTIMODAL_POLL_SECONDS": "2",
            "NAS_AI_QDRANT_URL": "http://qdrant:6333", "NAS_AI_MULTIMODAL_COLLECTION": COLLECTION,
            # Keep the Qwen text lane unconfigured: test its keyword fallback without pulling a second model.
            "NAS_AI_EMBEDDING_MODEL": "", "NAS_AI_VISION_MODEL": "", "NAS_AI_CHAT_MODEL": "",
            "NAS_AI_LOCAL_AI_URL": "", "NAS_AI_TRANSCRIPTION_URL": "",
        }
        shared = ["--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--tmpfs", "/tmp:size=256m"]
        for key, value in environment.items():
            shared += ["--env", f"{key}={value}"]
        for directory, target in (("data", "/app/data"), ("library", "/library"),
                                  ("uploads", "/uploads"), ("recycle", "/recycle")):
            shared += ["--mount", f"type=bind,src={self.temporary / directory},dst={target}" +
                       (",readonly" if directory == "library" else "")]
        self.start("app", self.args.app_image, [*shared, "--publish", "127.0.0.1::8000", "--memory", "2g"])
        self.app_url = self.port("app", 8000)
        health = self.wait("application", lambda: self.request(self.app_url, "/api/health"), 120)
        if health.get("ok") is not True or health.get("bootstrap_required") is not True:
            raise ValueError("Application did not start with a clean, uninitialized database")
        self.record("app_health", health)
        self.check_container("app")
        sqlite = self.python("app", """
import json,sqlite3
from app.config import settings
from app.database import Database
with Database(settings.database_path).connect() as connection:
    result={'version':sqlite3.sqlite_version,'version_info':list(sqlite3.sqlite_version_info),
            'journal_mode':connection.execute('PRAGMA journal_mode').fetchone()[0],
            'write_connection_synchronous':connection.execute('PRAGMA synchronous').fetchone()[0],
            'integrity_check':connection.execute('PRAGMA integrity_check').fetchone()[0]}
print(json.dumps(result))
""")
        if tuple(sqlite["version_info"]) < (3, 51, 3) or sqlite["write_connection_synchronous"] != 2:
            raise ValueError(f"SQLite version/FULL write-connection requirement failed: {sqlite}")
        if sqlite["journal_mode"] != "wal" or sqlite["integrity_check"] != "ok":
            raise ValueError(f"SQLite WAL/integrity requirement failed: {sqlite}")
        self.record("sqlite", sqlite)
        owner = self.request(self.app_url, "/api/auth/bootstrap", method="POST", payload={
            "username": "runtime-test", "display_name": "Synthetic acceptance", "password": uuid.uuid4().hex})
        self.token = owner["token"]
        if self.request(self.app_url, "/api/health")["bootstrap_required"]:
            raise ValueError("Initial account setup did not persist")
        self.record("bootstrap", {"completed": True})
        png = synthetic_png()
        (self.evidence / "synthetic-fixture.png").write_bytes(png)
        opaque = uuid.uuid4().hex
        image = self.upload("fixture-" + opaque + ".png", png, "image/png")
        text_marker = "nativeacceptance" + uuid.uuid4().hex
        document = self.upload("document-" + opaque + ".txt", ("Synthetic fixture content: " + text_marker).encode(), "text/plain")
        self.record("upload", {"image_file_id": image["file"]["id"], "document_file_id": document["file"]["id"],
                               "image_sha256": hashlib.sha256(png).hexdigest()})
        self.wait("content extraction", lambda: self.keyword_hit(text_marker, document["file"]["id"]), 120)
        self.record("keyword_content_search", {"file_id": document["file"]["id"], "semantic": False,
                                                "query_in_filename": False})
        text_vector = self.embedding(self.query_url, "task: search result | query: " + QUERY)
        self.record("real_text_embedding", text_vector)
        image_content = {"content": [{"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}
        self.record("real_image_embedding", self.embedding(self.media_url, image_content))
        self.start("indexer", self.args.app_image, [*shared, "--memory", "768m", "--cpus", "1"],
                   ["python", "-m", "app.services.multimodal_worker"])
        self.check_container("indexer")
        point = self.wait("production media worker to index the synthetic image",
                          lambda: self.indexed_point(image["file"]["id"]), self.args.inference_timeout)
        self.record("qdrant_media_vector", {"file_id": image["file"]["id"], "point_id": point["id"],
                                          **vector_summary(point["vector"]), "payload": point["payload"]})
        params = urllib.parse.urlencode({"q": QUERY, "kind": "image", "media_first": "true", "precise": "false", "limit": 5})
        response = self.request(self.app_url, "/api/search?" + params, timeout=self.args.inference_timeout)
        self.record("app_media_first_search", {**require_media_hit(response, image["file"]["id"], media_first=True),
                                              "retrieval_strategy": response["retrieval_strategy"], "query": QUERY})
        response = self.request(self.app_url, "/api/search/image?kind=image&limit=5", method="POST", raw=png,
                                headers={"Content-Type": "image/png"}, timeout=self.args.inference_timeout)
        self.record("app_image_search", require_media_hit(response, image["file"]["id"]))
        self.running()
        self.report["status"] = "passed"

    def upload(self, name, data, content_type):
        return self.request(self.app_url, "/api/uploads", method="POST", raw=data,
                            headers={"X-Filename": name, "Content-Type": content_type})

    def keyword_hit(self, query, file_id):
        params = urllib.parse.urlencode({"q": query, "semantic": "false", "precise": "false"})
        result = self.request(self.app_url, "/api/search?" + params)
        return any(row["id"] == file_id for row in result.get("results", []))

    def embedding(self, base, content):
        started = time.monotonic()
        response = self.request(base, "/v1/embeddings", method="POST", payload={
            "model": "embeddinggemma2", "input": [content], "encoding_format": "float"}, timeout=self.args.inference_timeout)
        if len(response.get("data", [])) != 1:
            raise ValueError("Embedding response did not contain exactly one result")
        return {**vector_summary(response["data"][0]["embedding"]), "elapsed_seconds": round(time.monotonic() - started, 3)}

    def indexed_point(self, file_id):
        response = self.request(self.qdrant_url, f"/collections/{COLLECTION}/points/scroll", method="POST", payload={
            "filter": {"must": [{"key": "file_id", "match": {"value": file_id}}]},
            "limit": 1, "with_payload": True, "with_vector": True})
        points = response["result"]["points"]
        return points[0] if points else None

    def cleanup(self):
        errors = []
        for service, name in reversed(list(self.containers.items())):
            try:
                # docker logs sends the container's stderr to the CLI's stderr.
                result = subprocess.run(["docker", "logs", "--timestamps", name], capture_output=True, text=True, timeout=20)
                (self.evidence / (service + ".log")).write_text(result.stdout + result.stderr)
                state = json.loads(self.docker("inspect", "--format", "{{json .State}}", name, timeout=15))
                (self.evidence / (service + "-state.json")).write_text(json.dumps(state, indent=2) + "\n")
            except Exception as exc:
                errors.append(f"collect {service}: {exc}")
            try:
                self.docker("rm", "--force", name, timeout=30)
            except Exception as exc:
                errors.append(f"remove {service}: {exc}")
        for name in self.volumes:
            try:
                self.docker("volume", "rm", name, timeout=30)
            except Exception as exc:
                errors.append(f"remove volume: {exc}")
        if self.network_created:
            try:
                self.docker("network", "rm", self.network, timeout=30)
            except Exception as exc:
                errors.append(f"remove network: {exc}")
        try:
            shutil.rmtree(self.temporary)
        except Exception as exc:
            errors.append(f"remove temporary data: {exc}")
        self.report["cleanup_errors"] = errors
        if errors and self.report["status"] == "passed":
            self.report["status"] = "failed"
        self.report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-image", default="nas-ai-space:test")
    parser.add_argument("--media-image", default="nas-ai-space:test-media")
    parser.add_argument("--qdrant-image", default="qdrant/qdrant:v1.18.2-unprivileged")
    parser.add_argument("--evidence", default="runtime-evidence")
    parser.add_argument("--model-timeout", type=int, default=1200)
    parser.add_argument("--inference-timeout", type=int, default=300)
    args = parser.parse_args()
    if args.model_timeout <= 0 or args.inference_timeout <= 0:
        parser.error("timeouts must be positive")
    acceptance = Acceptance(args)

    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"Interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        acceptance.run()
    except BaseException as exc:
        acceptance.report["status"] = "failed"
        acceptance.report["error"] = f"{type(exc).__name__}: {exc}"
        (acceptance.evidence / "failure.log").write_text(traceback.format_exc())
        print(acceptance.report["error"], flush=True)
    finally:
        acceptance.cleanup()
    print(f"Native runtime acceptance {acceptance.report['status']}; evidence: {acceptance.evidence}", flush=True)
    return 0 if acceptance.report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
