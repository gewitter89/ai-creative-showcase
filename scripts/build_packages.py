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
PHOTOSHOP_OVERLAY_IDS = ('ps-pack-01', 'ps-snow-02')
PHOTOSHOP_BACKGROUND_IDS = ('ps-bg-03',)
PHOTOSHOP_IDS = PHOTOSHOP_OVERLAY_IDS + PHOTOSHOP_BACKGROUND_IDS
CONCEPT_3D_IDS = ('3d-ref-01', '3d-tree-02', '3d-owl-03')
ALL_PRODUCT_IDS = P0_POSTCARD_IDS + PHOTOSHOP_IDS + CONCEPT_3D_IDS

DEMO_PRODUCT_ID = 'demo-ny'
DEMO_SOURCE_PREVIEW = 'assets/newyear/ny-kyiv-01/preview.webp'
PRINT_SIZE_PX = (1772, 1181)  # 150x100 mm @ 300 dpi
PRINT_DPI = (300, 300)
SOCIAL_SIZE_PX = (1200, 800)  # 3:2 web / social media
PS_OVERLAY_HIGH_RES_PX = (3000, 2000)  # 3:2 high-res Screen overlay
PS_BG_HIGH_RES_PX = (2400, 3000)  # 4:5 portrait high-res backdrop @ 300 dpi
PS_BG_WEB_PX = (960, 1200)  # 4:5 portrait web/social backdrop
CONCEPT_3D_HIGH_RES_PX = (2048, 2048)  # 1:1 sculpting viewport reference
CONCEPT_3D_PREVIEW_PX = (1200, 1200)  # 1:1 compact JPEG reference
PACKAGE_FILE_MODE = 0o640
ALLOWED_VARIANT_EXTS = frozenset({'.png', '.jpg', '.jpeg', '.webp'})

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


def _find_extra_variants(item_dir: Path, main_original: Path) -> list[Path]:
    """Return optional extra verified image variants in item_dir, excluding main_original."""
    main_resolved = main_original.resolve()
    extras: list[Path] = []
    for path in sorted(item_dir.iterdir()):
        if not path.is_file() or path.name.startswith('.'):
            continue
        if path.resolve() == main_resolved:
            continue
        if path.suffix.lower() not in ALLOWED_VARIANT_EXTS:
            continue
        if '_original' in path.stem:
            # Skip older versioned originals unless explicitly named as a variant/view
            if '_variant' not in path.stem and '_view' not in path.stem:
                continue
        extras.append(path)
    return extras


def _load_item_records(repo_root: Path, item_id: str) -> tuple[dict, dict, str]:
    """Load catalog & manifest records for item_id and resolve its category."""
    catalog = json.loads((repo_root / 'catalog.json').read_text(encoding='utf-8'))
    manifest = json.loads((repo_root / 'generation_manifest.json').read_text(encoding='utf-8'))

    c_item = next((x for x in catalog.get('items', []) if x.get('id') == item_id), None)
    m_item = next((x for x in manifest.get('items', []) if x.get('id') == item_id), None)
    if not c_item or not m_item:
        raise ValueError(f'Item {item_id!r} not found in catalog.json/generation_manifest.json')

    category = str(m_item.get('category') or c_item.get('category') or '').strip()
    if not CAT_RE.fullmatch(category):
        raise ValueError(f'Invalid category for {item_id!r}: {category!r}')
    return c_item, m_item, category


def _write_zip_atomic(output_dir: Path, item_id: str, files_map: dict[str, bytes]) -> Path:
    """Write deterministic ZIP archive atomically with 0640 permissions."""
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
    return zip_dest


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


def _encode_png_fitted(
    img: Image.Image,
    size: tuple[int, int],
    dpi: tuple[int, int] | None = None,
) -> bytes:
    """Encode an image resized/fitted cleanly with LANCZOS as lossless PNG without EXIF."""
    mode = 'RGBA' if img.mode in ('RGBA', 'LA', 'P') else 'RGB'
    fitted = ImageOps.fit(img.convert(mode), size, method=Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    kwargs: dict = {'format': 'PNG', 'optimize': True}
    if dpi is not None:
        kwargs['dpi'] = dpi
    fitted.save(buf, **kwargs)
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


def _build_photoshop_overlay_readme(
    catalog_item: dict,
    orig_size: tuple[int, int],
    orig_sha256: str,
    extra_names: list[str],
) -> str:
    item_id = catalog_item['id']
    title = catalog_item['title']
    ow, oh = orig_size
    extras_block = ''.join(f'- {name:<23}: Additional verified overlay variant (lossless PNG)\n' for name in extra_names)
    return (
        f"NAUM AI Creative Studio — Photoshop Overlay Package\n"
        f"===================================================\n\n"
        f"Product ID: {item_id}\n"
        f"Title: {title}\n"
        f"Native Original Resolution: {ow}x{oh} px\n"
        f"Original SHA-256: {orig_sha256}\n\n"
        f"HONEST FORMAT NOTICE:\n"
        f"- Raster PNG/JPEG files on a solid pure black RGB(0,0,0) background.\n"
        f"- NOT a layered .PSD file and NOT alpha-channel transparency.\n"
        f"- overlay_3000x2000.png is cleanly upscaled (3:2 LANCZOS) from the {ow}x{oh} native original.\n\n"
        f"Included Files:\n"
        f"- overlay_original.png   : Unmodified private original ({ow}x{oh} px, lossless PNG)\n"
        f"- overlay_3000x2000.png  : High-resolution 3:2 overlay (3000x2000 px, 300 dpi PNG)\n"
        f"- overlay_preview.jpg    : Reference preview (1200x800 px, sRGB JPEG, no EXIF)\n"
        f"{extras_block}"
        f"- README.txt             : Screen blending guide & format notice\n"
        f"- LICENSE.txt            : Usage license\n\n"
        f"How to Use in Adobe Photoshop (Screen Blending):\n"
        f"1. Open your base photo or banner composition in Photoshop.\n"
        f"2. File -> Place Embedded -> choose overlay_original.png or overlay_3000x2000.png.\n"
        f"3. In the Layers panel, change Blend Mode from Normal to Screen (Экран).\n"
        f"4. The pure black RGB(0,0,0) background becomes invisible, leaving only the light/snow.\n"
        f"5. Optional: press Ctrl+L (Cmd+L) / Levels to clip any faint dark edge, or adjust Opacity.\n"
        f"   (Also works in Affinity Photo, Photopea, GIMP, and CapCut/Premiere with Screen mode.)\n\n"
        f"Support & Custom Requests:\n"
        f"- Telegram: https://t.me/NaumCreativeStudioBot\n"
    )


def _build_photoshop_bg_readme(
    catalog_item: dict,
    orig_size: tuple[int, int],
    orig_sha256: str,
    extra_names: list[str],
) -> str:
    item_id = catalog_item['id']
    title = catalog_item['title']
    ow, oh = orig_size
    extras_block = ''.join(f'- {name:<25}: Additional verified backdrop variant (lossless PNG)\n' for name in extra_names)
    return (
        f"NAUM AI Creative Studio — Photoshop Studio Backdrop Package\n"
        f"===========================================================\n\n"
        f"Product ID: {item_id}\n"
        f"Title: {title}\n"
        f"Native Original Resolution: {ow}x{oh} px\n"
        f"Original SHA-256: {orig_sha256}\n\n"
        f"HONEST FORMAT NOTICE:\n"
        f"- Flat raster PNG/JPEG backgrounds in 4:5 portrait aspect ratio.\n"
        f"- NOT an editable multi-layer .PSD mockup.\n"
        f"- background_2400x3000.png and background_print_4x5.jpg are 4:5 upscaled from {ow}x{oh} px.\n\n"
        f"Included Files:\n"
        f"- background_original.png  : Unmodified private original ({ow}x{oh} px, lossless PNG)\n"
        f"- background_2400x3000.png : High-resolution 4:5 backdrop (2400x3000 px, 300 dpi PNG)\n"
        f"- background_print_4x5.jpg : Print-ready 4:5 sRGB JPEG (2400x3000 px, 300 dpi, no EXIF)\n"
        f"- background_web_960x1200.jpg : Web/Instagram 4:5 JPEG (960x1200 px, no EXIF)\n"
        f"{extras_block}"
        f"- README.txt               : Photoshop compositing guide & format notice\n"
        f"- LICENSE.txt              : Usage license\n\n"
        f"Photoshop Compositing Tips (including Screen Blending):\n"
        f"1. Place background_2400x3000.png as the bottom layer.\n"
        f"2. Place your cut-out product above the podium and paint a soft Multiply contact shadow.\n"
        f"3. For extra snow/bokeh atmosphere, add an overlay layer above your product and set\n"
        f"   Blend Mode to Screen (Экран) so the black background disappears.\n\n"
        f"Support & Custom Requests:\n"
        f"- Telegram: https://t.me/NaumCreativeStudioBot\n"
    )


def _build_3d_concept_readme(
    catalog_item: dict,
    orig_size: tuple[int, int],
    orig_sha256: str,
    extra_names: list[str],
) -> str:
    item_id = catalog_item['id']
    title = catalog_item['title']
    ow, oh = orig_size
    extras_block = ''.join(f'- {name:<24}: Additional orthographic view reference (PNG)\n' for name in extra_names)
    return (
        f"NAUM AI Creative Studio — 3D Modeling Concept Reference Package\n"
        f"===============================================================\n\n"
        f"Product ID: {item_id}\n"
        f"Title: {title}\n"
        f"Native Original Resolution: {ow}x{oh} px\n"
        f"Original SHA-256: {orig_sha256}\n\n"
        f"IMPORTANT / ВАЖНО — NOT A 3D PRINTABLE FILE (НЕ ГОТОВЫЙ STL):\n"
        f"- Это 2D референс-концепт для 3D-моделирования и скульптинга (PNG/JPG),\n"
        f"  а НЕ готовая 3D-модель (без STL / 3MF / OBJ / G-code).\n"
        f"- This package contains 2D raster design reference art ONLY (NOT STL / 3MF / OBJ).\n"
        f"- Printability, wall thickness, overhangs, and manifold geometry must be built and\n"
        f"  verified manually in Blender / ZBrush / Nomad Sculpt and your 3D slicer.\n\n"
        f"Included Files:\n"
        f"- concept_original.png    : Unmodified private reference original ({ow}x{oh} px, lossless PNG)\n"
        f"- reference_2048x2048.png : High-res 1:1 viewport reference (2048x2048 px, lossless PNG)\n"
        f"- reference_1200x1200.jpg : Compact 1:1 sRGB JPEG reference (1200x1200 px, no EXIF)\n"
        f"{extras_block}"
        f"- README.txt              : 3D modeling reference disclaimer & guide\n"
        f"- LICENSE.txt             : Usage license\n\n"
        f"How to Use for 3D Modeling:\n"
        f"- Blender: Add -> Image -> Reference (in Orthographic view) and block out base proportions.\n"
        f"- Nomad Sculpt / ZBrush: Load reference_2048x2048.png as Image Plane / Spotlight.\n\n"
        f"Support & Custom Requests:\n"
        f"- Telegram: https://t.me/NaumCreativeStudioBot\n"
    )


def _build_license(catalog_item: dict, kind: str = 'postcard') -> str:
    if kind == 'photoshop':
        permitted = (
            "1. Personal and commercial graphic design, social media visuals, banners, and mockups\n"
            "   where the asset is flattened/composited into a finished creative work.\n"
            "2. Client projects and printed promotional materials.\n"
        )
    elif kind == '3d_concept':
        permitted = (
            "1. Using the 2D reference artwork as a visual guide to sculpt or model your own\n"
            "   3D mesh in Blender, ZBrush, Nomad Sculpt, or CAD software for personal printing.\n"
            "2. Studying proportions and silhouette for non-commercial creative practice.\n"
        )
    else:
        permitted = (
            "1. Personal greetings, digital messaging, and social media posts.\n"
            "2. Printing physical postcards for personal, family, or non-resale corporate gifting.\n"
        )

    return (
        "NAUM AI Creative Studio — Standard Digital Asset License\n"
        "========================================================\n\n"
        f"Licensed Item: {catalog_item['id']} ({catalog_item['title']})\n"
        "Licensor: NAUM AI Creative Studio (https://t.me/NaumCreativeStudioBot)\n\n"
        "PERMITTED USES:\n"
        f"{permitted}\n"
        "PROHIBITED USES:\n"
        "1. Reselling, redistributing, sublicensing, or sharing the raw digital files on marketplaces,\n"
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

    c_item, m_item, category = _load_item_records(repo_root, item_id)
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
    license_txt = _build_license(c_item, kind='postcard').encode('utf-8')

    files_map = {
        'artwork_original.png': artwork_png,
        'greeting_ua.png': greeting_png,
        'print_150x100mm.jpg': print_jpg,
        'social_media.jpg': social_jpg,
        'README.txt': readme_txt,
        'LICENSE.txt': license_txt,
    }

    zip_dest = _write_zip_atomic(output_dir, item_id, files_map)
    return {
        'ok': True,
        'item_id': item_id,
        'category': category,
        'package_path': str(zip_dest),
        'package_sha256': sha256_file(zip_dest),
        'package_bytes': zip_dest.stat().st_size,
        'original_path': str(orig_path),
        'original_sha256': orig_sha256,
        'original_size': f'{orig_size[0]}x{orig_size[1]}',
        'files': list(files_map.keys()),
    }


def build_photoshop_package(
    repo_root: Path,
    item_id: str,
    originals_dir: Path = Path(DEFAULT_PRIVATE_DIR),
    output_dir: Path = Path(DEFAULT_PACKAGES_DIR),
    verify_hash: bool = True,
) -> dict:
    """Build a commercial ZIP package for a Photoshop overlay or studio backdrop item."""
    if item_id not in PHOTOSHOP_IDS:
        raise ValueError(f'Unsupported Photoshop item_id: {item_id!r}')

    c_item, m_item, category = _load_item_records(repo_root, item_id)
    orig_path = find_original_file(originals_dir, category, item_id, m_item, verify_hash=verify_hash)
    orig_sha256 = sha256_file(orig_path)
    extra_paths = _find_extra_variants(orig_path.parent, orig_path)

    files_map: dict[str, bytes] = {}
    extra_arcnames: list[str] = []

    with Image.open(orig_path) as img:
        img.load()
        orig_size = img.size
        if item_id in PHOTOSHOP_OVERLAY_IDS:
            files_map['overlay_original.png'] = _encode_original_png(orig_path, img)
            files_map['overlay_3000x2000.png'] = _encode_png_fitted(img, PS_OVERLAY_HIGH_RES_PX, dpi=PRINT_DPI)
            files_map['overlay_preview.jpg'] = _encode_jpeg(img, SOCIAL_SIZE_PX, quality=90, dpi=(72, 72))
        else:
            files_map['background_original.png'] = _encode_original_png(orig_path, img)
            files_map['background_2400x3000.png'] = _encode_png_fitted(img, PS_BG_HIGH_RES_PX, dpi=PRINT_DPI)
            files_map['background_print_4x5.jpg'] = _encode_jpeg(img, PS_BG_HIGH_RES_PX, quality=95, dpi=PRINT_DPI)
            files_map['background_web_960x1200.jpg'] = _encode_jpeg(img, PS_BG_WEB_PX, quality=90, dpi=(72, 72))

    for idx, extra_path in enumerate(extra_paths, start=2):
        with Image.open(extra_path) as extra_img:
            extra_img.load()
            prefix = 'overlay_variant' if item_id in PHOTOSHOP_OVERLAY_IDS else 'background_variant'
            arcname = f'{prefix}_{idx:02d}.png'
            files_map[arcname] = _encode_original_png(extra_path, extra_img)
            extra_arcnames.append(arcname)

    if item_id in PHOTOSHOP_OVERLAY_IDS:
        readme_str = _build_photoshop_overlay_readme(c_item, orig_size, orig_sha256, extra_arcnames)
    else:
        readme_str = _build_photoshop_bg_readme(c_item, orig_size, orig_sha256, extra_arcnames)

    files_map['README.txt'] = readme_str.encode('utf-8')
    files_map['LICENSE.txt'] = _build_license(c_item, kind='photoshop').encode('utf-8')

    zip_dest = _write_zip_atomic(output_dir, item_id, files_map)
    return {
        'ok': True,
        'item_id': item_id,
        'category': category,
        'package_path': str(zip_dest),
        'package_sha256': sha256_file(zip_dest),
        'package_bytes': zip_dest.stat().st_size,
        'original_path': str(orig_path),
        'original_sha256': orig_sha256,
        'original_size': f'{orig_size[0]}x{orig_size[1]}',
        'files': list(files_map.keys()),
    }


def build_3d_concept_package(
    repo_root: Path,
    item_id: str,
    originals_dir: Path = Path(DEFAULT_PRIVATE_DIR),
    output_dir: Path = Path(DEFAULT_PACKAGES_DIR),
    verify_hash: bool = True,
) -> dict:
    """Build a commercial ZIP package for a 3D modeling concept reference (not STL)."""
    if item_id not in CONCEPT_3D_IDS:
        raise ValueError(f'Unsupported 3D concept item_id: {item_id!r}')

    c_item, m_item, category = _load_item_records(repo_root, item_id)
    orig_path = find_original_file(originals_dir, category, item_id, m_item, verify_hash=verify_hash)
    orig_sha256 = sha256_file(orig_path)
    extra_paths = _find_extra_variants(orig_path.parent, orig_path)

    files_map: dict[str, bytes] = {}
    extra_arcnames: list[str] = []

    with Image.open(orig_path) as img:
        img.load()
        orig_size = img.size
        files_map['concept_original.png'] = _encode_original_png(orig_path, img)
        files_map['reference_2048x2048.png'] = _encode_png_fitted(img, CONCEPT_3D_HIGH_RES_PX, dpi=PRINT_DPI)
        files_map['reference_1200x1200.jpg'] = _encode_jpeg(img, CONCEPT_3D_PREVIEW_PX, quality=92, dpi=(72, 72))

    for idx, extra_path in enumerate(extra_paths, start=2):
        with Image.open(extra_path) as extra_img:
            extra_img.load()
            arcname = f'reference_view_{idx:02d}.png'
            files_map[arcname] = _encode_original_png(extra_path, extra_img)
            extra_arcnames.append(arcname)

    files_map['README.txt'] = _build_3d_concept_readme(c_item, orig_size, orig_sha256, extra_arcnames).encode('utf-8')
    files_map['LICENSE.txt'] = _build_license(c_item, kind='3d_concept').encode('utf-8')

    zip_dest = _write_zip_atomic(output_dir, item_id, files_map)
    return {
        'ok': True,
        'item_id': item_id,
        'category': category,
        'package_path': str(zip_dest),
        'package_sha256': sha256_file(zip_dest),
        'package_bytes': zip_dest.stat().st_size,
        'original_path': str(orig_path),
        'original_sha256': orig_sha256,
        'original_size': f'{orig_size[0]}x{orig_size[1]}',
        'files': list(files_map.keys()),
    }


def build_product_package(
    repo_root: Path,
    item_id: str,
    originals_dir: Path = Path(DEFAULT_PRIVATE_DIR),
    output_dir: Path = Path(DEFAULT_PACKAGES_DIR),
    verify_hash: bool = True,
) -> dict:
    """Dispatch ZIP package building for any supported catalog product ID."""
    if item_id in POSTCARD_GREETINGS:
        return build_postcard_package(repo_root, item_id, originals_dir, output_dir, verify_hash=verify_hash)
    if item_id in PHOTOSHOP_IDS:
        return build_photoshop_package(repo_root, item_id, originals_dir, output_dir, verify_hash=verify_hash)
    if item_id in CONCEPT_3D_IDS:
        return build_3d_concept_package(repo_root, item_id, originals_dir, output_dir, verify_hash=verify_hash)
    raise ValueError(f'Unsupported product item_id: {item_id!r}')


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


def build_all_packages(
    repo_root: Path,
    item_ids: Iterable[str] = ALL_PRODUCT_IDS,
    originals_dir: Path = Path(DEFAULT_PRIVATE_DIR),
    output_dir: Path = Path(DEFAULT_PACKAGES_DIR),
    verify_hash: bool = True,
) -> list[dict]:
    """Build commercial ZIP packages for all specified batch-01 catalog products."""
    return [
        build_product_package(
            repo_root=repo_root,
            item_id=item_id,
            originals_dir=originals_dir,
            output_dir=output_dir,
            verify_hash=verify_hash,
        )
        for item_id in item_ids
    ]


def _render_demo_watermark(canvas_rgba: Image.Image, text: str = 'DEMO · @NaumCreativeStudioBot') -> Image.Image:
    """Apply a clean semi-transparent demo badge across the bottom edge."""
    w, h = canvas_rgba.size
    overlay = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    bar_h = max(44, int(h * 0.065))
    draw.rectangle([(0, h - bar_h), (w, h)], fill=(8, 13, 23, 190))
    font = _load_font(24)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(((w - tw) // 2, h - bar_h + (bar_h - th) // 2 - 2), text, font=font, fill=(250, 234, 186, 245))
    return Image.alpha_composite(canvas_rgba, overlay)


def build_demo_postcard_package(
    repo_root: Path,
    output_dir: Path = Path(DEFAULT_PACKAGES_DIR),
    item_id: str = DEMO_PRODUCT_ID,
    source_rel_path: str = DEMO_SOURCE_PREVIEW,
) -> dict:
    """Build the free lead-magnet demo ZIP package from public preview artwork (no paid original exposed)."""
    if not ID_RE.fullmatch(item_id):
        raise ValueError(f'Invalid demo item_id: {item_id!r}')
    src_path = (repo_root / source_rel_path).resolve()
    assets_root = (repo_root / 'assets').resolve()
    if not src_path.is_relative_to(assets_root) or not src_path.is_file():
        raise FileNotFoundError(f'Demo preview source not found: {src_path}')

    with Image.open(src_path) as img:
        img.load()
        rgb = img.convert('RGB')
        base_social = ImageOps.fit(rgb, SOCIAL_SIZE_PX, method=Image.Resampling.LANCZOS).convert('RGBA')

    # Render greeting onto 1200x800 demo card with subtle DEMO footer
    spec = POSTCARD_GREETINGS['ny-kyiv-01']
    font = _load_font(58)
    measure = ImageDraw.Draw(base_social)
    bbox = measure.multiline_textbbox((0, 0), spec['text'], font=font, spacing=12, align='center')
    tw = bbox[2] - bbox[0]
    x = (SOCIAL_SIZE_PX[0] - tw) // 2
    y = int(SOCIAL_SIZE_PX[1] * 0.075)

    shadow_layer = Image.new('RGBA', SOCIAL_SIZE_PX, (0, 0, 0, 0))
    ImageDraw.Draw(shadow_layer).multiline_text(
        (x + 2, y + 3), spec['text'], font=font, fill=spec['shadow'], spacing=12, align='center'
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=4))
    greeting_rgba = Image.alpha_composite(base_social, shadow_layer)

    fg_layer = Image.new('RGBA', SOCIAL_SIZE_PX, (0, 0, 0, 0))
    ImageDraw.Draw(fg_layer).multiline_text(
        (x, y), spec['text'], font=font, fill=spec['fill'], spacing=12, align='center'
    )
    greeting_rgba = _render_demo_watermark(Image.alpha_composite(greeting_rgba, fg_layer))

    greeting_buf = io.BytesIO()
    greeting_rgba.save(greeting_buf, format='PNG', optimize=True)

    clean_demo_rgba = _render_demo_watermark(base_social, text='DEMO PREVIEW · @NaumCreativeStudioBot')
    jpg_buf = io.BytesIO()
    clean_demo_rgba.convert('RGB').save(jpg_buf, format='JPEG', quality=88, optimize=True)

    readme_txt = (
        "NAUM AI Creative Studio — Free Demo Postcard (Lead Magnet)\n"
        "==========================================================\n\n"
        f"Demo ID: {item_id}\n"
        "Source Artwork: ny-kyiv-01 (Киев в новогодних огнях)\n\n"
        "Included Files:\n"
        "- demo_greeting_ua.png : Demo postcard with Ukrainian greeting (1200x800 px, DEMO tag)\n"
        "- demo_ny_kyiv.jpg     : Demo clean preview (1200x800 px, DEMO tag)\n"
        "- README.txt           : How to unlock the full 300 dpi print package\n"
        "- LICENSE.txt          : Free personal demo license\n\n"
        "Want the full 300 dpi print version (1772x1181 px) without the DEMO badge?\n"
        "- Open @NaumCreativeStudioBot and run /catalog or /start buy_ny-kyiv-01\n"
        "- Bot link: https://t.me/NaumCreativeStudioBot\n"
    ).encode('utf-8')

    license_txt = (
        "NAUM AI Creative Studio — Free Demo Asset License\n"
        "==================================================\n\n"
        f"Item: {item_id}\n"
        "Licensor: NAUM AI Creative Studio (https://t.me/NaumCreativeStudioBot)\n\n"
        "PERMITTED: Personal preview, sharing with friends/family, and evaluating print/visual quality.\n"
        "PROHIBITED: Commercial use, resale, redistribution in asset packs, or removing attribution.\n"
    ).encode('utf-8')

    files_map = {
        'demo_greeting_ua.png': greeting_buf.getvalue(),
        'demo_ny_kyiv.jpg': jpg_buf.getvalue(),
        'README.txt': readme_txt,
        'LICENSE.txt': license_txt,
    }

    zip_dest = _write_zip_atomic(output_dir, item_id, files_map)
    return {
        'ok': True,
        'item_id': item_id,
        'is_demo': True,
        'package_path': str(zip_dest),
        'package_sha256': sha256_file(zip_dest),
        'package_bytes': zip_dest.stat().st_size,
        'files': list(files_map.keys()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description='Build commercial ZIP packages for NAUM AI Creative Studio')
    ap.add_argument('--repo', default='/opt/ai-creative-showcase', help='Path to ai-creative-showcase repo')
    ap.add_argument('--originals-dir', default=DEFAULT_PRIVATE_DIR, help='Private originals directory')
    ap.add_argument('--output-dir', default=DEFAULT_PACKAGES_DIR, help='Output packages directory')
    ap.add_argument('--item-id', action='append', dest='item_ids', help='Specific product ID (repeatable)')
    ap.add_argument('--p0-only', action='store_true', help='Build only P0 postcards (ny-kyiv-01, ny-cozy-02, ny-minimal-03)')
    ap.add_argument('--skip-hash-check', action='store_true', help='Skip manifest original SHA-256 check')
    ap.add_argument('--include-demo', action='store_true', help='Also build free demo-ny.zip lead magnet')
    ap.add_argument('--demo-only', action='store_true', help='Build only free demo-ny.zip lead magnet')
    args = ap.parse_args()

    repo_root = Path(args.repo).resolve()
    out_dir = Path(args.output_dir)
    results: list[dict] = []
    if not args.demo_only:
        if args.item_ids:
            ids = args.item_ids
        elif args.p0_only:
            ids = list(P0_POSTCARD_IDS)
        else:
            ids = list(ALL_PRODUCT_IDS)
        results.extend(
            build_all_packages(
                repo_root=repo_root,
                item_ids=ids,
                originals_dir=Path(args.originals_dir),
                output_dir=out_dir,
                verify_hash=not args.skip_hash_check,
            )
        )
    if args.include_demo or args.demo_only:
        results.append(build_demo_postcard_package(repo_root=repo_root, output_dir=out_dir))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
