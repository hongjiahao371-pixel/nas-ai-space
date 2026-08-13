from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any

from app.config import Settings
from app.database import Database, utc_now
from app.services.extractors import index_file, upgrade_image_caption
from app.services.faces import analyze_people
from app.services.hardware import detect_hardware, memory_runtime
from app.services.ingest import collect_project_inbox
from app.services.local_ai import LocalAIClient
from app.services.albums import analyze_events, analyze_places
from app.services.organizer import analyze_duplicates, analyze_similar
from app.services.proxy import generate_look_preview, generate_proxy
from app.services.productivity import ProductivityService
from app.services.scanner import scan_library
from app.services.vectors import VectorStore


logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, database: Database, settings: Settings, ai: LocalAIClient, vectors: VectorStore):
        self.database = database
        self.settings = settings
        self.ai = ai
        self.vectors = vectors
        self.productivity = ProductivityService(database, settings, ai)
        self.queue: asyncio.PriorityQueue[tuple[int, int]] = asyncio.PriorityQueue()
        self.light_queue: asyncio.PriorityQueue[tuple[int, int]] = asyncio.PriorityQueue()
        self.workers: list[asyncio.Task] = []
        self.light_workers: list[asyncio.Task] = []
        self.scheduler: asyncio.Task | None = None
        self.maintenance: asyncio.Task | None = None
        self.scan_locks: dict[int, asyncio.Lock] = {}
        self.stopping = False
        self.completed_since_prune = 0
        self._last_album_refresh_at = 0.0
        self.maintenance_state: dict[str, Any] = {
            "last_run_at": None,
            "last_backup": "",
            "last_pruned": 0,
            "last_error": "",
        }

    async def start(self) -> None:
        self.database.prune_tasks(self.settings.task_retention_count, self.settings.task_retention_days)
        for task_id in self.database.recover_tasks():
            task = self.database.get_task(task_id)
            if task:
                await self._enqueue(task["type"], int(task["priority"]), task_id)
        count = self.settings.task_workers or max(1, min(2, detect_hardware().plan.index_workers))
        self.workers = [asyncio.create_task(self._worker(index, self.queue), name=f"task-worker-{index}") for index in range(count)]
        self.light_workers = [asyncio.create_task(self._worker(0, self.light_queue), name="task-light-worker")]
        self.scheduler = asyncio.create_task(self._auto_index_loop(), name="auto-index-scheduler")
        self.maintenance = asyncio.create_task(self._maintenance_loop(), name="maintenance-scheduler")

    async def stop(self) -> None:
        self.stopping = True
        if self.scheduler:
            self.scheduler.cancel()
        if self.maintenance:
            self.maintenance.cancel()
        for worker in [*self.workers, *self.light_workers]:
            worker.cancel()
        tasks = [*self.workers, *self.light_workers]
        if self.scheduler:
            tasks.append(self.scheduler)
        if self.maintenance:
            tasks.append(self.maintenance)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def submit(
        self,
        task_type: str,
        payload: dict[str, Any],
        priority: int = 0,
        user_id: int | None = None,
    ) -> int:
        task_id = self.database.create_task(task_type, payload, priority, user_id)
        await self._enqueue(task_type, priority, task_id)
        return task_id

    async def _enqueue(self, task_type: str, priority: int, task_id: int) -> None:
        queue = self.light_queue if task_type == "scan_only" else self.queue
        await queue.put((-priority, task_id))

    async def submit_unique(
        self,
        task_type: str,
        payload: dict[str, Any],
        priority: int = 0,
        user_id: int | None = None,
    ) -> tuple[int, bool]:
        existing = self.database.active_task(task_type)
        if existing:
            return int(existing["id"]), True
        return await self.submit(task_type, payload, priority, user_id), False

    async def submit_unique_file(
        self,
        task_type: str,
        file_id: int,
        payload: dict[str, Any],
        priority: int = 0,
        user_id: int | None = None,
    ) -> tuple[int, bool]:
        # 按文件去重：同一文件已有排队/进行中的同类任务时直接复用，避免重复提交堆积重资源任务
        existing = self.database.active_task_with_file(task_type, file_id)
        if existing:
            return int(existing["id"]), True
        return await self.submit(task_type, payload, priority, user_id), False

    async def retry(self, task_id: int) -> None:
        self.database.reset_task(task_id)
        task = self.database.get_task(task_id)
        if task:
            await self._enqueue(task["type"], int(task["priority"]), task_id)

    def _notify_user(self, task: dict[str, Any], title: str, body: str) -> None:
        user_id = task.get("user_id")
        if user_id is None:
            return
        try:
            self.database.execute(
                """INSERT INTO notifications(user_id, type, title, body, target_type, target_id, created_at)
                   VALUES (?, 'task.finished', ?, ?, 'task', ?, ?)""",
                (int(user_id), title, body[:240], str(task["id"]), utc_now()),
            )
        except Exception:
            logger.exception("Failed to record task notification")

    async def _worker(self, _: int, queue: asyncio.PriorityQueue[tuple[int, int]]) -> None:
        while not self.stopping:
            _, task_id = await queue.get()
            try:
                task = self.database.get_task(task_id)
                if not task or task["cancel_requested"]:
                    self.database.mark_task_cancelled(task_id)
                    continue
                self.database.start_task(task_id)
                message = "完成"
                if task["type"] == "scan_library":
                    message = await self._scan_and_index(task_id, int(task["payload"]["library_id"]))
                elif task["type"] == "scan_only":
                    message = await self._scan_only(task_id, int(task["payload"]["library_id"]))
                elif task["type"] == "index_pending":
                    message = await self._index_pending(
                        task_id,
                        task["payload"].get("library_id"),
                        limit=task["payload"].get("limit"),
                        kind=str(task["payload"].get("kind") or ""),
                        order=str(task["payload"].get("order") or "balanced"),
                    )
                elif task["type"] == "index_files":
                    message = await self._index_file_ids(
                        task_id,
                        [int(value) for value in task["payload"].get("file_ids", [])],
                    )
                elif task["type"] == "restore_file":
                    message = await self._restore_file(
                        task_id,
                        int(task["payload"]["library_id"]),
                        str(task["payload"]["path"]),
                    )
                elif task["type"] == "upgrade_captions":
                    file_ids = self.database.caption_upgrade_file_ids(int(task["payload"].get("limit") or 50))
                    message = await self._upgrade_captions(task_id, file_ids)
                elif task["type"] == "repair_index":
                    file_ids = self.database.repair_file_ids(int(task["payload"].get("limit") or 50))
                    message = await self._index_file_ids(task_id, file_ids)
                elif task["type"] == "analyze_duplicates":
                    result = await asyncio.to_thread(
                        analyze_duplicates, self.database, self.settings,
                        lambda value, text: self.database.update_task(task_id, value, text),
                        lambda: self.database.is_task_cancelled(task_id),
                    )
                    message = f"发现 {result['groups']:,} 组重复文件"
                elif task["type"] == "analyze_similar":
                    result = await asyncio.to_thread(
                        analyze_similar, self.database, self.settings,
                        lambda value, text: self.database.update_task(task_id, value, text),
                        lambda: self.database.is_task_cancelled(task_id),
                    )
                    message = f"发现 {result['groups']:,} 组相似照片"
                elif task["type"] == "analyze_people":
                    result = await asyncio.to_thread(
                        analyze_people, self.database, self.settings,
                        lambda value, text: self.database.update_task(task_id, value, text),
                        lambda: self.database.is_task_cancelled(task_id),
                    )
                    message = f"识别 {result['people']:,} 组人物、{result['faces']:,} 张人脸"
                elif task["type"] == "analyze_places":
                    result = await asyncio.to_thread(
                        analyze_places, self.database,
                        lambda value, text: self.database.update_task(task_id, value, text),
                        lambda: self.database.is_task_cancelled(task_id),
                    )
                    message = f"生成 {result['places']:,} 个地点相册"
                elif task["type"] == "analyze_events":
                    result = await asyncio.to_thread(
                        analyze_events, self.database,
                        lambda value, text: self.database.update_task(task_id, value, text),
                        lambda: self.database.is_task_cancelled(task_id),
                    )
                    message = f"生成 {result['events']:,} 个事件相册"
                elif task["type"] == "generate_proxy":
                    result = await asyncio.to_thread(
                        generate_proxy,
                        self.database,
                        self.settings,
                        int(task["payload"]["version_id"]),
                        lambda value, text: self.database.update_task(task_id, value, text),
                        lambda: self.database.is_task_cancelled(task_id),
                    )
                    message = f"代理媒体已生成：版本 {result['version_id']}"
                elif task["type"] == "generate_look_preview":
                    result = await asyncio.to_thread(
                        generate_look_preview,
                        self.database,
                        self.settings,
                        int(task["payload"]["version_id"]),
                        int(task["payload"]["lut_file_id"]),
                        lambda value, text: self.database.update_task(task_id, value, text),
                        lambda: self.database.is_task_cancelled(task_id),
                    )
                    message = f"LUT 预览已生成：{result['look_name']}"
                elif task["type"] == "collect_project_inbox":
                    result = await asyncio.to_thread(
                        collect_project_inbox,
                        self.database,
                        self.settings,
                        int(task["payload"]["project_id"]),
                        task["payload"].get("user_id"),
                        lambda value, text: self.database.update_task(task_id, value, text),
                        lambda: self.database.is_task_cancelled(task_id),
                    )
                    index_message = await self._index_file_ids(
                        task_id,
                        result["pending_file_ids"],
                        start_progress=0.4,
                    )
                    message = (
                        f"入库 {result['files']:,} 个文件，解包 {result['packages_extracted']:,} 个素材包，"
                        f"新增 {result['assets_added']:,} 个素材；"
                        f"{index_message}"
                    )
                elif task["type"] == "generate_artifact":
                    artifact_id = int(task["payload"]["artifact_id"])
                    artifact = self.database.fetchone(
                        "SELECT status FROM artifacts WHERE id = ?", (artifact_id,)
                    )
                    if artifact and artifact["status"] == "ready":
                        version = self.database.fetchone(
                            """SELECT version_number FROM artifact_versions
                               WHERE artifact_id = ? ORDER BY version_number DESC LIMIT 1""",
                            (artifact_id,),
                        )
                        message = f"成果版本 V{int((version or {}).get('version_number') or 1)} 已生成"
                    else:
                        self.database.update_task(task_id, 0.1, "正在整理资料并生成成果")
                        try:
                            version = await asyncio.to_thread(
                                self.productivity.generate_artifact_version,
                                artifact_id,
                                str(task["payload"]["prompt"]),
                                [int(value) for value in task["payload"].get("file_ids", [])],
                                int(task["payload"]["user_id"]),
                            )
                        except Exception:
                            self.productivity.fail_artifact(artifact_id)
                            raise
                        message = f"成果版本 V{version['version_number']} 已生成"
                elif task["type"] == "agent_run":
                    run = await asyncio.to_thread(
                        self.productivity.execute_agent_run, int(task["payload"]["run_id"])
                    )
                    message = f"AI 任务 {run['id']} 已执行，共 {len(run['actions'])} 个动作"
                elif task["type"] == "automation_run":
                    result = await asyncio.to_thread(
                        self.productivity.execute_automation_run,
                        int(task["payload"]["automation_run_id"]),
                    )
                    message = f"自动化已完成，关联 AI 任务 {result['agent_run_id']}"
                else:
                    raise ValueError(f"未知任务类型：{task['type']}")
                if self.database.is_task_cancelled(task_id):
                    self.database.mark_task_cancelled(task_id)
                else:
                    self.database.finish_task_with_notification(
                        task_id,
                        int(task["user_id"]) if task.get("user_id") is not None else None,
                        message,
                    )
            except InterruptedError:
                self.database.mark_task_cancelled(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.database.fail_task_with_notification(
                    task_id,
                    int(task["user_id"]) if task.get("user_id") is not None else None,
                    error,
                )
            finally:
                self.completed_since_prune += 1
                if self.completed_since_prune >= 100:
                    self.completed_since_prune = 0
                    await asyncio.to_thread(
                        self.database.prune_tasks,
                        self.settings.task_retention_count,
                        self.settings.task_retention_days,
                    )
                queue.task_done()

    async def _scan_and_index(self, task_id: int, library_id: int) -> str:
        result = await self._scan(task_id, library_id)
        self.database.update_task(task_id, 0.1, f"扫描 {result['scanned']:,} 个文件，开始本地索引")
        message = await self._index_pending(task_id, library_id, start_progress=0.1)
        return f"扫描 {result['scanned']:,} 个文件；{message}"

    async def _restore_file(self, task_id: int, library_id: int, path: str) -> str:
        result = await self._scan(task_id, library_id)
        file = self.database.fetchone("SELECT id FROM files WHERE path = ?", (path,))
        if not file:
            raise FileNotFoundError("恢复后的文件未被媒体库扫描发现")
        self.database.update_task(task_id, 0.15, f"已恢复 {result['changed']:,} 个文件，开始重建索引")
        indexed = await self._index_file_ids(task_id, [int(file["id"])], start_progress=0.15)
        return f"文件已恢复；{indexed}"

    async def _scan_only(self, task_id: int, library_id: int) -> str:
        result = await self._scan(task_id, library_id)
        if result["changed"] or result["removed"]:
            await self._queue_album_refresh()
        return (
            f"扫描 {result['scanned']:,} 个文件，新增或变化 {result['changed']:,}，"
            f"移除 {result['removed']:,}"
        )

    async def _scan(self, task_id: int, library_id: int) -> dict[str, Any]:
        lock = self.scan_locks.setdefault(library_id, asyncio.Lock())
        async with lock:
            library = self.database.get_library(library_id)
            if not library:
                raise ValueError("媒体库不存在")

            def progress(value: float, message: str) -> None:
                self.database.update_task(task_id, value, message)

            result = await asyncio.to_thread(
                scan_library,
                self.database,
                library,
                progress,
                lambda: self.database.is_task_cancelled(task_id),
            )
            automation_run_ids = self.productivity.create_file_arrival_runs(
                [int(value) for value in result.get("changed_file_ids", [])]
            )
            for automation_run_id in automation_run_ids:
                await self.submit(
                    "automation_run", {"automation_run_id": automation_run_id}, priority=3
                )
            try:
                await asyncio.to_thread(self.vectors.delete_files, result.get("removed_file_ids", []))
            except Exception as exc:
                logger.warning(
                    "扫描后向量清理失败（%d 个文件）：%s",
                    len(result.get("removed_file_ids", [])),
                    exc,
                )
            return result

    async def _index_pending(
        self,
        task_id: int,
        library_id: int | None,
        start_progress: float = 0.0,
        limit: int | None = None,
        kind: str = "",
        order: str = "balanced",
    ) -> str:
        file_ids = self.database.pending_file_ids(
            int(library_id) if library_id is not None else None,
            int(limit) if limit is not None else None,
            kind,
            order,
        )
        return await self._index_file_ids(task_id, file_ids, start_progress)

    async def _index_file_ids(self, task_id: int, file_ids: list[int], start_progress: float = 0.0) -> str:
        total = len(file_ids)
        if not total:
            self.database.update_task(task_id, 1, "没有需要索引的文件")
            return "没有需要索引的文件"
        self.database.update_task(task_id, start_progress, f"准备处理 {total:,} 个文件", 0, total)

        plan = detect_hardware().plan
        max_workers = self.settings.index_workers or plan.index_workers
        max_workers = max(1, min(max_workers, total))
        indexed = 0
        partial = 0
        failed = 0
        pressure_lock = threading.Lock()
        low_memory_lock = threading.Lock()
        pressure_notice_at = 0.0

        def wait_for_resources(target: int, message: str) -> bool:
            nonlocal pressure_notice_at
            while not self.stopping and not self.database.is_task_cancelled(task_id):
                memory = memory_runtime()
                available = memory["available_bytes"]
                swap_free = max(0, memory["swap_total_bytes"] - memory["swap_used_bytes"])
                memory_ready = not target or not available or available >= target
                swap_ready = (
                    not self.settings.min_free_swap_bytes
                    or not memory["swap_total_bytes"]
                    or swap_free >= self.settings.min_free_swap_bytes
                )
                if memory_ready and swap_ready:
                    return True
                now = time.monotonic()
                with pressure_lock:
                    if now - pressure_notice_at >= 10:
                        pressure_notice_at = now
                        self.database.update_task(
                            task_id,
                            start_progress + (1 - start_progress) * (indexed + partial + failed) / total,
                            (
                                f"Swap 仅剩 {swap_free / 1024**2:.0f} MB，等待系统释放资源"
                                if not swap_ready
                                else f"可用内存仅 {available / 1024**2:.0f} MB，{message}"
                            ),
                        )
                time.sleep(2)
            return False

        def index_one(file_id: int) -> tuple[int, str, str]:
            file = self.database.get_file(file_id)
            if not file:
                return file_id, "error", "missing"
            try:
                result, chunks = index_file(file, self.settings, self.ai)
                stages = result.get("stages") or {}
                incomplete = any(
                    str((stages.get(name) or {}).get("status") or "") in {"error", "missing", "blocked"}
                    for name in ("vision", "transcription", "embedding")
                )
                previous_content = bool(
                    str(file.get("ai_caption") or file.get("extracted_text") or "").strip()
                    and self.database.fetchone(
                        "SELECT 1 AS found FROM content_chunks WHERE file_id = ? LIMIT 1", (file_id,)
                    )
                )
                if incomplete and previous_content:
                    errors = [
                        str((stages.get(name) or {}).get("error") or "")
                        for name in ("vision", "transcription", "embedding")
                    ]
                    error = "；".join(value for value in errors if value) or "新版索引不完整"
                    self.database.fail_file_index(
                        file_id,
                        f"新索引未提交，继续使用旧版：{error}",
                        self.settings.index_retry_max_attempts,
                        self.settings.index_retry_base_seconds,
                    )
                    return file_id, "partial", error
                embedded = any(chunk.get("embedding") for chunk in chunks)
                previous_points = self.vectors.file_points(file_id) if previous_content and embedded else []
                new_ids = self.vectors.stage_file(file, chunks) if embedded else []
                try:
                    final_status = self.database.finish_file_index(
                        file_id,
                        result,
                        chunks,
                        self.settings.index_retry_max_attempts,
                        self.settings.index_retry_base_seconds,
                    )
                except Exception:
                    if previous_points:
                        self.vectors.restore_points(previous_points)
                        previous_ids = {int(point["id"]) for point in previous_points}
                        self.vectors.delete_points([point_id for point_id in new_ids if point_id not in previous_ids])
                    else:
                        self.vectors.delete_points(new_ids)
                    raise
                if embedded:
                    new_id_set = set(new_ids)
                    try:
                        self.vectors.delete_points([
                            int(point["id"]) for point in previous_points if int(point["id"]) not in new_id_set
                        ])
                    except Exception:
                        logger.warning("Failed to remove stale index vectors for file %s", file_id, exc_info=True)
                return file_id, final_status, ""
            except Exception as exc:
                self.database.fail_file_index(
                    file_id,
                    f"{type(exc).__name__}: {exc}",
                    self.settings.index_retry_max_attempts,
                    self.settings.index_retry_base_seconds,
                )
                return file_id, "error", str(exc)

        def process(file_id: int) -> tuple[int, str, str]:
            if self.stopping or self.database.is_task_cancelled(task_id):
                return file_id, "cancelled", "cancelled"
            minimum = self.settings.min_available_memory_bytes
            available = memory_runtime()["available_bytes"]
            if minimum and available and available < minimum:
                emergency = max(256 * 1024 * 1024, minimum // 2)
                with low_memory_lock:
                    if not wait_for_resources(emergency, "进入低内存单路模式"):
                        return file_id, "cancelled", "cancelled"
                    return index_one(file_id)
            if not wait_for_resources(minimum, "等待系统释放资源"):
                return file_id, "cancelled", "cancelled"
            return index_one(file_id)

        loop = asyncio.get_running_loop()

        def run_pool() -> tuple[int, int, int]:
            nonlocal indexed, partial, failed
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="index") as executor:
                iterator = iter(file_ids)
                pending = set()
                for _ in range(max_workers * 2):
                    try:
                        pending.add(executor.submit(process, next(iterator)))
                    except StopIteration:
                        break
                while pending:
                    if self.database.is_task_cancelled(task_id):
                        for item in pending:
                            item.cancel()
                        break
                    completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        _, final_status, _ = future.result()
                        indexed += int(final_status == "ready")
                        partial += int(final_status == "partial")
                        failed += int(final_status not in {"ready", "partial", "cancelled"})
                        done = indexed + partial + failed
                        if done % 10 == 0 or done == total:
                            progress = start_progress + (1 - start_progress) * done / total
                            self.database.update_task(
                                task_id,
                                progress,
                                f"已处理 {done:,}/{total:,}，完整 {indexed:,}，部分完成 {partial:,}，失败 {failed:,}",
                                done,
                                total,
                            )
                        try:
                            pending.add(executor.submit(process, next(iterator)))
                        except StopIteration:
                            pass
            return indexed, partial, failed

        await loop.run_in_executor(None, run_pool)
        if indexed:
            await self._queue_album_refresh()
        remaining = int(self.database.pending_summary()["total"])
        return (
            f"本批完整索引 {indexed:,} 个，部分完成 {partial:,}，失败 {failed:,}，"
            f"全库待处理 {remaining:,}，待修复 {self.database.repair_count():,}"
        )

    async def _upgrade_captions(self, task_id: int, file_ids: list[int]) -> str:
        total = len(file_ids)
        if not total:
            self.database.update_task(task_id, 1, "没有需要升级的图片")
            return "没有需要升级的图片"
        self.database.update_task(task_id, 0, f"准备无损升级 {total:,} 张图片", 0, total)
        upgraded = 0
        preserved = 0
        consecutive_service_errors = 0
        transient_markers = (
            "503 Service Unavailable", "Connection refused", "Connection reset",
            "Server disconnected", "No address associated", "无法解析模型服务地址",
        )
        loop = asyncio.get_running_loop()

        def upgrade_one(file_id: int) -> tuple[bool, str]:
            file = self.database.get_file(file_id)
            if not file:
                return False, "文件记录不存在"
            previous_points = self.vectors.file_points(file_id)
            previous_ids = [int(point["id"]) for point in previous_points]
            last_error = ""
            for attempt, delay in enumerate((0, 5, 15), 1):
                if delay:
                    time.sleep(delay)
                try:
                    caption, metadata, chunks = upgrade_image_caption(file, self.settings, self.ai)
                    new_ids = self.vectors.stage_file(file, chunks)
                    try:
                        self.database.apply_caption_upgrade(file_id, caption, metadata, chunks)
                    except Exception:
                        if previous_points:
                            self.vectors.restore_points(previous_points)
                            previous_id_set = set(previous_ids)
                            self.vectors.delete_points([
                                point_id for point_id in new_ids if point_id not in previous_id_set
                            ])
                        else:
                            self.vectors.delete_points(new_ids)
                        raise
                    stale_ids = [point_id for point_id in previous_ids if point_id not in set(new_ids)]
                    try:
                        self.vectors.delete_points(stale_ids)
                    except Exception:
                        logger.warning("Failed to remove stale caption vectors for file %s", file_id, exc_info=True)
                    return True, ""
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if not any(marker in last_error for marker in transient_markers) or attempt == 3:
                        break
            self.database.record_caption_upgrade_failure(
                file_id,
                last_error,
                self.settings.index_retry_max_attempts,
                self.settings.index_retry_base_seconds,
            )
            return False, last_error

        for index, file_id in enumerate(file_ids, 1):
            if self.stopping or self.database.is_task_cancelled(task_id):
                break
            ok, error = await loop.run_in_executor(None, upgrade_one, file_id)
            if ok:
                upgraded += 1
                consecutive_service_errors = 0
            else:
                preserved += 1
                if any(marker in error for marker in transient_markers):
                    consecutive_service_errors += 1
                else:
                    consecutive_service_errors = 0
            self.database.update_task(
                task_id,
                index / total,
                f"已处理 {index:,}/{total:,}，升级 {upgraded:,}，保留旧版 {preserved:,}",
                index,
                total,
            )
            if consecutive_service_errors >= 3:
                raise RuntimeError("视觉服务连续不可用，已停止本批；旧描述和旧向量均已保留")
        return f"无损升级 {upgraded:,} 张，保留旧版等待重试 {preserved:,} 张"

    async def _queue_album_refresh(self) -> None:
        # 相册分析是全库重算，每批索引完成都刷代价太高：距上次排队不足
        # album_refresh_interval_seconds 且任务队列未排空时跳过（submit_unique 本身已保证
        # 队列中已有刷新任务时不重复排队）；队列排空后允许立即补刷，保证相册最终仍会更新。
        now = time.monotonic()
        if (
            now - self._last_album_refresh_at < self.settings.album_refresh_interval_seconds
            and not self.queue.empty()
        ):
            return
        _, places_existed = await self.submit_unique("analyze_places", {"source": "automatic"}, priority=0)
        _, events_existed = await self.submit_unique("analyze_events", {"source": "automatic"}, priority=0)
        if not (places_existed and events_existed):
            self._last_album_refresh_at = now

    def index_policy(self) -> dict[str, Any]:
        defaults = {
            "enabled": self.settings.auto_index_enabled,
            "start_hour": self.settings.auto_index_start_hour,
            "end_hour": self.settings.auto_index_end_hour,
            "batch_size": self.settings.auto_index_batch_size,
            "library_id": None,
            "kind": "",
            "order": "balanced",
        }
        saved = self.database.get_setting("index_policy", {})
        if isinstance(saved, dict):
            defaults.update(saved)
        return defaults

    def set_index_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        normalized = {
            "enabled": bool(policy.get("enabled")),
            "start_hour": max(0, min(23, int(policy.get("start_hour", 0)))),
            "end_hour": max(0, min(23, int(policy.get("end_hour", 7)))),
            "batch_size": max(1, min(10000, int(policy.get("batch_size", self.settings.index_batch_size)))),
            "library_id": int(policy["library_id"]) if policy.get("library_id") is not None else None,
            "kind": str(policy.get("kind") or ""),
            "order": str(policy.get("order") or "balanced"),
        }
        self.database.set_setting("index_policy", normalized)
        return normalized

    @staticmethod
    def _within_window(hour: int, start: int, end: int) -> bool:
        if start == end:
            return True
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def maintenance_status(self) -> dict[str, Any]:
        return dict(self.maintenance_state)

    async def _maintenance_loop(self) -> None:
        await asyncio.sleep(60)
        while not self.stopping:
            try:
                pruned = await asyncio.to_thread(
                    self.database.prune_tasks,
                    self.settings.task_retention_count,
                    self.settings.task_retention_days,
                )
                self.maintenance_state["last_pruned"] = pruned
                if self.settings.automatic_backup_enabled:
                    backup_directory = self.settings.data_dir / "backups"
                    backup_directory.mkdir(parents=True, exist_ok=True)
                    backups = sorted(
                        backup_directory.glob("nas-ai-space-*.db"),
                        key=lambda path: path.stat().st_mtime,
                        reverse=True,
                    )
                    latest_age = time.time() - backups[0].stat().st_mtime if backups else float("inf")
                    interval = self.settings.automatic_backup_interval_hours * 3600
                    if latest_age >= interval:
                        filename = f"nas-ai-space-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
                        destination = backup_directory / filename
                        await asyncio.to_thread(self.database.backup, destination)
                        backups = sorted(
                            backup_directory.glob("nas-ai-space-*.db"),
                            key=lambda path: path.stat().st_mtime,
                            reverse=True,
                        )
                        for path in backups[self.settings.automatic_backup_retention:]:
                            path.unlink(missing_ok=True)
                            path.with_suffix(path.suffix + ".verified").unlink(missing_ok=True)
                        self.database.audit(
                            "backup.automatic",
                            "system",
                            target_type="backup",
                            target_id=filename,
                            detail={"bytes": destination.stat().st_size},
                        )
                        self.maintenance_state["last_backup"] = filename
                self.maintenance_state["last_run_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                self.maintenance_state["last_error"] = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.maintenance_state["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
                logger.exception("Maintenance scheduler failed")
            await asyncio.sleep(3600)

    async def _auto_index_loop(self) -> None:
        await asyncio.sleep(15)
        while not self.stopping:
            try:
                for automation_run_id in self.productivity.create_due_schedule_runs():
                    await self.submit(
                        "automation_run", {"automation_run_id": automation_run_id}, priority=2
                    )
                policy = self.index_policy()
                memory = memory_runtime()
                available = memory["available_bytes"]
                swap_free = max(0, memory["swap_total_bytes"] - memory["swap_used_bytes"])
                swap_ready = (
                    not self.settings.min_free_swap_bytes
                    or not memory["swap_total_bytes"]
                    or swap_free >= self.settings.min_free_swap_bytes
                )
                repair_count = self.database.repair_count()
                pending_count = int(self.database.pending_summary()["total"])
                caption_count = self.database.caption_upgrade_count()
                if (
                    policy["enabled"]
                    and self._within_window(datetime.now().hour, policy["start_hour"], policy["end_hour"])
                    and self.database.active_task_count() == 0
                    and (pending_count > 0 or repair_count > 0 or caption_count > 0)
                    and (not available or available >= self.settings.min_available_memory_bytes)
                    and swap_ready
                ):
                    if repair_count:
                        await self.submit_unique(
                            "repair_index",
                            {"limit": min(50, policy["batch_size"]), "source": "schedule"},
                            priority=2,
                        )
                    else:
                        if pending_count:
                            await self.submit_unique(
                                "index_pending",
                                {
                                    "limit": policy["batch_size"],
                                    "library_id": policy["library_id"],
                                    "kind": policy["kind"],
                                    "order": policy["order"],
                                    "source": "schedule",
                                },
                                priority=1,
                            )
                        else:
                            await self.submit_unique(
                                "upgrade_captions",
                                {"limit": min(50, policy["batch_size"]), "source": "schedule"},
                                priority=1,
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Automatic index scheduler failed")
            await asyncio.sleep(60)
