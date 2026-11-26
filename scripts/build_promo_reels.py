#!/usr/bin/env python3
"""Build vertical 1080x1920 MP4 promo reels from assets/ previews using Pillow + ffmpeg (0 UAH, no paid APIs)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from ingest_asset import CAT_RE, ID_RE

REEL_SIZE = (1080, 1920)
BG_CANVAS_SIZE = (1620, 2880)  # 1.5x vertical canvas for crisp zoompan without upscaling
REEL_FPS = 30
DEFAULT_DURATION_SEC = 6.0
DEFAULT_PROMO_DIR = '/var/lib/naum-studio/promo'
BOT_HANDLE = '@NaumCreativeStudioBot'
BOT_URL = 'https://t.me/NaumCreativeStudioBot'
DEMO_URL = 'https://t.me/NaumCreativeStudioBot?start=demo_ny'

FONT_BOLD_CANDIDATES = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf',
)
FONT_REG_CANDIDATES = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf',
)

# ISO/IEC 18004 QR Code tables for Versions 1..6, Error Correction Level M
# (version, size, total_codewords, blocks: tuple[(count, data_cw, ec_cw), ...], align_coords)
_QR_VERSION_SPECS_M = [
    (1, 21, 26, ((1, 16, 10),), ()),
    (2, 25, 44, ((1, 28, 16),), (6, 18)),
    (3, 29, 70, ((1, 44, 26),), (6, 22)),
    (4, 33, 100, ((2, 32, 18),), (6, 26)),
    (5, 37, 134, ((2, 43, 24),), (6, 30)),
    (6, 41, 172, ((4, 27, 16),), (6, 34)),
]


def _gf_init() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _gf_init()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator_poly(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        next_poly = [0] * (len(poly) + 1)
        root = _GF_EXP[i]
        for idx, coeff in enumerate(poly):
            next_poly[idx] ^= coeff
            next_poly[idx + 1] ^= _gf_mul(coeff, root)
        poly = next_poly
    return poly


def _rs_encode(data: list[int], ec_len: int) -> list[int]:
    gen = _rs_generator_poly(ec_len)
    buf = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = buf[i]
        if coef != 0:
            for j in range(1, len(gen)):
                buf[i + j] ^= _gf_mul(gen[j], coef)
    return buf[len(data):]


def _qr_format_bits(mask_id: int) -> int:
    """Compute 15-bit BCH format info for ECC Level M (0b00) and mask_id (0..7)."""
    data = (0b00 << 3) | (mask_id & 0x07)
    val = data << 10
    gen = 0b10100110111
    for shift in range(14, 9, -1):
        if val & (1 << shift):
            val ^= gen << (shift - 10)
    raw = ((data << 10) | val) ^ 0b101010000010010
    return raw


def _qr_penalty(matrix: list[list[int]]) -> int:
    n = len(matrix)
    score = 0
    # Rule 1: 5+ consecutive same-color modules in rows/cols
    for line in matrix:
        run = 1
        for i in range(1, n):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    for c in range(n):
        run = 1
        for r in range(1, n):
            if matrix[r][c] == matrix[r - 1][c]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    # Rule 2: 2x2 blocks
    for r in range(n - 1):
        for c in range(n - 1):
            v = matrix[r][c]
            if v == matrix[r][c + 1] == matrix[r + 1][c] == matrix[r + 1][c + 1]:
                score += 3
    # Rule 4: dark ratio balance
    dark = sum(sum(row) for row in matrix)
    pct = (dark * 100) // (n * n)
    score += (abs(pct - 50) // 5) * 10
    return score


def generate_qr_matrix(text: str) -> list[list[int]]:
    """Encode a UTF-8 string (up to 106 bytes) into a standard ISO/IEC 18004 QR matrix (ECC Level M)."""
    payload = text.encode('utf-8')
    selected = None
    for spec in _QR_VERSION_SPECS_M:
        total_data_cw = sum(cnt * dcw for cnt, dcw, _ in spec[3])
        max_bytes = total_data_cw - 2  # 4-bit mode + 8-bit count + 4-bit terminator = 16 bits = 2 bytes
        if len(payload) <= max_bytes:
            selected = spec
            break
    if selected is None:
        raise ValueError(f'QR text too long ({len(payload)} bytes > 106 bytes)')

    version, size, _total_cw, blocks_spec, align_coords = selected
    total_data_cw = sum(cnt * dcw for cnt, dcw, _ in blocks_spec)

    # Bit stream: Byte mode (0100), 8-bit length, payload, terminator, pad bytes
    bits: list[int] = []

    def push_bits(val: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((val >> shift) & 1)

    push_bits(0b0100, 4)
    push_bits(len(payload), 8)
    for b in payload:
        push_bits(b, 8)
    term_len = min(4, total_data_cw * 8 - len(bits))
    push_bits(0, term_len)
    while len(bits) % 8 != 0:
        bits.append(0)
    data_cw = [int(''.join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
    pad_toggle = 0
    while len(data_cw) < total_data_cw:
        data_cw.append(0xEC if pad_toggle % 2 == 0 else 0x11)
        pad_toggle += 1

    # Split into RS blocks and interleave
    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    offset = 0
    for count, dcw, ecw in blocks_spec:
        for _ in range(count):
            blk = data_cw[offset:offset + dcw]
            offset += dcw
            data_blocks.append(blk)
            ec_blocks.append(_rs_encode(blk, ecw))

    codewords: list[int] = []
    max_dcw = max(len(b) for b in data_blocks)
    for i in range(max_dcw):
        for blk in data_blocks:
            if i < len(blk):
                codewords.append(blk[i])
    max_ecw = max(len(b) for b in ec_blocks)
    for i in range(max_ecw):
        for blk in ec_blocks:
            if i < len(blk):
                codewords.append(blk[i])

    bitstream: list[int] = []
    for cw in codewords:
        for shift in range(7, -1, -1):
            bitstream.append((cw >> shift) & 1)
    if version >= 2:
        bitstream.extend([0] * 7)  # remainder bits

    # Build function pattern grid
    matrix: list[list[int | None]] = [[None] * size for _ in range(size)]
    reserved: list[list[bool]] = [[False] * size for _ in range(size)]

    def set_func(r: int, c: int, val: int) -> None:
        matrix[r][c] = val
        reserved[r][c] = True

    def place_finder(top: int, left: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = top + dr, left + dc
                if 0 <= rr < size and 0 <= cc < size:
                    if 0 <= dr <= 6 and 0 <= dc <= 6:
                        is_border = dr in (0, 6) or dc in (0, 6)
                        is_inner = 2 <= dr <= 4 and 2 <= dc <= 4
                        set_func(rr, cc, 1 if (is_border or is_inner) else 0)
                    else:
                        set_func(rr, cc, 0)

    place_finder(0, 0)
    place_finder(0, size - 7)
    place_finder(size - 7, 0)

    # Alignment patterns
    for r in align_coords:
        for c in align_coords:
            if (r == 6 and c == 6) or (r == 6 and c == size - 7) or (r == size - 7 and c == 6):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    is_dark = abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0)
                    set_func(r + dr, c + dc, 1 if is_dark else 0)

    # Timing patterns
    for i in range(8, size - 8):
        val = 1 if i % 2 == 0 else 0
        if not reserved[6][i]:
            set_func(6, i, val)
        if not reserved[i][6]:
            set_func(i, 6, val)

    # Dark module & reserve format info areas
    set_func(4 * version + 9, 8, 1)
    for i in range(9):
        reserved[8][i] = True
        reserved[i][8] = True
    for i in range(size - 8, size):
        reserved[8][i] = True
    for i in range(size - 7, size):
        reserved[i][8] = True

    # Place data bits in standard zigzag upward/downward columns
    bit_idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for dc in (0, -1):
                c = col + dc
                if not reserved[row][c]:
                    matrix[row][c] = bitstream[bit_idx] if bit_idx < len(bitstream) else 0
                    bit_idx += 1
        col -= 2
        upward = not upward

    mask_fns = (
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: ((r * c) % 2) + ((r * c) % 3) == 0,
        lambda r, c: (((r * c) % 2) + ((r * c) % 3)) % 2 == 0,
        lambda r, c: (((r + c) % 2) + ((r * c) % 3)) % 2 == 0,
    )

    best_score: int | None = None
    best_grid: list[list[int]] = []
    tl_coords = [
        (8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
        (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8),
    ]
    for mask_id, fn in enumerate(mask_fns):
        cand = [
            [
                (int(matrix[r][c] or 0) ^ (1 if (not reserved[r][c] and fn(r, c)) else 0))
                for c in range(size)
            ]
            for r in range(size)
        ]
        fmt = _qr_format_bits(mask_id)
        for idx in range(15):
            bit = (fmt >> (14 - idx)) & 1
            r1, c1 = tl_coords[idx]
            cand[r1][c1] = bit
            if idx < 7:
                cand[size - 1 - idx][8] = bit
            else:
                cand[8][size - 15 + idx] = bit
        sc = _qr_penalty(cand)
        if best_score is None or sc < best_score:
            best_score = sc
            best_grid = cand

    return best_grid


def render_qr_image(text: str, target_px: int = 210, quiet_modules: int = 3) -> Image.Image:
    """Render a crisp black-on-white QR code PIL Image."""
    grid = generate_qr_matrix(text)
    modules = len(grid)
    total = modules + quiet_modules * 2
    scale = max(2, target_px // total)
    img_px = total * scale
    img = Image.new('RGB', (img_px, img_px), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for r, row in enumerate(grid):
        for c, val in enumerate(row):
            if val:
                x0 = (c + quiet_modules) * scale
                y0 = (r + quiet_modules) * scale
                draw.rectangle([x0, y0, x0 + scale - 1, y0 + scale - 1], fill=(10, 15, 28))
    if img.size != (target_px, target_px):
        img = img.resize((target_px, target_px), Image.Resampling.NEAREST)
    return img


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = FONT_BOLD_CANDIDATES if bold else FONT_REG_CANDIDATES
    for p in candidates:
        if Path(p).is_file():
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        trial = f'{cur} {w}'
        bbox = draw.textbbox((0, 0), trial, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _build_bg_canvas(preview_path: Path) -> Image.Image:
    """Create a 1620x2880 high-res canvas with dark blurred ambient + crisp centered artwork card."""
    with Image.open(preview_path) as src:
        src.load()
        rgb = src.convert('RGB')

    bg = ImageOps.fit(rgb, BG_CANVAS_SIZE, method=Image.Resampling.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=46))
    bg = ImageEnhance.Brightness(bg).enhance(0.42)

    # Dark vignette overlay for text contrast
    canvas = bg.convert('RGBA')
    shade = Image.new('RGBA', BG_CANVAS_SIZE, (7, 11, 22, 115))
    canvas = Image.alpha_composite(canvas, shade)

    # Fit artwork cleanly into central showcase window
    max_art_w = int(BG_CANVAS_SIZE[0] * 0.86)
    max_art_h = int(BG_CANVAS_SIZE[1] * 0.48)
    art = rgb.copy()
    art.thumbnail((max_art_w, max_art_h), Image.Resampling.LANCZOS)
    aw, ah = art.size

    # Drop shadow behind artwork card
    card_x = (BG_CANVAS_SIZE[0] - aw) // 2
    card_y = int(BG_CANVAS_SIZE[1] * 0.21)
    shadow = Image.new('RGBA', BG_CANVAS_SIZE, (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow)
    s_draw.rounded_rectangle(
        [card_x - 10, card_y + 12, card_x + aw + 10, card_y + ah + 26],
        radius=28,
        fill=(0, 0, 0, 185),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=22))
    canvas = Image.alpha_composite(canvas, shadow)

    # Crisp border + artwork paste
    art_rgba = art.convert('RGBA')
    mask = Image.new('L', (aw, ah), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, aw, ah], radius=24, fill=255)
    canvas.paste(art_rgba, (card_x, card_y), mask)

    border_layer = Image.new('RGBA', BG_CANVAS_SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(border_layer).rounded_rectangle(
        [card_x, card_y, card_x + aw, card_y + ah],
        radius=24,
        outline=(250, 234, 186, 120),
        width=4,
    )
    canvas = Image.alpha_composite(canvas, border_layer)
    return canvas.convert('RGB')


def _build_hud_overlay(item: dict, qr_url: str, is_demo: bool = False) -> Image.Image:
    """Render 1080x1920 RGBA HUD overlay with titles, price badge, QR code, and @NaumCreativeStudioBot CTA."""
    w, h = REEL_SIZE
    hud = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(hud)

    font_brand = _load_font(28, bold=True)
    font_cat = _load_font(26, bold=True)
    font_title = _load_font(52, bold=True)
    font_sub = _load_font(29, bold=False)
    font_price = _load_font(32, bold=True)
    font_cta_title = _load_font(30, bold=True)
    font_cta_sub = _load_font(24, bold=False)

    # Top brand header pill
    draw.rounded_rectangle([64, 72, w - 64, 152], radius=22, fill=(10, 16, 32, 215), outline=(96, 165, 250, 120), width=2)
    draw.text((94, 96), 'NAUM AI CREATIVE STUDIO', font=font_brand, fill=(248, 250, 252, 255))

    cat_label = 'ДЕМО · БЕСПЛАТНО' if is_demo else str(item.get('category_label') or 'COLLECTION').upper()
    cat_bbox = draw.textbbox((0, 0), cat_label, font=font_cat)
    cat_w = cat_bbox[2] - cat_bbox[0]
    draw.rounded_rectangle(
        [w - 94 - cat_w - 28, 88, w - 88, 136],
        radius=16,
        fill=(16, 185, 129, 210) if is_demo else (59, 130, 246, 200),
    )
    draw.text((w - 94 - cat_w - 14, 97), cat_label, font=font_cat, fill=(255, 255, 255, 255))

    # Mid-lower info glass panel
    panel_top = 1140
    panel_bot = 1525
    draw.rounded_rectangle(
        [56, panel_top, w - 56, panel_bot],
        radius=28,
        fill=(10, 15, 28, 222),
        outline=(250, 234, 186, 110),
        width=2,
    )

    title_text = str(item.get('title') or item.get('id') or 'NAUM Collection')
    title_lines = _wrap_text(draw, title_text, font_title, w - 168)[:2]
    ty = panel_top + 32
    for line in title_lines:
        draw.text((88, ty), line, font=font_title, fill=(255, 255, 255, 255))
        ty += 62

    desc_text = str(item.get('description') or item.get('format') or '')
    desc_lines = _wrap_text(draw, desc_text, font_sub, w - 168)[:2]
    dy = ty + 14
    for line in desc_lines:
        draw.text((88, dy), line, font=font_sub, fill=(203, 213, 225, 245))
        dy += 38

    fmt_text = str(item.get('format') or 'PNG / JPG · Digital Pack')
    price_text = (
        'БЕСПЛАТНОЕ ДЕМО · /start demo_ny'
        if is_demo
        else f"★ {int(item.get('price_xtr') or 0)} Stars · {int(item.get('price_uah') or 0)} грн"
    )
    draw.text((88, panel_bot - 102), fmt_text, font=font_cta_sub, fill=(148, 163, 184, 255))
    draw.rounded_rectangle(
        [88, panel_bot - 64, w - 88, panel_bot - 16],
        radius=14,
        fill=(16, 185, 129, 55) if is_demo else (245, 158, 11, 50),
        outline=(110, 231, 183, 190) if is_demo else (253, 230, 138, 190),
        width=2,
    )
    draw.text(
        (108, panel_bot - 56),
        price_text,
        font=font_price,
        fill=(167, 243, 208, 255) if is_demo else (253, 230, 138, 255),
    )

    # Bottom QR + Telegram Bot CTA bar
    cta_top = 1555
    cta_bot = 1848
    draw.rounded_rectangle(
        [56, cta_top, w - 56, cta_bot],
        radius=28,
        fill=(15, 23, 42, 238),
        outline=(59, 130, 246, 170),
        width=3,
    )

    qr_img = render_qr_image(qr_url, target_px=216, quiet_modules=3)
    qr_x = 84
    qr_y = cta_top + (cta_bot - cta_top - 216) // 2
    draw.rounded_rectangle([qr_x - 8, qr_y - 8, qr_x + 224, qr_y + 224], radius=16, fill=(255, 255, 255, 255))
    hud.paste(qr_img.convert('RGBA'), (qr_x, qr_y))

    tx = qr_x + 248
    draw.text((tx, cta_top + 36), 'Забрать демо и каталог:', font=font_cta_sub, fill=(148, 163, 184, 255))
    draw.text((tx, cta_top + 74), BOT_HANDLE, font=font_cta_title, fill=(96, 165, 250, 255))
    draw.text((tx, cta_top + 124), 'Бесплатно: /start demo_ny', font=font_cta_sub, fill=(110, 231, 183, 255))
    draw.text((tx, cta_top + 162), 't.me/NaumCreativeStudioBot', font=font_cta_sub, fill=(226, 232, 240, 255))
    draw.text((tx, cta_top + 206), 'QR ведёт в официальный бот студии', font=_load_font(20, bold=False), fill=(148, 163, 184, 235))

    return hud


def build_promo_reel(
    repo_root: Path,
    item: dict,
    output_path: Path,
    duration_sec: float = DEFAULT_DURATION_SEC,
    fps: int = REEL_FPS,
    is_demo: bool = False,
) -> dict:
    """Render a single 1080x1920 H.264 MP4 vertical promo reel atomically using Pillow + ffmpeg."""
    item_id = str(item.get('id') or '').strip()
    if not ID_RE.fullmatch(item_id):
        raise ValueError(f'Invalid item_id: {item_id!r}')

    category = str(item.get('category') or '').strip()
    if not CAT_RE.fullmatch(category):
        raise ValueError(f'Invalid category: {category!r}')

    preview_rel = str(item.get('image') or f'assets/{category}/{item_id}/preview.webp')
    preview_path = (repo_root / preview_rel).resolve()
    assets_root = (repo_root / 'assets').resolve()
    if not preview_path.is_relative_to(assets_root) or not preview_path.is_file():
        raise FileNotFoundError(f'Preview image not found for {item_id}: {preview_path}')

    qr_url = DEMO_URL if is_demo else f'{BOT_URL}?start=buy_{item_id}'
    total_frames = max(int(round(duration_sec * fps)), fps)
    fade_out_start = max(duration_sec - 0.45, 0.5)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f'naum_reel_{item_id}_') as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        bg_path = tmp_dir / 'bg.png'
        hud_path = tmp_dir / 'hud.png'
        _build_bg_canvas(preview_path).save(bg_path, format='PNG')
        _build_hud_overlay(item, qr_url=qr_url, is_demo=is_demo).save(hud_path, format='PNG')

        fd, tmp_mp4_name = tempfile.mkstemp(prefix=f'.{item_id}.', suffix='.tmp.mp4', dir=str(output_path.parent))
        os.close(fd)
        tmp_mp4 = Path(tmp_mp4_name)
        try:
            filter_complex = (
                f"[0:v]zoompan=z='min(zoom+0.0007,1.12)':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={total_frames}:s={REEL_SIZE[0]}x{REEL_SIZE[1]}:fps={fps}[bg];"
                f"[1:v]format=rgba[hud];"
                f"[bg][hud]overlay=0:0:shortest=1,"
                f"fade=t=in:st=0:d=0.4,"
                f"fade=t=out:st={fade_out_start:.2f}:d=0.45,"
                f"format=yuv420p[v]"
            )
            cmd = [
                'ffmpeg',
                '-y',
                '-i', str(bg_path),
                '-loop', '1',
                '-t', f'{duration_sec:.2f}',
                '-i', str(hud_path),
                '-filter_complex', filter_complex,
                '-map', '[v]',
                '-c:v', 'libx264',
                '-preset', 'medium',
                '-crf', '21',
                '-r', str(fps),
                '-t', f'{duration_sec:.2f}',
                '-f', 'mp4',
                '-movflags', '+faststart',
                '-an',
                str(tmp_mp4),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f'ffmpeg failed for {item_id}: {res.stderr[-400:]}')
            os.chmod(tmp_mp4, 0o644)
            os.replace(tmp_mp4, output_path)
            os.chmod(output_path, 0o644)
        except BaseException:
            tmp_mp4.unlink(missing_ok=True)
            raise

    return {
        'ok': True,
        'item_id': item_id,
        'is_demo': is_demo,
        'output_path': str(output_path),
        'bytes': output_path.stat().st_size,
        'resolution': f'{REEL_SIZE[0]}x{REEL_SIZE[1]}',
        'duration_sec': duration_sec,
        'qr_url': qr_url,
    }


def build_all_promo_reels(
    repo_root: Path,
    output_dir: Path = Path(DEFAULT_PROMO_DIR),
    item_ids: Iterable[str] | None = None,
    duration_sec: float = DEFAULT_DURATION_SEC,
    include_demo: bool = True,
) -> list[dict]:
    """Build vertical MP4 promo reels for all matching catalog previews (plus optional demo reel)."""
    catalog = json.loads((repo_root / 'catalog.json').read_text(encoding='utf-8'))
    items = list(catalog.get('items', []))
    selected_ids = set(item_ids) if item_ids else None

    results: list[dict] = []
    for item in items:
        pid = str(item.get('id') or '').strip()
        if not pid:
            continue
        if selected_ids is not None and pid not in selected_ids:
            continue
        preview_rel = str(item.get('image') or '')
        if not preview_rel or not (repo_root / preview_rel).is_file():
            continue
        out_mp4 = output_dir / f'{pid}_reel.mp4'
        results.append(
            build_promo_reel(
                repo_root=repo_root,
                item=item,
                output_path=out_mp4,
                duration_sec=duration_sec,
                is_demo=False,
            )
        )

    if include_demo or (selected_ids is not None and ('demo-ny' in selected_ids or 'demo_ny' in selected_ids)):
        demo_offer = dict(catalog.get('demo_offer') or {})
        demo_item = {
            'id': str(demo_offer.get('id') or 'demo-ny'),
            'category': 'newyear',
            'category_label': 'Бесплатное демо',
            'title': str(demo_offer.get('title') or 'Бесплатная демо-открытка «Киев в новогодних огнях»'),
            'description': str(demo_offer.get('description') or 'Ознакомительный ZIP-комплект с пометкой DEMO.'),
            'format': 'ZIP · 1200×800 · 0 Stars',
            'image': str(demo_offer.get('image') or 'assets/newyear/ny-kyiv-01/preview.webp'),
            'price_uah': 0,
            'price_xtr': 0,
        }
        if (repo_root / demo_item['image']).is_file():
            results.append(
                build_promo_reel(
                    repo_root=repo_root,
                    item=demo_item,
                    output_path=output_dir / 'demo_ny_reel.mp4',
                    duration_sec=duration_sec,
                    is_demo=True,
                )
            )
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description='Build 1080x1920 vertical MP4 promo reels for NAUM AI Creative Studio')
    ap.add_argument('--repo', default='/opt/ai-creative-showcase', help='Path to ai-creative-showcase repo')
    ap.add_argument('--output-dir', default=DEFAULT_PROMO_DIR, help='Output promo directory')
    ap.add_argument('--item-id', action='append', dest='item_ids', help='Specific catalog item ID (repeatable)')
    ap.add_argument('--duration', type=float, default=DEFAULT_DURATION_SEC, help='Reel duration in seconds')
    ap.add_argument('--no-demo', action='store_true', help='Skip generating demo_ny_reel.mp4')
    args = ap.parse_args()

    results = build_all_promo_reels(
        repo_root=Path(args.repo).resolve(),
        output_dir=Path(args.output_dir),
        item_ids=args.item_ids,
        duration_sec=args.duration,
        include_demo=not args.no_demo,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
