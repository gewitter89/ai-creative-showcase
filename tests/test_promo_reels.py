import json
import subprocess
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_promo_reels


def test_pure_python_qr_matrix_structure_and_render():
    grid = build_promo_reels.generate_qr_matrix(build_promo_reels.DEMO_URL)
    n = len(grid)
    assert n in (29, 33, 37, 41)

    # Verify all three 7x7 finder patterns
    for top, left in ((0, 0), (0, n - 7), (n - 7, 0)):
        for r in range(7):
            for c in range(7):
                expected = 1 if (r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4)) else 0
                assert grid[top + r][left + c] == expected

    # Timing patterns (row 6 and col 6 between finders)
    for i in range(8, n - 8):
        assert grid[6][i] == (1 if i % 2 == 0 else 0)
        assert grid[i][6] == (1 if i % 2 == 0 else 0)

    # Dark module at (n - 8, 8)
    assert grid[n - 8][8] == 1

    img = build_promo_reels.render_qr_image(build_promo_reels.DEMO_URL, target_px=216)
    assert img.size == (216, 216)
    assert img.getpixel((0, 0)) == (255, 255, 255)


def test_build_promo_reels_creates_vertical_1080x1920_mp4(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = tmp_path / 'promo'

    results = build_promo_reels.build_all_promo_reels(
        repo_root=repo_root,
        output_dir=out_dir,
        item_ids=['ny-kyiv-01'],
        duration_sec=1.5,
        include_demo=True,
    )
    assert len(results) == 2
    by_id = {r['item_id']: r for r in results}
    assert set(by_id) == {'ny-kyiv-01', 'demo-ny'}

    for key, res in by_id.items():
        mp4_path = Path(res['output_path'])
        assert mp4_path.is_file()
        assert mp4_path.stat().st_size > 10_000
        assert res['resolution'] == '1080x1920'

        probe = subprocess.run(
            [
                'ffprobe',
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name,width,height,pix_fmt',
                '-of', 'json',
                str(mp4_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        stream = json.loads(probe.stdout)['streams'][0]
        assert stream['codec_name'] == 'h264'
        assert stream['width'] == 1080
        assert stream['height'] == 1920
        assert stream['pix_fmt'] == 'yuv420p'


def test_build_promo_reel_rejects_invalid_id_or_missing_preview(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError, match='Invalid item_id'):
        build_promo_reels.build_promo_reel(
            repo_root,
            {'id': '../bad', 'category': 'newyear'},
            tmp_path / 'bad.mp4',
        )
    with pytest.raises(FileNotFoundError):
        build_promo_reels.build_promo_reel(
            repo_root,
            {'id': 'ny-missing-99', 'category': 'newyear', 'image': 'assets/newyear/ny-missing-99/preview.webp'},
            tmp_path / 'missing.mp4',
        )
