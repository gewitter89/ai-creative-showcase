import sys
from pathlib import Path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import studio_bot as bot
from viral_card import generate_personalized_card


def test_generate_personalized_card(tmp_path):
    preview = tmp_path / "assets" / "newyear" / "ny-kyiv-01" / "preview.webp"
    preview.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1200, 800), (15, 30, 60)).save(preview, format="WEBP")
    out = tmp_path / "card.jpg"
    res = generate_personalized_card(tmp_path, "Марина", out)
    assert res.is_file()
    with Image.open(res) as im:
        assert im.size == (1200, 800)


def test_bot_card_command_sends_photo(tmp_path):
    preview = tmp_path / "assets" / "newyear" / "ny-kyiv-01" / "preview.webp"
    preview.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1200, 800), (15, 30, 60)).save(preview, format="WEBP")

    photos = []
    def fake_photo(token, chat_id, file_path, caption, reply_markup=None):
        photos.append((chat_id, file_path.exists(), caption))
        return {"ok": True}

    cfg = bot.BotConfig(
        token="123:TEST",
        owner_ids={"7636868692"},
        repo=tmp_path,
        private_dir=tmp_path / "priv",
        auto_push=False,
        push_branch="feat/production-batch-01",
        db_path=tmp_path / "store.db",
        packages_dir=tmp_path / "pkg",
        payments_enabled=False,
    )
    upd = {
        "update_id": 10,
        "message": {"from": {"id": 555}, "chat": {"id": 555, "type": "private"}, "text": "/card Олена"},
    }
    bot.process_update(upd, cfg, api_fn=lambda *a, **k: {"ok": True}, send_photo_fn=fake_photo)
    assert len(photos) == 1
    assert photos[0][1] is True
    assert "Олена" in photos[0][2]
