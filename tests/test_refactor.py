import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from resources.lib.settings import FIELDS, Settings
from resources.lib.agent_tools import KodiTools
from resources.lib.codex_agent import launch_command
from resources.lib.intelligence.permissions import Permissions, restricted_catalog
from resources.lib.intelligence.sessions import Sessions
from resources.lib.intelligence.presentation import parse
from test_codex_agent import make_agent, finished


def settings(values=None):
    stored = dict(values or {})
    return Settings(SimpleNamespace(getSetting=lambda key: stored.get(key, ''),
                                    setSetting=lambda key, value: stored.update({key: value}))), stored


def test_new_defaults_and_validation_do_not_partially_save():
    config, stored = settings()
    values = config.values()
    assert (values['max_seconds'], values['start_timeout'], values['silence_ms']) == (30, 6, 1000)
    assert values['ur02_threshold'] == 1000 and values['threshold'] == 200
    assert values['permission_level'] == 'settings' and values['approval_strategy'] == 'request'
    assert values['agent_code_mode'] == 'off'
    assert values['recognition_engine'] == values['agent_backend'] == values['tts_enabled'] == 'off'
    with pytest.raises(ValueError):
        config.apply({'max_seconds': 31})
    config.apply({'recognition_engine': 'codex'})
    assert stored == {'v2_recognition_engine': 'codex'}


def test_zipformer_decoding_controls_validate_before_saving():
    config, stored = settings()
    assert config.get('zipformer_max_active_paths') == 4
    assert config.get('zipformer_hotwords_score') == '2'
    config.apply({'zipformer_max_active_paths': 8, 'zipformer_hotwords_score': '6'})
    assert config.get('zipformer_max_active_paths') == 8
    assert config.get('zipformer_hotwords_score') == '6'
    snapshot = dict(stored)
    for changes in ({'zipformer_max_active_paths': 17}, {'zipformer_max_active_paths': 0},
                    {'zipformer_max_active_paths': 8.5}, {'zipformer_max_active_paths': True},
                    {'zipformer_hotwords_score': 'nan'}, {'zipformer_hotwords_score': '10.5'},
                    {'zipformer_max_active_paths': 12, 'zipformer_hotwords_score': '-1'}):
        with pytest.raises(ValueError): config.apply(changes)
        assert stored == snapshot
    config.apply({'zipformer_hotwords_score': '0.5'})
    assert config.get('zipformer_hotwords_score') == '0.5'


@pytest.mark.parametrize('strategy', ['request', 'auto_review', 'full_access'])
def test_approval_cannot_raise_permission_level(strategy):
    calls = []
    for level in ('media', 'settings'):
        p = Permissions(level, strategy)
        tools = KodiTools(lambda *args: calls.append(args), permissions=p)
        assert not tools.execute('system_action', {'action': 'reboot'})['ok']
        if level == 'media':
            assert not tools.execute('set_setting', {'setting': 'x', 'json_value': '1'})['ok']
        assert p.sandbox == 'read-only' and not p.native
    assert not calls


def test_restricted_catalog_removes_native_file_and_command_tools(tmp_path):
    root = Path(__file__).resolve().parents[1]
    path = restricted_catalog(root, tmp_path, 'custom-api-model')
    for model in json.loads(Path(path).read_text())['models']:
        assert model['shell_type'] == 'disabled' and model['apply_patch_tool_type'] is None
        assert model['tool_mode'] == 'direct' and model['experimental_supported_tools'] == []
    command = launch_command('/binary', 'https://example.com', 'custom-api-model', 60,
                             permissions=Permissions(), catalog=path)
    assert 'features.shell_tool=false' in command and 'features.code_mode=false' in command
    assert 'model_catalog_json=' in ' '.join(command)


def test_setting_change_requires_approval_and_validated_scalar():
    calls = []
    tools = KodiTools(lambda method, params: calls.append((method, params)) or True,
        permissions=Permissions(), confirm=lambda *args: False)
    assert not tools.execute('set_setting', {'setting': 'audiooutput.passthrough', 'json_value': 'true'})['ok']
    assert not calls
    tools.confirm = lambda *args: True
    assert tools.execute('set_setting', {'setting': 'audiooutput.passthrough', 'json_value': 'true'})['ok']
    assert calls == [('Settings.SetSettingValue', {'setting': 'audiooutput.passthrough', 'value': True})]
    assert not tools.execute('set_setting', {'setting': 'services.webserverpassword', 'json_value': '"x"'})['ok']
    assert not tools.execute('set_setting', {'setting': 'x', 'json_value': '{"object":1}'})['ok']
    assert len(calls) == 1


def test_standard_api_keeps_dynamic_tools_in_standard_format_without_changing_subscription(tmp_path):
    root = Path(__file__).resolve().parents[1]
    subscription = json.loads(Path(restricted_catalog(root, tmp_path, 'deepseek-flash')).read_text())
    api = json.loads(Path(restricted_catalog(root, tmp_path, 'deepseek-flash', standard_api=True)).read_text())
    assert any(model.get('use_responses_lite') for model in subscription['models'])
    assert all(model['use_responses_lite'] is False for model in api['models'])
    for before, after in zip(subscription['models'], api['models']):
        assert {k:v for k,v in before.items() if k != 'use_responses_lite'} == {k:v for k,v in after.items() if k != 'use_responses_lite'}


def test_lifetime_keeps_followup_and_rotates_only_at_flow_or_day_boundary(tmp_path):
    session = Sessions(tmp_path, 'conversation', 'model')
    session.started('thread1')
    session.completed([])
    assert session.thread_id == 'thread1'
    assert Sessions(tmp_path, 'conversation', 'model').thread_id is None
    daily = Sessions(tmp_path, 'daily', 'model', today='2026-10-14')
    daily.started('daily1')
    assert Sessions(tmp_path, 'daily', 'model', today='2026-10-14').thread_id == 'daily1'
    assert Sessions(tmp_path, 'daily', 'model', today='2026-10-15').thread_id is None
    assert Sessions(tmp_path, 'daily', 'newmodel', today='2026-10-14').thread_id is None
    forever = Sessions(tmp_path, 'forever', 'model')
    forever.started('forever1')
    assert Sessions(tmp_path, 'forever', 'model', today='2027-01-01').thread_id == 'forever1'


def test_native_thread_is_resumed_instead_of_resending_a_truncated_history(tmp_path):
    agent, protocol = make_agent(tmp_path, [finished(), finished()])
    sessions = Sessions(tmp_path, 'forever', 'config')
    agent.sessions = sessions
    original = protocol.request
    def request(method, params):
        if method == 'thread/resume':
            protocol.requests.append((method, params))
            return {'thread': {'id': 'thread'}}
        return original(method, params)
    protocol.request = request
    agent.run('第一句话')
    agent.run('继续')
    assert [m for m, p in protocol.requests].count('thread/start') == 1
    assert [m for m, p in protocol.requests].count('thread/resume') == 1
    assert next(p for m, p in protocol.requests if m == 'thread/start')['ephemeral'] is False


def test_native_approval_rejection_does_not_become_a_client_permission_grant(tmp_path):
    approval = {'id': 31, 'method': 'item/commandExecution/requestApproval',
                'params': {'threadId': 'thread', 'command': 'touch /tmp/sample'}}
    agent, protocol = make_agent(tmp_path, [approval, finished()])
    agent.root = Path(__file__).resolve().parents[1]
    agent.permissions = Permissions('settings', 'full_access')
    agent.confirm = lambda *args: pytest.fail('Lower tier cannot request escape approval')
    agent.run('尝试越权')
    assert next(m for m in protocol.sent if m.get('id') == 31)['result']['decision'] == 'decline'


def test_media_choices_are_verified_and_model_cannot_close_chat():
    tools = KodiTools(lambda *args: None, media=[{'kind': 'movie', 'id': 1, 'title': '电影', 'art': {'poster': 'poster.jpg'}}])
    reply = json.dumps({'message': '选择一部', 'interaction': 'continue', 'choices': [
        {'label': '电影', 'value': '播放', 'kind': 'movie', 'id': 1},
        {'label': '不存在', 'value': '播放', 'kind': 'movie', 'id': 999}]})
    result = parse(reply, tools)
    assert len(result['choices']) == 1 and result['choices'][0]['art']['poster'] == 'poster.jpg'
    tools.playback_started = True
    assert parse(reply, tools)['interaction'] == 'continue'
    assert parse(json.dumps({'message': '结束', 'interaction': 'close'}), tools)['interaction'] == 'continue'


def test_assistant_setting_tool_cannot_change_its_security_or_credentials():
    from resources.lib.settings import assistant_settings, apply_assistant_setting
    config, stored = settings()
    tools = KodiTools(lambda *args: None, permissions=Permissions('settings', 'full_access'),
        assistant_get=lambda query: assistant_settings(config, query),
        assistant_set=lambda name, value: apply_assistant_setting(config, name, value))
    for name, value in (('permission_level', 'full'), ('approval_strategy', 'full_access'),
                        ('agent_base_url', 'https://untrusted.test'), ('doubao_key_file', '/private')):
        assert not tools.execute('set_assistant_setting', {'setting': name, 'json_value': json.dumps(value)})['ok']
    assert not stored
    assert tools.execute('set_assistant_setting', {'setting': 'threshold', 'json_value': '250'})['ok']
    assert stored == {'v2_threshold': '250'}
    assert not any('key' in row['id'] or row['id'] == 'permission_level'
                   for row in tools.execute('get_assistant_settings', {})['settings'])
