import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import sqlite3

spec = importlib.util.spec_from_file_location('residency', Path(__file__).resolve().parents[1] / 'scripts/pin-search-pages.py')
residency = importlib.util.module_from_spec(spec); spec.loader.exec_module(residency)


class FakeLocker:
    def __init__(self): self.active = {}; self.serial = 0
    def lock(self, fd, offset, length):
        self.serial += 1; self.active[self.serial] = length; return self.serial
    def unlock(self, address, length):
        assert self.active.pop(address) == length


class ResidencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.segment = self.root / 'collections/test/0/segments/segment'; self.segment.mkdir(parents=True)
        self.path = self.segment / 'vectors.dat'; self.path.write_bytes(b'a' * (2 * residency.PAGE))
        self.locker = FakeLocker(); self.keeper = residency.Keeper(self.root, 8 * residency.PAGE, self.locker)
    def tearDown(self):
        self.keeper.close(); self.assertFalse(self.locker.active); self.temp.cleanup()
    def test_refresh_reuses_pages_and_keeps_source_bytes(self):
        before = self.path.read_bytes(); first = self.keeper.refresh()
        self.assertTrue(first['ready']); self.assertEqual(first['locked_bytes'], 2 * residency.PAGE)
        serial = self.locker.serial
        self.assertTrue(self.keeper.refresh()['ready']); self.assertEqual(serial, self.locker.serial)
        self.assertEqual(self.path.read_bytes(), before)
    def test_replacement_and_deletion_release_old_pages(self):
        self.keeper.refresh(); previous = set(self.locker.active)
        replacement = self.root / 'new'; replacement.write_bytes(b'b' * residency.PAGE); replacement.replace(self.path)
        self.assertTrue(self.keeper.refresh()['ready'])
        self.assertFalse(previous & set(self.locker.active))
        self.path.unlink(); self.assertFalse(self.keeper.refresh()['ready']); self.assertFalse(self.locker.active)
    def test_budget_cannot_expand_or_claim_ready(self):
        self.keeper.budget = residency.PAGE
        result = self.keeper.refresh()
        self.assertFalse(result['ready']); self.assertLessEqual(result['locked_bytes'], residency.PAGE)
    def test_growth_is_rediscovered_without_writing_source(self):
        self.keeper.refresh()
        with self.path.open('ab') as f: f.write(b'b' * residency.PAGE)
        result = self.keeper.refresh()
        self.assertTrue(result['ready']); self.assertEqual(result['locked_bytes'], 3 * residency.PAGE)
    def test_symlinks_and_files_outside_segments_are_ignored(self):
        (self.segment / 'link').symlink_to('/etc/passwd')
        (self.root / 'credentials').write_bytes(b'x' * 10000)
        result = self.keeper.refresh()
        self.assertTrue(result['ready']); self.assertEqual(result['locked_bytes'], 2 * residency.PAGE)
    def test_lock_failure_is_visible_and_does_not_claim_readiness(self):
        self.locker.lock = lambda *_: (_ for _ in ()).throw(OSError('limit'))
        result = self.keeper.refresh()
        self.assertFalse(result['ready']); self.assertGreater(result['errors'], 0)
        self.assertEqual(result['locked_bytes'], 0)

    def test_explicit_database_and_wal_exclude_neighbor_files(self):
        db = self.root / 'nas-ai-space.db'
        db.write_bytes(b'd' * residency.PAGE)
        wal = self.root / 'nas-ai-space.db-wal'
        wal.write_bytes(b'w' * residency.PAGE)
        (self.root / 'config.json').write_bytes(b'private' * residency.PAGE)
        self.keeper.sqlite_files = (db,)
        result = self.keeper.refresh()
        self.assertTrue(result['ready'])
        self.assertEqual(result['locked_bytes'], 4 * residency.PAGE)
        self.assertEqual(db.read_bytes(), b'd' * residency.PAGE)
        self.assertEqual(wal.read_bytes(), b'w' * residency.PAGE)

    def test_missing_or_symlink_database_cannot_report_ready(self):
        db = self.root / 'missing.db'
        self.keeper.sqlite_files = (db,)
        self.assertFalse(self.keeper.refresh()['ready'])
        db.symlink_to(self.path)
        self.assertFalse(self.keeper.refresh()['ready'])

    def test_sqlite_only_does_not_require_vector_segments(self):
        db = self.root / 'events.sqlite3'
        db.write_bytes(b'd' * residency.PAGE)
        self.keeper.root = self.root / 'absent-vectors'
        self.keeper.sqlite_files = (db,)
        self.assertFalse(self.keeper.refresh()['ready'])
        self.keeper.require_vectors = False
        self.keeper.root = self.root  # sqlite-only excludes even existing vector files.
        result = self.keeper.refresh()
        self.assertTrue(result['ready'])
        self.assertEqual(result['locked_bytes'], residency.PAGE)
        db.unlink()
        self.assertFalse(self.keeper.refresh()['ready'])
        self.assertFalse(self.locker.active)

    def test_wal_commits_truncation_and_replacement_remain_visible(self):
        db = self.root / 'events.sqlite3'
        self.keeper.sqlite_files = (db,)
        self.keeper.budget = 128 * residency.PAGE
        c = sqlite3.connect(db)
        try:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA synchronous=FULL')
            c.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)')
            c.execute("INSERT INTO records VALUES(1,'before')"); c.commit()
            self.assertTrue(self.keeper.refresh()['ready'])
            c.execute("UPDATE records SET value='committed'"); c.commit()
            with sqlite3.connect(db) as reader:
                self.assertEqual(reader.execute('SELECT value FROM records').fetchone()[0], 'committed')
            self.assertEqual(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
            self.assertTrue(self.keeper.refresh()['ready'])
            c.execute("UPDATE records SET value='after-checkpoint'"); c.commit()
            self.assertTrue(self.keeper.refresh()['ready'])
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        finally:
            c.close()
        self.assertTrue(self.keeper.refresh()['ready'])

    def test_wal_same_inode_and_size_reuse_does_not_reuse_old_pin(self):
        db = self.root / 'events.sqlite3'; db.write_bytes(b'd' * residency.PAGE)
        wal = db.with_name(db.name + '-wal'); wal.write_bytes(b'a' * residency.PAGE)
        self.keeper.sqlite_files = (db,)
        self.assertTrue(self.keeper.refresh()['ready'])
        old_stat = wal.stat(); old_pins = set(self.locker.active)
        with wal.open('wb') as f: f.write(b'b' * residency.PAGE)
        os.utime(wal, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))
        self.assertEqual(wal.stat().st_ino, old_stat.st_ino)
        self.assertEqual(wal.stat().st_size, old_stat.st_size)
        self.assertTrue(self.keeper.refresh()['ready'])
        self.assertEqual(len(old_pins - set(self.locker.active)), 1)
        self.assertEqual(wal.read_bytes(), b'b' * residency.PAGE)
