"""Fail-closed verification of the exact ordered ZIP before checkout/delivery."""
import hashlib
import os
from pathlib import Path
import re
import stat

MAX_PACKAGE_BYTES = 50_000_000


def verified_order_package(root, order, version):
    """Return a verified path, not an immutable file handle. No DB or network I/O."""
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
