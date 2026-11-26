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
        pipeline = json.loads(meta['naum_showcase_pipeline'])
        assert pipeline['summary']['packages_ready'] == 3
        assert pipeline['summary']['packages_pending'] == 6
        assert pipeline['summary']['demo_ready'] is True
        blocker = json.loads(meta['naum_showcase_blocker_generation'])
        assert blocker['stage'] == 'generation'
        logs = conn.execute('SELECT message FROM ac_logs').fetchall()
        assert len(logs) == 1
        assert 'ready=3, pending=6, demo_ready=True' in logs[0][0]
