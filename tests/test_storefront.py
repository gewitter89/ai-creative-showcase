import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import serve_storefront


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_catalog_and_storefront_assets_contract():
    catalog = json.loads((REPO_ROOT / 'catalog.json').read_text(encoding='utf-8'))
    assert catalog['telegram_contact'] == 'https://t.me/NaumCreativeStudioBot'
    items = catalog['items']
    assert len(items) == 9

    ready_ids = {x['id'] for x in items if x.get('package_ready') is True}
    assert ready_ids == {'ny-kyiv-01', 'ny-cozy-02', 'ny-minimal-03'}

    for item in items:
        assert item['status'] == 'проверка качества'
        assert int(item['price_xtr']) > 0
        assert item['package_contents']

    html = (REPO_ROOT / 'index.html').read_text(encoding='utf-8')
    css = (REPO_ROOT / 'styles.css').read_text(encoding='utf-8')
    js = (REPO_ROOT / 'app.js').read_text(encoding='utf-8')

    assert './styles.css' in html
    assert './app.js' in html
    assert '@media (max-width: 640px)' in css
    assert 'buy_' in js
    assert "status === 'готов к продаже' && packageReady" in js
    assert 'package_contents' in js


def test_storefront_server_allowlist_and_path_traversal_protection():
    srv = serve_storefront.create_server(REPO_ROOT, host='127.0.0.1', port=0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{port}'
        for ok_path in (
            '/',
            '/index.html',
            '/app.js',
            '/styles.css',
            '/catalog.json',
            '/studio/',
            '/assets/newyear/ny-kyiv-01/preview.webp',
        ):
            with urllib.request.urlopen(base + ok_path, timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers.get('X-Content-Type-Options') == 'nosniff'

        for blocked_path in (
            '/.git/HEAD',
            '/scripts/studio_bot.py',
            '/generation_manifest.json',
            '/tests/test_studio_bot.py',
            '/../etc/passwd',
            '/assets/newyear/ny-kyiv-01/../../catalog.json',
        ):
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + blocked_path, timeout=5)
            assert err.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)
