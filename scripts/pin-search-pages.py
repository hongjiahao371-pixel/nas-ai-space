"""Keep bounded, read-only search pages resident; never modify index contents.

Linux only. Sparse unwritten capacity is excluded using SEEK_DATA/SEEK_HOLE.
The storage volume must be mounted read-only; locking is an optional cache,
not a substitute for the persistent Qdrant volume or its recovery snapshots.
"""
import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import stat
import time

PAGE = os.sysconf('SC_PAGE_SIZE')


def extents(fd, size):
    position = 0
    spans = []
    while position < size:
        try:
            start = os.lseek(fd, position, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                break
            # Filesystems without sparse extent reporting are safe to handle
            # as fully allocated: the same hard byte budget still applies.
            if exc.errno in (errno.EINVAL, errno.ENOTSUP):
                return [(0, ((size + PAGE - 1) // PAGE) * PAGE)] if size else []
            raise
        end = min(size, os.lseek(fd, start, os.SEEK_HOLE))
        if end <= position:
            raise OSError('Non-advancing sparse extent')
        first, last = start // PAGE * PAGE, (end + PAGE - 1) // PAGE * PAGE
        if spans and first <= spans[-1][0] + spans[-1][1]:
            previous, length = spans.pop(); spans.append((previous, max(last, previous + length) - previous))
        else:
            spans.append((first, last - first))
        position = end
    return spans


class Locker:
    def __init__(self):
        self.lib = ctypes.CDLL(None, use_errno=True)
        self.lib.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_longlong]
        self.lib.mmap.restype = ctypes.c_void_p
        for name in ('mlock', 'munlock', 'munmap'):
            getattr(self.lib, name).argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            getattr(self.lib, name).restype = ctypes.c_int

    def lock(self, fd, offset, length):
        address = self.lib.mmap(None, length, 1, 1, fd, offset)  # PROT_READ, MAP_SHARED
        if address == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_errno(), 'Read-only mmap failed')
        if self.lib.mlock(address, length):
            code = ctypes.get_errno(); self.lib.munmap(address, length)
            raise OSError(code, 'Page residency budget unavailable')
        return address

    def unlock(self, address, length):
        self.lib.munlock(address, length)
        self.lib.munmap(address, length)


class Keeper:
    def __init__(self, root, budget, locker=None, sqlite_files=(), require_vectors=True):
        self.root = Path(root).resolve()
        self.budget = budget
        self.locker = locker or Locker()
        self.pins = {}
        self.sqlite_files = tuple(Path(p).absolute() for p in sqlite_files)
        self.require_vectors = require_vectors
        self.sqlite_versions = {}

    def refresh(self):
        wanted = {}
        candidates = []
        vector_files = 0
        segments = self.root.glob('collections/*/[0-9]*/segments/*') if self.require_vectors else ()
        for segment in segments:
            if segment.is_symlink() or not segment.is_dir():
                continue
            for path in segment.rglob('*'):
                if path.is_symlink() or not path.is_file():
                    continue
                if not path.resolve().is_relative_to(self.root):
                    continue
                candidates.append(path)
                vector_files += 1
        missing_databases = 0
        sqlite_candidates = set()
        for main in self.sqlite_files:
            if main.is_symlink() or not main.is_file():
                missing_databases += 1
                continue
            candidates.append(main)
            sqlite_candidates.add(main)
            # Pin only explicitly configured databases and their current WAL.
            # No snapshots, credentials/config files, media or other neighbors.
            wal = main.with_name(main.name + '-wal')
            if wal.is_file() and not wal.is_symlink():
                candidates.append(wal)
                sqlite_candidates.add(wal)
        versions = {}
        for path in candidates:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if path in sqlite_candidates:
                        versions[(info.st_dev, info.st_ino)] = (info.st_mtime_ns, info.st_ctime_ns)
                    for offset, length in extents(fd, info.st_size):
                        wanted[(info.st_dev, info.st_ino, offset, length)] = path
                finally:
                    os.close(fd)
            except FileNotFoundError:
                if path in self.sqlite_files:
                    missing_databases += 1
                else:
                    continue  # Optimizer replacement; discover next interval.
        # Release deleted/replaced/resized extents before adding new ones.
        for key in list(self.pins):
            # A WAL can be truncated and regrown to the same size between
            # refreshes. Its old mapping then does not prove new pages resident.
            if key not in wanted or (key[:2] in versions and
                    self.sqlite_versions.get(key[:2]) != versions[key[:2]]):
                self.locker.unlock(self.pins.pop(key), key[3])
        self.sqlite_versions = versions
        used = sum(key[3] for key in self.pins)
        required = sum(key[3] for key in wanted)
        errors = 0
        for key, path in wanted.items():
            if key in self.pins or used + key[3] > self.budget:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) != key[:2] or info.st_size <= key[2]:
                        continue
                    self.pins[key] = self.locker.lock(fd, key[2], key[3]); used += key[3]
                finally:
                    os.close(fd)
            except OSError:
                errors += 1
        ready = (bool(wanted) and len(self.pins) == len(wanted) and not missing_databases
                 and (not self.require_vectors or bool(vector_files)))
        return {'checked_at': time.time(), 'ready': ready,
                'locked_bytes': used, 'required_bytes': required, 'budget_bytes': self.budget,
                'locked_extents': len(self.pins), 'required_extents': len(wanted), 'errors': errors,
                'sqlite_databases': len(self.sqlite_files), 'missing_databases': missing_databases}

    def close(self):
        for key, address in self.pins.items():
            self.locker.unlock(address, key[3])
        self.pins.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/qdrant/storage')
    parser.add_argument('--budget-mb', type=int, default=384)
    parser.add_argument('--status', default='/tmp/residency.json')
    parser.add_argument('--sqlite', action='append', default=[], help='Explicit SQLite file; include its current WAL read-only')
    parser.add_argument('--sqlite-only', action='store_true', help='Require the configured SQLite files without a vector collection')
    args = parser.parse_args()
    if args.budget_mb < 1:
        parser.error('Budget must be positive')
    if args.sqlite_only and not args.sqlite:
        parser.error('--sqlite-only requires at least one --sqlite file')
    keeper = Keeper(args.root, args.budget_mb * 1024 * 1024,
                    sqlite_files=args.sqlite, require_vectors=not args.sqlite_only)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    previous = None
    try:
        while not stopping:
            result = keeper.refresh()
            temporary = Path(args.status + '.new')
            temporary.write_text(json.dumps(result)); temporary.replace(args.status)
            state = {k: v for k, v in result.items() if k != 'checked_at'}
            if state != previous:
                print(json.dumps(state), flush=True); previous = state
            for _ in range(20):
                if stopping:
                    break
                time.sleep(.5)
    finally:
        keeper.close()


if __name__ == '__main__':
    main()
