# NeonClipping MVP — Design

## Архитектура модулей

1. `scripts/neon_clipper.py` — clean-room ядро анализа, скоринга, субтитров, 9:16 рендера и feedback-ранкера:
   - Переиспользует безопасные примитивы из `scripts/video_clipper.py` (`is_http_url`, `parse_subtitle_file`, `probe_media`, `extract_audio_wav`, `SpeechSegment`, `WordTimestamp`).
   - `parse_json3_captions(path)`: разбирает YouTube `json3` события (`tStartMs`, `dDurationMs`, `segs[].utf8`, `tOffsetMs`) в `list[SpeechSegment]` с пословными таймингами.
   - `fetch_audio_and_captions(url, dest_dir)` + `download_video_section(url, start, end, dest_dir)`: 2-фазный audio-first загрузчик на `yt-dlp` с инъекцией функций для тестов.
   - `compute_voice_band_energy(wav_path)` (Goertzel 2000–4000 Hz) и `compute_loudness_jumps(energy_frames)` + `compute_contrast_score(...)` + `score_transcript_hooks(...)`: чистый Python без сторонних бинарных пакетов.
   - `snap_window_to_sentences(...)`: сдвиг границ окна к концам предложений/сегментов внутри `[min_dur, max_dur]`.
   - `judge_candidate_with_local_llm(...)` + `generate_title_and_hook(...)`: локальный Ollama (`127.0.0.1`) с мгновенным deterministic fallback.
   - `build_styled_ass(...)`, `write_srt(...)`, `write_vtt(...)`, `render_neon_clip(...)`: поддержка 5 стилей субтитров, opening heading и 3 режимов кадрирования (`blur_pad`, `center_crop`, `split_cam`).
   - `record_clip_feedback(...)`, `fit_preference_weights(...)`: локальный JSONL-ранкер.
   - `neon_clip_pipeline(...)` + CLI `main()`.

2. `scripts/neon_clipper_app.py` + `clipper.html` — локальный Web UI и JSON API (`127.0.0.1:8770`):
   - `ThreadingHTTPServer`, привязанный к `127.0.0.1` по умолчанию.
   - Строгий allowlist путей, защита от `..` / симлинков через `resolve().is_relative_to(...)`, корректный HTTP `206 Partial Content` для предпросмотра MP4 в `<video>`.

3. `tests/test_neon_clipper.py` — полный набор автотестов `pytest`:
   - `json3` парсер, Goertzel 2–4 кГц, loudness jump + contrast, sentence snapping, SSRF-валидация.
   - Audio-first pipeline с mock-загрузчиками (доказательство что полное видео не качается до выбора окна).
   - Локальный LLM mock + deterministic fallback.
   - Все 5 стилей субтитров + `opening_heading` + `.srt`/`.vtt` + `split_cam`/`blur_pad`/`center_crop` реальный рендер через `ffmpeg` + проверка `ffprobe`.
   - `feedback.jsonl` дедупликация и адаптация весов после >= 8 уникальных оценок.
   - HTTP-сервер API (`/api/health`, `/api/clip`, `/api/clips`, `/api/feedback`, `/clips/...` Range + traversal protection).
