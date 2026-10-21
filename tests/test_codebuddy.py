from resources.lib.data_paths import data_path
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.codebuddy_account import CodeBuddyAccount
from resources.lib.codebuddy_agent import CodeBuddyAgent
from resources.lib.codebuddy_protocol import command, configured_proxy, environment
from resources.lib.dependencies.catalog import catalog, requirements
from resources.lib.intelligence.errors import AgentError
from resources.lib.intelligence.permissions import Permissions
from resources.lib.intelligence.sessions import Sessions
from resources.lib.intelligence.steering import Steering

ROOT = Path(__file__).resolve().parents[1]


class Protocol:
    def __init__(self, events, logged_in=True):
        self.events = iter(events)
        self.logged_in = logged_in
        self.sent, self.requests = [], []
        self.closed = False
    def request(self, value):
        self.requests.append(value)
        return {'account': {'userId': 'test-user', 'token': 'test-private-token'} if self.logged_in else {},
                'currentModelId': 'default-model'}
    def send(self, value): self.sent.append(value)
    def event(self): return next(self.events)
    def close(self): self.closed = True


def control(subtype, **kwargs):
    return {'type': 'control_request', 'request_id': 'control', 'request': {'subtype': subtype, **kwargs}}


def mcp(name, arguments, identifier=1):
    return control('mcp_message', server_name='kodi', message={'jsonrpc': '2.0', 'id': identifier,
        'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})


def make_agent(tmp_path, events, **kwargs):
    protocol = Protocol(events)
    binary = tmp_path / 'binary'
    binary.touch()
    calls = []
    tools = KodiTools(lambda *args: calls.append(args) or 20)
    agent = CodeBuddyAgent(ROOT, tmp_path, tools, binary=binary,
                          connection_factory=lambda *args: protocol, **kwargs)
    return agent, protocol, calls


def test_kodi_mcp_dispatch_deduplication_and_session(tmp_path):
    sessions = Sessions(tmp_path)
    event = mcp('set_volume', {'volume': 20})
    agent, protocol, calls = make_agent(tmp_path, [event, event,
        {'type': 'result', 'session_id': 'buddy-session', 'result': 'test-private-token 已完成。'}], sessions=sessions)
    outcome = agent.run('音量20')
    assert calls == [('Application.SetVolume', {'volume': 20})]
    assert outcome['reply'] == '[private] 已完成。' and outcome['agent_backend'] == 'codebuddy'
    assert sessions.thread_id == 'buddy-session' and protocol.closed
    assert protocol.requests[0]['sdkMcpServers'] == ['kodi']
    assert len(protocol.sent) == 3  # User + both acknowledgements, one execution.


def test_plugin_info_returns_background_manifest_through_codebuddy_mcp(tmp_path):
    event = mcp('search_addons', {'query': 'RTMP Input'})
    agent, protocol, calls = make_agent(tmp_path, [event, {
        'type': 'result', 'result': '{"message":"这是 RTMP 输入流组件。","interaction":"continue","choices":[]}',
    }])
    row = {'addonid': 'inputstream.rtmp', 'name': 'RTMP Input', 'type': 'kodi.inputstream',
           'enabled': True, 'version': '21.1.2.1', 'summary': 'RTMP 流的客户端',
           'description': '由 Kodi 播放器调用的输入流组件。', 'author': 'spiff'}
    def rpc(method, params):
        calls.append((method, params))
        assert method == 'Addons.GetAddons'
        return {'addons': [row]}
    agent.tools = KodiTools(rpc, permissions=Permissions('media'), defer_ui=True)
    outcome = agent.run('这个插件是干啥用的？')
    assert len(calls) == 1 and len(outcome['tools']) == 1
    content = protocol.sent[-1]['response']['response']['mcp_response']['result']['content']
    result = json.loads(content[0]['text'])
    assert result['addons'][0]['summary'] == row['summary']
    assert result['addons'][0]['can_open'] is False
    assert agent.tools.ui_action is None


def test_conflicting_call_and_no_login_do_not_execute(tmp_path):
    agent, protocol, calls = make_agent(tmp_path, [mcp('set_volume', {'volume': 20}), mcp('set_volume', {'volume': 30})])
    with pytest.raises(AgentError, match='冲突'): agent.run('音量20')
    assert len(calls) == 1 and protocol.closed
    agent, protocol, calls = make_agent(tmp_path, [])
    protocol.logged_in = False
    with pytest.raises(AgentError, match='未登录'): agent.run('你好')
    assert not calls and protocol.closed and not protocol.sent


def test_permission_ceiling_and_native_approval(tmp_path):
    native = control('can_use_tool', tool_name='Bash', input={'command': 'uname -a'}, tool_use_id='native')
    agent, protocol, calls = make_agent(tmp_path, [native, {'type': 'result', 'result': '未执行'}])
    agent.run('查询')
    assert not protocol.sent[1]['response']['response']['allowed']
    confirmations = []
    agent, protocol, calls = make_agent(tmp_path, [native, {'type': 'result', 'result': '完成'}],
        permissions=Permissions('full', 'request'), confirm=lambda *args: confirmations.append(args) or True)
    agent.run('查询')
    assert protocol.sent[1]['response']['response']['allowed'] and 'uname -a' in confirmations[0][1]
    agent, protocol, calls = make_agent(tmp_path, [mcp('set_setting', {'setting': 'audiooutput.passthrough', 'json_value': 'true'}),
        {'type': 'result', 'result': '未执行'}])
    agent.permissions = agent.tools.permissions = Permissions('media', 'request')
    agent.run('修改设置')
    reply = protocol.sent[1]['response']['response']['mcp_response']['result']
    assert reply['isError'] and not calls


def test_tier_one_mcp_upload_is_available_but_still_requires_confirmation(tmp_path, monkeypatch):
    from resources.lib import log_upload
    confirmations, uploads = [], []
    monkeypatch.setattr(log_upload, 'upload', lambda **kwargs: uploads.append('upload') or
                        {'uploaded': True, 'url': 'https://paste.coreelec.org/TEST-NOT-UPLOADED'})
    for approved in (False, True):
        agent, protocol, calls = make_agent(tmp_path, [mcp('upload_logs', {}),
            {'type': 'result', 'result': '完成'}], permissions=Permissions('media', 'request'))
        agent.tools.permissions = agent.permissions
        agent.tools.confirm = lambda title, message: confirmations.append(title) or approved
        outcome = agent.run('上传日志')
        reply = protocol.sent[1]['response']['response']['mcp_response']['result']
        assert bool(reply.get('isError')) is not approved
        assert outcome['tools'][-1]['ok'] is approved and not calls
    assert confirmations == ['上传日志', '上传日志'] and uploads == ['upload']


def test_questions_use_existing_ui_and_cancel_expires(tmp_path):
    def answer(question): question.answer('0', '继续播放')
    question = control('can_use_tool', tool_name='AskUserQuestion', input={
        'questions': [{'question': '要做什么？', 'options': [{'label': '继续播放'}]}]}, tool_use_id='question')
    agent, protocol, calls = make_agent(tmp_path, [question, {'type': 'result', 'result': '完成'}], request_user_input=answer)
    agent.run('继续')
    response = protocol.sent[1]['response']['response']
    assert response['allowed'] and response['updatedInput']['answers'] == {'要做什么？': '继续播放'}
    pending = []
    agent, protocol, calls = make_agent(tmp_path, [question], request_user_input=pending.append)
    with pytest.raises(InterruptedError): agent.run('继续', cancelled=lambda: bool(pending))
    assert pending[0].expired.is_set() and protocol.closed


def test_failed_result_never_claims_success_and_final_speech(tmp_path):
    agent, protocol, _ = make_agent(tmp_path, [{'type': 'result', 'is_error': True, 'result': 'secret-server-error'}])
    with pytest.raises(AgentError, match='未完成'): agent.run('你好')
    assert protocol.closed
    speech = []
    agent, _, _ = make_agent(tmp_path, [{'type': 'result', 'result': '{"message":"你好。","interaction":"none","choices":[]}'}])
    agent.run('你好', speech=lambda *args: speech.append(args))
    assert speech and speech[0][0]['text'] == '你好。'


@pytest.mark.parametrize('speech_enabled', [False, True])
def test_mixed_result_is_normalized_before_ui_history_and_speech(tmp_path, speech_enabled):
    from resources.lib.intelligence.presentation import parse
    row = {'text': '银行已经打开。',
           'pronunciations': [{'start': 1, 'text': '行', 'pinyin': ['hang2']}]}
    value = {'speech': [row], 'message': '银行已经打开。\nHello, Kodi.',
             'interaction': 'close', 'choices': []}
    reply = '我已经查到信息。\n\n```json\n' + json.dumps(value, ensure_ascii=False) + '\n```'
    agent, protocol, _ = make_agent(tmp_path, [{'type': 'result', 'result': reply}])
    spoken = []
    outcome = agent.run('查询', speech=(lambda *args: spoken.append(args)) if speech_enabled else None)
    assert json.loads(outcome['reply']) == value
    assert outcome['turn'][-1]['content'] == outcome['reply']
    assert parse(outcome['reply'], agent.tools)['message'] == value['message']
    assert spoken == ([(row, 'final')] if speech_enabled else [])
    prompt = protocol.requests[0]['appendSystemPrompt']
    assert '最终回复只能输出一个' in prompt and '不要在对象前后添加' in prompt


def test_structured_work_note_never_displays_or_speaks_protocol_json(tmp_path):
    note = {'speech': [{'text': '正在查询影片。', 'pronunciations': []}], 'message': '正在查询影片。'}
    event = {'type': 'assistant', 'message': {'content': [
        {'type': 'text', 'text': '```json\n' + json.dumps(note, ensure_ascii=False) + '\n```'},
        {'type': 'tool_use', 'name': 'mcp__kodi__get_kodi_status', 'input': {}}]}}
    agent, _, _ = make_agent(tmp_path, [event, {'type': 'result', 'result': '查询完成。'}])
    shown, spoken = [], []
    agent.run('查询', progress=shown.append, speech=lambda *args: spoken.append(args),
              speech_options={'tts_scope': 'all'})
    assert '正在查询影片。' in shown
    assert all('"speech"' not in row and '```' not in row for row in shown)
    assert spoken[0] == (note['speech'][0], 'commentary')
    assert spoken[1] == ({'text': '查询完成。', 'pronunciations': []}, 'final')


def test_cli_is_restricted_and_private_paths_and_proxy(tmp_path, monkeypatch):
    binary = tmp_path / 'binary'; binary.touch()
    restricted = command(ROOT, tmp_path, binary=binary, resume='previous-session')
    full = command(ROOT, tmp_path, binary=binary, native=True)
    assert restricted[restricted.index('--tools') + 1] == 'AskUserQuestion'
    assert '--tools' not in full and '--resume' in restricted
    assert '--strict-mcp-config' in restricted
    monkeypatch.setenv('CODEBUDDY_API_KEY', 'unrelated-secret')
    env = environment(tmp_path)
    assert 'CODEBUDDY_API_KEY' not in env
    assert Path(env['HOME']).is_relative_to(tmp_path)
    assert env['CODEBUDDY_INTERNET_ENVIRONMENT'] == 'external'
    proxy = configured_proxy({'codebuddy_proxy': 'http://proxy.test:8080', 'codebuddy_proxy_username': 'user',
                              'codebuddy_proxy_password': 'p@ss'})
    assert proxy == 'http://user:p%40ss@proxy.test:8080'
    with pytest.raises(ValueError, match='CodeBuddy'):
        configured_proxy({'codebuddy_proxy': 'http://proxy.test:8080', 'codebuddy_proxy_password': 'bad'})


def test_component_is_optional_but_keeps_codex_for_dictation():
    selected = requirements(ROOT, {'agent_backend': 'codebuddy', 'recognition_engine': 'codex'})
    assert 'codebuddy' in selected and 'codex' in selected
    component = catalog(str(ROOT), 'linux_aarch64')['components']['codebuddy']
    assert component['version'] == '2.168.2'
    asset = component['assets'][0]
    assert asset['filename'] == 'codebuddy-code-headless_Linux_arm64.tar.gz'
    assert len(asset['md5']) == 32 and len(asset['sha256']) == 64


def test_public_account_never_exposes_credentials():
    state = CodeBuddyAccount.public({'account': {'token': 'private', 'userId': 'user'},
        'models': [{'id': 'default-model', 'name': 'Auto'}]})
    assert state == {'logged_in': True, 'models': [{'model': 'default-model', 'displayName': 'Auto'}]}


def test_configuration_buddy_jobs_do_not_change_codex_account(tmp_path, monkeypatch):
    from resources.lib import codebuddy_account
    from resources.lib.configuration.server import Configuration
    import time
    class Account:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def status(self): return {'logged_in': True, 'models': [{'model': 'model', 'displayName': 'Model'}]}
        def login(self, show, progress=lambda phase: None):
            show({'url': 'https://www.codebuddy.ai/login?test=1'})
            return self.status()
        def logout(self): return {'logged_in': False, 'models': []}
    monkeypatch.setattr(codebuddy_account, 'CodeBuddyAccount', Account)
    stored = {}
    addon = SimpleNamespace(getSetting=lambda key: stored.get(key, ''),
                           setSetting=lambda key, value: stored.update({key: value}))
    server = Configuration(ROOT, tmp_path, addon, lambda *args: None, port=0)
    try:
        server.snapshot.update(logged_in=True, models=[{'model': 'codex-model'}])
        identifier = server.job('codebuddy-login')['job']
        deadline = time.monotonic() + 2
        while server.snapshot['jobs'][identifier]['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        state = server.state()
        assert state['jobs'][identifier]['state'] == 'done'
        assert state['codebuddy_logged_in'] and state['codebuddy_models'][0]['model'] == 'model'
        assert state['logged_in'] and state['models'] == [{'model': 'codex-model'}]
        identifier = server.job('codebuddy-logout')['job']
        deadline = time.monotonic() + 2
        while server.snapshot['jobs'][identifier]['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        assert not server.state()['codebuddy_logged_in'] and server.state()['logged_in']
    finally:
        server.close()


def test_regions_keep_existing_home_and_isolate_native_environment(tmp_path):
    from resources.lib.codebuddy_regions import home_path, work_path, selected_model
    external, internal = environment(tmp_path, region='external'), environment(tmp_path, region='internal')
    assert home_path(tmp_path, 'external') == data_path(tmp_path, 'codebuddy-home')
    assert external['HOME'] != internal['HOME']
    assert external['CODEBUDDY_CONFIG_DIR'] != internal['CODEBUDDY_CONFIG_DIR']
    assert internal['CODEBUDDY_INTERNET_ENVIRONMENT'] == 'internal'
    assert work_path(tmp_path, 'internal') != work_path(tmp_path, 'external')
    values = {'codebuddy_region': 'internal', 'codebuddy_model': 'overseas', 'codebuddy_internal_model': 'china'}
    assert selected_model(values) == 'china' and selected_model(values, 'external') == 'overseas'
    for invalid in ('../other', '', None, [], 'ioa'):
        with pytest.raises(ValueError): environment(tmp_path, region=invalid)


def test_internal_agent_passes_region_to_native_connection_and_initialize(tmp_path):
    protocol = Protocol([{'type': 'result', 'result': '正常'}])
    arguments = []
    binary = tmp_path / 'binary'; binary.touch()
    def connect(*args):
        arguments.append(args)
        return protocol
    agent = CodeBuddyAgent(ROOT, tmp_path, KodiTools(lambda *args: None), binary=binary,
        region='internal', connection_factory=connect)
    result = agent.run('你好')
    assert result['codebuddy_region'] == 'internal'
    assert arguments[0][-1] == 'internal' and protocol.requests[0]['environment'] == 'internal'
    protocol.logged_in = False
    with pytest.raises(AgentError, match='国内版未登录'): agent.run('你好')


@pytest.mark.parametrize('region', ['external', 'internal'])
def test_account_region_controls_authenticate_logout_and_permissions(tmp_path, monkeypatch, region):
    from resources.lib import codebuddy_account
    from resources.lib.codebuddy_regions import home_path
    events = [control('auth_url_callback', authState={'authUrl':
        'https://www.codebuddy.ai/auth' if region == 'external' else 'https://copilot.tencent.com/auth'}),
        control('auth_result_callback', success=True, userinfo={'userId': 'user'})]
    protocol = Protocol(events, logged_in=False)
    connect_args = []
    def connection(*args):
        connect_args.append(args)
        return protocol
    monkeypatch.setattr(codebuddy_account, 'Connection', connection)
    binary = tmp_path / 'binary'; binary.touch()
    other = home_path(tmp_path, 'internal' if region == 'external' else 'external')
    other.mkdir(); other_file = other / 'account.txt'; other_file.write_text('keep'); other_file.chmod(0o644)
    home = home_path(tmp_path, region); home.mkdir(); secret = home / 'account.txt'; secret.touch(mode=0o644)
    shown, phases = [], []
    with CodeBuddyAccount(ROOT, tmp_path, binary=binary, region=region) as account:
        assert account.login(shown.append, progress=phases.append)['logged_in']
        account.logout()
    assert phases == ['initializing', 'requesting_login_url', 'waiting_authorization']
    assert protocol.requests[0]['environment'] == protocol.requests[-1]['environment'] == region
    auth = next(v['request'] for v in protocol.sent if v.get('request', {}).get('subtype') == 'authenticate')
    assert auth['methodId'] == region and auth['environment'] == region and connect_args[0][-1] == region
    assert shown and secret.stat().st_mode & 0o777 == 0o600
    assert other_file.stat().st_mode & 0o777 == 0o644 and other_file.read_text() == 'keep'


def test_region_switch_retains_models_and_rotates_thread_signature(tmp_path):
    from resources.lib.settings import Settings
    from resources.lib.intelligence.sessions import signature
    stored = {}
    config = Settings(SimpleNamespace(getSetting=lambda key: stored.get(key, ''),
        setSetting=lambda key, value: stored.update({key: value})))
    config.apply({'agent_backend': 'codebuddy', 'codebuddy_model': 'overseas', 'codebuddy_internal_model': 'china'})
    before = signature(config.values())
    config.apply({'codebuddy_region': 'internal'})
    assert signature(config.values()) != before
    assert config.get('codebuddy_model') == 'overseas' and config.get('codebuddy_internal_model') == 'china'
    config.apply({'codebuddy_region': 'external'})
    assert signature(config.values()) == before


def test_regional_account_jobs_capture_requested_region_and_keep_other_accounts(tmp_path, monkeypatch):
    import time
    from resources.lib import codebuddy_account
    from resources.lib.configuration.server import Configuration
    seen = []
    class Account:
        def __init__(self, *args, region, **kwargs): self.region = region
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def status(self):
            seen.append(('status', self.region))
            return {'logged_in': True, 'models': [{'model': self.region, 'displayName': self.region}]}
        def logout(self):
            seen.append(('logout', self.region))
            return {'logged_in': False, 'models': []}
    monkeypatch.setattr(codebuddy_account, 'CodeBuddyAccount', Account)
    server = Configuration(ROOT, tmp_path, SimpleNamespace(getSetting=lambda key: ''), lambda *args: None, port=0)
    def job(kind, region):
        identifier = server.job(kind, region)['job']; deadline = time.monotonic() + 2
        while server.snapshot['jobs'][identifier]['state'] == 'running' and time.monotonic() < deadline: time.sleep(.01)
        result = server.state()['jobs'][identifier]
        assert result['state'] == 'done' and result['region'] == region
    try:
        job('codebuddy-account', 'external')
        # Explicit draft region is independent of the saved settings region.
        job('codebuddy-account', 'internal')
        job('codebuddy-logout', 'internal')
        state = server.state()
        assert state['codebuddy_accounts']['external']['logged_in']
        assert state['codebuddy_accounts']['external']['models'][0]['model'] == 'external'
        assert not state['codebuddy_accounts']['internal']['logged_in']
        assert seen == [('status', 'external'), ('status', 'internal'), ('logout', 'internal')]
        with pytest.raises(ValueError): server.job('codebuddy-account', '../other')
        with pytest.raises(ValueError): server.job('account', 'internal')
    finally: server.close()


def test_login_job_publishes_waiting_stages_and_clears_used_authorization_link(tmp_path, monkeypatch):
    import threading
    import time
    from resources.lib import codebuddy_account
    from resources.lib.configuration.server import Configuration
    initializing, linked, refreshing = (threading.Event() for _ in range(3))
    request_link, authorize, finish_refresh = (threading.Event() for _ in range(3))

    class Account:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, show, progress):
            progress('initializing')
            initializing.set()
            assert request_link.wait(3)
            progress('requesting_login_url')
            show({'url': 'https://www.codebuddy.ai/test-authorization'})
            progress('waiting_authorization')
            linked.set()
            assert authorize.wait(3)
            return {'logged_in': True, 'models': []}
        def status(self):
            refreshing.set()
            assert finish_refresh.wait(3)
            return {'logged_in': True, 'models': [{'model': 'ready'}], 'credits': {'remaining': '12'}}

    monkeypatch.setattr(codebuddy_account, 'CodeBuddyAccount', Account)
    server = Configuration(ROOT, tmp_path, SimpleNamespace(getSetting=lambda key: ''), lambda *args: None, port=0)
    try:
        identifier = server.job('codebuddy-login', 'external')['job']
        assert initializing.wait(2)
        state = server.state()['jobs'][identifier]
        assert state['phase'] == 'initializing' and 'url' not in state
        request_link.set()
        assert linked.wait(2)
        state = server.state()['jobs'][identifier]
        assert state['phase'] == 'waiting_authorization' and state['url'].startswith('https://www.codebuddy.ai/')
        authorize.set()
        assert refreshing.wait(2)
        state = server.state()['jobs'][identifier]
        assert state['state'] == 'running' and state['phase'] == 'refreshing_account' and 'url' not in state
        finish_refresh.set()
        deadline = time.monotonic() + 2
        while server.state()['jobs'][identifier]['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        state = server.state()
        assert state['jobs'][identifier]['state'] == 'done'
        assert state['codebuddy_accounts']['external']['credits']['remaining'] == '12'
    finally:
        request_link.set(); authorize.set(); finish_refresh.set()
        server.close()
