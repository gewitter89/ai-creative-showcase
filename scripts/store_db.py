#!/usr/bin/env python3
"""SQLite persistence and audit layer for NAUM AI Creative Studio store."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ingest_asset import sha256_file
from package_version import verified_order_package, retain_catalog_package

DEFAULT_DB_PATH = '/var/lib/naum-studio/store.db'
DB_FILE_MODE = 0o600
VALID_STATUSES = frozenset({'черновик', 'проверка качества', 'готов к продаже'})
ACTIVE_ORDER_STATUSES = frozenset({'created', 'invoice_sent', 'pre_checkout_ok'})
DEMO_PRODUCT_ID = 'demo-ny'
CLIP_PRODUCT_ID = 'ai-video-clip'
CLIP_FREE_TRIAL_LIMIT = 2
CLIP_PRICE_XTR = 50
KIDS_VIDEO_PRODUCT_ID = 'kids-greeting-video'
KIDS_VIDEO_PRICE_XTR = 45
KIDS_VIDEO_PRICE_UAH = 39


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class StoreDB:
    """Thread-safe / single-process SQLite store for products, orders, Stars payments, and deliveries."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA foreign_keys=ON;')
        return conn

    def init_schema(self) -> None:
        """Create all required tables and indexes, enforcing restrictive file permissions."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    product_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    package_contents TEXT NOT NULL DEFAULT '',
                    price_uah INTEGER NOT NULL DEFAULT 0,
                    price_xtr INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'проверка качества',
                    package_ready INTEGER NOT NULL DEFAULT 0,
                    package_path TEXT NOT NULL DEFAULT '',
                    package_sha256 TEXT NOT NULL DEFAULT '',
                    package_bytes INTEGER NOT NULL DEFAULT 0,
                    active_version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS product_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL REFERENCES products(product_id),
                    version INTEGER NOT NULL,
                    package_path TEXT NOT NULL,
                    package_sha256 TEXT NOT NULL,
                    package_bytes INTEGER NOT NULL,
                    original_sha256 TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(product_id, version)
                );

                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    product_id TEXT NOT NULL REFERENCES products(product_id),
                    product_version INTEGER NOT NULL DEFAULT 1,
                    amount_xtr INTEGER NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'XTR',
                    invoice_payload TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    user_id TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    total_amount INTEGER NOT NULL,
                    invoice_payload TEXT NOT NULL,
                    telegram_payment_charge_id TEXT NOT NULL UNIQUE,
                    provider_payment_charge_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    refunded_at TEXT,
                    refunded_by TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
                    user_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    package_path TEXT NOT NULL,
                    package_sha256 TEXT NOT NULL,
                    telegram_file_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    delivered_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS support_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    ticket_type TEXT NOT NULL,
                    order_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed_updates (
                    update_id INTEGER PRIMARY KEY,
                    update_type TEXT NOT NULL DEFAULT '',
                    processed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL DEFAULT '',
                    order_id TEXT NOT NULL DEFAULT '',
                    product_id TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clip_jobs (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL,
                    source_value TEXT NOT NULL,
                    is_free_trial INTEGER NOT NULL DEFAULT 1,
                    order_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    output_path TEXT NOT NULL DEFAULT '',
                    telegram_file_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS kids_jobs (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    child_name TEXT NOT NULL,
                    age INTEGER,
                    city TEXT NOT NULL DEFAULT '',
                    good_deed TEXT NOT NULL DEFAULT '',
                    lang TEXT NOT NULL DEFAULT 'uk',
                    persona TEXT NOT NULL DEFAULT 'mykolay',
                    photo_file_id TEXT NOT NULL DEFAULT '',
                    order_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    telegram_file_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id);
                CREATE INDEX IF NOT EXISTS idx_audit_order_id ON audit_events(order_id);
                CREATE INDEX IF NOT EXISTS idx_clip_jobs_user ON clip_jobs(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_clip_jobs_order ON clip_jobs(order_id);
                CREATE INDEX IF NOT EXISTS idx_kids_jobs_order ON kids_jobs(order_id);
                """
            )
        if self.db_path.exists():
            os.chmod(self.db_path, DB_FILE_MODE)

    def _log_audit_conn(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        actor_id: str = '',
        order_id: str = '',
        product_id: str = '',
        details: dict | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_events (event_type, actor_id, order_id, product_id, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                str(actor_id),
                str(order_id),
                str(product_id),
                json.dumps(details or {}, ensure_ascii=False),
                utc_now_iso(),
            ),
        )

    def log_audit(
        self,
        event_type: str,
        actor_id: str = '',
        order_id: str = '',
        product_id: str = '',
        details: dict | None = None,
    ) -> None:
        """Persist an audit event row."""
        with self._connect() as conn:
            self._log_audit_conn(conn, event_type, actor_id, order_id, product_id, details)

    def sync_catalog(
        self,
        catalog_items: list[dict],
        packages_dir: Path,
        manifest_items: list[dict] | None = None,
    ) -> int:
        """Synchronize products and package versions from catalog.json and packages directory."""
        manifest_by_id = {
            str(m.get('id')): m for m in (manifest_items or []) if isinstance(m, dict) and m.get('id')
        }
        now = utc_now_iso()
        synced = 0

        with self._connect() as conn:
            for item in catalog_items:
                product_id = str(item.get('id') or '').strip()
                if not product_id:
                    continue
                category = str(item.get('category') or 'misc').strip()
                title = str(item.get('title') or product_id).strip()
                description = str(item.get('description') or '').strip()
                package_contents = str(item.get('package_contents') or item.get('format') or '').strip()
                price_uah = int(item.get('price_uah') or 0)
                price_xtr = int(item.get('price_xtr') or price_uah or 0)
                status = str(item.get('status') or 'проверка качества').strip()
                if status not in VALID_STATUSES:
                    status = 'черновик'

                retained = retain_catalog_package(packages_dir, product_id)
                package_ready = int(retained is not None)
                pkg_sha256 = retained['package_sha256'] if retained else ''
                pkg_bytes = retained['package_bytes'] if retained else 0
                pkg_path_str = retained['package_path'] if retained else ''

                existing = conn.execute(
                    'SELECT active_version FROM products WHERE product_id = ?',
                    (product_id,),
                ).fetchone()
                active_version = int(existing['active_version']) if existing else 0

                conn.execute(
                    """
                    INSERT INTO products (
                        product_id, category, title, description, package_contents,
                        price_uah, price_xtr, status, package_ready, package_path,
                        package_sha256, package_bytes, active_version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(product_id) DO UPDATE SET
                        category = excluded.category,
                        title = excluded.title,
                        description = excluded.description,
                        package_contents = excluded.package_contents,
                        price_uah = excluded.price_uah,
                        price_xtr = excluded.price_xtr,
                        status = excluded.status,
                        package_ready = excluded.package_ready,
                        package_path = excluded.package_path,
                        package_sha256 = excluded.package_sha256,
                        package_bytes = excluded.package_bytes,
                        updated_at = excluded.updated_at
                    """,
                    (
                        product_id,
                        category,
                        title,
                        description,
                        package_contents,
                        price_uah,
                        price_xtr,
                        status,
                        package_ready,
                        pkg_path_str,
                        pkg_sha256,
                        pkg_bytes,
                        active_version,
                        now,
                    ),
                )

                if package_ready and pkg_sha256:
                    ver_row = conn.execute(
                        """
                        SELECT version, package_bytes FROM product_versions
                        WHERE product_id = ? AND package_sha256 = ?
                        ORDER BY version LIMIT 1
                        """,
                        (product_id, pkg_sha256),
                    ).fetchone()
                    if ver_row:
                        new_ver = int(ver_row['version'])
                        if int(ver_row['package_bytes']) != pkg_bytes:
                            conn.execute('UPDATE products SET package_ready = 0 WHERE product_id = ?', (product_id,))
                            synced += 1
                            continue
                        # Upgrade an existing matching legacy row to the retained copy.
                        conn.execute(
                            'UPDATE product_versions SET package_path = ? WHERE product_id = ? AND version = ? AND package_sha256 = ? AND package_bytes = ?',
                            (pkg_path_str, product_id, new_ver, pkg_sha256, pkg_bytes),
                        )
                    else:
                        max_row = conn.execute(
                            'SELECT COALESCE(MAX(version), 0) AS max_v FROM product_versions WHERE product_id = ?',
                            (product_id,),
                        ).fetchone()
                        new_ver = int(max_row['max_v']) + 1
                        orig_sha = str((manifest_by_id.get(product_id) or {}).get('original_sha256') or '')
                        conn.execute(
                            """
                            INSERT INTO product_versions (
                                product_id, version, package_path, package_sha256,
                                package_bytes, original_sha256, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (product_id, new_ver, pkg_path_str, pkg_sha256, pkg_bytes, orig_sha, now),
                        )
                    conn.execute(
                        'UPDATE products SET active_version = ? WHERE product_id = ?',
                        (new_ver, product_id),
                    )
                synced += 1
        return synced

    def get_product(self, product_id: str) -> dict | None:
        """Return a single product row as dict or None."""
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM products WHERE product_id = ?', (product_id,)).fetchone()
            return dict(row) if row else None

    def list_ready_products(self) -> list[dict]:
        """Return products that have a built ZIP package on disk."""
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM products WHERE package_ready = 1 ORDER BY rowid ASC'
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_update_processed(self, update_id: int, update_type: str = '') -> bool:
        """Record Telegram update_id atomically. Returns False if update_id was already processed."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO processed_updates (update_id, update_type, processed_at)
                VALUES (?, ?, ?)
                """,
                (int(update_id), update_type, utc_now_iso()),
            )
            return cur.rowcount == 1

    def create_order(self, user_id: str, chat_id: str, username: str, product: dict) -> dict:
        """Create a new server-side order for a Telegram Stars checkout."""
        order_id = f'ord_{uuid.uuid4().hex[:16]}'
        invoice_payload = f'naum:{order_id}'
        now = utc_now_iso()
        amount_xtr = int(product['price_xtr'])
        if amount_xtr <= 0:
            raise ValueError(f"Invalid price_xtr for product {product['product_id']}: {amount_xtr}")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO orders (
                    order_id, user_id, chat_id, username, product_id, product_version,
                    amount_xtr, currency, invoice_payload, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'XTR', ?, 'created', ?, ?)
                """,
                (
                    order_id,
                    str(user_id),
                    str(chat_id),
                    username,
                    product['product_id'],
                    int(product.get('active_version') or 1),
                    amount_xtr,
                    invoice_payload,
                    now,
                    now,
                ),
            )
            self._log_audit_conn(
                conn,
                'order_created',
                actor_id=str(user_id),
                order_id=order_id,
                product_id=product['product_id'],
                details={'amount_xtr': amount_xtr, 'currency': 'XTR'},
            )
            row = conn.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,)).fetchone()
            return dict(row)

    def update_order_status(
        self,
        order_id: str,
        status: str,
        actor_id: str = '',
        details: dict | None = None,
    ) -> None:
        """Update order status and log audit event."""
        with self._connect() as conn:
            row = conn.execute('SELECT product_id FROM orders WHERE order_id = ?', (order_id,)).fetchone()
            product_id = row['product_id'] if row else ''
            conn.execute(
                'UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?',
                (status, utc_now_iso(), order_id),
            )
            self._log_audit_conn(
                conn,
                f'order_{status}',
                actor_id=actor_id,
                order_id=order_id,
                product_id=product_id,
                details=details,
            )

    def get_order(self, order_id: str) -> dict | None:
        """Fetch an order by order_id."""
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,)).fetchone()
            return dict(row) if row else None

    def get_order_by_payload(self, invoice_payload: str) -> dict | None:
        """Fetch an order by invoice_payload."""
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM orders WHERE invoice_payload = ?',
                (invoice_payload,),
            ).fetchone()
            return dict(row) if row else None

    def list_user_orders(self, user_id: str, limit: int = 10) -> list[dict]:
        """List recent orders for a buyer joined with product title and delivery status."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    o.*,
                    COALESCE(p.title, o.product_id) AS product_title,
                    COALESCE(d.status, '') AS delivery_status
                FROM orders o
                LEFT JOIN products p ON p.product_id = o.product_id
                LEFT JOIN deliveries d ON d.order_id = o.order_id
                WHERE o.user_id = ?
                ORDER BY o.created_at DESC, o.rowid DESC
                LIMIT ?
                """,
                (str(user_id), int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_order_package_version(self, order: dict) -> dict | None:
        """Read the package identity fixed at order creation, not the active product."""
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM product_versions WHERE product_id = ? AND version = ?',
                (order['product_id'], order['product_version']),
            ).fetchone()
            return dict(row) if row else None

    def validate_pre_checkout(
        self,
        user_id: str,
        currency: str,
        total_amount: int,
        invoice_payload: str,
        packages_dir: Path,
        payments_enabled: bool,
    ) -> tuple[bool, str, dict | None]:
        """Strictly validate a Telegram pre_checkout_query against server-side order and product state."""
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM orders WHERE invoice_payload = ?',
                (invoice_payload,),
            ).fetchone()
            order = dict(row) if row else None

            def reject(reason: str) -> tuple[bool, str, dict | None]:
                self._log_audit_conn(
                    conn,
                    'pre_checkout_rejected',
                    actor_id=str(user_id),
                    order_id=order['order_id'] if order else '',
                    product_id=order['product_id'] if order else '',
                    details={
                        'reason': reason,
                        'currency': currency,
                        'total_amount': total_amount,
                    },
                )
                return False, reason, order

            if not payments_enabled:
                return reject('Приём платежей временно отключён.')
            if currency != 'XTR':
                return reject('Неверная валюта оплаты (ожидается XTR).')
            if order is None:
                return reject('Заказ не найден.')
            if str(user_id) != str(order['user_id']):
                return reject('Заказ принадлежит другому пользователю.')
            if int(total_amount) != int(order['amount_xtr']):
                return reject('Сумма заказа не совпадает.')
            if order['status'] not in ACTIVE_ORDER_STATUSES:
                return reject('Заказ уже оплачен или отменён.')

            prod_row = conn.execute(
                'SELECT * FROM products WHERE product_id = ?',
                (order['product_id'],),
            ).fetchone()
            product = dict(prod_row) if prod_row else None
            if order['product_id'] == CLIP_PRODUCT_ID:
                clip_row = conn.execute(
                    'SELECT * FROM clip_jobs WHERE order_id = ?',
                    (order['order_id'],),
                ).fetchone()
                if not product or product['status'] != 'готов к продаже' or not clip_row:
                    return reject('Задание на нарезку клипа недоступно или устарело.')
            elif order['product_id'] == KIDS_VIDEO_PRODUCT_ID:
                kids_row = conn.execute(
                    'SELECT * FROM kids_jobs WHERE order_id = ?',
                    (order['order_id'],),
                ).fetchone()
                if not product or product['status'] != 'готов к продаже' or not kids_row:
                    return reject('Задание на создание детского видео недоступно или устарело.')
            else:
                if not product or int(product['package_ready']) != 1 or product['status'] != 'готов к продаже':
                    return reject('Товар временно недоступен для покупки.')

                version_row = conn.execute(
                    'SELECT * FROM product_versions WHERE product_id = ? AND version = ?',
                    (order['product_id'], order['product_version']),
                ).fetchone()
                version = dict(version_row) if version_row else None
                if verified_order_package(packages_dir, order, version) is None:
                    return reject('Версия архива изменилась или недоступна. Оформите новый заказ.')

            conn.execute(
                "UPDATE orders SET status = 'pre_checkout_ok', updated_at = ? WHERE order_id = ?",
                (utc_now_iso(), order['order_id']),
            )
            self._log_audit_conn(
                conn,
                'pre_checkout_ok',
                actor_id=str(user_id),
                order_id=order['order_id'],
                product_id=order['product_id'],
                details={'amount_xtr': total_amount, 'currency': currency},
            )
            order['status'] = 'pre_checkout_ok'
            return True, '', order

    def record_successful_payment(
        self,
        user_id: str,
        currency: str,
        total_amount: int,
        invoice_payload: str,
        telegram_charge_id: str,
        provider_charge_id: str = '',
    ) -> tuple[str, dict | None]:
        """Durably classify a charge before acknowledging the Telegram update."""
        if (type(total_amount) is not int or total_amount <= 0 or
                not isinstance(telegram_charge_id, str) or not telegram_charge_id or
                len(telegram_charge_id) > 256):
            return 'mismatch', None
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("""CREATE TABLE IF NOT EXISTS payment_review (
                charge_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                currency TEXT NOT NULL, amount INTEGER NOT NULL,
                invoice_payload TEXT NOT NULL, provider_charge_id TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            prior = conn.execute('SELECT 1 FROM payment_review WHERE charge_id = ?',
                                 (telegram_charge_id,)).fetchone()
            if prior:
                return 'review_required', None
            existing = conn.execute('SELECT * FROM payments WHERE telegram_payment_charge_id = ?',
                                    (telegram_charge_id,)).fetchone()
            if existing:
                if (str(user_id) != existing['user_id'] or currency != existing['currency'] or
                        total_amount != existing['total_amount'] or
                        invoice_payload != existing['invoice_payload'] or
                        provider_charge_id != existing['provider_payment_charge_id']):
                    self._log_audit_conn(conn, 'payment_duplicate_mismatch', order_id=existing['order_id'])
                    return 'mismatch', None
                row = conn.execute('SELECT * FROM orders WHERE order_id = ?',
                                   (existing['order_id'],)).fetchone()
                if existing['status'] != 'paid' or row is None or row['status'] not in ('paid', 'delivered', 'delivery_failed'):
                    return 'review_required', None
                return 'duplicate_charge', dict(row)
            row = conn.execute('SELECT * FROM orders WHERE invoice_payload = ?',
                               (invoice_payload,)).fetchone()
            order = dict(row) if row else None
            reason = ''
            if (order is None or currency != 'XTR' or total_amount != order['amount_xtr'] or
                    str(user_id) != order['user_id']):
                reason = 'payment_mismatch'
            elif conn.execute('SELECT 1 FROM payments WHERE order_id = ?',
                              (order['order_id'],)).fetchone():
                reason = 'additional_charge'
            elif order['status'] != 'pre_checkout_ok':
                reason = 'unexpected_order_state'
            if reason:
                conn.execute('INSERT INTO payment_review VALUES (?,?,?,?,?,?,?,?)',
                             (telegram_charge_id, str(user_id), currency, total_amount,
                              invoice_payload, provider_charge_id, reason, now))
                self._log_audit_conn(conn, 'payment_mismatch' if reason == 'payment_mismatch' else 'payment_needs_review',
                                     actor_id=str(user_id), order_id=order['order_id'] if order else '',
                                     product_id=order['product_id'] if order else '', details={'reason': reason})
                return 'review_required', None
            conn.execute("""INSERT INTO payments (order_id, user_id, currency, total_amount,
                         invoice_payload, telegram_payment_charge_id, provider_payment_charge_id, status, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, 'paid', ?)""",
                         (order['order_id'], str(user_id), currency, total_amount, invoice_payload,
                          telegram_charge_id, provider_charge_id, now))
            conn.execute("UPDATE orders SET status = 'paid', updated_at = ? WHERE order_id = ?",
                         (now, order['order_id']))
            self._log_audit_conn(conn, 'payment_received', actor_id=str(user_id),
                                 order_id=order['order_id'], product_id=order['product_id'],
                                 details={'amount_xtr': total_amount})
            order['status'] = 'paid'
            return 'recorded', order

    def get_payment_by_order(self, order_id: str) -> dict | None:
        """Return payment row for an order."""
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE order_id = ?', (order_id,)).fetchone()
            return dict(row) if row else None

    def get_delivery_by_order(self, order_id: str) -> dict | None:
        """Return delivery row for an order."""
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM deliveries WHERE order_id = ?', (order_id,)).fetchone()
            return dict(row) if row else None

    def reserve_paid_delivery(self, order: dict, version: dict) -> bool:
        """Claim one upload attempt durably; existing rows require reconciliation."""
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            cur = conn.execute(
                """
                INSERT INTO deliveries (
                    order_id, user_id, product_id, package_path, package_sha256, status, created_at
                )
                SELECT o.order_id, o.user_id, o.product_id, v.package_path,
                       v.package_sha256, 'sending', ?
                FROM orders o JOIN product_versions v
                  ON v.product_id = o.product_id AND v.version = o.product_version
                WHERE o.order_id = ? AND o.user_id = ? AND o.chat_id = ?
                  AND o.product_id = ? AND o.product_version = ? AND o.status = 'paid'
                  AND v.package_path = ? AND v.package_sha256 = ? AND v.package_bytes = ?
                  AND EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id
                              AND p.user_id = o.user_id AND p.status = 'paid'
                              AND p.currency = o.currency AND p.total_amount = o.amount_xtr)
                ON CONFLICT(order_id) DO NOTHING
                """,
                (utc_now_iso(), order['order_id'], str(order['user_id']), str(order['chat_id']),
                 order['product_id'], order['product_version'], version['package_path'],
                 version['package_sha256'], version['package_bytes']),
            )
            if cur.rowcount != 1:
                return False
            self._log_audit_conn(conn, 'delivery_reserved', actor_id=str(order['user_id']),
                                 order_id=order['order_id'], product_id=order['product_id'])
            return True

    def create_or_get_delivery(self, order: dict, package_path: Path, package_sha256: str) -> dict:
        """Create a pending delivery record if missing, or return existing delivery record."""
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO deliveries (
                    order_id, user_id, product_id, package_path, package_sha256, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    order['order_id'],
                    str(order['user_id']),
                    order['product_id'],
                    str(package_path),
                    package_sha256,
                    now,
                ),
            )
            row = conn.execute(
                'SELECT * FROM deliveries WHERE order_id = ?',
                (order['order_id'],),
            ).fetchone()
            return dict(row)

    def mark_delivery_sent(self, order_id: str, telegram_file_id: str = '') -> None:
        """Mark delivery and order as delivered."""
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute('SELECT user_id, product_id FROM orders WHERE order_id = ?', (order_id,)).fetchone()
            conn.execute(
                """
                UPDATE deliveries
                SET status = 'sent', telegram_file_id = ?, error = '', delivered_at = ?
                WHERE order_id = ?
                """,
                (telegram_file_id, now, order_id),
            )
            conn.execute(
                "UPDATE orders SET status = 'delivered', updated_at = ? WHERE order_id = ?",
                (now, order_id),
            )
            self._log_audit_conn(
                conn,
                'delivery_sent',
                actor_id=row['user_id'] if row else '',
                order_id=order_id,
                product_id=row['product_id'] if row else '',
                details={'telegram_file_id': telegram_file_id},
            )

    def mark_delivery_failed(self, order_id: str, error: str) -> None:
        """Mark delivery and order as failed without losing payment history."""
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute('SELECT user_id, product_id FROM orders WHERE order_id = ?', (order_id,)).fetchone()
            conn.execute(
                "UPDATE deliveries SET status = 'failed', error = ? WHERE order_id = ?",
                (error[:500], order_id),
            )
            conn.execute(
                "UPDATE orders SET status = 'delivery_failed', updated_at = ? WHERE order_id = ?",
                (now, order_id),
            )
            self._log_audit_conn(
                conn,
                'delivery_failed',
                actor_id=row['user_id'] if row else '',
                order_id=order_id,
                product_id=row['product_id'] if row else '',
                details={'error': error[:500]},
            )

    def mark_refunded(self, order_id: str, admin_id: str) -> None:
        """Mark payment and order as refunded by an owner."""
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute('SELECT product_id FROM orders WHERE order_id = ?', (order_id,)).fetchone()
            conn.execute(
                """
                UPDATE payments SET status = 'refunded', refunded_at = ?, refunded_by = ?
                WHERE order_id = ?
                """,
                (now, str(admin_id), order_id),
            )
            conn.execute(
                "UPDATE orders SET status = 'refunded', updated_at = ? WHERE order_id = ?",
                (now, order_id),
            )
            self._log_audit_conn(
                conn,
                'payment_refunded',
                actor_id=str(admin_id),
                order_id=order_id,
                product_id=row['product_id'] if row else '',
                details={'refunded_by': str(admin_id)},
            )

    def create_support_ticket(
        self,
        user_id: str,
        chat_id: str,
        ticket_type: str,
        message: str,
        order_id: str = '',
    ) -> int:
        """Insert a customer support or pay-support ticket and audit event."""
        now = utc_now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO support_tickets (user_id, chat_id, ticket_type, order_id, message, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'open', ?)
                """,
                (str(user_id), str(chat_id), ticket_type, order_id, message[:2000], now),
            )
            ticket_id = int(cur.lastrowid)
            self._log_audit_conn(
                conn,
                f'ticket_{ticket_type}',
                actor_id=str(user_id),
                order_id=order_id,
                details={'ticket_id': ticket_id},
            )
            return ticket_id

    def _ensure_demo_product_conn(
        self,
        conn: sqlite3.Connection,
        demo_meta: dict | None = None,
        product_id: str = DEMO_PRODUCT_ID,
    ) -> None:
        """Ensure the lead-magnet demo product row exists without polluting paid package_ready counts."""
        meta = demo_meta or {}
        title = str(meta.get('title') or 'Демо-открытка «Киев в новогодних огнях» (Бесплатно)').strip()
        desc = str(meta.get('description') or 'Бесплатная демо-открытка (лид-магнит) NAUM AI Creative Studio.').strip()
        contents = str(meta.get('package_contents') or 'demo_greeting_ua.png, demo_ny_kyiv.jpg, README.txt, LICENSE.txt').strip()
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO products (
                product_id, category, title, description, package_contents,
                price_uah, price_xtr, status, package_ready, package_path,
                package_sha256, package_bytes, active_version, updated_at
            ) VALUES (?, 'newyear', ?, ?, ?, 0, 0, 'готов к продаже', 0, '', '', 0, 1, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                package_contents = excluded.package_contents,
                updated_at = excluded.updated_at
            """,
            (product_id, title, desc, contents, now),
        )

    def record_demo_delivery_start(
        self,
        user_id: str,
        chat_id: str,
        username: str,
        package_path: Path,
        package_sha256: str,
        demo_meta: dict | None = None,
        product_id: str = DEMO_PRODUCT_ID,
        source: str = 'demo_ny',
    ) -> tuple[str, dict | None, dict | None]:
        """Record a free lead-magnet demo request/order and pending delivery idempotently per user."""
        now = utc_now_iso()
        uid = str(user_id)
        with self._connect() as conn:
            self._ensure_demo_product_conn(conn, demo_meta=demo_meta, product_id=product_id)

            existing_deliv = conn.execute(
                """
                SELECT d.* FROM deliveries d
                WHERE d.user_id = ? AND d.product_id = ? AND d.status = 'sent'
                ORDER BY d.id DESC LIMIT 1
                """,
                (uid, product_id),
            ).fetchone()
            if existing_deliv:
                existing_order = conn.execute(
                    'SELECT * FROM orders WHERE order_id = ?',
                    (existing_deliv['order_id'],),
                ).fetchone()
                self._log_audit_conn(
                    conn,
                    'demo_repeat_requested',
                    actor_id=uid,
                    order_id=existing_deliv['order_id'],
                    product_id=product_id,
                    details={'username': username, 'source': source},
                )
                return (
                    'already_delivered',
                    dict(existing_order) if existing_order else None,
                    dict(existing_deliv),
                )

            order_id = f'demo_{uuid.uuid4().hex[:14]}'
            invoice_payload = f'demo:{order_id}'
            conn.execute(
                """
                INSERT INTO orders (
                    order_id, user_id, chat_id, username, product_id, product_version,
                    amount_xtr, currency, invoice_payload, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, 0, 'FREE', ?, 'created', ?, ?)
                """,
                (order_id, uid, str(chat_id), username, product_id, invoice_payload, now, now),
            )
            conn.execute(
                """
                INSERT INTO deliveries (
                    order_id, user_id, product_id, package_path, package_sha256, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (order_id, uid, product_id, str(package_path), package_sha256, now),
            )
            self._log_audit_conn(
                conn,
                'demo_requested',
                actor_id=uid,
                order_id=order_id,
                product_id=product_id,
                details={'username': username, 'source': source, 'is_free_demo': True},
            )
            order_row = conn.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,)).fetchone()
            deliv_row = conn.execute('SELECT * FROM deliveries WHERE order_id = ?', (order_id,)).fetchone()
            return 'ready', dict(order_row), dict(deliv_row)

    def get_metrics_summary(self) -> dict:
        """Aggregate sales, lead-magnet demo deliveries, and audit metrics from SQLite."""
        with self._connect() as conn:
            demo_delivered = conn.execute(
                """
                SELECT COUNT(*) AS cnt, COUNT(DISTINCT user_id) AS uniq
                FROM deliveries WHERE product_id = ? AND status = 'sent'
                """,
                (DEMO_PRODUCT_ID,),
            ).fetchone()
            demo_repeats = conn.execute(
                "SELECT COUNT(*) AS cnt FROM audit_events WHERE event_type = 'demo_repeat_requested'"
            ).fetchone()
            paid_stats = conn.execute(
                """
                SELECT COUNT(*) AS cnt, COALESCE(SUM(total_amount), 0) AS xtr
                FROM payments WHERE status = 'paid'
                """
            ).fetchone()
            paid_deliveries = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM deliveries
                WHERE product_id != ? AND status = 'sent'
                """,
                (DEMO_PRODUCT_ID,),
            ).fetchone()
            converted_leads = conn.execute(
                """
                SELECT COUNT(DISTINCT p.user_id) AS cnt
                FROM payments p
                INNER JOIN deliveries d ON d.user_id = p.user_id
                WHERE p.status = 'paid' AND d.product_id = ? AND d.status = 'sent'
                """,
                (DEMO_PRODUCT_ID,),
            ).fetchone()
            open_tickets = conn.execute(
                "SELECT COUNT(*) AS cnt FROM support_tickets WHERE status = 'open'"
            ).fetchone()

        demo_leads = int(demo_delivered['uniq'] or 0)
        paid_orders = int(paid_stats['cnt'] or 0)
        conv = round((int(converted_leads['cnt'] or 0) / demo_leads) * 100, 2) if demo_leads > 0 else 0.0
        return {
            'demo_deliveries_sent': int(demo_delivered['cnt'] or 0),
            'demo_unique_leads': demo_leads,
            'demo_repeat_requests': int(demo_repeats['cnt'] or 0),
            'paid_orders': paid_orders,
            'paid_revenue_xtr': int(paid_stats['xtr'] or 0),
            'paid_deliveries_sent': int(paid_deliveries['cnt'] or 0),
            'demo_to_paid_conversion_pct': conv,
            'open_support_tickets': int(open_tickets['cnt'] or 0),
        }

    def _ensure_clip_product_conn(self, conn: sqlite3.Connection, price_xtr: int = CLIP_PRICE_XTR) -> None:
        """Ensure the AI Video Shorts Clipper service product exists without polluting ZIP catalog."""
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO products (
                product_id, category, title, description, package_contents,
                price_uah, price_xtr, status, package_ready, package_path,
                package_sha256, package_bytes, active_version, updated_at
            ) VALUES (?, 'video', 'AI Shorts Clipper 9:16', ?, 'Вертикальный клип 1080x1920 MP4 с караоке-субтитрами',
                0, ?, 'готов к продаже', 0, '', '', 0, 1, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                price_xtr = excluded.price_xtr,
                status = 'готов к продаже',
                updated_at = excluded.updated_at
            """,
            (
                CLIP_PRODUCT_ID,
                'Нарезка динамичных вертикальных Shorts 9:16 с караоке-субтитрами.',
                int(price_xtr),
                now,
            ),
        )

    def count_user_free_clips(self, user_id: str) -> int:
        """Return number of used (processing or delivered) free trial clips for user_id."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM clip_jobs
                WHERE user_id = ? AND is_free_trial = 1 AND status IN ('processing', 'delivered')
                """,
                (str(user_id),),
            ).fetchone()
            return int(row['cnt'] or 0)

    def claim_free_clip_trial(
        self,
        user_id: str,
        chat_id: str,
        username: str,
        source_type: str,
        source_value: str,
        limit: int = CLIP_FREE_TRIAL_LIMIT,
    ) -> tuple[bool, int, dict | None]:
        """Atomically reserve one free trial clip slot if user is below limit."""
        uid = str(user_id)
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM clip_jobs
                WHERE user_id = ? AND is_free_trial = 1 AND status IN ('processing', 'delivered')
                """,
                (uid,),
            ).fetchone()
            used = int(row['cnt'] or 0)
            if used >= limit:
                return False, used, None
            job_id = f'clip_{uuid.uuid4().hex[:14]}'
            conn.execute(
                """
                INSERT INTO clip_jobs (
                    job_id, user_id, chat_id, username, source_type, source_value,
                    is_free_trial, order_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, '', 'processing', ?, ?)
                """,
                (job_id, uid, str(chat_id), username, source_type, source_value[:1000], now, now),
            )
            trial_num = used + 1
            self._log_audit_conn(
                conn,
                'clip_trial_started',
                actor_id=uid,
                product_id=CLIP_PRODUCT_ID,
                details={'job_id': job_id, 'trial_num': trial_num, 'source_type': source_type},
            )
            job_row = conn.execute('SELECT * FROM clip_jobs WHERE job_id = ?', (job_id,)).fetchone()
            return True, trial_num, dict(job_row)

    def create_clip_paid_order(
        self,
        user_id: str,
        chat_id: str,
        username: str,
        source_type: str,
        source_value: str,
        price_xtr: int = CLIP_PRICE_XTR,
    ) -> tuple[dict, dict]:
        """Create an XTR order and awaiting_payment clip job after free trial limit is exhausted."""
        uid = str(user_id)
        now = utc_now_iso()
        amount_xtr = int(price_xtr)
        if amount_xtr <= 0:
            raise ValueError(f'Invalid clip price_xtr: {amount_xtr}')
        order_id = f'ord_{uuid.uuid4().hex[:16]}'
        invoice_payload = f'naum:{order_id}'
        job_id = f'clip_{uuid.uuid4().hex[:14]}'

        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._ensure_clip_product_conn(conn, price_xtr=amount_xtr)
            conn.execute(
                """
                INSERT INTO orders (
                    order_id, user_id, chat_id, username, product_id, product_version,
                    amount_xtr, currency, invoice_payload, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, 'XTR', ?, 'created', ?, ?)
                """,
                (order_id, uid, str(chat_id), username, CLIP_PRODUCT_ID, amount_xtr, invoice_payload, now, now),
            )
            conn.execute(
                """
                INSERT INTO clip_jobs (
                    job_id, user_id, chat_id, username, source_type, source_value,
                    is_free_trial, order_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'awaiting_payment', ?, ?)
                """,
                (job_id, uid, str(chat_id), username, source_type, source_value[:1000], order_id, now, now),
            )
            self._log_audit_conn(
                conn,
                'clip_order_created',
                actor_id=uid,
                order_id=order_id,
                product_id=CLIP_PRODUCT_ID,
                details={'job_id': job_id, 'amount_xtr': amount_xtr, 'source_type': source_type},
            )
            order_row = conn.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,)).fetchone()
            job_row = conn.execute('SELECT * FROM clip_jobs WHERE job_id = ?', (job_id,)).fetchone()
            return dict(order_row), dict(job_row)

    def get_clip_job_by_order(self, order_id: str) -> dict | None:
        """Return clip_job associated with order_id."""
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM clip_jobs WHERE order_id = ?', (order_id,)).fetchone()
            return dict(row) if row else None

    def reserve_clip_delivery(self, order_id: str) -> dict | None:
        """Atomically transition an awaiting_payment paid clip job to processing."""
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            cur = conn.execute(
                """
                UPDATE clip_jobs
                SET status = 'processing', updated_at = ?
                WHERE order_id = ? AND status = 'awaiting_payment'
                  AND EXISTS (SELECT 1 FROM orders o WHERE o.order_id = ? AND o.status = 'paid')
                """,
                (now, order_id, order_id),
            )
            if cur.rowcount != 1:
                return None
            row = conn.execute('SELECT * FROM clip_jobs WHERE order_id = ?', (order_id,)).fetchone()
            return dict(row) if row else None

    def mark_clip_job_delivered(
        self,
        job_id: str,
        output_path: str = '',
        telegram_file_id: str = '',
    ) -> None:
        """Mark a clip job as delivered."""
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute('SELECT user_id, order_id FROM clip_jobs WHERE job_id = ?', (job_id,)).fetchone()
            conn.execute(
                """
                UPDATE clip_jobs
                SET status = 'delivered', output_path = ?, telegram_file_id = ?, error = '', updated_at = ?
                WHERE job_id = ?
                """,
                (output_path, telegram_file_id, now, job_id),
            )
            self._log_audit_conn(
                conn,
                'clip_delivered',
                actor_id=row['user_id'] if row else '',
                order_id=row['order_id'] if row else '',
                product_id=CLIP_PRODUCT_ID,
                details={'job_id': job_id, 'telegram_file_id': telegram_file_id},
            )

    def mark_clip_job_failed(self, job_id: str, error: str) -> None:
        """Mark a clip job as failed (frees trial slot if trial failed before delivery)."""
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute('SELECT user_id, order_id FROM clip_jobs WHERE job_id = ?', (job_id,)).fetchone()
            conn.execute(
                """
                UPDATE clip_jobs
                SET status = 'failed', error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (error[:500], now, job_id),
            )
            self._log_audit_conn(
                conn,
                'clip_failed',
                actor_id=row['user_id'] if row else '',
                order_id=row['order_id'] if row else '',
                product_id=CLIP_PRODUCT_ID,
                details={'job_id': job_id, 'error': error[:500]},
            )

    def _ensure_kids_product_conn(
        self,
        conn: sqlite3.Connection,
        price_xtr: int = KIDS_VIDEO_PRICE_XTR,
        price_uah: int = KIDS_VIDEO_PRICE_UAH,
    ) -> None:
        """Ensure the personalized kids HD 9:16 video product exists in products table."""
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO products (
                product_id, category, title, description, package_contents,
                price_uah, price_xtr, status, package_ready, package_path,
                package_sha256, package_bytes, active_version, updated_at
            ) VALUES (?, 'video', 'Именное HD-видео 9:16 для ребёнка', ?, 'Вертикальное видео 1080x1920 MP4 с книгой сказок, колокольчиками и голосом',
                ?, ?, 'готов к продаже', 0, '', '', 0, 1, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                price_uah = excluded.price_uah,
                price_xtr = excluded.price_xtr,
                status = 'готов к продаже',
                updated_at = excluded.updated_at
            """,
            (
                KIDS_VIDEO_PRODUCT_ID,
                'Персональное видео-поздравление 9:16 от Святого Николая / Деда Мороза / Санты.',
                int(price_uah),
                int(price_xtr),
                now,
            ),
        )

    def create_kids_lead_job(
        self,
        user_id: str,
        chat_id: str,
        username: str,
        child_name: str,
        age: int | None,
        city: str,
        good_deed: str,
        lang: str,
        persona: str,
        photo_file_id: str = '',
    ) -> dict:
        """Store a free lead-magnet greeting job so the user can upgrade to paid HD 9:16 video."""
        uid = str(user_id)
        now = utc_now_iso()
        job_id = f'kids_{uuid.uuid4().hex[:14]}'
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                """
                INSERT INTO kids_jobs (
                    job_id, user_id, chat_id, username, child_name, age, city, good_deed,
                    lang, persona, photo_file_id, order_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'lead_sent', ?, ?)
                """,
                (
                    job_id, uid, str(chat_id), username, child_name, age,
                    city, good_deed, lang, persona, photo_file_id, now, now,
                ),
            )
            self._log_audit_conn(
                conn,
                'kids_greeting_created',
                actor_id=uid,
                product_id=KIDS_VIDEO_PRODUCT_ID,
                details={'job_id': job_id, 'child_name': child_name, 'lang': lang, 'persona': persona},
            )
            row = conn.execute('SELECT * FROM kids_jobs WHERE job_id = ?', (job_id,)).fetchone()
            return dict(row)

    def get_kids_job(self, job_id: str) -> dict | None:
        """Return kids_job by job_id."""
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM kids_jobs WHERE job_id = ?', (job_id,)).fetchone()
            return dict(row) if row else None

    def create_kids_paid_order(
        self,
        job_id: str,
        user_id: str,
        chat_id: str,
        username: str,
        price_xtr: int = KIDS_VIDEO_PRICE_XTR,
    ) -> tuple[dict, dict]:
        """Create an XTR Stars order for an HD 9:16 kids greeting video."""
        uid = str(user_id)
        now = utc_now_iso()
        amount_xtr = int(price_xtr)
        if amount_xtr <= 0:
            raise ValueError(f'Invalid kids video price_xtr: {amount_xtr}')
        order_id = f'ord_{uuid.uuid4().hex[:16]}'
        invoice_payload = f'naum:{order_id}'

        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            src_row = conn.execute('SELECT * FROM kids_jobs WHERE job_id = ?', (job_id,)).fetchone()
            if src_row is None or str(src_row['user_id']) != uid:
                raise ValueError('Kids greeting job not found for user')

            self._ensure_kids_product_conn(conn, price_xtr=amount_xtr)
            target_job_id = job_id
            if src_row['order_id']:
                target_job_id = f'kids_{uuid.uuid4().hex[:14]}'
                conn.execute(
                    """
                    INSERT INTO kids_jobs (
                        job_id, user_id, chat_id, username, child_name, age, city, good_deed,
                        lang, persona, photo_file_id, order_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?, ?)
                    """,
                    (
                        target_job_id, uid, str(chat_id), username, src_row['child_name'],
                        src_row['age'], src_row['city'], src_row['good_deed'], src_row['lang'],
                        src_row['persona'], src_row['photo_file_id'], order_id, now, now,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE kids_jobs SET order_id = ?, status = 'awaiting_payment', updated_at = ? WHERE job_id = ?",
                    (order_id, now, target_job_id),
                )

            conn.execute(
                """
                INSERT INTO orders (
                    order_id, user_id, chat_id, username, product_id, product_version,
                    amount_xtr, currency, invoice_payload, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, 'XTR', ?, 'created', ?, ?)
                """,
                (order_id, uid, str(chat_id), username, KIDS_VIDEO_PRODUCT_ID, amount_xtr, invoice_payload, now, now),
            )
            self._log_audit_conn(
                conn,
                'kids_order_created',
                actor_id=uid,
                order_id=order_id,
                product_id=KIDS_VIDEO_PRODUCT_ID,
                details={'job_id': target_job_id, 'amount_xtr': amount_xtr},
            )
            order_row = conn.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,)).fetchone()
            job_row = conn.execute('SELECT * FROM kids_jobs WHERE job_id = ?', (target_job_id,)).fetchone()
            return dict(order_row), dict(job_row)

    def reserve_kids_video_delivery(self, order_id: str) -> dict | None:
        """Atomically transition an awaiting_payment paid kids video job to processing."""
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            cur = conn.execute(
                """
                UPDATE kids_jobs
                SET status = 'processing', updated_at = ?
                WHERE order_id = ? AND status = 'awaiting_payment'
                  AND EXISTS (SELECT 1 FROM orders o WHERE o.order_id = ? AND o.status = 'paid')
                """,
                (now, order_id, order_id),
            )
            if cur.rowcount != 1:
                return None
            row = conn.execute('SELECT * FROM kids_jobs WHERE order_id = ?', (order_id,)).fetchone()
            return dict(row) if row else None

    def mark_kids_job_delivered(self, job_id: str, telegram_file_id: str = '') -> None:
        """Mark a kids video job as delivered."""
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute('SELECT user_id, order_id FROM kids_jobs WHERE job_id = ?', (job_id,)).fetchone()
            conn.execute(
                "UPDATE kids_jobs SET status = 'delivered', telegram_file_id = ?, error = '', updated_at = ? WHERE job_id = ?",
                (telegram_file_id, now, job_id),
            )
            self._log_audit_conn(
                conn,
                'kids_video_delivered',
                actor_id=row['user_id'] if row else '',
                order_id=row['order_id'] if row else '',
                product_id=KIDS_VIDEO_PRODUCT_ID,
                details={'job_id': job_id, 'telegram_file_id': telegram_file_id},
            )

    def mark_kids_job_failed(self, job_id: str, error: str) -> None:
        """Mark a kids video job as failed."""
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute('SELECT user_id, order_id FROM kids_jobs WHERE job_id = ?', (job_id,)).fetchone()
            conn.execute(
                "UPDATE kids_jobs SET status = 'failed', error = ?, updated_at = ? WHERE job_id = ?",
                (error[:500], now, job_id),
            )
            self._log_audit_conn(
                conn,
                'kids_video_failed',
                actor_id=row['user_id'] if row else '',
                order_id=row['order_id'] if row else '',
                product_id=KIDS_VIDEO_PRODUCT_ID,
                details={'job_id': job_id, 'error': error[:500]},
            )
