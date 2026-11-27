import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_version import staged_order_package


class StageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'ny-kyiv-01.zip'
        with zipfile.ZipFile(self.source, 'w') as z:
            z.writestr('README.txt', 'purchased version')
        self.original = self.source.read_bytes()
        self.order = {'product_id': 'ny-kyiv-01', 'product_version': 1}
        self.version = {'product_id': 'ny-kyiv-01', 'version': 1,
                        'package_path': str(self.source), 'package_bytes': len(self.original),
                        'package_sha256': hashlib.sha256(self.original).hexdigest()}

    def stage(self):
        return staged_order_package(self.root, self.order, self.version)

    def test_source_replaced_after_verification(self):
        with self.stage() as path:
            self.assertIsNotNone(path)
            self.source.unlink()
            self.source.write_bytes(b'replacement')
            self.assertEqual(path.read_bytes(), self.original)
            self.assertEqual(path.name, self.source.name)
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_source_overwritten_in_place_after_verification(self):
        with self.stage() as path:
            self.source.write_bytes(b'wrong')
            self.assertEqual(path.read_bytes(), self.original)

    def test_private_permissions(self):
        with self.stage() as path:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertNotEqual(path.parent, self.root)

    def test_cleanup_on_upload_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'simulated upload failure'):
            with self.stage() as path:
                raise RuntimeError('simulated upload failure')
        self.assertFalse(path.parent.exists())

    def test_bad_hash(self):
        self.version['package_sha256'] = '0' * 64
        with self.stage() as path:
            self.assertIsNone(path)

    def test_wrong_version(self):
        self.order['product_version'] = 2
        with self.stage() as path:
            self.assertIsNone(path)

    def test_source_symlink(self):
        alternate = self.root / 'alternate.zip'
        self.source.rename(alternate)
        self.source.symlink_to(alternate)
        with self.stage() as path:
            self.assertIsNone(path)

    def test_fifo(self):
        self.source.unlink()
        os.mkfifo(self.source)
        with self.stage() as path:
            self.assertIsNone(path)

    def test_missing(self):
        self.source.unlink()
        with self.stage() as path:
            self.assertIsNone(path)


if __name__ == '__main__':
    unittest.main()
