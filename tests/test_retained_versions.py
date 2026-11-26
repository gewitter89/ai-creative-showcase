import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import package_version as pv


class RetentionTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.pid = 'ny-kyiv-01'
        self.source = self.root / (self.pid + '.zip')
        self.write('first edition')

    def write(self, text):
        with zipfile.ZipFile(self.source, 'w') as z:
            z.writestr('README.txt', text)

    def retain(self):
        return pv.retain_catalog_package(self.root, self.pid)

    def test_old_version_survives_update_and_source_removal(self):
        first = self.retain()
        original = self.source.read_bytes()
        self.write('second edition')
        second = self.retain()
        self.assertNotEqual(first['package_sha256'], second['package_sha256'])
        self.source.unlink()
        with pv.staged_order_package(self.root, {'product_id': self.pid, 'product_version': 1}, first) as p:
            self.assertEqual(p.read_bytes(), original)
        self.assertTrue(Path(second['package_path']).exists())

    def test_duplicate_retention_preserves_inode(self):
        v = self.retain()
        p = Path(v['package_path'])
        inode = p.stat().st_ino
        self.assertEqual(self.retain(), v)
        self.assertEqual(p.stat().st_ino, inode)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)

    def test_corrupt_existing_never_overwritten(self):
        v = self.retain()
        p = Path(v['package_path'])
        p.write_bytes(b'corrupt')
        self.assertIsNone(self.retain())
        self.assertEqual(p.read_bytes(), b'corrupt')

    def test_reject_symlink_version_tree(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        (self.root / '.versions').symlink_to(t.name)
        self.assertIsNone(self.retain())
        self.assertEqual(list(Path(t.name).iterdir()), [])

    def test_reject_nonprivate_version_tree(self):
        (self.root / '.versions').mkdir(mode=0o755)
        self.assertIsNone(self.retain())

    def test_reject_bad_zip(self):
        self.source.write_bytes(b'bad')
        self.assertIsNone(self.retain())
        self.assertEqual(list((self.root / '.versions' / self.pid).iterdir()), [])

    def test_reject_source_symlink(self):
        alternate = self.root / 'other.zip'
        self.source.rename(alternate)
        self.source.symlink_to(alternate)
        self.assertIsNone(self.retain())

    def test_reject_source_fifo(self):
        self.source.unlink()
        os.mkfifo(self.source)
        self.assertIsNone(self.retain())

    def test_reject_wrong_archive_path(self):
        v = self.retain()
        v['package_path'] = str(self.root / 'other.zip')
        self.assertIsNone(pv.verified_order_package(self.root, {'product_id': self.pid, 'product_version': 1}, v))

    def test_low_disk_blocks_new_copy(self):
        with patch.object(pv.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 1024})()):
            self.assertIsNone(self.retain())
        self.assertFalse((self.root / '.versions').exists())


if __name__ == '__main__':
    unittest.main()
