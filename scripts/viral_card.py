#!/usr/bin/env python3
"""Free viral personalized postcard generator (/card <Name>) on Pillow."""
from __future__ import annotations

import os
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/montserrat/Montserrat-Medium.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for fp in FONT_CANDIDATES:
        if Path(fp).is_file():
            return ImageFont.truetype(fp, size=size)
    return ImageFont.load_default()


def sanitize_recipient_name(raw_name: str) -> str:
    cleaned = re.sub(r"[^\w\s\-'.!]", "", raw_name, flags=re.UNICODE).strip()
    return cleaned[:28]


def generate_personalized_card(repo_root: Path, recipient_name: str, out_path: Path) -> Path:
    """Render a personalized Kyiv New Year postcard from preview.webp with watermark."""
    name = sanitize_recipient_name(recipient_name)
    if not name:
        raise ValueError("Укажите имя, например: /card Марина")

    src = repo_root / "assets" / "newyear" / "ny-kyiv-01" / "preview.webp"
    if not src.is_file():
        raise FileNotFoundError(f"Preview not found: {src}")

    with Image.open(src) as im:
        img = im.convert("RGBA")

    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Soft dark-blue top banner in the open night sky
    banner_h = max(int(h * 0.20), 110)
    draw.rectangle([(0, 0), (w, banner_h)], fill=(8, 18, 42, 145))

    title_font = _load_font(max(int(w * 0.042), 26))
    sub_font = _load_font(max(int(w * 0.022), 16))
    wm_font = _load_font(max(int(w * 0.018), 14))

    headline = f"З Новим роком, {name}!"
    subline = "Тепла, світла та миру у 2027 році ✨"

    tb = draw.textbbox((0, 0), headline, font=title_font)
    tw = tb[2] - tb[0]
    draw.text(((w - tw) / 2, int(banner_h * 0.18)), headline, font=title_font, fill=(255, 232, 165, 255))

    sb = draw.textbbox((0, 0), subline, font=sub_font)
    sw = sb[2] - sb[0]
    draw.text(((w - sw) / 2, int(banner_h * 0.62)), subline, font=sub_font, fill=(235, 242, 255, 240))

    # Small bottom watermark pointing to the bot
    wm = "Створено безкоштовно в @NaumCreativeStudioBot"
    wb = draw.textbbox((0, 0), wm, font=wm_font)
    ww = wb[2] - wb[0]
    draw.rectangle([(w - ww - 28, h - 34), (w, h)], fill=(8, 18, 42, 165))
    draw.text((w - ww - 14, h - 27), wm, font=wm_font, fill=(255, 255, 255, 230))

    out = Image.alpha_composite(img, overlay).convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, format="JPEG", quality=90)
    return out_path
