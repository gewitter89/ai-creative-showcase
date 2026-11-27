import json
import shutil
import subprocess
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio_bot


@pytest.fixture()
def cfg(tmp_path: Path) -> studio_bot.BotConfig:
    repo = tmp_path / 'repo'
    repo.mkdir()
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / 'catalog.json', repo / 'catalog.json')
    shutil.copy2(root / 'generation_manifest.json', repo / 'generation_manifest.json')
    return studio_bot.BotConfig(
        token='12345:SECRET_TOKEN',
        owner_ids=frozenset({'111222'}),
        repo=repo,
        private_dir=tmp_path / 'private',
        auto_push=True,
        push_branch='feat/production-batch-01',
    )


def test_fail_closed_when_owner_ids_empty(monkeypatch):
    with pytest.raises(ValueError, match='fail-closed'):
        studio_bot.load_config({'STUDIO_BOT_TOKEN': '123:abc', 'STUDIO_OWNER_IDS': ''})

    with pytest.raises(ValueError, match='fail-closed'):
        studio_bot.load_config({'STUDIO_BOT_TOKEN': '123:abc', 'STUDIO_OWNER_IDS': ' , '})

    monkeypatch.setenv('STUDIO_BOT_TOKEN', '123:abc')
    monkeypatch.setenv('STUDIO_OWNER_IDS', '')
    with pytest.raises(SystemExit, match='fail-closed'):
        studio_bot.main()


def test_load_config_rejects_invalid_owner_id_and_main_branch():
    with pytest.raises(ValueError, match='Invalid owner ID'):
        studio_bot.load_config({'STUDIO_BOT_TOKEN': '123:abc', 'STUDIO_OWNER_IDS': 'alice'})

    with pytest.raises(ValueError, match='feat/production-batch-01'):
        studio_bot.load_config({
            'STUDIO_BOT_TOKEN': '123:abc',
            'STUDIO_OWNER_IDS': '111222',
            'STUDIO_PUSH_BRANCH': 'main',
        })


def test_customer_role_cannot_access_admin_or_ingest(cfg: studio_bot.BotConfig):
    sent = []
    ingested = []

    def fake_api(token, method, payload=None):
        sent.append((method, payload))
        return {'ok': True}

    def fake_ingest(*args, **kwargs):
        ingested.append(args)
        return {}

    # Customer /start in private chat gets customer prompt, not admin prompt
    studio_bot.process_update(
        {
            'update_id': 1,
            'message': {
                'from': {'id': 999888},
                'chat': {'id': 999888, 'type': 'private'},
                'text': '/start buy_ny-kyiv-01',
            },
        },
        cfg,
        api_fn=fake_api,
        ingest_fn=fake_ingest,
    )
    assert len(sent) == 1
    assert 'Заявка по NAUM AI Creative Studio' in sent[0][1]['text']

    # Customer /items and upload are ignored
    studio_bot.process_update(
        {
            'update_id': 2,
            'message': {
                'from': {'id': 999888},
                'chat': {'id': 999888, 'type': 'private'},
                'text': '/items',
            },
        },
        cfg,
        api_fn=fake_api,
        ingest_fn=fake_ingest,
    )
    studio_bot.process_update(
        {
            'update_id': 3,
            'message': {
                'from': {'id': 999888},
                'chat': {'id': 999888, 'type': 'private'},
                'caption': 'ny-kyiv-01',
                'document': {'file_id': 'doc1'},
            },
        },
        cfg,
        api_fn=fake_api,
        ingest_fn=fake_ingest,
    )
    assert len(sent) == 1
    assert ingested == []


def test_owner_upload_rejected_outside_private_chat(cfg: studio_bot.BotConfig):
    sent = []
    ingested = []
    pushed = []

    def fake_api(token, method, payload=None):
        sent.append((method, payload))
        return {'ok': True}

    for chat_type in ('group', 'supergroup', 'channel'):
        studio_bot.process_update(
            {
                'update_id': 10,
                'message': {
                    'from': {'id': 111222},
                    'chat': {'id': -100555, 'type': chat_type},
                    'caption': 'ny-kyiv-01',
                    'document': {'file_id': 'doc1'},
                },
            },
            cfg,
            api_fn=fake_api,
            ingest_fn=lambda *a, **kw: ingested.append(a) or {},
            push_fn=lambda *a, **kw: pushed.append(a),
        )
        studio_bot.process_update(
            {
                'update_id': 11,
                'message': {
                    'from': {'id': 111222},
                    'chat': {'id': -100555, 'type': chat_type},
                    'text': '/items',
                },
            },
            cfg,
            api_fn=fake_api,
            ingest_fn=lambda *a, **kw: ingested.append(a) or {},
            push_fn=lambda *a, **kw: pushed.append(a),
        )

    assert ingested == []
    assert pushed == []
    assert all('приватном чате' in payload['text'] for _, payload in sent)


def test_owner_private_upload_ingests_and_pushes_and_sanitizes_token(cfg: studio_bot.BotConfig):
    sent = []
    pushed = []

    def fake_api(token, method, payload=None):
        if method.startswith('getFile'):
            return {'ok': True, 'result': {'file_path': 'photos/file.png'}}
        sent.append((method, payload))
        return {'ok': True}

    def fake_download(token, file_path, dest_path):
        dest_path.write_bytes(b'png-bytes')

    def fake_ingest(repo, item_id, tmp_path, private_dir):
        return {
            'item_id': item_id,
            'preview_path': 'assets/newyear/ny-kyiv-01/preview.webp',
            'preview_size': '1200x800',
            'catalog_status': 'проверка качества',
        }

    def fake_push(repo, preview_path, item_id, branch):
        pushed.append((repo, preview_path, item_id, branch))

    studio_bot.process_update(
        {
            'update_id': 20,
            'message': {
                'from': {'id': 111222},
                'chat': {'id': 111222, 'type': 'private'},
                'caption': 'ny-kyiv-01',
                'document': {'file_id': 'doc1', 'file_size': 1024},
            },
        },
        cfg,
        api_fn=fake_api,
        ingest_fn=fake_ingest,
        download_fn=fake_download,
        push_fn=fake_push,
    )

    assert len(pushed) == 1
    assert pushed[0][3] == 'feat/production-batch-01'
    assert 'OK ny-kyiv-01' in sent[-1][1]['text']

    # Verify token redaction on error
    def fail_download(token, file_path, dest_path):
        raise RuntimeError(f'failed https://api.telegram.org/file/bot{token}/x')

    studio_bot.process_update(
        {
            'update_id': 21,
            'message': {
                'from': {'id': 111222},
                'chat': {'id': 111222, 'type': 'private'},
                'caption': 'ny-kyiv-01',
                'document': {'file_id': 'doc2'},
            },
        },
        cfg,
        api_fn=fake_api,
        ingest_fn=fake_ingest,
        download_fn=fail_download,
        push_fn=fake_push,
    )
    assert cfg.token not in sent[-1][1]['text']
    assert '***' in sent[-1][1]['text']


def test_verify_push_branch_allows_only_feat_production_batch_01(tmp_path: Path):
    repo = tmp_path / 'git_repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.com'], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Test'], check=True)
    (repo / 'README.md').write_text('init\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(repo), 'add', 'README.md'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'init'], check=True)

    with pytest.raises(RuntimeError, match="Refusing git push on branch 'main'"):
        studio_bot.verify_push_branch(repo)

    subprocess.run(['git', '-C', str(repo), 'checkout', '-b', 'feat/production-batch-01'], check=True)
    assert studio_bot.verify_push_branch(repo) == 'feat/production-batch-01'
