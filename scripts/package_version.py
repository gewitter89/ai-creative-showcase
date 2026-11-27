"""Fail-closed verification of the exact ordered ZIP before checkout/delivery."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import zipfile
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
        base = Path(root).resolve(strict=True)
        stored = Path(version['package_path'])
        legacy = base / (pid + '.zip')
        archived = base / '.versions' / pid / (expected + '.zip')
        if stored.absolute() not in (legacy, archived):
            return None
        path = stored.absolute()
        if path == archived and not (_private_dir(base / '.versions') and _private_dir(archived.parent)):
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


def _private_dir(path):
    """The retention tree is private and owned by this worker."""
    try:
        meta = path.lstat()
        return (stat.S_ISDIR(meta.st_mode) and meta.st_uid == os.geteuid()
                and stat.S_IMODE(meta.st_mode) == 0o700)
    except OSError:
        return False


def retain_catalog_package(root, product_id):
    """Retain a bounded ZIP as a create-only content-addressed version.

    The root must be a dedicated private package directory. Never delete or
    replace older versions. A failed capture means not ready for new orders.
    Existing versions survive catalog refresh and source replacement.
    """
    temp_path = None
    try:
        if type(product_id) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,40}', product_id):
            return None
        root = Path(root).resolve(strict=True)
        source = root / (product_id + '.zip')
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_PACKAGE_BYTES
                    or shutil.disk_usage(root).free < before.st_size + 256 * 1024 * 1024):
                return None
            versions = root / '.versions'
            directory = versions / product_id
            for component in (versions, directory):
                component.mkdir(mode=0o700, exist_ok=True)
                if not _private_dir(component):
                    return None
            fd, name = tempfile.mkstemp(prefix='.capture-', dir=directory)
            temp_path = Path(name)
            digest = hashlib.sha256()
            remaining = before.st_size
            with os.fdopen(fd, 'wb') as sink:
                os.fchmod(sink.fileno(), 0o600)
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        return None
                    sink.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                after = os.fstat(stream.fileno())
                current = source.stat(follow_symlinks=False)
                identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                if stream.read(1) or identity(before) != identity(after) or identity(after) != identity(current):
                    return None
                sink.flush()
                os.fsync(sink.fileno())
        if not zipfile.is_zipfile(temp_path):
            return None
        sha = digest.hexdigest()
        dest = directory / (sha + '.zip')
        try:
            os.link(temp_path, dest, follow_symlinks=False)
        except FileExistsError:
            pass
        version = {'product_id': product_id, 'version': 1, 'package_path': str(dest),
                   'package_sha256': sha, 'package_bytes': before.st_size}
        if verified_order_package(root, {'product_id': product_id, 'product_version': 1}, version) is None:
            return None
        if stat.S_IMODE(dest.lstat().st_mode) != 0o600:
            return None
        # Persist directory entries before allowing the database to reference them.
        for component in (directory, versions, root):
            dfd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        return version
    except (OSError, ValueError, TypeError, RuntimeError):
        return None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
