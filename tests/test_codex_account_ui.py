import importlib
import sys
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def ui(monkeypatch):
    calls, windows = [], []
    main = threading.get_ident()

    class Control:
        def record(self, kind, value):
            calls.append((kind, value, threading.get_ident()))

        def setImage(self, value):
            self.record('image', value)

        def setText(self, value):
            self.record('text', value)

        def setLabel(self, value):
            self.record('label', value)

    class Window:
        def __init__(self, *args, **kwargs):
            self.controls = {}
            windows.append(self)

        def getControl(self, identifier):
            return self.controls.setdefault(identifier, Control())

        def setFocusId(self, value):
            calls.append(('focus', value, threading.get_ident()))

        def show(self):
            self.onInit()

        def close(self):
            calls.append(('close', '', threading.get_ident()))

    class Dialog:
        def ok(self, title, text):
            calls.append(('dialog', (title, text), threading.get_ident()))

    class Monitor:
        def abortRequested(self):
            return False

        def waitForAbort(self, seconds):
            time.sleep(min(seconds, .01))
            return False

    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace(Monitor=Monitor))
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace(WindowXMLDialog=Window, Dialog=Dialog))
    monkeypatch.setitem(sys.modules, 'xbmcvfs', SimpleNamespace(translatePath=lambda path: path))
    name = 'resources.lib.codex_account_ui'
    monkeypatch.delitem(sys.modules, name, raising=False)
    module = importlib.import_module(name)
    yield module, calls, windows, main
    sys.modules.pop(name, None)


def addon(path):
    return SimpleNamespace(getAddonInfo=lambda key: str(path), getSetting=lambda key: '')


def test_completed_login_updates_only_main_thread_and_removes_qr_file(ui, tmp_path):
    module, calls, windows, main = ui

    class Account:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def login(self, show):
            show({'url': 'https://auth.openai.com/codex/device', 'code': 'TEST-1234'})
            time.sleep(.05)
            return {'logged_in': True, 'email': 'test@example.com', 'plan': 'plus'}

    result = module.login(addon(tmp_path), tmp_path, Account)
    assert result == {'state': 'done', 'logged_in': True, 'plan': 'plus'}
    assert any(kind == 'label' and text == 'TEST-1234' for kind, text, thread in calls)
    assert any(kind == 'dialog' and text[0] == '登录成功' for kind, text, thread in calls)
    assert all(thread == main for kind, text, thread in calls)
    assert not list(tmp_path.glob('codex-login-*.png'))


def test_return_during_pending_login_cancels_worker_and_removes_qr(ui, tmp_path):
    module, calls, windows, main = ui
    closed = threading.Event()

    class Account:
        def __init__(self, root, profile, cancelled, **kwargs):
            self.cancelled = cancelled

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.set()

        def login(self, show):
            show({'url': 'https://auth.openai.com/codex/device', 'code': 'TEST-1234'})
            # Simulate the Return action after the QR has had time to display.
            while not self.cancelled():
                if any(kind == 'image' for kind, text, thread in calls):
                    windows[0].stopped.set()
                time.sleep(.01)
            raise InterruptedError('cancelled')

    began = time.monotonic()
    result = module.login(addon(tmp_path), tmp_path, Account)
    assert result == {'state': 'cancelled'} and closed.is_set()
    assert time.monotonic() - began < 1
    assert not list(tmp_path.glob('codex-login-*.png'))
    assert not any(kind == 'dialog' and text[0] == '登录成功' for kind, text, thread in calls)


def test_auth_error_messages_never_echo_untrusted_tokens(ui):
    module, calls, windows, main = ui
    assert 'private-token' not in module.login_error(RuntimeError('private-token'))
    assert 'private-token' not in module.login_error(RuntimeError('403 private-token'))
