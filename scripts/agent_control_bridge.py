#!/usr/bin/env python3
"""Integrate the 11-stage commercial release lifecycle into existing AGENT CONTROL (no separate factory)."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from build_packages import DEFAULT_PACKAGES_DIR, DEMO_PRODUCT_ID
from build_promo_reels import DEFAULT_PROMO_DIR
from store_db import DEFAULT_DB_PATH, StoreDB

BATCH_02_MANIFEST_REL = Path('manifests/batch_02_manifest.json')
BATCH_02_PROMPTS_REL = Path('manifests/batch_02_prompts.md')

PIPELINE_STAGES = (
    'research',
    'specification',
    'generation',
    'processing',
    'packaging',
    'qa',
    'approved',
    'publication',
    'orders',
    'delivery',
    'analytics',
)

FREE_IMAGE_GENERATOR_BLOCKER = {
    'id': 'free_image_generator',
    'stage': 'generation',
    'status': 'blocked_manual_required',
    'budget_uah': 0,
    'paid_apis_allowed': False,
    'description': (
        'Автоматический бесплатный генератор изображений (0 грн) не подключён: '
        'генерация оригиналов выполняется вручную и загружается через приватный админ-приёмник, '
        'либо ожидает отдельного подключения локального/бесплатного провайдера.'
    ),
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def derive_item_stage(
    catalog_item: dict,
    manifest_item: dict | None,
    packages_dir: Path,
    promo_dir: Path,
) -> str:
    """Determine the current lifecycle stage for a catalog item without guessing."""
    pid = str(catalog_item.get('id') or '')
    status = str(catalog_item.get('status') or 'черновик')
    has_original = bool((manifest_item or {}).get('original_sha256'))
    has_preview = bool(catalog_item.get('image'))
    has_zip = (packages_dir / f'{pid}.zip').is_file()
    has_promo = (promo_dir / f'{pid}_reel.mp4').is_file()

    if status == 'готов к продаже' and has_zip:
        return 'publication' if has_promo else 'approved'
    if has_zip and status == 'проверка качества':
        return 'qa'
    if has_preview and has_original:
        return 'processing'
    if manifest_item and manifest_item.get('prompt'):
        return 'specification'
    return 'research'


def collect_batch_02_report(
    repo_root: Path,
    catalog_ids: set[str] | None = None,
    packages_dir: Path = Path(DEFAULT_PACKAGES_DIR),
    promo_dir: Path = Path(DEFAULT_PROMO_DIR),
) -> dict:
    """Build a readiness report for upcoming Batch 02 without mutating catalog.json."""
    manifest_path = repo_root / BATCH_02_MANIFEST_REL
    prompts_path = repo_root / BATCH_02_PROMPTS_REL
    stage_counts = {stage: 0 for stage in PIPELINE_STAGES}
    if not manifest_path.is_file():
        return {
            'batch': '02',
            'present': False,
            'manifest_path': str(BATCH_02_MANIFEST_REL),
            'prompts_path': str(BATCH_02_PROMPTS_REL),
            'prompts_doc_present': prompts_path.is_file(),
            'total_items': 0,
            'in_catalog_count': 0,
            'prompts_ready': 0,
            'originals_ready': 0,
            'previews_ready': 0,
            'packages_ready': 0,
            'promo_reels_ready': 0,
            'by_category': {},
            'stage_counts': stage_counts,
            'items': [],
        }

    if catalog_ids is None:
        cat_path = repo_root / 'catalog.json'
        if cat_path.is_file():
            cat_data = json.loads(cat_path.read_text(encoding='utf-8'))
            catalog_ids = {
                str(x.get('id')) for x in cat_data.get('items', []) if isinstance(x, dict) and x.get('id')
            }
        else:
            catalog_ids = set()

    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    items_report: list[dict] = []
    by_category: dict[str, int] = {}

    for m_item in manifest.get('items', []):
        if not isinstance(m_item, dict):
            continue
        pid = str(m_item.get('id') or '')
        if not pid:
            continue
        category = str(m_item.get('category') or '')
        preview_rel = str(m_item.get('preview_path') or '')
        preview_ready = bool(preview_rel) and (repo_root / preview_rel).is_file()
        original_ready = bool(m_item.get('original_sha256'))
        prompt_ready = bool(m_item.get('prompt'))
        package_ready = (packages_dir / f'{pid}.zip').is_file()
        promo_reel_ready = (promo_dir / f'{pid}_reel.mp4').is_file()
        in_catalog = pid in catalog_ids

        stage = derive_item_stage(
            {
                'id': pid,
                'status': m_item.get('status', 'черновик'),
                'image': preview_rel if preview_ready else '',
            },
            m_item,
            packages_dir,
            promo_dir,
        )
        stage_counts[stage] += 1
        by_category[category] = by_category.get(category, 0) + 1
        items_report.append(
            {
                'id': pid,
                'title': m_item.get('title', ''),
                'category': category,
                'status': m_item.get('status', 'prompt_ready'),
                'stage': stage,
                'prompt_ready': prompt_ready,
                'original_ready': original_ready,
                'preview_ready': preview_ready,
                'package_ready': package_ready,
                'promo_reel_ready': promo_reel_ready,
                'in_catalog': in_catalog,
                'proposed_price_uah': m_item.get('proposed_price_uah'),
                'proposed_price_xtr': m_item.get('proposed_price_xtr'),
            }
        )

    return {
        'batch': str(manifest.get('batch') or '02'),
        'season': str(manifest.get('season') or 'valentine_spring_overlays'),
        'title': str(manifest.get('title') or ''),
        'status': str(manifest.get('status') or 'draft_not_for_sale'),
        'present': True,
        'manifest_path': str(BATCH_02_MANIFEST_REL),
        'prompts_path': str(BATCH_02_PROMPTS_REL),
        'prompts_doc_present': prompts_path.is_file(),
        'total_items': len(items_report),
        'in_catalog_count': sum(1 for x in items_report if x['in_catalog']),
        'prompts_ready': sum(1 for x in items_report if x['prompt_ready']),
        'originals_ready': sum(1 for x in items_report if x['original_ready']),
        'previews_ready': sum(1 for x in items_report if x['preview_ready']),
        'packages_ready': sum(1 for x in items_report if x['package_ready']),
        'promo_reels_ready': sum(1 for x in items_report if x['promo_reel_ready']),
        'by_category': by_category,
        'stage_counts': stage_counts,
        'items': items_report,
    }


def collect_pipeline_snapshot(
    repo_root: Path,
    db_path: Path = Path(DEFAULT_DB_PATH),
    packages_dir: Path = Path(DEFAULT_PACKAGES_DIR),
    promo_dir: Path = Path(DEFAULT_PROMO_DIR),
) -> dict:
    """Build a structured 11-stage snapshot + analytics + #cloud summary + explicit free generator blocker."""
    catalog = json.loads((repo_root / 'catalog.json').read_text(encoding='utf-8'))
    manifest = json.loads((repo_root / 'generation_manifest.json').read_text(encoding='utf-8'))
    manifest_by_id = {
        str(x.get('id')): x for x in manifest.get('items', []) if isinstance(x, dict) and x.get('id')
    }

    items_state: list[dict] = []
    stage_counts = {stage: 0 for stage in PIPELINE_STAGES}
    catalog_ids: set[str] = set()
    for c_item in catalog.get('items', []):
        pid = str(c_item.get('id') or '')
        if not pid:
            continue
        catalog_ids.add(pid)
        stage = derive_item_stage(c_item, manifest_by_id.get(pid), packages_dir, promo_dir)
        stage_counts[stage] += 1
        items_state.append(
            {
                'id': pid,
                'category': c_item.get('category', ''),
                'status': c_item.get('status', 'черновик'),
                'stage': stage,
                'package_ready': (packages_dir / f'{pid}.zip').is_file(),
                'catalog_package_ready': bool(c_item.get('package_ready')),
                'promo_reel_ready': (promo_dir / f'{pid}_reel.mp4').is_file(),
            }
        )

    analytics = (
        StoreDB(db_path).get_metrics_summary()
        if db_path.is_file()
        else {
            'demo_deliveries_sent': 0,
            'demo_unique_leads': 0,
            'demo_repeat_requests': 0,
            'paid_orders': 0,
            'paid_revenue_xtr': 0,
            'paid_deliveries_sent': 0,
            'demo_to_paid_conversion_pct': 0.0,
            'open_support_tickets': 0,
        }
    )

    demo_offer = catalog.get('demo_offer') if isinstance(catalog.get('demo_offer'), dict) else {}
    demo_id = str(demo_offer.get('id') or DEMO_PRODUCT_ID)
    demo_package_ready = (packages_dir / f'{demo_id}.zip').is_file()
    demo_reel_ready = (promo_dir / 'demo_ny_reel.mp4').is_file()
    packages_ready_count = sum(1 for x in items_state if x['package_ready'])
    packages_pending_count = len(items_state) - packages_ready_count
    batch_02 = collect_batch_02_report(
        repo_root=repo_root,
        catalog_ids=catalog_ids,
        packages_dir=packages_dir,
        promo_dir=promo_dir,
    )

    return {
        'generated_at': utc_now_iso(),
        'stages': list(PIPELINE_STAGES),
        'stage_counts': stage_counts,
        'blockers': [FREE_IMAGE_GENERATOR_BLOCKER],
        'demo_package_ready': demo_package_ready,
        'demo_reel_ready': demo_reel_ready,
        'demo': {
            'id': demo_id,
            'deeplink': str(demo_offer.get('deeplink') or 'demo_ny'),
            'package_ready': demo_package_ready,
            'promo_reel_ready': demo_reel_ready,
            'deliveries_sent': analytics['demo_deliveries_sent'],
            'unique_leads': analytics['demo_unique_leads'],
        },
        'summary': {
            'total_products': len(items_state),
            'packages_ready': packages_ready_count,
            'packages_pending': packages_pending_count,
            'approved_for_sale': sum(
                1 for x in items_state if x['status'] == 'готов к продаже' and x['package_ready']
            ),
            'in_qa_or_build': sum(
                1 for x in items_state if x['status'] != 'готов к продаже' or not x['package_ready']
            ),
            'promo_reels_ready': sum(1 for x in items_state if x['promo_reel_ready']),
            'demo_ready': demo_package_ready,
        },
        'analytics': analytics,
        'items': items_state,
        'batch_02': batch_02,
    }


def sync_snapshot_to_agent_control(ac_db_path: Path, snapshot: dict) -> dict:
    """Persist pipeline snapshot and blocker state into existing AGENT CONTROL ac_meta/ac_logs tables."""
    if not ac_db_path.is_file():
        return {'ok': False, 'reason': f'AGENT CONTROL DB not found: {ac_db_path}'}

    now = utc_now_iso()
    summary = snapshot.get('summary') or {}
    batch_02 = snapshot.get('batch_02') or {}
    batch_02_log = (
        f"batch_02={batch_02.get('total_items', 0)} items "
        f"(prompts={batch_02.get('prompts_ready', 0)}, previews={batch_02.get('previews_ready', 0)}, "
        f"ready={batch_02.get('packages_ready', 0)}, in_catalog={batch_02.get('in_catalog_count', 0)})"
    )
    with sqlite3.connect(str(ac_db_path), timeout=15.0) as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS ac_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)'
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ac_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                ts_utc TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO ac_meta(k, v) VALUES ('naum_showcase_pipeline', ?)",
            (json.dumps(snapshot, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO ac_meta(k, v) VALUES ('naum_showcase_batch_02', ?)",
            (json.dumps(batch_02, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO ac_meta(k, v) VALUES ('naum_showcase_blocker_generation', ?)",
            (json.dumps(FREE_IMAGE_GENERATOR_BLOCKER, ensure_ascii=False),),
        )
        conn.execute(
            'INSERT INTO ac_logs(job_id, level, message, ts_utc) VALUES (?, ?, ?, ?)',
            (
                0,
                'INFO',
                (
                    f"Showcase pipeline synced ({summary.get('total_products', len(snapshot.get('items', [])))} items, "
                    f"ready={summary.get('packages_ready', 0)}, pending={summary.get('packages_pending', 0)}, "
                    f"demo_ready={summary.get('demo_ready', False)}); {batch_02_log}; "
                    f"blocker={FREE_IMAGE_GENERATOR_BLOCKER['status']}"
                ),
                now,
            ),
        )
    return {'ok': True, 'ac_db_path': str(ac_db_path), 'synced_at': now}


def main() -> None:
    ap = argparse.ArgumentParser(description='Sync NAUM Showcase lifecycle snapshot into AGENT CONTROL')
    ap.add_argument('--repo', default='/opt/ai-creative-showcase', help='Path to ai-creative-showcase repo')
    ap.add_argument('--db-path', default=DEFAULT_DB_PATH, help='Path to store.db')
    ap.add_argument('--packages-dir', default=DEFAULT_PACKAGES_DIR, help='Path to built ZIP packages')
    ap.add_argument('--promo-dir', default=DEFAULT_PROMO_DIR, help='Path to promo MP4 reels')
    ap.add_argument('--ac-db', default='', help='Optional path to existing AGENT CONTROL sqlite3 DB')
    args = ap.parse_args()

    snapshot = collect_pipeline_snapshot(
        repo_root=Path(args.repo).resolve(),
        db_path=Path(args.db_path),
        packages_dir=Path(args.packages_dir),
        promo_dir=Path(args.promo_dir),
    )
    result = {'snapshot': snapshot}
    if args.ac_db:
        result['agent_control_sync'] = sync_snapshot_to_agent_control(Path(args.ac_db), snapshot)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
