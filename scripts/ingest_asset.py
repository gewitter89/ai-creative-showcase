#!/usr/bin/env python3
"""Safe asset ingestion pipeline for NAUM AI Creative Studio."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from PIL import Image

ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{2,40}$')
DEFAULT_PRIVATE_DIR = '/var/lib/naum-studio/originals'
MAX_PREVIEW_SIDE = 1200
MIN_IMAGE_SIDE = 512


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def ingest_asset(
    repo_root: Path,
    item_id: str,
    input_path: Path,
    private_dir: Path = Path(DEFAULT_PRIVATE_DIR),
    min_side: int = MIN_IMAGE_SIDE,
) -> dict:
    if not ID_RE.fullmatch(item_id):
        raise ValueError(f'Invalid item_id format: {item_id!r}')
    if not input_path.is_file():
        raise FileNotFoundError(f'Input file not found: {input_path}')

    manifest_path = repo_root / 'generation_manifest.json'
    catalog_path = repo_root / 'catalog.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    catalog = json.loads(catalog_path.read_text(encoding='utf-8'))

    m_item = next((x for x in manifest.get('items', []) if x.get('id') == item_id), None)
    c_item = next((x for x in catalog.get('items', []) if x.get('id') == item_id), None)
    if not m_item or not c_item:
        raise ValueError(f'Unknown item_id not present in manifest/catalog: {item_id}')

    # Validate & inspect image without trusting extension
    with Image.open(input_path) as img:
        img.verify()
    with Image.open(input_path) as img:
        width, height = img.size
        fmt = (img.format or 'UNKNOWN').upper()
        if width < min_side or height < min_side:
            raise ValueError(f'Image resolution {width}x{height} below minimum {min_side}x{min_side}')

        # Store original privately outside public repo
        category = str(m_item.get('category') or c_item.get('category') or 'misc')
        if not ID_RE.fullmatch(category):
            raise ValueError(f'Invalid category: {category!r}')
        orig_dir = private_dir / category / item_id
        orig_dir.mkdir(parents=True, exist_ok=True)
        ext = input_path.suffix.lower() if input_path.suffix else f'.{fmt.lower()}'
        orig_dest = orig_dir / f'{item_id}_original{ext}'
        shutil.copy2(input_path, orig_dest)
        os.chmod(orig_dest, 0o640)

        # Build optimized public WebP preview (strip EXIF, cap dimension)
        rel_preview = str(m_item.get('preview_path') or f'assets/{category}/{item_id}/preview.webp')
        if '..' in Path(rel_preview).parts or not rel_preview.startswith('assets/'):
            raise ValueError(f'Unsafe preview_path: {rel_preview}')
        preview_dest = (repo_root / rel_preview).resolve()
        if not str(preview_dest).startswith(str((repo_root / 'assets').resolve())):
            raise ValueError('preview_path escapes assets/')
        preview_dest.parent.mkdir(parents=True, exist_ok=True)

        work = img.convert('RGBA' if img.mode in ('RGBA', 'LA', 'P') else 'RGB')
        work.thumbnail((MAX_PREVIEW_SIDE, MAX_PREVIEW_SIDE), Image.Resampling.LANCZOS)
        work.save(preview_dest, format='WEBP', quality=84, method=6)
        pw, ph = work.size

    # Update catalog & manifest -> status = 'проверка качества' (never auto-publish for sale)
    c_item['image'] = rel_preview
    c_item['status'] = 'проверка качества'
    c_item['badge'] = 'Проверка качества'
    m_item['status'] = 'qa_pending'
    m_item['original_sha256'] = sha256_file(orig_dest)
    m_item['original_dimensions'] = f'{width}x{height}'

    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    return {
        'ok': True,
        'item_id': item_id,
        'original_path': str(orig_dest),
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
    res = ingest_asset(Path(args.repo).resolve(), args.item_id.strip(), Path(args.input).resolve(), Path(args.private_dir))
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
