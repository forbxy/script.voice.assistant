"""Recording must release its state before the response opens a speech session."""
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('previous', ['idle', 'thinking', 'conversation'])
@pytest.mark.parametrize('outcome', ['done', 'cancelled', 'error', 'no_text'])
def test_capture_releases_recording_state(previous, outcome, tmp_path, monkeypatch):
    properties = {'VoiceAssistant.State': previous}
    notifications = []
    home = SimpleNamespace(
        getProperty=lambda key: properties.get(key, ''),
        setProperty=lambda key, value: properties.update({key: value}),
        clearProperty=lambda key: properties.pop(key, None))
    monitor = SimpleNamespace(abortRequested=lambda: False, waitForAbort=lambda seconds: None)
    for name, module in {
        'xbmc': SimpleNamespace(Monitor=lambda: monitor),
        'xbmcgui': SimpleNamespace(Window=lambda *args: home,
            Dialog=lambda: SimpleNamespace(notification=lambda *args, **kwargs: notifications.append(kwargs))),
        'xbmcaddon': SimpleNamespace(),
        'xbmcvfs': SimpleNamespace(translatePath=lambda path: path),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    class Stream:
        def __init__(self, *args, **kwargs):
            self.stop = threading.Event()
            self.done = False
        def start(self): pass
        def cancel(self): self.done = True
        def join(self): pass
        def finish_capture(self): pass
        def snapshot(self):
            self.done = True
            return {'phase': 'recognizing', 'text': '' if outcome == 'no_text' else '测试',
                    'speech_detected': True, 'level': 0,
                    'error': 'test failure' if outcome == 'error' else ''}

    monkeypatch.setitem(sys.modules, 'resources.lib.streaming', SimpleNamespace(StreamingSession=Stream))
    monkeypatch.setitem(sys.modules, 'resources.lib.asr.factory', SimpleNamespace(
        gate=lambda *args: SimpleNamespace(), recognizer=lambda *args: None))
    monkeypatch.setitem(sys.modules, 'resources.lib.recording_ui', SimpleNamespace(RecordingProgress=None))
    path = Path(__file__).resolve().parents[1] / 'resources/lib/application.py'
    spec = importlib.util.spec_from_file_location('resources.lib.capture_test', path)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    observed = []
    progress = SimpleNamespace(create=lambda *args: None, set_recording=lambda *args: None,
        finish_requested=threading.Event(), update=lambda *args: observed.append(properties['VoiceAssistant.State']),
        iscanceled=lambda: outcome == 'cancelled', close=lambda: observed.append(properties['VoiceAssistant.State']))
    addon = SimpleNamespace(getAddonInfo=lambda key: str(tmp_path))
    values = {'voice_input': 'usb', 'recognition_engine': 'codex', 'alsa_device': 'default'}
    if outcome == 'error':
        with pytest.raises(RuntimeError, match='test failure'):
            app.capture(addon, tmp_path, values=values, progress=progress)
    else:
        result = app.capture(addon, tmp_path, values=values, progress=progress)
        assert result == ('测试' if outcome == 'done' else None)
    assert observed[-1] == previous
    assert home.getProperty('VoiceAssistant.State') == previous
    if outcome != 'cancelled':
        assert observed[0] == 'recognizing'
    if outcome == 'no_text':
        assert notifications == [{'icon': app.NOTIFICATION_ICON, 'sound': False}]
