import json
from pathlib import Path
import sqlite3

import pytest

from resources.lib.data_paths import data_path
from dev.profile_layout import migrate, plan, rollback, _project_key


def test_agent_homes_and_workspaces_are_separate_and_components_stay_shared(tmp_path):
    expected = {'codex-home': 'agents/codex/home', 'codex-work': 'agents/codex/work',
                'codebuddy-home': 'agents/codebuddy/external/home',
                'codebuddy-work': 'agents/codebuddy/external/work',
                'codebuddy-home-internal': 'agents/codebuddy/internal/home',
                'codebuddy-work-internal': 'agents/codebuddy/internal/work',
                'capture.lock': 'runtime/capture.lock', 'b25.sock': 'runtime/b25.sock',
                'codebuddy-auth-internal.json': 'agents/codebuddy/internal/auth-revision.json',
                'asr/control.sock': 'runtime/asr/control.sock',
                'asr/lifecycle.json': 'diagnostics/asr/lifecycle.json',
                'last_agent.json': 'diagnostics/last_agent.json',
                'conversation.json': 'history/conversation.json',
                'voice-binding.json': 'config/voice-binding.json',
                'components': 'components', 'settings.xml': 'settings.xml',
                'codex-api-key.txt': 'codex-api-key.txt', 'doubao-api-key.txt': 'doubao-api-key.txt'}
    for name, target in expected.items():
        assert data_path(tmp_path, name) == tmp_path / target
    assert (tmp_path / 'agents/codebuddy/internal').stat().st_mode & 0o777 == 0o700
    for name in ('/etc/shadow', '../other', 'runtime/../../other'):
        with pytest.raises(ValueError):
            data_path(tmp_path, name)


def sample_profile(profile):
    (profile / 'codex-home').mkdir()
    (profile / 'codex-home/auth.json').write_text('private-login-test')
    session = profile / 'codex-home/sessions/example.jsonl'
    session.parent.mkdir()
    session.write_text('private-history-test')
    with sqlite3.connect(str(profile / 'codex-home/state_5.sqlite')) as connection:
        connection.execute('CREATE TABLE threads(id TEXT, rollout_path TEXT, cwd TEXT)')
        connection.execute('INSERT INTO threads VALUES(?,?,?)',
                           ('existing-thread', str(session), str(profile / 'codex-work')))
    for home, work in (('codebuddy-home', 'codebuddy-work'), ('codebuddy-home-internal', 'codebuddy-work-internal')):
        project = profile / home / 'projects' / _project_key(profile / work)
        project.mkdir(parents=True)
        (project / 'session.jsonl').write_text('private-buddy-history-test')
        (profile / work).mkdir()
        (profile / work / 'user-file.txt').write_text('keep this user-created file')
    (profile / 'asr').mkdir()
    (profile / 'asr/lifecycle.json').write_text('[]')
    (profile / 'last_agent.json').write_text('{}')
    (profile / 'codebuddy-metadata-check.json').write_text('{}')
    (profile / 'settings.xml').write_text('unchanged-settings')
    (profile / 'components/model').mkdir(parents=True)
    (profile / 'components/model/network.onnx').write_bytes(b'unchanged-model')
    (profile / 'chat-history.json').write_text('unchanged-history')


def test_offline_migration_preserves_logins_models_history_and_fixes_native_paths(tmp_path):
    sample_profile(tmp_path)
    models = tmp_path / 'components/model/network.onnx'
    inode = models.stat().st_ino
    backup = tmp_path / 'diagnostics/layout-backup'
    result = migrate(tmp_path, backup)
    assert result['codex_paths_updated'] == 2
    assert data_path(tmp_path, 'codex-home/auth.json').read_text() == 'private-login-test'
    assert data_path(tmp_path, 'chat-history.json').read_text() == 'unchanged-history'
    assert not (tmp_path / 'codex-home').exists()
    assert not (tmp_path / 'codebuddy-work').exists()
    assert models.stat().st_ino == inode and models.read_bytes() == b'unchanged-model'
    assert (tmp_path / 'settings.xml').read_text() == 'unchanged-settings'
    assert (tmp_path / 'diagnostics/asr/lifecycle.json').is_file()
    assert (tmp_path / 'diagnostics/codebuddy/codebuddy-metadata-check.json').is_file()
    with sqlite3.connect(str(data_path(tmp_path, 'codex-home/state_5.sqlite'))) as connection:
        row = connection.execute('SELECT rollout_path,cwd FROM threads').fetchone()
        assert Path(row[0]).read_text() == 'private-history-test'
        assert row[1] == str(tmp_path / 'agents/codex/work')
    for home, work in (('codebuddy-home', 'codebuddy-work'), ('codebuddy-home-internal', 'codebuddy-work-internal')):
        project = data_path(tmp_path, home) / 'projects' / _project_key(data_path(tmp_path, work))
        assert (project / 'session.jsonl').read_text() == 'private-buddy-history-test'
        assert data_path(tmp_path, work + '/user-file.txt').read_text() == 'keep this user-created file'
    assert not plan(tmp_path)
    rollback(backup)
    assert (tmp_path / 'codex-home/auth.json').read_text() == 'private-login-test'
    with sqlite3.connect(str(tmp_path / 'codex-home/state_5.sqlite')) as connection:
        assert connection.execute('SELECT cwd FROM threads').fetchone()[0] == str(tmp_path / 'codex-work')
    assert (tmp_path / 'codebuddy-work/user-file.txt').is_file()


def test_migration_refuses_conflicts_before_touching_data(tmp_path):
    sample_profile(tmp_path)
    data_path(tmp_path, 'codex-home').mkdir()
    with pytest.raises(ValueError, match='迁移目标已存在'):
        migrate(tmp_path, tmp_path / 'backup')
    assert (tmp_path / 'codex-home/auth.json').is_file()
    assert not (tmp_path / 'backup').exists()


def test_failed_native_path_repair_rolls_back_directory_moves(tmp_path, monkeypatch):
    sample_profile(tmp_path)
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr('resources.lib.profile_migration._fix_codex', fail)
    backup = tmp_path / 'diagnostics/layout-backup'
    with pytest.raises(OSError):
        migrate(tmp_path, backup)
    assert (tmp_path / 'codex-home/auth.json').read_text() == 'private-login-test'
    assert (tmp_path / 'codebuddy-work/user-file.txt').is_file()
    assert json.loads((backup / 'journal.json').read_text())['phase'] == 'rolled_back'
