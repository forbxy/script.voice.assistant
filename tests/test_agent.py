import json
from pathlib import Path
import subprocess

import pytest

from legacy.agent import Agent, AgentError, ChatClient
from legacy.agent_config import Conversation, load_key, save_key, validate_url
from resources.lib.agent_tools import KodiTools


def call(name, arguments, identifier='call-1'):
    return {'id': identifier, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools, cancelled):
        self.requests.append(json.loads(json.dumps(messages)))
        return next(self.responses)


def test_agent_uses_real_search_results_before_playing_and_reports_tool_outcome():
    requests = []
    def rpc(method, params):
        requests.append((method, params))
        if method == 'VideoLibrary.GetMovies':
            return {'movies': [{'movieid': 42, 'title': '老无所依', 'year': 2007, 'file': 'private-path'}]}
        return 'OK'
    tools = KodiTools(rpc)
    client = Client([
        {'tool_calls': [call('search_media', {'query': '老无所依', 'kind': 'movie'})]},
        {'tool_calls': [call('play_media', {'kind': 'movie', 'id': 42}, 'call-2')]},
        {'content': '已开始播放老无所依。'}])
    result = Agent(client, tools).run('播放老无所依')
    assert requests[-1] == ('Player.Open', {'item': {'movieid': 42}, 'options': {'resume': True}})
    assert result['reply'] == '已开始播放老无所依。'
    assert 'private-path' not in json.dumps(client.requests)
    assert len(result['tools']) == 2


def test_agent_does_not_execute_repeated_tool_call_id_twice():
    requests = []
    tools = KodiTools(lambda method, params: requests.append((method, params)) or 20)
    tool_call = call('set_volume', {'volume': 20})
    client = Client([{'tool_calls': [tool_call]}, {'tool_calls': [tool_call]}, {'content': '音量已设为20。'}])
    Agent(client, tools).run('音量20')
    assert len(requests) == 1


def test_agent_rejects_conflicting_repeated_call_ids():
    tools = KodiTools(lambda *unused: 20)
    client = Client([{'tool_calls': [call('set_volume', {'volume': 20})]},
                     {'tool_calls': [call('set_volume', {'volume': 30})]}])
    with pytest.raises(AgentError, match='冲突'):
        Agent(client, tools).run('音量20')


def test_agent_limits_work_and_cancellation_prevents_followup_tool():
    requests = []
    tools = KodiTools(lambda *args: requests.append(args) or 20)
    client = Client([{'tool_calls': [call('set_volume', {'volume': 20})]}] * 5)
    with pytest.raises(AgentError, match='限定步骤'):
        Agent(client, tools, max_rounds=2).run('音量20')
    assert len(requests) == 1
    with pytest.raises(InterruptedError):
        Agent(client, tools).run('停止', cancelled=lambda: True)
    assert len(requests) == 1


def test_invalid_tool_and_fabricated_media_id_never_reach_kodi():
    requests = []
    tools = KodiTools(lambda *args: requests.append(args))
    for name, arguments in [('execute_shell', {'command': 'reboot'}),
                            ('set_volume', {'volume': True}), ('set_volume', {'volume': 101}),
                            ('set_volume', {'volume': 10, 'command': 'reboot'}),
                            ('play_media', {'kind': 'movie', 'id': 42})]:
        assert tools.execute(name, arguments)['ok'] is False
    assert not requests


def test_tv_episode_selection_uses_episode_id_and_survives_next_turn():
    requests = []
    def rpc(method, params):
        requests.append((method, params))
        if method.endswith('GetEpisodes'):
            return {'episodes': [{'episodeid': 13, 'season': 2, 'episode': 1, 'title': '第一集'}]}
        return 'OK'
    tools = KodiTools(rpc, media=[{'kind': 'tvshow', 'id': 7, 'title': '测试剧'}])
    assert tools.execute('get_episodes', {'tvshowid': 7, 'season': 2})['ok']
    following = KodiTools(rpc, media=tools.media.values())
    assert following.execute('play_media', {'kind': 'episode', 'id': 13, 'resume': True})['ok']
    assert requests[-1][1] == {'item': {'episodeid': 13}, 'options': {'resume': True}}


def test_seek_is_clamped_and_pause_is_idempotent():
    requests = []
    def rpc(method, params):
        requests.append((method, params))
        if method.endswith('GetActivePlayers'):
            return [{'playerid': 1, 'type': 'video'}]
        if method.endswith('GetProperties'):
            return {'time': {'hours': 0, 'minutes': 0, 'seconds': 30},
                    'totaltime': {'hours': 0, 'minutes': 2, 'seconds': 0}}
        return 'OK'
    tools = KodiTools(rpc)
    tools.execute('seek_playback', {'seconds': -60})
    assert requests[-1][1]['value']['time']['seconds'] == 0
    tools.execute('control_playback', {'action': 'pause'})
    assert requests[-1] == ('Player.PlayPause', {'playerid': 1, 'play': False})


def test_refused_maintenance_cannot_be_retried_or_scheduled(monkeypatch):
    import resources.lib.agent_tools as module
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/' + name)
    confirmations, scheduled = [], []
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: scheduled.append(args))
    tools = KodiTools(lambda *unused: None, confirm=lambda *args: confirmations.append(args) or False)
    assert not tools.execute('system_action', {'action': 'restart_kodi'})['ok']
    assert not tools.execute('system_action', {'action': 'reboot'})['ok']
    assert len(confirmations) == 1
    assert tools.commit_deferred() is None and not scheduled


def test_maintenance_only_schedules_after_confirmation_and_explicit_commit(monkeypatch):
    import resources.lib.agent_tools as module
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/' + name)
    scheduled = []
    monkeypatch.setattr(module.subprocess, 'run', lambda command, **kwargs: scheduled.append(command))
    tools = KodiTools(lambda *unused: None, confirm=lambda *unused: True)
    assert tools.execute('system_action', {'action': 'restart_kodi'})['deferred'] == 'restart_kodi'
    assert not scheduled
    with pytest.raises(InterruptedError):
        tools.commit_deferred(cancelled=lambda: True)
    assert not scheduled
    assert tools.commit_deferred()['scheduled']
    assert scheduled[0][-3:] == ['/usr/bin/systemctl', 'restart', 'kodi']
    assert '--on-active=5s' in scheduled[0]
    assert tools.commit_deferred() is None


def test_disabled_system_tools_cannot_be_called():
    tools = KodiTools(lambda *unused: None, system_enabled=False)
    assert not tools.execute('get_system_status', {})['ok']
    assert not tools.execute('system_action', {'action': 'reboot'})['ok']
    assert all(tool['function']['name'] not in ('get_system_status', 'system_action') for tool in tools.definitions())


def test_credentials_stay_private_and_context_expires_in_complete_turns(tmp_path, monkeypatch):
    import legacy.agent_config as module
    save_key(tmp_path, 'test-secret')
    assert load_key(tmp_path) == 'test-secret'
    assert (tmp_path / 'codex-api-key.txt').stat().st_mode & 0o777 == 0o600
    conversation = Conversation(tmp_path)
    turn = [{'role': 'user', 'content': '找电影'}, {'role': 'assistant', 'content': '找到电影'}]
    conversation.save(turn, [{'kind': 'movie', 'id': 7, 'title': '电影'}])
    loaded = Conversation(tmp_path)
    assert loaded.history() == turn and loaded.media[0]['id'] == 7
    monkeypatch.setattr(module.time, 'time', lambda: json.loads(conversation.path.read_text())['updated_at'] + 901)
    assert Conversation(tmp_path).history() == []
    loaded.clear()
    assert not conversation.path.exists()
    assert load_key(tmp_path) == 'test-secret'


def test_url_rejects_plain_remote_http_and_embedded_credentials():
    assert validate_url('https://api.deepseek.com/v1/') == 'https://api.deepseek.com/v1'
    for url in ('http://example.com', 'https://secret@example.com', 'https://example.com?key=secret'):
        with pytest.raises(ValueError):
            validate_url(url)


def test_chinese_conversation_survives_a_default_ascii_file_encoding(tmp_path, monkeypatch):
    original = Path.read_text
    def ascii_default(path, *args, **kwargs):
        kwargs.setdefault('encoding', 'ascii')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', ascii_default)
    turn = [{'role': 'user', 'content': '播放刚找到的电影'}, {'role': 'assistant', 'content': '已播放'}]
    Conversation(tmp_path).save(turn, [])
    assert Conversation(tmp_path).history() == turn


def test_cancelled_http_request_kills_and_reaps_child_without_key_in_arguments(monkeypatch):
    import legacy.agent as module
    events = []
    class Process:
        dead = False
        def communicate(self, input=None, timeout=None):
            if timeout is not None:
                events.append('request')
                raise subprocess.TimeoutExpired('python3', timeout)
            events.append('reaped')
            return b'', None
        def poll(self):
            return 0 if self.dead else None
        def kill(self):
            self.dead = True
            events.append('killed')
    def popen(command, **kwargs):
        assert 'secret-key' not in ' '.join(command)
        return Process()
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/python3')
    client = ChatClient('secret-key')
    with pytest.raises(InterruptedError):
        client.complete([], [], cancelled=lambda: bool(events))
    assert events == ['request', 'killed', 'reaped']
