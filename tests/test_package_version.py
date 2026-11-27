import hashlib
from pathlib import Path
import tempfile
import sys
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_version import verified_order_package, MAX_PACKAGE_BYTES


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'ny-kyiv-01.zip'
        with zipfile.ZipFile(self.path, 'w') as z:
            z.writestr('README.txt', 'version 1')
        data = self.path.read_bytes()
        self.order = {'product_id': 'ny-kyiv-01', 'product_version': 1}
        self.version = {'product_id': 'ny-kyiv-01', 'version': 1, 'package_path': str(self.path),
                        'package_sha256': hashlib.sha256(data).hexdigest(), 'package_bytes': len(data)}

    def check(self, version=None, order=None):
        return verified_order_package(self.root, self.order if order is None else order,
                                      self.version if version is None else version)

    def test_correct(self):
        self.assertEqual(self.check(), self.path)

    def test_replaced_same_size(self):
        old = self.path.read_bytes()
        self.path.write_bytes(old[:-1] + bytes([old[-1] ^ 1]))
        self.assertIsNone(self.check())

    def test_new_version(self):
        self.assertIsNone(self.check({**self.version, 'version': 2}))

    def test_other_product(self):
        self.assertIsNone(self.check({**self.version, 'product_id': 'ny-cozy-02'}))

    def test_bad_id(self):
        self.assertIsNone(self.check(order={**self.order, 'product_id': '../outside'}))

    def test_missing(self):
        self.path.unlink()
        self.assertIsNone(self.check())

    def test_symlink(self):
        alternate = self.root / 'copy.zip'
        self.path.rename(alternate)
        self.path.symlink_to(alternate)
        self.assertIsNone(self.check())

    def test_oversize_or_wrong_digest(self):
        self.assertIsNone(self.check({**self.version, 'package_bytes': MAX_PACKAGE_BYTES + 1}))
        self.assertIsNone(self.check({**self.version, 'package_sha256': '0' * 64}))

    def test_fifo(self):
        import os
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertIsNone(self.check())

    def test_no_version(self):
        self.assertIsNone(verified_order_package(self.root, self.order, None))


if __name__ == '__main__':
    unittest.main()
