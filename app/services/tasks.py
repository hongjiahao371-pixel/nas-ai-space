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
from app.database import Database
from app.services.extractors import index_file
from app.services.faces import analyze_people
from app.services.hardware import detect_hardware, memory_runtime
from app.services.ingest import collect_project_inbox
from app.services.local_ai import LocalAIClient
from app.services.albums import analyze_events, analyze_places
from app.services.organizer import analyze_duplicates, analyze_similar
from app.services.proxy import generate_look_preview, generate_proxy
from app.services.scanner import scan_library
from app.services.vectors import VectorStore


logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, database: Database, settings: Settings, ai: LocalAIClient, vectors: VectorStore):
        self.database = database
        self.settings = settings
        self.ai = ai
        self.vectors = vectors
        self.queue: asyncio.PriorityQueue[tuple[int, int]] = asyncio.PriorityQueue()
        self.workers: list[asyncio.Task] = []
        self.scheduler: asyncio.Task | None = None
        self.maintenance: asyncio.Task | None = None
        self.scan_locks: dict[int, asyncio.Lock] = {}
        self.stopping = False
        self.completed_since_prune = 0
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
                await self.queue.put((-int(task["priority"]), task_id))
        count = self.settings.task_workers or max(1, min(2, detect_hardware().plan.index_workers))
        self.workers = [asyncio.create_task(self._worker(index), name=f"task-worker-{index}") for index in range(count)]
        self.scheduler = asyncio.create_task(self._auto_index_loop(), name="auto-index-scheduler")
        self.maintenance = asyncio.create_task(self._maintenance_loop(), name="maintenance-scheduler")

    async def stop(self) -> None:
        self.stopping = True
        if self.scheduler:
            self.scheduler.cancel()
        if self.maintenance:
            self.maintenance.cancel()
        for worker in self.workers:
            worker.cancel()
        tasks = [*self.workers]
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
        await self.queue.put((-priority, task_id))
        return task_id

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

    async def retry(self, task_id: int) -> None:
        self.database.reset_task(task_id)
        task = self.database.get_task(task_id)
        if task:
            await self.queue.put((-int(task["priority"]), task_id))

    async def _worker(self, _: int) -> None:
        while not self.stopping:
            _, task_id = await self.queue.get()
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
                    message = await self._index_file_ids(task_id, file_ids)
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
                else:
                    raise ValueError(f"未知任务类型：{task['type']}")
                if self.database.is_task_cancelled(task_id):
                    self.database.mark_task_cancelled(task_id)
                else:
                    self.database.finish_task(task_id, message)
            except InterruptedError:
                self.database.mark_task_cancelled(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.database.fail_task(task_id, f"{type(exc).__name__}: {exc}")
            finally:
                self.completed_since_prune += 1
                if self.completed_since_prune >= 100:
                    self.completed_since_prune = 0
                    await asyncio.to_thread(
                        self.database.prune_tasks,
                        self.settings.task_retention_count,
                        self.settings.task_retention_days,
                    )
                self.queue.task_done()

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
            try:
                await asyncio.to_thread(self.vectors.delete_files, result.get("removed_file_ids", []))
            except Exception:
                pass
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
                final_status = self.database.finish_file_index(
                    file_id,
                    result,
                    chunks,
                    self.settings.index_retry_max_attempts,
                    self.settings.index_retry_base_seconds,
                )
                if any(chunk.get("embedding") for chunk in chunks):
                    self.vectors.replace_file(file, chunks)
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

    async def _queue_album_refresh(self) -> None:
        await self.submit_unique("analyze_places", {"source": "automatic"}, priority=0)
        await self.submit_unique("analyze_events", {"source": "automatic"}, priority=0)

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
                if (
                    policy["enabled"]
                    and self._within_window(datetime.now().hour, policy["start_hour"], policy["end_hour"])
                    and self.database.active_task_count() == 0
                    and (pending_count > 0 or repair_count > 0)
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
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Automatic index scheduler failed")
            await asyncio.sleep(60)
