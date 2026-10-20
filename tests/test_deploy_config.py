import json
import sys
from types import SimpleNamespace

from dev.deploy import configuration_script


def test_deploy_saves_forwarded_settings_and_verifies_selected_codex(tmp_path, monkeypatch):
    saved, pending, calls = {'agent_backend': 'builtin'}, {}, []
    active = [True]

    def set_setting(name, value):
        (pending if active[0] else saved)[name] = value

    def builtin(action, wait):
        calls.append(action)
        saved.update(pending)
        active[0] = False

    addon = SimpleNamespace(setSetting=set_setting, getSetting=lambda name: saved.get(name, ''))
    monkeypatch.setitem(sys.modules, 'xbmcaddon', SimpleNamespace(Addon=lambda name: addon))
    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace(
        Monitor=lambda: SimpleNamespace(waitForAbort=lambda seconds: False),
        getCondVisibility=lambda name: active[0], executebuiltin=builtin))
    status = tmp_path / 'status.json'
    exec(configuration_script({'agent_backend': 'codex'}, str(status)), {})
    assert json.loads(status.read_text()) == {'ok': True, 'settings': {'agent_backend': 'codex'}}
    assert calls == ['SendClick(10140,28)']


def test_deploy_does_not_claim_success_when_setting_update_fails(tmp_path, monkeypatch):
    addon = SimpleNamespace(setSetting=lambda *args: None, getSetting=lambda name: 'builtin')
    monkeypatch.setitem(sys.modules, 'xbmcaddon', SimpleNamespace(Addon=lambda name: addon))
    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace(
        Monitor=lambda: None, getCondVisibility=lambda name: False))
    status = tmp_path / 'status.json'
    exec(configuration_script({'agent_backend': 'codex'}, str(status)), {})
    assert json.loads(status.read_text())['ok'] is False
