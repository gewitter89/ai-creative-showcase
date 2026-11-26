import json
import sqlite3
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import agent_control_bridge
from store_db import StoreDB


def test_agent_control_bridge_snapshot_and_sync(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    packages_dir = tmp_path / 'packages'
    packages_dir.mkdir()
    for pid in ('ny-kyiv-01', 'ny-cozy-02', 'ny-minimal-03', 'demo-ny'):
        with zipfile.ZipFile(packages_dir / f'{pid}.zip', 'w') as zf:
            zf.writestr('README.txt', b'ok')

    promo_dir = tmp_path / 'promo'
    promo_dir.mkdir()
    (promo_dir / 'demo_ny_reel.mp4').write_bytes(b'mp4')

    db_path = tmp_path / 'store.db'
    store = StoreDB(db_path)
    store.init_schema()
    outcome, order, _ = store.record_demo_delivery_start(
        user_id='7001',
        chat_id='7001',
        username='lead_cloud',
        package_path=packages_dir / 'demo-ny.zip',
        package_sha256='abc123',
    )
    assert outcome == 'ready' and order is not None
    store.mark_delivery_sent(order['order_id'], telegram_file_id='tg_demo_1')

    snap = agent_control_bridge.collect_pipeline_snapshot(
        repo_root=repo_root,
        db_path=db_path,
        packages_dir=packages_dir,
        promo_dir=promo_dir,
    )
    assert snap['stages'] == list(agent_control_bridge.PIPELINE_STAGES)
    assert snap['demo_reel_ready'] is True
    assert snap['demo_package_ready'] is True
    assert snap['demo']['id'] == 'demo-ny'
    assert snap['demo']['deeplink'] == 'demo_ny'
    assert snap['demo']['deliveries_sent'] == 1
    assert snap['demo']['unique_leads'] == 1
    assert snap['summary'] == {
        'total_products': 9,
        'packages_ready': 3,
        'packages_pending': 6,
        'approved_for_sale': 0,
        'in_qa_or_build': 9,
        'promo_reels_ready': 0,
        'demo_ready': True,
    }
    assert snap['stage_counts']['qa'] == 3
    assert snap['stage_counts']['processing'] == 6
    assert snap['analytics']['demo_deliveries_sent'] == 1
    assert snap['blockers'][0]['id'] == 'free_image_generator'
    assert snap['blockers'][0]['paid_apis_allowed'] is False

    batch_02 = snap['batch_02']
    assert batch_02['present'] is True
    assert batch_02['batch'] == '02'
    assert batch_02['prompts_doc_present'] is True
    assert batch_02['total_items'] == 9
    assert batch_02['in_catalog_count'] == 0
    assert batch_02['prompts_ready'] == 9
    assert batch_02['originals_ready'] == 0
    assert batch_02['previews_ready'] == 0
    assert batch_02['packages_ready'] == 0
    assert batch_02['stage_counts']['specification'] == 9
    assert batch_02['by_category'] == {'valentine': 2, 'spring': 1, 'photoshop': 3, '3d': 3}

    by_id = {x['id']: x for x in snap['items']}
    assert by_id['ny-kyiv-01']['stage'] == 'qa'
    assert by_id['ny-kyiv-01']['package_ready'] is True
    assert by_id['ps-pack-01']['stage'] == 'processing'
    assert by_id['ps-pack-01']['package_ready'] is False

    ac_db = tmp_path / 'agent_control.sqlite3'
    sqlite3.connect(str(ac_db)).close()
    sync_res = agent_control_bridge.sync_snapshot_to_agent_control(ac_db, snap)
    assert sync_res['ok'] is True

    with sqlite3.connect(str(ac_db)) as conn:
        meta = dict(conn.execute('SELECT k, v FROM ac_meta').fetchall())
        assert 'naum_showcase_pipeline' in meta
        assert 'naum_showcase_blocker_generation' in meta
        assert 'naum_showcase_batch_02' in meta
        pipeline = json.loads(meta['naum_showcase_pipeline'])
        assert pipeline['summary']['packages_ready'] == 3
        assert pipeline['summary']['packages_pending'] == 6
        assert pipeline['summary']['demo_ready'] is True
        b2_meta = json.loads(meta['naum_showcase_batch_02'])
        assert b2_meta['total_items'] == 9
        assert b2_meta['in_catalog_count'] == 0
        blocker = json.loads(meta['naum_showcase_blocker_generation'])
        assert blocker['stage'] == 'generation'
        logs = conn.execute('SELECT message FROM ac_logs').fetchall()
        assert len(logs) == 1
        assert 'ready=3, pending=6, demo_ready=True' in logs[0][0]
        assert 'batch_02=9 items (prompts=9, previews=0, ready=0, in_catalog=0)' in logs[0][0]


def test_batch_02_manifest_and_prompts_contract(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    catalog = json.loads((repo_root / 'catalog.json').read_text(encoding='utf-8'))
    catalog_ids = {item['id'] for item in catalog['items']}
    assert len(catalog_ids) == 9

    manifest = json.loads((repo_root / 'manifests' / 'batch_02_manifest.json').read_text(encoding='utf-8'))
    prompts_md = (repo_root / 'manifests' / 'batch_02_prompts.md').read_text(encoding='utf-8')
    items = manifest['items']
    assert 6 <= len(items) <= 9
    assert manifest['batch'] == '02'
    assert manifest['in_catalog'] is False

    ids = [item['id'] for item in items]
    assert len(set(ids)) == len(ids)
    assert catalog_ids.isdisjoint(ids)

    categories = {item['category'] for item in items}
    assert {'valentine', 'spring', 'photoshop', '3d'}.issubset(categories)

    for item in items:
        for field in (
            'id',
            'category',
            'category_label',
            'title',
            'description',
            'format',
            'package_contents',
            'preview_path',
            'delivery_requirements',
            'prompt',
            'status',
        ):
            assert item[field]
        assert int(item['proposed_price_uah']) > 0
        assert int(item['proposed_price_xtr']) > 0
        assert item['id'] in prompts_md
        assert item['preview_path'] in prompts_md

    missing_report = agent_control_bridge.collect_batch_02_report(
        tmp_path,
        catalog_ids=catalog_ids,
        packages_dir=tmp_path,
        promo_dir=tmp_path,
    )
    assert missing_report['present'] is False
    assert missing_report['total_items'] == 0
