# NAUM AI Creative Studio — Commercial Release Tasks

- [x] Этап 1: Безопасность приёмника, `0600` оригиналы, SHA-256 версионирование, branch guard (`feat/production-batch-01`).
- [x] Этап 2: Сборка P0 ZIP-комплектов открыток (`ny-kyiv-01`, `ny-cozy-02`, `ny-minimal-03`).
- [x] Этап 3: Коммерческая витрина на VPS (`serve_storefront.py`, `deploy/`).
- [x] Этап 4: Магазин Telegram Stars в `studio_bot.py` + `store_db.py`.
- [x] Этап 5: Интеграция 11-стадийного цикла и метрик в `AGENT CONTROL` (`scripts/agent_control_bridge.py`) + фиксация блокера бесплатного генератора изображений (`free_image_generator`).
- [x] Этап 6.1: Бесплатная демо-открытка (лид-магнит) `demo-ny` в `/catalog`, на витрине и выдача через `/start demo_ny` с записью в `deliveries` и `audit_events`.
- [x] Этап 6.2: Генератор вертикальных промо-роликов `1080x1920` MP4 (`scripts/build_promo_reels.py`) на Pillow + ffmpeg с QR и ссылкой на `@NaumCreativeStudioBot`.
- [x] Этап 6.3: Покрытие тестами `pytest`, прогон `pytest -q`, пуш в `origin/feat/production-batch-01`.
- [ ] Ручное действие владельца (после ревью): генерация промо-MP4 в `/var/lib/naum-studio/promo/` под root, включение `STUDIO_PAYMENTS_ENABLED=1` и перевод выбранных товаров в статус `готов к продаже`.
