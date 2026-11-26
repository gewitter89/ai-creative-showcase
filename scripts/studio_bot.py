#!/usr/bin/env python3
"""Telegram store and intake bot for NAUM AI Creative Studio."""
from __future__ import annotations

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

from build_packages import DEFAULT_PACKAGES_DIR
from ingest_asset import ID_RE, MAX_INPUT_BYTES, ingest_asset, sha256_file
from store_db import DEFAULT_DB_PATH, StoreDB

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


def _deliver_paid_order(
    order: dict,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB,
    api_fn: Callable[[str, str, dict | None], dict],
    send_document_fn: Callable[[str, int | str, Path, str], dict],
) -> None:
    packages_dir = _resolved_packages_dir(cfg)
    pkg_path = _safe_package_path(packages_dir, order['product_id'])
    if pkg_path is None:
        store.mark_delivery_failed(order['order_id'], 'ZIP package missing on server')
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': (
                    f"Оплата по заказу {order['order_id']} получена, но архив временно недоступен. "
                    f"Обратитесь в /paysupport {order['order_id']}."
                ),
            },
        )
        return

    pkg_sha = sha256_file(pkg_path)
    delivery = store.create_or_get_delivery(order, pkg_path, pkg_sha)
    if delivery.get('status') == 'sent':
        return

    try:
        resp = send_document_fn(
            cfg.token,
            chat_id,
            pkg_path,
            f"NAUM AI Creative Studio — заказ {order['order_id']} ({order['product_id']})",
        )
        file_id = str((((resp or {}).get('result') or {}).get('document') or {}).get('file_id') or '')
        store.mark_delivery_sent(order['order_id'], telegram_file_id=file_id)
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': (
                    f"Спасибо за покупку! Заказ {order['order_id']} доставлен.\n"
                    f"Посмотреть историю заказов: /myorders · Поддержка по оплате: /paysupport"
                ),
            },
        )
    except Exception as exc:
        err = _sanitize_error(exc, cfg.token)
        store.mark_delivery_failed(order['order_id'], err)
        api_fn(
            cfg.token,
            'sendMessage',
            {
                'chat_id': chat_id,
                'text': (
                    f"Оплата по заказу {order['order_id']} подтверждена, но отправка файла не удалась. "
                    f"Напишите в /paysupport {order['order_id']}."
                ),
            },
        )


def _handle_successful_payment(
    upd: dict,
    msg: dict,
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB,
    api_fn: Callable[[str, str, dict | None], dict],
    send_document_fn: Callable[[str, int | str, Path, str], dict],
) -> None:
    if not is_private_chat(msg):
        return
    update_id = upd.get('update_id')
    if isinstance(update_id, int) and not store.mark_update_processed(update_id, 'successful_payment'):
        return

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
    if outcome == 'mismatch' or order is None:
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

    _deliver_paid_order(order, chat_id, cfg, store, api_fn, send_document_fn)


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


def _handle_catalog_command(
    chat_id: int | str,
    cfg: BotConfig,
    store: StoreDB | None,
    api_fn: Callable[[str, str, dict | None], dict],
) -> None:
    packages_dir = _resolved_packages_dir(cfg)
    if store is not None:
        ready = [
            p for p in store.list_ready_products()
            if _safe_package_path(packages_dir, p['product_id']) is not None
        ]
    else:
        catalog_items, _ = _load_catalog_and_manifest(cfg.repo)
        ready = []
        for x in catalog_items:
            pid = str(x.get('id') or '')
            if _safe_package_path(packages_dir, pid) is not None:
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
                {'chat_id': chat_id, 'text': 'Сейчас нет товаров, открытых для покупки. Проверьте позже или напишите в /support.'},
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
            {'chat_id': chat_id, 'text': 'Готовые комплекты NAUM AI Creative Studio:\n\n' + '\n\n'.join(lines)},
        )
        return

    if not ready:
        api_fn(
            cfg.token,
            'sendMessage',
            {'chat_id': chat_id, 'text': 'Готовые ZIP-комплекты пока на сборке. Следите за обновлениями!'},
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


def process_update(
    upd: dict,
    cfg: BotConfig,
    api_fn: Callable[[str, str, dict | None], dict] = api_call,
    ingest_fn: Callable[..., dict] = ingest_asset,
    download_fn: Callable[[str, str, Path], None] = download_telegram_file,
    push_fn: Callable[[Path, str, str, str], None] = git_push_preview,
    send_document_fn: Callable[[str, int | str, Path, str], dict] = send_telegram_document,
) -> None:
    """Process a single Telegram update with strict role separation and private-chat admin checks."""
    if not cfg.owner_ids:
        raise RuntimeError('STUDIO_OWNER_IDS is empty (fail-closed)')

    store = ensure_store(cfg)

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
            _handle_successful_payment(upd, msg, chat_id, cfg, store, api_fn, send_document_fn)
        return

    text = (msg.get('text') or '').strip()
    cmd = text.split()[0].split('@')[0].lower() if text.startswith('/') else ''
    owner = is_owner(msg, cfg.owner_ids)
    private = is_private_chat(msg)
    admin = owner and private

    if store is not None and isinstance(upd.get('update_id'), int):
        if not store.mark_update_processed(upd['update_id'], cmd or 'message'):
            return

    if cmd == '/start':
        arg = text.split(maxsplit=1)[1].strip() if ' ' in text else ''
        if private and arg.startswith('buy_'):
            _handle_buy_deeplink(arg[4:].strip(), msg, chat_id, cfg, store, api_fn)
        elif admin:
            api_fn(
                cfg.token,
                'sendMessage',
                {
                    'chat_id': chat_id,
                    'text': (
                        'NAUM AI Creative Studio активен. '
                        'Отправьте фото/документ с подписью ID товара (например: ny-kyiv-01), /items или /refund <order_id>.'
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
                        f"Доступные команды: /catalog, /myorders, /support, /paysupport."
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
        if cmd in ('/items', '/refund') or (file_id and caption):
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
                offset = upd['update_id'] + 1
                process_update(upd, cfg)
        except Exception:
            time.sleep(5)


if __name__ == '__main__':
    main()
