#!/usr/bin/env python3
"""Build commercial ZIP delivery packages from private originals for NAUM AI Creative Studio."""
from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from ingest_asset import CAT_RE, ID_RE, DEFAULT_PRIVATE_DIR, sha256_file

DEFAULT_PACKAGES_DIR = '/var/lib/naum-studio/packages'
P0_POSTCARD_IDS = ('ny-kyiv-01', 'ny-cozy-02', 'ny-minimal-03')
PRINT_SIZE_PX = (1772, 1181)  # 150x100 mm @ 300 dpi
PRINT_DPI = (300, 300)
SOCIAL_SIZE_PX = (1200, 800)  # 3:2 web / social media
PACKAGE_FILE_MODE = 0o640

FONT_CANDIDATES = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf',
    '/usr/share/fonts/truetype/montserrat/Montserrat-SemiBold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
)

POSTCARD_GREETINGS = {
    'ny-kyiv-01': {
        'text': 'З Новим 2027 роком!',
        'align': 'top_center',
        'fill': (250, 234, 186, 255),
        'shadow': (8, 16, 36, 200),
    },
    'ny-cozy-02': {
        'text': 'З Різдвом Христовим!',
        'align': 'top_left',
        'fill': (252, 242, 214, 255),
        'shadow': (24, 14, 10, 210),
    },
    'ny-minimal-03': {
        'text': '2027\nЗ Новим роком!',
        'align': 'center',
        'fill': (236, 206, 138, 255),
        'shadow': (8, 24, 16, 190),
    },
}


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def find_original_file(
    originals_dir: Path,
    category: str,
    item_id: str,
    manifest_item: dict,
    verify_hash: bool = True,
) -> Path:
    """Locate the private original file for item_id and verify its SHA-256 against manifest."""
    if not ID_RE.fullmatch(item_id):
        raise ValueError(f'Invalid item_id: {item_id!r}')
    if not CAT_RE.fullmatch(category):
        raise ValueError(f'Invalid category: {category!r}')

    item_dir = originals_dir / category / item_id
    if not item_dir.is_dir():
        raise FileNotFoundError(f'Private original directory not found: {item_dir}')

    expected_sha = (manifest_item.get('original_sha256') or '').strip()
    candidates: list[Path] = []

    orig_filename = (manifest_item.get('original_filename') or '').strip()
    if orig_filename:
        candidates.append(item_dir / orig_filename)

    if expected_sha:
        candidates.extend(sorted(item_dir.glob(f'{item_id}_original_{expected_sha[:12]}.*')))

    candidates.extend(sorted(item_dir.glob(f'{item_id}_original.*')))
    candidates.extend(sorted(item_dir.glob(f'{item_id}_original_*.*')))

    resolved: Path | None = None
    for path in candidates:
        if path.is_file():
            resolved = path
            break

    if resolved is None:
        raise FileNotFoundError(f'Original image not found for {item_id} in {item_dir}')

    if verify_hash and expected_sha:
        actual_sha = sha256_file(resolved)
        if actual_sha != expected_sha:
            raise ValueError(
                f'SHA-256 mismatch for {resolved}: expected {expected_sha}, got {actual_sha}'
            )

    return resolved


def _render_greeting_card(base_rgb: Image.Image, item_id: str) -> bytes:
    """Render clean Ukrainian greeting typography onto a 1772x1181 PNG card."""
    spec = POSTCARD_GREETINGS.get(
        item_id,
        {
            'text': 'З Новим роком!',
            'align': 'top_center',
            'fill': (250, 240, 210, 255),
            'shadow': (10, 15, 25, 200),
        },
    )
    canvas = ImageOps.fit(base_rgb, PRINT_SIZE_PX, method=Image.Resampling.LANCZOS).convert('RGBA')
    w, h = canvas.size
    font = _load_font(86 if '\n' not in spec['text'] else 78)

    measure = ImageDraw.Draw(canvas)
    bbox = measure.multiline_textbbox((0, 0), spec['text'], font=font, spacing=18, align='center')
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    align = spec['align']
    if align == 'top_left':
        x = int(w * 0.06)
        y = int(h * 0.08)
        text_align = 'left'
    elif align == 'center':
        x = (w - tw) // 2
        y = (h - th) // 2
        text_align = 'center'
    else:  # top_center
        x = (w - tw) // 2
        y = int(h * 0.075)
        text_align = 'center'

    shadow_layer = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow_layer)
    s_draw.multiline_text(
        (x + 3, y + 4),
        spec['text'],
        font=font,
        fill=spec['shadow'],
        spacing=18,
        align=text_align,
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=6))
    canvas = Image.alpha_composite(canvas, shadow_layer)

    fg_layer = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    f_draw = ImageDraw.Draw(fg_layer)
    f_draw.multiline_text(
        (x, y),
        spec['text'],
        font=font,
        fill=spec['fill'],
        spacing=18,
        align=text_align,
    )
    out_img = Image.alpha_composite(canvas, fg_layer)

    buf = io.BytesIO()
    out_img.save(buf, format='PNG', optimize=True, dpi=PRINT_DPI)
    return buf.getvalue()


def _encode_original_png(orig_path: Path, src_img: Image.Image) -> bytes:
    """Return lossless PNG bytes of the unmodified original image."""
    if orig_path.suffix.lower() == '.png' and (src_img.format or '').upper() == 'PNG':
        return orig_path.read_bytes()
    buf = io.BytesIO()
    mode = 'RGBA' if src_img.mode in ('RGBA', 'LA', 'P') else 'RGB'
    src_img.convert(mode).save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def _encode_jpeg(img: Image.Image, size: tuple[int, int], quality: int, dpi: tuple[int, int]) -> bytes:
    fitted = ImageOps.fit(img.convert('RGB'), size, method=Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    fitted.save(buf, format='JPEG', quality=quality, subsampling=0 if quality >= 95 else 2, optimize=True, dpi=dpi)
    return buf.getvalue()


def _build_readme(catalog_item: dict, orig_size: tuple[int, int], orig_sha256: str) -> str:
    item_id = catalog_item['id']
    title = catalog_item['title']
    ow, oh = orig_size
    return (
        f"NAUM AI Creative Studio — Digital Postcard Package\n"
        f"==================================================\n\n"
        f"Product ID: {item_id}\n"
        f"Title: {title}\n"
        f"Original Resolution: {ow}x{oh} px\n"
        f"Original SHA-256: {orig_sha256}\n\n"
        f"Included Files:\n"
        f"- artwork_original.png : Full unmodified artwork without text (lossless PNG)\n"
        f"- greeting_ua.png      : Ready-to-send Ukrainian greeting card (1772x1181 px, 300 dpi)\n"
        f"- print_150x100mm.jpg  : Print-ready 150x100 mm card at 300 dpi (1772x1181 px, sRGB JPEG)\n"
        f"- social_media.jpg     : Web & social media version (1200x800 px, sRGB JPEG, no EXIF)\n"
        f"- README.txt           : Package documentation & print guide\n"
        f"- LICENSE.txt          : Usage license\n\n"
        f"Print Recommendations:\n"
        f"- Use print_150x100mm.jpg for standard 150x100 mm (A6/postcard) printing at 300 dpi.\n"
        f"- Color profile: sRGB. Recommended paper: matte or silk 300-350 gsm.\n\n"
        f"Support & Custom Requests:\n"
        f"- Telegram: https://t.me/NaumCreativeStudioBot\n"
    )


def _build_license(catalog_item: dict) -> str:
    return (
        "NAUM AI Creative Studio — Standard Digital Asset License\n"
        "========================================================\n\n"
        f"Licensed Item: {catalog_item['id']} ({catalog_item['title']})\n"
        "Licensor: NAUM AI Creative Studio (https://t.me/NaumCreativeStudioBot)\n\n"
        "PERMITTED USES:\n"
        "1. Personal greetings, digital messaging, and social media posts.\n"
        "2. Printing physical postcards for personal, family, or non-resale corporate gifting.\n\n"
        "PROHIBITED USES:\n"
        "1. Reselling, redistributing, sublicensing, or sharing the digital files on marketplaces,\n"
        "   stock platforms, or free asset collections.\n"
        "2. Claiming authorship or registering the artwork as a trademark/NFT.\n\n"
        "All rights not expressly granted remain reserved by NAUM AI Creative Studio.\n"
    )


def build_postcard_package(
    repo_root: Path,
    item_id: str,
    originals_dir: Path = Path(DEFAULT_PRIVATE_DIR),
    output_dir: Path = Path(DEFAULT_PACKAGES_DIR),
    verify_hash: bool = True,
) -> dict:
    """Build a commercial ZIP package for a P0 postcard item atomically."""
    if item_id not in POSTCARD_GREETINGS:
        raise ValueError(f'Unsupported postcard item_id for P0 builder: {item_id!r}')

    catalog = json.loads((repo_root / 'catalog.json').read_text(encoding='utf-8'))
    manifest = json.loads((repo_root / 'generation_manifest.json').read_text(encoding='utf-8'))

    c_item = next((x for x in catalog.get('items', []) if x.get('id') == item_id), None)
    m_item = next((x for x in manifest.get('items', []) if x.get('id') == item_id), None)
    if not c_item or not m_item:
        raise ValueError(f'Item {item_id!r} not found in catalog.json/generation_manifest.json')

    category = str(m_item.get('category') or c_item.get('category') or 'newyear')
    orig_path = find_original_file(originals_dir, category, item_id, m_item, verify_hash=verify_hash)
    orig_sha256 = sha256_file(orig_path)

    with Image.open(orig_path) as img:
        img.load()
        orig_size = img.size
        rgb = img.convert('RGB')
        artwork_png = _encode_original_png(orig_path, img)
        greeting_png = _render_greeting_card(rgb, item_id)
        print_jpg = _encode_jpeg(rgb, PRINT_SIZE_PX, quality=95, dpi=PRINT_DPI)
        social_jpg = _encode_jpeg(rgb, SOCIAL_SIZE_PX, quality=90, dpi=(72, 72))

    readme_txt = _build_readme(c_item, orig_size, orig_sha256).encode('utf-8')
    license_txt = _build_license(c_item).encode('utf-8')

    files_map = {
        'artwork_original.png': artwork_png,
        'greeting_ua.png': greeting_png,
        'print_150x100mm.jpg': print_jpg,
        'social_media.jpg': social_jpg,
        'README.txt': readme_txt,
        'LICENSE.txt': license_txt,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    zip_dest = output_dir / f'{item_id}.zip'
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{item_id}.', suffix='.zip.tmp', dir=str(output_dir))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for arcname, payload in files_map.items():
                info = zipfile.ZipInfo(filename=arcname, date_time=(2026, 11, 26, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o644 & 0xFFFF) << 16
                zf.writestr(info, payload)
        os.chmod(tmp_path, PACKAGE_FILE_MODE)
        os.replace(tmp_path, zip_dest)
        os.chmod(zip_dest, PACKAGE_FILE_MODE)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return {
        'ok': True,
        'item_id': item_id,
        'package_path': str(zip_dest),
        'package_sha256': sha256_file(zip_dest),
        'package_bytes': zip_dest.stat().st_size,
        'original_path': str(orig_path),
        'original_sha256': orig_sha256,
        'original_size': f'{orig_size[0]}x{orig_size[1]}',
        'files': list(files_map.keys()),
    }


def build_all_p0_postcards(
    repo_root: Path,
    item_ids: Iterable[str] = P0_POSTCARD_IDS,
    originals_dir: Path = Path(DEFAULT_PRIVATE_DIR),
    output_dir: Path = Path(DEFAULT_PACKAGES_DIR),
    verify_hash: bool = True,
) -> list[dict]:
    """Build commercial ZIP packages for all specified P0 postcard IDs."""
    return [
        build_postcard_package(
            repo_root=repo_root,
            item_id=item_id,
            originals_dir=originals_dir,
            output_dir=output_dir,
            verify_hash=verify_hash,
        )
        for item_id in item_ids
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description='Build commercial ZIP packages for NAUM P0 postcards')
    ap.add_argument('--repo', default='/opt/ai-creative-showcase', help='Path to ai-creative-showcase repo')
    ap.add_argument('--originals-dir', default=DEFAULT_PRIVATE_DIR, help='Private originals directory')
    ap.add_argument('--output-dir', default=DEFAULT_PACKAGES_DIR, help='Output packages directory')
    ap.add_argument('--item-id', action='append', dest='item_ids', help='Specific postcard ID (repeatable)')
    ap.add_argument('--skip-hash-check', action='store_true', help='Skip manifest original SHA-256 check')
    args = ap.parse_args()

    ids = args.item_ids if args.item_ids else list(P0_POSTCARD_IDS)
    results = build_all_p0_postcards(
        repo_root=Path(args.repo).resolve(),
        item_ids=ids,
        originals_dir=Path(args.originals_dir),
        output_dir=Path(args.output_dir),
        verify_hash=not args.skip_hash_check,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
