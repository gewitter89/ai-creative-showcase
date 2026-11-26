import json
import os
import shutil
import stat
import sys
from pathlib import Path
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ingest_asset import atomic_write_json, ingest_asset, sha256_file


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / 'repo'
    repo.mkdir()
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / 'catalog.json', repo / 'catalog.json')
    shutil.copy2(root / 'generation_manifest.json', repo / 'generation_manifest.json')
    (repo / 'assets').mkdir()
    return repo


def test_ingest_valid_image_creates_webp_0600_versioned_original(tmp_repo: Path, tmp_path: Path):
    src = tmp_path / 'kyiv.png'
    Image.new('RGB', (2400, 1600), (18, 42, 88)).save(src, format='PNG')
    priv = tmp_path / 'private_originals'

    res = ingest_asset(tmp_repo, 'ny-kyiv-01', src, private_dir=priv)

    assert res['ok'] is True
    assert res['catalog_status'] == 'проверка качества'
    assert res['preview_path'] == 'assets/newyear/ny-kyiv-01/preview.webp'
    assert (tmp_repo / res['preview_path']).is_file()

    expected_sha = sha256_file(src)
    orig_path = priv / 'newyear' / 'ny-kyiv-01' / f'ny-kyiv-01_original_{expected_sha[:12]}.png'
    assert Path(res['original_path']) == orig_path
    assert orig_path.is_file()
    assert stat.S_IMODE(orig_path.stat().st_mode) == 0o600
    assert res['original_sha256'] == expected_sha
    assert res['original_reused'] is False

    cat = json.loads((tmp_repo / 'catalog.json').read_text(encoding='utf-8'))
    item = next(x for x in cat['items'] if x['id'] == 'ny-kyiv-01')
    assert item['status'] == 'проверка качества'
    assert item['status'] != 'готов к продаже'
    assert item['image'] == 'assets/newyear/ny-kyiv-01/preview.webp'


def test_ingest_versions_originals_without_silent_overwrite(tmp_repo: Path, tmp_path: Path):
    priv = tmp_path / 'private_originals'
    legacy_dir = priv / 'newyear' / 'ny-kyiv-01'
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy_dir / 'ny-kyiv-01_original.png'
    legacy_file.write_bytes(b'legacy-original-bytes')

    src1 = tmp_path / 'v1.png'
    src2 = tmp_path / 'v2.png'
    Image.new('RGB', (1800, 1200), (10, 20, 30)).save(src1, format='PNG')
    Image.new('RGB', (1800, 1200), (200, 150, 40)).save(src2, format='PNG')

    r1 = ingest_asset(tmp_repo, 'ny-kyiv-01', src1, private_dir=priv)
    r2 = ingest_asset(tmp_repo, 'ny-kyiv-01', src2, private_dir=priv)
    r2_dup = ingest_asset(tmp_repo, 'ny-kyiv-01', src2, private_dir=priv)

    assert r1['original_path'] != r2['original_path']
    assert Path(r1['original_path']).is_file()
    assert Path(r2['original_path']).is_file()
    assert legacy_file.read_bytes() == b'legacy-original-bytes'
    assert r2_dup['original_reused'] is True
    assert stat.S_IMODE(Path(r1['original_path']).stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(r2['original_path']).stat().st_mode) == 0o600

    manifest = json.loads((tmp_repo / 'generation_manifest.json').read_text(encoding='utf-8'))
    m_item = next(x for x in manifest['items'] if x['id'] == 'ny-kyiv-01')
    shas = [v['sha256'] for v in m_item['original_versions']]
    assert r1['original_sha256'] in shas
    assert r2['original_sha256'] in shas
    assert len(shas) == 2


def test_atomic_write_json_preserves_target_on_replace_failure(tmp_path: Path, monkeypatch):
    target = tmp_path / 'catalog.json'
    target.write_text('{"ok": true}\n', encoding='utf-8')

    def boom(src, dst):
        raise OSError('simulated replace error')

    monkeypatch.setattr(os, 'replace', boom)
    with pytest.raises(OSError, match='simulated replace error'):
        atomic_write_json(target, {'ok': False})

    assert target.read_text(encoding='utf-8') == '{"ok": true}\n'
    assert list(tmp_path.glob('*.tmp')) == []


def test_ingest_rejects_small_oversized_and_unsupported_images(tmp_repo: Path, tmp_path: Path):
    tiny = tmp_path / 'tiny.png'
    Image.new('RGB', (200, 200), (0, 0, 0)).save(tiny, format='PNG')
    with pytest.raises(ValueError, match='below minimum'):
        ingest_asset(tmp_repo, 'ny-kyiv-01', tiny, private_dir=tmp_path / 'priv')

    gif_spoof = tmp_path / 'anim.png'
    Image.new('RGB', (600, 600), (1, 2, 3)).save(gif_spoof, format='GIF')
    with pytest.raises(ValueError, match='Unsupported image format'):
        ingest_asset(tmp_repo, 'ny-kyiv-01', gif_spoof, private_dir=tmp_path / 'priv')

    ok_img = tmp_path / 'ok.png'
    Image.new('RGB', (800, 800), (0, 0, 0)).save(ok_img, format='PNG')
    with pytest.raises(ValueError, match='exceeds limit'):
        ingest_asset(tmp_repo, 'ny-kyiv-01', ok_img, private_dir=tmp_path / 'priv', max_bytes=10)


def test_ingest_rejects_unknown_id_and_traversal(tmp_repo: Path, tmp_path: Path):
    src = tmp_path / 'ok.png'
    Image.new('RGB', (800, 800), (0, 0, 0)).save(src, format='PNG')
    with pytest.raises(ValueError):
        ingest_asset(tmp_repo, '../etc/passwd', src, private_dir=tmp_path / 'priv')
    with pytest.raises(ValueError, match='Unknown item_id'):
        ingest_asset(tmp_repo, 'nonexistent-99', src, private_dir=tmp_path / 'priv')
