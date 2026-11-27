#!/usr/bin/env python3
"""Telegram store and intake bot for NAUM AI Creative Studio."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Callable, Mapping

from build_packages import DEFAULT_PACKAGES_DIR, DEMO_PRODUCT_ID, build_demo_postcard_package
from ingest_asset import ID_RE, MAX_INPUT_BYTES, ingest_asset, sha256_file
from kids_greeting import (
    generate_nicholas_audio,
    parse_greeting_args,
    render_certificate,
    render_video_greeting,
)
from store_db import (
    CLIP_FREE_TRIAL_LIMIT,
    CLIP_PRICE_XTR,
    CLIP_PRODUCT_ID,
    DEFAULT_DB_PATH,
    KIDS_VIDEO_PRICE_UAH,
    KIDS_VIDEO_PRICE_XTR,
    KIDS_VIDEO_PRODUCT_ID,
    StoreDB,
)
from package_version import staged_order_package
from video_clipper import ALLOWED_VIDEO_EXTENSIONS, clip_video, is_http_url

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
    db_path: Path | None = None
    packages_dir: Path | None = None
    payments_enabled: bool = False


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
    db_path = Path(env.get('STUDIO_DB_PATH', DEFAULT_DB_PATH))
    packages_dir = Path(env.get('STUDIO_PACKAGES_DIR', DEFAULT_PACKAGES_DIR))
    auto_push = env.get('STUDIO_AUTO_PUSH', '0') == '1'
    payments_enabled = env.get('STUDIO_PAYMENTS_ENABLED', '0') == '1'
    return BotConfig(
        token=token,
        owner_ids=owner_ids,
        repo=repo,
        private_dir=private_dir,
        auto_push=auto_push,
        push_branch=push_branch,
        db_path=db_path,
        packages_dir=packages_dir,
        payments_enabled=payments_enabled,
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


def send_telegram_document(
    token: str,
    chat_id: int | str,
    file_path: Path,
    caption: str = '',
) -> dict:
    """Upload a local file to Telegram using multipart/form-data sendDocument."""
    boundary = f'----naumBoundary{uuid.uuid4().hex}'
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'.encode('utf-8')
        )

    add_field('chat_id', str(chat_id))
    if caption:
        add_field('caption', caption[:1024])

    file_bytes = file_path.read_bytes()
    parts.append(
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="document"; filename="{file_path.name}"\r\n'
        f'Content-Type: application/zip\r\n\r\n'.encode('utf-8')
    )
    parts.append(file_bytes)
    parts.append(f'\r\n--{boundary}--\r\n'.encode('utf-8'))

    body = b''.join(parts)
    url = f'https://api.telegram.org/bot{token}/sendDocument'
    req = urllib.request.Request(
        url,
        data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        res = json.loads(resp.read().decode('utf-8'))
    if not res.get('ok'):
        raise RuntimeError(f"Telegram sendDocument failed: {res.get('description', 'unknown error')}")
    return res


def send_telegram_video(
    token: str,
    chat_id: int | str,
    file_path: Path,
    caption: str = '',
) -> dict:
    """Upload a local MP4 video to Telegram using multipart/form-data sendVideo."""
    boundary = f'----naumVideo{uuid.uuid4().hex}'
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'.encode('utf-8')
        )

    add_field('chat_id', str(chat_id))
    add_field('supports_streaming', 'true')
    add_field('width', '1080')
    add_field('height', '1920')
    if caption:
        add_field('caption', caption[:1024])

    file_bytes = file_path.read_bytes()
    parts.append(
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="video"; filename="{file_path.name}"\r\n'
        f'Content-Type: video/mp4\r\n\r\n'.encode('utf-8')
    )
    parts.append(file_bytes)
    parts.append(f'\r\n--{boundary}--\r\n'.encode('utf-8'))

    body = b''.join(parts)
    url = f'https://api.telegram.org/bot{token}/sendVideo'
    req = urllib.request.Request(
        url,
        data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        res = json.loads(resp.read().decode('utf-8'))
    if not res.get('ok'):
        raise RuntimeError(f"Telegram sendVideo failed: {res.get('description', 'unknown error')}")
    return res


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


def _resolved_packages_dir(cfg: BotConfig) -> Path:
    return (cfg.packages_dir or Path(DEFAULT_PACKAGES_DIR)).resolve()


def _load_catalog_and_manifest(repo: Path) -> tuple[list[dict], list[dict]]:
    cat_path = repo / 'catalog.json'
    man_path = repo / 'generation_manifest.json'
    catalog = json.loads(cat_path.read_text(encoding='utf-8')) if cat_path.is_file() else {}
    manifest = json.loads(man_path.read_text(encoding='utf-8')) if man_path.is_file() else {}
    return list(catalog.get('items', [])), list(manifest.get('items', []))


def _load_demo_offer(repo: Path) -> dict:
    cat_path = repo / 'catalog.json'
    catalog = json.loads(cat_path.read_text(encoding='utf-8')) if cat_path.is_file() else {}
    demo = catalog.get('demo_offer') if isinstance(catalog, dict) else None
    if isinstance(demo, dict) and demo.get('id'):
        return demo
    return {
        'id': DEMO_PRODUCT_ID,
        'deeplink': 'demo_ny',
        'title': 'Бесплатная демо-открытка «Киев в новогодних огнях»',
        'description': 'Ознакомительный ZIP-комплект (лид-магнит) с пометкой DEMO.',
        'package_contents': 'ZIP-комплект (4 файла): demo_greeting_ua.png, demo_ny_kyiv.jpg, README.txt, LICENSE.txt',
        'price_uah': 0,
        'price_xtr': 0,
    }


def ensure_store(cfg: BotConfig) -> StoreDB | None:
    """Initialize SQLite store and synchronize catalog/packages if db_path is configured."""
    if cfg.db_path is None:
        return None
    store = StoreDB(cfg.db_path)
    store.init_schema()
    catalog_items, manifest_items = _load_catalog_and_manifest(cfg.repo)
    store.sync_catalog(catalog_items, _resolved_packages_dir(cfg), manifest_items=manifest_items)
    return store


def _safe_package_path(packages_dir: Path, product_id: str) -> Path | None:
    if not ID_RE.fullmatch(product_id):
        return None
    candidate = (packages_dir / f'{product_id}.zip').resolve()
    if not candidate.is_relative_to(packages_dir.resolve()) or not candidate.is_file():
        return None
    return candidate


def _handle_pre_checkout(
    upd: dict,
    cfg: BotConfig,
    store: StoreDB,
    api_fn: Callable[[str, str, dict | None], dict],
) -> None:
    pcq = upd['pre_checkout_query']
    update_id = upd.get('update_id')
    if isinstance(update_id, int) and not store.mark_update_processed(update_id, 'pre_checkout_query'):
        return

    qid = pcq.get('id')
    user_id = str((pcq.get('from') or {}).get('id') or '').strip()
    currency = str(pcq.get('currency') or '').strip()
    total_amount = int(pcq.get('total_amount') or 0)
    invoice_payload = str(pcq.get('invoice_payload') or '').strip()

    ok, reason, _ = store.validate_pre_checkout(
        user_id=user_id,
        currency=currency,
        total_amount=total_amount,
        invoice_payload=invoice_payload,
        packages_dir=_resolved_packages_dir(cfg),
        payments_enabled=cfg.payments_enabled,
    )
    payload: dict[str, object] = {'pre_checkout_query_id': qid, 'ok': ok}
    if not ok:
        payload['error_message'] = reason[:255]
    api_fn(cfg.token, 'answerPreCheckoutQuery', payload)


def _extract_video_file_id(msg_obj: dict) -> str | None:
    """Extract Telegram video or video-document file_id from a message dict."""
    if not isinstance(msg_obj, dict):
        return None
    video = msg_obj.get('video')
    if isinstance(video, dict) and video.get('file_id'):
        return str(video['file_id'])
    doc = msg_obj.get('document')
    if isinstance(doc, dict) and doc.get('file_id'):
        mime = str(doc.get('mime_type') or '').lower()
        fname = str(doc.get('file_name') or '').lower()
        if mime.startswith('video/') or Path(fname).suffix in ALLOWED_VIDEO_EXTENSIONS:
            return str(doc['file_id'])
    return None


def _extract_clip_source(msg: dict, effective_text: str) -> tuple[str, str] | None:
    """Extract ('url', url) or ('file', file_id) from /clip message or reply."""
    parts = effective_text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ''
    if arg:
        if is_http_url(arg):
            return 'url', arg
        raise ValueError('Укажите корректную ссылку http(s):// или прикрепите видео к команде /clip.')

    file_id = _extract_video_file_id(msg)
    if file_id:
        return 'file', file_id

    reply_id = _extract_video_file_id(msg.get('reply_to_message') or {})
    if reply_id:
        return 'file', reply_id
    return None


def _run_and_send_clip(
    source_type: str,
    source_value: str,
    chat_id: int | str,
    cfg: BotConfig,
    api_fn: Callable[[str, str, dict | None], dict],
    download_fn: Callable[[str, str, Path], None],
    send_video_fn: Callable[[str, int | str, Path, str], dict],
    clip_fn: Callable[[str | Path, Path], dict],
    caption: str,
) -> tuple[str, str]:
    """Download/clip source video into 9:16 karaoke MP4, upload to Telegram, and return (sha256, file_id)."""
    with tempfile.TemporaryDirectory(prefix='naum_clip_') as tmp_str:
        tmp_dir = Path(tmp_str)
        clip_input: str | Path
        if source_type == 'file':
            finfo = api_fn(cfg.token, f'getFile?file_id={source_value}', None)['result']
            suffix = Path(finfo['file_path']).suffix or '.mp4'
            src_path = tmp_dir / f'input{suffix}'
            download_fn(cfg.token, finfo['file_path'], src_path)
            clip_input = src_path
        else:
            clip_input = source_value

        out_dir = tmp_dir / 'out'
        res = clip_fn(clip_input, out_dir)
        clips = list(res.get('clips') or [])
        if not clips:
            raise RuntimeError('Клиппер не вернул ни одного ролика')
        out_path = Path(clips[0]['output_path'])
        if not out_path.is_file():
            raise FileNotFoundError(f'Clip output missing: {out_path}')

        out_sha = sha256_file(out_path)
        resp = send_video_fn(cfg.token, chat_id, out_path, caption)
        res_obj = (resp or {}).get('result') or {}
        media_obj = res_obj.get('video') or res_obj.get('document') or {}
        file_id = str(media_obj.get('file_id') or '')
        if not isinstance(resp, dict) or resp.get('ok') is not True or not file_id:
            raise RuntimeError('Telegram did not confirm video delivery')
        return out_sha, file_id


def _deliver_paid_clip_order(
    order: dict,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB,
    api_fn: Callable[[str, str, dict | None], dict],
    download_fn: Callable[[str, str, Path], None],
    send_video_fn: Callable[[str, int | str, Path, str], dict],
    clip_fn: Callable[[str | Path, Path], dict],
) -> None:
    """Render and deliver a paid Stars /clip order idempotently."""
    job = store.reserve_clip_delivery(order['order_id'])
    if job is None:
        return
    try:
        caption = (
            f"🎬 Ваш вертикальный Shorts 9:16 с караоке-субтитрами готов!\n"
            f"Заказ: {order['order_id']} · NAUM AI Creative Studio"
        )
        out_sha, file_id = _run_and_send_clip(
            source_type=job['source_type'],
            source_value=job['source_value'],
            chat_id=chat_id,
            cfg=cfg,
            api_fn=api_fn,
            download_fn=download_fn,
            send_video_fn=send_video_fn,
            clip_fn=clip_fn,
            caption=caption,
        )
        store.create_or_get_delivery(order, Path(f"{job['job_id']}.mp4"), out_sha)
        store.mark_delivery_sent(order['order_id'], telegram_file_id=file_id)
        store.mark_clip_job_delivered(job['job_id'], output_path=f"{job['job_id']}.mp4", telegram_file_id=file_id)
    except Exception as exc:
        err = _sanitize_error(exc, cfg.token)
        store.mark_clip_job_failed(job['job_id'], err)
        store.create_or_get_delivery(order, Path(f"{job['job_id']}.mp4"), 'clip-failed')
        store.mark_delivery_failed(order['order_id'], err)
        api_fn(
            cfg.token,
            'sendMessage',
            {'chat_id': chat_id, 'text': f"Не удалось собрать клип по заказу {order['order_id']}. Обратитесь в /paysupport."},
        )
        return

    try:
        api_fn(
            cfg.token,
            'sendMessage',
            {'chat_id': chat_id, 'text': f"Спасибо за покупку! Клип по заказу {order['order_id']} доставлен. /myorders · /paysupport"},
        )
    except Exception:
        store.log_audit('delivery_notification_failed', actor_id=str(order['user_id']),
                        order_id=order['order_id'], product_id=order['product_id'])


def _deliver_paid_kids_video_order(
    order: dict,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB,
    api_fn: Callable[[str, str, dict | None], dict],
    download_fn: Callable[[str, str, Path], None],
    send_video_fn: Callable[[str, int | str, Path, str], dict],
) -> None:
    """Render and deliver a paid Stars 9:16 personalized kids greeting video idempotently."""
    job = store.reserve_kids_video_delivery(order['order_id'])
    if job is None:
        return
    try:
        with tempfile.TemporaryDirectory(prefix='naum_kids_paid_') as tmp_str:
            tmp_dir = Path(tmp_str)
            audio_path = tmp_dir / 'greeting.mp3'
            video_path = tmp_dir / 'greeting_9_16.mp4'
            photo_path: Path | None = None
            if job.get('photo_file_id'):
                finfo = api_fn(cfg.token, f"getFile?file_id={job['photo_file_id']}", None)['result']
                suffix = Path(finfo['file_path']).suffix or '.jpg'
                photo_path = tmp_dir / f'child{suffix}'
                download_fn(cfg.token, finfo['file_path'], photo_path)

            asyncio.run(
                generate_nicholas_audio(
                    child_name=job['child_name'],
                    good_deed=job.get('good_deed') or '',
                    out_mp3=audio_path,
                    lang=job.get('lang') or 'uk',
                    age=job.get('age'),
                    city=job.get('city') or '',
                    persona=job.get('persona') or 'mykolay',
                )
            )
            render_video_greeting(
                repo_root=cfg.repo,
                child_name=job['child_name'],
                audio_path=audio_path,
                out_video_path=video_path,
                child_photo_path=photo_path,
                age=job.get('age'),
                city=job.get('city') or '',
                good_deed=job.get('good_deed') or '',
                lang=job.get('lang') or 'uk',
                persona=job.get('persona') or 'mykolay',
            )
            caption = (
                f"🎬 Чарівна HD-листівка 9:16 для {job['child_name']} готова!\n"
                f"Замовлення: {order['order_id']} · NAUM AI Creative Studio"
                if (job.get('lang') or 'uk') == 'uk'
                else f"🎬 Волшебная HD-открытка 9:16 для {job['child_name']} готова!\n"
                f"Заказ: {order['order_id']} · NAUM AI Creative Studio"
            )
            resp = send_video_fn(cfg.token, chat_id, video_path, caption)
            res_obj = (resp or {}).get('result') or {}
            media_obj = res_obj.get('video') or res_obj.get('document') or {}
            file_id = str(media_obj.get('file_id') or '')
            if not isinstance(resp, dict) or resp.get('ok') is not True or not file_id:
                raise RuntimeError('Telegram did not confirm video delivery')
            out_sha = sha256_file(video_path)

        store.create_or_get_delivery(order, Path(f"{job['job_id']}.mp4"), out_sha)
        store.mark_delivery_sent(order['order_id'], telegram_file_id=file_id)
        store.mark_kids_job_delivered(job['job_id'], telegram_file_id=file_id)
    except Exception as exc:
        err = _sanitize_error(exc, cfg.token)
        store.mark_kids_job_failed(job['job_id'], err)
        store.create_or_get_delivery(order, Path(f"{job['job_id']}.mp4"), 'kids-video-failed')
        store.mark_delivery_failed(order['order_id'], err)
        api_fn(
            cfg.token,
            'sendMessage',
            {'chat_id': chat_id, 'text': f"Не удалось собрать видео по заказу {order['order_id']}. Обратитесь в /paysupport."},
        )
        return

    try:
        api_fn(
            cfg.token,
            'sendMessage',
            {'chat_id': chat_id, 'text': f"Спасибо за покупку! Видео-поздравление по заказу {order['order_id']} доставлено. /myorders · /paysupport"},
        )
    except Exception:
        store.log_audit('delivery_notification_failed', actor_id=str(order['user_id']),
                        order_id=order['order_id'], product_id=order['product_id'])


def _deliver_paid_order(
    order: dict,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB,
    api_fn: Callable[[str, str, dict | None], dict],
    send_document_fn: Callable[[str, int | str, Path, str], dict],
) -> None:
    if str(chat_id) != str(order['chat_id']) or str(chat_id) != str(order['user_id']):
        return
    if store.get_delivery_by_order(order['order_id']) is not None:
        # Even a legacy pending row may represent an already accepted upload.
        return
    packages_dir = _resolved_packages_dir(cfg)
    version = store.get_order_package_version(order)
    with staged_order_package(packages_dir, order, version) as pkg_path:
        if pkg_path is None:
            store.log_audit('delivery_package_unavailable', actor_id=str(order['user_id']),
                            order_id=order['order_id'], product_id=order['product_id'])
            api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text':
                   f"Архив заказа {order['order_id']} временно недоступен. Обратитесь в /paysupport."})
            return
        if not store.reserve_paid_delivery(order, version):
            return
        try:
            resp = send_document_fn(
                cfg.token, chat_id, pkg_path,
                f"NAUM AI Creative Studio — заказ {order['order_id']} ({order['product_id']})",
            )
            file_id = str((((resp or {}).get('result') or {}).get('document') or {}).get('file_id') or '')
            if not isinstance(resp, dict) or resp.get('ok') is not True or not file_id:
                raise RuntimeError('Telegram did not confirm document delivery')
            store.mark_delivery_sent(order['order_id'], telegram_file_id=file_id)
        except Exception:
            # A remote timeout or local commit failure is ambiguous. Never replay automatically.
            store.log_audit('delivery_needs_reconciliation', actor_id=str(order['user_id']),
                            order_id=order['order_id'], product_id=order['product_id'])
            api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text':
                   f"Доставка заказа {order['order_id']} требует проверки. Обратитесь в /paysupport."})
            return
    # Notification failure must not undo the durable successful delivery.
    try:
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text':
               f"Спасибо за покупку! Заказ {order['order_id']} доставлен. /myorders · /paysupport"})
    except Exception:
        store.log_audit('delivery_notification_failed', actor_id=str(order['user_id']),
                        order_id=order['order_id'], product_id=order['product_id'])


def _handle_successful_payment(
    upd: dict,
    msg: dict,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB,
    api_fn: Callable[[str, str, dict | None], dict],
    send_document_fn: Callable[[str, int | str, Path, str], dict],
    download_fn: Callable[[str, str, Path], None] = download_telegram_file,
    send_video_fn: Callable[[str, int | str, Path, str], dict] = send_telegram_video,
    clip_fn: Callable[[str | Path, Path], dict] = clip_video,
) -> None:
    if not is_private_chat(msg):
        return
    # Payment identity, not a precommitted update marker, controls replay.
    # A crash before receipt commit must permit this update to be processed again.
    sp = msg['successful_payment']
    user_id = get_sender_id(msg)
    currency = str(sp.get('currency') or '').strip()
    total_amount = int(sp.get('total_amount') or 0)
    invoice_payload = str(sp.get('invoice_payload') or '').strip()
    telegram_charge_id = str(sp.get('telegram_payment_charge_id') or '').strip()
    provider_charge_id = str(sp.get('provider_payment_charge_id') or '').strip()

    outcome, order = store.record_successful_payment(
        user_id=user_id,
        currency=currency,
        total_amount=total_amount,
        invoice_payload=invoice_payload,
        telegram_charge_id=telegram_charge_id,
        provider_charge_id=provider_charge_id,
    )
    if outcome not in ('recorded', 'duplicate_charge') or order is None:
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': 'Не удалось подтвердить параметры платежа на сервере. Обратитесь в /paysupport.',
            },
        )
        return

    if outcome == 'duplicate_charge':
        existing_delivery = store.get_delivery_by_order(order['order_id'])
        if existing_delivery and existing_delivery.get('status') == 'sent':
            return

    if order['product_id'] == CLIP_PRODUCT_ID:
        if str(chat_id) != str(order['chat_id']) or str(chat_id) != str(order['user_id']):
            return
        _deliver_paid_clip_order(order, chat_id, cfg, store, api_fn, download_fn, send_video_fn, clip_fn)
        return

    if order['product_id'] == KIDS_VIDEO_PRODUCT_ID:
        if str(chat_id) != str(order['chat_id']) or str(chat_id) != str(order['user_id']):
            return
        _deliver_paid_kids_video_order(order, chat_id, cfg, store, api_fn, download_fn, send_video_fn)
        return

    _deliver_paid_order(
        order,
        chat_id,
        cfg,
        store,
        api_fn,
        send_document_fn,
    )


def _handle_buy_deeplink(
    item_id: str,
    msg: dict,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB | None,
    api_fn: Callable[[str, str, dict | None], dict],
) -> None:
    if not ID_RE.fullmatch(item_id):
        api_fn(
            cfg.token,
            'sendMessage',
            {'chat_id': chat_id, 'text': 'Некорректный идентификатор товара. Посмотрите доступное в /catalog.'},
        )
        return

    product = store.get_product(item_id) if store is not None else None
    if product is None:
        catalog_items, _ = _load_catalog_and_manifest(cfg.repo)
        c_item = next((x for x in catalog_items if x.get('id') == item_id), None)
        if c_item is not None:
            pkg_ready = _safe_package_path(_resolved_packages_dir(cfg), item_id) is not None
            product = {
                'product_id': item_id,
                'title': c_item.get('title', item_id),
                'description': c_item.get('description', ''),
                'package_contents': c_item.get('package_contents') or c_item.get('format', ''),
                'price_uah': int(c_item.get('price_uah') or 0),
                'price_xtr': int(c_item.get('price_xtr') or c_item.get('price_uah') or 0),
                'status': c_item.get('status', 'проверка качества'),
                'package_ready': 1 if pkg_ready else 0,
                'active_version': 1,
            }

    if product is None:
        api_fn(
            cfg.token,
            'sendMessage',
            {'chat_id': chat_id, 'text': f'Товар {item_id} не найден. Доступные комплекты: /catalog.'},
        )
        return

    if cfg.payments_enabled and store is not None:
        pkg_path = _safe_package_path(_resolved_packages_dir(cfg), item_id)
        if int(product.get('package_ready') or 0) != 1 or product.get('status') != 'готов к продаже' or pkg_path is None:
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': (
                        f"Товар «{product['title']}» ({item_id}) сейчас в статусе «{product.get('status')}» "
                        f"и пока недоступен для оплаты. Готовые комплекты: /catalog."
                    ),
                },
            )
            return

        user_id = get_sender_id(msg)
        username = str((msg.get('from') or {}).get('username') or '').strip()
        order = store.create_order(user_id=user_id, chat_id=str(chat_id), username=username, product=product)
        title = str(product['title'])[:32]
        desc = str(product.get('package_contents') or product.get('description') or title)[:255]
        res = api_fn(
            cfg.token,
            'sendInvoice',
            {
                'chat_id': chat_id,
                'title': title,
                'description': desc,
                'payload': order['invoice_payload'],
                'provider_token': '',
                'currency': 'XTR',
                'prices': [{'label': title, 'amount': int(order['amount_xtr'])}],
                'start_parameter': f'buy_{item_id}',
            },
        )
        if isinstance(res, dict) and res.get('ok') is False:
            store.update_order_status(order['order_id'], 'cancelled', actor_id=user_id, details={'error': res})
            api_fn(
                cfg.token,
                'sendMessage',
                {'chat_id': chat_id, 'text': 'Не удалось выставить счёт. Попробуйте позже или напишите в /paysupport.'},
            )
        else:
            store.update_order_status(order['order_id'], 'invoice_sent', actor_id=user_id)
        return

    # Fail-closed when payments are not enabled yet (or store not configured)
    api_fn(
        cfg.token,
        'sendMessage',
        {
            'chat_id': chat_id,
            'text': (
                f"Здравствуйте! Заявка по NAUM AI Creative Studio (buy_{item_id}) принята.\n"
                f"Товар: {product['title']} [{product.get('status')}]\n"
                f"Автоматическая оплата Telegram Stars ожидает включения владельцем. "
                f"Вы можете посмотреть /catalog или написать вопрос в /support."
            ),
        },
    )


def _handle_demo_command(
    msg: dict,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB | None,
    api_fn: Callable[[str, str, dict | None], dict],
    send_document_fn: Callable[[str, int | str, Path, str], dict],
) -> None:
    """Deliver free lead-magnet demo postcard ZIP without payment and log metrics to SQLite."""
    demo_meta = _load_demo_offer(cfg.repo)
    demo_id = str(demo_meta.get('id') or DEMO_PRODUCT_ID)
    packages_dir = _resolved_packages_dir(cfg)
    pkg_path = _safe_package_path(packages_dir, demo_id)
    tmp_dir_obj: tempfile.TemporaryDirectory[str] | None = None

    try:
        if pkg_path is None:
            try:
                build_res = build_demo_postcard_package(cfg.repo, output_dir=packages_dir, item_id=demo_id)
                pkg_path = Path(build_res['package_path'])
            except PermissionError:
                tmp_dir_obj = tempfile.TemporaryDirectory(prefix='naum_demo_')
                build_res = build_demo_postcard_package(cfg.repo, output_dir=Path(tmp_dir_obj.name), item_id=demo_id)
                pkg_path = Path(build_res['package_path'])

        pkg_sha = sha256_file(pkg_path)
        user_id = get_sender_id(msg)
        username = str((msg.get('from') or {}).get('username') or '').strip()

        order: dict | None = None
        if store is not None:
            outcome, order, _ = store.record_demo_delivery_start(
                user_id=user_id,
                chat_id=str(chat_id),
                username=username,
                package_path=pkg_path,
                package_sha256=pkg_sha,
                demo_meta=demo_meta,
                product_id=demo_id,
                source='demo_ny',
            )
            if outcome == 'already_delivered':
                api_fn(
                    cfg.token,
                    'sendMessage',
                    {
                        'chat_id': chat_id,
                        'text': (
                            'Вы уже получили бесплатную демо-открытку NAUM AI Creative Studio!\n'
                            'Полные комплекты для печати (300 dpi, без пометки DEMO): /catalog'
                        ),
                    },
                )
                return

        try:
            resp = send_document_fn(
                cfg.token,
                chat_id,
                pkg_path,
                (
                    f"🎁 {demo_meta.get('title', 'Бесплатная демо-открытка')} — NAUM AI Creative Studio\n"
                    f"Полная версия для печати 300 dpi: /catalog"
                ),
            )
            file_id = str((((resp or {}).get('result') or {}).get('document') or {}).get('file_id') or '')
            if store is not None and order is not None:
                store.mark_delivery_sent(order['order_id'], telegram_file_id=file_id)
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': (
                        'Демо-открытка доставлена! Проверьте качество и композицию.\n'
                        'Хотите полный комплект 150×100 мм (300 dpi) без водяного знака? '
                        'Откройте /catalog или /start buy_ny-kyiv-01.'
                    ),
                },
            )
        except Exception as exc:
            err = _sanitize_error(exc, cfg.token)
            if store is not None and order is not None:
                store.mark_delivery_failed(order['order_id'], err)
            api_fn(
                cfg.token,
                'sendMessage',
                {'chat_id': chat_id, 'text': 'Не удалось отправить демо-комплект. Попробуйте позже или напишите в /support.'},
            )
    finally:
        if tmp_dir_obj is not None:
            tmp_dir_obj.cleanup()


def _handle_catalog_command(
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB | None,
    api_fn: Callable[[str, str, dict | None], dict],
) -> None:
    demo_meta = _load_demo_offer(cfg.repo)
    demo_block = (
        f"🎁 БЕСПЛАТНО (лид-магнит): {demo_meta.get('title', 'Бесплатная демо-открытка')}\n"
        f"  Комплект: {demo_meta.get('package_contents', 'демо-открытка ZIP')}\n"
        f"  Цена: 0 Stars (бесплатно) · Получить: /start {demo_meta.get('deeplink', 'demo_ny')}"
    )

    packages_dir = _resolved_packages_dir(cfg)
    if store is not None:
        ready = [
            p for p in store.list_ready_products()
            if p.get('product_id') != DEMO_PRODUCT_ID and _safe_package_path(packages_dir, p['product_id']) is not None
        ]
    else:
        catalog_items, _ = _load_catalog_and_manifest(cfg.repo)
        ready = []
        for x in catalog_items:
            pid = str(x.get('id') or '')
            if pid and pid != DEMO_PRODUCT_ID and _safe_package_path(packages_dir, pid) is not None:
                ready.append(
                    {
                        'product_id': pid,
                        'title': x.get('title', pid),
                        'package_contents': x.get('package_contents') or x.get('format', ''),
                        'price_uah': int(x.get('price_uah') or 0),
                        'price_xtr': int(x.get('price_xtr') or x.get('price_uah') or 0),
                        'status': x.get('status', 'проверка качества'),
                    }
                )

    if cfg.payments_enabled:
        sellable = [p for p in ready if p.get('status') == 'готов к продаже']
        if not sellable:
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': (
                        f'{demo_block}\n\n'
                        'Платные комплекты пока готовятся к открытию продаж. Попробуйте бесплатное демо или напишите в /support.'
                    ),
                },
            )
            return
        lines = [
            f"• {p['product_id']} — {p['title']} | {p['price_xtr']} Stars ({p['price_uah']} грн)\n"
            f"  Комплект: {p['package_contents']}\n"
            f"  Купить: /start buy_{p['product_id']}"
            for p in sellable
        ]
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': f"Каталог NAUM AI Creative Studio:\n\n{demo_block}\n\n" + '\n\n'.join(lines),
            },
        )
        return

    if not ready:
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': (
                    f"{demo_block}\n\n"
                    "Платные ZIP-комплекты пока на сборке. Попробуйте бесплатное демо прямо сейчас!"
                ),
            },
        )
        return

    lines = [
        f"• {p['product_id']} — {p['title']} [{p['status']}]\n"
        f"  Комплект: {p['package_contents']}\n"
        f"  Ориентир: {p['price_xtr']} Stars / {p['price_uah']} грн"
        for p in ready
    ]
    api_fn(
        cfg.token,
        'sendMessage',
        {
            'chat_id': chat_id,
            'text': (
                f"{demo_block}\n\n"
                'Собранные комплекты (оплата Telegram Stars временно в режиме ожидания подтверждения владельцем):\n\n'
                + '\n\n'.join(lines)
            ),
        },
    )


def _handle_myorders_command(
    msg: dict,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB | None,
    api_fn: Callable[[str, str, dict | None], dict],
) -> None:
    if store is None:
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': 'История заказов временно недоступна.'})
        return
    orders = store.list_user_orders(get_sender_id(msg), limit=10)
    if not orders:
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': 'У вас пока нет заказов. Посмотрите /catalog.'})
        return
    lines = [
        f"• {o['order_id']} | {o['product_id']} ({o['product_title']}) | "
        f"{o['amount_xtr']} {o['currency']} | статус: {o['status']}"
        + (f" (доставка: {o['delivery_status']})" if o.get('delivery_status') else '')
        for o in orders
    ]
    api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': 'Ваши заказы:\n' + '\n'.join(lines)})


def _handle_support_command(
    cmd: str,
    text: str,
    msg: dict,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB | None,
    api_fn: Callable[[str, str, dict | None], dict],
) -> None:
    ticket_type = 'paysupport' if cmd == '/paysupport' else 'support'
    rest = text.split(maxsplit=1)[1].strip() if ' ' in text else ''
    user_id = get_sender_id(msg)

    if rest:
        order_id = ''
        first_word = rest.split()[0]
        if first_word.startswith('ord_'):
            order_id = first_word
        if store is not None:
            ticket_id = store.create_support_ticket(user_id, str(chat_id), ticket_type, rest, order_id=order_id)
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': f'Обращение #{ticket_id} ({ticket_type}) зарегистрировано. Мы ответим вам в этом чате.',
                },
            )
            return
        api_fn(
            cfg.token,
            'sendMessage',
            {'chat_id': chat_id, 'text': f'Обращение ({ticket_type}) принято. Мы ответим вам в этом чате.'},
        )
        return

    if ticket_type == 'paysupport':
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': (
                    'Поддержка по оплате Telegram Stars и доставке ZIP-архивов:\n'
                    '• Проверьте статус заказа через /myorders.\n'
                    '• Чтобы оставить обращение, отправьте:\n'
                    '/paysupport <номер заказа ord_... и описание проблемы>'
                ),
            },
        )
    else:
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': (
                    'Поддержка NAUM AI Creative Studio:\n'
                    'Чтобы отправить сообщение команде студии, напишите:\n'
                    '/support <ваш вопрос или номер заказа>'
                ),
            },
        )


def _handle_refund_command(
    text: str,
    msg: dict,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB | None,
    api_fn: Callable[[str, str, dict | None], dict],
) -> None:
    if store is None:
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': 'Ошибка: база магазина не настроена.'})
        return
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': 'Использование: /refund <order_id>'})
        return

    order_id = parts[1].strip()
    order = store.get_order(order_id)
    if not order:
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': f'Заказ {order_id} не найден.'})
        return

    payment = store.get_payment_by_order(order_id)
    if not payment or payment.get('currency') != 'XTR':
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': f'Оплата Stars для {order_id} не найдена.'})
        return
    if payment.get('status') == 'refunded':
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': f'По заказу {order_id} возврат уже выполнен.'})
        return

    admin_id = get_sender_id(msg)
    try:
        res = api_fn(
            cfg.token,
            'refundStarPayment',
            {
                'user_id': int(payment['user_id']),
                'telegram_payment_charge_id': payment['telegram_payment_charge_id'],
            },
        )
        if isinstance(res, dict) and res.get('ok') is False:
            raise RuntimeError(str(res.get('description') or 'refundStarPayment rejected'))
    except Exception as exc:
        api_fn(
            cfg.token,
            'sendMessage',
            {'chat_id': chat_id, 'text': f'Ошибка возврата {order_id}: {_sanitize_error(exc, cfg.token)}'},
        )
        return

    store.mark_refunded(order_id, admin_id)
    api_fn(
        cfg.token,
        'sendMessage',
        {'chat_id': chat_id, 'text': f'OK: возврат Stars по заказу {order_id} выполнен.'},
    )





def send_telegram_photo(token: str, chat_id: int | str, file_path: Path, caption: str, reply_markup: dict | None = None) -> dict:
    """Send a JPEG/PNG image via Telegram Bot API sendPhoto."""
    import json as _json
    import urllib.request as _req
    boundary = f'----NaumPhoto{int(time.time() * 1000)}'
    fields = {'chat_id': str(chat_id), 'caption': caption}
    if reply_markup is not None:
        fields['reply_markup'] = _json.dumps(reply_markup)
    body = bytearray()
    for key, val in fields.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{val}\r\n'.encode('utf-8'))
    file_bytes = file_path.read_bytes()
    body.extend(
        f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{file_path.name}"\r\nContent-Type: image/jpeg\r\n\r\n'.encode('utf-8')
    )
    body.extend(file_bytes)
    body.extend(f'\r\n--{boundary}--\r\n'.encode('utf-8'))
    req = _req.Request(
        f'https://api.telegram.org/bot{token}/sendPhoto',
        data=bytes(body),
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
        method='POST',
    )
    with _req.urlopen(req, timeout=30) as resp:
        return _json.loads(resp.read().decode('utf-8'))


def send_telegram_audio(
    token: str,
    chat_id: int | str,
    file_path: Path,
    caption: str = '',
    title: str = '',
    performer: str = 'NAUM AI Studio',
) -> dict:
    """Send an MP3 audio greeting via Telegram Bot API sendAudio."""
    boundary = f'----naumAudio{uuid.uuid4().hex}'
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'.encode('utf-8')
        )

    add_field('chat_id', str(chat_id))
    if caption:
        add_field('caption', caption[:1024])
    if title:
        add_field('title', title[:64])
    if performer:
        add_field('performer', performer[:64])

    file_bytes = file_path.read_bytes()
    parts.append(
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="audio"; filename="{file_path.name}"\r\n'
        f'Content-Type: audio/mpeg\r\n\r\n'.encode('utf-8')
    )
    parts.append(file_bytes)
    parts.append(f'\r\n--{boundary}--\r\n'.encode('utf-8'))

    body = b''.join(parts)
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/sendAudio',
        data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        res = json.loads(resp.read().decode('utf-8'))
    if not res.get('ok'):
        raise RuntimeError(f"Telegram sendAudio failed: {res.get('description', 'unknown error')}")
    return res

def _queue_radar_task(source_id: str) -> tuple[bool, str]:
    """Read signal from radar.db and enqueue a cloud GitHub MVP task."""
    import json as _json
    import re as _re
    import sqlite3 as _sqlite3
    radar_db = Path(os.environ.get('STUDIO_RADAR_DB', '/var/lib/naum-studio/radar.db'))
    queue_file = Path(os.environ.get('STUDIO_AUTOPILOT_QUEUE', '/var/lib/naum-studio/autopilot_queue.json'))
    if not radar_db.is_file():
        return False, 'База радара пока пуста.'
    conn = _sqlite3.connect(radar_db)
    row = conn.execute(
        'SELECT source_id, title, url, summary, license FROM signals WHERE source_id = ?',
        (source_id,),
    ).fetchone()
    conn.close()
    if not row:
        return False, f'Находка {source_id} не найдена в базе.'
    sid, title, url, summary, lic = row
    queue = _json.loads(queue_file.read_text(encoding='utf-8')) if queue_file.is_file() else []
    slug = _re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:32] or 'idea'
    if any(x.get('source_id') == sid for x in queue):
        return True, f'Задача «{title}» уже стоит в очереди облака.'
    queue.append({
        'id': f'radar-{slug}',
        'source_id': sid,
        'title': f'MVP по находке радара: {title}',
        'prompt': (
            f'Ты — облачный Antigravity на VPS (инстанс Kyiv-AI-Pro, --remote-control). '
            f'Владелец нажал «Взять в работу» по находке Opportunity Radar:\n'
            f'- Проект/идея: {title}\n- Ссылка: {url}\n- Лицензия: {lic}\n- Описание: {summary}\n\n'
            f'Задача: изучи идею, создай на отдельной ветке (НЕ в main) рабочий MVP/прототип или интеграцию в /opt/ai-creative-showcase '
            f'(или новый подпроект в /opt/ai-projects/{slug} с git + Draft PR на GitHub), напиши автотесты pytest, прогони pytest -q и отчитайся.'
        ),
    })
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    queue_file.write_text(_json.dumps(queue, ensure_ascii=False, indent=2), encoding='utf-8')
    return True, f'🚀 Принято: {title}\nОблако заберёт задачу в течение 3 минут, сделает прототип + демо-страницу и откроет ветку на GitHub.'


def _queue_custom_idea(idea_text: str) -> tuple[bool, str]:
    """Enqueue a free-form owner idea into the cloud autopilot queue."""
    import json as _json
    import re as _re
    clean = idea_text.strip()
    if len(clean) < 4:
        return False, 'Напиши идею после команды, например: /idea Сделать бота для обложек YouTube'
    queue_file = Path(os.environ.get('STUDIO_AUTOPILOT_QUEUE', '/var/lib/naum-studio/autopilot_queue.json'))
    queue = _json.loads(queue_file.read_text(encoding='utf-8')) if queue_file.is_file() else []
    slug = _re.sub(r'[^a-z0-9]+', '-', clean.lower()).strip('-')[:24] or 'custom-idea'
    queue.append({
        'id': f'idea-{slug}',
        'title': f'Идея владельца: {clean[:60]}',
        'prompt': (
            f'Ты — облачный Antigravity на VPS (инстанс Kyiv-AI-Pro, --remote-control). '
            f'Владелец прислал идею через /idea в Telegram: "{clean}".\n'
            f'Задача: спроектируй простой и рабочий MVP на отдельной ветке (НЕ в main) в /opt/ai-projects/{slug} '
            f'(или модулем в /opt/ai-creative-showcase), добавь простую демо-страницу и автотесты pytest, '
            f'прогони pytest -q, создай/обнови ветку на GitHub и коротко отчитайся.'
        ),
    })
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    queue_file.write_text(_json.dumps(queue, ensure_ascii=False, indent=2), encoding='utf-8')
    return True, f'💡 Идея в работе: «{clean[:60]}»\nОблачный агент соберёт прототип и покажет результат.'


def _cloud_status_text() -> str:
    import json as _json
    import subprocess as _sp
    state_file = Path(os.environ.get('STUDIO_AUTOPILOT_STATE', '/var/lib/naum-studio/autopilot_state.json'))
    queue_file = Path(os.environ.get('STUDIO_AUTOPILOT_QUEUE', '/var/lib/naum-studio/autopilot_queue.json'))
    state = _json.loads(state_file.read_text(encoding='utf-8')) if state_file.is_file() else {}
    queue = _json.loads(queue_file.read_text(encoding='utf-8')) if queue_file.is_file() else []
    r = _sp.run(
        ['su', '-', 'antigravity-pro', '-s', '/bin/bash', '-c', 'tmux has-session -t naum-cloud'],
        capture_output=True, text=True,
    )
    active_line = 'работает прямо сейчас (Kyiv-AI-Pro)' if r.returncode == 0 else 'свободен (ждёт задачу)'
    q_lines = '\n'.join(f"  • {q.get('title', q.get('id'))}" for q in queue[:5]) or '  (очередь пуста)'
    return (
        f"🤖 Статус облака:\n"
        f"• Агент naum-cloud: {active_line}\n"
        f"• Аккаунт: {state.get('current_account', 'antigravity-pro')}\n"
        f"• Последний коммит: {state.get('last_reported_head', '-')}\n"
        f"• В очереди ({len(queue)}):\n{q_lines}"
    )


def _handle_kids_video_callback(
    cb_id: str,
    job_id: str,
    user_id: str,
    username: str,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB | None,
    api_fn: Callable[[str, str, dict | None], dict],
) -> None:
    """Handle customer inline button click to buy HD 9:16 kids greeting video (45 Stars / 39 UAH)."""
    if store is None:
        api_fn(cfg.token, 'answerCallbackQuery', {'callback_query_id': cb_id, 'text': 'Сервіс тимчасово недоступний.'})
        return

    job = store.get_kids_job(job_id)
    if job is None or str(job['user_id']) != str(user_id):
        api_fn(
            cfg.token,
            'answerCallbackQuery',
            {'callback_query_id': cb_id, 'text': 'Заявка не знайдена. Спробуйте команду /mykolay або /santa ще раз.', 'show_alert': True},
        )
        return

    lang = job.get('lang') or 'uk'
    if not cfg.payments_enabled:
        store.log_audit('kids_paywall_paused', actor_id=user_id, product_id=KIDS_VIDEO_PRODUCT_ID, details={'job_id': job_id})
        api_fn(cfg.token, 'answerCallbackQuery', {'callback_query_id': cb_id, 'text': f'{KIDS_VIDEO_PRICE_UAH} грн / {KIDS_VIDEO_PRICE_XTR} Stars'})
        msg_text = (
            f"🎬 Повне HD-відео 9:16 зі сказкою та золотою книгою для {job['child_name']} "
            f"коштує {KIDS_VIDEO_PRICE_UAH} грн ({KIDS_VIDEO_PRICE_XTR} Stars XTR).\n"
            f"Аудіо-привітання та Грамота — безкоштовно! Оплата Telegram Stars очікує ввімкнення власником (/support)."
            if lang == 'uk'
            else f"🎬 Полное HD-видео 9:16 со сказкой и волшебной книгой для {job['child_name']} "
            f"стоит {KIDS_VIDEO_PRICE_UAH} грн ({KIDS_VIDEO_PRICE_XTR} Stars XTR).\n"
            f"Аудио-поздравление и Грамота — бесплатно! Оплата Telegram Stars ожидает включения владельцем (/support)."
        )
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': msg_text})
        return

    api_fn(cfg.token, 'answerCallbackQuery', {'callback_query_id': cb_id, 'text': 'Готую рахунок Stars...'})
    order, paid_job = store.create_kids_paid_order(
        job_id=job_id,
        user_id=user_id,
        chat_id=str(chat_id),
        username=username,
        price_xtr=KIDS_VIDEO_PRICE_XTR,
    )
    title = f"HD-відео для {job['child_name']}"[:32] if lang == 'uk' else f"HD-видео для {job['child_name']}"[:32]
    desc = (
        f"Персональне святкове відео 9:16 (книга казок + дзвіночки + голос) — {KIDS_VIDEO_PRICE_UAH} грн."
        if lang == 'uk'
        else f"Персональное праздничное видео 9:16 (книга сказок + колокольчики + голос) — {KIDS_VIDEO_PRICE_UAH} грн."
    )
    res = api_fn(
        cfg.token,
        'sendInvoice',
        {
            'chat_id': chat_id,
            'title': title,
            'description': desc[:255],
            'payload': order['invoice_payload'],
            'provider_token': '',
            'currency': 'XTR',
            'prices': [{'label': title, 'amount': int(order['amount_xtr'])}],
            'start_parameter': 'kids_video',
        },
    )
    if isinstance(res, dict) and res.get('ok') is False:
        store.update_order_status(order['order_id'], 'cancelled', actor_id=user_id, details={'error': res})
        store.mark_kids_job_failed(paid_job['job_id'], 'sendInvoice rejected')
        api_fn(
            cfg.token,
            'sendMessage',
            {'chat_id': chat_id, 'text': 'Не удалось выставить счёт Stars. Попробуйте позже или напишите в /paysupport.'},
        )
    else:
        store.update_order_status(order['order_id'], 'invoice_sent', actor_id=user_id)


def _handle_callback_query(upd: dict, cfg: BotConfig, store, api_fn) -> None:
    cb = upd.get('callback_query') or {}
    cb_id = cb.get('id')
    cb_msg = cb.get('message') or {}
    chat = cb_msg.get('chat') or {}
    chat_id = chat.get('id')
    cb_from = cb.get('from') or {}
    fake_msg = {'from': cb_from, 'chat': chat}
    data = (cb.get('data') or '').strip()

    if data.startswith('kids_video:'):
        if not is_private_chat(fake_msg) or not chat_id:
            api_fn(cfg.token, 'answerCallbackQuery', {'callback_query_id': cb_id, 'text': 'Напишіть боту в особисті повідомлення.', 'show_alert': True})
            return
        job_id = data.split(':', 1)[1].strip()
        user_id = str(cb_from.get('id') or chat_id)
        username = str(cb_from.get('username') or '').strip()
        _handle_kids_video_callback(cb_id, job_id, user_id, username, chat_id, cfg, store, api_fn)
        return

    if not (is_owner(fake_msg, cfg.owner_ids) and is_private_chat(fake_msg)):
        api_fn(cfg.token, 'answerCallbackQuery', {'callback_query_id': cb_id, 'text': 'Доступ только для владельца в личке.', 'show_alert': True})
        return
    if data.startswith('radar_build:'):
        sid = data.split(':', 1)[1]
        ok, reply = _queue_radar_task(sid)
        api_fn(cfg.token, 'answerCallbackQuery', {'callback_query_id': cb_id, 'text': 'Готово!' if ok else 'Ошибка'})
        if chat_id:
            api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': reply})
        return
    if data == 'radar_skip':
        api_fn(cfg.token, 'answerCallbackQuery', {'callback_query_id': cb_id, 'text': 'Пропущено'})
        if chat_id:
            api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': '⏭ Находки пропущены. Следующий поиск — по расписанию.'})
        return
    if data == 'panel_cloud':
        api_fn(cfg.token, 'answerCallbackQuery', {'callback_query_id': cb_id, 'text': 'Обновлено'})
        if chat_id:
            api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': _cloud_status_text()})
        return
    if data == 'panel_radar':
        import subprocess as _sp
        api_fn(cfg.token, 'answerCallbackQuery', {'callback_query_id': cb_id, 'text': 'Запустил разведку...'})
        _sp.Popen(['/usr/bin/python3', '/opt/opportunity-radar/opportunity_radar.py', '--notify'])
        if chat_id:
            api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': '📡 Разведка запущена, через ~30 секунд пришлю свежий отчёт с кнопками.'})
        return
    if data == 'panel_stats' and store is not None:
        m = store.get_metrics_summary()
        api_fn(cfg.token, 'answerCallbackQuery', {'callback_query_id': cb_id, 'text': 'Готово'})
        if chat_id:
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': (
                        f"📊 Метрики студии:\n"
                        f"• Демо: {m['demo_requests']} (выдач {m['demo_deliveries_sent']})\n"
                        f"• Платные заказы: {m['paid_orders']} ({m['paid_revenue_xtr']} XTR)\n"
                        f"• Конверсия демо → покупка: {m['demo_to_paid_conversion_pct']}%"
                    ),
                },
            )
        return
    api_fn(cfg.token, 'answerCallbackQuery', {'callback_query_id': cb_id})

def _handle_clip_command(
    msg: dict,
    effective_text: str,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB | None,
    api_fn: Callable[[str, str, dict | None], dict],
    download_fn: Callable[[str, str, Path], None],
    send_video_fn: Callable[[str, int | str, Path, str], dict],
    clip_fn: Callable[[str | Path, Path], dict],
) -> None:
    """Handle /clip <url or video>: 1-2 free trial clips (lead magnet), then Telegram Stars (XTR)."""
    user_id = get_sender_id(msg) or str(chat_id)
    username = str((msg.get('from') or {}).get('username') or '').strip()
    used = store.count_user_free_clips(user_id) if store is not None else 0
    remaining = max(0, CLIP_FREE_TRIAL_LIMIT - used)

    try:
        source = _extract_clip_source(msg, effective_text)
    except ValueError as exc:
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': str(exc)})
        return

    if source is None:
        trial_line = (
            f'У вас осталось бесплатных клипов: {remaining} из {CLIP_FREE_TRIAL_LIMIT}.'
            if remaining > 0
            else f'Бесплатные пробные клипы ({CLIP_FREE_TRIAL_LIMIT}/{CLIP_FREE_TRIAL_LIMIT}) использованы. Далее: {CLIP_PRICE_XTR} Stars (XTR).'
        )
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': (
                    '🎬 AI Shorts Clipper 9:16 (PeakCut + караоке-субтитры):\n'
                    '• Отправьте /clip <ссылка на видео>\n'
                    '• Или прикрепите видео с подписью /clip (либо ответьте /clip на видео)\n'
                    f'{trial_line}'
                ),
            },
        )
        return

    source_type, source_value = source
    if store is None:
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': 'Сервис нарезки клипов временно недоступен: база не настроена.'})
        return

    claimed, trial_num, job = store.claim_free_clip_trial(
        user_id=user_id,
        chat_id=str(chat_id),
        username=username,
        source_type=source_type,
        source_value=source_value,
        limit=CLIP_FREE_TRIAL_LIMIT,
    )
    if claimed and job is not None:
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': (
                    f'🎬 Нарезаю вертикальный ролик 9:16 с караоке-субтитрами... '
                    f'(пробный клип {trial_num}/{CLIP_FREE_TRIAL_LIMIT} бесплатно)'
                ),
            },
        )
        left_after = max(0, CLIP_FREE_TRIAL_LIMIT - trial_num)
        tail = (
            f'Остался ещё {left_after} бесплатный клип!'
            if left_after > 0
            else f'Пробный лимит исчерпан. Следующий клип — {CLIP_PRICE_XTR} Stars (XTR).'
        )
        caption = f'🎬 Готово! Вертикальный клип 9:16 (пробный {trial_num}/{CLIP_FREE_TRIAL_LIMIT}).\n{tail}'
        try:
            _, tg_file_id = _run_and_send_clip(
                source_type=source_type,
                source_value=source_value,
                chat_id=chat_id,
                cfg=cfg,
                api_fn=api_fn,
                download_fn=download_fn,
                send_video_fn=send_video_fn,
                clip_fn=clip_fn,
                caption=caption,
            )
            store.mark_clip_job_delivered(job['job_id'], telegram_file_id=tg_file_id)
        except Exception as exc:
            err = _sanitize_error(exc, cfg.token)
            store.mark_clip_job_failed(job['job_id'], err)
            api_fn(
                cfg.token,
                'sendMessage',
                {'chat_id': chat_id, 'text': f'Не удалось обработать видео: {err}'},
            )
        return

    # Free trial exhausted -> Telegram Stars (XTR) paywall
    if not cfg.payments_enabled:
        store.log_audit('clip_paywall_paused', actor_id=user_id, product_id=CLIP_PRODUCT_ID)
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': (
                    f'Вы уже использовали {CLIP_FREE_TRIAL_LIMIT} бесплатных пробных клипа (лид-магнит).\n'
                    f'Следующая нарезка 9:16 с караоке-субтитрами стоит {CLIP_PRICE_XTR} Stars (XTR).\n'
                    f'Оплата Telegram Stars ожидает включения владельцем. По вопросам: /support.'
                ),
            },
        )
        return

    order, paid_job = store.create_clip_paid_order(
        user_id=user_id,
        chat_id=str(chat_id),
        username=username,
        source_type=source_type,
        source_value=source_value,
        price_xtr=CLIP_PRICE_XTR,
    )
    title = 'AI Shorts 9:16 с субтитрами'
    desc = (
        f'Бесплатные пробные клипы ({CLIP_FREE_TRIAL_LIMIT}/{CLIP_FREE_TRIAL_LIMIT}) использованы. '
        f'Нарезка вертикального ролика 1080x1920 с караоке-субтитрами.'
    )
    res = api_fn(
        cfg.token,
        'sendInvoice',
        {
            'chat_id': chat_id,
            'title': title,
            'description': desc[:255],
            'payload': order['invoice_payload'],
            'provider_token': '',
            'currency': 'XTR',
            'prices': [{'label': title, 'amount': int(order['amount_xtr'])}],
            'start_parameter': 'clip_short',
        },
    )
    if isinstance(res, dict) and res.get('ok') is False:
        store.update_order_status(order['order_id'], 'cancelled', actor_id=user_id, details={'error': res})
        store.mark_clip_job_failed(paid_job['job_id'], 'sendInvoice rejected')
        api_fn(
            cfg.token,
            'sendMessage',
            {'chat_id': chat_id, 'text': 'Не удалось выставить счёт Stars. Попробуйте позже или напишите в /paysupport.'},
        )
    else:
        store.update_order_status(order['order_id'], 'invoice_sent', actor_id=user_id)


def _handle_kids_command(
    cmd: str,
    msg: dict,
    effective_text: str,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB | None,
    api_fn: Callable[[str, str, dict | None], dict],
    send_photo_fn: Callable[..., dict],
    send_audio_fn: Callable[..., dict],
) -> None:
    """Handle /mykolay and /santa: FREE voice greeting with bells + 1200x800 Diploma, plus 39 UAH / 45 Stars HD video upsell."""
    raw_args = effective_text[len(cmd):].strip()
    req = parse_greeting_args(raw_args, command=cmd)
    if req is None:
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': (
                    '🎅 Персональне привітання для дитини (UA / RU):\n'
                    'Напишіть: ім’я, вік, місто та добру справу за рік.\n\n'
                    'Приклади:\n'
                    '• /mykolay Артемчик, 6, Київ, слухав маму і читав книжки\n'
                    '• /santa Маша, 7, Одесса, помогала бабушке и учила стихи\n\n'
                    '🎁 Аудіо з дзвіночками + іменна Грамота — БЕЗКОШТОВНО!\n'
                    f'🎬 Повне HD-відео 9:16 зі сказкою — всього {KIDS_VIDEO_PRICE_UAH} грн ({KIDS_VIDEO_PRICE_XTR} Stars).'
                ),
            },
        )
        return

    user_id = str(get_sender_id(msg) or chat_id)
    username = str((msg.get('from') or {}).get('username') or '').strip()
    photo_list = msg.get('photo') or []
    photo_file_id = str(photo_list[-1].get('file_id') or '') if photo_list else ''

    try:
        with tempfile.TemporaryDirectory(prefix='naum_kids_free_') as tmp_str:
            tmp_dir = Path(tmp_str)
            cert_path = tmp_dir / 'certificate.jpg'
            audio_path = tmp_dir / 'greeting.mp3'

            render_certificate(
                repo_root=cfg.repo,
                child_name=req.child_name,
                city=req.city,
                out_path=cert_path,
                age=req.age,
                good_deed=req.good_deed,
                lang=req.lang,
                persona=req.persona,
            )
            asyncio.run(
                generate_nicholas_audio(
                    child_name=req.child_name,
                    good_deed=req.good_deed,
                    out_mp3=audio_path,
                    lang=req.lang,
                    age=req.age,
                    city=req.city,
                    persona=req.persona,
                )
            )

            job_id = 'demo'
            if store is not None:
                lead_job = store.create_kids_lead_job(
                    user_id=user_id,
                    chat_id=str(chat_id),
                    username=username,
                    child_name=req.child_name,
                    age=req.age,
                    city=req.city,
                    good_deed=req.good_deed,
                    lang=req.lang,
                    persona=req.persona,
                    photo_file_id=photo_file_id,
                )
                job_id = lead_job['job_id']

            if req.lang == 'uk':
                cert_caption = (
                    f"📜 Іменна Грамота чемної дитини для {req.child_name} (безкоштовно)!\n"
                    f"Надсилайте матусям у чати — кожен може зробити через /mykolay або /santa.\n\n"
                    f"Хочете повне HD-відео 9:16 з книгою казок та дзвіночками за {KIDS_VIDEO_PRICE_UAH} грн ({KIDS_VIDEO_PRICE_XTR} Stars)?"
                )
                btn_text = f"🎬 Повне HD-відео 9:16 ({KIDS_VIDEO_PRICE_UAH} грн / {KIDS_VIDEO_PRICE_XTR} Stars)"
                audio_title = f"Привітання для {req.child_name}"
                audio_caption = f"🔔 Чарівне голосове привітання з дзвіночками для {req.child_name}!"
            else:
                cert_caption = (
                    f"📜 Именная Грамота вежливого ребёнка для {req.child_name} (бесплатно)!\n"
                    f"Делитесь в чатах — каждый может сделать через /santa или /mykolay.\n\n"
                    f"Хотите полное HD-видео 9:16 с книгой сказок и колокольчиками за {KIDS_VIDEO_PRICE_UAH} грн ({KIDS_VIDEO_PRICE_XTR} Stars)?"
                )
                btn_text = f"🎬 Полное HD-видео 9:16 ({KIDS_VIDEO_PRICE_UAH} грн / {KIDS_VIDEO_PRICE_XTR} Stars)"
                audio_title = f"Поздравление для {req.child_name}"
                audio_caption = f"🔔 Волшебное голосовое поздравление с колокольчиками для {req.child_name}!"

            send_photo_fn(
                cfg.token,
                chat_id,
                cert_path,
                cert_caption,
                {'inline_keyboard': [[{'text': btn_text, 'callback_data': f'kids_video:{job_id}'}]]},
            )
            send_audio_fn(
                cfg.token,
                chat_id,
                audio_path,
                caption=audio_caption,
                title=audio_title,
                performer='NAUM AI Studio',
            )
    except Exception as exc:
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': f'Помилка створення привітання: {_sanitize_error(exc, cfg.token)}'})


def process_update(
    upd: dict,
    cfg: BotConfig,
    api_fn: Callable[[str, str, dict | None], dict] = api_call,
    ingest_fn: Callable[..., dict] = ingest_asset,
    download_fn: Callable[[str, str, Path], None] = download_telegram_file,
    push_fn: Callable[[Path, str, str, str], None] = git_push_preview,
    send_document_fn: Callable[[str, int | str, Path, str], dict] = send_telegram_document,
    send_photo_fn: Callable[..., dict] = send_telegram_photo,
    send_video_fn: Callable[[str, int | str, Path, str], dict] = send_telegram_video,
    send_audio_fn: Callable[..., dict] = send_telegram_audio,
    clip_fn: Callable[[str | Path, Path], dict] = clip_video,
) -> None:
    """Process a single Telegram update with strict role separation and private-chat admin checks."""
    if not cfg.owner_ids:
        raise RuntimeError('STUDIO_OWNER_IDS is empty (fail-closed)')

    store = ensure_store(cfg)

    if 'callback_query' in upd and upd['callback_query']:
        _handle_callback_query(upd, cfg, store, api_fn)
        return

    if 'pre_checkout_query' in upd and upd['pre_checkout_query']:
        if store is None:
            api_fn(
                cfg.token,
                'answerPreCheckoutQuery',
                {
                    'pre_checkout_query_id': upd['pre_checkout_query'].get('id'),
                    'ok': False,
                    'error_message': 'Магазин временно недоступен.',
                },
            )
            return
        _handle_pre_checkout(upd, cfg, store, api_fn)
        return

    msg = upd.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id')
    if not chat_id:
        return

    if msg.get('successful_payment'):
        if store is not None:
            _handle_successful_payment(
                upd,
                msg,
                chat_id,
                cfg,
                store,
                api_fn,
                send_document_fn,
                download_fn=download_fn,
                send_video_fn=send_video_fn,
                clip_fn=clip_fn,
            )
        return

    text = (msg.get('text') or '').strip()
    caption_text = (msg.get('caption') or '').strip()
    cmd = text.split()[0].split('@')[0].lower() if text.startswith('/') else ''
    caption_cmd = caption_text.split()[0].split('@')[0].lower() if caption_text.startswith('/') else ''
    owner = is_owner(msg, cfg.owner_ids)
    private = is_private_chat(msg)
    admin = owner and private

    if store is not None and isinstance(upd.get('update_id'), int):
        if not store.mark_update_processed(upd['update_id'], cmd or caption_cmd or 'message'):
            return

    if cmd == '/start':
        arg = text.split(maxsplit=1)[1].strip() if ' ' in text else ''
        if private and arg in ('demo_ny', 'demo-ny'):
            _handle_demo_command(msg, chat_id, cfg, store, api_fn, send_document_fn)
        elif private and arg.startswith('buy_'):
            _handle_buy_deeplink(arg[4:].strip(), msg, chat_id, cfg, store, api_fn)
        elif admin:
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': (
                        'NAUM AI Creative Studio активен. '
                        'Отправьте фото/документ с подписью ID товара (например: ny-kyiv-01), /items, /stats или /refund <order_id>.'
                    ),
                },
            )
        elif private:
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': (
                        f"Здравствуйте! Заявка по NAUM AI Creative Studio ({arg or 'общий запрос'}) принята.\n"
                        f"Бесплатная демо-открытка: /start demo_ny\n"
                        f"Доступные команды: /mykolay, /santa, /clip, /catalog, /myorders, /support, /paysupport."
                    ),
                },
            )
        return

    if cmd == '/catalog' and private:
        _handle_catalog_command(chat_id, cfg, store, api_fn)
        return

    if cmd == '/myorders' and private:
        _handle_myorders_command(msg, chat_id, cfg, store, api_fn)
        return

    if cmd in ('/support', '/paysupport') and private:
        _handle_support_command(cmd, text, msg, chat_id, cfg, store, api_fn)
        return

    if (cmd == '/clip' or (not text and caption_cmd == '/clip')) and private:
        _handle_clip_command(
            msg=msg,
            effective_text=text or caption_text,
            chat_id=chat_id,
            cfg=cfg,
            store=store,
            api_fn=api_fn,
            download_fn=download_fn,
            send_video_fn=send_video_fn,
            clip_fn=clip_fn,
        )
        return

    if cmd in ('/mykolay', '/santa') or (not text and caption_cmd in ('/mykolay', '/santa')):
        _handle_kids_command(
            cmd=cmd or caption_cmd,
            msg=msg,
            effective_text=text or caption_text,
            chat_id=chat_id,
            cfg=cfg,
            store=store,
            api_fn=api_fn,
            send_photo_fn=send_photo_fn,
            send_audio_fn=send_audio_fn,
        )
        return

    if cmd == '/card':
        raw_name = text[len('/card'):].strip()
        if not raw_name:
            api_fn(cfg.token, 'sendMessage', {
                'chat_id': chat_id,
                'text': '✨ Напиши имя после команды, и я сделаю именную открытку за 3 секунды!\nНапример: /card Марина',
            })
            return
        try:
            from viral_card import generate_personalized_card
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
                card_path = Path(tmp.name)
            try:
                generate_personalized_card(cfg.repo, raw_name, card_path)
                markup = {'inline_keyboard': [[{'text': '🔥 Полный HD-набор без подписи', 'url': 'https://t.me/NaumCreativeStudioBot?start=buy_ny-kyiv-01'}]]}
                send_photo_fn(
                    cfg.token,
                    chat_id,
                    card_path,
                    f'🎄 Готово! Именная открытка для {raw_name[:28]}.\nПерешли друзьям — пусть тоже сделают себе через /card!',
                    markup,
                )
                if store is not None:
                    store.log_audit('viral_card_created', actor_id=str(get_sender_id(msg) or chat_id), details={'name': raw_name[:28]})
            finally:
                card_path.unlink(missing_ok=True)
        except Exception as exc:
            api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': f'Не получилось сделать открытку: {_sanitize_error(exc, cfg.token)}'})
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
        if cmd in ('/items', '/refund', '/stats') or (file_id and caption):
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': 'Админ-команды и загрузка ассетов разрешены только в приватном чате с ботом.',
                },
            )
        return

    if cmd == '/items':
        cat = json.loads((cfg.repo / 'catalog.json').read_text(encoding='utf-8'))
        lines = [f"• {x['id']} — {x['title']} [{x.get('status', 'черновик')}]" for x in cat.get('items', [])]
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': 'Товары:\n' + '\n'.join(lines)})
        return

    if cmd == '/stats':
        if store is None:
            api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': 'Метрики недоступны: база магазина не настроена.'})
            return
        m = store.get_metrics_summary()
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': (
                    'Метрики NAUM AI Creative Studio:\n'
                    f"• Лид-магнит (demo_ny): {m['demo_deliveries_sent']} выдач / {m['demo_unique_leads']} уник. лидов (повторов: {m['demo_repeat_requests']})\n"
                    f"• Платные заказы: {m['paid_orders']} | Выручка: {m['paid_revenue_xtr']} XTR | Доставок: {m['paid_deliveries_sent']}\n"
                    f"• Конверсия демо → покупка: {m['demo_to_paid_conversion_pct']}%\n"
                    f"• Открытые тикеты: {m['open_support_tickets']}"
                ),
            },
        )
        return

    if cmd == '/radar':
        if not admin:
            return
        target_url = text[len('/radar'):].strip()
        if not target_url or not target_url.startswith('http'):
            api_fn(cfg.token, 'sendMessage', {
                'chat_id': chat_id,
                'text': 'Отправь ссылку после команды, например: /radar https://twitter.com/... или https://github.com/...',
            })
            return
        ok, reply = _queue_custom_idea(f"Разбор ссылки из сети: {target_url}")
        api_fn(cfg.token, 'sendMessage', {
            'chat_id': chat_id,
            'text': f"🔍 Взял ссылку на радар: {target_url}\nАгенты разберут суть и подготовят ТЗ.",
        })
        return

    if cmd == '/idea':
        if not admin:
            return
        idea_body = text[len('/idea'):].strip()
        ok, reply = _queue_custom_idea(idea_body)
        api_fn(cfg.token, 'sendMessage', {'chat_id': chat_id, 'text': reply})
        return

    if cmd in ('/panel', '/menu'):
        if not admin:
            return
        api_fn(cfg.token, 'sendMessage', {
            'chat_id': chat_id,
            'text': '🎛 Пульт студии (нажми любую кнопку):\n• Разведка — найти новые идеи\n• Облако — что сейчас делают агенты\n• Продажи — сколько заявок и Stars\n• Своя идея — напиши /idea и текст',
            'reply_markup': {'inline_keyboard': [
                [{'text': '📡 Запустить разведку сейчас', 'callback_data': 'panel_radar'}],
                [{'text': '🤖 Статус облака и очереди', 'callback_data': 'panel_cloud'}],
                [{'text': '📊 Метрики продаж', 'callback_data': 'panel_stats'}],
            ]},
        })
        return

    if cmd == '/refund':
        _handle_refund_command(text, msg, chat_id, cfg, store, api_fn)
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

    ensure_store(cfg)
    offset = 0
    while True:
        try:
            updates = api_call(cfg.token, f'getUpdates?timeout=25&offset={offset}').get('result', [])
            for upd in updates:
                process_update(upd, cfg)
                offset = upd['update_id'] + 1
        except Exception:
            time.sleep(5)


if __name__ == '__main__':
    main()
