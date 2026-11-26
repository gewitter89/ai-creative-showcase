#!/usr/bin/env python3
"""Allowlisted static HTTP server for NAUM AI Creative Studio storefront on VPS."""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
import socketserver
from pathlib import Path
from urllib.parse import unquote, urlparse

PUBLIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
    '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
    '/catalog.json': ('catalog.json', 'application/json; charset=utf-8'),
}
ASSET_PREVIEW_RE = re.compile(r'^/assets/(newyear|photoshop|3d)/([a-z0-9][a-z0-9-]{2,40})/preview\.webp$')


def resolve_public_target(repo_root: Path, request_target: str) -> tuple[Path, str] | None:
    """Resolve an HTTP request path to an allowlisted public file, or return None."""
    parsed = urlparse(request_target)
    raw_path = unquote(parsed.path)
    # Strip optional /studio prefix when behind reverse proxy without strip_prefix
    if raw_path == '/studio':
        raw_path = '/'
    elif raw_path.startswith('/studio/'):
        raw_path = raw_path[len('/studio'):]

    root = repo_root.resolve()
    if raw_path in PUBLIC_FILES:
        rel, ctype = PUBLIC_FILES[raw_path]
        target = (root / rel).resolve()
        if target.is_relative_to(root) and target.is_file():
            return target, ctype
        return None

    m = ASSET_PREVIEW_RE.fullmatch(raw_path)
    if m:
        category, item_id = m.groups()
        assets_root = (root / 'assets').resolve()
        target = (assets_root / category / item_id / 'preview.webp').resolve()
        if target.is_relative_to(assets_root) and target.is_file():
            return target, 'image/webp'
    return None


class StorefrontHandler(http.server.BaseHTTPRequestHandler):
    """Minimal allowlisted read-only HTTP handler for the commercial storefront."""

    server_version = 'NaumStorefront/1.0'

    def __init__(self, *args, repo_root: Path, **kwargs) -> None:
        self.repo_root = repo_root
        super().__init__(*args, **kwargs)

    def _serve(self, head_only: bool = False) -> None:
        resolved = resolve_public_target(self.repo_root, self.path)
        if resolved is None:
            body = b'Not Found\n'
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return

        file_path, ctype = resolved
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'public, max-age=300')
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_GET(self) -> None:
        self._serve(head_only=False)

    def do_HEAD(self) -> None:
        self._serve(head_only=True)

    def log_message(self, format: str, *args) -> None:
        return


class ReusableThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(repo_root: Path, host: str = '127.0.0.1', port: int = 8090) -> ReusableThreadingHTTPServer:
    """Create a configured storefront HTTP server instance."""
    handler = functools.partial(StorefrontHandler, repo_root=repo_root.resolve())
    return ReusableThreadingHTTPServer((host, port), handler)


def main() -> None:
    ap = argparse.ArgumentParser(description='Serve NAUM AI Creative Studio storefront from VPS')
    ap.add_argument('--repo', default=os.environ.get('STUDIO_REPO', '/opt/ai-creative-showcase'))
    ap.add_argument('--host', default=os.environ.get('STUDIO_WEB_HOST', '127.0.0.1'))
    ap.add_argument('--port', type=int, default=int(os.environ.get('STUDIO_WEB_PORT', '8090')))
    args = ap.parse_args()

    srv = create_server(Path(args.repo), host=args.host, port=args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
