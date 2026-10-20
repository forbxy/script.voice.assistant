import importlib
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def ui(monkeypatch):
    class Control:
        def setLabel(self, value): self.label = value
        def setText(self, value): self.text = value
    class Window:
        def __init__(self, *args): self.controls = {}
        def getControl(self, identifier): return self.controls.setdefault(identifier, Control())
        def setFocusId(self, identifier): self.focus = identifier
        def show(self): self.onInit()
        def close(self): self.closed = True
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace(WindowXMLDialog=Window))
    name = 'resources.lib.recording_ui'
    monkeypatch.delitem(sys.modules, name, raising=False)
    module = importlib.import_module(name)
    monkeypatch.setattr(module, 'current_codes', lambda: {'61588'})
    yield module
    sys.modules.pop(name, None)


def test_ok_finishes_but_does_not_cancel_or_move_focus_to_cancel(ui):
    progress = ui.RecordingProgress('')
    progress.create('语音', '正在听')
    assert progress.window.focus == 400
    progress.window.onClick(400)
    assert progress.finish_requested.is_set() and not progress.iscanceled()
    progress.set_recording(False)
    progress.update(0, '等待结果')
    progress.window.onClick(400)
    assert not progress.iscanceled() and progress.window.focus == 400
    progress.window.onAction(SimpleNamespace(getId=lambda: 92))
    assert progress.iscanceled()


def test_automatic_end_and_cancel_do_not_request_manual_finish(ui):
    progress = ui.RecordingProgress('')
    progress.create('语音', '正在听')
    progress.set_recording(False)
    progress.window.onClick(400)
    assert not progress.finish_requested.is_set()
    progress.window.onClick(401)
    assert progress.iscanceled() and not progress.finish_requested.is_set()


def test_voice_key_with_window_back_mapping_does_not_cancel_recording(ui):
    progress = ui.RecordingProgress('')
    progress.create('语音', '正在听')
    progress.window.onAction(SimpleNamespace(getId=lambda: 92, getButtonCode=lambda: 61588))
    assert not progress.iscanceled()
    progress.window.onAction(SimpleNamespace(getId=lambda: 92, getButtonCode=lambda: 61467))
    assert progress.iscanceled()


def test_preopened_window_preserves_early_ok_and_closes_only_once(ui):
    progress = ui.RecordingProgress('')
    calls = []
    original = progress.window.show
    def show():
        calls.append('show')
        original()
    progress.window.show = show
    progress.window.close = lambda: calls.append('close')
    progress.create('语音', '准备录音')
    progress.window.onClick(400)
    progress.create('语音', '正在听')
    assert progress.finish_requested.is_set()
    progress.close()
    progress.close()
    assert calls == ['show', 'close']
