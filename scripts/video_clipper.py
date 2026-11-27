#!/usr/bin/env python3
"""AI Video Shorts Clipper (PeakCut-inspired + ffmpeg + speech/whisper timestamp analysis, 0 UAH)."""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.parse
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REEL_WIDTH = 1080
REEL_HEIGHT = 1920
REEL_FPS = 30
DEFAULT_CLIP_DURATION = 20.0
MIN_CLIP_DURATION = 3.0
MAX_CLIP_DURATION = 60.0
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024
ALLOWED_VIDEO_EXTENSIONS = frozenset({'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'})

FONT_NAME_CANDIDATES = (
    ('Montserrat', Path('/usr/share/fonts/truetype/montserrat/Montserrat-Bold.ttf')),
    ('DejaVu Sans', Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')),
)

_TIMECODE_RE = re.compile(
    r'(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})'
)
_INLINE_VTT_WORD_RE = re.compile(r'<(\d{2}:\d{2}:\d{2}\.\d{3})><c>(.*?)</c>')
_HTML_TAG_RE = re.compile(r'<[^>]+>')
_SILENCE_START_RE = re.compile(r'silence_start:\s*([0-9.]+)')
_SILENCE_END_RE = re.compile(r'silence_end:\s*([0-9.]+)')
_SCENE_PTS_RE = re.compile(r'pts_time:([0-9.]+)')


@dataclass(frozen=True)
class WordTimestamp:
    """Single word with start and end timestamps in seconds."""

    word: str
    start: float
    end: float


@dataclass
class SpeechSegment:
    """Timed speech segment with word-level karaoke cues."""

    start: float
    end: float
    text: str
    words: list[WordTimestamp] = field(default_factory=list)
    energy: float = 0.0


@dataclass(frozen=True)
class ClipCandidate:
    """Scored highlight window selected by PeakCut multi-signal analyzer."""

    start: float
    end: float
    score: float
    energy_score: float
    speech_ratio: float
    scene_hits: int


def is_http_url(value: str) -> bool:
    """Return True if value is a safe public http/https URL without whitespace or local SSRF targets."""
    raw = (value or '').strip()
    if not raw or any(ch.isspace() for ch in raw):
        return False
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return False
    host = (parsed.hostname or '').strip().lower().rstrip('.')
    if not host or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    except ValueError:
        pass
    return True


def _pick_ass_font_name() -> str:
    for name, path in FONT_NAME_CANDIDATES:
        if path.is_file():
            return name
    return 'DejaVu Sans'


def _escape_ffmpeg_filter_path(path: Path) -> str:
    """Escape a filesystem path for ffmpeg filtergraph options like ass='...'."""
    s = str(path.resolve())
    s = s.replace('\\', '\\\\')
    s = s.replace(':', '\\:')
    s = s.replace("'", "\\'")
    s = s.replace('[', '\\[').replace(']', '\\]')
    return s


def _parse_timecode_parts(hours: str | None, mins: str, secs: str, ms: str) -> float:
    h = int(hours) if hours else 0
    return h * 3600.0 + int(mins) * 60.0 + int(secs) + int(ms) / 1000.0


def _distribute_words(text: str, start: float, end: float) -> list[WordTimestamp]:
    tokens = [tok.strip() for tok in re.split(r'\s+', text.strip()) if tok.strip()]
    if not tokens:
        return []
    span = max(end - start, 0.25)
    weights = [max(len(re.sub(r'\W+', '', tok)), 1) for tok in tokens]
    total_w = sum(weights)
    cursor = start
    result: list[WordTimestamp] = []
    for idx, (tok, w) in enumerate(zip(tokens, weights)):
        dur = span * (w / total_w)
        nxt = end if idx == len(tokens) - 1 else min(end, cursor + dur)
        result.append(WordTimestamp(word=tok, start=round(cursor, 3), end=round(max(nxt, cursor + 0.05), 3)))
        cursor = nxt
    return result


def parse_subtitle_file(sub_path: Path) -> list[SpeechSegment]:
    """Parse .srt or .vtt subtitles into SpeechSegments with word-level timestamps."""
    if not sub_path.is_file():
        return []
    raw = sub_path.read_text(encoding='utf-8', errors='replace')
    blocks = re.split(r'\r?\n\r?\n+', raw.strip())
    segments: list[SpeechSegment] = []

    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        tc_idx = next((i for i, ln in enumerate(lines) if _TIMECODE_RE.search(ln)), None)
        if tc_idx is None:
            continue
        m = _TIMECODE_RE.search(lines[tc_idx])
        assert m is not None
        start = _parse_timecode_parts(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _parse_timecode_parts(m.group(5), m.group(6), m.group(7), m.group(8))
        if end <= start:
            continue
        body_raw = ' '.join(lines[tc_idx + 1:])

        # Support YouTube VTT inline word-timing tags (<00:00:01.200><c> word</c>) when present
        inline_matches = list(_INLINE_VTT_WORD_RE.finditer(body_raw))
        clean_text = html.unescape(_HTML_TAG_RE.sub('', body_raw))
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        if not clean_text:
            continue

        words: list[WordTimestamp] = []
        if inline_matches:
            # First prefix word before first <timestamp>
            prefix = html.unescape(_HTML_TAG_RE.sub('', body_raw[:inline_matches[0].start()])).strip()
            cue_times: list[tuple[str, float]] = []
            if prefix:
                first_ts_m = _TIMECODE_RE.search(f"{inline_matches[0].group(1)} --> {inline_matches[0].group(1)}")
                first_ts = start
                if first_ts_m:
                    first_ts = _parse_timecode_parts(first_ts_m.group(1), first_ts_m.group(2), first_ts_m.group(3), first_ts_m.group(4))
                cue_times.append((prefix, max(start, first_ts)))
            for im in inline_matches:
                ts_parts = im.group(1).split(':')
                ts_sec = int(ts_parts[0]) * 3600.0 + int(ts_parts[1]) * 60.0 + float(ts_parts[2])
                w_text = html.unescape(_HTML_TAG_RE.sub('', im.group(2))).strip()
                if w_text:
                    cue_times.append((w_text, ts_sec))
            for idx, (w_txt, w_start) in enumerate(cue_times):
                w_end = cue_times[idx + 1][1] if idx + 1 < len(cue_times) else end
                if w_end <= w_start:
                    w_end = min(end, w_start + 0.15)
                for sub_w in _distribute_words(w_txt, w_start, max(w_end, w_start + 0.08)):
                    words.append(sub_w)

        if not words:
            words = _distribute_words(clean_text, start, end)

        segments.append(SpeechSegment(start=start, end=end, text=clean_text, words=words))

    return segments


def probe_media(video_path: Path) -> dict[str, Any]:
    """Inspect video streams and duration using ffprobe."""
    if not video_path.is_file():
        raise FileNotFoundError(f'Video file not found: {video_path}')
    res = subprocess.run(
        [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration:stream=codec_type,codec_name,width,height,r_frame_rate',
            '-of', 'json',
            str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f'ffprobe failed: {res.stderr.strip()[-300:]}')
    data = json.loads(res.stdout or '{}')
    streams = data.get('streams') or []
    v_stream = next((s for s in streams if s.get('codec_type') == 'video'), None)
    a_stream = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if v_stream is None:
        raise ValueError(f'No video stream found in {video_path.name}')

    duration_raw = (data.get('format') or {}).get('duration')
    duration = float(duration_raw) if duration_raw is not None else 0.0
    if duration <= 0:
        raise ValueError(f'Invalid video duration ({duration}) in {video_path.name}')

    return {
        'duration': duration,
        'width': int(v_stream.get('width') or 0),
        'height': int(v_stream.get('height') or 0),
        'has_video': True,
        'has_audio': a_stream is not None,
        'video_codec': str(v_stream.get('codec_name') or ''),
    }


def download_video_from_url(
    url: str,
    dest_dir: Path,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> tuple[Path, Path | None]:
    """Download video (and free auto-captions if available) via yt-dlp without paid APIs."""
    if not is_http_url(url):
        raise ValueError(f'Invalid video URL: {url!r}')
    if shutil.which('yt-dlp') is None:
        raise RuntimeError('yt-dlp is not installed')

    dest_dir.mkdir(parents=True, exist_ok=True)
    out_tpl = str(dest_dir / 'source.%(ext)s')
    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--no-warnings',
        '--max-filesize', str(max_bytes),
        '--format', 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        '--merge-output-format', 'mp4',
        '--write-subs',
        '--write-auto-subs',
        '--sub-langs', 'ru,uk,en',
        '--sub-format', 'vtt/srt',
        '-o', out_tpl,
        '--',
        url,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(f'yt-dlp failed: {(res.stderr or res.stdout).strip()[-350:]}')

    video_file: Path | None = None
    sub_file: Path | None = None
    for candidate in sorted(dest_dir.iterdir()):
        if not candidate.is_file():
            continue
        suf = candidate.suffix.lower()
        if suf in ALLOWED_VIDEO_EXTENSIONS and video_file is None:
            video_file = candidate
        elif suf in ('.vtt', '.srt') and sub_file is None:
            sub_file = candidate

    if video_file is None:
        raise FileNotFoundError('yt-dlp finished without producing a video file')
    if video_file.stat().st_size > max_bytes:
        raise ValueError(f'Downloaded video exceeds limit ({max_bytes} bytes)')
    return video_file, sub_file


def extract_audio_wav(video_path: Path, wav_path: Path, sample_rate: int = 16000) -> bool:
    """Extract 16kHz mono PCM WAV for local speech/energy analysis. Returns False if no audio."""
    cmd = [
        'ffmpeg',
        '-y',
        '-i', str(video_path),
        '-vn',
        '-ac', '1',
        '-ar', str(sample_rate),
        '-c:a', 'pcm_s16le',
        str(wav_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not wav_path.is_file() or wav_path.stat().st_size <= 44:
        return False
    return True


def analyze_wav_energy(wav_path: Path, step_sec: float = 0.25) -> list[tuple[float, float, float]]:
    """Compute normalized RMS energy frames (start, end, rms_norm) from a 16-bit PCM WAV file."""
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

    frame_samples = max(int(framerate * step_sec), 1)
    bytes_per_sample = sampwidth * channels
    total_samples = len(raw) // bytes_per_sample
    windows: list[tuple[float, float, float]] = []
    raw_rms: list[float] = []

    for start_idx in range(0, total_samples, frame_samples):
        end_idx = min(start_idx + frame_samples, total_samples)
        chunk = raw[start_idx * bytes_per_sample:end_idx * bytes_per_sample]
        count = len(chunk) // 2
        if count <= 0:
            continue
        samples = struct.unpack(f'<{count}h', chunk[:count * 2])
        if channels > 1:
            samples = samples[::channels]
        mean_sq = sum(s * s for s in samples) / max(len(samples), 1)
        rms = math.sqrt(mean_sq)
        raw_rms.append(rms)
        windows.append((start_idx / framerate, end_idx / framerate, rms))

    peak_rms = max(raw_rms) if raw_rms else 0.0
    if peak_rms <= 1e-6:
        return [(s, e, 0.0) for s, e, _ in windows]
    return [(s, e, min(r / peak_rms, 1.0)) for s, e, r in windows]


def detect_silence_intervals(
    video_path: Path,
    noise_db: str = '-32dB',
    min_silence: float = 0.35,
) -> list[tuple[float, float]]:
    """Detect silence intervals using ffmpeg silencedetect filter."""
    cmd = [
        'ffmpeg',
        '-i', str(video_path),
        '-af', f'silencedetect=noise={noise_db}:d={min_silence:.2f}',
        '-f', 'null',
        '-',
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    intervals: list[tuple[float, float]] = []
    cur_start: float | None = None
    for line in res.stderr.splitlines():
        ms = _SILENCE_START_RE.search(line)
        if ms:
            cur_start = float(ms.group(1))
            continue
        me = _SILENCE_END_RE.search(line)
        if me and cur_start is not None:
            end_t = float(me.group(1))
            if end_t > cur_start:
                intervals.append((cur_start, end_t))
            cur_start = None
    return intervals


def detect_scene_changes(video_path: Path, threshold: float = 0.28) -> list[float]:
    """Detect visual scene-change timestamps via ffmpeg select/showinfo."""
    cmd = [
        'ffmpeg',
        '-i', str(video_path),
        '-vf', f"select='gt(scene,{threshold:.2f})',showinfo",
        '-f', 'null',
        '-',
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return [float(m.group(1)) for m in _SCENE_PTS_RE.finditer(res.stderr)]


def _transcribe_with_local_whisper(wav_path: Path, lang: str | None = None) -> list[SpeechSegment] | None:
    """Optionally transcribe WAV using local faster-whisper (CPU int8, 0 UAH) if installed."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        return None

    model_size = os.environ.get('CLIPPER_WHISPER_MODEL', 'tiny')
    model = WhisperModel(model_size, device='cpu', compute_type='int8')
    kw: dict[str, Any] = {'word_timestamps': True, 'vad_filter': True}
    if lang:
        kw['language'] = lang
    raw_segments, _ = model.transcribe(str(wav_path), **kw)
    segments: list[SpeechSegment] = []
    for seg in raw_segments:
        txt = (seg.text or '').strip()
        if not txt:
            continue
        words: list[WordTimestamp] = []
        for w in (getattr(seg, 'words', None) or []):
            w_txt = (w.word or '').strip()
            if w_txt:
                words.append(WordTimestamp(word=w_txt, start=float(w.start), end=float(w.end)))
        if not words:
            words = _distribute_words(txt, float(seg.start), float(seg.end))
        segments.append(SpeechSegment(start=float(seg.start), end=float(seg.end), text=txt, words=words))
    return segments


def build_speech_segments_from_energy(
    energy_frames: list[tuple[float, float, float]],
    duration: float,
    threshold: float = 0.18,
    min_seg_sec: float = 0.5,
) -> list[SpeechSegment]:
    """Fallback speech/activity segment detector from RMS energy when no Whisper/VTT text is present."""
    if not energy_frames:
        if duration <= 0:
            return []
        cue = 'WATCH THIS CLIP'
        return [SpeechSegment(start=0.0, end=min(duration, 4.0), text=cue, words=_distribute_words(cue, 0.0, min(duration, 4.0)))]

    active_spans: list[tuple[float, float, float]] = []
    cur_start: float | None = None
    cur_end: float = 0.0
    energies: list[float] = []

    for s, e, rms in energy_frames:
        if rms >= threshold:
            if cur_start is None:
                cur_start = s
            cur_end = e
            energies.append(rms)
        else:
            if cur_start is not None:
                if cur_end - cur_start >= min_seg_sec:
                    active_spans.append((cur_start, cur_end, sum(energies) / len(energies)))
                cur_start = None
                energies = []

    if cur_start is not None and cur_end - cur_start >= min_seg_sec:
        active_spans.append((cur_start, cur_end, sum(energies) / len(energies)))

    if not active_spans:
        # Even quiet audio gets a clean highlight cue on the highest energy point
        best = max(energy_frames, key=lambda x: x[2])
        s = max(0.0, best[0])
        e = min(duration, max(s + 2.5, best[1]))
        active_spans = [(s, e, best[2])]

    cue_phrases = (
        'ГЛАВНЫЙ МОМЕНТ',
        'СМОТРИ ДО КОНЦА',
        'КЛЮЧЕВОЙ ФРАГМЕНТ',
        'ПИК ДИНАМИКИ',
        'ЛУЧШАЯ СЦЕНА',
    )
    segments: list[SpeechSegment] = []
    for idx, (s, e, eng) in enumerate(active_spans):
        # Split long spans into 2.5-3.5s chunks for rhythmic karaoke motion
        cursor = s
        sub_idx = 0
        while cursor < e - 0.35:
            chunk_end = min(e, cursor + 3.0)
            phrase = cue_phrases[(idx + sub_idx) % len(cue_phrases)]
            segments.append(
                SpeechSegment(
                    start=round(cursor, 3),
                    end=round(chunk_end, 3),
                    text=phrase,
                    words=_distribute_words(phrase, cursor, chunk_end),
                    energy=round(eng, 4),
                )
            )
            cursor = chunk_end
            sub_idx += 1
    return segments


def _transcript_dicts_to_segments(items: list[dict[str, Any]]) -> list[SpeechSegment]:
    segments: list[SpeechSegment] = []
    for item in items:
        start = float(item['start'])
        end = float(item['end'])
        text = str(item.get('text') or '').strip()
        if not text or end <= start:
            continue
        raw_words = item.get('words')
        if isinstance(raw_words, list) and raw_words:
            words = [
                WordTimestamp(
                    word=str(w['word']).strip(),
                    start=float(w['start']),
                    end=float(w['end']),
                )
                for w in raw_words
                if str(w.get('word') or '').strip()
            ]
        else:
            words = _distribute_words(text, start, end)
        segments.append(SpeechSegment(start=start, end=end, text=text, words=words))
    return segments


def analyze_speech_timestamps(
    video_path: Path,
    wav_path: Path | None,
    duration: float,
    subtitle_path: Path | None = None,
    transcript_segments: list[dict[str, Any]] | None = None,
    lang: str | None = None,
) -> dict[str, Any]:
    """Run PeakCut acoustic + speech + scene analysis without paid APIs."""
    energy_frames = analyze_wav_energy(wav_path) if wav_path is not None else []
    silence_intervals = detect_silence_intervals(video_path) if wav_path is not None else []
    scene_changes = detect_scene_changes(video_path)

    stt_engine = 'energy_vad'
    segments: list[SpeechSegment] = []

    if transcript_segments:
        segments = _transcript_dicts_to_segments(transcript_segments)
        stt_engine = 'provided_transcript'
    elif subtitle_path is not None and subtitle_path.is_file():
        segments = parse_subtitle_file(subtitle_path)
        if segments:
            stt_engine = 'subtitle_timestamps'

    if not segments and wav_path is not None:
        whisper_segs = _transcribe_with_local_whisper(wav_path, lang=lang)
        if whisper_segs:
            segments = whisper_segs
            stt_engine = 'faster_whisper_local'

    if not segments:
        segments = build_speech_segments_from_energy(energy_frames, duration=duration)
        stt_engine = 'energy_vad'

    return {
        'stt_engine': stt_engine,
        'segments': segments,
        'energy_frames': energy_frames,
        'silence_intervals': silence_intervals,
        'scene_changes': scene_changes,
    }


def _snap_to_silence(t: float, silence_intervals: list[tuple[float, float]], tolerance: float = 0.6) -> float:
    best = t
    best_dist = tolerance
    for s, e in silence_intervals:
        mid = (s + e) / 2.0
        for candidate in (s, mid, e):
            d = abs(candidate - t)
            if d < best_dist:
                best_dist = d
                best = candidate
    return best


def select_peak_segments(
    duration: float,
    energy_frames: list[tuple[float, float, float]],
    speech_segments: list[SpeechSegment],
    silence_intervals: list[tuple[float, float]] | None = None,
    scene_changes: list[float] | None = None,
    clip_duration: float = DEFAULT_CLIP_DURATION,
    max_clips: int = 1,
) -> list[ClipCandidate]:
    """Select top-K non-overlapping dynamic/speech-rich highlight windows (PeakCut scoring)."""
    if duration <= 0:
        raise ValueError(f'Invalid media duration: {duration}')
    target_dur = min(max(clip_duration, MIN_CLIP_DURATION), MAX_CLIP_DURATION, duration)
    if duration <= target_dur + 0.5:
        avg_eng = sum(f[2] for f in energy_frames) / len(energy_frames) if energy_frames else 0.5
        speech_cov = min(1.0, sum(max(0.0, s.end - s.start) for s in speech_segments) / duration)
        return [
            ClipCandidate(
                start=0.0,
                end=round(duration, 3),
                score=round(0.55 * avg_eng + 0.45 * speech_cov, 4),
                energy_score=round(avg_eng, 4),
                speech_ratio=round(speech_cov, 4),
                scene_hits=len(scene_changes or []),
            )
        ]

    silences = silence_intervals or []
    scenes = scene_changes or []
    stride = max(1.0, min(2.5, target_dur / 6.0))
    candidates: list[ClipCandidate] = []

    cursor = 0.0
    while cursor + target_dur <= duration + 1e-6:
        w_start = cursor
        w_end = min(duration, cursor + target_dur)
        win_len = max(w_end - w_start, 0.1)

        win_eng = [f[2] for f in energy_frames if f[1] > w_start and f[0] < w_end]
        if win_eng:
            sorted_eng = sorted(win_eng, reverse=True)
            top_k = max(1, len(sorted_eng) // 5)
            peak_avg = sum(sorted_eng[:top_k]) / top_k
            mean_eng = sum(win_eng) / len(win_eng)
            energy_score = 0.55 * mean_eng + 0.45 * peak_avg
        else:
            energy_score = 0.0

        # Active speech coverage (or non-silence coverage)
        speech_cov = 0.0
        for seg in speech_segments:
            ov = min(w_end, seg.end) - max(w_start, seg.start)
            if ov > 0:
                speech_cov += ov
        if speech_cov == 0.0 and silences:
            silent_cov = sum(max(0.0, min(w_end, se) - max(w_start, ss)) for ss, se in silences)
            speech_ratio = max(0.0, 1.0 - silent_cov / win_len)
        else:
            speech_ratio = min(1.0, speech_cov / win_len)

        scene_hits = sum(1 for sc in scenes if w_start <= sc <= w_end)
        scene_score = min(1.0, scene_hits / 4.0)

        # PeakCut multi-signal score
        score = 0.50 * energy_score + 0.35 * speech_ratio + 0.15 * scene_score
        candidates.append(
            ClipCandidate(
                start=round(w_start, 3),
                end=round(w_end, 3),
                score=round(score, 4),
                energy_score=round(energy_score, 4),
                speech_ratio=round(speech_ratio, 4),
                scene_hits=scene_hits,
            )
        )
        cursor += stride

    # Non-maximum suppression: pick top-K non-overlapping clips
    ranked = sorted(candidates, key=lambda c: (-c.score, c.start))
    selected: list[ClipCandidate] = []
    for cand in ranked:
        if len(selected) >= max_clips:
            break
        overlaps = any(min(cand.end, s.end) - max(cand.start, s.start) > target_dur * 0.2 for s in selected)
        if overlaps:
            continue
        snapped_start = max(0.0, _snap_to_silence(cand.start, silences))
        snapped_end = min(duration, snapped_start + target_dur)
        if snapped_end - snapped_start < MIN_CLIP_DURATION:
            snapped_start = cand.start
            snapped_end = cand.end
        selected.append(
            ClipCandidate(
                start=round(snapped_start, 3),
                end=round(snapped_end, 3),
                score=cand.score,
                energy_score=cand.energy_score,
                speech_ratio=cand.speech_ratio,
                scene_hits=cand.scene_hits,
            )
        )

    return selected


def _format_ass_time(sec: float) -> str:
    clamped = max(0.0, sec)
    cs_total = int(round(clamped * 100))
    cs = cs_total % 100
    s_total = cs_total // 100
    s = s_total % 60
    m_total = s_total // 60
    m = m_total % 60
    h = m_total // 60
    return f'{h}:{m:02d}:{s:02d}.{cs:02d}'


def _escape_ass_text(text: str) -> str:
    return text.replace('\\', '').replace('{', '(').replace('}', ')')


def build_karaoke_ass(
    speech_segments: list[SpeechSegment],
    clip_start: float,
    clip_end: float,
    ass_path: Path,
    max_words_per_line: int = 4,
) -> int:
    """Write an ASS karaoke subtitle file (\\kf word-by-word highlight) for a clip window."""
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
        f'Style: Karaoke,{font_name},64,&H0000D7FF,&H00FFFFFF,&H00080D18,&H88000000,'
        '-1,0,0,0,100,100,1,0,1,5,2,2,64,64,290,1\n\n'
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
        clip_words = _distribute_words('НАУМ СТУДИЯ · SHORTS', 0.0, min(dur, 4.0))

    lines: list[str] = [header]
    dialogue_count = 0
    for idx in range(0, len(clip_words), max_words_per_line):
        chunk = clip_words[idx:idx + max_words_per_line]
        line_start = chunk[0].start
        line_end = max(chunk[-1].end, line_start + 0.35)
        karaoke_parts: list[str] = []
        for w in chunk:
            dur_cs = max(int(round((w.end - w.start) * 100)), 8)
            karaoke_parts.append(f'{{\\kf{dur_cs}}}{_escape_ass_text(w.word)}')
        text_expr = ' '.join(karaoke_parts)
        lines.append(
            f'Dialogue: 0,{_format_ass_time(line_start)},{_format_ass_time(line_end)},Karaoke,,0,0,0,,{text_expr}\n'
        )
        dialogue_count += 1

    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(''.join(lines), encoding='utf-8')
    return dialogue_count


def render_vertical_clip(
    video_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    ass_path: Path,
    meta: dict[str, Any],
    framing: str = 'blur_pad',
    fps: int = REEL_FPS,
) -> dict[str, Any]:
    """Cut [start_sec, end_sec], reframe to 1080x1920 9:16, burn ASS karaoke subtitles atomically."""
    clip_dur = max(end_sec - start_sec, 0.5)
    width = int(meta.get('width') or 0)
    height = int(meta.get('height') or 0)
    has_audio = bool(meta.get('has_audio', True))
    is_portrait = width > 0 and height > 0 and (width / height) <= 0.65

    ass_escaped = _escape_ffmpeg_filter_path(ass_path)
    if framing == 'center_crop' or is_portrait:
        vf = (
            f"[0:v]scale={REEL_WIDTH}:{REEL_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={REEL_WIDTH}:{REEL_HEIGHT},setsar=1,"
            f"ass='{ass_escaped}',format=yuv420p[v]"
        )
    else:
        vf = (
            f"[0:v]split=2[bg_src][fg_src];"
            f"[bg_src]scale={REEL_WIDTH}:{REEL_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={REEL_WIDTH}:{REEL_HEIGHT},boxblur=20:4,eq=brightness=-0.08[bg];"
            f"[fg_src]scale={REEL_WIDTH}:-2:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1,"
            f"ass='{ass_escaped}',format=yuv420p[v]"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix='.clip_', suffix='.tmp.mp4', dir=str(output_path.parent))
    os.close(fd)
    tmp_mp4 = Path(tmp_name)

    try:
        cmd = [
            'ffmpeg',
            '-y',
            '-ss', f'{start_sec:.3f}',
            '-t', f'{clip_dur:.3f}',
            '-i', str(video_path),
            '-filter_complex', vf,
            '-map', '[v]',
        ]
        if has_audio:
            cmd.extend(['-map', '0:a:0?', '-c:a', 'aac', '-b:a', '128k', '-ac', '2'])
        else:
            cmd.append('-an')
        cmd.extend([
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-crf', '22',
            '-r', str(fps),
            '-movflags', '+faststart',
            '-f', 'mp4',
            str(tmp_mp4),
        ])
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f'ffmpeg render failed: {res.stderr[-400:]}')

        out_meta = probe_media(tmp_mp4)
        if out_meta['width'] != REEL_WIDTH or out_meta['height'] != REEL_HEIGHT:
            raise RuntimeError(
                f"Unexpected output resolution {out_meta['width']}x{out_meta['height']}"
            )
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


def clip_video(
    source: str | Path,
    output_dir: Path,
    clip_duration: float = DEFAULT_CLIP_DURATION,
    max_clips: int = 1,
    framing: str = 'blur_pad',
    lang: str | None = None,
    transcript_segments: list[dict[str, Any]] | None = None,
    subtitle_path: Path | None = None,
    download_fn: Callable[[str, Path], tuple[Path, Path | None]] = download_video_from_url,
) -> dict[str, Any]:
    """End-to-end PeakCut clipper: accept file or URL, score peaks, cut 9:16 shorts with karaoke subs."""
    source_str = str(source).strip()
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix='naum_clipper_') as tmp_str:
        tmp_dir = Path(tmp_str)
        downloaded_sub: Path | None = None

        if is_http_url(source_str):
            source_type = 'url'
            video_path, downloaded_sub = download_fn(source_str, tmp_dir / 'dl')
        else:
            source_type = 'file'
            video_path = Path(source_str).expanduser().resolve()
            if not video_path.is_file():
                raise FileNotFoundError(f'Input video not found: {source_str}')

        effective_sub = subtitle_path if subtitle_path is not None else downloaded_sub
        meta = probe_media(video_path)
        wav_path: Path | None = tmp_dir / 'audio.wav'
        if not meta['has_audio'] or not extract_audio_wav(video_path, wav_path):
            wav_path = None

        analysis = analyze_speech_timestamps(
            video_path=video_path,
            wav_path=wav_path,
            duration=meta['duration'],
            subtitle_path=effective_sub,
            transcript_segments=transcript_segments,
            lang=lang,
        )

        candidates = select_peak_segments(
            duration=meta['duration'],
            energy_frames=analysis['energy_frames'],
            speech_segments=analysis['segments'],
            silence_intervals=analysis['silence_intervals'],
            scene_changes=analysis['scene_changes'],
            clip_duration=clip_duration,
            max_clips=max_clips,
        )

        stem = re.sub(r'[^a-zA-Z0-9_-]+', '_', video_path.stem).strip('_')[:32] or 'clip'
        rendered: list[dict[str, Any]] = []
        for idx, cand in enumerate(candidates, start=1):
            ass_file = tmp_dir / f'sub_{idx}.ass'
            sub_count = build_karaoke_ass(
                speech_segments=analysis['segments'],
                clip_start=cand.start,
                clip_end=cand.end,
                ass_path=ass_file,
            )
            out_mp4 = output_dir / f'{stem}_short_{idx:02d}.mp4'
            clip_info = render_vertical_clip(
                video_path=video_path,
                output_path=out_mp4,
                start_sec=cand.start,
                end_sec=cand.end,
                ass_path=ass_file,
                meta=meta,
                framing=framing,
            )
            clip_info['index'] = idx
            clip_info['score'] = cand.score
            clip_info['energy_score'] = cand.energy_score
            clip_info['speech_ratio'] = cand.speech_ratio
            clip_info['scene_hits'] = cand.scene_hits
            clip_info['subtitles_count'] = sub_count
            rendered.append(clip_info)

    return {
        'ok': True,
        'source': source_str,
        'source_type': source_type,
        'duration_sec': round(meta['duration'], 3),
        'stt_engine': analysis['stt_engine'],
        'clips': rendered,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description='PeakCut-based 9:16 AI Video Shorts Clipper with karaoke subtitles')
    ap.add_argument('--input', required=True, help='Path to local video file or http(s) URL')
    ap.add_argument('--output-dir', default='/var/lib/naum-studio/clips', help='Directory for output 9:16 MP4 clips')
    ap.add_argument('--duration', type=float, default=DEFAULT_CLIP_DURATION, help='Target clip duration in seconds')
    ap.add_argument('--max-clips', type=int, default=1, help='Maximum number of highlight clips to render')
    ap.add_argument('--framing', choices=('blur_pad', 'center_crop'), default='blur_pad', help='9:16 reframing mode')
    ap.add_argument('--lang', default=None, help='Optional language hint for local Whisper (e.g. ru, uk, en)')
    ap.add_argument('--subs-json', default=None, help='Optional JSON file with [{start, end, text}] transcript')
    ap.add_argument('--subtitle-file', default=None, help='Optional .srt/.vtt file')
    args = ap.parse_args()

    transcript = None
    if args.subs_json:
        transcript = json.loads(Path(args.subs_json).read_text(encoding='utf-8'))

    res = clip_video(
        source=args.input,
        output_dir=Path(args.output_dir),
        clip_duration=args.duration,
        max_clips=args.max_clips,
        framing=args.framing,
        lang=args.lang,
        transcript_segments=transcript,
        subtitle_path=Path(args.subtitle_file) if args.subtitle_file else None,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
