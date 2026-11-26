import json
import sqlite3
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio_bot as bot


def test_callback_radar_build_queues_task_for_owner(tmp_path, monkeypatch):
    radar_db = tmp_path / "radar.db"
    queue_file = tmp_path / "queue.json"
    conn = sqlite3.connect(radar_db)
    conn.execute(
        "CREATE TABLE signals (source_id TEXT PRIMARY KEY, title TEXT, url TEXT, summary TEXT, license TEXT)"
    )
    conn.execute(
        "INSERT INTO signals VALUES (?, ?, ?, ?, ?)",
        ("gh:123", "acme/cool-tool", "https://github.com/acme/cool-tool", "Useful tool", "MIT"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("STUDIO_RADAR_DB", str(radar_db))
    monkeypatch.setenv("STUDIO_AUTOPILOT_QUEUE", str(queue_file))

    calls = []
    def fake_api(token, method, payload=None):
        calls.append((method, payload))
        return {"ok": True, "result": {}}

    cfg = bot.BotConfig(
        token="123:TEST",
        owner_ids={'7636868692'},
        repo=tmp_path,
        private_dir=tmp_path / "priv",
        auto_push=False,
        push_branch="feat/production-batch-01",
        db_path=tmp_path / "store.db",
        packages_dir=tmp_path / "pkg",
        payments_enabled=False,
    )
    upd = {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 7636868692},
            "message": {"chat": {"id": 7636868692, "type": "private"}},
            "data": "radar_build:gh:123",
        },
    }
    bot.process_update(upd, cfg, api_fn=fake_api)
    q = json.loads(queue_file.read_text(encoding="utf-8"))
    assert len(q) == 1
    assert q[0]["source_id"] == "gh:123"
    assert any(m == "sendMessage" and "Поставлено в очередь облака" in p["text"] for m, p in calls)


def test_callback_blocked_for_non_owner(tmp_path):
    calls = []
    def fake_api(token, method, payload=None):
        calls.append((method, payload))
        return {"ok": True, "result": {}}

    cfg = bot.BotConfig(
        token="123:TEST",
        owner_ids={'7636868692'},
        repo=tmp_path,
        private_dir=tmp_path / "priv",
        auto_push=False,
        push_branch="feat/production-batch-01",
        db_path=tmp_path / "store.db",
        packages_dir=tmp_path / "pkg",
        payments_enabled=False,
    )
    upd = {
        "update_id": 2,
        "callback_query": {
            "id": "cb2",
            "from": {"id": 999999},
            "message": {"chat": {"id": 999999, "type": "private"}},
            "data": "radar_build:gh:123",
        },
    }
    bot.process_update(upd, cfg, api_fn=fake_api)
    assert calls[0][0] == "answerCallbackQuery"
    assert calls[0][1]["show_alert"] is True
