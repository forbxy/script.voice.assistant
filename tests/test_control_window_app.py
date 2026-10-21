"""The application never constructs a conversation for a successful local control."""
import importlib.util
import json
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('mode', ['success', 'tool_error', 'exception', 'cancelled'])
@pytest.mark.parametrize('backend', ['off', 'codex', 'codebuddy'])
@pytest.mark.parametrize('phrase', ['暂停', '继续播放', '继续看电影', '继续看电视剧'])
def test_small_control_runs_once_and_only_errors_open_the_conversation(tmp_path, monkeypatch, mode, backend, phrase):
    properties, executed, large, displayed = {}, [], [], []
    home = SimpleNamespace(getProperty=lambda key: properties.get(key, ''),
        setProperty=lambda key, value: properties.update({key: value}), clearProperty=lambda key: properties.pop(key, None))
    monitor = SimpleNamespace(abortRequested=lambda: False, waitForAbort=lambda seconds: False)
    for name, value in {
        'xbmc': SimpleNamespace(Monitor=lambda: monitor, executebuiltin=lambda *args: None),
        'xbmcgui': SimpleNamespace(Window=lambda *args: home), 'xbmcaddon': SimpleNamespace(),
        'xbmcvfs': SimpleNamespace(translatePath=lambda value: value),
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    monkeypatch.setattr('resources.lib.intelligence.ui_context.capture', lambda *args: {'selected': None})
    class Window:
        def __init__(self, *args):
            large.append(self)
            self.ready, self.cancelled, self.task_stop = threading.Event(), threading.Event(), threading.Event()
            self.ready.set()
            self.events = queue.Queue()
            self.backgrounded = False
        def show(self):pass
        def restore(self, rows):pass
        def append_message(self, *args):pass
        def present(self, value):
            displayed.append(value)
            self.cancelled.set()
        def close(self):pass
    monkeypatch.setitem(sys.modules, 'resources.lib.ui.conversation',
        SimpleNamespace(ConversationWindow=Window, ConversationProgress=lambda window: None))
    monkeypatch.setitem(sys.modules, 'resources.lib.ui.live_input',
        SimpleNamespace(LiveInput=lambda *args, **kwargs: SimpleNamespace(poll=lambda *args: None)))
    monkeypatch.setitem(sys.modules, 'resources.lib.ui.background',
        SimpleNamespace(Background=lambda *args: SimpleNamespace(close=lambda: None, complete=lambda *args: None)))
    monkeypatch.setitem(sys.modules, 'resources.lib.speech.client',
        SimpleNamespace(Speech=lambda *args: SimpleNamespace(stop=lambda: None, finish=lambda *args: None)))
    def run_turn(*args, **kwargs):
        executed.append(args[3])
        assert kwargs.get('progress') is None  # TurnController uses RecordingProgress.
        if mode == 'exception':raise RuntimeError('播放器未响应')
        if mode == 'cancelled':return None
        return dict(message='已暂停播放。' if mode == 'success' else '未能执行：播放器未响应',
                    interaction='close' if mode == 'success' else 'continue', choices=[])
    monkeypatch.setitem(sys.modules, 'resources.lib.ui.agent_turn', SimpleNamespace(
        run_turn=run_turn, playback_state=lambda: (False, ''), close_for_playback=lambda *args: False))
    path = Path(__file__).resolve().parents[1] / 'resources/lib/application.py'
    spec = importlib.util.spec_from_file_location('resources.lib.application_control_test', path)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    values = dict(agent_backend=backend, voice_input='usb', session_policy='daily',
                  permission_level='media', approval_strategy='request')
    monkeypatch.setattr(app, 'Settings', lambda addon: SimpleNamespace(get=values.get, values=lambda: dict(values)))
    addon = SimpleNamespace(getAddonInfo=lambda key: str(tmp_path))
    app._conversation(addon, tmp_path, initial_text=phrase)
    assert executed == [phrase]
    assert len(large) == len(displayed) == (1 if mode in ('tool_error', 'exception') else 0)
    assert properties['VoiceAssistant.State'] == 'idle'
    history = tmp_path / 'history/chat-history.json'
    if mode != 'cancelled':
        rows = json.loads(history.read_text())['rounds']
        assert len(rows) == 1 and rows[0]['user'] == phrase and rows[0]['state'] == 'done'
