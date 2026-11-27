#!/usr/bin/env python3
"""Local-only zero-dependency HTTP server & JSON API for NeonClipping MVP (0 UAH)."""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import re
import shutil
import socketserver
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from neon_clipper import (
    CAPTION_STYLES,
    FRAMING_MODES,
    fit_preference_weights,
    neon_clip_pipeline,
    record_clip_feedback,
)

SAFE_CLIP_FILE_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{1,120}\.(mp4|srt|vtt)$')
MAX_JSON_BODY_BYTES = 64 * 1024


def _check_ollama_ready(url: str = 'http://127.0.0.1:11434/api/tags') -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def list_saved_clips(output_dir: Path) -> list[dict[str, Any]]:
    """Return saved clip metadata records ordered newest-first."""
    if not output_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for meta_file in sorted(output_dir.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            obj = json.loads(meta_file.read_text(encoding='utf-8'))
            if isinstance(obj, dict) and obj.get('mp4_name'):
                items.append(obj)
        except (json.JSONDecodeError, OSError):
            continue
    return items


def resolve_clip_asset(output_dir: Path, req_path: str) -> tuple[Path, str] | None:
    """Resolve /clips/<filename> strictly inside output_dir for allowlisted extensions."""
    parsed = urlparse(req_path)
    raw = unquote(parsed.path)
    if not raw.startswith('/clips/'):
        return None
    name = raw[len('/clips/'):]
    if not SAFE_CLIP_FILE_RE.fullmatch(name):
        return None
    root = output_dir.resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    ext = target.suffix.lower()
    ctype = {
        '.mp4': 'video/mp4',
        '.srt': 'application/x-subrip; charset=utf-8',
        '.vtt': 'text/vtt; charset=utf-8',
    }[ext]
    return target, ctype


class NeonClipperHandler(http.server.BaseHTTPRequestHandler):
    """Local HTTP handler serving clipper.html, /api/* endpoints, and /clips/* assets."""

    server_version = 'NeonClipperMVP/1.0'

    def __init__(self, *args, repo_root: Path, output_dir: Path, **kwargs) -> None:
        self.repo_root = repo_root
        self.output_dir = output_dir
        super().__init__(*args, **kwargs)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0 or length > MAX_JSON_BODY_BYTES:
            raise ValueError('Invalid JSON body length')
        raw = self.rfile.read(length)
        obj = json.loads(raw.decode('utf-8'))
        if not isinstance(obj, dict):
            raise ValueError('JSON body must be an object')
        return obj

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ('/', '/clipper.html'):
            html_file = (self.repo_root / 'clipper.html').resolve()
            if not html_file.is_file():
                self._send_json(404, {'ok': False, 'error': 'clipper.html not found'})
                return
            data = html_file.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            self.end_headers()
            self.wfile.write(data)
            return

        if path == '/api/health':
            fb_path = self.output_dir / 'feedback.jsonl'
            weights, fb_stats = fit_preference_weights(fb_path)
            self._send_json(
                200,
                {
                    'ok': True,
                    'ffmpeg': shutil.which('ffmpeg') is not None,
                    'yt_dlp': shutil.which('yt-dlp') is not None,
                    'ollama': _check_ollama_ready(),
                    'styles': list(CAPTION_STYLES.keys()),
                    'framing_modes': list(FRAMING_MODES),
                    'weights': weights,
                    'feedback_stats': fb_stats,
                },
            )
            return

        if path == '/api/clips':
            fb_path = self.output_dir / 'feedback.jsonl'
            weights, fb_stats = fit_preference_weights(fb_path)
            self._send_json(
                200,
                {
                    'ok': True,
                    'clips': list_saved_clips(self.output_dir),
                    'weights': weights,
                    'feedback_stats': fb_stats,
                },
            )
            return

        resolved = resolve_clip_asset(self.output_dir, self.path)
        if resolved is None:
            self._send_json(404, {'ok': False, 'error': 'Not Found'})
            return

        file_path, ctype = resolved
        size = file_path.stat().st_size
        range_hdr = self.headers.get('Range')
        start, end = 0, size - 1
        status = 200
        if range_hdr and range_hdr.startswith('bytes='):
            m = re.match(r'^bytes=(\d*)-(\d*)$', range_hdr.strip())
            if m:
                s_str, e_str = m.groups()
                if s_str:
                    start = min(int(s_str), max(0, size - 1))
                if e_str:
                    end = min(int(e_str), max(0, size - 1))
                if end >= start:
                    status = 206

        chunk_len = (end - start + 1) if size > 0 else 0
        try:
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(chunk_len))
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            with file_path.open('rb') as fh:
                fh.seek(start)
                self.wfile.write(fh.read(chunk_len))
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {'ok': False, 'error': str(exc)})
            return

        if path == '/api/clip':
            source = str(body.get('source') or '').strip()
            if not source:
                self._send_json(400, {'ok': False, 'error': 'Field "source" is required'})
                return
            try:
                res = neon_clip_pipeline(
                    source=source,
                    output_dir=self.output_dir,
                    mode=str(body.get('mode') or 'fast'),
                    min_duration=float(body.get('min_duration') or 15.0),
                    max_duration=float(body.get('max_duration') or 60.0),
                    max_clips=int(body.get('max_clips') or 2),
                    caption_style=str(body.get('style') or 'neon'),
                    framing=str(body.get('framing') or 'blur_pad'),
                    opening_heading=bool(body.get('opening_heading', True)),
                    use_local_llm=bool(body.get('use_llm', False)),
                )
                self._send_json(200, res)
            except Exception as exc:
                self._send_json(400, {'ok': False, 'error': str(exc)})
            return

        if path == '/api/feedback':
            try:
                rec = record_clip_feedback(
                    feedback_path=self.output_dir / 'feedback.jsonl',
                    clip_id=str(body.get('clip_id') or ''),
                    rating=int(body.get('rating') or 0),
                    features=dict(body.get('features') or {}),
                    source_url=str(body.get('source') or ''),
                )
                weights, stats = fit_preference_weights(self.output_dir / 'feedback.jsonl')
                self._send_json(200, {'ok': True, 'record': rec, 'weights': weights, 'feedback_stats': stats})
            except Exception as exc:
                self._send_json(400, {'ok': False, 'error': str(exc)})
            return

        self._send_json(404, {'ok': False, 'error': 'Unknown API endpoint'})

    def log_message(self, format: str, *args) -> None:
        return


class ReusableThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(repo_root: Path, output_dir: Path, host: str = '127.0.0.1', port: int = 8770) -> ReusableThreadingHTTPServer:
    """Create configured NeonClipping local HTTP server."""
    output_dir.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(NeonClipperHandler, repo_root=repo_root.resolve(), output_dir=output_dir.resolve())
    return ReusableThreadingHTTPServer((host, port), handler)


def main() -> None:
    ap = argparse.ArgumentParser(description='Run NeonClipping MVP local web UI & API')
    ap.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument('--output-dir', default=os.environ.get('NEONCLIP_OUTPUT_DIR', '/tmp/neon_clips'))
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=int(os.environ.get('NEONCLIP_PORT', '8770')))
    args = ap.parse_args()

    srv = create_server(Path(args.repo), Path(args.output_dir), host=args.host, port=args.port)
    print(f'NeonClipping MVP listening on http://{args.host}:{args.port}')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
