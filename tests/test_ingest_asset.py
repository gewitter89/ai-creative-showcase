import json
import shutil
import sys
from pathlib import Path
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ingest_asset import ingest_asset


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / 'repo'
    repo.mkdir()
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / 'catalog.json', repo / 'catalog.json')
    shutil.copy2(root / 'generation_manifest.json', repo / 'generation_manifest.json')
    (repo / 'assets').mkdir()
    return repo


def test_ingest_valid_image_creates_webp_and_qa_status(tmp_repo: Path, tmp_path: Path):
    src = tmp_path / 'kyiv.png'
    Image.new('RGB', (2400, 1600), (18, 42, 88)).save(src, format='PNG')
    priv = tmp_path / 'private_originals'

    res = ingest_asset(tmp_repo, 'ny-kyiv-01', src, private_dir=priv)

    assert res['ok'] is True
    assert res['catalog_status'] == 'проверка качества'
    assert res['preview_path'] == 'assets/newyear/ny-kyiv-01/preview.webp'
    assert (tmp_repo / res['preview_path']).is_file()
    assert (priv / 'newyear' / 'ny-kyiv-01' / 'ny-kyiv-01_original.png').is_file()

    cat = json.loads((tmp_repo / 'catalog.json').read_text(encoding='utf-8'))
    item = next(x for x in cat['items'] if x['id'] == 'ny-kyiv-01')
    assert item['status'] == 'проверка качества'
    assert item['status'] != 'готов к продаже'
    assert item['image'] == 'assets/newyear/ny-kyiv-01/preview.webp'


def test_ingest_rejects_small_image(tmp_repo: Path, tmp_path: Path):
    src = tmp_path / 'tiny.png'
    Image.new('RGB', (200, 200), (0, 0, 0)).save(src, format='PNG')
    with pytest.raises(ValueError, match='below minimum'):
        ingest_asset(tmp_repo, 'ny-kyiv-01', src, private_dir=tmp_path / 'priv')


def test_ingest_rejects_unknown_id_and_traversal(tmp_repo: Path, tmp_path: Path):
    src = tmp_path / 'ok.png'
    Image.new('RGB', (800, 800), (0, 0, 0)).save(src, format='PNG')
    with pytest.raises(ValueError):
        ingest_asset(tmp_repo, '../etc/passwd', src, private_dir=tmp_path / 'priv')
    with pytest.raises(ValueError, match='Unknown item_id'):
        ingest_asset(tmp_repo, 'nonexistent-99', src, private_dir=tmp_path / 'priv')
