"""Background work keeps the same task; reopening consumes no recognition input."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import queue
import sys
from types import SimpleNamespace

import pytest

from test_conversation_list import conversation_module


@pytest.fixture
def background_env(conversation_module, monkeypatch, tmp_path):
    properties, notices, claims = {}, [], []
    home = SimpleNamespace(getProperty=lambda key: properties.get(key, ''),
        setProperty=lambda key, value: properties.update({key: value}),
        clearProperty=lambda key: properties.pop(key, None))
    monitor = SimpleNamespace(abortRequested=lambda: False, waitForAbort=lambda n: False)
    monkeypatch.setattr(conversation_module.xbmcgui, 'Dialog',
        lambda: SimpleNamespace(notification=lambda *args, **kwargs: notices.append(args)), raising=False)
    path = Path(__file__).resolve().parents[1] / 'resources/lib/ui/background.py'
    spec = importlib.util.spec_from_file_location('resources.lib.ui.background_under_test', path)
    background_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(background_module)
    @contextmanager
    def claim(*args):
        claims.append(('claim', args[-1]))
        yield None
        claims.append(('release', args[-1]))
    monkeypatch.setattr(background_module, 'claim', claim)
    window = conversation_module.ConversationWindow()
    window.show()
    bg = background_module.Background(window, home, tmp_path, monitor, 'usb')
    window.task_active = window.busy = window.accepting_input = True
    return SimpleNamespace(window=window, bg=bg, home=home, monitor=monitor,
        properties=properties, notices=notices, claims=claims, module=background_module,
        gui=conversation_module.xbmcgui, xbmc=conversation_module.xbmc, conversation=conversation_module)


def hide(env):
    env.window.onClick(404)
    assert env.bg.poll()
    assert env.window.backgrounded and not env.window.visible
    assert env.properties['VoiceAssistant.Background'] == 'true'


def test_hiding_preserves_worker_transcript_and_skin_reload_recovery(background_env, monkeypatch):
    env = background_env
    env.window.append_message('user', '处理任务')
    hide(env)
    monkeypatch.setattr(env.xbmc, 'getCondVisibility', lambda condition: False)
    env.window.activity('隐藏后继续执行')
    env.window.status('正在处理')
    env.window.refresh()
    assert not env.window.visible and not env.window.cancelled.is_set()
    assert not env.conversation.ConversationProgress(env.window).iscanceled()
    assert len(env.window.items) == 2
    env.bg.foreground()
    assert env.window.visible and not env.window.backgrounded
    assert env.window.history == [('user', '处理任务')]
    assert env.window.getControl(500).size() == len(env.window.display_items)
    assert not env.window._closed


@pytest.mark.parametrize('source', ['usb', 'cmcc', 'nanosic', 'ur02', 'r22', 'b25'])
def test_first_voice_only_reopens_and_second_voice_is_available(background_env, source):
    from resources.lib.inputs.registry import INPUTS
    from resources.lib.ui.activation import request_existing
    env = background_env
    env.bg.source = source
    env.properties['VoiceAssistant.State'] = 'thinking'
    hide(env)
    if source == 'usb':
        request_existing(env.home, source)
    else:
        env.properties['VoiceAssistant.' + INPUTS[source][1] + 'Session'] = 'a' * 32
    env.window.events.put(('voice', ''))
    assert env.bg.poll()  # Caller must skip recognition/input on this tick.
    assert env.window.visible and not env.window.backgrounded
    assert env.window.events.empty()
    assert not env.properties.get('VoiceAssistant.USBRecordRequested')
    assert not env.properties.get('VoiceAssistant.Background')
    assert not env.bg.pending()  # Stale lease property cannot start capture next tick.
    assert not env.bg.poll()
    if source == 'usb':
        request_existing(env.home, source)
        assert env.properties['VoiceAssistant.USBRecordRequested']
        assert not env.claims
    else:
        assert env.claims == [('claim', 'a' * 32), ('release', 'a' * 32)]
        env.properties['VoiceAssistant.' + INPUTS[source][1] + 'Session'] = 'b' * 32
        assert env.bg.pending() == 'b' * 32


def test_binding_preceding_bluetooth_token_discards_same_press(background_env):
    from resources.lib.ui.activation import request_existing
    env = background_env
    env.bg.source = 'b25'
    env.properties['VoiceAssistant.State'] = 'thinking'
    hide(env)
    request_existing(env.home, 'b25')
    def publish(seconds):
        env.properties['VoiceAssistant.B25Session'] = 'c' * 32
        return False
    env.monitor.waitForAbort = publish
    assert env.bg.poll()
    assert env.window.visible and not env.bg.pending()
    assert env.claims == [('claim', 'c' * 32), ('release', 'c' * 32)]


@pytest.mark.parametrize('recording,question', [(True, ''), (False, 'question')])
def test_background_not_allowed_during_capture_or_question(background_env, recording, question):
    env = background_env
    env.window.recording, env.window.question_token = recording, question
    env.window.onClick(404)
    assert not env.window.background_requested.is_set()
    assert not env.bg.poll() and env.window.visible


@pytest.mark.parametrize('failed', [False, True])
def test_completion_notifies_only_when_hidden_and_retains_result(background_env, failed):
    env = background_env
    env.bg.complete(failed)
    assert not env.notices
    hide(env)
    env.window.task_active = False
    env.window.present({'message': '失败详情' if failed else '已完成', 'choices': []})
    env.bg.complete(failed)
    assert len(env.notices) == 1 and ('失败' if failed else '已完成') in env.notices[0][1]
    assert not env.window.visible and env.window.backgrounded
    env.properties['VoiceAssistant.ForegroundRequested'] = 'request'
    assert env.bg.poll()
    assert env.window.visible and env.window.history[-1][1] == ('失败详情' if failed else '已完成')
    env.bg.close()
    assert not env.properties.get('VoiceAssistant.Background')
    assert not env.properties.get('VoiceAssistant.ForegroundRequested')


def test_question_restores_background_without_losing_answer_routing(background_env):
    from resources.lib.intelligence.questions import UserQuestions
    from resources.lib.ui.questions import QuestionFlow
    env = background_env
    hide(env)
    requests, history = queue.Queue(), []
    request = UserQuestions({'questions': [{'id': 'q', 'header': '选择', 'question': '选哪个？',
        'options': [{'label': 'A', 'description': '第一个'}]}], 'isBlocking': True})
    requests.put(request)
    flow = QuestionFlow(requests, env.window, lambda *args: history.append(args), foreground=env.bg.foreground)
    flow.tick()
    assert env.window.visible and not env.window.backgrounded
    assert flow.answer('A', flow.token) and request.ready.is_set()
    assert request.answers['q']['answers'] == ['A']


def test_back_after_restoring_still_cancels_and_final_close_cannot_reopen(background_env):
    env = background_env
    hide(env)
    env.bg.foreground()
    env.window.onAction(SimpleNamespace(getId=lambda: 92))
    assert env.window.cancelled.is_set()
    env.bg.close()
    env.window.close()
    env.window.set_background(False)
    assert not env.window.visible


@pytest.mark.parametrize('source', ['usb', 'b25'])
def test_actual_entry_routes_background_key_without_recording(tmp_path, monkeypatch, source):
    import fcntl
    import importlib.util
    from resources.lib.data_paths import data_path
    props = {'VoiceAssistant.State': 'thinking', 'VoiceAssistant.Background': 'true'}
    addon = SimpleNamespace(getAddonInfo=lambda key: str(tmp_path),
        getSetting=lambda key: source if key == 'v2_voice_input' else '')
    home = SimpleNamespace(getProperty=lambda key: props.get(key, ''),
        setProperty=lambda key, value: props.update({key: value}))
    for name, module in {
        'xbmc': SimpleNamespace(), 'xbmcgui': SimpleNamespace(Window=lambda *args: home),
        'xbmcaddon': SimpleNamespace(Addon=lambda *args: addon),
        'xbmcvfs': SimpleNamespace(translatePath=lambda path: path),
    }.items(): monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr('resources.lib.profile_upgrade.prepare_profile', lambda *args: True)
    path = Path(__file__).resolve().parents[1] / 'default.py'
    spec = importlib.util.spec_from_file_location('entry_background_test', path)
    entry = importlib.util.module_from_spec(spec)
    # Avoid the development-only import of application on module import.
    monkeypatch.setitem(sys.modules, 'resources.lib.application', SimpleNamespace(main=None, capture=None, rpc=None))
    spec.loader.exec_module(entry)
    monkeypatch.setattr(sys, 'argv', ['default.py', '--voice'])
    with data_path(tmp_path, 'invocation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        entry.guarded_main()
    assert props['VoiceAssistant.ForegroundRequested']
    assert 'VoiceAssistant.USBRecordRequested' not in props


@pytest.mark.parametrize('source', ['usb', 'b25'])
@pytest.mark.parametrize('failed', [False, True])
def test_conversation_finishes_hidden_reopens_without_capture_then_accepts_next_turn(
        background_env, tmp_path, monkeypatch, source, failed):
    from resources.lib.data_paths import data_path
    from resources.lib.inputs.registry import INPUTS
    env = background_env
    calls, captures, waits, phases = [], [], [], []
    values = {'voice_input': source, 'session_policy': 'conversation'}
    addon = SimpleNamespace(getAddonInfo=lambda key: str(tmp_path))
    monkeypatch.setattr(env.gui, 'Window', lambda *args: env.home, raising=False)
    monkeypatch.setattr(env.xbmc, 'Monitor', lambda: env.monitor, raising=False)
    monkeypatch.setattr(env.xbmc, 'executebuiltin', lambda *args: None, raising=False)
    monkeypatch.setitem(sys.modules, 'xbmcaddon', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'xbmcvfs', SimpleNamespace(translatePath=lambda path: path))
    monkeypatch.setitem(sys.modules, 'resources.lib.ui.background', env.module)
    monkeypatch.setitem(sys.modules, 'resources.lib.ui.conversation', env.conversation)
    monkeypatch.setattr(env.conversation, 'ConversationWindow', lambda *args: env.window)
    monkeypatch.setattr('resources.lib.intelligence.ui_context.capture', lambda rpc: {'selected': None})
    monkeypatch.setattr('resources.lib.inputs.registry.claim', env.module.claim)
    path = Path(__file__).resolve().parents[1] / 'resources/lib/application.py'
    spec = importlib.util.spec_from_file_location('resources.lib.application_background_test', path)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    monkeypatch.setattr(app, 'Settings', lambda addon: SimpleNamespace(values=lambda: dict(values), get=values.get))

    def wait(seconds):
        waits.append(seconds)
        assert len(waits) < 30, phases
        if env.notices and 'requested' not in phases:
            assert env.window.backgrounded and not env.window.visible
            phases.append('requested')
            if source == 'usb':
                env.properties['VoiceAssistant.ForegroundRequested'] = 'first-press'
            else:
                env.properties['VoiceAssistant.B25Session'] = 'a' * 32
        elif 'requested' in phases and 'restored' not in phases and env.window.visible:
            assert not captures and env.window.history[-1][1].startswith('处理失败' if failed else '任务一完成')
            phases.append('restored')
            if source == 'usb':
                env.properties['VoiceAssistant.USBRecordRequested'] = 'second-press'
            else:
                env.properties['VoiceAssistant.B25Session'] = 'b' * 32
        elif len(calls) == 2:
            env.window.cancelled.set()
        return False
    env.monitor.waitForAbort = wait

    def capture(*args, **kwargs):
        assert phases == ['requested', 'restored'] and env.window.visible
        captures.append(True)
        env.properties.pop('VoiceAssistant.' + INPUTS[source][1] + 'Session', None)
        return '第二句话'
    monkeypatch.setattr(app, 'capture', capture)

    def run_turn(*args, **kwargs):
        calls.append((args[3], args[4].history_id))
        if len(calls) == 1:
            env.window.onClick(404)
            assert kwargs['background'].poll()
            env.window.activity('后台完成的步骤')
            assert not env.window.visible and not kwargs['progress'].iscanceled()
            if failed: raise RuntimeError('测试错误')
            message = '任务一完成'
        else:
            assert args[3] == '第二句话' and len(captures) == 1
            assert not env.window.backgrounded
            message = '任务二完成'
        return {'message': message, 'interaction': 'continue', 'choices': []}
    monkeypatch.setitem(sys.modules, 'resources.lib.ui.agent_turn', SimpleNamespace(
        run_turn=run_turn, playback_state=lambda: (False, ''), close_for_playback=lambda *args: False))
    app._conversation(addon, tmp_path, initial_text='原始任务')
    assert len(calls) == 2 and calls[0][1] == calls[1][1]
    assert len(env.notices) == 1 and len(captures) == 1
    history = json.loads(data_path(tmp_path, 'chat-history.json').read_text())
    assert len(history['rounds']) == 2 and all(row['state'] == 'done' for row in history['rounds'])
    assert env.properties['VoiceAssistant.State'] == 'idle'
    assert not env.properties.get('VoiceAssistant.Background')
    assert not env.properties.get('VoiceAssistant.ForegroundRequested')
