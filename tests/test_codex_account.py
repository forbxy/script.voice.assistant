from resources.lib.data_paths import data_path
import json
import os
from pathlib import Path
import sys

import pytest

from resources.lib.intelligence.errors import AgentError
from resources.lib.codex_account import CodexAccount, account_summary, device_challenge
from resources.lib.codex_agent import Connection, launch_command, validate_proxy
from test_codex_agent import Protocol, finished, make_agent


CHALLENGE = {'type': 'chatgptDeviceCode', 'loginId': 'login-1',
             'verificationUrl': 'https://auth.openai.com/codex/device', 'userCode': 'ABCD-1234'}


class AccountProtocol:
    def __init__(self, events=(), logged_in=False):
        self.requests, self.sent, self.events = [], [], iter(events)
        self.logged_in, self.closed, self.sequence = logged_in, False, 0
        self.timeout, self.deadline = 30, 0

    def request(self, method, params):
        self.requests.append((method, params))
        if method == 'initialize':
            return {}
        if method == 'account/login/start':
            return dict(CHALLENGE)
        if method == 'account/logout':
            self.logged_in = False
            return {}
        if method == 'account/read':
            return {'account': {'type': 'chatgpt', 'email': 'tester@example.com',
                                'planType': 'plus', 'accessToken': 'never-export'}
                    if self.logged_in else None}
        raise AssertionError(method)

    def event(self):
        event = next(self.events)
        if isinstance(event, Exception):
            raise event
        if event.get('params', {}).get('loginId') == 'login-1' and event['params'].get('success'):
            self.logged_in = True
        return event

    def send(self, value):
        self.sent.append(value)

    def close(self, *args):
        self.closed = True


def manager(tmp_path, protocol):
    binary = tmp_path / 'app-server'
    binary.touch(mode=0o755)
    return CodexAccount(tmp_path, tmp_path, binary=binary, connection_factory=lambda *args: protocol)


@pytest.mark.parametrize('url', ['http://auth.openai.com/codex/device',
                               'https://auth.openai.com.evil.test/codex/device',
                               'https://evil.test@auth.openai.com/codex/device',
                               'https://auth.openai.com:444/codex/device',
                               'https://auth.openai.com/codex/device?redirect=evil',
                               'https://auth.openai.com/another/path'])
def test_device_code_only_links_to_official_login_page(url):
    with pytest.raises(AgentError, match='无效'):
        device_challenge(dict(CHALLENGE, verificationUrl=url))


def test_device_code_rejects_control_characters():
    with pytest.raises(AgentError):
        device_challenge(dict(CHALLENGE, userCode='ABCD\n1234'))


def test_login_waits_for_its_own_completion_and_keeps_private_credentials(tmp_path):
    protocol = AccountProtocol([
        {'method': 'account/login/completed', 'params': {'loginId': 'other', 'success': True}},
        {'method': 'account/updated', 'params': {'authMode': 'chatgpt'}},
        {'method': 'account/login/completed', 'params': {'loginId': 'login-1', 'success': True}},
    ])
    shown = []
    with manager(tmp_path, protocol) as account:
        home = data_path(tmp_path, 'codex-home')
        (home / 'auth.json').write_text('{"fake_token":"never-export"}')
        result = account.login(shown.append)
        assert result == {'logged_in': True, 'email': 'tester@example.com', 'plan': 'plus'}
        assert 'never-export' not in json.dumps(result)
        assert home.stat().st_mode & 0o777 == 0o700
        assert (home / 'auth.json').stat().st_mode & 0o777 == 0o600
    assert shown == [{'login_id': 'login-1', 'url': CHALLENGE['verificationUrl'], 'code': 'ABCD-1234'}]
    assert protocol.closed and not any(x.get('method') == 'account/login/cancel' for x in protocol.sent)


@pytest.mark.parametrize('event,exception', [
    (InterruptedError('cancel'), InterruptedError),
    ({'method': 'account/login/completed', 'params': {'loginId': 'login-1', 'success': False,
                                                   'error': 'never-export-secret'}}, AgentError),
])
def test_cancel_or_failure_cancels_pending_login_and_reaps_connection(tmp_path, event, exception):
    protocol = AccountProtocol([event])
    with pytest.raises(exception) as error:
        with manager(tmp_path, protocol) as account:
            account.login(lambda value: None)
    assert 'never-export-secret' not in str(error.value)
    assert protocol.closed
    assert any(x.get('method') == 'account/login/cancel' and x['params']['loginId'] == 'login-1'
               for x in protocol.sent)
    assert not (data_path(tmp_path, 'codex-home/auth.json')).exists()


def test_logout_removes_only_the_selected_codex_account(tmp_path):
    protocol = AccountProtocol(logged_in=True)
    deepseek = tmp_path / 'agent_credentials.json'
    deepseek.write_text('unrelated-private-credential')
    with manager(tmp_path, protocol) as account:
        assert not account.logout()['logged_in']
    assert deepseek.read_text() == 'unrelated-private-credential'
    assert protocol.closed


def test_account_summaries_do_not_export_authentication_fields():
    value = {'account': {'type': 'chatgpt', 'planType': 'pro', 'email': None,
                         'accessToken': 'secret', 'refreshToken': 'secret'}}
    assert account_summary(value) == {'logged_in': True, 'plan': 'pro', 'email': ''}
    assert not account_summary({'account': {'type': 'apiKey'}})['logged_in']


def test_chatgpt_launch_uses_managed_auth_and_leaves_model_auto():
    command = launch_command('/binary', 'https://api.deepseek.com', '', 30, 'chatgpt')
    text = ' '.join(command)
    assert 'model_provider="openai"' in text and 'forced_login_method="chatgpt"' in text
    assert 'cli_auth_credentials_store="file"' in text
    assert 'deepseek' not in text and 'model_reasoning_effort="none"' not in text
    assert 'model="' not in text


def test_chatgpt_agent_checks_login_then_uses_default_model_without_api_key(tmp_path):
    agent, protocol = make_agent(tmp_path, [finished()])
    original = protocol.request

    def request(method, params):
        if method == 'account/read':
            protocol.requests.append((method, params))
            return {'account': {'type': 'chatgpt', 'planType': 'plus'}}
        value = original(method, params)
        if method == 'thread/start':
            value['thread']['model'] = 'account-default-model'
        return value

    protocol.request = request
    agent.provider, agent.model, agent.key = 'chatgpt', '', ''
    host = agent.binary.with_name('codex-code-mode-host')
    host.chmod(0o600)
    result = agent.run('你好')
    thread = next(params for method, params in protocol.requests if method == 'thread/start')
    assert thread['modelProvider'] == 'openai' and 'model' not in thread
    assert result['model'] == 'account-default-model' and result['codex_provider'] == 'chatgpt'
    assert protocol.closed
    assert os.access(host, os.X_OK)


def test_missing_subscription_host_fails_before_launch_with_download_instruction(tmp_path):
    agent, protocol = make_agent(tmp_path, [])
    agent.provider, agent.key = 'chatgpt', ''
    agent.binary.with_name('codex-code-mode-host').unlink()
    with pytest.raises(AgentError, match='codex-code-mode-host.*重新下载'):
        agent.run('检查日志')
    assert protocol.requests == []


def test_missing_chatgpt_login_does_not_start_inference(tmp_path):
    agent, protocol = make_agent(tmp_path, [])
    original = protocol.request

    def request(method, params):
        if method == 'account/read':
            return {'account': None}
        return original(method, params)

    protocol.request = request
    agent.provider, agent.model, agent.key = 'chatgpt', '', ''
    with pytest.raises(AgentError, match='登录 Codex'):
        agent.run('你好')
    assert not any(method == 'thread/start' for method, params in protocol.requests)
    assert protocol.closed


@pytest.mark.parametrize('proxy', ['file:///secret', 'http://:password@host:8080',
                                 'http://host:70000', 'http://host:0', 'http://host/path',
                                 'http://host:80\nheader', 'http://user:p%0Ass@host:8080'])
def test_proxy_rejects_invalid_credentials_ports_and_paths(proxy):
    with pytest.raises(ValueError):
        validate_proxy(proxy)


def test_native_connection_isolates_auth_and_uses_explicit_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'never-export')
    monkeypatch.setenv('CODEX_ACCESS_TOKEN', 'never-export')
    monkeypatch.setenv('VOICE_DEEPSEEK_API_KEY', 'never-export')
    home = tmp_path / 'private-home'
    script = ('import json,os,time\n'
              'print(json.dumps({"params":{"home":os.getenv("CODEX_HOME"),'
              '"proxy":os.getenv("HTTPS_PROXY"),"keys":any(os.getenv(x) for x in '
              '["OPENAI_API_KEY","VOICE_DEEPSEEK_API_KEY","CODEX_ACCESS_TOKEN"])}}),flush=True)\n'
              'time.sleep(30)\n')
    connection = Connection([sys.executable, '-c', script], '', tmp_path, lambda: False,
                            5, home, 'http://user:p%40ss@127.0.0.1:8080')
    try:
        params = connection.read()['params']
        assert params == {'home': str(home), 'proxy': 'http://user:p%40ss@127.0.0.1:8080', 'keys': False}
    finally:
        connection.close()
    assert connection.process.poll() is not None
