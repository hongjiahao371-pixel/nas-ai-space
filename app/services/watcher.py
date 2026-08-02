from __future__ import annotations

import asyncio
import ctypes
import os
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import Database
from app.services.scanner import is_ignored


class LibraryWatcher:
    EVENT_STRUCT = struct.Struct("iIII")
    WATCH_MASK = 0x00000004 | 0x00000008 | 0x00000040 | 0x00000080 | 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800
    IN_ISDIR = 0x40000000
    IN_IGNORED = 0x00008000

    def __init__(self, database: Database, settings: Settings, tasks: Any):
        self.database = database
        self.settings = settings
        self.tasks = tasks
        self.fd = -1
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wd_paths: dict[int, Path] = {}
        self.wd_libraries: dict[int, int] = {}
        self.pending: dict[int, float] = {}
        self.signatures: dict[int, tuple[int, int, int]] = {}
        self.shallow: dict[int, tuple[tuple[tuple[Any, ...], ...], tuple[int, int, int]]] = {}
        self.background: list[asyncio.Task] = []
        self.mode = "disabled"
        self.error = ""
        self.last_event_at = ""
        self.last_poll_at = ""
        self.last_scan_at = ""

    async def start(self) -> None:
        if not self.settings.watch_enabled:
            return
        self.loop = asyncio.get_running_loop()
        if self._install_inotify():
            self.mode = "hybrid"
            self.loop.add_reader(self.fd, self._read_events)
        else:
            self.mode = "polling"
        self.signatures = await asyncio.to_thread(self._signatures)
        self.background = [
            asyncio.create_task(self._debounce_loop(), name="library-watch-debounce"),
            asyncio.create_task(self._poll_loop(), name="library-watch-poll"),
            asyncio.create_task(self._fallback_loop(), name="library-watch-fallback"),
        ]

    async def stop(self) -> None:
        for task in self.background:
            task.cancel()
        if self.background:
            await asyncio.gather(*self.background, return_exceptions=True)
        if self.loop and self.fd >= 0:
            self.loop.remove_reader(self.fd)
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _install_inotify(self) -> bool:
        if not sys.platform.startswith("linux"):
            self.error = "当前平台不支持 inotify，已使用周期扫描"
            return False
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            init = libc.inotify_init1
            init.argtypes = [ctypes.c_int]
            init.restype = ctypes.c_int
            self._add_watch = libc.inotify_add_watch
            self._add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            self._add_watch.restype = ctypes.c_int
            self.fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
            if self.fd < 0:
                raise OSError(ctypes.get_errno(), "inotify_init1")
            self.refresh()
            return True
        except Exception as exc:
            self.error = str(exc)
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1
            return False

    def refresh(self) -> None:
        if self.fd < 0:
            return
        watched = set(self.wd_paths.values())
        for library in self.database.list_libraries():
            root = Path(library["path"])
            if not library["enabled"] or not root.is_dir():
                continue
            for directory, names, _ in os.walk(root):
                names[:] = [name for name in names if not is_ignored(Path(name))]
                path = Path(directory)
                if path not in watched:
                    self._watch_directory(path, int(library["id"]))
                    watched.add(path)

    def _watch_directory(self, path: Path, library_id: int) -> None:
        try:
            wd = self._add_watch(self.fd, os.fsencode(path), self.WATCH_MASK)
            if wd >= 0:
                self.wd_paths[wd] = path
                self.wd_libraries[wd] = library_id
        except OSError:
            pass

    def _read_events(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 256 * 1024)
            except BlockingIOError:
                break
            except OSError as exc:
                self.error = str(exc)
                break
            offset = 0
            while offset + self.EVENT_STRUCT.size <= len(data):
                wd, mask, _, name_length = self.EVENT_STRUCT.unpack_from(data, offset)
                offset += self.EVENT_STRUCT.size
                raw_name = data[offset:offset + name_length].split(b"\0", 1)[0]
                offset += name_length
                library_id = self.wd_libraries.get(wd)
                directory = self.wd_paths.get(wd)
                if library_id is None or directory is None:
                    continue
                name = os.fsdecode(raw_name) if raw_name else ""
                path = directory / name if name else directory
                if is_ignored(path):
                    continue
                if mask & self.IN_IGNORED:
                    self.wd_paths.pop(wd, None)
                    self.wd_libraries.pop(wd, None)
                if mask & self.IN_ISDIR and path.is_dir():
                    self._watch_directory(path, library_id)
                self.pending[library_id] = time.monotonic() + self.settings.watch_debounce_seconds
                self.last_event_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async def _debounce_loop(self) -> None:
        while True:
            now = time.monotonic()
            ready = [library_id for library_id, deadline in self.pending.items() if deadline <= now]
            for library_id in ready:
                self.pending.pop(library_id, None)
                await self._submit_scan(library_id, "watch")
            await asyncio.sleep(1)

    async def _fallback_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.watch_fallback_seconds)
            self.refresh()
            for library in self.database.list_libraries():
                if library["enabled"]:
                    await self._submit_scan(int(library["id"]), "fallback")

    def _signature_interval(self) -> int:
        # hybrid 模式下 inotify 已覆盖实时事件，签名轮询只是兜底，默认拉长到 1800 秒；
        # 纯 polling 模式没有 inotify，仍按 watch_poll_seconds 高频轮询。
        if self.mode == "hybrid":
            return self.settings.watch_signature_seconds
        return self.settings.watch_poll_seconds

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._signature_interval())
            current = await asyncio.to_thread(self._signatures)
            for library_id, signature in current.items():
                previous = self.signatures.get(library_id)
                if previous is not None and previous != signature:
                    await self._submit_scan(library_id, "signature")
            self.signatures = current
            self.last_poll_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _signatures(self) -> dict[int, tuple[int, int, int]]:
        result: dict[int, tuple[int, int, int]] = {}
        for library in self.database.list_libraries():
            root = Path(library["path"])
            if not library["enabled"] or not root.is_dir():
                continue
            library_id = int(library["id"])
            if self.mode == "hybrid":
                # 浅层探测：先只比较顶层目录项（直接增删改文件会刷新所在目录 mtime），
                # 没变化就复用缓存的深签名，避免每轮全库递归 stat。
                # 取舍：更深层子目录里纯内容修改不会刷新顶层 mtime，浅层探测可能漏掉，
                # 这类变化在 hybrid 下由 inotify 实时事件覆盖；顶层 mtime 一旦变化仍走完整递归。
                shallow = self._shallow_probe(root)
                cached = self.shallow.get(library_id)
                if cached is not None and cached[0] == shallow:
                    result[library_id] = cached[1]
                    continue
                deep = self._deep_signature(root)
                self.shallow[library_id] = (shallow, deep)
                result[library_id] = deep
            else:
                # polling 模式没有 inotify 兜底，必须每次全量递归，保证不漏任何深层变化
                result[library_id] = self._deep_signature(root)
        return result

    def _shallow_probe(self, root: Path) -> tuple[tuple[Any, ...], ...]:
        entries: list[tuple[Any, ...]] = []
        try:
            iterator = os.scandir(root)
        except (PermissionError, OSError):
            return ()
        with iterator:
            for entry in iterator:
                path = Path(entry.path)
                if is_ignored(path):
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                except (PermissionError, FileNotFoundError, OSError):
                    continue
                entries.append((entry.name, entry.is_dir(follow_symlinks=False), int(stat.st_mtime_ns), int(stat.st_size)))
        return tuple(sorted(entries))

    def _deep_signature(self, root: Path) -> tuple[int, int, int]:
        count = 0
        total_bytes = 0
        fingerprint = 0
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                iterator = os.scandir(directory)
            except (PermissionError, OSError):
                continue
            with iterator:
                for entry in iterator:
                    path = Path(entry.path)
                    if is_ignored(path):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except (PermissionError, FileNotFoundError, OSError):
                        continue
                    count += 1
                    total_bytes += int(stat.st_size)
                    fingerprint ^= hash((
                        str(path.relative_to(root)),
                        int(stat.st_size),
                        int(stat.st_mtime_ns),
                        int(getattr(stat, "st_ino", 0)),
                    ))
        return (count, total_bytes, fingerprint)

    async def _submit_scan(self, library_id: int, source: str) -> None:
        if self.database.active_task_for_library("scan_only", library_id):
            return
        await self.tasks.submit("scan_only", {"library_id": library_id, "source": source}, priority=4)
        self.last_scan_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.watch_enabled,
            "mode": self.mode,
            "watches": len(self.wd_paths),
            "pending_libraries": len(self.pending),
            "debounce_seconds": self.settings.watch_debounce_seconds,
            "poll_seconds": self.settings.watch_poll_seconds,
            "signature_seconds": self.settings.watch_signature_seconds,
            "fallback_seconds": self.settings.watch_fallback_seconds,
            "last_event_at": self.last_event_at,
            "last_poll_at": self.last_poll_at,
            "last_scan_at": self.last_scan_at,
            "error": self.error,
        }
