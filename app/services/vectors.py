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
        self._client_lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._dimension: int | None = None

    def _http(self) -> httpx.Client:
        # 复用同一 httpx.Client（连接池），避免每次操作重建 TCP 连接；
        # httpx.Client 线程安全，懒初始化；各请求按需传 timeout 覆盖默认值
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
        return self._client

    def health(self) -> dict[str, Any]:
        try:
            response = self._http().get(f"{self.settings.qdrant_url}/readyz", timeout=2)
            return {"reachable": response.is_success, "status": response.status_code}
        except Exception as exc:
            return {"reachable": False, "error": str(exc)}

    def _ensure_collection(self, dimension: int) -> None:
        if self._dimension == dimension:
            return
        with self._lock:
            if self._dimension == dimension:
                return
            client = self._http()
            response = client.get(
                f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}",
                timeout=10,
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
                    timeout=10,
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
        points = self._file_points(file, chunks)
        if not points:
            return
        dimension = len(points[0]["vector"])
        self._ensure_collection(dimension)
        client = self._http()
        client.post(
            f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/delete?wait=false",
            json={"filter": {"must": [{"key": "file_id", "match": {"value": int(file["id"])}}]}},
            timeout=60,
        )
        response = client.put(
            f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points?wait=false",
            json={"points": points},
            timeout=60,
        )
        response.raise_for_status()

    @staticmethod
    def _file_points(file: dict[str, Any], chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        points = []
        for index, chunk in enumerate(chunks):
            if not chunk.get("embedding"):
                continue
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
        return points

    def file_point_ids(self, file_id: int) -> list[int]:
        return [int(point["id"]) for point in self.file_points(file_id)]

    def file_points(self, file_id: int) -> list[dict[str, Any]]:
        points: list[dict[str, Any]] = []
        offset: int | str | None = None
        while True:
            payload: dict[str, Any] = {
                "filter": {"must": [{"key": "file_id", "match": {"value": int(file_id)}}]},
                "limit": 1000,
                "with_payload": True,
                "with_vector": True,
            }
            if offset is not None:
                payload["offset"] = offset
            response = self._http().post(
                f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/scroll",
                json=payload,
                timeout=60,
            )
            if response.status_code == 404:
                return []
            response.raise_for_status()
            result = response.json().get("result", {})
            batch = result.get("points", [])
            points.extend(
                {"id": point["id"], "vector": point.get("vector"), "payload": point.get("payload") or {}}
                for point in batch
                if point.get("id") is not None and isinstance(point.get("vector"), list)
            )
            offset = result.get("next_page_offset")
            if offset is None or not batch:
                return points

    def file_point_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        offset: int | str | None = None
        while True:
            payload: dict[str, Any] = {
                "limit": 10000,
                "with_payload": ["file_id"],
                "with_vector": False,
            }
            if offset is not None:
                payload["offset"] = offset
            response = self._http().post(
                f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/scroll",
                json=payload,
                timeout=60,
            )
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            result = response.json().get("result", {})
            points = result.get("points", [])
            for point in points:
                file_id = int((point.get("payload") or {}).get("file_id") or 0)
                if file_id:
                    counts[file_id] = counts.get(file_id, 0) + 1
            offset = result.get("next_page_offset")
            if offset is None or not points:
                break
        return counts

    def stage_file(self, file: dict[str, Any], chunks: list[dict[str, Any]]) -> list[int]:
        points = self._file_points(file, chunks)
        if not points:
            raise ValueError("没有可写入的向量")
        self._ensure_collection(len(points[0]["vector"]))
        client = self._http()
        response = client.put(
            f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points?wait=true",
            json={"points": points},
            timeout=60,
        )
        response.raise_for_status()
        return [int(point["id"]) for point in points]

    def delete_points(self, point_ids: list[int]) -> None:
        if not point_ids:
            return
        response = self._http().post(
            f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/delete?wait=true",
            json={"points": [int(point_id) for point_id in point_ids]},
            timeout=60,
        )
        response.raise_for_status()

    def restore_points(self, points: list[dict[str, Any]]) -> None:
        if not points:
            return
        response = self._http().put(
            f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points?wait=true",
            json={"points": points},
            timeout=60,
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
        client = self._http()
        response = client.post(
            f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/query",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return response.json().get("result", {}).get("points", [])

    def representative_vector(self, file_id: int) -> list[float]:
        client = self._http()
        response = client.post(
            f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/scroll",
            json={
                "filter": {"must": [{"key": "file_id", "match": {"value": file_id}}]},
                "limit": 8,
                "with_payload": False,
                "with_vector": True,
            },
            timeout=30,
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
        response = self._http().post(
            f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/delete?wait=false",
            json={"filter": {"must": [{"key": "file_id", "match": {"any": file_ids}}]}},
            timeout=30,
        )
        if response.status_code not in {200, 404}:
            response.raise_for_status()

    def delete_library(self, library_id: int) -> None:
        response = self._http().post(
            f"{self.settings.qdrant_url}/collections/{self.settings.qdrant_collection}/points/delete?wait=false",
            json={"filter": {"must": [{"key": "library_id", "match": {"value": library_id}}]}},
            timeout=30,
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
            client = self._http()
            exists = client.get(f"{self.settings.qdrant_url}/collections/{collection}", timeout=30)
            if exists.status_code == 404:
                raise RuntimeError("向量集合尚未建立")
            exists.raise_for_status()
            response = client.post(f"{self.settings.qdrant_url}/collections/{collection}/snapshots", timeout=30)
            response.raise_for_status()
            result = response.json().get("result") or {}
            name = Path(str(result.get("name") or "")).name
            if not name:
                raise RuntimeError("Qdrant 未返回快照名称")
            destination = directory / name
            try:
                with client.stream(
                    "GET",
                    f"{self.settings.qdrant_url}/collections/{collection}/snapshots/{name}",
                    timeout=600,
                ) as download:
                    download.raise_for_status()
                    with destination.open("wb") as handle:
                        for chunk in download.iter_bytes():
                            handle.write(chunk)
                destination.chmod(0o600)
            finally:
                try:
                    client.delete(
                        f"{self.settings.qdrant_url}/collections/{collection}/snapshots/{name}",
                        timeout=30,
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
        with self._snapshot_lock, source.open("rb") as handle:
            response = self._http().post(
                f"{self.settings.qdrant_url}/collections/{collection}/snapshots/upload",
                params={"priority": "snapshot"},
                files={"snapshot": (safe_name, handle, "application/octet-stream")},
                timeout=900,
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
