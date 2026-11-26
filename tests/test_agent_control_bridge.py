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
    with zipfile.ZipFile(packages_dir / 'ny-kyiv-01.zip', 'w') as zf:
        zf.writestr('README.txt', b'ok')

    promo_dir = tmp_path / 'promo'
    promo_dir.mkdir()
    (promo_dir / 'demo_ny_reel.mp4').write_bytes(b'mp4')

    db_path = tmp_path / 'store.db'
    store = StoreDB(db_path)
    store.init_schema()

    snap = agent_control_bridge.collect_pipeline_snapshot(
        repo_root=repo_root,
        db_path=db_path,
        packages_dir=packages_dir,
        promo_dir=promo_dir,
    )
    assert snap['stages'] == list(agent_control_bridge.PIPELINE_STAGES)
    assert snap['demo_reel_ready'] is True
    assert snap['blockers'][0]['id'] == 'free_image_generator'
    assert snap['blockers'][0]['paid_apis_allowed'] is False
    by_id = {x['id']: x for x in snap['items']}
    assert by_id['ny-kyiv-01']['stage'] == 'qa'
    assert by_id['ps-pack-01']['stage'] == 'processing'

    ac_db = tmp_path / 'agent_control.sqlite3'
    sqlite3.connect(str(ac_db)).close()
    sync_res = agent_control_bridge.sync_snapshot_to_agent_control(ac_db, snap)
    assert sync_res['ok'] is True

    with sqlite3.connect(str(ac_db)) as conn:
        meta = dict(conn.execute('SELECT k, v FROM ac_meta').fetchall())
        assert 'naum_showcase_pipeline' in meta
        assert 'naum_showcase_blocker_generation' in meta
        blocker = json.loads(meta['naum_showcase_blocker_generation'])
        assert blocker['stage'] == 'generation'
