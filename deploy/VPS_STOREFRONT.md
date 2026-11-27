# Развёртывание витрины NAUM AI Creative Studio на VPS

Витрина размещается на существующем VPS (без использования GitHub Pages для коммерческих продаж).

## 1. Архитектура раздачи
- `scripts/serve_storefront.py` слушает только `127.0.0.1:8090` и отдаёт строго белый список публичных файлов:
  - `/`, `/index.html`, `/app.js`, `/styles.css`, `/catalog.json`
  - `/assets/<category>/<id>/preview.webp`
- Все внутренние файлы (`.git/`, `scripts/`, `tests/`, `.kiro/`, `generation_manifest.json`) отдают `404 Not Found`.

## 2. Локальный запуск для проверки
```bash
python3 /opt/ai-creative-showcase/scripts/serve_storefront.py --host 127.0.0.1 --port 8090
curl -I http://127.0.0.1:8090/
```

## 3. Установка systemd-сервиса (после одобрения владельца)
```bash
sudo cp /opt/ai-creative-showcase/deploy/naum-storefront.service /etc/systemd/system/naum-storefront.service
sudo systemctl daemon-reload
sudo systemctl enable --now naum-storefront.service
sudo systemctl status naum-storefront.service
```

## 4. Публикация через Caddy (HTTPS)
Добавьте блок из `deploy/Caddyfile.snippet` в `/etc/caddy/Caddyfile` (не удаляя существующие маршруты) и выполните:
```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## 5. Включение реальных платежей Telegram Stars (только после одобрения владельца)
1. Проверьте комплекты и переведите нужные товары в `catalog.json` в `"status": "готов к продаже"`.
2. В `/etc/naum-studio-bot.env` добавьте:
   ```ini
   STUDIO_PAYMENTS_ENABLED=1
   STUDIO_DB_PATH=/var/lib/naum-studio/store.db
   STUDIO_PACKAGES_DIR=/var/lib/naum-studio/packages
   ```
3. Перезапустите бота:
   ```bash
   sudo systemctl restart naum-studio-bot.service
   ```
