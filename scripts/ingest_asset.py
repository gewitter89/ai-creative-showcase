#!/usr/bin/env python3
"""Safe asset ingestion pipeline for NAUM AI Creative Studio."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterator
from PIL import Image

ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{2,40}$')
CAT_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,40}$')
DEFAULT_PRIVATE_DIR = '/var/lib/naum-studio/originals'
MAX_PREVIEW_SIDE = 1200
MIN_IMAGE_SIDE = 512
MAX_IMAGE_SIDE = 12000
MAX_INPUT_BYTES = 50 * 1024 * 1024
ORIGINAL_FILE_MODE = 0o600
ALLOWED_FORMATS = {'PNG', 'JPEG', 'WEBP'}
FORMAT_EXTENSIONS = {
    'PNG': '.png',
    'JPEG': '.jpg',
    'WEBP': '.webp',
}
ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}


def sha256_file(path: Path) -> str:
    """Compute hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Write bytes atomically using a temporary file in the target directory and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        if mode is not None:
            os.chmod(path, mode)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: dict) -> None:
    """Atomically write formatted UTF-8 JSON to disk."""
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    atomic_write_bytes(path, encoded)


@contextlib.contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Acquire an exclusive advisory lock for manifest/catalog updates."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+', encoding='utf-8') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _store_versioned_original(
    input_path: Path,
    orig_dir: Path,
    item_id: str,
    ext: str,
    digest: str,
) -> tuple[Path, bool]:
    """Store original under a SHA256-versioned filename with 0600 permissions without overwriting."""
    orig_dir.mkdir(parents=True, exist_ok=True)
    versioned_dest = orig_dir / f'{item_id}_original_{digest[:12]}{ext}'
    if versioned_dest.exists():
        existing_sha = sha256_file(versioned_dest)
        if existing_sha != digest:
            raise RuntimeError(
                f'Existing versioned original {versioned_dest.name} has mismatched SHA-256'
            )
        os.chmod(versioned_dest, ORIGINAL_FILE_MODE)
        return versioned_dest, True

    atomic_write_bytes(versioned_dest, input_path.read_bytes(), mode=ORIGINAL_FILE_MODE)
    return versioned_dest, False


def _save_atomic_webp_preview(work: Image.Image, preview_dest: Path) -> None:
    """Save WebP preview atomically without EXIF metadata."""
    preview_dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f'.{preview_dest.name}.',
        suffix='.tmp',
        dir=str(preview_dest.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        work.save(tmp_path, format='WEBP', quality=84, method=6)
        os.replace(tmp_path, preview_dest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def ingest_asset(
    repo_root: Path,
    item_id: str,
    input_path: Path,
    private_dir: Path = Path(DEFAULT_PRIVATE_DIR),
    min_side: int = MIN_IMAGE_SIDE,
    max_side: int = MAX_IMAGE_SIDE,
    max_bytes: int = MAX_INPUT_BYTES,
) -> dict:
    """Validate, privately archive (0600, SHA256-versioned), build WebP preview, and update JSON atomically."""
    if not ID_RE.fullmatch(item_id):
        raise ValueError(f'Invalid item_id format: {item_id!r}')
    if not input_path.is_file():
        raise FileNotFoundError(f'Input file not found: {input_path}')

    file_size = input_path.stat().st_size
    if file_size <= 0:
        raise ValueError('Input file is empty')
    if file_size > max_bytes:
        raise ValueError(f'Input file size {file_size} exceeds limit {max_bytes} bytes')

    # Validate & inspect image without trusting extension
    with Image.open(input_path) as img:
        img.verify()

    with Image.open(input_path) as img:
        fmt = (img.format or 'UNKNOWN').upper()
        if fmt not in ALLOWED_FORMATS:
            raise ValueError(f'Unsupported image format: {fmt}')
        width, height = img.size
        if width < min_side or height < min_side:
            raise ValueError(f'Image resolution {width}x{height} below minimum {min_side}x{min_side}')
        if width > max_side or height > max_side:
            raise ValueError(f'Image resolution {width}x{height} exceeds maximum {max_side}x{max_side}')

        suffix = input_path.suffix.lower()
        ext = suffix if suffix in ALLOWED_EXTENSIONS else FORMAT_EXTENSIONS[fmt]
        new_sha256 = sha256_file(input_path)

        with file_lock(private_dir / '.ingest.lock'):
            manifest_path = repo_root / 'generation_manifest.json'
            catalog_path = repo_root / 'catalog.json'
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            catalog = json.loads(catalog_path.read_text(encoding='utf-8'))

            m_item = next((x for x in manifest.get('items', []) if x.get('id') == item_id), None)
            c_item = next((x for x in catalog.get('items', []) if x.get('id') == item_id), None)
            if not m_item or not c_item:
                raise ValueError(f'Unknown item_id not present in manifest/catalog: {item_id}')

            category = str(m_item.get('category') or c_item.get('category') or 'misc')
            if not CAT_RE.fullmatch(category):
                raise ValueError(f'Invalid category: {category!r}')

            rel_preview = str(m_item.get('preview_path') or f'assets/{category}/{item_id}/preview.webp')
            if '..' in Path(rel_preview).parts or not rel_preview.startswith('assets/'):
                raise ValueError(f'Unsafe preview_path: {rel_preview}')
            preview_dest = (repo_root / rel_preview).resolve()
            assets_root = (repo_root / 'assets').resolve()
            if not preview_dest.is_relative_to(assets_root):
                raise ValueError('preview_path escapes assets/')

            orig_dir = private_dir / category / item_id
            orig_dest, original_reused = _store_versioned_original(
                input_path=input_path,
                orig_dir=orig_dir,
                item_id=item_id,
                ext=ext,
                digest=new_sha256,
            )

            # Build optimized public WebP preview (strip EXIF, cap dimension)
            work = img.convert('RGBA' if img.mode in ('RGBA', 'LA', 'P') else 'RGB')
            work.thumbnail((MAX_PREVIEW_SIDE, MAX_PREVIEW_SIDE), Image.Resampling.LANCZOS)
            _save_atomic_webp_preview(work, preview_dest)
            pw, ph = work.size

            # Update catalog & manifest -> status = 'проверка качества' (never auto-publish for sale)
            c_item['image'] = rel_preview
            c_item['status'] = 'проверка качества'
            c_item['badge'] = 'Проверка качества'
            m_item['status'] = 'qa_pending'
            m_item['original_sha256'] = new_sha256
            m_item['original_filename'] = orig_dest.name
            m_item['original_dimensions'] = f'{width}x{height}'

            versions = m_item.setdefault('original_versions', [])
            if not any(v.get('sha256') == new_sha256 for v in versions if isinstance(v, dict)):
                versions.append({
                    'sha256': new_sha256,
                    'filename': orig_dest.name,
                    'dimensions': f'{width}x{height}',
                })

            atomic_write_json(catalog_path, catalog)
            atomic_write_json(manifest_path, manifest)

    return {
        'ok': True,
        'item_id': item_id,
        'original_path': str(orig_dest),
        'original_sha256': new_sha256,
        'original_reused': original_reused,
        'original_mode': '0600',
        'original_size': f'{width}x{height}',
        'preview_path': rel_preview,
        'preview_size': f'{pw}x{ph}',
        'preview_bytes': preview_dest.stat().st_size,
        'catalog_status': c_item['status'],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description='Ingest generated image into NAUM AI Creative Studio')
    ap.add_argument('--repo', default='.', help='Path to ai-creative-showcase repo')
    ap.add_argument('--item-id', required=True, help='Catalog item ID (e.g. ny-kyiv-01)')
    ap.add_argument('--input', required=True, help='Path to original image')
    ap.add_argument('--private-dir', default=DEFAULT_PRIVATE_DIR, help='Private directory for originals')
    args = ap.parse_args()
    res = ingest_asset(
        Path(args.repo).resolve(),
        args.item_id.strip(),
        Path(args.input).resolve(),
        Path(args.private_dir),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
