# NAUM AI Creative Studio — Commercial Release Design

## 1. Архитектура по этапам (0 грн доп. расходов)
- **Этап 1 (Безопасность):** fail-closed при пустом `STUDIO_OWNER_IDS`, загрузка оригиналов только владельцем в приватном чате, `0600` + SHA-256 версионирование в `/var/lib/naum-studio/originals/`, атомарные JSON-обновления под `fcntl.flock`, запрет пуша в `main`.
- **Этап 2 (ZIP-комплекты):** `scripts/build_packages.py` собирает P0-открытки (`artwork_original.png`, `greeting_ua.png` 1772×1181 @ 300 dpi, `print_150x100mm.jpg`, `social_media.jpg`, `README.txt`, `LICENSE.txt`) и бесплатный лид-магнит `demo-ny.zip` (`1200×800` с пометкой `DEMO`, без раскрытия платных оригиналов).
- **Этап 3 (Витрина на VPS):** `scripts/serve_storefront.py` (localhost allowlist) + `index.html` / `app.js` / `styles.css` + баннер бесплатного демо (`/start demo_ny`) + deep link `?start=buy_<id>` только для `status == 'готов к продаже' && package_ready`.
- **Этап 4 (Магазин Telegram Stars):** `scripts/store_db.py` + `scripts/studio_bot.py` (`XTR`, `sendInvoice`, `pre_checkout_query`, `successful_payment`, идемпотентность, `/myorders`, `/support`, `/paysupport`, `/refund`).

## 2. Этап 5 — Интеграция в существующий `AGENT CONTROL`
- Без создания отдельной фабрики: `scripts/agent_control_bridge.py` отображает 11 стадий (`research → specification → generation → processing → packaging → qa → approved → publication → orders → delivery → analytics`) в таблицу `ac_meta`/`ac_logs` существующего `AGENT CONTROL` и агрегирует метрики из `StoreDB.get_metrics_summary()`.
- **Зафиксированный блокер (`free_image_generator`):** автоматический бесплатный генератор изображений (0 грн, без платных API) не подключён; генерация новых оригиналов остаётся ручным/внешним шагом с последующим инджестом через приватный бот.

## 3. Этап 6 — Лид-магнит, промо-ролики и метрики (0 грн)
- **Лид-магнит (`/start demo_ny` и `/catalog`):** работает без оплаты и даже при `STUDIO_PAYMENTS_ENABLED=0`. Идемпотентная выдача `demo-ny.zip` с записью в `products`, `orders` (`currency='FREE'`, `amount_xtr=0`), `deliveries` и `audit_events` (`demo_requested` / `demo_repeat_requested` / `delivery_sent`).
- **Аналитика:** `StoreDB.get_metrics_summary()` и админ-команда `/stats` (только владелец в приватном чате) считают уникальных лидов, повторные запросы, платные заказы, выручку XTR и конверсию демо → покупка.
- **Промо-ролики (`scripts/build_promo_reels.py`):** локальный рендер на Pillow + `ffmpeg` (0 грн) вертикальных MP4 `1080×1920` (H.264 `yuv420p`, `zoompan`, титры, pure-Python QR-код на `https://t.me/NaumCreativeStudioBot?start=demo_ny` / `buy_<id>`) с сохранением в `/var/lib/naum-studio/promo/`.
