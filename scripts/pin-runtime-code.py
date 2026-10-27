"""Bounded read-only residency for a service's mapped ELF executable files.

Run in the target container's PID namespace. No process memory, arguments,
environment, user data, SQL, model inputs or results are read.
"""
import argparse
import importlib.util
import json
import os
import re
from pathlib import Path
import signal
import stat
import time

common = Path(__file__).with_name('nas-ai-pin-search-pages.py')
if not common.exists():
    common = Path(__file__).with_name('pin-search-pages.py')
spec = importlib.util.spec_from_file_location('search_pages', common)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


class RuntimeKeeper:
    def __init__(self, process, budget, locker=None):
        self.process = Path(process)
        self.budget = budget
        self.locker = locker or base.Locker()
        self.pins = {}
        self.identity = None

    def version(self):
        return (self.process / 'stat').read_text().rsplit(')', 1)[1].split()[19]

    def refresh(self):
        wanted = {}
        errors = 0
        try:
            identity = self.version()
            if self.identity != identity:
                self.close()
                self.identity = identity
            for line in (self.process / 'maps').read_text().splitlines():
                fields = line.split(None, 5)
                if len(fields) != 6 or 'x' not in fields[1] or not fields[5].startswith('/'):
                    continue
                if fields[5].endswith(' (deleted)'):
                    errors += 1
                    continue
                if not re.fullmatch(r'[0-9a-f]+-[0-9a-f]+', fields[0]):
                    errors += 1
                    continue
                # Follow only the kernel-owned mapping link. A merged overlay
                # path can refer to a different backing inode after copy-up.
                path = self.process / 'map_files' / fields[0]
                major, minor = (int(x, 16) for x in fields[3].split(':'))
                expected = (os.makedev(major, minor), int(fields[4]))
                try:
                    fd = os.open(path, os.O_RDONLY)
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_ino != expected[1]:
                            raise OSError('Executable identity changed')
                        if os.pread(fd, 4, 0) != b'\x7fELF':
                            raise OSError('Mapped file is not ELF')
                        length = ((info.st_size + base.PAGE - 1) // base.PAGE) * base.PAGE
                        # The kernel-owned handle identifies the mapped file itself.
                        # Overlay getattr may report a backing device different
                        # from maps; retain both identities when reusing a pin.
                        wanted[(info.st_dev, info.st_ino, length, expected[0])] = path
                    finally:
                        os.close(fd)
                except OSError:
                    errors += 1
            if self.version() != identity:
                raise OSError('Process changed during observation')
        except (OSError, ValueError, IndexError):
            self.close()
            return self.report({}, 1)
        for key in list(self.pins):
            if key not in wanted:
                self.locker.unlock(self.pins.pop(key), key[2])
        used = sum(k[2] for k in self.pins)
        for key, path in wanted.items():
            if key in self.pins or used + key[2] > self.budget:
                continue
            try:
                fd = os.open(path, os.O_RDONLY)
                try:
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) != key[:2] or self.version() != identity:
                        raise OSError('Process or executable changed')
                    self.pins[key] = self.locker.lock(fd, 0, key[2])
                    used += key[2]
                finally:
                    os.close(fd)
            except OSError:
                errors += 1
        try:
            if self.version() != identity:
                raise OSError('Process changed after locking')
        except (OSError, ValueError, IndexError):
            self.close()
            return self.report({}, 1)
        return self.report(wanted, errors)

    def report(self, wanted, errors):
        return {'checked_at': time.time(), 'ready': bool(wanted) and not errors and set(wanted) == set(self.pins),
                'locked_bytes': sum(k[2] for k in self.pins), 'required_bytes': sum(k[2] for k in wanted),
                'budget_bytes': self.budget, 'files': len(wanted), 'errors': errors,
                'process_start_ticks': self.identity}

    def close(self):
        for key, address in self.pins.items():
            self.locker.unlock(address, key[2])
        self.pins.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, default=1)
    parser.add_argument('--budget-mb', type=int, default=128)
    args = parser.parse_args()
    if args.pid < 1 or args.budget_mb < 1:
        parser.error('PID and budget must be positive')
    keeper = RuntimeKeeper(Path('/proc') / str(args.pid), args.budget_mb * 1024 * 1024)
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped:
            result = keeper.refresh()
            p = Path('/tmp/runtime-residency.json.new')
            p.write_text(json.dumps(result))
            p.replace('/tmp/runtime-residency.json')
            for _ in range(20):
                if stopped:
                    break
                time.sleep(.5)
    finally:
        keeper.close()


if __name__ == '__main__':
    main()
