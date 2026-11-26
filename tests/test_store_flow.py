import json
import shutil
import sqlite3
import stat
import sys
import zipfile
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio_bot
from store_db import StoreDB


REQUIRED_TABLES = {
    'products',
    'product_versions',
    'orders',
    'payments',
    'deliveries',
    'support_tickets',
    'processed_updates',
    'audit_events',
}


def _make_zip(path: Path, content: bytes = b'PK-test-zip') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('README.txt', content)


@pytest.fixture()
def store_env(tmp_path: Path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / 'catalog.json').read_text(encoding='utf-8'))
    for item in catalog['items']:
        if item['id'] == 'ny-kyiv-01':
            item['status'] = 'готов к продаже'
            item['badge'] = 'Готов к продаже'
    (repo / 'catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
    shutil.copy2(root / 'generation_manifest.json', repo / 'generation_manifest.json')

    packages_dir = tmp_path / 'packages'
    _make_zip(packages_dir / 'ny-kyiv-01.zip', b'kyiv-package')
    _make_zip(packages_dir / 'ny-cozy-02.zip', b'cozy-package')

    db_path = tmp_path / 'store.db'
    cfg = studio_bot.BotConfig(
        token='12345:SECRET_TOKEN',
        owner_ids=frozenset({'111222'}),
        repo=repo,
        private_dir=tmp_path / 'private',
        auto_push=False,
        db_path=db_path,
        packages_dir=packages_dir,
        payments_enabled=True,
    )
    return cfg, db_path, packages_dir


def test_store_schema_tables_and_config_defaults(store_env):
    cfg, db_path, _ = store_env
    store = studio_bot.ensure_store(cfg)
    assert store is not None
    assert db_path.is_file()
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    with sqlite3.connect(str(db_path)) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert REQUIRED_TABLES.issubset(tables)
        ready = conn.execute('SELECT product_id, active_version FROM products WHERE package_ready = 1').fetchall()
        assert {r[0] for r in ready} == {'ny-kyiv-01', 'ny-cozy-02'}
        assert all(r[1] == 1 for r in ready)

    loaded = studio_bot.load_config({'STUDIO_BOT_TOKEN': '123:xyz', 'STUDIO_OWNER_IDS': '111222'})
    assert loaded.payments_enabled is False
    assert str(loaded.db_path) == '/var/lib/naum-studio/store.db'


def test_catalog_shows_only_ready_packages_and_preserves_owner_items(store_env):
    cfg, _, _ = store_env
    calls = []

    def fake_api(token, method, payload=None):
        calls.append((method, payload))
        return {'ok': True}

    # Customer /catalog sees only ny-kyiv-01 (ready for sale when payments_enabled=True)
    studio_bot.process_update(
        {
            'update_id': 101,
            'message': {
                'from': {'id': 999001},
                'chat': {'id': 999001, 'type': 'private'},
                'text': '/catalog',
            },
        },
        cfg,
        api_fn=fake_api,
    )
    assert len(calls) == 1
    cat_text = calls[0][1]['text']
    assert 'ny-kyiv-01' in cat_text
    assert 'ps-pack-01' not in cat_text
    assert '3d-ref-01' not in cat_text

    # Customer /items is ignored; owner /items shows all 9 items
    studio_bot.process_update(
        {
            'update_id': 102,
            'message': {
                'from': {'id': 999001},
                'chat': {'id': 999001, 'type': 'private'},
                'text': '/items',
            },
        },
        cfg,
        api_fn=fake_api,
    )
    assert len(calls) == 1

    studio_bot.process_update(
        {
            'update_id': 103,
            'message': {
                'from': {'id': 111222},
                'chat': {'id': 111222, 'type': 'private'},
                'text': '/items',
            },
        },
        cfg,
        api_fn=fake_api,
    )
    assert len(calls) == 2
    assert 'ps-pack-01' in calls[1][1]['text']
    assert '3d-owl-03' in calls[1][1]['text']


def test_full_stars_purchase_idempotency_delivery_support_and_refund(store_env):
    cfg, db_path, packages_dir = store_env
    api_calls = []
    sent_docs = []

    def fake_api(token, method, payload=None):
        api_calls.append((method, payload))
        return {'ok': True}

    def fake_send_doc(token, chat_id, file_path, caption=''):
        sent_docs.append((chat_id, Path(file_path), caption))
        return {'ok': True, 'result': {'document': {'file_id': 'tg_zip_file_123'}}}

    # 1. Deep link /start buy_ny-kyiv-01 sends XTR invoice
    studio_bot.process_update(
        {
            'update_id': 200,
            'message': {
                'from': {'id': 555001, 'username': 'buyer1'},
                'chat': {'id': 555001, 'type': 'private'},
                'text': '/start buy_ny-kyiv-01',
            },
        },
        cfg,
        api_fn=fake_api,
        send_document_fn=fake_send_doc,
    )
    assert api_calls[-1][0] == 'sendInvoice'
    inv = api_calls[-1][1]
    assert inv['currency'] == 'XTR'
    assert inv['provider_token'] == ''
    assert inv['prices'] == [{'label': 'Киев в новогодних огнях', 'amount': 150}]
    invoice_payload = inv['payload']
    order_id = invoice_payload.split(':', 1)[1]

    # 2. Tampered pre_checkout_query (wrong amount, wrong currency, wrong user) must be rejected
    for idx, bad_pcq in enumerate([
        {'id': 'q1', 'from': {'id': 555001}, 'currency': 'XTR', 'total_amount': 1, 'invoice_payload': invoice_payload},
        {'id': 'q2', 'from': {'id': 555001}, 'currency': 'USD', 'total_amount': 150, 'invoice_payload': invoice_payload},
        {'id': 'q3', 'from': {'id': 777777}, 'currency': 'XTR', 'total_amount': 150, 'invoice_payload': invoice_payload},
    ]):
        studio_bot.process_update(
            {'update_id': 201 + idx, 'pre_checkout_query': bad_pcq},
            cfg,
            api_fn=fake_api,
        )
        assert api_calls[-1][0] == 'answerPreCheckoutQuery'
        assert api_calls[-1][1]['ok'] is False

    # 3. Valid pre_checkout_query passes
    studio_bot.process_update(
        {
            'update_id': 205,
            'pre_checkout_query': {
                'id': 'q_valid',
                'from': {'id': 555001},
                'currency': 'XTR',
                'total_amount': 150,
                'invoice_payload': invoice_payload,
            },
        },
        cfg,
        api_fn=fake_api,
    )
    assert api_calls[-1] == ('answerPreCheckoutQuery', {'pre_checkout_query_id': 'q_valid', 'ok': True})

    # 4. successful_payment delivers ZIP and records payment/delivery
    payment_upd = {
        'update_id': 206,
        'message': {
            'from': {'id': 555001},
            'chat': {'id': 555001, 'type': 'private'},
            'successful_payment': {
                'currency': 'XTR',
                'total_amount': 150,
                'invoice_payload': invoice_payload,
                'telegram_payment_charge_id': 'chg_star_abc123',
                'provider_payment_charge_id': '',
            },
        },
    }
    studio_bot.process_update(payment_upd, cfg, api_fn=fake_api, send_document_fn=fake_send_doc)
    assert len(sent_docs) == 1
    assert sent_docs[0][0] == 555001
    assert sent_docs[0][1] == (packages_dir / 'ny-kyiv-01.zip').resolve()
    assert order_id in sent_docs[0][2]

    # 5. Idempotency: same update_id AND new update_id with same charge_id must NOT re-deliver
    studio_bot.process_update(payment_upd, cfg, api_fn=fake_api, send_document_fn=fake_send_doc)
    dup_charge_upd = dict(payment_upd)
    dup_charge_upd['update_id'] = 207
    studio_bot.process_update(dup_charge_upd, cfg, api_fn=fake_api, send_document_fn=fake_send_doc)
    assert len(sent_docs) == 1

    # 6. /myorders, /support, /paysupport
    studio_bot.process_update(
        {
            'update_id': 208,
            'message': {
                'from': {'id': 555001},
                'chat': {'id': 555001, 'type': 'private'},
                'text': '/myorders',
            },
        },
        cfg,
        api_fn=fake_api,
    )
    assert order_id in api_calls[-1][1]['text']
    assert 'delivered' in api_calls[-1][1]['text']

    studio_bot.process_update(
        {
            'update_id': 209,
            'message': {
                'from': {'id': 555001},
                'chat': {'id': 555001, 'type': 'private'},
                'text': f'/paysupport {order_id} нужна квитанция',
            },
        },
        cfg,
        api_fn=fake_api,
    )
    assert 'зарегистрировано' in api_calls[-1][1]['text']

    # 7. Refund only allowed for owner in private chat
    before_calls = len(api_calls)
    studio_bot.process_update(
        {
            'update_id': 210,
            'message': {
                'from': {'id': 555001},
                'chat': {'id': 555001, 'type': 'private'},
                'text': f'/refund {order_id}',
            },
        },
        cfg,
        api_fn=fake_api,
    )
    assert len(api_calls) == before_calls

    studio_bot.process_update(
        {
            'update_id': 211,
            'message': {
                'from': {'id': 111222},
                'chat': {'id': 111222, 'type': 'private'},
                'text': f'/refund {order_id}',
            },
        },
        cfg,
        api_fn=fake_api,
    )
    assert api_calls[-2] == (
        'refundStarPayment',
        {'user_id': 555001, 'telegram_payment_charge_id': 'chg_star_abc123'},
    )
    assert 'возврат Stars' in api_calls[-1][1]['text']

    # Verify DB state and audit trail
    store = StoreDB(db_path)
    order = store.get_order(order_id)
    assert order is not None and order['status'] == 'refunded'
    payment = store.get_payment_by_order(order_id)
    assert payment is not None and payment['status'] == 'refunded'
    delivery = store.get_delivery_by_order(order_id)
    assert delivery is not None and delivery['telegram_file_id'] == 'tg_zip_file_123'


def test_qa_product_and_disabled_payments_do_not_send_invoice(store_env):
    cfg, _, _ = store_env
    calls = []

    def fake_api(token, method, payload=None):
        calls.append((method, payload))
        return {'ok': True}

    # QA item (ny-cozy-02 is still 'проверка качества') must NOT trigger sendInvoice
    studio_bot.process_update(
        {
            'update_id': 300,
            'message': {
                'from': {'id': 555002},
                'chat': {'id': 555002, 'type': 'private'},
                'text': '/start buy_ny-cozy-02',
            },
        },
        cfg,
        api_fn=fake_api,
    )
    assert calls[-1][0] == 'sendMessage'
    assert 'недоступен для оплаты' in calls[-1][1]['text']

    # payments_enabled=False blocks sendInvoice even for 'готов к продаже'
    paused_cfg = studio_bot.BotConfig(
        token=cfg.token,
        owner_ids=cfg.owner_ids,
        repo=cfg.repo,
        private_dir=cfg.private_dir,
        auto_push=False,
        db_path=cfg.db_path,
        packages_dir=cfg.packages_dir,
        payments_enabled=False,
    )
    studio_bot.process_update(
        {
            'update_id': 301,
            'message': {
                'from': {'id': 555002},
                'chat': {'id': 555002, 'type': 'private'},
                'text': '/start buy_ny-kyiv-01',
            },
        },
        paused_cfg,
        api_fn=fake_api,
    )
    assert calls[-1][0] == 'sendMessage'
    assert 'ожидает включения владельцем' in calls[-1][1]['text']


def test_successful_payment_mismatch_blocks_delivery_and_audits(store_env):
    cfg, db_path, _ = store_env
    calls = []
    docs = []

    def fake_api(token, method, payload=None):
        calls.append((method, payload))
        return {'ok': True}

    def fake_send_doc(token, chat_id, file_path, caption=''):
        docs.append((chat_id, file_path))
        return {'ok': True}

    studio_bot.process_update(
        {
            'update_id': 400,
            'message': {
                'from': {'id': 555003},
                'chat': {'id': 555003, 'type': 'private'},
                'text': '/start buy_ny-kyiv-01',
            },
        },
        cfg,
        api_fn=fake_api,
    )
    payload = calls[-1][1]['payload']

    # Tampered total_amount in successful_payment must NOT deliver ZIP
    studio_bot.process_update(
        {
            'update_id': 401,
            'message': {
                'from': {'id': 555003},
                'chat': {'id': 555003, 'type': 'private'},
                'successful_payment': {
                    'currency': 'XTR',
                    'total_amount': 1,
                    'invoice_payload': payload,
                    'telegram_payment_charge_id': 'chg_bad_1',
                },
            },
        },
        cfg,
        api_fn=fake_api,
        send_document_fn=fake_send_doc,
    )
    assert docs == []
    assert 'Не удалось подтвердить параметры платежа' in calls[-1][1]['text']

    with sqlite3.connect(str(db_path)) as conn:
        events = [r[0] for r in conn.execute('SELECT event_type FROM audit_events').fetchall()]
        assert 'payment_mismatch' in events


def test_send_telegram_document_multipart_upload(tmp_path: Path, monkeypatch):
    zip_file = tmp_path / 'ny-kyiv-01.zip'
    _make_zip(zip_file, b'hello-zip')
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true, "result": {"document": {"file_id": "doc_99"}}}'

    def fake_urlopen(req, timeout=0):
        captured['url'] = req.full_url
        captured['content_type'] = req.get_header('Content-type')
        captured['body'] = req.data
        captured['timeout'] = timeout
        return FakeResp()

    monkeypatch.setattr(studio_bot.urllib.request, 'urlopen', fake_urlopen)
    res = studio_bot.send_telegram_document('123:tok', 555001, zip_file, caption='Test ZIP')
    assert res['result']['document']['file_id'] == 'doc_99'
    assert 'multipart/form-data; boundary=' in captured['content_type']
    assert b'filename="ny-kyiv-01.zip"' in captured['body']
    assert b'Test ZIP' in captured['body']
