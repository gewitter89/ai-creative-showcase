import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio_bot
import video_clipper
from store_db import CLIP_FREE_TRIAL_LIMIT, CLIP_PRICE_XTR, CLIP_PRODUCT_ID, StoreDB


def _make_sample_video(path: Path) -> None:
    """Create an 8s 1280x720 MP4: 0-3s quiet, 3-7s loud modulated tone, 7-8s quiet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        'ffmpeg',
        '-y',
        '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=25:duration=8',
        '-f', 'lavfi', '-i',
        "aevalsrc='if(between(t,3.0,7.0),0.65*sin(2*PI*440*t)*sin(2*PI*3*t),0.002*sin(2*PI*100*t))':s=16000:d=8",
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '96k',
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


@pytest.fixture()
def bot_env(tmp_path: Path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / 'catalog.json', repo / 'catalog.json')
    shutil.copy2(root / 'generation_manifest.json', repo / 'generation_manifest.json')
    shutil.copytree(root / 'assets', repo / 'assets')

    db_path = tmp_path / 'store.db'
    packages_dir = tmp_path / 'packages'
    packages_dir.mkdir()

    cfg_paused = studio_bot.BotConfig(
        token='12345:SECRET_CLIP_TOKEN',
        owner_ids=frozenset({'111222'}),
        repo=repo,
        private_dir=tmp_path / 'private',
        auto_push=False,
        db_path=db_path,
        packages_dir=packages_dir,
        payments_enabled=False,
    )
    cfg_paid = studio_bot.BotConfig(
        token='12345:SECRET_CLIP_TOKEN',
        owner_ids=frozenset({'111222'}),
        repo=repo,
        private_dir=tmp_path / 'private',
        auto_push=False,
        db_path=db_path,
        packages_dir=packages_dir,
        payments_enabled=True,
    )
    return cfg_paused, cfg_paid, db_path


def test_url_validation_and_vtt_srt_karaoke_parsing(tmp_path: Path):
    assert video_clipper.is_http_url('https://www.youtube.com/watch?v=dQw4w9WgXcQ') is True
    assert video_clipper.is_http_url('http://example.com/video.mp4') is True
    assert video_clipper.is_http_url('file:///etc/passwd') is False
    assert video_clipper.is_http_url('https://bad.com/v;rm -rf /') is False
    assert video_clipper.is_http_url('http://127.0.0.1:8091/secret') is False
    assert video_clipper.is_http_url('http://localhost/admin') is False
    assert video_clipper.is_http_url('http://169.254.169.254/latest/meta-data') is False

    vtt = tmp_path / 'subs.vtt'
    vtt.write_text(
        'WEBVTT\n\n'
        '00:00:01.000 --> 00:00:03.200 align:start position:0%\n'
        'Привет &amp; <00:00:01.500><c> мир</c><00:00:02.100><c> PeakCut</c>\n\n'
        '00:00:03.500 --> 00:00:05.000\n'
        'Вертикальный шортс готов!\n',
        encoding='utf-8',
    )
    segs = video_clipper.parse_subtitle_file(vtt)
    assert len(segs) == 2
    assert [w.word for w in segs[0].words] == ['Привет', '&', 'мир', 'PeakCut']

    ass_path = tmp_path / 'karaoke.ass'
    line_count = video_clipper.build_karaoke_ass(segs, clip_start=0.5, clip_end=5.5, ass_path=ass_path)
    assert line_count >= 2
    ass_txt = ass_path.read_text(encoding='utf-8')
    assert 'PlayResX: 1080' in ass_txt
    assert 'PlayResY: 1920' in ass_txt
    assert '{\\kf' in ass_txt


def test_clip_video_peakcut_selects_loud_segment_and_renders_9_16_karaoke(tmp_path: Path):
    src_mp4 = tmp_path / 'sample_landscape.mp4'
    _make_sample_video(src_mp4)
    out_dir = tmp_path / 'shorts'

    res = video_clipper.clip_video(
        source=src_mp4,
        output_dir=out_dir,
        clip_duration=3.5,
        max_clips=1,
        transcript_segments=[
            {'start': 3.2, 'end': 4.8, 'text': 'Динамичный старт ролика'},
            {'start': 4.9, 'end': 6.7, 'text': 'Караоке субтитры работают'},
        ],
    )
    assert res['ok'] is True
    assert res['source_type'] == 'file'
    assert res['stt_engine'] == 'provided_transcript'
    assert len(res['clips']) == 1

    clip = res['clips'][0]
    # PeakCut should select the loud 3-7s segment rather than the quiet 0-3s intro
    assert clip['start_sec'] >= 2.0
    assert clip['end_sec'] <= 7.8
    assert clip['resolution'] == '1080x1920'
    assert clip['subtitles_count'] >= 2

    out_mp4 = Path(clip['output_path'])
    assert out_mp4.is_file() and out_mp4.stat().st_size > 10_000

    probe = subprocess.run(
        [
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name,width,height,pix_fmt',
            '-of', 'json', str(out_mp4),
        ],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(probe.stdout)['streams'][0]
    assert stream['codec_name'] == 'h264'
    assert stream['width'] == 1080
    assert stream['height'] == 1920
    assert stream['pix_fmt'] == 'yuv420p'


def test_clip_video_url_mode_uses_downloader_and_energy_vad_fallback(tmp_path: Path):
    src_mp4 = tmp_path / 'downloaded_src.mp4'
    _make_sample_video(src_mp4)

    def fake_download(url: str, dest_dir: Path):
        dest_dir.mkdir(parents=True, exist_ok=True)
        local = dest_dir / 'dl_video.mp4'
        shutil.copy2(src_mp4, local)
        return local, None

    res = video_clipper.clip_video(
        source='https://example.com/watch?v=demo123',
        output_dir=tmp_path / 'out_url',
        clip_duration=3.5,
        max_clips=1,
        download_fn=fake_download,
    )
    assert res['ok'] is True
    assert res['source_type'] == 'url'
    assert res['stt_engine'] == 'energy_vad'
    assert res['clips'][0]['start_sec'] >= 2.0
    assert Path(res['clips'][0]['output_path']).is_file()


def test_bot_clip_free_trials_then_stars_paywall_and_payment_delivery(bot_env, tmp_path: Path):
    cfg_paused, cfg_paid, db_path = bot_env
    sample_clip = tmp_path / 'rendered_short.mp4'
    sample_clip.write_bytes(b'FAKE-MP4-BYTES-9-16')

    api_calls = []
    sent_videos = []
    clip_calls = []

    def fake_api(token, method, payload=None):
        if method.startswith('getFile'):
            return {'ok': True, 'result': {'file_path': 'videos/in.mp4'}}
        api_calls.append((method, payload))
        return {'ok': True}

    def fake_download(token, file_path, dest_path):
        dest_path.write_bytes(b'raw-tg-video')

    def fake_send_video(token, chat_id, file_path, caption=''):
        sent_videos.append((chat_id, Path(file_path).name, Path(file_path).read_bytes(), caption))
        return {'ok': True, 'result': {'video': {'file_id': f'tg_vid_{len(sent_videos)}'}}}

    def fake_clip(source, out_dir: Path):
        clip_calls.append(str(source))
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / 'short_01.mp4'
        shutil.copy2(sample_clip, dest)
        return {'ok': True, 'clips': [{'output_path': str(dest), 'resolution': '1080x1920'}]}

    # 0. Bare /clip shows usage + 2/2 free trials remaining; invalid arg rejected
    studio_bot.process_update(
        {
            'update_id': 700,
            'message': {'from': {'id': 777001, 'username': 'clipper'}, 'chat': {'id': 777001, 'type': 'private'}, 'text': '/clip'},
        },
        cfg_paused, api_fn=fake_api, send_video_fn=fake_send_video, clip_fn=fake_clip,
    )
    assert 'осталось бесплатных клипов: 2 из 2' in api_calls[-1][1]['text']

    studio_bot.process_update(
        {
            'update_id': 701,
            'message': {'from': {'id': 777001}, 'chat': {'id': 777001, 'type': 'private'}, 'text': '/clip not-a-url'},
        },
        cfg_paused, api_fn=fake_api, send_video_fn=fake_send_video, clip_fn=fake_clip,
    )
    assert 'корректную ссылку' in api_calls[-1][1]['text']

    # Failed trial must NOT consume free quota
    def failing_clip(source, out_dir: Path):
        raise RuntimeError(f'broken https://api.telegram.org/bot{cfg_paused.token}/fail')

    studio_bot.process_update(
        {
            'update_id': 702,
            'message': {
                'from': {'id': 777001}, 'chat': {'id': 777001, 'type': 'private'},
                'text': '/clip https://example.com/broken',
            },
        },
        cfg_paused, api_fn=fake_api, send_video_fn=fake_send_video, clip_fn=failing_clip,
    )
    assert cfg_paused.token not in api_calls[-1][1]['text']
    assert StoreDB(db_path).count_user_free_clips('777001') == 0

    # 1. Free trial 1/2 via URL (works even with payments_enabled=False)
    studio_bot.process_update(
        {
            'update_id': 703,
            'message': {
                'from': {'id': 777001, 'username': 'clipper'}, 'chat': {'id': 777001, 'type': 'private'},
                'text': '/clip https://youtube.com/watch?v=abc123',
            },
        },
        cfg_paused, api_fn=fake_api, send_video_fn=fake_send_video, clip_fn=fake_clip,
    )
    assert len(sent_videos) == 1
    assert 'пробный 1/2' in sent_videos[0][3]

    # 2. Free trial 2/2 via attached Telegram video with caption /clip
    studio_bot.process_update(
        {
            'update_id': 704,
            'message': {
                'from': {'id': 777001, 'username': 'clipper'}, 'chat': {'id': 777001, 'type': 'private'},
                'caption': '/clip',
                'video': {'file_id': 'tg_raw_vid_1'},
            },
        },
        cfg_paused, api_fn=fake_api, download_fn=fake_download, send_video_fn=fake_send_video, clip_fn=fake_clip,
    )
    assert len(sent_videos) == 2
    assert 'пробный 2/2' in sent_videos[1][3]
    assert StoreDB(db_path).count_user_free_clips('777001') == CLIP_FREE_TRIAL_LIMIT

    # 3a. 3rd /clip with payments_enabled=False must fail-closed (no invoice, no render)
    before_clips = len(clip_calls)
    studio_bot.process_update(
        {
            'update_id': 705,
            'message': {
                'from': {'id': 777001}, 'chat': {'id': 777001, 'type': 'private'},
                'text': '/clip https://youtube.com/watch?v=paid3',
            },
        },
        cfg_paused, api_fn=fake_api, send_video_fn=fake_send_video, clip_fn=fake_clip,
    )
    assert len(clip_calls) == before_clips
    assert api_calls[-1][0] == 'sendMessage'
    assert f'{CLIP_PRICE_XTR} Stars (XTR)' in api_calls[-1][1]['text']

    # 3b. 3rd /clip with payments_enabled=True sends Telegram Stars (XTR) invoice
    studio_bot.process_update(
        {
            'update_id': 706,
            'message': {
                'from': {'id': 777001, 'username': 'clipper'}, 'chat': {'id': 777001, 'type': 'private'},
                'text': '/clip https://youtube.com/watch?v=paid3',
            },
        },
        cfg_paid, api_fn=fake_api, send_video_fn=fake_send_video, clip_fn=fake_clip,
    )
    assert api_calls[-1][0] == 'sendInvoice'
    inv = api_calls[-1][1]
    assert inv['currency'] == 'XTR'
    assert inv['provider_token'] == ''
    assert inv['prices'][0]['amount'] == CLIP_PRICE_XTR
    payload = inv['payload']
    order_id = payload.split(':', 1)[1]

    # Tampered pre_checkout rejected, valid pre_checkout accepted
    studio_bot.process_update(
        {
            'update_id': 707,
            'pre_checkout_query': {
                'id': 'pcq_bad', 'from': {'id': 777001}, 'currency': 'XTR',
                'total_amount': 1, 'invoice_payload': payload,
            },
        },
        cfg_paid, api_fn=fake_api,
    )
    assert api_calls[-1] == ('answerPreCheckoutQuery', {'pre_checkout_query_id': 'pcq_bad', 'ok': False, 'error_message': 'Сумма заказа не совпадает.'})

    studio_bot.process_update(
        {
            'update_id': 708,
            'pre_checkout_query': {
                'id': 'pcq_ok', 'from': {'id': 777001}, 'currency': 'XTR',
                'total_amount': CLIP_PRICE_XTR, 'invoice_payload': payload,
            },
        },
        cfg_paid, api_fn=fake_api,
    )
    assert api_calls[-1] == ('answerPreCheckoutQuery', {'pre_checkout_query_id': 'pcq_ok', 'ok': True})

    # successful_payment renders and delivers 3rd clip; replay does not duplicate
    pay_upd = {
        'update_id': 709,
        'message': {
            'from': {'id': 777001}, 'chat': {'id': 777001, 'type': 'private'},
            'successful_payment': {
                'currency': 'XTR', 'total_amount': CLIP_PRICE_XTR,
                'invoice_payload': payload, 'telegram_payment_charge_id': 'chg_clip_001',
            },
        },
    }
    studio_bot.process_update(pay_upd, cfg_paid, api_fn=fake_api, send_video_fn=fake_send_video, clip_fn=fake_clip)
    assert len(sent_videos) == 3
    assert order_id in sent_videos[2][3]

    dup_upd = dict(pay_upd)
    dup_upd['update_id'] = 710
    studio_bot.process_update(dup_upd, cfg_paid, api_fn=fake_api, send_video_fn=fake_send_video, clip_fn=fake_clip)
    assert len(sent_videos) == 3

    store = StoreDB(db_path)
    assert store.get_order(order_id)['status'] == 'delivered'
    assert store.get_clip_job_by_order(order_id)['status'] == 'delivered'
    assert all(p['product_id'] != CLIP_PRODUCT_ID for p in store.list_ready_products())


def test_send_telegram_video_multipart_upload(tmp_path: Path, monkeypatch):
    mp4 = tmp_path / 'short.mp4'
    mp4.write_bytes(b'mp4-data')
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b'{"ok": true, "result": {"video": {"file_id": "vid_123"}}}'

    def fake_urlopen(req, timeout=0):
        captured['url'] = req.full_url
        captured['content_type'] = req.get_header('Content-type')
        captured['body'] = req.data
        return FakeResp()

    monkeypatch.setattr(studio_bot.urllib.request, 'urlopen', fake_urlopen)
    res = studio_bot.send_telegram_video('123:tok', 777001, mp4, caption='Shorts 9:16')
    assert res['result']['video']['file_id'] == 'vid_123'
    assert '/sendVideo' in captured['url']
    assert b'filename="short.mp4"' in captured['body']
    assert b'Shorts 9:16' in captured['body']
