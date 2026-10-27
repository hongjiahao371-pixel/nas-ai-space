import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

p = Path(__file__).resolve().parents[1] / 'scripts/pin-runtime-code.py'
spec = importlib.util.spec_from_file_location('runtime_code', p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class FakeLocker:
    def __init__(self): self.pins = {}; self.counter = 0
    def lock(self, fd, offset, length):
        self.counter += 1; self.pins[self.counter] = length; return self.counter
    def unlock(self, address, length): self.pins.pop(address)

class RuntimeCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.p = Path(self.tmp.name)
        (self.p / 'root').mkdir(); self.f = self.p / 'root/code'
        self.f.write_bytes(b'\x7fELF' + b'x' * 4092)
        (self.p / 'map_files').mkdir()
        (self.p / 'map_files/1000-2000').symlink_to(self.f)
        self.identity('10'); self.mapping()
        self.lock = FakeLocker(); self.k = m.RuntimeKeeper(self.p, 2 * m.base.PAGE, self.lock)
    def tearDown(self): self.k.close(); self.assertFalse(self.lock.pins); self.tmp.cleanup()
    def identity(self, value): (self.p / 'stat').write_text('1 (name) ' + ' '.join(['S'] + ['0'] * 18 + [value]))
    def mapping(self, suffix='', mode='r-xp', inode=None):
        s = self.f.stat(); (self.p / 'maps').write_text(f'1000-2000 {mode} 0 {os.major(s.st_dev):x}:{os.minor(s.st_dev):x} {inode or s.st_ino} /code{suffix}\n')
    def test_read_only_reuse_and_ignore_data_mappings(self):
        before = self.f.read_bytes(); self.assertTrue(self.k.refresh()['ready'])
        with (self.p / 'maps').open('a') as f: f.write('3000-4000 rw-p 0 00:00 123 /private.db\n')
        self.assertTrue(self.k.refresh()['ready']); self.assertEqual(self.lock.counter, 1)
        self.assertEqual(self.f.read_bytes(), before)
    def test_identity_change_releases_previous_process_pages(self):
        self.k.refresh(); old = set(self.lock.pins); self.identity('11')
        self.assertTrue(self.k.refresh()['ready']); self.assertFalse(old & set(self.lock.pins))
    def test_budget_failure_cannot_report_ready(self):
        self.k.budget = 1; self.assertFalse(self.k.refresh()['ready']); self.assertFalse(self.lock.pins)
    def test_stale_inode_and_deleted_mapping_are_rejected(self):
        self.k.refresh(); self.mapping(inode=1); self.assertFalse(self.k.refresh()['ready']); self.assertFalse(self.lock.pins)
        self.mapping(suffix=' (deleted)'); self.assertFalse(self.k.refresh()['ready'])
    def test_non_elf_and_missing_process_do_not_claim_ready(self):
        self.f.write_bytes(b'private data'); self.assertFalse(self.k.refresh()['ready'])
        (self.p / 'stat').unlink(); self.assertFalse(self.k.refresh()['ready'])
    def test_lock_failure_is_visible(self):
        self.lock.lock = lambda *_: (_ for _ in ()).throw(OSError('budget'))
        self.assertFalse(self.k.refresh()['ready']); self.assertFalse(self.lock.pins)

    def test_mapping_handle_wins_over_replaced_merged_path(self):
        old = self.p / 'mapped-code'
        self.f.rename(old)
        self.f.write_bytes(b'not the running executable')
        link = self.p / 'map_files/1000-2000'
        link.unlink(); link.symlink_to(old)
        self.assertTrue(self.k.refresh()['ready'])
        link.unlink(); link.symlink_to(self.f)
        self.assertFalse(self.k.refresh()['ready'])

    def test_unavailable_mapping_handle_does_not_fall_back_to_merged_file(self):
        (self.p / 'map_files/1000-2000').unlink()
        self.assertFalse(self.k.refresh()['ready'])
        self.assertFalse(self.lock.pins)

    def test_kernel_mapping_handle_can_report_backing_device(self):
        fields = (self.p / 'maps').read_text().split()
        fields[3] = '00:ffff'
        (self.p / 'maps').write_text(' '.join(fields) + '\n')
        self.assertTrue(self.k.refresh()['ready'])
