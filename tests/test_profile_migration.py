import fcntl
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from resources.lib import profile_migration as api
from resources.lib.data_paths import data_path, ensure_profile, LayoutError, LAYOUT_MARKER
from resources.lib.storage import write_json
from test_data_paths import sample_profile


def assert_migrated(profile):
    assert data_path(profile, 'codex-home/auth.json').read_text() == 'private-login-test'
    assert data_path(profile, 'chat-history.json').read_text() == 'unchanged-history'
    assert not (profile / 'codex-home').exists()
    assert (profile / 'settings.xml').read_text() == 'unchanged-settings'
    with sqlite3.connect(str(data_path(profile, 'codex-home/state_5.sqlite'))) as connection:
        rollout, cwd = connection.execute('SELECT rollout_path,cwd FROM threads').fetchone()
    assert Path(rollout).is_file() and cwd == str(data_path(profile, 'codex-work'))
    for home, work in (('codebuddy-home', 'codebuddy-work'), ('codebuddy-home-internal', 'codebuddy-work-internal')):
        project = data_path(profile, home) / 'projects' / api._project_key(data_path(profile, work))
        assert (project / 'session.jsonl').read_text() == 'private-buddy-history-test'


def test_first_start_migrates_once_and_keeps_models_and_credentials(tmp_path, monkeypatch):
    sample_profile(tmp_path)
    model = tmp_path / 'components/model/network.onnx'
    inode = model.stat().st_ino
    assert ensure_profile(tmp_path)
    assert_migrated(tmp_path)
    assert model.stat().st_ino == inode
    assert (tmp_path / LAYOUT_MARKER).stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(api, 'migrate', lambda *args: pytest.fail('Migration must run only once'))
    assert not ensure_profile(tmp_path)


def test_fresh_or_already_migrated_profile_is_not_modified_by_later_startup(tmp_path):
    home = data_path(tmp_path, 'codex-home')
    home.mkdir()
    (home / 'auth.json').write_text('existing-new-login')
    with data_path(tmp_path, 'cmcc-service.lock').open('a') as active_service:
        fcntl.flock(active_service, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not ensure_profile(tmp_path)
        assert not ensure_profile(tmp_path)
    assert (home / 'auth.json').read_text() == 'existing-new-login'
    assert not (tmp_path / 'diagnostics').exists()


def test_conflicting_credentials_are_neither_overwritten_nor_reported_as_migrated(tmp_path):
    sample_profile(tmp_path)
    home = data_path(tmp_path, 'codex-home')
    home.mkdir()
    (home / 'auth.json').write_text('different-new-login')
    with pytest.raises(LayoutError, match='目标已存在'):
        ensure_profile(tmp_path)
    assert (tmp_path / 'codex-home/auth.json').read_text() == 'private-login-test'
    assert (home / 'auth.json').read_text() == 'different-new-login'
    assert not (tmp_path / LAYOUT_MARKER).exists()


def test_active_legacy_dialog_blocks_migration_without_stopping_service(tmp_path):
    sample_profile(tmp_path)
    with (tmp_path / 'conversation.lock').open('a') as current:
        fcntl.flock(current, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LayoutError, match='先关闭当前语音助手弹窗'):
            ensure_profile(tmp_path)
    assert not (tmp_path / 'cmcc-stop.flag').exists()
    assert not (tmp_path / LAYOUT_MARKER).exists()
    assert (tmp_path / 'codex-home/auth.json').is_file()


def test_voice_entry_does_not_stop_an_old_service_and_leave_no_replacement(tmp_path):
    sample_profile(tmp_path)
    with (tmp_path / 'cmcc-service.lock').open('a') as current:
        fcntl.flock(current, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LayoutError, match='重新启动 Kodi'):
            ensure_profile(tmp_path)
    assert not (tmp_path / 'cmcc-stop.flag').exists()
    assert (tmp_path / 'codex-home/auth.json').is_file()


def test_service_entry_waits_for_old_owner_to_release_before_moving(tmp_path):
    sample_profile(tmp_path)
    stop = tmp_path / 'cmcc-stop.flag'
    observed = threading.Event()
    with (tmp_path / 'cmcc-service.lock').open('a') as old_service:
        fcntl.flock(old_service, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def finish_old_service():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if stop.exists():
                    observed.set()
                    fcntl.flock(old_service, fcntl.LOCK_UN)
                    return
                time.sleep(.01)
        worker = threading.Thread(target=finish_old_service)
        worker.start()
        try:
            assert api.ensure(tmp_path, timeout=2, stop_service=True)
        finally:
            worker.join(2)
    assert observed.is_set() and not stop.exists()
    assert not data_path(tmp_path, 'cmcc-stop.flag').exists()
    assert_migrated(tmp_path)


def test_old_service_timeout_preserves_data_and_cleans_own_stop_request(tmp_path):
    sample_profile(tmp_path)
    with (tmp_path / 'cmcc-service.lock').open('a') as current:
        fcntl.flock(current, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LayoutError, match='旧版语音助手仍在运行'):
            api.ensure(tmp_path, timeout=.01, stop_service=True)
    assert not (tmp_path / 'cmcc-stop.flag').exists()
    assert (tmp_path / 'codex-home/auth.json').is_file()
    assert not (tmp_path / LAYOUT_MARKER).exists()


def test_cancel_or_leftover_native_process_never_moves_credentials(tmp_path, monkeypatch):
    sample_profile(tmp_path)
    with pytest.raises(InterruptedError):
        api.ensure(tmp_path, cancelled=lambda: True)
    monkeypatch.setattr(api, '_native_busy', lambda *args: True)
    with pytest.raises(LayoutError, match='旧版智能体仍在使用'):
        ensure_profile(tmp_path)
    assert (tmp_path / 'codex-home/auth.json').read_text() == 'private-login-test'
    assert not (tmp_path / LAYOUT_MARKER).exists()


def test_interrupted_move_and_sqlite_repair_are_recovered_before_retry(tmp_path):
    sample_profile(tmp_path)
    backup = tmp_path / 'diagnostics/interrupted-upgrade'
    backup.mkdir(parents=True)
    old, new = tmp_path / 'codex-home', data_path(tmp_path, 'codex-home')
    old.rename(new)
    api._fix_codex(tmp_path, backup)
    write_json(backup / 'journal.json', {'phase': 'moving', 'moves': [[str(old), str(new)]]})
    write_json(tmp_path / api.UPGRADE_STATE, {'backup': str(backup.relative_to(tmp_path))})
    assert ensure_profile(tmp_path)
    assert_migrated(tmp_path)
    assert json.loads((backup / 'journal.json').read_text())['phase'] == 'rolled_back'
    assert not (tmp_path / api.UPGRADE_STATE).exists()


def test_completed_transaction_without_marker_is_not_repeated(tmp_path, monkeypatch):
    sample_profile(tmp_path)
    backup = tmp_path / 'diagnostics/complete-upgrade'
    api.migrate(tmp_path, backup)
    write_json(tmp_path / api.UPGRADE_STATE, {'backup': str(backup.relative_to(tmp_path))})
    monkeypatch.setattr(api, 'migrate', lambda *args: pytest.fail('Completed transaction must not repeat'))
    assert ensure_profile(tmp_path)
    assert_migrated(tmp_path)
    assert (tmp_path / LAYOUT_MARKER).exists()


def test_recovery_refuses_paths_outside_plugin_profile(tmp_path):
    sample_profile(tmp_path)
    backup = tmp_path / 'diagnostics/bad-upgrade'
    write_json(backup / 'journal.json', {'phase': 'moving', 'moves': [
        [str(tmp_path / 'codex-home'), str(tmp_path.parent / 'outside-home')]]})
    write_json(tmp_path / api.UPGRADE_STATE, {'backup': str(backup.relative_to(tmp_path))})
    with pytest.raises(LayoutError, match='超出插件数据目录'):
        ensure_profile(tmp_path)
    assert (tmp_path / 'codex-home/auth.json').is_file()


def test_invalid_upgrade_state_is_not_ignored(tmp_path):
    sample_profile(tmp_path)
    path = tmp_path / api.UPGRADE_STATE
    path.parent.mkdir(parents=True)
    path.write_text('{incomplete')
    with pytest.raises(LayoutError, match='迁移状态文件无效'):
        ensure_profile(tmp_path)
    assert (tmp_path / 'codex-home/auth.json').is_file()
    assert not (tmp_path / LAYOUT_MARKER).exists()


def test_parallel_first_start_runs_only_one_transaction(tmp_path, monkeypatch):
    sample_profile(tmp_path)
    original = api.migrate
    calls = []
    began, release = threading.Event(), threading.Event()
    def slow(*args):
        calls.append(True)
        began.set()
        assert release.wait(2)
        return original(*args)
    monkeypatch.setattr(api, 'migrate', slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ensure_profile, tmp_path)
        assert began.wait(2)
        second = pool.submit(ensure_profile, tmp_path)
        release.set()
        assert sorted([first.result(3), second.result(3)]) == [False, True]
    assert len(calls) == 1
    assert_migrated(tmp_path)


def test_regular_startup_does_not_import_backup_or_sqlite_modules(tmp_path):
    marker = tmp_path / LAYOUT_MARKER
    marker.parent.mkdir()
    marker.write_text('{}')
    code = '''import sys
from resources.lib.data_paths import ensure_profile
assert ensure_profile(sys.argv[1]) is False
assert not any(name in sys.modules for name in ('sqlite3','tempfile','resources.lib.profile_migration'))
'''
    subprocess.run([sys.executable, '-c', code, str(tmp_path)], check=True,
                   cwd=Path(__file__).resolve().parents[1])


def test_failed_upgrade_is_visible_and_does_not_enter_normal_startup(tmp_path, monkeypatch):
    from resources.lib import profile_upgrade
    notices, logs = [], []
    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace(log=lambda *args: logs.append(args), LOGERROR=3))
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace(Dialog=lambda: SimpleNamespace(ok=lambda *args: notices.append(args))))
    def fail(*args, **kwargs):
        raise LayoutError('新旧目录冲突，未覆盖已有数据。')
    monkeypatch.setattr(profile_upgrade, 'ensure_profile', fail)
    assert not profile_upgrade.prepare_profile(tmp_path, cancelled=lambda: False)
    assert notices == [('语音助手 · 升级数据迁移', '新旧目录冲突，未覆盖已有数据。')]
    assert logs and not (tmp_path / 'agents').exists()


def test_service_checks_upgrade_before_opening_runtime_or_input_devices(tmp_path, monkeypatch):
    import importlib
    from resources.lib import profile_upgrade
    calls = []
    addon = SimpleNamespace(getAddonInfo=lambda key: str(tmp_path))
    class Monitor:
        def abortRequested(self):
            return False
    for name, value in {
        'xbmc': SimpleNamespace(Monitor=Monitor),
        'xbmcaddon': SimpleNamespace(Addon=lambda *args: addon),
        'xbmcgui': SimpleNamespace(Window=lambda *args: SimpleNamespace()),
        'xbmcvfs': SimpleNamespace(translatePath=lambda path: path),
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    def blocked(profile, cancelled, service=False):
        calls.append((profile, service))
        return False
    monkeypatch.setattr(profile_upgrade, 'prepare_profile', blocked)
    monkeypatch.delitem(sys.modules, 'resources.lib.inputs.runtime', raising=False)
    try:
        module = importlib.import_module('resources.lib.inputs.runtime')
        module.main()
        assert calls == [(tmp_path, True)]
        assert not (tmp_path / 'runtime').exists()
    finally:
        sys.modules.pop('resources.lib.inputs.runtime', None)
