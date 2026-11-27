import io
import json
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from build_packages import (
    ALL_PRODUCT_IDS,
    CONCEPT_3D_HIGH_RES_PX,
    CONCEPT_3D_IDS,
    CONCEPT_3D_PREVIEW_PX,
    DEMO_PRODUCT_ID,
    P0_POSTCARD_IDS,
    PHOTOSHOP_BACKGROUND_IDS,
    PHOTOSHOP_OVERLAY_IDS,
    PRINT_SIZE_PX,
    PS_BG_HIGH_RES_PX,
    PS_BG_WEB_PX,
    PS_OVERLAY_HIGH_RES_PX,
    SOCIAL_SIZE_PX,
    build_all_p0_postcards,
    build_all_packages,
    build_demo_postcard_package,
    build_postcard_package,
    build_product_package,
)
from ingest_asset import sha256_file


@pytest.fixture()
def populated_studio(tmp_path: Path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / 'catalog.json', repo / 'catalog.json')
    manifest = json.loads((root / 'generation_manifest.json').read_text(encoding='utf-8'))

    originals = tmp_path / 'originals'
    packages = tmp_path / 'packages'

    specs = {
        'ny-kyiv-01': ('newyear', 'ny-kyiv-01_original.jpg', 'JPEG', (1024, 682), (18, 42, 88)),
        'ny-cozy-02': ('newyear', 'ny-cozy-02_original.jpg', 'JPEG', (1024, 682), (62, 38, 24)),
        'ny-minimal-03': ('newyear', 'ny-minimal-03_original.png', 'PNG', (1536, 1024), (16, 48, 32)),
        'ps-pack-01': ('photoshop', 'ps-pack-01_original.png', 'PNG', (1536, 1024), (0, 0, 0)),
        'ps-snow-02': ('photoshop', 'ps-snow-02_original.png', 'PNG', (1536, 1024), (0, 0, 0)),
        'ps-bg-03': ('photoshop', 'ps-bg-03_original.png', 'PNG', (1122, 1402), (12, 24, 48)),
        '3d-ref-01': ('3d', '3d-ref-01_original.png', 'PNG', (1254, 1254), (160, 155, 150)),
        '3d-tree-02': ('3d', '3d-tree-02_original.png', 'PNG', (1254, 1254), (150, 150, 145)),
        '3d-owl-03': ('3d', '3d-owl-03_original.png', 'PNG', (1254, 1254), (155, 155, 155)),
    }
    for item_id, (category, fname, fmt, size, color) in specs.items():
        d = originals / category / item_id
        d.mkdir(parents=True, exist_ok=True)
        fpath = d / fname
        Image.new('RGB', size, color).save(fpath, format=fmt)
        m_item = next(x for x in manifest['items'] if x['id'] == item_id)
        m_item['original_sha256'] = sha256_file(fpath)

    (repo / 'generation_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return repo, originals, packages


def test_build_all_p0_postcards_creates_valid_zips(populated_studio):
    repo, originals, packages = populated_studio
    results = build_all_p0_postcards(repo, originals_dir=originals, output_dir=packages)

    assert [r['item_id'] for r in results] == list(P0_POSTCARD_IDS)

    expected_members = {
        'artwork_original.png',
        'greeting_ua.png',
        'print_150x100mm.jpg',
        'social_media.jpg',
        'README.txt',
        'LICENSE.txt',
    }

    for res in results:
        zip_path = Path(res['package_path'])
        assert zip_path.is_file()
        assert zip_path.name == f"{res['item_id']}.zip"
        assert stat.S_IMODE(zip_path.stat().st_mode) == 0o640

        with zipfile.ZipFile(zip_path, 'r') as zf:
            names = set(zf.namelist())
            assert names == expected_members

            with Image.open(io.BytesIO(zf.read('print_150x100mm.jpg'))) as p_img:
                assert p_img.size == PRINT_SIZE_PX
                assert p_img.info.get('dpi') == (300, 300)

            with Image.open(io.BytesIO(zf.read('social_media.jpg'))) as s_img:
                assert s_img.size == SOCIAL_SIZE_PX
                assert 'exif' not in s_img.info

            with Image.open(io.BytesIO(zf.read('greeting_ua.png'))) as g_img:
                assert g_img.size == PRINT_SIZE_PX
                assert g_img.format == 'PNG'

            with Image.open(io.BytesIO(zf.read('artwork_original.png'))) as a_img:
                assert a_img.format == 'PNG'
                w, h = map(int, res['original_size'].split('x'))
                assert a_img.size == (w, h)

            readme = zf.read('README.txt').decode('utf-8')
            license_txt = zf.read('LICENSE.txt').decode('utf-8')
            assert res['item_id'] in readme
            assert '150x100 mm' in readme
            assert '300 dpi' in readme
            assert 'NAUM AI Creative Studio' in license_txt


def test_build_all_9_packages_photoshop_and_3d_concepts(populated_studio):
    repo, originals, packages = populated_studio
    results = build_all_packages(repo, originals_dir=originals, output_dir=packages)
    assert [r['item_id'] for r in results] == list(ALL_PRODUCT_IDS)

    by_id = {r['item_id']: r for r in results}

    for item_id in PHOTOSHOP_OVERLAY_IDS:
        zip_path = Path(by_id[item_id]['package_path'])
        assert stat.S_IMODE(zip_path.stat().st_mode) == 0o640
        with zipfile.ZipFile(zip_path, 'r') as zf:
            assert set(zf.namelist()) == {
                'overlay_original.png',
                'overlay_3000x2000.png',
                'overlay_preview.jpg',
                'README.txt',
                'LICENSE.txt',
            }
            with Image.open(io.BytesIO(zf.read('overlay_original.png'))) as orig_im:
                assert orig_im.size == (1536, 1024)
                assert orig_im.format == 'PNG'
            with Image.open(io.BytesIO(zf.read('overlay_3000x2000.png'))) as hi_im:
                assert hi_im.size == PS_OVERLAY_HIGH_RES_PX
                assert hi_im.format == 'PNG'
            with Image.open(io.BytesIO(zf.read('overlay_preview.jpg'))) as prev_im:
                assert prev_im.size == SOCIAL_SIZE_PX
                assert 'exif' not in prev_im.info
            readme = zf.read('README.txt').decode('utf-8')
            license_txt = zf.read('LICENSE.txt').decode('utf-8')
            assert 'Screen' in readme
            assert 'RGB(0,0,0)' in readme
            assert 'NOT a layered .PSD' in readme
            assert 'NAUM AI Creative Studio' in license_txt

    for item_id in PHOTOSHOP_BACKGROUND_IDS:
        zip_path = Path(by_id[item_id]['package_path'])
        assert stat.S_IMODE(zip_path.stat().st_mode) == 0o640
        with zipfile.ZipFile(zip_path, 'r') as zf:
            assert set(zf.namelist()) == {
                'background_original.png',
                'background_2400x3000.png',
                'background_print_4x5.jpg',
                'background_web_960x1200.jpg',
                'README.txt',
                'LICENSE.txt',
            }
            with Image.open(io.BytesIO(zf.read('background_original.png'))) as orig_im:
                assert orig_im.size == (1122, 1402)
            with Image.open(io.BytesIO(zf.read('background_2400x3000.png'))) as hi_im:
                assert hi_im.size == PS_BG_HIGH_RES_PX
            with Image.open(io.BytesIO(zf.read('background_print_4x5.jpg'))) as pr_im:
                assert pr_im.size == PS_BG_HIGH_RES_PX
                assert pr_im.info.get('dpi') == (300, 300)
                assert 'exif' not in pr_im.info
            with Image.open(io.BytesIO(zf.read('background_web_960x1200.jpg'))) as web_im:
                assert web_im.size == PS_BG_WEB_PX
            readme = zf.read('README.txt').decode('utf-8')
            assert 'Screen' in readme
            assert 'NOT an editable multi-layer .PSD' in readme

    for item_id in CONCEPT_3D_IDS:
        zip_path = Path(by_id[item_id]['package_path'])
        assert stat.S_IMODE(zip_path.stat().st_mode) == 0o640
        with zipfile.ZipFile(zip_path, 'r') as zf:
            assert set(zf.namelist()) == {
                'concept_original.png',
                'reference_2048x2048.png',
                'reference_1200x1200.jpg',
                'README.txt',
                'LICENSE.txt',
            }
            with Image.open(io.BytesIO(zf.read('concept_original.png'))) as orig_im:
                assert orig_im.size == (1254, 1254)
                assert orig_im.format == 'PNG'
            with Image.open(io.BytesIO(zf.read('reference_2048x2048.png'))) as hi_im:
                assert hi_im.size == CONCEPT_3D_HIGH_RES_PX
            with Image.open(io.BytesIO(zf.read('reference_1200x1200.jpg'))) as prev_im:
                assert prev_im.size == CONCEPT_3D_PREVIEW_PX
                assert 'exif' not in prev_im.info
            readme = zf.read('README.txt').decode('utf-8')
            license_txt = zf.read('LICENSE.txt').decode('utf-8')
            assert 'референс-концепт для 3D-моделирования' in readme
            assert 'НЕ ГОТОВЫЙ STL' in readme
            assert 'NOT STL / 3MF / OBJ' in readme
            assert 'sculpt or model' in license_txt


def test_optional_extra_variants_included_and_unknown_product_rejected(populated_studio):
    repo, originals, packages = populated_studio

    # Optional extra view for 3D concept and extra variant for Photoshop overlay
    extra_view = originals / '3d' / '3d-ref-01' / '3d-ref-01_view_left.png'
    Image.new('RGB', (1254, 1254), (120, 120, 120)).save(extra_view, format='PNG')
    res_3d = build_product_package(repo, '3d-ref-01', originals_dir=originals, output_dir=packages)
    assert 'reference_view_02.png' in res_3d['files']
    with zipfile.ZipFile(res_3d['package_path'], 'r') as zf:
        assert 'reference_view_02.png' in zf.namelist()
        assert 'reference_view_02.png' in zf.read('README.txt').decode('utf-8')

    extra_ov = originals / 'photoshop' / 'ps-pack-01' / 'ps-pack-01_variant_02.png'
    Image.new('RGB', (1536, 1024), (10, 20, 30)).save(extra_ov, format='PNG')
    res_ps = build_product_package(repo, 'ps-pack-01', originals_dir=originals, output_dir=packages)
    assert 'overlay_variant_02.png' in res_ps['files']

    with pytest.raises(ValueError, match='Unsupported product'):
        build_product_package(repo, 'unknown-99', originals_dir=originals, output_dir=packages)


def test_build_package_verifies_sha256_and_supports_versioned_originals(populated_studio):
    repo, originals, packages = populated_studio

    # Supports SHA256-versioned original filename when manifest specifies original_filename
    manifest = json.loads((repo / 'generation_manifest.json').read_text(encoding='utf-8'))
    kyiv_dir = originals / 'newyear' / 'ny-kyiv-01'
    v_img = kyiv_dir / 'ny-kyiv-01_original_deadbeef1234.png'
    Image.new('RGB', (1800, 1200), (20, 40, 90)).save(v_img, format='PNG')
    v_sha = sha256_file(v_img)
    v_versioned = kyiv_dir / f'ny-kyiv-01_original_{v_sha[:12]}.png'
    v_img.rename(v_versioned)
    m_item = next(x for x in manifest['items'] if x['id'] == 'ny-kyiv-01')
    m_item['original_sha256'] = v_sha
    m_item['original_filename'] = v_versioned.name
    (repo / 'generation_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    res_v = build_postcard_package(repo, 'ny-kyiv-01', originals_dir=originals, output_dir=packages)
    assert Path(res_v['original_path']) == v_versioned
    assert res_v['original_size'] == '1800x1200'

    # Corrupt original to ensure hash check fails-closed
    Image.new('RGB', (1024, 682), (255, 0, 0)).save(v_versioned, format='PNG')
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        build_postcard_package(repo, 'ny-kyiv-01', originals_dir=originals, output_dir=packages)

    # Reject unknown/non-P0 IDs in P0 postcard builder
    with pytest.raises(ValueError, match='Unsupported postcard'):
        build_postcard_package(repo, 'ps-pack-01', originals_dir=originals, output_dir=packages)


def test_build_demo_postcard_package_uses_public_preview(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = tmp_path / 'packages'
    res = build_demo_postcard_package(repo_root, output_dir=out_dir)

    assert res['ok'] is True
    assert res['is_demo'] is True
    assert res['item_id'] == DEMO_PRODUCT_ID
    zip_path = Path(res['package_path'])
    assert zip_path.is_file()
    assert stat.S_IMODE(zip_path.stat().st_mode) == 0o640

    with zipfile.ZipFile(zip_path, 'r') as zf:
        assert set(zf.namelist()) == {
            'demo_greeting_ua.png',
            'demo_ny_kyiv.jpg',
            'README.txt',
            'LICENSE.txt',
        }
        with Image.open(io.BytesIO(zf.read('demo_greeting_ua.png'))) as g_img:
            assert g_img.size == SOCIAL_SIZE_PX
            assert g_img.format == 'PNG'
        with Image.open(io.BytesIO(zf.read('demo_ny_kyiv.jpg'))) as j_img:
            assert j_img.size == SOCIAL_SIZE_PX
            assert j_img.format == 'JPEG'
        readme = zf.read('README.txt').decode('utf-8')
        assert 'NaumCreativeStudioBot' in readme
        assert 'buy_ny-kyiv-01' in readme
