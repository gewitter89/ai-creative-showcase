#!/usr/bin/env python3
"""Minimal Telegram intake bot for NAUM AI Creative Studio (requires STUDIO_BOT_TOKEN and STUDIO_OWNER_IDS)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Callable, Mapping

from ingest_asset import MAX_INPUT_BYTES, ingest_asset

ALLOWED_PUSH_BRANCH = 'feat/production-batch-01'
FORBIDDEN_PUSH_BRANCHES = frozenset({'main', 'master', 'HEAD'})


@dataclass(frozen=True)
class BotConfig:
    """Runtime configuration for studio_bot."""

    token: str
    owner_ids: frozenset[str]
    repo: Path
    private_dir: Path
    auto_push: bool
    push_branch: str = ALLOWED_PUSH_BRANCH


def parse_owner_ids(raw: str) -> frozenset[str]:
    """Parse comma-separated Telegram numeric user IDs."""
    ids: set[str] = set()
    for part in raw.split(','):
        val = part.strip()
        if val:
            if not val.isdigit():
                raise ValueError(f'Invalid owner ID in STUDIO_OWNER_IDS: {val!r}')
            ids.add(val)
    return frozenset(ids)


def load_config(environ: Mapping[str, str] | None = None) -> BotConfig:
    """Load and validate fail-closed bot configuration from environment."""
    env = os.environ if environ is None else environ
    token = env.get('STUDIO_BOT_TOKEN', '').strip()
    if not token:
        raise ValueError('STUDIO_BOT_TOKEN is not set')

    owner_ids = parse_owner_ids(env.get('STUDIO_OWNER_IDS', ''))
    if not owner_ids:
        raise ValueError('STUDIO_OWNER_IDS is empty: refusing to start admin intake (fail-closed)')

    push_branch = (env.get('STUDIO_PUSH_BRANCH', ALLOWED_PUSH_BRANCH) or ALLOWED_PUSH_BRANCH).strip()
    if push_branch != ALLOWED_PUSH_BRANCH or push_branch in FORBIDDEN_PUSH_BRANCHES:
        raise ValueError(
            f'Invalid STUDIO_PUSH_BRANCH {push_branch!r}: only {ALLOWED_PUSH_BRANCH!r} is permitted'
        )

    repo = Path(env.get('STUDIO_REPO', '/opt/ai-creative-showcase')).resolve()
    private_dir = Path(env.get('STUDIO_PRIVATE_DIR', '/var/lib/naum-studio/originals'))
    auto_push = env.get('STUDIO_AUTO_PUSH', '0') == '1'
    return BotConfig(
        token=token,
        owner_ids=owner_ids,
        repo=repo,
        private_dir=private_dir,
        auto_push=auto_push,
        push_branch=push_branch,
    )


def get_sender_id(msg: dict) -> str:
    """Extract Telegram sender user ID string if present."""
    uid = (msg.get('from') or {}).get('id')
    return str(uid).strip() if uid is not None else ''


def is_private_chat(msg: dict) -> bool:
    """Return True only when the message comes from a 1:1 private chat."""
    return (msg.get('chat') or {}).get('type') == 'private'


def is_owner(msg: dict, owner_ids: AbstractSet[str]) -> bool:
    """Return True if message sender is a configured studio owner (non-empty allowlist)."""
    if not owner_ids or msg.get('sender_chat'):
        return False
    uid = get_sender_id(msg)
    return bool(uid) and uid in owner_ids


def is_admin_message(msg: dict, owner_ids: AbstractSet[str]) -> bool:
    """Return True only for configured owners messaging in a private chat."""
    return is_owner(msg, owner_ids) and is_private_chat(msg)


def verify_push_branch(repo: Path, allowed_branch: str = ALLOWED_PUSH_BRANCH) -> str:
    """Verify git HEAD matches the permitted target branch before committing or pushing."""
    res = subprocess.run(
        ['git', '-C', str(repo), 'rev-parse', '--abbrev-ref', 'HEAD'],
        capture_output=True,
        text=True,
        check=True,
    )
    branch = res.stdout.strip()
    if branch in FORBIDDEN_PUSH_BRANCHES or branch != allowed_branch:
        raise RuntimeError(
            f'Refusing git push on branch {branch!r}; only {allowed_branch!r} is allowed'
        )
    return branch


def git_push_preview(
    repo: Path,
    preview_rel_path: str,
    item_id: str,
    allowed_branch: str = ALLOWED_PUSH_BRANCH,
) -> None:
    """Stage preview & metadata, commit, and push exclusively to allowed_branch."""
    branch = verify_push_branch(repo, allowed_branch=allowed_branch)
    subprocess.run(
        ['git', '-C', str(repo), 'add', '--', preview_rel_path, 'catalog.json', 'generation_manifest.json'],
        check=True,
    )
    diff_check = subprocess.run(['git', '-C', str(repo), 'diff', '--cached', '--quiet'])
    if diff_check.returncode != 0:
        subprocess.run(
            ['git', '-C', str(repo), 'commit', '-m', f'assets: ingest {item_id} preview (qa_pending)'],
            check=True,
        )
    subprocess.run(['git', '-C', str(repo), 'push', 'origin', branch], check=True)


def api_call(token: str, method: str, payload: dict | None = None) -> dict:
    """Call Telegram Bot API with timeout."""
    url = f'https://api.telegram.org/bot{token}/{method}'
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=35) as resp:
        return json.loads(resp.read().decode('utf-8'))


def download_telegram_file(token: str, file_path: str, dest_path: Path, max_bytes: int = MAX_INPUT_BYTES) -> None:
    """Stream-download a Telegram file with timeout and byte-limit enforcement."""
    dl_url = f'https://api.telegram.org/file/bot{token}/{file_path}'
    total = 0
    with urllib.request.urlopen(dl_url, timeout=60) as resp, dest_path.open('wb') as out:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f'Downloaded file exceeds limit {max_bytes} bytes')
            out.write(chunk)


def _sanitize_error(exc: Exception, token: str) -> str:
    """Redact bot token before sending error details to Telegram."""
    text = str(exc)
    return text.replace(token, '***') if token else text


def process_update(
    upd: dict,
    cfg: BotConfig,
    api_fn: Callable[[str, str, dict | None], dict] = api_call,
    ingest_fn: Callable[..., dict] = ingest_asset,
    download_fn: Callable[[str, str, Path], None] = download_telegram_file,
    push_fn: Callable[[Path, str, str, str], None] = git_push_preview,
) -> None:
    """Process a single Telegram update with strict role separation and private-chat admin checks."""
    if not cfg.owner_ids:
        raise RuntimeError('STUDIO_OWNER_IDS is empty (fail-closed)')

    msg = upd.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id')
    if not chat_id:
        return

    text = (msg.get('text') or '').strip()
    owner = is_owner(msg, cfg.owner_ids)
    private = is_private_chat(msg)
    admin = owner and private

    if text.startswith('/start'):
        arg = text.split(maxsplit=1)[1] if ' ' in text else ''
        if admin:
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': 'NAUM AI Creative Studio активен. Отправьте фото/документ с подписью ID товара (например: ny-kyiv-01) или /items.',
                },
            )
        elif private:
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': f'Здравствуйте! Заявка по NAUM AI Creative Studio ({arg or "общий запрос"}) принята. Оставьте ваш контакт или напишите детали.',
                },
            )
        return

    # Non-owners (customers) never reach admin commands or asset ingestion
    if not owner:
        return

    doc = msg.get('document')
    photo = (msg.get('photo') or [None])[-1]
    caption = (msg.get('caption') or '').strip().split()[0] if msg.get('caption') else ''
    media_obj = doc or photo or {}
    file_id = media_obj.get('file_id')

    # Owners in non-private chats (groups/channels) cannot run admin commands or upload assets
    if not private:
        if text == '/items' or (file_id and caption):
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': 'Админ-команды и загрузка ассетов разрешены только в приватном чате с ботом.',
                },
            )
        return

    if text == '/items':
        cat = json.loads((cfg.repo / 'catalog.json').read_text(encoding='utf-8'))
        lines = [f"• {x['id']} — {x['title']} [{x.get('status', 'черновик')}]" for x in cat.get('items', [])]
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': 'Товары:\n' + '\n'.join(lines)})
        return

    if file_id and caption:
        file_size = media_obj.get('file_size')
        if isinstance(file_size, int) and file_size > MAX_INPUT_BYTES:
            api_fn(
                cfg.token,
                'sendMessage',
                {'chat_id': chat_id, 'text': f'Ошибка: файл превышает лимит {MAX_INPUT_BYTES} байт'},
            )
            return

        finfo = api_fn(cfg.token, f'getFile?file_id={file_id}', None)['result']
        suffix = Path(finfo['file_path']).suffix or '.png'
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            download_fn(cfg.token, finfo['file_path'], tmp_path)
            res = ingest_fn(cfg.repo, caption, tmp_path, cfg.private_dir)
            if cfg.auto_push:
                push_fn(cfg.repo, res['preview_path'], res['item_id'], cfg.push_branch)
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': f"OK {res['item_id']}: {res['preview_path']} ({res['preview_size']}), статус: {res['catalog_status']}",
                },
            )
        except Exception as exc:
            api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': f'Ошибка: {_sanitize_error(exc, cfg.token)}'})
        finally:
            tmp_path.unlink(missing_ok=True)


def main() -> None:
    try:
        cfg = load_config()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    offset = 0
    while True:
        try:
            updates = api_call(cfg.token, f'getUpdates?timeout=25&offset={offset}').get('result', [])
            for upd in updates:
                offset = upd['update_id'] + 1
                process_update(upd, cfg)
        except Exception:
            time.sleep(5)


if __name__ == '__main__':
    main()
