#!/usr/bin/env python3
"""Personalized Saint Nicholas / Ded Moroz / Santa greeting generator (audio, certificate, 9:16 video)."""
from __future__ import annotations

import asyncio
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import edge_tts
from PIL import Image, ImageDraw, ImageFont

KIDS_VIDEO_PRODUCT_ID = "kids-greeting-video"
KIDS_VIDEO_PRICE_UAH = 39
KIDS_VIDEO_PRICE_XTR = 45
KIDS_AUDIO_PRICE_UAH = 0
KIDS_AUDIO_PRICE_XTR = 0

VALID_PERSONAS = frozenset({"mykolay", "ded_moroz", "santa"})
VALID_LANGS = frozenset({"uk", "ru"})

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/montserrat/Montserrat-Medium.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
REGULAR_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/montserrat/Montserrat-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

_UK_CHARS_RE = re.compile(r"[іїєґІЇЄҐ]")
_RU_CHARS_RE = re.compile(r"[ыэёъЫЭЁЪ]")


@dataclass(frozen=True)
class GreetingRequest:
    """Normalized child greeting parameters."""

    child_name: str
    age: int | None = None
    city: str = ""
    good_deed: str = ""
    lang: str = "uk"
    persona: str = "mykolay"


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = FONT_CANDIDATES if bold else REGULAR_FONT_CANDIDATES
    for fp in candidates:
        if Path(fp).is_file():
            return ImageFont.truetype(fp, size=size)
    return ImageFont.load_default()


def normalize_lang(lang: str | None, text_hint: str = "", default: str = "uk") -> str:
    """Normalize language code to 'uk' or 'ru', auto-detecting from text if needed."""
    raw = (lang or "").strip().lower()
    if raw in ("uk", "ua", "ukr"):
        return "uk"
    if raw in ("ru", "rus"):
        return "ru"
    if _UK_CHARS_RE.search(text_hint):
        return "uk"
    if _RU_CHARS_RE.search(text_hint):
        return "ru"
    return default if default in VALID_LANGS else "uk"


def normalize_persona(persona: str | None, lang: str = "uk", default: str = "mykolay") -> str:
    """Normalize holiday persona to 'mykolay', 'ded_moroz', or 'santa'."""
    raw = (persona or "").strip().lower()
    if raw in ("mykolay", "nicholas", "st_nicholas", "миколай", "николай"):
        return "mykolay"
    if raw in ("ded_moroz", "moroz", "дід мороз", "дед мороз"):
        return "ded_moroz"
    if raw in ("santa", "санта"):
        return "santa"
    if default in VALID_PERSONAS:
        return default
    return "mykolay" if lang == "uk" else "ded_moroz"


def clean_child_name(raw: str, lang: str = "uk") -> str:
    """Sanitize child's name safely for typography and speech."""
    cleaned = re.sub(r"[^\w\s\-'.]", "", raw, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    fallback = "Маленький друже" if lang == "uk" else "Маленький друг"
    return cleaned[:30] or fallback


def clean_city(raw: str) -> str:
    """Sanitize city string."""
    cleaned = re.sub(r"[^\w\s\-'.,]", "", raw, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:30]


def clean_good_deed(raw: str) -> str:
    """Sanitize good-deed text."""
    cleaned = re.sub(r"[^\w\s\-'.,!?:;()]", "", raw, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120]


def parse_age(raw: str | int | None) -> int | None:
    """Parse child age (1..17) from integer or text like '6 лет' / '6 років'."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw if 1 <= raw <= 17 else None
    m = re.search(r"\b(\d{1,2})\b", str(raw))
    if not m:
        return None
    val = int(m.group(1))
    return val if 1 <= val <= 17 else None


def format_age_phrase(age: int | None, lang: str = "uk") -> str:
    """Format age with grammatically correct Ukrainian/Russian plural noun."""
    if age is None:
        return ""
    mod10 = age % 10
    mod100 = age % 100
    if lang == "uk":
        if mod10 == 1 and mod100 != 11:
            unit = "рік"
        elif 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
            unit = "роки"
        else:
            unit = "років"
    else:
        if mod10 == 1 and mod100 != 11:
            unit = "год"
        elif 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
            unit = "года"
        else:
            unit = "лет"
    return f"{age} {unit}"


def parse_greeting_args(raw_args: str, command: str = "/mykolay") -> GreetingRequest | None:
    """Parse free-form or delimited arguments for /mykolay and /santa.

    Supported formats:
      - Delimited: "Артемчик, 6, Київ, слухав маму" (comma, pipe, or semicolon)
      - Space-separated: "Артемчик 6 Київ слухав маму" or "Артемчик слухав маму"
    """
    text = (raw_args or "").strip()
    if not text:
        return None

    cmd_lower = (command or "/mykolay").strip().lower()
    default_lang = "ru" if cmd_lower == "/santa" and _RU_CHARS_RE.search(text) else "uk"
    if cmd_lower == "/santa" and not _UK_CHARS_RE.search(text):
        if re.search(r"\b(маму|папу|бабушке|дедушке|хорошо|учился|помогала|город|лет|год)\b", text, re.I):
            default_lang = "ru"
    lang = normalize_lang(None, text_hint=text, default=default_lang)

    # Optional explicit prefix: "ru: ..." or "uk: ..."
    lang_prefix = re.match(r"^(uk|ua|ru)\s*[:\-]\s*(.+)$", text, flags=re.I)
    if lang_prefix:
        lang = normalize_lang(lang_prefix.group(1))
        text = lang_prefix.group(2).strip()

    if cmd_lower == "/santa":
        persona = "ded_moroz" if (lang == "ru" and "сант" not in text.lower()) else "santa"
    else:
        persona = "mykolay"

    age: int | None = None
    city = ""
    good_deed = ""

    if any(sep in text for sep in (",", "|", ";")):
        parts = [p.strip() for p in re.split(r"[,|;]", text) if p.strip()]
        name_raw = parts[0] if parts else ""
        idx = 1
        if idx < len(parts) and parse_age(parts[idx]) is not None and re.fullmatch(
            r"\d{1,2}(\s*(років|роки|рік|лет|года|год))?", parts[idx], re.I
        ):
            age = parse_age(parts[idx])
            idx += 1
        if idx < len(parts):
            if len(parts) - idx >= 2:
                city = clean_city(parts[idx])
                good_deed = clean_good_deed(", ".join(parts[idx + 1 :]))
            else:
                tail = parts[idx]
                if len(tail.split()) == 1 and len(tail) <= 18:
                    city = clean_city(tail)
                else:
                    good_deed = clean_good_deed(tail)
    else:
        tokens = text.split()
        name_raw = tokens[0]
        rest = tokens[1:]
        if rest and re.fullmatch(r"\d{1,2}", rest[0]):
            age = parse_age(rest[0])
            rest = rest[1:]
            if rest and re.fullmatch(r"(років|роки|рік|лет|года|год)", rest[0], re.I):
                rest = rest[1:]
        if rest and len(rest) >= 2 and len(rest[0]) <= 18 and rest[0][0].isupper():
            city = clean_city(rest[0])
            rest = rest[1:]
        if rest:
            good_deed = clean_good_deed(" ".join(rest))

    return GreetingRequest(
        child_name=clean_child_name(name_raw, lang=lang),
        age=age,
        city=city,
        good_deed=good_deed,
        lang=lang,
        persona=persona,
    )


def _persona_labels(persona: str, lang: str) -> dict[str, str]:
    p = normalize_persona(persona, lang=lang)
    if lang == "uk":
        mapping = {
            "mykolay": {
                "speaker": "Святий Миколай",
                "office": "Канцелярія Святого Миколая",
                "cert_title": "ГРАМОТА ЧЕМНОЇ ДИТИНИ",
                "video_title": "Святий Миколай вітає:",
                "sign": "Святий Миколай",
            },
            "ded_moroz": {
                "speaker": "Дід Мороз",
                "office": "Канцелярія Діда Мороза",
                "cert_title": "ГРАМОТА ЧЕМНОЇ ДИТИНИ",
                "video_title": "Дід Мороз вітає:",
                "sign": "Дід Мороз",
            },
            "santa": {
                "speaker": "Санта-Клаус",
                "office": "Канцелярія Санта-Клауса",
                "cert_title": "ГРАМОТА ЧЕМНОЇ ДИТИНИ",
                "video_title": "Санта-Клаус вітає:",
                "sign": "Санта-Клаус",
            },
        }
    else:
        mapping = {
            "mykolay": {
                "speaker": "Святой Николай",
                "office": "Канцелярия Святого Николая",
                "cert_title": "ГРАМОТА ВЕЖЛИВОГО РЕБЁНКА",
                "video_title": "Святой Николай поздравляет:",
                "sign": "Святой Николай",
            },
            "ded_moroz": {
                "speaker": "Дедушка Мороз",
                "office": "Канцелярия Деда Мороза",
                "cert_title": "ГРАМОТА ВЕЖЛИВОГО РЕБЁНКА",
                "video_title": "Дед Мороз поздравляет:",
                "sign": "Дед Мороз",
            },
            "santa": {
                "speaker": "Санта-Клаус",
                "office": "Канцелярия Санта-Клауса",
                "cert_title": "ГРАМОТА ВЕЖЛИВОГО РЕБЁНКА",
                "video_title": "Санта-Клаус поздравляет:",
                "sign": "Санта-Клаус",
            },
        }
    return mapping[p]


def build_greeting_text(
    child_name: str,
    age: int | None = None,
    city: str = "",
    good_deed: str = "",
    lang: str = "uk",
    persona: str = "mykolay",
) -> str:
    """Build warm, personalized speech text for Nicholas / Ded Moroz / Santa."""
    l_norm = normalize_lang(lang, text_hint=f"{child_name} {city} {good_deed}")
    p_norm = normalize_persona(persona, lang=l_norm)
    labels = _persona_labels(p_norm, l_norm)
    name = clean_child_name(child_name, lang=l_norm)
    city_c = clean_city(city)
    deed_c = clean_good_deed(good_deed)
    age_val = parse_age(age)
    age_str = format_age_phrase(age_val, lang=l_norm)

    if l_norm == "uk":
        details = []
        if age_str:
            details.append(f"знаю, що тобі вже {age_str}")
        if city_c:
            details.append(f"і я зазирнув у місто {city_c}")
        detail_sentence = (" Я " + " ".join(details) + ".") if details else ""
        deed_sentence = f" У моїй Золотій Книзі записано, як ти {deed_c}." if deed_c else ""
        return (
            f"Привіт, любий {name}! Це {labels['speaker']}!"
            f"{detail_sentence}{deed_sentence} "
            f"Ти велика розумничка! Радуй батьків, будь здоровим, щирим та вір у зимові дива! "
            f"З прийдешнім святом тебе!"
        )

    details = []
    if age_str:
        details.append(f"знаю, что тебе уже {age_str}")
    if city_c:
        details.append(f"и я заглянул в город {city_c}")
    detail_sentence = (" Я " + " ".join(details) + ".") if details else ""
    deed_sentence = f" В моей Волшебной Книге записано, как ты {deed_c}." if deed_c else ""
    return (
        f"Здравствуй, дорогой {name}! Это {labels['speaker']}!"
        f"{detail_sentence}{deed_sentence} "
        f"Ты большая умница! Радуй близких, будь здоровым, смелым и верь в чудеса! "
        f"С наступающим праздником тебя!"
    )


def _probe_audio_duration(audio_path: Path, default: float = 10.0) -> float:
    """Return audio duration in seconds using ffprobe."""
    res = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        try:
            dur = float(res.stdout.strip())
            if math.isfinite(dur) and dur > 0.2:
                return dur
        except ValueError:
            pass
    return default


def _mix_voice_with_holiday_bells(voice_path: Path, out_mp3: Path) -> Path:
    """Synthesize festive celesta bells + warm music pad at 0₴ and mix with speech."""
    voice_dur = _probe_audio_duration(voice_path, default=8.0)
    total_dur = max(voice_dur + 2.2, 4.0)
    fade_out_start = max(total_dur - 1.1, 0.5)

    bell_expr = (
        r"0.18*sin(2*PI*523.25*t)*exp(-4.5*mod(t\,0.8))"
        r"+0.14*sin(2*PI*659.25*t)*exp(-4.5*mod(t+0.2\,0.8))"
        r"+0.12*sin(2*PI*783.99*t)*exp(-4.5*mod(t+0.4\,0.8))"
        r"+0.10*sin(2*PI*1046.5*t)*exp(-5.5*mod(t+0.6\,0.8))"
        r"+0.04*sin(2*PI*261.63*t)+0.03*sin(2*PI*392.00*t)"
    )

    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    filter_complex = (
        "[0:a]aresample=44100,aformat=channel_layouts=stereo,"
        "adelay=900|900,apad=pad_dur=1.3,volume=1.15[v];"
        "[1:a]aformat=channel_layouts=stereo,"
        f"volume=0.22,afade=t=in:st=0:d=0.35,afade=t=out:st={fade_out_start:.2f}:d=1.0[m];"
        "[v][m]amix=inputs=2:duration=first:dropout_transition=0.4:normalize=0,"
        "alimiter=limit=0.95[out]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(voice_path),
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc='{bell_expr}':s=44100:d={total_dur:.2f}",
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(out_mp3),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg audio mix failed: {res.stderr[-400:]}")
    return out_mp3


async def _synthesize_edge_tts(text: str, voice: str, out_mp3: Path) -> None:
    comm = edge_tts.Communicate(text, voice, pitch="-8Hz", rate="-8%")
    await comm.save(str(out_mp3))


async def generate_nicholas_audio(
    child_name: str,
    good_deed: str = "",
    out_mp3: Path = Path("greeting.mp3"),
    lang: str = "uk",
    *,
    age: int | None = None,
    city: str = "",
    persona: str = "mykolay",
    tts_fn: Callable[[str, str, Path], object] | None = None,
) -> Path:
    """Generate personalized voice greeting mixed with festive bells and holiday music (0₴)."""
    l_norm = normalize_lang(lang, text_hint=f"{child_name} {city} {good_deed}")
    p_norm = normalize_persona(persona, lang=l_norm)
    voice = "uk-UA-OstapNeural" if l_norm == "uk" else "ru-RU-DmitryNeural"
    text = build_greeting_text(
        child_name=child_name,
        age=age,
        city=city,
        good_deed=good_deed,
        lang=l_norm,
        persona=p_norm,
    )

    out_mp3 = Path(out_mp3)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
        raw_voice = Path(tf.name)
    try:
        if tts_fn is not None:
            maybe_coro = tts_fn(text, voice, raw_voice)
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
        else:
            await _synthesize_edge_tts(text, voice, raw_voice)
        _mix_voice_with_holiday_bells(raw_voice, out_mp3)
    finally:
        raw_voice.unlink(missing_ok=True)
    return out_mp3


def _draw_star(draw: ImageDraw.ImageDraw, cx: int, cy: int, outer_r: int, inner_r: int, fill: tuple[int, int, int]) -> None:
    pts = []
    for i in range(10):
        angle = math.radians(-90 + i * 36)
        r = outer_r if i % 2 == 0 else inner_r
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    draw.polygon(pts, fill=fill)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        candidate = f"{cur} {w}"
        bb = draw.textbbox((0, 0), candidate, font=font)
        if (bb[2] - bb[0]) <= max_width:
            cur = candidate
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _draw_centered(draw: ImageDraw.ImageDraw, width: int, y: int, text: str, font: ImageFont.ImageFont, fill: tuple[int, int, int]) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    tw = bb[2] - bb[0]
    th = bb[3] - bb[1]
    draw.text(((width - tw) / 2, y), text, font=font, fill=fill)
    return y + th


def render_certificate(
    repo_root: Path,
    child_name: str,
    city: str = "",
    out_path: Path = Path("certificate.jpg"),
    *,
    age: int | None = None,
    good_deed: str = "",
    lang: str = "uk",
    persona: str = "mykolay",
) -> Path:
    """Render high-res 1200x800 gold diploma / certificate in Ukrainian or Russian."""
    l_norm = normalize_lang(lang, text_hint=f"{child_name} {city} {good_deed}")
    p_norm = normalize_persona(persona, lang=l_norm)
    labels = _persona_labels(p_norm, l_norm)

    w, h = 1200, 800
    im = Image.new("RGB", (w, h), (12, 20, 44))
    draw = ImageDraw.Draw(im)

    for step in range(6):
        inset = step * 18
        shade = (12 + step * 2, 20 + step * 3, 44 + step * 5)
        draw.rectangle([(inset, inset), (w - inset, h - inset)], fill=shade)

    gold = (218, 178, 64)
    light_gold = (255, 232, 155)
    draw.rectangle([(36, 36), (w - 36, h - 36)], outline=gold, width=6)
    draw.rectangle([(52, 52), (w - 52, h - 52)], outline=light_gold, width=2)
    for cx, cy in ((78, 78), (w - 78, 78), (78, h - 78), (w - 78, h - 78)):
        _draw_star(draw, cx, cy, 16, 7, gold)

    title_font = _load_font(44)
    name_font = _load_font(54)
    body_font = _load_font(25)
    small_font = _load_font(20, bold=False)
    sign_font = _load_font(26)

    y = _draw_centered(draw, w, 92, f"★  {labels['cert_title']}  ★", title_font, light_gold)

    sub = (
        f"{labels['office']} підтверджує, що"
        if l_norm == "uk"
        else f"{labels['office']} подтверждает, что"
    )
    y = _draw_centered(draw, w, y + 36, sub, body_font, (220, 232, 255))

    name = clean_child_name(child_name, lang=l_norm)
    age_str = format_age_phrase(parse_age(age), lang=l_norm)
    name_line = f"{name}, {age_str}" if age_str else name
    y = _draw_centered(draw, w, y + 26, name_line, name_font, (255, 242, 188))

    city_clean = clean_city(city)
    deed_clean = clean_good_deed(good_deed)
    if l_norm == "uk":
        loc = f"із м. {city_clean}" if city_clean else "з України"
        praise = (
            f"проявила доброту та старанність: {deed_clean}."
            if deed_clean
            else "проявляє доброту, старанність та щирість."
        )
        body_text = f"Дитина {loc} {praise}"
        ledger_line = "✦ Занесено до Золотої Книги Добрих Справ на 2026–2027 рік ✦"
        seal_label = "Канцелярія Чудес"
    else:
        loc = f"из г. {city_clean}" if city_clean else ""
        loc_part = f" {loc}" if loc else ""
        praise = (
            f"проявляет доброту и старание: {deed_clean}."
            if deed_clean
            else "проявляет доброту, послушание и искренность."
        )
        body_text = f"Ребёнок{loc_part} {praise}"
        ledger_line = "✦ Занесено в Золотую Книгу Добрых Дел на 2026–2027 год ✦"
        seal_label = "Канцелярия Чудес"

    cur_y = y + 34
    for line in _wrap_text(draw, body_text, body_font, max_width=940)[:2]:
        cur_y = _draw_centered(draw, w, cur_y, line, body_font, (232, 240, 255)) + 10

    _draw_centered(draw, w, max(cur_y + 18, 490), ledger_line, body_font, light_gold)

    seal_cx, seal_cy = w // 2, 650
    draw.ellipse([(seal_cx - 48, seal_cy - 48), (seal_cx + 48, seal_cy + 48)], fill=(175, 130, 32), outline=light_gold, width=3)
    _draw_star(draw, seal_cx, seal_cy, 24, 10, (255, 244, 195))

    draw.text((110, 636), f"✦ {seal_label}", font=sign_font, fill=(205, 220, 248))
    sign_txt = f"{labels['sign']}  ★"
    sb = draw.textbbox((0, 0), sign_txt, font=sign_font)
    draw.text((w - 110 - (sb[2] - sb[0]), 636), sign_txt, font=sign_font, fill=light_gold)

    _draw_centered(draw, w, 725, "@NaumCreativeStudioBot", small_font, (150, 170, 210))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path, format="JPEG", quality=92)
    return out_path


def _draw_storybook_emblem(draw: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
    """Draw a glowing open Golden Book of Tales illustration inside the 9:16 canvas."""
    for radius, col in ((270, (22, 34, 72)), (220, (30, 46, 92)), (175, (42, 62, 115))):
        draw.ellipse([(cx - radius, cy - radius), (cx + radius, cy + radius)], fill=col)

    draw.rounded_rectangle([(cx - 210, cy - 125), (cx + 210, cy + 145)], radius=18, fill=(135, 92, 24), outline=(245, 205, 95), width=5)
    draw.rounded_rectangle([(cx - 190, cy - 110), (cx - 6, cy + 125)], radius=10, fill=(252, 244, 220), outline=(212, 175, 55), width=3)
    draw.rounded_rectangle([(cx + 6, cy - 110), (cx + 190, cy + 125)], radius=10, fill=(248, 238, 208), outline=(212, 175, 55), width=3)
    for line_i in range(5):
        ly = cy - 72 + line_i * 34
        draw.line([(cx - 165, ly), (cx - 32, ly)], fill=(185, 150, 80), width=4)
        draw.line([(cx + 32, ly), (cx + 165, ly)], fill=(185, 150, 80), width=4)
    _draw_star(draw, cx, cy - 155, 36, 15, (255, 228, 130))


def _build_video_canvases(
    repo_root: Path,
    child_name: str,
    age: int | None,
    city: str,
    good_deed: str,
    lang: str,
    persona: str,
    child_photo_path: Path | None,
) -> tuple[Image.Image, Image.Image]:
    """Build (1.5x background canvas with book_frame + photo/book, 1080x1920 RGBA HUD overlay)."""
    w, h = 1080, 1920
    labels = _persona_labels(persona, lang)
    name = clean_child_name(child_name, lang=lang)
    age_str = format_age_phrase(parse_age(age), lang=lang)
    city_c = clean_city(city)
    deed_c = clean_good_deed(good_deed)

    book_frame_path = Path(repo_root) / "assets" / "kids" / "book_frame.jpg"
    if book_frame_path.is_file():
        with Image.open(book_frame_path) as bf:
            bg_base = bf.convert("RGB").resize((w, h), Image.Resampling.LANCZOS)
    else:
        bg_base = Image.new("RGB", (w, h), (10, 16, 36))

    bg_draw = ImageDraw.Draw(bg_base)
    _draw_storybook_emblem(bg_draw, w // 2, 920)

    if child_photo_path and Path(child_photo_path).is_file():
        try:
            with Image.open(child_photo_path) as p:
                photo = p.convert("RGB")
                photo.thumbnail((540, 540), Image.Resampling.LANCZOS)
                pw, ph = photo.size
                px = (w - pw) // 2
                py = 920 - ph // 2
                bg_base.paste(photo, (px, py))
                bg_draw.rectangle([(px - 5, py - 5), (px + pw + 5, py + ph + 5)], outline=(245, 205, 95), width=5)
        except Exception:
            pass

    bg_15x = bg_base.resize((1620, 2880), Image.Resampling.LANCZOS)

    hud = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hud)
    hd.rectangle([(44, 124), (w - 44, h - 124)], outline=(218, 178, 64, 240), width=6)
    hd.rectangle([(60, 140), (w - 60, h - 140)], outline=(255, 232, 155, 200), width=2)

    title_font = _load_font(48)
    name_font = _load_font(68)
    meta_font = _load_font(34)
    sub_font = _load_font(30)
    brand_font = _load_font(24, bold=False)

    t = labels["video_title"]
    tb = hd.textbbox((0, 0), t, font=title_font)
    hd.text(((w - (tb[2] - tb[0])) / 2, 230), t, font=title_font, fill=(255, 230, 150, 255))

    nb = hd.textbbox((0, 0), name, font=name_font)
    hd.text(((w - (nb[2] - nb[0])) / 2, 315), name, font=name_font, fill=(255, 246, 205, 255))

    loc_label = f"м. {city_c}" if (city_c and lang == "uk") else (f"г. {city_c}" if city_c else "")
    meta_parts = [p for p in (age_str, loc_label) if p]
    if meta_parts:
        meta_line = " • ".join(meta_parts)
        mb = hd.textbbox((0, 0), meta_line, font=meta_font)
        hd.text(((w - (mb[2] - mb[0])) / 2, 415), meta_line, font=meta_font, fill=(215, 232, 255, 245))

    if deed_c:
        deed_y = 1320
        for line in _wrap_text(hd, f"★ {deed_c}", sub_font, max_width=860)[:2]:
            db = hd.textbbox((0, 0), line, font=sub_font)
            hd.text(((w - (db[2] - db[0])) / 2, deed_y), line, font=sub_font, fill=(255, 236, 175, 245))
            deed_y += 44

    footer = (
        "✦ Вір у дива та слухай батьків! ✦"
        if lang == "uk"
        else "✦ Верь в чудеса и будь счастлив! ✦"
    )
    fb = hd.textbbox((0, 0), footer, font=sub_font)
    hd.text(((w - (fb[2] - fb[0])) / 2, 1520), footer, font=sub_font, fill=(225, 238, 255, 255))

    brand = "@NaumCreativeStudioBot"
    bb = hd.textbbox((0, 0), brand, font=brand_font)
    hd.text(((w - (bb[2] - bb[0])) / 2, 1690), brand, font=brand_font, fill=(170, 190, 225, 220))

    return bg_15x, hud


def render_video_greeting(
    repo_root: Path,
    child_name: str,
    audio_path: Path,
    out_video_path: Path,
    child_photo_path: Path | None = None,
    *,
    age: int | None = None,
    city: str = "",
    good_deed: str = "",
    lang: str = "uk",
    persona: str = "mykolay",
    fps: int = 25,
) -> Path:
    """Compose smooth 9:16 (1080x1920) HD video greeting with book_frame + zoompan + audio."""
    l_norm = normalize_lang(lang, text_hint=f"{child_name} {city} {good_deed}")
    p_norm = normalize_persona(persona, lang=l_norm)

    bg_15x, hud = _build_video_canvases(
        repo_root=Path(repo_root),
        child_name=child_name,
        age=age,
        city=city,
        good_deed=good_deed,
        lang=l_norm,
        persona=p_norm,
        child_photo_path=child_photo_path,
    )

    audio_path = Path(audio_path)
    out_video_path = Path(out_video_path)
    out_video_path.parent.mkdir(parents=True, exist_ok=True)

    duration = max(_probe_audio_duration(audio_path, default=6.0), 2.0)
    total_frames = max(int(round(duration * fps)), fps)

    with tempfile.TemporaryDirectory(prefix="naum_kids_vid_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        bg_path = tmp_dir / "bg.jpg"
        hud_path = tmp_dir / "hud.png"
        bg_15x.save(bg_path, format="JPEG", quality=92)
        hud.save(hud_path, format="PNG")

        filter_complex = (
            f"[0:v]zoompan=z='min(zoom+0.0005,1.10)':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={total_frames}:s=1080x1920:fps={fps}[bg];"
            f"[1:v]format=rgba[hud];"
            f"[bg][hud]overlay=0:0:shortest=1,format=yuv420p[v]"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(bg_path),
            "-loop",
            "1",
            "-t",
            f"{duration:.2f}",
            "-i",
            str(hud_path),
            "-i",
            str(audio_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-af",
            "aecho=0.8:0.85:30:0.22,bass=g=3:f=120",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-t",
            f"{duration:.2f}",
            "-movflags",
            "+faststart",
            str(out_video_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg video render failed: {res.stderr[-400:]}")
    return out_video_path


async def generate_kids_greeting_bundle(
    repo_root: Path,
    child_name: str,
    age: int | None = None,
    city: str = "",
    good_deed: str = "",
    out_dir: Path = Path("out_kids"),
    lang: str = "uk",
    persona: str = "mykolay",
    child_photo_path: Path | None = None,
    include_video: bool = True,
    tts_fn: Callable[[str, str, Path], object] | None = None,
) -> dict[str, object]:
    """Generate full greeting bundle: free audio+certificate, and optional 9:16 HD video."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    l_norm = normalize_lang(lang, text_hint=f"{child_name} {city} {good_deed}")
    p_norm = normalize_persona(persona, lang=l_norm)

    cert_path = out_dir / "certificate.jpg"
    audio_path = out_dir / "greeting.mp3"
    video_path = out_dir / "greeting_9_16.mp4"

    render_certificate(
        repo_root=Path(repo_root),
        child_name=child_name,
        city=city,
        out_path=cert_path,
        age=age,
        good_deed=good_deed,
        lang=l_norm,
        persona=p_norm,
    )
    await generate_nicholas_audio(
        child_name=child_name,
        good_deed=good_deed,
        out_mp3=audio_path,
        lang=l_norm,
        age=age,
        city=city,
        persona=p_norm,
        tts_fn=tts_fn,
    )
    if include_video:
        render_video_greeting(
            repo_root=Path(repo_root),
            child_name=child_name,
            audio_path=audio_path,
            out_video_path=video_path,
            child_photo_path=child_photo_path,
            age=age,
            city=city,
            good_deed=good_deed,
            lang=l_norm,
            persona=p_norm,
        )

    return {
        "child_name": clean_child_name(child_name, lang=l_norm),
        "age": parse_age(age),
        "city": clean_city(city),
        "good_deed": clean_good_deed(good_deed),
        "lang": l_norm,
        "persona": p_norm,
        "certificate_path": cert_path,
        "audio_path": audio_path,
        "video_path": video_path if include_video else None,
        "free_bundle_price_uah": KIDS_AUDIO_PRICE_UAH,
        "video_price_uah": KIDS_VIDEO_PRICE_UAH,
        "video_price_xtr": KIDS_VIDEO_PRICE_XTR,
    }
