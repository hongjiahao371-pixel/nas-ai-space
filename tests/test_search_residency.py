import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

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
