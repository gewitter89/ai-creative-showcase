#!/usr/bin/env python3
"""Minimal Telegram intake bot for NAUM AI Creative Studio (requires STUDIO_BOT_TOKEN)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from ingest_asset import ingest_asset


def api_call(token: str, method: str, payload: dict | None = None) -> dict:
    url = f'https://api.telegram.org/bot{token}/{method}'
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=35) as resp:
        return json.loads(resp.read().decode('utf-8'))


def main() -> None:
    token = os.environ.get('STUDIO_BOT_TOKEN', '').strip()
    if not token:
        raise SystemExit('STUDIO_BOT_TOKEN is not set')
    allowed = {x.strip() for x in os.environ.get('STUDIO_OWNER_IDS', '').split(',') if x.strip()}
    repo = Path(os.environ.get('STUDIO_REPO', '/opt/ai-creative-showcase')).resolve()
    private_dir = Path(os.environ.get('STUDIO_PRIVATE_DIR', '/var/lib/naum-studio/originals'))
    auto_push = os.environ.get('STUDIO_AUTO_PUSH', '0') == '1'

    offset = 0
    while True:
        try:
            updates = api_call(token, f'getUpdates?timeout=25&offset={offset}').get('result', [])
            for upd in updates:
                offset = upd['update_id'] + 1
                msg = upd.get('message') or {}
                uid = str((msg.get('from') or {}).get('id') or '')
                chat_id = (msg.get('chat') or {}).get('id')
                if not chat_id or (allowed and uid not in allowed):
                    continue
                text = (msg.get('text') or '').strip()
                if text == '/items':
                    cat = json.loads((repo / 'catalog.json').read_text(encoding='utf-8'))
                    lines = [f"• {x['id']} — {x['title']} [{x.get('status','черновик')}]" for x in cat.get('items', [])]
                    api_call(token, 'sendMessage', {'chat_id': chat_id, 'text': 'Товары:\n' + '\n'.join(lines)})
                    continue
                doc = msg.get('document')
                photo = (msg.get('photo') or [None])[-1]
                caption = (msg.get('caption') or '').strip().split()[0] if msg.get('caption') else ''
                file_id = (doc or photo or {}).get('file_id')
                if file_id and caption:
                    finfo = api_call(token, f'getFile?file_id={file_id}')['result']
                    dl_url = f"https://api.telegram.org/file/bot{token}/{finfo['file_path']}"
                    suffix = Path(finfo['file_path']).suffix or '.png'
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                        urllib.request.urlretrieve(dl_url, tmp.name)
                        tmp_path = Path(tmp.name)
                    try:
                        res = ingest_asset(repo, caption, tmp_path, private_dir)
                        if auto_push:
                            subprocess.run(['git', '-C', str(repo), 'add', 'assets', 'catalog.json', 'generation_manifest.json'], check=True)
                            subprocess.run(['git', '-C', str(repo), 'commit', '-m', f"assets: ingest {caption} preview (qa_pending)"], check=True)
                            subprocess.run(['git', '-C', str(repo), 'push'], check=True)
                        api_call(token, 'sendMessage', {'chat_id': chat_id, 'text': f"OK {res['item_id']}: {res['preview_path']} ({res['preview_size']}), статус: {res['catalog_status']}"})
                    except Exception as exc:
                        api_call(token, 'sendMessage', {'chat_id': chat_id, 'text': f"Ошибка: {exc}"})
                    finally:
                        tmp_path.unlink(missing_ok=True)
        except Exception:
            time.sleep(5)


if __name__ == '__main__':
    main()
