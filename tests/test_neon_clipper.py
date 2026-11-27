import json
import math
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import wave
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import neon_clipper
import neon_clipper_app


def _write_test_wav(path: Path, duration_sec: float = 8.0, sample_rate: int = 16000) -> None:
    """Create WAV: 0-3s quiet 180Hz hum, 3-6.5s loud 2800Hz raised-voice band, 6.5-8s quiet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    nframes = int(duration_sec * sample_rate)
    samples: list[int] = []
    for i in range(nframes):
        t = i / sample_rate
        if 3.0 <= t <= 6.5:
            amp = 0.72 * math.sin(2.0 * math.pi * 2800.0 * t)
        else:
            amp = 0.03 * math.sin(2.0 * math.pi * 180.0 * t)
        samples.append(int(max(-32767, min(32767, amp * 32767))))
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f'<{len(samples)}h', *samples))


def _make_sample_mp4(path: Path, duration_sec: float = 10.0) -> None:
    """Create 1280x720 MP4 with quiet intro/outro and loud 3.5-8.0s action segment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        'ffmpeg', '-y',
        '-f', 'lavfi', '-i', f'testsrc2=size=1280x720:rate=25:duration={duration_sec}',
        '-f', 'lavfi', '-i',
        f"aevalsrc='if(between(t,3.5,8.0),0.65*sin(2*PI*2600*t),0.01*sin(2*PI*160*t))':s=16000:d={duration_sec}",
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '96k',
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_parse_json3_captions_and_word_timings(tmp_path: Path):
    j3 = tmp_path / 'captions.json3'
    j3.write_text(
        json.dumps(
            {
                'events': [
                    {
                        'tStartMs': 1000,
                        'dDurationMs': 2400,
                        'segs': [
                            {'utf8': 'Ого ', 'tOffsetMs': 0},
                            {'utf8': 'дивись\n', 'tOffsetMs': 600},
                            {'utf8': 'сюда!', 'tOffsetMs': 1300},
                        ],
                    },
                    {'tStartMs': 3500, 'dDurationMs': 1200, 'segs': [{'utf8': '\n'}]},
                    {'tStartMs': 4000, 'dDurationMs': 2000, 'segs': [{'utf8': 'Вот это поворот'}]},
                ]
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )
    segs = neon_clipper.parse_json3_captions(j3)
    assert len(segs) == 2
    assert segs[0].text == 'Ого дивись сюда!'
    assert [w.word for w in segs[0].words] == ['Ого', 'дивись', 'сюда!']
    assert segs[0].words[1].start >= 1.5
    assert segs[1].text == 'Вот это поворот'


def test_goertzel_voice_band_2_4khz_and_loudness_jump(tmp_path: Path):
    wav = tmp_path / 'voice.wav'
    _write_test_wav(wav, duration_sec=8.0)

    energy = neon_clipper.analyze_wav_energy(wav, step_sec=0.25)
    jumps = neon_clipper.compute_loudness_jumps(energy)
    voice = neon_clipper.compute_voice_band_energy(wav, step_sec=0.25)

    quiet_voice = [v for s, e, v in voice if e <= 2.75]
    loud_voice = [v for s, e, v in voice if s >= 3.25 and e <= 6.25]
    loud_jumps = [j for s, e, j in jumps if s >= 3.0 and e <= 6.5]
    quiet_jumps = [j for s, e, j in jumps if e <= 2.5]

    assert max(loud_voice) > 0.75
    assert max(quiet_voice) < 0.1
    assert max(loud_jumps) > max(quiet_jumps) + 0.5


def test_sentence_boundary_snapping_and_hook_scoring():
    segs = [
        neon_clipper.SpeechSegment(start=0.0, end=2.4, text='Обычное вступление'),
        neon_clipper.SpeechSegment(start=2.5, end=6.2, text='Ого, вы видели этот секрет?!'),
        neon_clipper.SpeechSegment(start=6.3, end=10.5, text='Вот почему это работает идеально!'),
        neon_clipper.SpeechSegment(start=10.8, end=14.0, text='Заключение ролика.'),
    ]
    s, e = neon_clipper.snap_window_to_sentences(
        start=2.8,
        end=9.9,
        duration=15.0,
        speech_segments=segs,
        silence_intervals=[],
        min_dur=4.0,
        max_dur=9.0,
    )
    assert s == 2.5
    assert e == 10.5
    assert 4.0 <= (e - s) <= 9.0

    hook_score = neon_clipper.score_transcript_hooks(segs, 2.5, 10.5)
    flat_score = neon_clipper.score_transcript_hooks(segs, 0.0, 2.4)
    assert hook_score > flat_score

    title, hook = neon_clipper.generate_title_and_hook(segs, 2.5, 10.5)
    assert title
    assert '?' in hook or '!' in hook


def test_audio_first_url_pipeline_never_downloads_full_video(tmp_path: Path):
    full_src = tmp_path / 'upstream_stream.mp4'
    _make_sample_mp4(full_src, duration_sec=12.0)

    audio_wav = tmp_path / 'only_audio.wav'
    assert neon_clipper.extract_audio_wav(full_src, audio_wav) is True

    j3 = tmp_path / 'yt.json3'
    j3.write_text(
        json.dumps(
            {
                'events': [
                    {'tStartMs': 500, 'dDurationMs': 2500, 'segs': [{'utf8': 'Тихое начало'}]},
                    {'tStartMs': 3600, 'dDurationMs': 4200, 'segs': [{'utf8': 'Шок! Смотри как взлетает звук!'}]},
                    {'tStartMs': 8500, 'dDurationMs': 2500, 'segs': [{'utf8': 'Финал'}]},
                ]
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )

    section_calls: list[tuple[float, float]] = []

    def fake_audio_fetch(url: str, dest: Path):
        dest.mkdir(parents=True, exist_ok=True)
        a_copy = dest / 'audio.wav'
        c_copy = dest / 'audio.json3'
        a_copy.write_bytes(audio_wav.read_bytes())
        c_copy.write_bytes(j3.read_bytes())
        return a_copy, c_copy, 12.0

    def fake_section_fetch(url: str, start_sec: float, end_sec: float, dest: Path):
        section_calls.append((start_sec, end_sec))
        dest.mkdir(parents=True, exist_ok=True)
        sec_mp4 = dest / 'section.mp4'
        res = subprocess.run(
            [
                'ffmpeg', '-y', '-ss', f'{start_sec:.3f}', '-t', f'{max(1.0, end_sec - start_sec):.3f}',
                '-i', str(full_src), '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(sec_mp4),
            ],
            capture_output=True, text=True,
        )
        assert res.returncode == 0, res.stderr
        return sec_mp4, start_sec

    def forbidden_full_download(url: str, dest: Path):
        raise AssertionError('Full video download must NOT run in fast audio-first mode!')

    out_dir = tmp_path / 'out'
    res = neon_clipper.neon_clip_pipeline(
        source='https://www.youtube.com/watch?v=dQw4w9WgXcQ',
        output_dir=out_dir,
        mode='fast',
        min_duration=3.5,
        max_duration=5.5,
        max_clips=1,
        caption_style='neon',
        framing='blur_pad',
        audio_fetch_fn=fake_audio_fetch,
        section_fetch_fn=fake_section_fetch,
        full_download_fn=forbidden_full_download,
    )

    assert res['ok'] is True
    assert res['source_type'] == 'url_audio_first'
    assert res['stt_engine'] == 'youtube_json3'
    assert len(section_calls) == 1
    assert len(res['clips']) == 1

    clip = res['clips'][0]
    assert clip['start_sec'] >= 2.0
    assert clip['resolution'] == '1080x1920'
    assert Path(clip['output_path']).is_file()
    assert Path(clip['srt_path']).is_file()
    assert Path(clip['vtt_path']).is_file()
    assert 'WEBVTT' in Path(clip['vtt_path']).read_text(encoding='utf-8')


def test_all_caption_styles_split_cam_and_local_llm_judge(tmp_path: Path):
    segs = [neon_clipper.SpeechSegment(start=0.5, end=3.5, text='Тест пяти стилей')]
    for style in neon_clipper.CAPTION_STYLES:
        ass = tmp_path / f'{style}.ass'
        cnt = neon_clipper.build_styled_ass(
            segs, 0.0, 4.0, ass, style_name=style, opening_heading='Почему это работает?'
        )
        assert cnt == 1
        txt = ass.read_text(encoding='utf-8')
        assert 'Style: Heading' in txt
        assert 'Почему это работает?' in txt

    assert neon_clipper.is_local_ollama_url('http://127.0.0.1:11434/api/generate') is True
    assert neon_clipper.is_local_ollama_url('https://evil.example.com/api') is False

    src = tmp_path / 'split_src.mp4'
    _make_sample_mp4(src, duration_sec=6.0)

    res = neon_clipper.neon_clip_pipeline(
        source=src,
        output_dir=tmp_path / 'split_out',
        mode='full',
        min_duration=3.0,
        max_duration=4.5,
        max_clips=1,
        caption_style='hormozi',
        framing='split_cam',
        transcript_segments=[{'start': 1.0, 'end': 4.5, 'text': 'Вау, split cam работает!'}],
        use_local_llm=True,
        llm_fn=lambda text, model: {'score': 0.92, 'title': 'Секрет сплит-экрана', 'hook': 'Как это сделано?'},
    )
    clip = res['clips'][0]
    assert clip['llm_score'] == 0.92
    assert clip['title'] == 'Секрет сплит-экрана'
    assert clip['hook'] == 'Как это сделано?'
    assert clip['framing'] == 'split_cam'

    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json', clip['output_path']],
        capture_output=True, text=True, check=True,
    )
    st = json.loads(probe.stdout)['streams'][0]
    assert st['width'] == 1080 and st['height'] == 1920


def test_feedback_jsonl_dedup_and_weight_adaptation(tmp_path: Path):
    fb = tmp_path / 'feedback.jsonl'
    for i in range(7):
        neon_clipper.record_clip_feedback(
            fb, clip_id=f'c_{i}', rating=5 if i % 2 == 0 else 1,
            features={'jump': 0.9 if i % 2 == 0 else 0.1, 'voice_2_4k': 0.8, 'contrast': 0.2, 'keyword': 0.2, 'speech_ratio': 0.5},
        )
    # Duplicate update for c_0 must NOT satisfy >= 8 unique moments
    neon_clipper.record_clip_feedback(
        fb, clip_id='c_0', rating=4,
        features={'jump': 0.85, 'voice_2_4k': 0.8, 'contrast': 0.2, 'keyword': 0.2, 'speech_ratio': 0.5},
    )
    w_before, stats_before = neon_clipper.fit_preference_weights(fb)
    assert stats_before['adapted'] is False
    assert stats_before['unique_moments'] == 7
    assert stats_before['total_events'] == 8
    assert w_before == neon_clipper.DEFAULT_SIGNAL_WEIGHTS

    # 8th unique moment unlocks preference adaptation
    neon_clipper.record_clip_feedback(
        fb, clip_id='c_7', rating=1,
        features={'jump': 0.05, 'voice_2_4k': 0.1, 'contrast': 0.2, 'keyword': 0.2, 'speech_ratio': 0.5},
    )
    w_after, stats_after = neon_clipper.fit_preference_weights(fb)
    assert stats_after['adapted'] is True
    assert stats_after['unique_moments'] == 8
    assert abs(sum(w_after.values()) - 1.0) < 0.01
    assert w_after['jump'] > w_after['contrast']


def test_neon_clipper_http_server_api_range_and_traversal_guard(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = tmp_path / 'web_clips'
    src = tmp_path / 'web_src.mp4'
    _make_sample_mp4(src, duration_sec=6.0)

    srv = neon_clipper_app.create_server(repo_root=repo_root, output_dir=out_dir, host='127.0.0.1', port=0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{port}'
        with urllib.request.urlopen(base + '/', timeout=5) as resp:
            assert resp.status == 200
            assert 'NeonClipping MVP' in resp.read().decode('utf-8')

        with urllib.request.urlopen(base + '/api/health', timeout=5) as resp:
            h = json.loads(resp.read().decode('utf-8'))
            assert h['ok'] is True
            assert 'neon' in h['styles']

        req_clip = urllib.request.Request(
            base + '/api/clip',
            data=json.dumps({
                'source': str(src),
                'mode': 'full',
                'min_duration': 3.0,
                'max_duration': 4.5,
                'max_clips': 1,
                'style': 'impact',
                'framing': 'blur_pad',
            }).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req_clip, timeout=60) as resp:
            res = json.loads(resp.read().decode('utf-8'))
            assert res['ok'] is True
            assert len(res['clips']) == 1
            clip = res['clips'][0]

        with urllib.request.urlopen(base + '/api/clips', timeout=5) as resp:
            listed = json.loads(resp.read().decode('utf-8'))
            assert len(listed['clips']) == 1

        range_req = urllib.request.Request(
            f"{base}/clips/{clip['mp4_name']}",
            headers={'Range': 'bytes=0-255'},
        )
        with urllib.request.urlopen(range_req, timeout=5) as resp:
            assert resp.status == 206
            assert resp.headers.get('Content-Range', '').startswith('bytes 0-255/')
            assert len(resp.read()) == 256

        req_fb = urllib.request.Request(
            base + '/api/feedback',
            data=json.dumps({'clip_id': clip['clip_id'], 'rating': 5, 'features': clip['features']}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req_fb, timeout=5) as resp:
            fb_res = json.loads(resp.read().decode('utf-8'))
            assert fb_res['ok'] is True
            assert fb_res['feedback_stats']['unique_moments'] == 1

        for bad in ('/clips/../../etc/passwd', '/clips/missing.mp4', '/scripts/neon_clipper.py'):
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + bad, timeout=5)
            assert err.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)
