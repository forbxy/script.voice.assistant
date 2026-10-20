import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def turn_ui(monkeypatch):
    for name in ('xbmc', 'xbmcgui', 'xbmcaddon'):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'resources.lib.recording_ui', SimpleNamespace(RecordingProgress=None))
    path = Path(__file__).resolve().parents[1] / 'resources/lib/ui/agent_turn.py'
    spec = importlib.util.spec_from_file_location('resources.lib.ui.turn_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_this_turn_actual_fullscreen_video_closes(turn_ui):
    tools = SimpleNamespace(playback_started=False, fullscreen_requested=False)
    check = turn_ui.close_for_playback
    assert not check((False, ''), (False, ''), tools)
    assert not check((True, 'movie'), (True, 'movie'), tools)
    assert check((False, ''), (True, 'movie'), tools)
    assert check((True, 'old'), (True, 'new'), tools)
    tools.playback_started = True
    assert not check((False, ''), (False, 'movie'), tools)
    assert check((True, 'movie'), (True, 'movie'), tools)
    tools.playback_started = False
    tools.fullscreen_requested = True
    assert not check((False, ''), (False, ''), tools)
    assert check((True, 'movie'), (True, 'movie'), tools)


@pytest.mark.parametrize('backend', ['off', 'codex', 'codebuddy'])
def test_local_command_never_constructs_model_even_when_agent_is_disabled(turn_ui, tmp_path, monkeypatch, backend):
    import json
    from resources.lib.intelligence.sessions import Sessions
    class Progress:
        def create(self, *args): pass
        def set_recording(self, *args): pass
        def update(self, *args): pass
        def iscanceled(self): return False
        def close(self): pass
        def activity(self, message): pass
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        if method == 'VideoLibrary.GetMovies':
            return {'movies': [{'movieid': 162, 'title': '黑客帝国', 'year': 1999}], 'limits': {'total': 1}}
        if method == 'Player.Open': return 'OK'
        raise AssertionError(method)
    monkeypatch.setattr(turn_ui, 'create_agent', lambda *args, **kwargs: pytest.fail('Model constructed for local command'))
    monkeypatch.setattr(turn_ui, 'playback_state', lambda: (False, ''))
    monitor = SimpleNamespace(abortRequested=lambda: False, waitForAbort=lambda n: None)
    values = {'agent_backend': backend, 'permission_level': 'media', 'approval_strategy': 'request'}
    session = Sessions(tmp_path)
    result = turn_ui.run_turn('', tmp_path, values, '播放黑客帝国', session, rpc, lambda *args: None,
                             SimpleNamespace(setProperty=lambda *args: None), monitor, Progress())
    assert result['message'] == '正在打开《黑客帝国》。' and result['interaction'] == 'continue'
    assert len(calls) == 2 and calls[-1][0] == 'Player.Open'
    assert session.thread_id is None and session.last_local_action['request'] == '播放黑客帝国'
    saved = json.loads((tmp_path / 'diagnostics/last_agent.json').read_text())
    assert saved['agent_backend'] == 'local' and saved['model_invocations'] == 0


def test_fast_turn_delivers_all_work_on_ui_thread_and_maintenance_does_not_close(turn_ui, tmp_path, monkeypatch):
    thread = threading.get_ident()
    work = []
    context = {'source': 'before_assistant', 'selected': None}
    contexts = []
    class Progress:
        def create(self, *args): pass
        def set_recording(self, *args): pass
        def update(self, *args): pass
        def iscanceled(self): return False
        def close(self): pass
        def activity(self, value):
            assert threading.get_ident() == thread
            work.append(value)
    def run(text, cancelled, progress):
        for message in ('检查系统', '读取文件', '检查完成'):
            progress(message)
        return {'reply': '{"message":"完成","interaction":"close","choices":[]}', 'tools': []}
    tools = SimpleNamespace(media={}, acted=False, deferred=True, commit_deferred=lambda abort: True)
    def create(*args, **kwargs):
        contexts.append(kwargs.get('ui_context'))
        return SimpleNamespace(run=run), tools
    monkeypatch.setattr(turn_ui, 'create_agent', create)
    monkeypatch.setattr(turn_ui, 'playback_state', lambda: (False, ''))
    monitor = SimpleNamespace(abortRequested=lambda: False, waitForAbort=lambda n: None)
    sessions = SimpleNamespace(completed=lambda media: None)
    home = SimpleNamespace(setProperty=lambda *a: None)
    result = turn_ui.run_turn('', tmp_path, {'permission_level': 'settings', 'approval_strategy': 'request'},
                              '测试', sessions, None, None, home, monitor, Progress(), ui_context=context)
    assert result['interaction'] == 'continue'
    assert work == ['检查系统', '读取文件', '检查完成']
    assert contexts == [context]


@pytest.mark.parametrize('cancel_before_open', [False, True])
def test_open_waits_for_ui_message_and_a_refresh_interval_before_blocking_rpc(
        turn_ui, tmp_path, monkeypatch, cancel_before_open):
    import json
    import time
    from resources.lib.intelligence.sessions import Sessions
    ui_thread = threading.get_ident()
    events = []
    stopped = threading.Event()

    class Progress:
        def create(self, *args): pass
        def set_recording(self, *args): pass
        def close(self): pass
        def iscanceled(self): return stopped.is_set()
        def activity(self, message):
            assert threading.get_ident() == ui_thread
            if message == '正在打开影片…':events.append('activity')
        def update(self, percent, message):
            assert threading.get_ident() == ui_thread
            if message.startswith('正在打开影片…'):events.append('status')

    def wait(seconds):
        if events and events[-1] == 'status':
            events.append('refresh_interval')
            if cancel_before_open:stopped.set()
        time.sleep(.001)
        return False

    def rpc(method, params):
        if method == 'VideoLibrary.GetMovies':
            return {'movies': [{'movieid': 42, 'title': '测试影片'}], 'limits': {'total': 1}}
        assert method == 'Player.Open'
        assert not cancel_before_open
        assert events[:3] == ['activity', 'status', 'refresh_interval']
        events.append('open')
        time.sleep(.02)  # Slow native preparation starts after UI acknowledgement.
        return 'OK'

    monkeypatch.setattr(turn_ui, 'playback_state', lambda: (False, ''))
    monitor = SimpleNamespace(abortRequested=lambda: False, waitForAbort=wait)
    result = turn_ui.run_turn('', tmp_path,
        {'agent_backend': 'off', 'permission_level': 'media', 'approval_strategy': 'request'},
        '播放测试影片', Sessions(tmp_path), rpc, lambda *args: None,
        SimpleNamespace(setProperty=lambda *args: None), monitor, Progress())
    if cancel_before_open:
        assert result is None and 'open' not in events
    else:
        assert result['message'] == '正在打开《测试影片》。'
        timing = json.loads((tmp_path / 'diagnostics/last_agent.json').read_text())['playback_timing'][0]
        assert timing['requested_seconds'] <= timing['ui_ready_seconds'] <= timing['dispatch_seconds']
        assert timing['rpc_seconds'] >= .02


def test_input_finishing_after_turn_is_processed_before_maintenance(turn_ui, tmp_path, monkeypatch):
    import time
    returned = threading.Event()
    calls, displayed, commits = [], [], []
    class Progress:
        def create(self, *args): pass
        def set_recording(self, *args): pass
        def update(self, *args): pass
        def iscanceled(self): return False
        def close(self): pass
        def activity(self, message): pass
    def run(text, cancelled, progress, steering):
        calls.append(text)
        returned.set()
        return {'reply': '{"message":"完成' + text + '","choices":[]}', 'tools': []}
    def poll(drain):
        if len(calls) == 1 and not displayed:
            assert returned.wait(2)
            drain()
            return '补充'
    tools = SimpleNamespace(media={}, acted=False, commit_deferred=lambda abort: commits.append(len(calls)))
    monkeypatch.setattr(turn_ui, 'create_agent', lambda *a, **k: (SimpleNamespace(run=run), tools))
    monkeypatch.setattr(turn_ui, 'playback_state', lambda: (False, ''))
    result = turn_ui.run_turn('', tmp_path, {'permission_level':'full','approval_strategy':'full_access'},
        '原任务', SimpleNamespace(completed=lambda media: None), None, None,
        SimpleNamespace(setProperty=lambda *args: None),
        SimpleNamespace(abortRequested=lambda: False, waitForAbort=lambda n: time.sleep(.001)),
        Progress(), poll_input=poll, on_message=lambda *args: displayed.append(args))
    assert calls == ['原任务', '补充']
    assert displayed == [('user','补充'), ('assistant','完成原任务')]
    assert result['message'] == '完成补充' and commits == [2]


def test_stop_task_cancels_worker_without_maintenance_or_closing_conversation(turn_ui, tmp_path, monkeypatch):
    import time
    active, stopped, exited = threading.Event(), threading.Event(), threading.Event()
    class Progress:
        def create(self, *args): pass
        def set_recording(self, *args): pass
        def update(self, *args): pass
        def close(self): pass
        def activity(self, value): pass
        def iscanceled(self): return stopped.is_set()
        def task_stopped(self): return stopped.is_set()
    def run(text, cancelled, progress, steering):
        active.set()
        while not cancelled():
            time.sleep(.001)
        exited.set()
        raise InterruptedError()
    def poll(drain):
        assert active.wait(2)
        stopped.set()
    def commit(abort):
        raise AssertionError('Cancelled task must not execute maintenance')
    tools = SimpleNamespace(media={}, acted=False, commit_deferred=commit)
    monkeypatch.setattr(turn_ui, 'create_agent', lambda *a, **k: (SimpleNamespace(run=run), tools))
    monkeypatch.setattr(turn_ui, 'playback_state', lambda: (False, ''))
    result = turn_ui.run_turn('', tmp_path, {'permission_level':'full','approval_strategy':'full_access'},
        '执行', SimpleNamespace(completed=lambda media: None), None, None, SimpleNamespace(setProperty=lambda *args: None),
        SimpleNamespace(abortRequested=lambda: False, waitForAbort=lambda n: time.sleep(.001)),
        Progress(), poll_input=poll)
    assert result['interaction'] == 'continue' and result['message'].startswith('任务已停止')
    assert exited.is_set()


@pytest.mark.parametrize('mode', ['handoff', 'supplement', 'cancel', 'failed_dispatch'])
def test_external_playback_releases_modal_before_slow_model_reply_and_preserves_input(
        turn_ui, tmp_path, monkeypatch, mode):
    import time
    from resources.lib.ui import navigation
    ui_thread = threading.get_ident()
    ready, stopped, tail_ready = threading.Event(), threading.Event(), threading.Event()
    events, calls, displayed = [], [], []
    window = SimpleNamespace(cancelled=threading.Event(), backgrounded=False, clear_question=lambda: None)
    dialog = [12000]
    def hide():
        assert threading.get_ident() == ui_thread
        events.append('hide')
        window.backgrounded = True
    def foreground():
        events.append('restore')
        window.backgrounded = False
    def wait(seconds):
        if window.backgrounded:
            dialog[0] = 9999
        # The native model cannot finish until several UI ticks after handoff.
        if mode == 'handoff' and 'dispatch' in events:
            events.append('tail_tick')
            if events.count('tail_tick') >= 3:
                tail_ready.set()
        time.sleep(.001)
        return False
    def builtin(command):
        assert threading.get_ident() == ui_thread
        assert window.backgrounded and dialog[0] == 9999
        assert command == 'test-playback'
        assert not tail_ready.is_set()
        events.append('dispatch')
        if mode == 'failed_dispatch':
            raise RuntimeError('test dispatch failure')
    monkeypatch.setattr(navigation.xbmcgui, 'getCurrentWindowDialogId', lambda: dialog[0], raising=False)
    class Progress:
        def __init__(self): self.window = window
        def create(self, *args): pass
        def set_recording(self, *args): pass
        def update(self, *args): pass
        def close(self): pass
        def activity(self, value): pass
        def iscanceled(self): return stopped.is_set()
    tools_ref = []
    def run(text, cancelled, progress, **kwargs):
        tools = tools_ref[0]
        calls.append(text)
        if len(calls) == 1:
            ready.set()
            result = tools._queue_playback('test-playback', 'test-addon')
            if mode == 'handoff':
                assert result['status'] == 'dispatched' and 'deferred' not in result
                assert tail_ready.wait(2), 'Model must not block UI handoff'
                events.append('final_reply')
            else:
                assert mode == 'supplement' and result['status'] == 'pending'
        else:
            assert text == '取消播放' and not window.backgrounded
            tools.ui_action = None  # Supplement supersedes the pending destination.
        return {'reply': '{"message":"完成","choices":[]}', 'tools': []}
    def create(*args, **kwargs):
        tools_ref.append(kwargs['tools'])
        return SimpleNamespace(run=run), kwargs['tools']
    def poll(drain):
        if not displayed:
            assert ready.wait(2)
            if mode == 'supplement':
                return '取消播放'
            if mode == 'cancel':
                stopped.set()
    monkeypatch.setattr(turn_ui, 'create_agent', create)
    monkeypatch.setattr(turn_ui, 'playback_state', lambda: (False, ''))
    background = SimpleNamespace(poll=lambda: window.backgrounded, hide=hide, foreground=foreground)
    def invoke():
        return turn_ui.run_turn('', tmp_path, {'permission_level':'media','approval_strategy':'request'},
            '测试', SimpleNamespace(completed=lambda media: None), None, builtin,
            SimpleNamespace(setProperty=lambda *args: None),
            SimpleNamespace(abortRequested=lambda: False, waitForAbort=wait), Progress(),
            poll_input=poll, on_message=lambda *args: displayed.append(args), background=background)
    if mode == 'failed_dispatch':
        with pytest.raises(RuntimeError, match='test dispatch failure'):
            invoke()
        assert events == ['hide', 'dispatch', 'restore'] and not window.backgrounded
    else:
        result = invoke()
        if mode == 'handoff':
            assert events[0:2] == ['hide', 'dispatch'] and 'final_reply' in events
            assert result['ui_handed_off'] and 'ui_action' not in result
            assert events.count('dispatch') == 1 and calls == ['测试']
            import json
            timing = json.loads((tmp_path/'diagnostics/last_agent.json').read_text())['playback_timing'][0]
            assert timing['requested_seconds'] <= timing['dispatch_seconds'] <= timing['handoff_seconds']
            assert timing['reply_after_handoff_seconds'] >= 0
        elif mode == 'supplement':
            assert calls == ['测试', '取消播放'] and not events
            assert displayed == [('user', '取消播放'), ('assistant', '完成')]
            assert 'ui_handed_off' not in result and 'ui_action' not in result
        else:
            assert result is None and not events
