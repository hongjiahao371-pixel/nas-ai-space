from __future__ import annotations

import os
import threading
from array import array
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from app.config import Settings
from app.database import Database, utc_now


def _embedding_blob(values: Any) -> bytes:
    return array("f", (float(value) for value in values)).tobytes()


def _embedding_array(value: bytes) -> Any:
    import numpy as np

    result = np.frombuffer(value, dtype=np.float32).copy()
    norm = float(np.linalg.norm(result))
    return result / norm if norm else result


class FaceAnalyzer:
    def __init__(self, settings: Settings, cpu_threads: int | None = None):
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("人物识别组件未安装") from exc
        if not settings.face_detection_model.is_file() or not settings.face_recognition_model.is_file():
            raise RuntimeError("人物识别模型文件缺失")
        self.cv2 = cv2
        cv2.setNumThreads(max(1, min(4, cpu_threads or settings.task_workers or 2)))
        self.detector = cv2.FaceDetectorYN.create(
            str(settings.face_detection_model), "", (320, 320), 0.82, 0.3, 5000
        )
        self.recognizer = cv2.FaceRecognizerSF.create(str(settings.face_recognition_model), "")

    def _read(self, path: Path) -> Any:
        image = self.cv2.imread(str(path), self.cv2.IMREAD_COLOR)
        if image is not None:
            return image
        try:
            import numpy as np
            from PIL import Image, ImageOps

            with Image.open(path) as source:
                rgb = ImageOps.exif_transpose(source).convert("RGB")
                return self.cv2.cvtColor(np.asarray(rgb), self.cv2.COLOR_RGB2BGR)
        except Exception:
            return None

    def analyze(self, path: Path) -> list[dict[str, Any]]:
        image = self._read(path)
        if image is None:
            raise RuntimeError("无法解码图片")
        original_height, original_width = image.shape[:2]
        scale = min(1.0, 1600.0 / max(original_width, original_height))
        if scale < 1:
            image = self.cv2.resize(
                image,
                (max(1, round(original_width * scale)), max(1, round(original_height * scale))),
                interpolation=self.cv2.INTER_AREA,
            )
        height, width = image.shape[:2]
        self.detector.setInputSize((width, height))
        _, detected = self.detector.detect(image)
        if detected is None:
            return []
        faces = []
        for item in detected:
            if float(item[2]) < 20 or float(item[3]) < 20:
                continue
            try:
                aligned = self.recognizer.alignCrop(image, item)
                feature = self.recognizer.feature(aligned).flatten()
            except Exception:
                continue
            faces.append({
                "x": max(0.0, float(item[0]) / scale),
                "y": max(0.0, float(item[1]) / scale),
                "width": min(float(original_width), float(item[2]) / scale),
                "height": min(float(original_height), float(item[3]) / scale),
                "confidence": float(item[14]),
                "embedding": _embedding_blob(feature),
            })
        return faces


def _store_face_results(database: Database, results: list[tuple[dict[str, Any], list[dict[str, Any]], str]]) -> None:
    if not results:
        return
    now = utc_now()
    with database.transaction() as connection:
        for file, faces, error in results:
            file_id = int(file["id"])
            connection.execute("DELETE FROM faces WHERE file_id = ?", (file_id,))
            connection.executemany(
                """INSERT INTO faces(file_id, face_index, x, y, width, height, confidence, embedding, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        file_id, index, face["x"], face["y"], face["width"], face["height"],
                        face["confidence"], face["embedding"], now,
                    )
                    for index, face in enumerate(faces)
                ],
            )
            connection.execute(
                """INSERT INTO face_scans(file_id, mtime_ns, face_count, status, error, scanned_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_id) DO UPDATE SET mtime_ns = excluded.mtime_ns,
                   face_count = excluded.face_count, status = excluded.status, error = excluded.error,
                   scanned_at = excluded.scanned_at""",
                (file_id, file["mtime_ns"], len(faces), "error" if error else "ready", error[:1000], now),
            )


def _cluster_faces(database: Database, threshold: float, cancelled: Callable[[], bool]) -> int:
    import numpy as np

    with database.transaction() as connection:
        auto_ids = [int(row[0]) for row in connection.execute("SELECT id FROM people WHERE is_named = 0")]
        if auto_ids:
            placeholders = ",".join("?" for _ in auto_ids)
            connection.execute(f"UPDATE faces SET person_id = NULL WHERE person_id IN ({placeholders})", auto_ids)
            connection.execute(f"DELETE FROM people WHERE id IN ({placeholders})", auto_ids)

    named_rows = database.fetchall(
        """SELECT p.id AS person_id, f.embedding FROM people p JOIN faces f ON f.person_id = p.id
           WHERE p.is_named = 1 ORDER BY p.id"""
    )
    named_values: dict[int, list[Any]] = {}
    for row in named_rows:
        named_values.setdefault(int(row["person_id"]), []).append(_embedding_array(row["embedding"]))
    named_centroids: dict[int, Any] = {}
    for person_id, values in named_values.items():
        centroid = np.mean(values, axis=0)
        named_centroids[person_id] = centroid / max(float(np.linalg.norm(centroid)), 1e-12)

    rows = database.fetchall("SELECT id, embedding FROM faces WHERE person_id IS NULL ORDER BY id")
    remaining: list[tuple[int, Any]] = []
    named_updates: list[tuple[int, int]] = []
    for index, row in enumerate(rows):
        if index % 100 == 0 and cancelled():
            raise InterruptedError("任务已取消")
        value = _embedding_array(row["embedding"])
        best_id = None
        best_score = threshold
        for person_id, centroid in named_centroids.items():
            score = float(np.dot(value, centroid))
            if score >= best_score:
                best_id = person_id
                best_score = score
        if best_id is None:
            remaining.append((int(row["id"]), value))
        else:
            named_updates.append((best_id, int(row["id"])))
    if named_updates:
        with database.transaction() as connection:
            connection.executemany("UPDATE faces SET person_id = ? WHERE id = ?", named_updates)

    clusters: list[dict[str, Any]] = []
    for index, (face_id, value) in enumerate(remaining):
        if index % 100 == 0 and cancelled():
            raise InterruptedError("任务已取消")
        best_index = -1
        best_score = threshold
        for cluster_index, cluster in enumerate(clusters):
            score = float(np.dot(value, cluster["centroid"]))
            if score >= best_score:
                best_index = cluster_index
                best_score = score
        if best_index < 0:
            clusters.append({"ids": [face_id], "sum": value.copy(), "centroid": value})
        else:
            cluster = clusters[best_index]
            cluster["ids"].append(face_id)
            cluster["sum"] += value
            norm = max(float(np.linalg.norm(cluster["sum"])), 1e-12)
            cluster["centroid"] = cluster["sum"] / norm

    now = utc_now()
    created = 0
    with database.transaction() as connection:
        for cluster in sorted((item for item in clusters if len(item["ids"]) >= 2), key=lambda item: -len(item["ids"])):
            created += 1
            cursor = connection.execute(
                "INSERT INTO people(name, is_named, face_count, created_at, updated_at) VALUES (?, 0, ?, ?, ?)",
                (f"人物 {created}", len(cluster["ids"]), now, now),
            )
            person_id = int(cursor.lastrowid)
            connection.executemany(
                "UPDATE faces SET person_id = ? WHERE id = ?",
                [(person_id, face_id) for face_id in cluster["ids"]],
            )
        connection.execute(
            """UPDATE people SET face_count = (SELECT COUNT(*) FROM faces WHERE person_id = people.id),
               cover_face_id = (SELECT id FROM faces WHERE person_id = people.id ORDER BY confidence * width * height DESC LIMIT 1),
               updated_at = ?""",
            (now,),
        )
    return created + len(named_centroids)


def analyze_people(
    database: Database,
    settings: Settings,
    progress: Callable[[float, str], None],
    cancelled: Callable[[], bool],
) -> dict[str, int]:
    files = database.fetchall(
        """SELECT f.* FROM files f LEFT JOIN face_scans fs ON fs.file_id = f.id
           WHERE f.kind = 'image' AND (fs.file_id IS NULL OR fs.mtime_ns != f.mtime_ns OR fs.status = 'error')
           ORDER BY f.id"""
    )
    total = len(files)
    logical_cpus = os.cpu_count() or 2
    workers = settings.face_workers or max(1, min(4, logical_cpus // 2))
    workers = min(workers, max(1, total))
    cpu_threads = max(1, logical_cpus // workers)
    analyzer = FaceAnalyzer(settings, cpu_threads) if workers == 1 else None
    thread_state = threading.local()

    def scan(file: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        nonlocal analyzer
        try:
            if workers == 1:
                current = analyzer
            else:
                current = getattr(thread_state, "analyzer", None)
                if current is None:
                    current = FaceAnalyzer(settings, cpu_threads)
                    thread_state.analyzer = current
            if current is None:
                raise RuntimeError("人物识别组件初始化失败")
            return file, current.analyze(Path(file["path"])), ""
        except Exception as exc:
            return file, [], f"{type(exc).__name__}: {exc}"

    def results():
        if workers == 1:
            for file in files:
                yield scan(file)
            return
        iterator = iter(files)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="face") as executor:
            pending = set()
            for _ in range(min(total, workers * 2)):
                pending.add(executor.submit(scan, next(iterator)))
            while pending:
                if cancelled():
                    for future in pending:
                        future.cancel()
                    raise InterruptedError("任务已取消")
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    yield future.result()
                    try:
                        pending.add(executor.submit(scan, next(iterator)))
                    except StopIteration:
                        pass

    detected = 0
    errors = 0
    batch: list[tuple[dict[str, Any], list[dict[str, Any]], str]] = []
    progress(0.0, f"使用 {workers} 路并行识别人脸")
    for index, (file, faces, error) in enumerate(results(), 1):
        if cancelled():
            raise InterruptedError("任务已取消")
        detected += len(faces)
        if error:
            errors += 1
        batch.append((file, faces, error))
        if len(batch) >= 20 or index == total:
            _store_face_results(database, batch)
            batch.clear()
        if index % 20 == 0 or index == total:
            value = 0.82 * index / max(1, total)
            progress(value, f"正在识别人脸 {index:,}/{total:,}，发现 {detected:,} 张")
    progress(0.84, f"开始聚类 {detected:,} 张新识别人脸")
    people = _cluster_faces(database, settings.face_match_threshold, cancelled)
    progress(1.0, f"识别完成人物 {people:,} 组，人脸 {detected:,} 张，失败 {errors:,}")
    total_faces = database.fetchone("SELECT COUNT(*) AS count FROM faces") or {"count": 0}
    return {"people": people, "faces": int(total_faces["count"]), "errors": errors}
