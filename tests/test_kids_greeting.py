import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kids_greeting as kg
import studio_bot as bot
from store_db import KIDS_VIDEO_PRICE_UAH, KIDS_VIDEO_PRICE_XTR, KIDS_VIDEO_PRODUCT_ID, StoreDB


def _make_stub_tts(text: str, voice: str, out_path: Path) -> None:
    """Fast deterministic 1.2s sine MP3 for offline/deterministic ffmpeg audio mix tests."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=340:duration=1.2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(out_path),
        ],
        check=True,
        capture_output=True,
    )


def _probe_streams(media_path: Path) -> dict:
    res = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,sample_rate,channels:format=duration",
            "-of",
            "json",
            str(media_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(res.stdout)


def test_parse_greeting_args_uk_and_ru():
    assert kg.parse_greeting_args("   ", "/mykolay") is None
    assert kg.clean_child_name("Артемчик!!!") == "Артемчик"
    assert kg.clean_child_name("   ", lang="uk") == "Маленький друже"
    assert kg.clean_child_name("   ", lang="ru") == "Маленький друг"

    req_uk = kg.parse_greeting_args("Артемчик, 6, Київ, слухав маму і вчився", "/mykolay")
    assert req_uk is not None
    assert req_uk.child_name == "Артемчик"
    assert req_uk.age == 6
    assert req_uk.city == "Київ"
    assert req_uk.good_deed == "слухав маму і вчився"
    assert req_uk.lang == "uk"
    assert req_uk.persona == "mykolay"

    req_ru = kg.parse_greeting_args("Маша, 7, Одесса, помогала бабушке", "/santa")
    assert req_ru is not None
    assert req_ru.child_name == "Маша"
    assert req_ru.age == 7
    assert req_ru.city == "Одесса"
    assert req_ru.good_deed == "помогала бабушке"
    assert req_ru.lang == "ru"
    assert req_ru.persona == "ded_moroz"

    req_lazy = kg.parse_greeting_args("Іванко 5 Львів малював листівки", "/mykolay")
    assert req_lazy is not None
    assert req_lazy.child_name == "Іванко"
    assert req_lazy.age == 5
    assert req_lazy.city == "Львів"
    assert req_lazy.good_deed == "малював листівки"

    req_santa_explicit = kg.parse_greeting_args("ru: Денис, 8, Харьков, хорошо учился", "/santa")
    assert req_santa_explicit is not None
    assert req_santa_explicit.lang == "ru"
    assert req_santa_explicit.age == 8


def test_build_greeting_text_personas_and_market_pricing():
    assert kg.KIDS_AUDIO_PRICE_UAH == 0
    assert kg.KIDS_AUDIO_PRICE_XTR == 0
    assert 39 <= kg.KIDS_VIDEO_PRICE_UAH <= 49
    assert 45 <= kg.KIDS_VIDEO_PRICE_XTR <= 55

    uk_txt = kg.build_greeting_text("Софійка", age=5, city="Київ", good_deed="допомагала мамі", lang="uk", persona="mykolay")
    assert "Святий Миколай" in uk_txt
    assert "Софійка" in uk_txt
    assert "5 років" in uk_txt
    assert "Київ" in uk_txt

    ru_txt = kg.build_greeting_text("Миша", age=4, city="Одесса", good_deed="убрал игрушки", lang="ru", persona="ded_moroz")
    assert "Дедушка Мороз" in ru_txt
    assert "Миша" in ru_txt
    assert "4 года" in ru_txt
    assert "Одесса" in ru_txt

    santa_txt = kg.build_greeting_text("Олег", age=1, city="Львів", good_deed="був чемним", lang="uk", persona="santa")
    assert "Санта-Клаус" in santa_txt
    assert "1 рік" in santa_txt


def test_render_certificate_uk_and_ru(tmp_path: Path):
    out_uk = tmp_path / "cert_uk.jpg"
    kg.render_certificate(tmp_path, "Софійка", "Київ", out_uk, age=6, good_deed="читала казки", lang="uk", persona="mykolay")
    assert out_uk.is_file()
    with Image.open(out_uk) as im:
        assert im.size == (1200, 800)

    out_ru = tmp_path / "cert_ru.jpg"
    kg.render_certificate(tmp_path, "Маша", "Одесса", out_ru, age=7, good_deed="помогала бабушке", lang="ru", persona="ded_moroz")
    assert out_ru.is_file()
    with Image.open(out_ru) as im:
        assert im.size == (1200, 800)


def test_generate_audio_with_bells_and_9_16_video_with_book_frame(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    photo = tmp_path / "kid.jpg"
    Image.new("RGB", (400, 400), (200, 160, 120)).save(photo, format="JPEG")

    bundle = asyncio.run(
        kg.generate_kids_greeting_bundle(
            repo_root=repo_root,
            child_name="Іванко",
            age=6,
            city="Київ",
            good_deed="слухав маму",
            out_dir=tmp_path / "bundle",
            lang="uk",
            persona="mykolay",
            child_photo_path=photo,
            include_video=True,
            tts_fn=_make_stub_tts,
        )
    )
    audio_path = bundle["audio_path"]
    video_path = bundle["video_path"]
    assert bundle["certificate_path"].is_file()
    assert audio_path.is_file() and audio_path.stat().st_size > 2000
    assert video_path.is_file() and video_path.stat().st_size > 5000

    a_info = _probe_streams(audio_path)
    assert float(a_info["format"]["duration"]) > 2.0
    assert a_info["streams"][0]["channels"] == 2

    v_info = _probe_streams(video_path)
    v_streams = [s for s in v_info["streams"] if s["codec_type"] == "video"]
    a_streams = [s for s in v_info["streams"] if s["codec_type"] == "audio"]
    assert len(v_streams) == 1
    assert v_streams[0]["width"] == 1080 and v_streams[0]["height"] == 1920
    assert len(a_streams) == 1


def _bot_repo(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy2(root / "catalog.json", repo / "catalog.json")
    shutil.copy2(root / "generation_manifest.json", repo / "generation_manifest.json")
    shutil.copytree(root / "assets", repo / "assets")
    return repo


def test_bot_mykolay_and_santa_free_lead_and_stars_video_flow(tmp_path: Path, monkeypatch):
    repo = _bot_repo(tmp_path)
    db_path = tmp_path / "store.db"
    packages_dir = tmp_path / "packages"
    packages_dir.mkdir()

    async def fake_edge_tts(text: str, voice: str, out_mp3: Path) -> None:
        _make_stub_tts(text, voice, out_mp3)

    monkeypatch.setattr(kg, "_synthesize_edge_tts", fake_edge_tts)

    cfg_paused = bot.BotConfig(
        token="12345:TEST_KIDS_TOKEN",
        owner_ids=frozenset({"111222"}),
        repo=repo,
        private_dir=tmp_path / "priv",
        auto_push=False,
        db_path=db_path,
        packages_dir=packages_dir,
        payments_enabled=False,
    )
    cfg_paid = bot.BotConfig(
        token="12345:TEST_KIDS_TOKEN",
        owner_ids=frozenset({"111222"}),
        repo=repo,
        private_dir=tmp_path / "priv",
        auto_push=False,
        db_path=db_path,
        packages_dir=packages_dir,
        payments_enabled=True,
    )

    api_calls = []
    photos = []
    audios = []
    videos = []

    def fake_api(token, method, payload=None):
        api_calls.append((method, payload))
        return {"ok": True}

    def fake_photo(token, chat_id, file_path, caption, reply_markup=None):
        photos.append((chat_id, file_path.is_file(), caption, reply_markup))
        return {"ok": True, "result": {"photo": [{"file_id": "photo_1"}]}}

    def fake_audio(token, chat_id, file_path, caption="", title="", performer=""):
        audios.append((chat_id, file_path.is_file(), caption, title, performer))
        return {"ok": True, "result": {"audio": {"file_id": "audio_1"}}}

    def fake_video(token, chat_id, file_path, caption=""):
        videos.append((chat_id, file_path.is_file(), caption))
        return {"ok": True, "result": {"video": {"file_id": "vid_kids_1"}}}

    # 1) Empty /mykolay returns help
    bot.process_update(
        {"update_id": 1, "message": {"from": {"id": 555}, "chat": {"id": 555, "type": "private"}, "text": "/mykolay"}},
        cfg_paused,
        api_fn=fake_api,
        send_photo_fn=fake_photo,
        send_audio_fn=fake_audio,
    )
    assert api_calls[-1][0] == "sendMessage"
    assert "39 грн" in api_calls[-1][1]["text"]

    # 2) /mykolay with full args delivers FREE certificate + FREE bells audio
    bot.process_update(
        {
            "update_id": 2,
            "message": {
                "from": {"id": 555, "username": "mama_kyiv"},
                "chat": {"id": 555, "type": "private"},
                "text": "/mykolay Артемчик, 6, Київ, слухав маму",
            },
        },
        cfg_paused,
        api_fn=fake_api,
        send_photo_fn=fake_photo,
        send_audio_fn=fake_audio,
    )
    assert len(photos) == 1 and photos[0][1] is True
    assert len(audios) == 1 and audios[0][1] is True
    assert "Артемчик" in photos[0][2]
    assert "Артемчик" in audios[0][3]
    cb_data = photos[0][3]["inline_keyboard"][0][0]["callback_data"]
    assert cb_data.startswith("kids_video:kids_")
    job_id = cb_data.split(":", 1)[1]

    # 3) /santa in Russian works cleanly
    bot.process_update(
        {
            "update_id": 3,
            "message": {
                "from": {"id": 556},
                "chat": {"id": 556, "type": "private"},
                "text": "/santa Маша, 7, Одесса, помогала бабушке",
            },
        },
        cfg_paused,
        api_fn=fake_api,
        send_photo_fn=fake_photo,
        send_audio_fn=fake_audio,
    )
    assert len(photos) == 2 and len(audios) == 2
    assert "Грамота вежливого ребёнка" in photos[1][2]

    # 4) Wrong user clicking someone else's kids_video button is rejected
    bot.process_update(
        {
            "update_id": 4,
            "callback_query": {
                "id": "cb_other",
                "from": {"id": 999},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": cb_data,
            },
        },
        cfg_paid,
        api_fn=fake_api,
    )
    assert api_calls[-1][0] == "answerCallbackQuery"
    assert api_calls[-1][1]["show_alert"] is True

    # 5) Owner-paused paywall (payments_enabled=False) does not send invoice
    bot.process_update(
        {
            "update_id": 5,
            "callback_query": {
                "id": "cb_paused",
                "from": {"id": 555, "username": "mama_kyiv"},
                "message": {"chat": {"id": 555, "type": "private"}},
                "data": cb_data,
            },
        },
        cfg_paused,
        api_fn=fake_api,
    )
    assert not any(m == "sendInvoice" for m, _ in api_calls)
    assert f"{KIDS_VIDEO_PRICE_UAH} грн" in api_calls[-1][1]["text"]

    # 6) Payments enabled -> sendInvoice (45 Stars XTR) -> pre_checkout -> successful_payment -> 9:16 video delivered
    bot.process_update(
        {
            "update_id": 6,
            "callback_query": {
                "id": "cb_buy",
                "from": {"id": 555, "username": "mama_kyiv"},
                "message": {"chat": {"id": 555, "type": "private"}},
                "data": cb_data,
            },
        },
        cfg_paid,
        api_fn=fake_api,
    )
    inv = [p for m, p in api_calls if m == "sendInvoice"][-1]
    assert inv["currency"] == "XTR"
    assert inv["prices"][0]["amount"] == KIDS_VIDEO_PRICE_XTR
    payload = inv["payload"]

    bot.process_update(
        {
            "update_id": 7,
            "pre_checkout_query": {
                "id": "pcq_kids_1",
                "from": {"id": 555},
                "currency": "XTR",
                "total_amount": KIDS_VIDEO_PRICE_XTR,
                "invoice_payload": payload,
            },
        },
        cfg_paid,
        api_fn=fake_api,
    )
    pcq_ans = [p for m, p in api_calls if m == "answerPreCheckoutQuery"][-1]
    assert pcq_ans["ok"] is True

    sp_upd = {
        "update_id": 8,
        "message": {
            "from": {"id": 555},
            "chat": {"id": 555, "type": "private"},
            "successful_payment": {
                "currency": "XTR",
                "total_amount": KIDS_VIDEO_PRICE_XTR,
                "invoice_payload": payload,
                "telegram_payment_charge_id": "tg_kids_charge_1",
                "provider_payment_charge_id": "",
            },
        },
    }
    bot.process_update(
        sp_upd,
        cfg_paid,
        api_fn=fake_api,
        send_video_fn=fake_video,
    )
    assert len(videos) == 1 and videos[0][1] is True
    assert "Артемчик" in videos[0][2]

    # Duplicate payment charge is idempotent (no double render)
    bot.process_update(
        {**sp_upd, "update_id": 9},
        cfg_paid,
        api_fn=fake_api,
        send_video_fn=fake_video,
    )
    assert len(videos) == 1

    store = StoreDB(db_path)
    job = store.get_kids_job(job_id)
    assert job is not None and job["status"] == "delivered"
    assert job["telegram_file_id"] == "vid_kids_1"
