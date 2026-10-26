"""Build a pinned SQLite with the WAL-reset fix (no runtime compiler needed)."""
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

URL = 'https://sqlite.org/2026/sqlite-amalgamation-3510300.zip'
ARCHIVE_SHA256 = 'acb1e6f5d832484bf6d32b681e858c38add8b2acdfd42ac5df24b8afb46552b4'
SOURCE_SHA3_256 = '32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e'


def build(destination):
    data = urllib.request.urlopen(URL, timeout=60).read()
    if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError('SQLite archive checksum mismatch')
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        archive = zipfile.ZipFile(io.BytesIO(data))
        for name in ('sqlite3.c', 'sqlite3.h'):
            content = archive.read('sqlite-amalgamation-3510300/' + name)
            if name == 'sqlite3.c' and hashlib.sha3_256(content).hexdigest() != SOURCE_SHA3_256:
                raise RuntimeError('SQLite source checksum mismatch')
            (root / name).write_bytes(content)
        subprocess.run(['cc', '-O2', '-fPIC', '-shared', '-Wl,-soname,libsqlite3.so.0',
            '-DSQLITE_THREADSAFE=1', '-DSQLITE_ENABLE_FTS5', '-DSQLITE_ENABLE_RTREE',
            '-DSQLITE_ENABLE_COLUMN_METADATA', '-DSQLITE_ENABLE_MATH_FUNCTIONS',
            '-DSQLITE_ENABLE_DBSTAT_VTAB', '-DSQLITE_USE_URI=1',
            str(root / 'sqlite3.c'), '-o', str(destination / 'libsqlite3.so.0'),
            '-lpthread', '-ldl', '-lm'], check=True)


if __name__ == '__main__':
    build(sys.argv[1])
