#!/usr/bin/env python3
"""Clean-room NeonClipping-inspired audio-first 9:16 video clipping engine (0 UAH).

All code is original clean-room Python (stdlib + ffmpeg/yt-dlp + optional local Ollama).
No third-party AGPL source code is copied or embedded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from video_clipper import (
    ALLOWED_VIDEO_EXTENSIONS,
    MAX_DOWNLOAD_BYTES,
    REEL_FPS,
    REEL_HEIGHT,
    REEL_WIDTH,
    SpeechSegment,
    WordTimestamp,
    _distribute_words,
    _escape_ass_text,
    _escape_ffmpeg_filter_path,
    _format_ass_time,
    _pick_ass_font_name,
    _transcript_dicts_to_segments,
    analyze_wav_energy,
    build_speech_segments_from_energy,
    detect_scene_changes,
    detect_silence_intervals,
    download_video_from_url,
    extract_audio_wav,
    is_http_url,
    parse_subtitle_file,
    probe_media,
)

ALLOWED_AUDIO_EXTENSIONS = frozenset({'.m4a', '.mp3', '.opus', '.ogg', '.wav', '.webm', '.aac', '.flac'})
CAPTION_EXTENSIONS = ('.json3', '.vtt', '.srt')

DEFAULT_MIN_DURATION = 15.0
DEFAULT_MAX_DURATION = 60.0
DEFAULT_BASELINE_SEC = 120.0
MIN_FEEDBACK_SAMPLES = 8

DEFAULT_SIGNAL_WEIGHTS: dict[str, float] = {
    'jump': 0.30,
    'voice_2_4k': 0.25,
    'contrast': 0.15,
    'keyword': 0.15,
    'speech_ratio': 0.15,
}

# Caption styles: (PrimaryColour, SecondaryColour, OutlineColour, BackColour, Fontsize, Outline, Shadow, BorderStyle)
CAPTION_STYLES: dict[str, dict[str, Any]] = {
    'neon': {
        'primary': '&H006EFF00',
        'secondary': '&H00FFFF00',
        'outline_col': '&H00120820',
        'back_col': '&H90080814',
        'fontsize': 66,
        'outline': 5,
        'shadow': 2,
        'border_style': 1,
        'margin_v': 300,
    },
    'impact': {
        'primary': '&H0000D7FF',
        'secondary': '&H00FFFFFF',
        'outline_col': '&H00000000',
        'back_col': '&H88000000',
        'fontsize': 70,
        'outline': 6,
        'shadow': 3,
        'border_style': 1,
        'margin_v': 290,
    },
    'clean': {
        'primary': '&H00FFFFFF',
        'secondary': '&H00D8D8D8',
        'outline_col': '&H00181818',
        'back_col': '&H60000000',
        'fontsize': 56,
        'outline': 3,
        'shadow': 1,
        'border_style': 1,
        'margin_v': 270,
    },
    'hormozi': {
        'primary': '&H0000FF88',
        'secondary': '&H0000D7FF',
        'outline_col': '&H00000000',
        'back_col': '&HAA000000',
        'fontsize': 72,
        'outline': 7,
        'shadow': 3,
        'border_style': 1,
        'margin_v': 320,
    },
    'bold_box': {
        'primary': '&H00FFFFFF',
        'secondary': '&H0000E5FF',
        'outline_col': '&H00101018',
        'back_col': '&HCC101018',
        'fontsize': 60,
        'outline': 12,
        'shadow': 0,
        'border_style': 3,
        'margin_v': 295,
    },
}

FRAMING_MODES = ('blur_pad', 'center_crop', 'split_cam')

HOOK_TRIGGER_TERMS = (
    'ого', 'шок', 'жесть', 'вау', 'дивись', 'дивіться', 'слухай', 'слушай',
    'неможливо', 'невероятно', 'секрет', 'лайфхак', 'почему', 'чому', 'реально',
    'срочно', 'терміново', 'капец', 'офигеть', 'увага', 'внимание',
    'oh my god', 'omg', 'no way', 'wait what', 'watch this', 'crazy', 'insane',
    'secret', 'unbelievable', 'why', 'look at', 'listen',
)


@dataclass(frozen=True)
class NeonCandidate:
    """Scored highlight window with individual acoustic/transcript features and copy."""

    start: float
    end: float
    score: float
    jump_score: float
    voice_2_4k_score: float
    contrast_score: float
    keyword_score: float
    speech_ratio: float
    llm_score: float | None = None
    title: str = ''
    hook: str = ''
    clip_id: str = ''


def is_local_ollama_url(url: str) -> bool:
    """Allow only loopback URLs for local zero-cost Ollama inference."""
    parsed = urllib.parse.urlparse((url or '').strip())
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return False
    host = (parsed.hostname or '').strip().lower()
    return host in ('127.0.0.1', 'localhost', '::1')


def probe_media_duration(media_path: Path) -> float:
    """Return duration in seconds for any audio or video file using ffprobe."""
    if not media_path.is_file():
        raise FileNotFoundError(f'Media file not found: {media_path}')
    res = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(media_path)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f'ffprobe failed: {res.stderr.strip()[-300:]}')
    data = json.loads(res.stdout or '{}')
    dur = float((data.get('format') or {}).get('duration') or 0.0)
    if dur <= 0:
        raise ValueError(f'Invalid duration ({dur}) for {media_path.name}')
    return dur


def parse_json3_captions(json3_path: Path) -> list[SpeechSegment]:
    """Parse YouTube json3 timed-text payload into word-timed SpeechSegments."""
    if not json3_path.is_file():
        return []
    payload = json.loads(json3_path.read_text(encoding='utf-8', errors='replace'))
    events = payload.get('events') or []
    segments: list[SpeechSegment] = []

    for idx, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        t_start_ms = ev.get('tStartMs')
        if t_start_ms is None:
            continue
        start_sec = max(0.0, float(t_start_ms) / 1000.0)
        d_ms = ev.get('dDurationMs')
        if d_ms is None and idx + 1 < len(events) and isinstance(events[idx + 1], dict):
            nxt_ms = events[idx + 1].get('tStartMs')
            if nxt_ms is not None and float(nxt_ms) > float(t_start_ms):
                d_ms = float(nxt_ms) - float(t_start_ms)
        dur_sec = max(float(d_ms or 1500.0) / 1000.0, 0.2)
        end_sec = start_sec + dur_sec

        raw_segs = ev.get('segs') or []
        if not isinstance(raw_segs, list) or not raw_segs:
            continue

        pieces: list[tuple[str, float]] = []
        text_parts: list[str] = []
        for s in raw_segs:
            if not isinstance(s, dict):
                continue
            chunk = str(s.get('utf8') or '').replace('\n', ' ').strip()
            if not chunk:
                continue
            offset_sec = max(0.0, float(s.get('tOffsetMs') or 0.0) / 1000.0)
            pieces.append((chunk, min(end_sec - 0.05, start_sec + offset_sec)))
            text_parts.append(chunk)

        full_text = re.sub(r'\s+', ' ', ' '.join(text_parts)).strip()
        if not full_text:
            continue

        words: list[WordTimestamp] = []
        for p_idx, (chunk, w_start) in enumerate(pieces):
            w_end = pieces[p_idx + 1][1] if p_idx + 1 < len(pieces) else end_sec
            if w_end <= w_start:
                w_end = min(end_sec, w_start + 0.12)
            words.extend(_distribute_words(chunk, w_start, max(w_end, w_start + 0.06)))

        if not words:
            words = _distribute_words(full_text, start_sec, end_sec)
        segments.append(SpeechSegment(start=round(start_sec, 3), end=round(end_sec, 3), text=full_text, words=words))

    return segments


def _pick_best_caption_file(directory: Path) -> Path | None:
    files = [p for p in sorted(directory.iterdir()) if p.is_file()]
    for ext in CAPTION_EXTENSIONS:
        for p in files:
            if p.suffix.lower() == ext:
                return p
    return None


def fetch_audio_and_captions(
    url: str,
    dest_dir: Path,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> tuple[Path, Path | None, float]:
    """Audio-first Stage 1: download only audio + captions (json3/vtt/srt), never full video."""
    if not is_http_url(url):
        raise ValueError(f'Invalid media URL: {url!r}')
    if shutil.which('yt-dlp') is None:
        raise RuntimeError('yt-dlp is not installed')

    dest_dir.mkdir(parents=True, exist_ok=True)
    out_tpl = str(dest_dir / 'audio.%(ext)s')
    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--no-warnings',
        '--max-filesize', str(max_bytes),
        '--format', 'bestaudio[ext=m4a]/bestaudio/best',
        '--write-subs',
        '--write-auto-subs',
        '--sub-langs', 'ru,uk,en',
        '--sub-format', 'json3/vtt/srt',
        '-o', out_tpl,
        '--',
        url,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(f'yt-dlp audio fetch failed: {(res.stderr or res.stdout).strip()[-350:]}')

    audio_file: Path | None = None
    for p in sorted(dest_dir.iterdir()):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf in ALLOWED_AUDIO_EXTENSIONS or suf in ALLOWED_VIDEO_EXTENSIONS:
            if audio_file is None:
                audio_file = p

    if audio_file is None:
        raise FileNotFoundError('yt-dlp did not produce an audio file')

    cap_file = _pick_best_caption_file(dest_dir)
    duration = probe_media_duration(audio_file)
    return audio_file, cap_file, duration


def download_video_section(
    url: str,
    start_sec: float,
    end_sec: float,
    dest_dir: Path,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> tuple[Path, float]:
    """Audio-first Stage 2: download only [start_sec, end_sec] section; returns (video_path, section_offset_sec)."""
    if not is_http_url(url):
        raise ValueError(f'Invalid media URL: {url!r}')
    if shutil.which('yt-dlp') is None:
        raise RuntimeError('yt-dlp is not installed')

    sec_start = max(0.0, float(start_sec))
    sec_end = max(sec_start + 1.0, float(end_sec))
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_tpl = str(dest_dir / 'section.%(ext)s')
    section_spec = f'*{sec_start:.2f}-{sec_end:.2f}'
    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--no-warnings',
        '--max-filesize', str(max_bytes),
        '--download-sections', section_spec,
        '--force-keyframes-at-cuts',
        '--format', 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        '--merge-output-format', 'mp4',
        '-o', out_tpl,
        '--',
        url,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode == 0:
        for p in sorted(dest_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS:
                return p, sec_start

    # Clean fallback when extractor does not support partial range cuts
    full_video, _ = download_video_from_url(url, dest_dir, max_bytes=max_bytes)
    return full_video, 0.0


def _biquad_bandpass_coeffs(sample_rate: int, f0_hz: float = 2850.0, q: float = 1.35) -> tuple[float, float, float, float, float]:
    """Compute normalized RBJ biquad bandpass filter coefficients for raised-voice band (2-4 kHz)."""
    w0 = (2.0 * math.pi * f0_hz) / max(sample_rate, 1)
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    a0 = 1.0 + alpha
    return (
        alpha / a0,
        0.0,
        -alpha / a0,
        (-2.0 * cos_w0) / a0,
        (1.0 - alpha) / a0,
    )


def compute_voice_band_energy(
    wav_path: Path,
    step_sec: float = 0.25,
) -> list[tuple[float, float, float]]:
    """Measure normalized 2-4 kHz raised-voice energy per frame using a cascaded biquad bandpass."""
    if not wav_path.is_file():
        return []
    with wave.open(str(wav_path), 'rb') as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        nframes = wf.getnframes()
        if sampwidth != 2 or framerate <= 0 or nframes <= 0:
            return []
        raw = wf.readframes(nframes)

    frame_samples = max(int(framerate * step_sec), 32)
    bytes_per_sample = sampwidth * channels
    total_samples = len(raw) // bytes_per_sample
    b0, b1, b2, a1, a2 = _biquad_bandpass_coeffs(framerate, f0_hz=2850.0, q=1.35)

    # Second-order cascade (two passes) gives sharp out-of-band rejection (>40 dB at <200 Hz)
    x1_a = x2_a = y1_a = y2_a = 0.0
    x1_b = x2_b = y1_b = y2_b = 0.0
    windows: list[tuple[float, float, float]] = []
    raw_rms: list[float] = []

    for start_idx in range(0, total_samples, frame_samples):
        end_idx = min(start_idx + frame_samples, total_samples)
        chunk = raw[start_idx * bytes_per_sample:end_idx * bytes_per_sample]
        count = len(chunk) // 2
        if count < 16:
            continue
        samples = struct.unpack(f'<{count}h', chunk[:count * 2])
        if channels > 1:
            samples = samples[::channels]
        sum_sq = 0.0
        for raw_s in samples:
            x0 = raw_s / 32768.0
            y0_a = b0 * x0 + b1 * x1_a + b2 * x2_a - a1 * y1_a - a2 * y2_a
            x2_a, x1_a = x1_a, x0
            y2_a, y1_a = y1_a, y0_a

            y0_b = b0 * y0_a + b1 * x1_b + b2 * x2_b - a1 * y1_b - a2 * y2_b
            x2_b, x1_b = x1_b, y0_a
            y2_b, y1_b = y1_b, y0_b

            sum_sq += y0_b * y0_b
        band_rms = math.sqrt(sum_sq / max(len(samples), 1))
        raw_rms.append(band_rms)
        windows.append((start_idx / framerate, end_idx / framerate, band_rms))

    peak = max(raw_rms) if raw_rms else 0.0
    if peak <= 1e-8:
        return [(s, e, 0.0) for s, e, _ in windows]
    return [(s, e, min(p / peak, 1.0)) for s, e, p in windows]


def compute_loudness_jumps(
    energy_frames: list[tuple[float, float, float]],
    baseline_sec: float = DEFAULT_BASELINE_SEC,
) -> list[tuple[float, float, float]]:
    """Score sudden loudness jumps relative to a rolling trailing baseline window."""
    if not energy_frames:
        return []
    jumps: list[tuple[float, float, float]] = []
    raw_ratios: list[float] = []

    for idx, (s, e, rms) in enumerate(energy_frames):
        win_start = max(0.0, s - baseline_sec)
        prior = [f[2] for f in energy_frames[:idx] if f[1] >= win_start]
        if not prior:
            prior = [f[2] for f in energy_frames]
        baseline = sum(prior) / max(len(prior), 1)
        # Positive jump over local rolling baseline
        delta = max(0.0, rms - baseline * 1.15)
        ratio = delta / max(baseline + 0.05, 0.05)
        raw_ratios.append(ratio)
        jumps.append((s, e, ratio))

    peak_ratio = max(raw_ratios) if raw_ratios else 0.0
    if peak_ratio <= 1e-6:
        return [(s, e, 0.0) for s, e, _ in jumps]
    return [(s, e, min(r / peak_ratio, 1.0)) for s, e, r in jumps]


def compute_contrast_score(
    energy_frames: list[tuple[float, float, float]],
    w_start: float,
    w_end: float,
    pre_sec: float = 4.0,
) -> float:
    """Measure quiet-to-loud transition contrast entering/inside [w_start, w_end]."""
    win = [f[2] for f in energy_frames if f[1] > w_start and f[0] < w_end]
    if not win:
        return 0.0
    lead_in = [f[2] for f in energy_frames if f[1] > max(0.0, w_start - pre_sec) and f[0] < w_start + 1.5]
    if not lead_in:
        lead_in = win[: max(1, len(win) // 4)]
    low_ref = min(lead_in) if lead_in else 0.0
    sorted_win = sorted(win, reverse=True)
    top_k = max(1, len(sorted_win) // 5)
    high_ref = sum(sorted_win[:top_k]) / top_k
    return round(max(0.0, min(1.0, (high_ref - low_ref) * 1.35)), 4)


def score_transcript_hooks(speech_segments: list[SpeechSegment], w_start: float, w_end: float) -> float:
    """Score transcript window by excitement phrases, punctuation hooks, and word density."""
    texts: list[str] = []
    word_count = 0
    for seg in speech_segments:
        if seg.end <= w_start or seg.start >= w_end:
            continue
        texts.append(seg.text)
        word_count += len(re.findall(r'\w+', seg.text))
    if not texts:
        return 0.0

    joined = ' '.join(texts).lower()
    term_hits = sum(1 for term in HOOK_TRIGGER_TERMS if term in joined)
    punct_hits = joined.count('!') + joined.count('?')
    dur = max(w_end - w_start, 1.0)
    words_per_sec = word_count / dur

    term_part = min(1.0, term_hits / 2.0)
    punct_part = min(1.0, punct_hits / 3.0)
    pace_part = min(1.0, words_per_sec / 2.8)
    return round(min(1.0, 0.50 * term_part + 0.25 * punct_part + 0.25 * pace_part), 4)


def snap_window_to_sentences(
    start: float,
    end: float,
    duration: float,
    speech_segments: list[SpeechSegment],
    silence_intervals: list[tuple[float, float]],
    min_dur: float,
    max_dur: float,
) -> tuple[float, float]:
    """Snap clip boundaries to nearby sentence/segment boundaries inside [min_dur, max_dur]."""
    clamped_min = max(3.0, min(min_dur, duration))
    clamped_max = max(clamped_min, min(max_dur, duration))

    start_candidates = [seg.start for seg in speech_segments]
    end_candidates = [seg.end for seg in speech_segments]
    if not start_candidates:
        start_candidates = [(ss + se) / 2.0 for ss, se in silence_intervals]
    if not end_candidates:
        end_candidates = [(ss + se) / 2.0 for ss, se in silence_intervals]

    def _nearest(target: float, pool: list[float], tol: float) -> float:
        best = target
        best_dist = tol
        for b in pool:
            d = abs(b - target)
            if d < best_dist:
                best_dist = d
                best = b
        return best

    s_new = max(0.0, _nearest(start, start_candidates, tol=2.2))
    e_new = min(duration, _nearest(end, end_candidates, tol=2.8))

    if e_new - s_new < clamped_min:
        after = [b for b in sorted(end_candidates) if s_new + clamped_min <= b <= min(duration, s_new + clamped_max)]
        if after:
            e_new = after[0]
        else:
            e_new = min(duration, s_new + clamped_min)
            if e_new - s_new < clamped_min:
                s_new = max(0.0, e_new - clamped_min)

    if e_new - s_new > clamped_max:
        before = [b for b in sorted(end_candidates, reverse=True) if s_new + clamped_min <= b <= s_new + clamped_max]
        e_new = before[0] if before else (s_new + clamped_max)

    return round(max(0.0, s_new), 3), round(min(duration, e_new), 3)


def generate_title_and_hook(
    speech_segments: list[SpeechSegment],
    clip_start: float,
    clip_end: float,
    fallback_index: int = 1,
) -> tuple[str, str]:
    """Clean-room deterministic title & opening curiosity hook from transcript lines."""
    lines = [
        seg.text.strip()
        for seg in speech_segments
        if seg.end > clip_start and seg.start < clip_end and seg.text.strip()
    ]
    if not lines:
        return f'Ключевой момент #{fallback_index}', 'Досмотри до развязки'

    # Prefer a line with ! or ? or trigger keyword as the opening hook
    best_line = max(
        lines,
        key=lambda t: (
            sum(1 for term in HOOK_TRIGGER_TERMS if term in t.lower()) * 3
            + t.count('?') * 2
            + t.count('!') * 2
            + min(len(t) // 15, 2)
        ),
    )
    cleaned = re.sub(r'\s+', ' ', best_line).strip(' -—:;,.')
    words = cleaned.split()
    title_words = words[:7]
    title = ' '.join(title_words)
    if len(words) > 7:
        title += '...'
    hook_words = words[:9]
    hook = ' '.join(hook_words)
    if '?' not in hook and '!' not in hook:
        hook += '?'
    return title[:72], hook[:90]


def judge_candidate_with_local_llm(
    transcript_excerpt: str,
    ollama_url: str = 'http://127.0.0.1:11434/api/generate',
    model: str | None = None,
    llm_fn: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> dict[str, Any] | None:
    """Optionally score a moment and write title+hook via a local Ollama model (0 UAH)."""
    excerpt = (transcript_excerpt or '').strip()
    if not excerpt:
        return None
    chosen_model = model or os.environ.get('NEONCLIP_OLLAMA_MODEL', 'llama3.2:3b')

    if llm_fn is not None:
        raw = llm_fn(excerpt, chosen_model)
        if not isinstance(raw, dict):
            return None
        score = max(0.0, min(1.0, float(raw.get('score', 0.5))))
        return {
            'score': round(score, 4),
            'title': str(raw.get('title') or '').strip()[:72],
            'hook': str(raw.get('hook') or '').strip()[:90],
        }

    if not is_local_ollama_url(ollama_url):
        raise ValueError(f'Only localhost Ollama URLs are permitted, got: {ollama_url!r}')

    prompt = (
        'Rate this video transcript excerpt for a viral 9:16 short clip.\n'
        'Reply strictly with JSON: {"score": 0.0-1.0, "title": "short title", "hook": "opening question"}.\n'
        f'Transcript:\n{excerpt[:1200]}'
    )
    body = json.dumps({'model': chosen_model, 'prompt': prompt, 'stream': False}).encode('utf-8')
    req = urllib.request.Request(ollama_url, data=body, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode('utf-8', errors='replace'))
        text = str(payload.get('response') or '').strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return None
        parsed = json.loads(m.group(0))
        score = max(0.0, min(1.0, float(parsed.get('score', 0.5))))
        return {
            'score': round(score, 4),
            'title': str(parsed.get('title') or '').strip()[:72],
            'hook': str(parsed.get('hook') or '').strip()[:90],
        }
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def compute_clip_id(source_key: str, start_sec: float, end_sec: float) -> str:
    """Stable clip identity for local preference training and deduplication."""
    raw = f'{source_key.strip()}|{round(start_sec, 1):.1f}|{round(end_sec, 1):.1f}'
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def load_feedback_records(feedback_path: Path) -> list[dict[str, Any]]:
    """Read local JSONL rating records."""
    if not feedback_path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in feedback_path.read_text(encoding='utf-8', errors='replace').splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and 'clip_id' in obj and 'rating' in obj:
                records.append(obj)
        except json.JSONDecodeError:
            continue
    return records


def record_clip_feedback(
    feedback_path: Path,
    clip_id: str,
    rating: int,
    features: dict[str, float],
    source_url: str = '',
) -> dict[str, Any]:
    """Append a 1-5 star rating to local feedback.jsonl without deleting rating history."""
    stars = int(rating)
    if stars < 1 or stars > 5:
        raise ValueError(f'Rating must be between 1 and 5, got {rating}')
    cid = (clip_id or '').strip()
    if not cid:
        raise ValueError('clip_id must not be empty')

    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'clip_id': cid,
        'rating': stars,
        'source': source_url,
        'features': {k: round(float(features.get(k, 0.0)), 4) for k in DEFAULT_SIGNAL_WEIGHTS},
    }
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    with feedback_path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    return record


def fit_preference_weights(feedback_path: Path) -> tuple[dict[str, float], dict[str, Any]]:
    """Adapt ranking weights once >= 8 unique moments have varied ratings (pure Python)."""
    records = load_feedback_records(feedback_path)
    latest_by_id: dict[str, dict[str, Any]] = {}
    for rec in records:
        latest_by_id[str(rec['clip_id'])] = rec

    unique = list(latest_by_id.values())
    ratings = [float(r['rating']) for r in unique]
    has_variance = len(ratings) >= MIN_FEEDBACK_SAMPLES and (max(ratings) - min(ratings) >= 1.0)
    if not has_variance:
        return dict(DEFAULT_SIGNAL_WEIGHTS), {
            'adapted': False,
            'unique_moments': len(unique),
            'total_events': len(records),
            'min_required': MIN_FEEDBACK_SAMPLES,
        }

    mean_r = sum(ratings) / len(ratings)
    adapted: dict[str, float] = {}
    for feat, base_w in DEFAULT_SIGNAL_WEIGHTS.items():
        vals = [float((r.get('features') or {}).get(feat, 0.0)) for r in unique]
        mean_v = sum(vals) / len(vals)
        cov = sum((v - mean_v) * (r - mean_r) for v, r in zip(vals, ratings))
        var_v = sum((v - mean_v) ** 2 for v in vals)
        slope = (cov / var_v) if var_v > 1e-6 else 0.0
        # Keep weights positive and bounded around baseline
        adapted[feat] = max(0.05, min(0.55, base_w + 0.12 * slope))

    total_w = sum(adapted.values()) or 1.0
    normalized = {k: round(v / total_w, 4) for k, v in adapted.items()}
    return normalized, {
        'adapted': True,
        'unique_moments': len(unique),
        'total_events': len(records),
        'min_required': MIN_FEEDBACK_SAMPLES,
    }


def select_neon_candidates(
    duration: float,
    energy_frames: list[tuple[float, float, float]],
    jump_frames: list[tuple[float, float, float]],
    voice_frames: list[tuple[float, float, float]],
    speech_segments: list[SpeechSegment],
    silence_intervals: list[tuple[float, float]] | None = None,
    min_duration: float = DEFAULT_MIN_DURATION,
    max_duration: float = DEFAULT_MAX_DURATION,
    max_clips: int = 3,
    weights: dict[str, float] | None = None,
    use_local_llm: bool = False,
    llm_fn: Callable[[str, str], dict[str, Any] | None] | None = None,
    source_key: str = 'local',
) -> list[NeonCandidate]:
    """Rank and select non-overlapping highlight clips using NeonClipping multi-signal scoring."""
    if duration <= 0:
        raise ValueError(f'Invalid media duration: {duration}')
    w_map = weights or DEFAULT_SIGNAL_WEIGHTS
    silences = silence_intervals or []

    eff_min = max(3.0, min(min_duration, duration))
    eff_max = max(eff_min, min(max_duration, duration))
    target_dur = min(eff_max, max(eff_min, (eff_min + eff_max) * 0.5))

    def _window_avg(frames: list[tuple[float, float, float]], ws: float, we: float) -> float:
        vals = [f[2] for f in frames if f[1] > ws and f[0] < we]
        if not vals:
            return 0.0
        top = sorted(vals, reverse=True)[: max(1, len(vals) // 4)]
        return 0.55 * (sum(vals) / len(vals)) + 0.45 * (sum(top) / len(top))

    if duration <= eff_min + 1.0:
        windows = [(0.0, round(duration, 3))]
    else:
        stride = max(1.0, min(3.0, target_dur / 5.0))
        windows = []
        cur = 0.0
        while cur + eff_min <= duration + 1e-6:
            we = min(duration, cur + target_dur)
            windows.append((round(cur, 3), round(we, 3)))
            if we >= duration:
                break
            cur += stride

    scored_raw: list[NeonCandidate] = []
    for ws, we in windows:
        win_len = max(we - ws, 0.1)
        jump_s = _window_avg(jump_frames, ws, we)
        voice_s = _window_avg(voice_frames, ws, we)
        contrast_s = compute_contrast_score(energy_frames, ws, we)
        kw_s = score_transcript_hooks(speech_segments, ws, we)

        speech_cov = sum(max(0.0, min(we, seg.end) - max(ws, seg.start)) for seg in speech_segments)
        if speech_cov == 0.0 and silences:
            silent_cov = sum(max(0.0, min(we, se) - max(ws, ss)) for ss, se in silences)
            speech_ratio = max(0.0, 1.0 - silent_cov / win_len)
        else:
            speech_ratio = min(1.0, speech_cov / win_len)

        base_score = (
            w_map.get('jump', 0.30) * jump_s
            + w_map.get('voice_2_4k', 0.25) * voice_s
            + w_map.get('contrast', 0.15) * contrast_s
            + w_map.get('keyword', 0.15) * kw_s
            + w_map.get('speech_ratio', 0.15) * speech_ratio
        )
        scored_raw.append(
            NeonCandidate(
                start=ws,
                end=we,
                score=round(base_score, 4),
                jump_score=round(jump_s, 4),
                voice_2_4k_score=round(voice_s, 4),
                contrast_score=round(contrast_s, 4),
                keyword_score=round(kw_s, 4),
                speech_ratio=round(speech_ratio, 4),
            )
        )

    ranked = sorted(scored_raw, key=lambda c: (-c.score, c.start))
    selected: list[NeonCandidate] = []
    for cand in ranked:
        if len(selected) >= max(1, max_clips):
            break
        overlaps = any(min(cand.end, s.end) - max(cand.start, s.start) > target_dur * 0.25 for s in selected)
        if overlaps:
            continue

        snap_s, snap_e = snap_window_to_sentences(
            cand.start,
            cand.end,
            duration,
            speech_segments,
            silences,
            min_dur=eff_min,
            max_dur=eff_max,
        )
        excerpt = ' '.join(
            seg.text for seg in speech_segments if seg.end > snap_s and seg.start < snap_e
        ).strip()
        title, hook = generate_title_and_hook(speech_segments, snap_s, snap_e, fallback_index=len(selected) + 1)
        llm_score: float | None = None
        final_score = cand.score

        if use_local_llm or llm_fn is not None:
            verdict = judge_candidate_with_local_llm(excerpt, llm_fn=llm_fn)
            if verdict is not None:
                llm_score = verdict['score']
                final_score = round(0.65 * cand.score + 0.35 * llm_score, 4)
                if verdict['title']:
                    title = verdict['title']
                if verdict['hook']:
                    hook = verdict['hook']

        selected.append(
            NeonCandidate(
                start=snap_s,
                end=snap_e,
                score=final_score,
                jump_score=cand.jump_score,
                voice_2_4k_score=cand.voice_2_4k_score,
                contrast_score=cand.contrast_score,
                keyword_score=cand.keyword_score,
                speech_ratio=cand.speech_ratio,
                llm_score=llm_score,
                title=title,
                hook=hook,
                clip_id=compute_clip_id(source_key, snap_s, snap_e),
            )
        )

    return sorted(selected, key=lambda c: (-c.score, c.start))


def _format_srt_time(sec: float) -> str:
    ms_total = int(round(max(0.0, sec) * 1000))
    ms = ms_total % 1000
    s_total = ms_total // 1000
    s = s_total % 60
    m_total = s_total // 60
    m = m_total % 60
    h = m_total // 60
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def _format_vtt_time(sec: float) -> str:
    return _format_srt_time(sec).replace(',', '.')


def write_srt_and_vtt(
    speech_segments: list[SpeechSegment],
    clip_start: float,
    clip_end: float,
    srt_path: Path,
    vtt_path: Path,
) -> int:
    """Export trimmed clip subtitles as standard .srt and .vtt files."""
    cues: list[tuple[float, float, str]] = []
    for seg in speech_segments:
        if seg.end <= clip_start or seg.start >= clip_end:
            continue
        s = max(0.0, seg.start - clip_start)
        e = min(clip_end - clip_start, seg.end - clip_start)
        txt = re.sub(r'\s+', ' ', seg.text).strip()
        if txt and e > s + 0.05:
            cues.append((s, e, txt))

    srt_lines: list[str] = []
    vtt_lines: list[str] = ['WEBVTT\n\n']
    for idx, (s, e, txt) in enumerate(cues, start=1):
        srt_lines.append(f'{idx}\n{_format_srt_time(s)} --> {_format_srt_time(e)}\n{txt}\n\n')
        vtt_lines.append(f'{_format_vtt_time(s)} --> {_format_vtt_time(e)}\n{txt}\n\n')

    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(''.join(srt_lines), encoding='utf-8')
    vtt_path.write_text(''.join(vtt_lines), encoding='utf-8')
    return len(cues)


def build_styled_ass(
    speech_segments: list[SpeechSegment],
    clip_start: float,
    clip_end: float,
    ass_path: Path,
    style_name: str = 'neon',
    opening_heading: str = '',
    heading_sec: float = 6.0,
    max_words_per_line: int = 4,
) -> int:
    """Write a styled ASS karaoke subtitle file with optional opening hook banner."""
    if style_name not in CAPTION_STYLES:
        raise ValueError(f'Unknown caption style: {style_name!r}')
    st = CAPTION_STYLES[style_name]
    font_name = _pick_ass_font_name()
    header = (
        '[Script Info]\n'
        'ScriptType: v4.00+\n'
        f'PlayResX: {REEL_WIDTH}\n'
        f'PlayResY: {REEL_HEIGHT}\n'
        'WrapStyle: 2\n'
        'ScaledBorderAndShadow: yes\n\n'
        '[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, '
        'Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, '
        'Alignment, MarginL, MarginR, MarginV, Encoding\n'
        f"Style: Caption,{font_name},{st['fontsize']},{st['primary']},{st['secondary']},"
        f"{st['outline_col']},{st['back_col']},-1,0,0,0,100,100,1,0,{st['border_style']},"
        f"{st['outline']},{st['shadow']},2,64,64,{st['margin_v']},1\n"
        f"Style: Heading,{font_name},52,&H00FFFFFF,&H00FFFFFF,&H00080D18,&HDD080D18,"
        "-1,0,0,0,100,100,0,0,3,16,0,8,56,56,160,1\n\n"
        '[Events]\n'
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    )

    clip_words: list[WordTimestamp] = []
    for seg in speech_segments:
        if seg.end <= clip_start or seg.start >= clip_end:
            continue
        words = seg.words or _distribute_words(seg.text, seg.start, seg.end)
        for w in words:
            if w.end <= clip_start or w.start >= clip_end:
                continue
            ws = max(0.0, w.start - clip_start)
            we = min(clip_end - clip_start, w.end - clip_start)
            if we > ws + 0.02:
                clip_words.append(WordTimestamp(word=w.word, start=ws, end=we))

    if not clip_words:
        dur = max(clip_end - clip_start, 1.0)
        clip_words = _distribute_words('НАУМ · NEON CLIP', 0.0, min(dur, 4.0))

    lines: list[str] = [header]
    if opening_heading.strip():
        clip_len = max(clip_end - clip_start, 1.0)
        h_end = min(clip_len, max(2.0, heading_sec))
        lines.append(
            f"Dialogue: 1,{_format_ass_time(0.0)},{_format_ass_time(h_end)},Heading,,0,0,0,,"
            f"{_escape_ass_text(opening_heading.strip())}\n"
        )

    dialogue_count = 0
    for idx in range(0, len(clip_words), max_words_per_line):
        chunk = clip_words[idx:idx + max_words_per_line]
        line_start = chunk[0].start
        line_end = max(chunk[-1].end, line_start + 0.35)
        karaoke_parts: list[str] = []
        for w in chunk:
            dur_cs = max(int(round((w.end - w.start) * 100)), 8)
            karaoke_parts.append(f'{{\\kf{dur_cs}}}{_escape_ass_text(w.word)}')
        lines.append(
            f"Dialogue: 0,{_format_ass_time(line_start)},{_format_ass_time(line_end)},Caption,,0,0,0,,{' '.join(karaoke_parts)}\n"
        )
        dialogue_count += 1

    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(''.join(lines), encoding='utf-8')
    return dialogue_count


def render_neon_clip(
    video_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    ass_path: Path,
    meta: dict[str, Any],
    framing: str = 'blur_pad',
    fps: int = REEL_FPS,
) -> dict[str, Any]:
    """Render a 1080x1920 9:16 MP4 clip with blur_pad, center_crop, or split_cam framing."""
    if framing not in FRAMING_MODES:
        raise ValueError(f'Unsupported framing mode: {framing!r}')
    clip_dur = max(end_sec - start_sec, 0.5)
    width = int(meta.get('width') or 0)
    height = int(meta.get('height') or 0)
    has_audio = bool(meta.get('has_audio', True))
    is_portrait = width > 0 and height > 0 and (width / height) <= 0.65

    ass_escaped = _escape_ffmpeg_filter_path(ass_path)
    if framing == 'center_crop' or (is_portrait and framing != 'split_cam'):
        vf = (
            f'[0:v]scale={REEL_WIDTH}:{REEL_HEIGHT}:force_original_aspect_ratio=increase,'
            f"crop={REEL_WIDTH}:{REEL_HEIGHT},setsar=1,ass='{ass_escaped}',format=yuv420p[v]"
        )
    elif framing == 'split_cam':
        # Top 720px: zoomed upper-center speaker/cam panel; Bottom 1200px: full-width content crop
        vf = (
            f'[0:v]split=2[top_src][bot_src];'
            f'[top_src]crop=iw*0.5:ih*0.6:iw*0.25:0,scale={REEL_WIDTH}:720:force_original_aspect_ratio=increase,'
            f'crop={REEL_WIDTH}:720[top];'
            f'[bot_src]scale={REEL_WIDTH}:1200:force_original_aspect_ratio=increase,'
            f'crop={REEL_WIDTH}:1200[bot];'
            f"[top][bot]vstack=inputs=2,setsar=1,ass='{ass_escaped}',format=yuv420p[v]"
        )
    else:
        vf = (
            f'[0:v]split=2[bg_src][fg_src];'
            f'[bg_src]scale={REEL_WIDTH}:{REEL_HEIGHT}:force_original_aspect_ratio=increase,'
            f'crop={REEL_WIDTH}:{REEL_HEIGHT},boxblur=20:4,eq=brightness=-0.08[bg];'
            f'[fg_src]scale={REEL_WIDTH}:-2:force_original_aspect_ratio=decrease[fg];'
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1,ass='{ass_escaped}',format=yuv420p[v]"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix='.neon_', suffix='.tmp.mp4', dir=str(output_path.parent))
    os.close(fd)
    tmp_mp4 = Path(tmp_name)

    try:
        cmd = [
            'ffmpeg', '-y', '-ss', f'{start_sec:.3f}', '-t', f'{clip_dur:.3f}',
            '-i', str(video_path), '-filter_complex', vf, '-map', '[v]',
        ]
        if has_audio:
            cmd.extend(['-map', '0:a:0?', '-c:a', 'aac', '-b:a', '128k', '-ac', '2'])
        else:
            cmd.append('-an')
        cmd.extend([
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22',
            '-r', str(fps), '-movflags', '+faststart', '-f', 'mp4', str(tmp_mp4),
        ])
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f'ffmpeg render failed: {res.stderr[-400:]}')

        out_meta = probe_media(tmp_mp4)
        if out_meta['width'] != REEL_WIDTH or out_meta['height'] != REEL_HEIGHT:
            raise RuntimeError(f"Unexpected output resolution {out_meta['width']}x{out_meta['height']}")
        os.chmod(tmp_mp4, 0o644)
        os.replace(tmp_mp4, output_path)
        os.chmod(output_path, 0o644)
    except BaseException:
        tmp_mp4.unlink(missing_ok=True)
        raise

    return {
        'output_path': str(output_path),
        'start_sec': round(start_sec, 3),
        'end_sec': round(end_sec, 3),
        'duration_sec': round(out_meta['duration'], 3),
        'resolution': f'{REEL_WIDTH}x{REEL_HEIGHT}',
        'bytes': output_path.stat().st_size,
    }


def _resolve_segments(
    wav_path: Path | None,
    duration: float,
    energy_frames: list[tuple[float, float, float]],
    caption_path: Path | None,
    transcript_segments: list[dict[str, Any]] | None,
    lang: str | None,
) -> tuple[str, list[SpeechSegment]]:
    if transcript_segments:
        segs = _transcript_dicts_to_segments(transcript_segments)
        if segs:
            return 'provided_transcript', segs

    if caption_path is not None and caption_path.is_file():
        if caption_path.suffix.lower() == '.json3':
            segs = parse_json3_captions(caption_path)
            if segs:
                return 'youtube_json3', segs
        else:
            segs = parse_subtitle_file(caption_path)
            if segs:
                return 'subtitle_timestamps', segs

    if wav_path is not None:
        try:
            from video_clipper import _transcribe_with_local_whisper
            w_segs = _transcribe_with_local_whisper(wav_path, lang=lang)
            if w_segs:
                return 'faster_whisper_local', w_segs
        except Exception:
            pass

    return 'energy_vad', build_speech_segments_from_energy(energy_frames, duration=duration)


def neon_clip_pipeline(
    source: str | Path,
    output_dir: Path,
    mode: str = 'fast',
    min_duration: float = DEFAULT_MIN_DURATION,
    max_duration: float = DEFAULT_MAX_DURATION,
    max_clips: int = 3,
    caption_style: str = 'neon',
    framing: str = 'blur_pad',
    opening_heading: bool = True,
    lang: str | None = None,
    transcript_segments: list[dict[str, Any]] | None = None,
    subtitle_path: Path | None = None,
    feedback_path: Path | None = None,
    use_local_llm: bool = False,
    llm_fn: Callable[[str, str], dict[str, Any] | None] | None = None,
    audio_fetch_fn: Callable[[str, Path], tuple[Path, Path | None, float]] = fetch_audio_and_captions,
    section_fetch_fn: Callable[[str, float, float, Path], tuple[Path, float]] = download_video_section,
    full_download_fn: Callable[[str, Path], tuple[Path, Path | None]] = download_video_from_url,
) -> dict[str, Any]:
    """End-to-end NeonClipping-inspired pipeline (audio-first fast mode + local full mode)."""
    if mode not in ('fast', 'full'):
        raise ValueError(f'Unsupported mode: {mode!r}')
    if caption_style not in CAPTION_STYLES:
        raise ValueError(f'Unsupported caption_style: {caption_style!r}')
    if framing not in FRAMING_MODES:
        raise ValueError(f'Unsupported framing: {framing!r}')

    source_str = str(source).strip()
    output_dir.mkdir(parents=True, exist_ok=True)
    fb_path = feedback_path or (output_dir / 'feedback.jsonl')
    weights, fb_stats = fit_preference_weights(fb_path)

    with tempfile.TemporaryDirectory(prefix='neonclip_') as tmp_str:
        tmp_dir = Path(tmp_str)
        is_url = is_http_url(source_str)
        full_video_path: Path | None = None
        cap_file: Path | None = subtitle_path

        if is_url and mode == 'fast':
            source_type = 'url_audio_first'
            audio_src, fetched_cap, duration = audio_fetch_fn(source_str, tmp_dir / 'audio')
            if cap_file is None:
                cap_file = fetched_cap
            wav_path: Path | None = tmp_dir / 'analysis.wav'
            if not extract_audio_wav(audio_src, wav_path):
                wav_path = None
            silences = detect_silence_intervals(audio_src) if wav_path is not None else []
        else:
            if is_url:
                source_type = 'url_full'
                full_video_path, fetched_cap = full_download_fn(source_str, tmp_dir / 'dl')
                if cap_file is None:
                    cap_file = fetched_cap
            else:
                source_type = 'file'
                full_video_path = Path(source_str).expanduser().resolve()
                if not full_video_path.is_file():
                    raise FileNotFoundError(f'Input video not found: {source_str}')
            meta_full = probe_media(full_video_path)
            duration = meta_full['duration']
            wav_path = tmp_dir / 'analysis.wav'
            if not meta_full['has_audio'] or not extract_audio_wav(full_video_path, wav_path):
                wav_path = None
            silences = detect_silence_intervals(full_video_path) if wav_path is not None else []

        energy_frames = analyze_wav_energy(wav_path) if wav_path is not None else []
        jump_frames = compute_loudness_jumps(energy_frames)
        voice_frames = compute_voice_band_energy(wav_path) if wav_path is not None else []
        stt_engine, speech_segments = _resolve_segments(
            wav_path=wav_path,
            duration=duration,
            energy_frames=energy_frames,
            caption_path=cap_file,
            transcript_segments=transcript_segments,
            lang=lang,
        )

        candidates = select_neon_candidates(
            duration=duration,
            energy_frames=energy_frames,
            jump_frames=jump_frames,
            voice_frames=voice_frames,
            speech_segments=speech_segments,
            silence_intervals=silences,
            min_duration=min_duration,
            max_duration=max_duration,
            max_clips=max_clips,
            weights=weights,
            use_local_llm=use_local_llm,
            llm_fn=llm_fn,
            source_key=source_str,
        )

        stem = re.sub(r'[^a-zA-Z0-9_-]+', '_', Path(source_str).stem if not is_url else 'neon').strip('_')[:28] or 'neon'
        rendered: list[dict[str, Any]] = []
        for idx, cand in enumerate(candidates, start=1):
            if full_video_path is not None:
                clip_src = full_video_path
                cut_start = cand.start
                cut_end = cand.end
                clip_meta = probe_media(clip_src)
            else:
                # Download only the selected window with a small 1.5s margin for keyframe accuracy
                pad_start = max(0.0, cand.start - 1.5)
                pad_end = min(duration, cand.end + 1.5)
                clip_src, section_offset = section_fetch_fn(source_str, pad_start, pad_end, tmp_dir / f'sec_{idx}')
                clip_meta = probe_media(clip_src)
                cut_start = max(0.0, cand.start - section_offset)
                cut_end = max(cut_start + 0.5, cand.end - section_offset)

            heading_txt = cand.hook if opening_heading else ''
            ass_file = tmp_dir / f'neon_{idx}.ass'
            sub_lines = build_styled_ass(
                speech_segments=speech_segments,
                clip_start=cand.start,
                clip_end=cand.end,
                ass_path=ass_file,
                style_name=caption_style,
                opening_heading=heading_txt,
            )

            out_mp4 = output_dir / f'{stem}_neon_{idx:02d}.mp4'
            out_srt = output_dir / f'{stem}_neon_{idx:02d}.srt'
            out_vtt = output_dir / f'{stem}_neon_{idx:02d}.vtt'
            out_meta = output_dir / f'{stem}_neon_{idx:02d}.json'

            write_srt_and_vtt(speech_segments, cand.start, cand.end, out_srt, out_vtt)
            render_info = render_neon_clip(
                video_path=clip_src,
                output_path=out_mp4,
                start_sec=cut_start,
                end_sec=cut_end,
                ass_path=ass_file,
                meta=clip_meta,
                framing=framing,
            )

            record = {
                **render_info,
                'index': idx,
                'clip_id': cand.clip_id,
                'title': cand.title,
                'hook': cand.hook,
                'start_sec': cand.start,
                'end_sec': cand.end,
                'score': cand.score,
                'llm_score': cand.llm_score,
                'features': {
                    'jump': cand.jump_score,
                    'voice_2_4k': cand.voice_2_4k_score,
                    'contrast': cand.contrast_score,
                    'keyword': cand.keyword_score,
                    'speech_ratio': cand.speech_ratio,
                },
                'caption_style': caption_style,
                'framing': framing,
                'subtitles_count': sub_lines,
                'srt_path': str(out_srt),
                'vtt_path': str(out_vtt),
                'mp4_name': out_mp4.name,
                'srt_name': out_srt.name,
                'vtt_name': out_vtt.name,
            }
            out_meta.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            rendered.append(record)

    return {
        'ok': True,
        'source': source_str,
        'source_type': source_type,
        'mode': mode,
        'duration_sec': round(duration, 3),
        'stt_engine': stt_engine,
        'weights_used': weights,
        'feedback_stats': fb_stats,
        'clips': rendered,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description='NeonClipping clean-room audio-first 9:16 video clipper')
    ap.add_argument('--input', required=True, help='Local video path or public http(s) URL')
    ap.add_argument('--output-dir', default='/var/lib/naum-studio/neon_clips', help='Output directory for MP4/SRT/VTT')
    ap.add_argument('--mode', choices=('fast', 'full'), default='fast', help='Audio-first fast mode vs full download')
    ap.add_argument('--min-duration', type=float, default=DEFAULT_MIN_DURATION)
    ap.add_argument('--max-duration', type=float, default=DEFAULT_MAX_DURATION)
    ap.add_argument('--max-clips', type=int, default=3)
    ap.add_argument('--style', choices=tuple(CAPTION_STYLES.keys()), default='neon')
    ap.add_argument('--framing', choices=FRAMING_MODES, default='blur_pad')
    ap.add_argument('--no-heading', action='store_true', help='Disable opening curiosity heading')
    ap.add_argument('--use-llm', action='store_true', help='Enable local Ollama scoring/title judge')
    ap.add_argument('--lang', default=None, help='Optional language hint for local Whisper')
    ap.add_argument('--subtitle-file', default=None, help='Optional .json3/.vtt/.srt file')
    args = ap.parse_args()

    res = neon_clip_pipeline(
        source=args.input,
        output_dir=Path(args.output_dir),
        mode=args.mode,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        max_clips=args.max_clips,
        caption_style=args.style,
        framing=args.framing,
        opening_heading=not args.no_heading,
        lang=args.lang,
        subtitle_path=Path(args.subtitle_file) if args.subtitle_file else None,
        use_local_llm=args.use_llm,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
