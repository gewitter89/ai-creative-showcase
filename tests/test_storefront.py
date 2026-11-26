import json
import subprocess
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
    demo = catalog.get('demo_offer')
    assert isinstance(demo, dict)
    assert demo['id'] == 'demo-ny'
    assert demo['deeplink'] == 'demo_ny'
    assert int(demo['price_xtr']) == 0
    assert int(demo['price_uah']) == 0
    items = catalog['items']
    assert len(items) == 9

    ready_ids = {x['id'] for x in items if x.get('package_ready') is True}
    assert ready_ids == {'ny-kyiv-01', 'ny-cozy-02', 'ny-minimal-03'}

    for item in items:
        assert item['status'] == 'проверка качества'
        assert int(item['price_xtr']) > 0
        assert item['package_contents']
        assert (REPO_ROOT / item['image']).is_file()

    html = (REPO_ROOT / 'index.html').read_text(encoding='utf-8')
    css = (REPO_ROOT / 'styles.css').read_text(encoding='utf-8')
    js = (REPO_ROOT / 'app.js').read_text(encoding='utf-8')

    assert './styles.css' in html
    assert './app.js' in html
    assert '@media (max-width: 640px)' in css
    assert 'buy_' in js
    assert 'demo_ny' in js
    assert 'demo-banner' in html
    assert "status === 'готов к продаже' && packageReady" in js
    assert 'package_contents' in js
    assert 'github.io' not in html.lower()
    assert 'github.io' not in js.lower()


def test_storefront_deeplink_js_outputs():
    js = (REPO_ROOT / 'app.js').read_text(encoding='utf-8')
    cat_json = (REPO_ROOT / 'catalog.json').read_text(encoding='utf-8')
    stub = (
        'const els = {\n'
        '  "demo-banner": { hidden: true, innerHTML: "" },\n'
        '  "tg-top": { hidden: true, href: "", removeAttribute() {} },\n'
        '  "grid": { innerHTML: "", textContent: "" },\n'
        '  "q": { value: "", addEventListener() {} }\n'
        '};\n'
        'const document = {\n'
        '  querySelectorAll: () => [],\n'
        '  getElementById: (id) => els[id] || null\n'
        '};\n'
    )
    script = (
        stub
        + js.split('fetch(')[0]
        + f'\ncatalog = {cat_json};\nrender();\n'
        + 'const demoHtml = els["demo-banner"].innerHTML;\n'
        + 'const qaGridHtml = els["grid"].innerHTML;\n'
        + 'catalog.items[0].status = "готов к продаже";\n'
        + 'catalog.items[3].status = "готов к продаже";\n'
        + 'catalog.items[3].package_ready = true;\n'
        + 'render();\n'
        + 'const readyGridHtml = els["grid"].innerHTML;\n'
        + 'console.log(JSON.stringify({\n'
        + "  buy: buildDeepLink('https://t.me/NaumCreativeStudioBot', 'ny-kyiv-01'),\n"
        + "  demo: buildDemoLink('https://t.me/NaumCreativeStudioBot', 'demo_ny'),\n"
        + '  demoHtml,\n'
        + '  qaGridHtml,\n'
        + '  readyGridHtml\n'
        + '}));\n'
    )
    proc = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    out = json.loads(proc.stdout)
    assert out['buy'] == 'https://t.me/NaumCreativeStudioBot?start=buy_ny-kyiv-01'
    assert out['demo'] == 'https://t.me/NaumCreativeStudioBot?start=demo_ny'
    assert 'https://t.me/NaumCreativeStudioBot?start=demo_ny' in out['demoHtml']
    assert 'https://t.me/NaumCreativeStudioBot?start=buy_ny-kyiv-01' in out['readyGridHtml']
    assert 'https://t.me/NaumCreativeStudioBot?start=buy_ps-pack-01' in out['readyGridHtml']
    assert 'https://t.me/NaumCreativeStudioBot?start=buy_' not in out['qaGridHtml']


def test_storefront_server_allowlist_all_9_previews_and_traversal_protection():
    catalog = json.loads((REPO_ROOT / 'catalog.json').read_text(encoding='utf-8'))
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
            '/studio',
            '/studio/',
        ):
            with urllib.request.urlopen(base + ok_path, timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers.get('X-Content-Type-Options') == 'nosniff'

        for item in catalog['items']:
            preview_url = f"{base}/{item['image']}"
            with urllib.request.urlopen(preview_url, timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers.get('Content-Type') == 'image/webp'
                raw = resp.read()
                assert raw[:4] == b'RIFF' and raw[8:12] == b'WEBP'

        head_req = urllib.request.Request(base + '/', method='HEAD')
        with urllib.request.urlopen(head_req, timeout=5) as resp:
            assert resp.status == 200

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
