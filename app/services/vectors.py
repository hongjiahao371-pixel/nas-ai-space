from __future__ import annotations

import math
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings


class VectorStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._dimension: int | None = None

    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=2) as client:
                response = client.get(f"{self.settings.qdrant_url}/readyz")
            return {"reachable": response.is_success, "status": response.status_code}
        except Exception as exc:
            return {"reachable": False, "error": str(exc)}

    def _ensure_collection(self, dimension: int) -> None:
        if self._dimension == dimension:
            return
        with self._lock:
            if self._dimension == dimension:
                return
            with httpx.Client(timeout=10) as client:
                response = client.get(
                    f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}"
                )
                if response.status_code == 404:
                    create = client.put(
                        f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}",
                        json={
                            "vectors": {
                                "size": dimension,
                                "distance": "Cosine",
                                "on_disk": self.settings.vectors_on_disk,
                            },
                            "hnsw_config": {"m": 16, "ef_construct": 100, "on_disk": self.settings.vectors_on_disk},
                            "optimizers_config": {"indexing_threshold": 10000},
                        },
                    )
                    create.raise_for_status()
                else:
                    response.raise_for_status()
                    size = response.json()["result"]["config"]["params"]["vectors"]["size"]
                    if int(size) != dimension:
                        raise RuntimeError(
                            f"向量维度由 {size} 变为 {dimension}，请更换 NAS_AI_QDRANT_COLLECTION 后重新索引"
                        )
            self._dimension = dimension

    def replace_file(self, file: dict[str, Any], chunks: list[dict[str, Any]]) -> None:
        vectors = [(index, chunk) for index, chunk in enumerate(chunks) if chunk.get("embedding")]
        if not vectors:
            return
        dimension = len(vectors[0][1]["embedding"])
        self._ensure_collection(dimension)
        points = []
        for index, chunk in vectors:
            points.append({
                "id": int(file["id"]) * 1_000_000 + index,
                "vector": chunk["embedding"],
                "payload": {
                    "file_id": int(file["id"]),
                    "chunk_index": index,
                    "library_id": int(file["library_id"]),
                    "kind": file["kind"],
                    "path": file["relative_path"],
                    "content": chunk["content"][:4000],
                    "source_label": chunk.get("source_label", ""),
                    "start_time": chunk.get("start_time"),
                    "end_time": chunk.get("end_time"),
                },
            })
        with httpx.Client(timeout=60) as client:
            client.post(
                f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/delete?wait=true",
                json={"filter": {"must": [{"key": "file_id", "match": {"value": int(file["id"])}}]}},
            )
            response = client.put(
                f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points?wait=true",
                json={"points": points},
            )
            response.raise_for_status()

    def search(
        self,
        vector: list[float],
        limit: int,
        kind: str = "",
        library_ids: list[int] | None = None,
        file_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        if not vector:
            return []
        self._ensure_collection(len(vector))
        payload: dict[str, Any] = {"query": vector, "limit": limit, "with_payload": True}
        filters = []
        if kind:
            filters.append({"key": "kind", "match": {"value": kind}})
        if library_ids is not None:
            if not library_ids:
                return []
            filters.append({"key": "library_id", "match": {"any": library_ids}})
        if file_ids is not None:
            if not file_ids:
                return []
            filters.append({"key": "file_id", "match": {"any": file_ids}})
        if filters:
            payload["filter"] = {"must": filters}
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/query",
                json=payload,
            )
            response.raise_for_status()
            return response.json().get("result", {}).get("points", [])

    def representative_vector(self, file_id: int) -> list[float]:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/scroll",
                json={
                    "filter": {"must": [{"key": "file_id", "match": {"value": file_id}}]},
                    "limit": 8,
                    "with_payload": False,
                    "with_vector": True,
                },
            )
            if response.status_code == 404:
                return []
            response.raise_for_status()
        points = response.json().get("result", {}).get("points", [])
        vectors = [point.get("vector") for point in points if isinstance(point.get("vector"), list)]
        if not vectors:
            return []
        dimension = len(vectors[0])
        compatible = [vector for vector in vectors if len(vector) == dimension]
        average = [sum(vector[index] for vector in compatible) / len(compatible) for index in range(dimension)]
        norm = math.sqrt(sum(value * value for value in average))
        return [value / norm for value in average] if norm else average

    def delete_files(self, file_ids: list[int]) -> None:
        if not file_ids:
            return
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/delete?wait=true",
                json={"filter": {"must": [{"key": "file_id", "match": {"any": file_ids}}]}},
            )
            if response.status_code not in {200, 404}:
                response.raise_for_status()

    def delete_library(self, library_id: int) -> None:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/delete?wait=true",
                json={"filter": {"must": [{"key": "library_id", "match": {"value": library_id}}]}},
            )
            if response.status_code not in {200, 404}:
                response.raise_for_status()

    def list_snapshots(self) -> list[dict[str, Any]]:
        directory = self.settings.vector_backup_dir
        directory.mkdir(parents=True, exist_ok=True)
        return [
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "created_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            }
            for path in sorted(directory.glob("*.snapshot"), key=lambda item: item.stat().st_mtime, reverse=True)
        ]

    def create_snapshot(self, retain: int = 3) -> dict[str, Any]:
        with self._snapshot_lock:
            directory = self.settings.vector_backup_dir
            directory.mkdir(parents=True, exist_ok=True)
            collection = self.settings.qdrant_collection
            with httpx.Client(timeout=30) as client:
                exists = client.get(f"{self.settings.qdrant_url}/collections/{collection}")
                if exists.status_code == 404:
                    raise RuntimeError("向量集合尚未建立")
                exists.raise_for_status()
                response = client.post(f"{self.settings.qdrant_url}/collections/{collection}/snapshots")
                response.raise_for_status()
                result = response.json().get("result") or {}
                name = Path(str(result.get("name") or "")).name
                if not name:
                    raise RuntimeError("Qdrant 未返回快照名称")
            destination = directory / name
            try:
                with httpx.Client(timeout=600) as client:
                    with client.stream(
                        "GET",
                        f"{self.settings.qdrant_url}/collections/{collection}/snapshots/{name}",
                    ) as download:
                        download.raise_for_status()
                        with destination.open("wb") as handle:
                            for chunk in download.iter_bytes():
                                handle.write(chunk)
                destination.chmod(0o600)
            finally:
                try:
                    with httpx.Client(timeout=30) as client:
                        client.delete(
                            f"{self.settings.qdrant_url}/collections/{collection}/snapshots/{name}"
                        )
                except Exception:
                    pass
            snapshots = sorted(
                directory.glob("*.snapshot"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for old in snapshots[max(1, retain):]:
                old.unlink(missing_ok=True)
            return {
                "name": destination.name,
                "bytes": destination.stat().st_size,
                "retained": max(1, retain),
            }

    def restore_snapshot(self, name: str) -> dict[str, Any]:
        safe_name = Path(name).name
        if safe_name != name or not safe_name.endswith(".snapshot"):
            raise ValueError("快照名称无效")
        source = self.settings.vector_backup_dir / safe_name
        if not source.is_file():
            raise FileNotFoundError("向量快照不存在")
        collection = self.settings.qdrant_collection
        with self._snapshot_lock, source.open("rb") as handle, httpx.Client(timeout=900) as client:
            response = client.post(
                f"{self.settings.qdrant_url}/collections/{collection}/snapshots/upload",
                params={"priority": "snapshot"},
                files={"snapshot": (safe_name, handle, "application/octet-stream")},
            )
            response.raise_for_status()
        self._dimension = None
        return {"name": safe_name, "bytes": source.stat().st_size, "restored": True}

    def delete_snapshot(self, name: str) -> None:
        safe_name = Path(name).name
        if safe_name != name or not safe_name.endswith(".snapshot"):
            raise ValueError("快照名称无效")
        path = self.settings.vector_backup_dir / safe_name
        if not path.is_file():
            raise FileNotFoundError("向量快照不存在")
        path.unlink()
