"""Fail-closed verification of the exact ordered ZIP before checkout/delivery."""
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from contextlib import contextmanager

MAX_PACKAGE_BYTES = 50_000_000


def _verify(root, order, version, sink=None):
    """Verify bytes from a bounded regular file, optionally copying those same bytes."""
    try:
        pid = order['product_id']
        number = order['product_version']
        if (type(pid) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,40}', pid)
                or type(number) is not int or number < 1 or not isinstance(version, dict)
                or version.get('product_id') != pid or version.get('version') != number):
            return None
        expected = version.get('package_sha256')
        size = version.get('package_bytes')
        if (type(expected) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected)
                or type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES):
            return None
        path = Path(root).resolve(strict=True) / (pid + '.zip')
        if Path(version['package_path']).resolve(strict=True) != path:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                return None
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    return None
                digest.update(chunk)
                if sink is not None:
                    sink.write(chunk)
                remaining -= len(chunk)
            after = os.fstat(stream.fileno())
            current = path.stat(follow_symlinks=False)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if (stream.read(1) or identity(before) != identity(after)
                    or identity(after) != identity(current) or digest.hexdigest() != expected):
                return None
        return path
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return None


def verified_order_package(root, order, version):
    """Verify the current file for checkout; not a promise of future availability."""
    return _verify(root, order, version)


@contextmanager
def staged_order_package(root, order, version):
    """Yield a private verified copy or None; remove it even on upload failure.

    The caller must upload only this path, never reopen the mutable source.
    The temporary directory is 0700 and the file is 0600. Not a security
    boundary against malicious processes running under the same UID.
    """
    directory = None
    staged = None
    try:
        try:
            pid = order['product_id']
            if type(pid) is str and re.fullmatch(r'[a-z0-9][a-z0-9-]{2,40}', pid):
                directory = tempfile.TemporaryDirectory(prefix='naum-delivery-')
                candidate = Path(directory.name) / (pid + '.zip')
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as sink:
                    os.fchmod(sink.fileno(), 0o600)
                    verified = _verify(root, order, version, sink)
                if verified is not None:
                    staged = candidate
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            staged = None
        yield staged
    finally:
        if directory is not None:
            directory.cleanup()
