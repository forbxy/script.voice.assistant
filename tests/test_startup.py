"""Startup dependencies and the lifecycle of the early recording window."""
from resources.lib.data_paths import data_path
import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from resources.lib.settings import Settings, get_setting


def test_recording_settings_keep_validation_without_reading_agent_settings():
    read = []
    stored = {'v2_voice_input': 'b25', 'v2_recognition_engine': 'onnx',
              'v2_threshold': '-1', 'v2_max_seconds': '500', 'v2_silence_ms': '750'}
    def setting(name):
        read.append(name)
        return stored.get(name, '')
    addon = SimpleNamespace(getSetting=setting)
    assert get_setting(addon, 'voice_input') == 'b25'
    assert read == ['v2_voice_input']
    read.clear()
    values = Settings(addon).capture_values()
    assert values['threshold'] == 200 and values['max_seconds'] == 30
    assert values['silence_ms'] == 750
    assert not any(name.startswith(('v2_agent_', 'v2_codex_proxy')) for name in read)
    stored['v2_recognition_engine'] = 'codex'
    stored.update(v2_codex_proxy='http://localhost:7890', v2_codex_proxy_username='用户',
                  v2_codex_proxy_password='p@ss')
    values = Settings(addon).capture_values()
    assert values['codex_proxy_password'] == 'p@ss'


def test_fresh_startup_does_not_load_http_or_native_decoders():
    # Fresh interpreter: earlier tests may have already imported these libraries.
    code = """
import sys
from types import SimpleNamespace
for name in ('xbmc', 'xbmcgui', 'xbmcaddon', 'xbmcvfs'):
    sys.modules[name] = SimpleNamespace()
from resources.lib import application
from resources.lib.settings import Settings
values = Settings(SimpleNamespace(getSetting=lambda name: '')).values()
from resources.lib.inputs import pcm
for name in ('dataclasses', 'inspect', 'urllib.request', 'ssl', 'tempfile', 'ctypes',
             'subprocess', 'resources.lib.intelligence.sessions', 'resources.lib.cmcc_audio',
             'resources.lib.ur02_audio', 'resources.lib.b25_audio'):
    if name in sys.modules:
        raise RuntimeError('Unexpected startup dependency: ' + name)
"""
    subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1], check=True)


@pytest.mark.parametrize('failure,cancelled', [(False, False), (True, False), (False, True)])
def test_window_ready_before_socket_claim_and_closes_on_failure(tmp_path, monkeypatch, failure, cancelled):
    events = []
    monkeypatch.setattr('resources.lib.intelligence.ui_context.capture',
                        lambda rpc: events.append('context') or {'selected': None})
    addon = SimpleNamespace(getSetting=lambda key: {'v2_voice_input': 'b25',
        'v2_recognition_engine': 'onnx'}.get(key, ''), getAddonInfo=lambda key: str(tmp_path))
    home = SimpleNamespace(getProperty=lambda key: 'a' * 32 if key.endswith('B25Session') else '')
    for name, value in {
        'xbmc': SimpleNamespace(Monitor=lambda: SimpleNamespace(abortRequested=lambda: False)),
        'xbmcgui': SimpleNamespace(Window=lambda *args: home),
        'xbmcaddon': SimpleNamespace(), 'xbmcvfs': SimpleNamespace(translatePath=lambda path: path),
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    class Progress:
        def __init__(self, root):
            self.window = SimpleNamespace(ready=SimpleNamespace(is_set=lambda: True))
        def create(self, *args): events.append('window')
        def iscanceled(self): return cancelled
        def close(self): events.append('close')
    monkeypatch.setitem(sys.modules, 'resources.lib.recording_ui', SimpleNamespace(RecordingProgress=Progress))
    class Client:
        def __init__(self, profile, token, socket_name):
            assert events == ['context', 'window'] and socket_name == 'b25.sock'
            events.append('claim')
            if failure: raise RuntimeError('connection failed')
        def __enter__(self): return SimpleNamespace(microphone='fixture')
        def __exit__(self, *args): events.append('release')
    monkeypatch.setattr('resources.lib.inputs.pcm.Client', Client)
    monkeypatch.delitem(sys.modules, 'resources.lib.application', raising=False)
    app = importlib.import_module('resources.lib.application')
    monkeypatch.setattr(app, 'capture', lambda *args, **kwargs: events.append('capture') and None)
    try:
        if failure:
            with pytest.raises(RuntimeError, match='connection failed'):
                app._conversation(addon, tmp_path)
            assert events == ['context', 'window', 'claim', 'close']
        else:
            app._conversation(addon, tmp_path)
            assert events == (['context', 'window', 'claim', 'release', 'close'] if cancelled else
                              ['context', 'window', 'claim', 'capture', 'release', 'close'])
        assert not (data_path(tmp_path, 'conversation.json')).exists()
    finally:
        sys.modules.pop('resources.lib.application', None)
