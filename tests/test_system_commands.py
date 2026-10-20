import pytest

from resources.lib import agent_tools
from resources.lib.intelligence.command_rules import common_request
from resources.lib.intelligence.commands import Commands
from resources.lib.intelligence.permissions import Permissions
from resources.lib.intelligence.runtime import create_tools


@pytest.mark.parametrize('text,action', [
    ('关机', 'shutdown'), ('关闭系统', 'shutdown'), ('关掉系统', 'shutdown'),
    ('请帮我关掉电视盒子吧。', 'shutdown'), ('关闭机顶盒', 'shutdown'), ('把盒子关掉', 'shutdown'),
    ('重启', 'reboot'), ('重启系统', 'reboot'), ('重新启动', 'reboot'),
    ('麻烦你帮我重启一下盒子。', 'reboot'), ('重启系统一下吧', 'reboot'), ('重启 CoreELEC', 'reboot'),
    ('挂起', 'suspend'), ('挂起系统', 'suspend'), ('睡眠', 'suspend'), ('系统睡眠', 'suspend'),
    ('进入睡眠模式', 'suspend'), ('进入待机', 'suspend'), ('待机', 'suspend'),
    ('请让盒子进入睡眠模式吧', 'suspend'), ('把盒子挂起', 'suspend'), ('让机顶盒睡眠', 'suspend'),
    ('重启到安卓', 'reboot_android'), ('切换到安卓', 'reboot_android'), ('返回安卓系统', 'reboot_android'),
    ('请重启进入 Android。', 'reboot_android'), ('重启到 Android 系统', 'reboot_android'),
])
def test_clear_power_phrases_resolve_to_one_system_action(text, action):
    request = common_request(text)
    assert request['tool'] == 'system_action' and request['arguments'] == {'action': action}


@pytest.mark.parametrize('text', [
    '不要关机', '先别重启', '不要让盒子睡眠', '不进入待机', '取消关机', '取消重启',
    '怎么关机', '关机了吗', '重启吗', '能不能重启系统', '如果卡了就重启',
    '十分钟后关机', '看完电影再关机', '暂停然后关机', '重启系统并打开筛选',
    '进入睡眠模式之前先保存', '关机还是重启', '关机，重启', '关闭电视',
    '不要切换到安卓', '取消重启到安卓', '返回安卓前先关闭电影', '重启安卓插件',
    '重启Kodi', '休眠', '同意', '好的', '可以',
])
def test_ambiguous_conditional_or_unrelated_text_never_becomes_a_power_command(text):
    assert common_request(text) is None


@pytest.fixture
def power(monkeypatch):
    state = {'supported': True, 'allowed': True}
    rpc_calls, confirmations, scheduled = [], [], []
    monkeypatch.setattr(agent_tools.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(agent_tools.subprocess, 'run', lambda command, **kwargs: scheduled.append(command))
    def rpc(method, params):
        rpc_calls.append((method, params))
        assert method == 'System.GetProperties' and params == {'properties': ['cansuspend']}
        return {'cansuspend': state['supported']}
    def confirm(title, message):
        confirmations.append((title, message))
        return state['allowed']
    tools = agent_tools.KodiTools(rpc, confirm=confirm, permissions=Permissions('full', 'request'))
    return state, rpc_calls, confirmations, scheduled, tools


@pytest.mark.parametrize('phrase,verb', [('关机', 'poweroff'), ('重启', 'reboot'), ('睡眠', 'suspend'),
                                       ('重启到安卓', 'reboot')])
def test_local_power_retains_confirmation_and_only_schedules_at_commit(power, phrase, verb):
    state, rpc_calls, confirmations, scheduled, tools = power
    result = Commands(tools).process(phrase)
    assert result['model_invocations'] == 0 and result['tools'][-1]['ok']
    assert len(confirmations) == 1 and not scheduled
    action = result['tools'][-1]['arguments']['action']
    assert tools.deferred == action
    assert '将在本轮结束后执行' in result['reply']
    with pytest.raises(InterruptedError):
        tools.commit_deferred(cancelled=lambda: True)
    assert not scheduled
    committed = tools.commit_deferred()
    assert committed == {'action': action, 'scheduled': True, 'delay_seconds': 5}
    assert len(scheduled) == 1 and scheduled[0][-2:] == ['/usr/bin/systemctl', verb]
    assert '--on-active=5s' in scheduled[0]
    hooks = [part for part in scheduled[0] if part.startswith('--property=ExecStartPre=')]
    assert hooks == (['--property=ExecStartPre=/usr/bin/rebootfromnand'] if action == 'reboot_android' else [])
    assert tools.commit_deferred() is None


def test_declining_a_local_power_command_cannot_retry_with_another_synonym(power):
    state, _, confirmations, scheduled, tools = power
    state['allowed'] = False
    first = Commands(tools).process('关机')
    second = Commands(tools).process('重启系统')
    assert not first['tools'][-1]['ok'] and not second['tools'][-1]['ok']
    assert len(confirmations) == 1 and tools.deferred is None
    assert tools.commit_deferred() is None and not scheduled


def test_unsupported_sleep_does_not_fall_back_to_shutdown_or_hibernate(power):
    state, rpc_calls, confirmations, scheduled, tools = power
    state['supported'] = False
    result = Commands(tools).process('进入睡眠模式')
    assert not result['tools'][-1]['ok'] and '不支持挂起' in result['reply']
    assert len(rpc_calls) == 1 and not confirmations and not scheduled and tools.deferred is None


@pytest.mark.parametrize('level', ['media', 'settings'])
def test_local_power_cannot_raise_the_selected_permission_level(power, level):
    _, rpc_calls, confirmations, scheduled, tools = power
    tools.permissions = Permissions(level, 'full_access')
    for text in ('关机', '重启', '睡眠'):
        result = Commands(tools).process(text)
        assert result['model_invocations'] == 0 and not result['tools'][-1]['ok']
    assert not rpc_calls and not confirmations and not scheduled


def test_local_power_works_with_agent_off_and_existing_automatic_approval(power):
    _, _, confirmations, scheduled, old_tools = power
    tools = create_tools(None, {'agent_backend': 'off', 'permission_level': 'full', 'approval_strategy': 'full_access'},
                         old_tools.rpc, confirm=old_tools.confirm)
    result = Commands(tools).process('进入睡眠模式')
    assert result['model_invocations'] == 0 and result['tools'][-1]['ok'] and tools.deferred == 'suspend'
    assert not confirmations and not scheduled


def test_cancellation_and_system_disable_prevent_scheduling(power):
    _, rpc_calls, confirmations, scheduled, tools = power
    with pytest.raises(InterruptedError):
        Commands(tools).process('关机', cancelled=lambda: True)
    tools.system_enabled = False
    assert not Commands(tools).process('挂起')['tools'][-1]['ok']
    assert not rpc_calls and not confirmations and not scheduled


def test_missing_android_boot_script_never_changes_flags_or_falls_back_to_reboot(power, monkeypatch):
    _, _, confirmations, scheduled, tools = power
    monkeypatch.setattr(agent_tools.shutil, 'which', lambda name: None if name == 'rebootfromnand' else '/usr/bin/' + name)
    result = Commands(tools).process('切换到安卓')
    assert not result['tools'][-1]['ok'] and '原生启动脚本' in result['reply']
    assert not confirmations and not scheduled and tools.deferred is None


def test_android_script_is_rechecked_before_commit(power, monkeypatch):
    _, _, _, scheduled, tools = power
    Commands(tools).process('重启到安卓')
    monkeypatch.setattr(agent_tools.shutil, 'which', lambda name: None if name == 'rebootfromnand' else '/usr/bin/' + name)
    with pytest.raises(ValueError, match='原生启动脚本'):
        tools.commit_deferred()
    assert not scheduled
