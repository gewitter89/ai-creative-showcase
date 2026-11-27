import asyncio
from pathlib import Path
from PIL import Image
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import kids_greeting as kg


def test_clean_child_name():
    assert kg.clean_child_name("Артемчик!!!") == "Артемчик"
    assert kg.clean_child_name("   ") == "Маленький друже"


def test_render_certificate(tmp_path):
    out = tmp_path / "cert.jpg"
    kg.render_certificate(tmp_path, "Софійка", "Київ", out)
    assert out.is_file()
    with Image.open(out) as im:
        assert im.size == (1200, 800)


def test_generate_audio_and_video(tmp_path):
    mp3 = tmp_path / "speech.mp3"
    asyncio.run(kg.generate_nicholas_audio("Іванко", "слухав маму", mp3, lang="uk"))
    assert mp3.is_file() and mp3.stat().st_size > 1000

    out_video = tmp_path / "greeting.mp4"
    kg.render_video_greeting(tmp_path, "Іванко", mp3, out_video)
    assert out_video.is_file() and out_video.stat().st_size > 5000
