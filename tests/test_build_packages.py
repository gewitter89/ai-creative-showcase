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
    DEMO_PRODUCT_ID,
    P0_POSTCARD_IDS,
    PRINT_SIZE_PX,
    SOCIAL_SIZE_PX,
    build_all_p0_postcards,
    build_demo_postcard_package,
    build_postcard_package,
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
        'ny-kyiv-01': ('ny-kyiv-01_original.jpg', 'JPEG', (1024, 682), (18, 42, 88)),
        'ny-cozy-02': ('ny-cozy-02_original.jpg', 'JPEG', (1024, 682), (62, 38, 24)),
        'ny-minimal-03': ('ny-minimal-03_original.png', 'PNG', (1536, 1024), (16, 48, 32)),
    }
    for item_id, (fname, fmt, size, color) in specs.items():
        d = originals / 'newyear' / item_id
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

    # Reject unknown/non-P0 IDs
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
