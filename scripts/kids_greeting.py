#!/usr/bin/env python3
"""Magical personalized Saint Nicholas / Santa video & audio generator for kids."""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
import edge_tts
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


def clean_child_name(raw: str) -> str:
    cleaned = re.sub(r"[^\w\s\-'.]", "", raw, flags=re.UNICODE).strip()
    return cleaned[:30] or "Маленький друже"


async def generate_nicholas_audio(
    child_name: str,
    good_deed: str,
    out_mp3: Path,
    lang: str = "uk",
) -> Path:
    """Generate authentic warm grandfatherly speech for Saint Nicholas."""
    name = clean_child_name(child_name)
    deed_text = f" Я бачив, як ти {good_deed}." if good_deed else ""

    if lang == "uk":
        voice = "uk-UA-OstapNeural"
        text = (
            f"Привіт, дорогий {name}! Святий Миколай передає тобі найщиріші чарівні вітання!{deed_text} "
            f"Ти велика розумничка! Продовжуй радувати батьків, будь здоровим, щасливим та вір у дива! "
            f"З прийдешнім святом тебе!"
        )
    else:
        voice = "ru-RU-DmitryNeural"
        text = (
            f"Здравствуй, дорогой {name}! Дедушка Мороз передаёт тебе самые тёплые поздравления!{deed_text} "
            f"Ты большая умница! Радуй родителей, будь здоровым и верь в чудеса! "
            f"С Новым годом тебя!"
        )

    comm = edge_tts.Communicate(text, voice, pitch="-8Hz", rate="-10%")
    await comm.save(str(out_mp3))
    return out_mp3


def render_certificate(
    repo_root: Path,
    child_name: str,
    city: str,
    out_path: Path,
) -> Path:
    """Render high-res gold diploma / certificate from Saint Nicholas."""
    w, h = 1200, 800
    im = Image.new("RGB", (w, h), (14, 22, 45))
    draw = ImageDraw.Draw(im)

    # Gold borders
    draw.rectangle([(40, 40), (w - 40, h - 40)], outline=(212, 175, 55), width=6)
    draw.rectangle([(55, 55), (w - 55, h - 55)], outline=(255, 235, 160), width=2)

    title_font = _load_font(44)
    name_font = _load_font(52)
    body_font = _load_font(24)
    sign_font = _load_font(28)

    t = "ГРАМОТА ЧЕМНОЇ ДИТИНИ"
    tb = draw.textbbox((0, 0), t, font=title_font)
    draw.text(((w - (tb[2] - tb[0])) / 2, 110), t, font=title_font, fill=(255, 225, 140))

    sub = "Канцелярія Святого Миколая підтверджує, що"
    sb = draw.textbbox((0, 0), sub, font=body_font)
    draw.text(((w - (sb[2] - sb[0])) / 2, 200), sub, font=body_font, fill=(220, 230, 255))

    name = clean_child_name(child_name)
    nb = draw.textbbox((0, 0), name, font=name_font)
    draw.text(((w - (nb[2] - nb[0])) / 2, 270), name, font=name_font, fill=(255, 240, 180))

    city_clean = re.sub(r"[^\w\s\-',.]", "", city, flags=re.UNICODE).strip()[:30]
    loc = f"м. {city_clean}" if city_clean else "Україна"
    b1 = f"із міста {loc} проявляє доброту, старанність та щирість."
    b1b = draw.textbbox((0, 0), b1, font=body_font)
    draw.text(((w - (b1b[2] - b1b[0])) / 2, 380), b1, font=body_font, fill=(230, 240, 255))

    b2 = "Занесено до Золотої Книги Добрих Справ на 2026–2027 рік! ✨"
    b2b = draw.textbbox((0, 0), b2, font=body_font)
    draw.text(((w - (b2b[2] - b2b[0])) / 2, 430), b2, font=body_font, fill=(255, 225, 140))

    # Bottom Seal & signature
    draw.text((120, 640), "✨ Канцелярія Чудес", font=sign_font, fill=(200, 215, 245))
    draw.text((w - 420, 640), "Святий Миколай ✍️", font=sign_font, fill=(255, 225, 140))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path, format="JPEG", quality=92)
    return out_path


def render_video_greeting(
    repo_root: Path,
    child_name: str,
    audio_path: Path,
    out_video_path: Path,
    child_photo_path: Path | None = None,
) -> Path:
    """Compose smooth vertical 9:16 (1080x1920) video greeting with ffmpeg."""
    w, h = 1080, 1920
    im = Image.new("RGB", (w, h), (10, 16, 36))
    draw = ImageDraw.Draw(im)

    # Decorative frame
    draw.rectangle([(40, 120), (w - 40, h - 120)], outline=(212, 175, 55), width=6)
    draw.rectangle([(55, 135), (w - 55, h - 135)], outline=(255, 235, 160), width=2)

    title_font = _load_font(52)
    name_font = _load_font(68)
    sub_font = _load_font(32)

    t = "Святий Миколай вітає:"
    tb = draw.textbbox((0, 0), t, font=title_font)
    draw.text(((w - (tb[2] - tb[0])) / 2, 280), t, font=title_font, fill=(255, 230, 150))

    name = clean_child_name(child_name)
    nb = draw.textbbox((0, 0), name, font=name_font)
    draw.text(((w - (nb[2] - nb[0])) / 2, 380), name, font=name_font, fill=(255, 245, 200))

    # Center photo or golden book emblem
    if child_photo_path and child_photo_path.is_file():
        try:
            with Image.open(child_photo_path) as p:
                photo = p.convert("RGB")
                photo.thumbnail((680, 680))
                pw, ph = photo.size
                im.paste(photo, ((w - pw) // 2, 540))
                draw.rectangle([((w - pw) // 2 - 4, 536), ((w + pw) // 2 + 4, 544 + ph)], outline=(212, 175, 55), width=4)
        except Exception:
            pass

    footer = "✨ Вір у дива та будь слухняним! ✨"
    fb = draw.textbbox((0, 0), footer, font=sub_font)
    draw.text(((w - (fb[2] - fb[0])) / 2, 1420), footer, font=sub_font, fill=(225, 238, 255))

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tmp_frame = Path(tf.name)
    im.save(tmp_frame, format="JPEG", quality=90)

    # Use ffmpeg zoompan + atmospheric audio processing (subtle reverb + warm bass)
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(tmp_frame),
        "-i", str(audio_path),
        "-vf", "zoompan=z='min(zoom+0.0006,1.08)':d=700:s=1080x1920:fps=25",
        "-af", "aecho=0.8:0.88:35:0.3,bass=g=4:f=120",
        "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p", "-shortest",
        str(out_video_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    tmp_frame.unlink(missing_ok=True)
    return out_video_path
