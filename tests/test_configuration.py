from resources.lib.data_paths import data_path
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import http.client
import io
import errno
import socket

import pytest

from resources.lib.configuration.server import Configuration


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    # No startup account/network request in the unit test.
    monkeypatch.setattr(Configuration, 'job', lambda self, kind: {'job': 'fake'})
    stored, writes = {}, []
    def setting(name, value):
        writes.append(threading.current_thread().ident)
        stored[name] = value
    addon = SimpleNamespace(getSetting=lambda key: stored.get(key, ''), setSetting=setting)
    root = Path(__file__).resolve().parents[1]
    value = Configuration(root, tmp_path, addon, lambda *args: None, port=0)
    value.test_writes = writes
    try:
        yield value
    finally:
        value.close()


def request(server, path, body=None, token=None, headers=None):
    h = {'Content-Type': 'application/json'}
    if token is not None:
        h['Authorization'] = 'Bearer ' + token
    h.update(headers or {})
    connection = http.client.HTTPConnection('127.0.0.1', server.http.server_port, timeout=6)
    try:
        connection.request('POST' if body is not None else 'GET', path,
            body=json.dumps(body).encode() if body is not None else None, headers=h)
        response = connection.getresponse()
        data = response.read()
        if response.status >= 400:
            raise HTTPError(path, response.status, response.reason, response.headers, io.BytesIO(data))
        return data
    finally:
        connection.close()


def test_configuration_requires_current_token_and_rejects_cross_origin(configuration):
    server = configuration
    assert '申请豆包' in request(server, '/').decode()
    with pytest.raises(HTTPError) as error:
        request(server, '/api/state')
    assert error.value.code == 401
    token = server.activate()
    state = json.loads(request(server, '/api/state', token=token))
    assert 'schema' in state and 'keys' in state
    field = state['schema']['agent_code_mode']
    assert field['default'] == 'off' and field['visible'] == 'agent_backend=codex&codex_provider=api'
    with pytest.raises(HTTPError):
        request(server, '/api/state', token=token, headers={'Origin': 'http://untrusted.test'})
    with pytest.raises(HTTPError):
        request(server, '/api/state', token=token, headers={'Host': 'untrusted.test'})
    server.expires = time.monotonic() - 1
    with pytest.raises(HTTPError):
        request(server, '/api/state', token=token)


def test_occupied_port_uses_live_fallback_with_normal_authentication(tmp_path, monkeypatch):
    monkeypatch.setattr(Configuration, 'job', lambda *args: {'job': 'fake'})
    root = Path(__file__).resolve().parents[1]
    addon = SimpleNamespace(getSetting=lambda key: '')
    with socket.socket() as occupied:
        occupied.bind(('0.0.0.0', 0))
        occupied.listen()
        preferred = occupied.getsockname()[1]
        server = Configuration(root, tmp_path, addon, lambda *args: None, port=preferred)
        fallback = server.http.server_port
        try:
            assert fallback > 0 and fallback != preferred
            assert '申请豆包' in request(server, '/').decode()
            with pytest.raises(HTTPError) as error:
                request(server, '/api/state')
            assert error.value.code == 401
            token = server.activate()
            assert 'settings' in json.loads(request(server, '/api/state', token=token))
            assert occupied.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
        finally:
            server.close()
        with socket.socket() as released:
            released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            released.bind(('0.0.0.0', fallback))
        with socket.socket() as probe:
            with pytest.raises(OSError) as error:
                probe.bind(('0.0.0.0', preferred))
            assert error.value.errno == errno.EADDRINUSE


@pytest.mark.parametrize('port,errors,attempts', [
    (8767, [errno.EACCES], [8767]),
    (0, [errno.EADDRINUSE], [0]),
    (8767, [errno.EADDRINUSE, errno.EACCES], [8767, 0]),
])
def test_failed_bind_releases_owned_dependencies_and_preserves_error(tmp_path, monkeypatch, port, errors, attempts):
    from resources.lib.configuration import server as module
    from resources.lib.dependencies import service
    original = service.Manager
    managers, ports = [], []
    def manager(*args):
        value = original(*args)
        managers.append(value)
        return value
    def fail(address, handler):
        index = len(ports)
        ports.append(address[1])
        raise OSError(errors[index], 'fixture bind failure')
    monkeypatch.setattr(service, 'Manager', manager)
    monkeypatch.setattr(module, 'ThreadingHTTPServer', fail)
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(OSError) as error:
        Configuration(root, tmp_path, SimpleNamespace(getSetting=lambda key: ''), lambda *args: None, port=port)
    assert error.value.errno == errors[-1] and ports == attempts
    assert len(managers) == 1 and managers[0].lease.closed
    assert not managers[0].server_thread.is_alive() and not managers[0].path.exists()
    # A failed web start must not leave the component owner lease locked.
    replacement = original(root, tmp_path)
    replacement.close()


def test_failed_web_start_does_not_close_shared_dependency_owner(tmp_path, monkeypatch):
    from resources.lib.configuration import server as module
    closed = []
    shared = SimpleNamespace(update=lambda values: None, close=lambda: closed.append(True))
    def fail(*args):
        raise OSError(errno.EACCES, 'fixture permission failure')
    monkeypatch.setattr(module, 'ThreadingHTTPServer', fail)
    with pytest.raises(OSError):
        Configuration(Path(__file__).resolve().parents[1], tmp_path,
                      SimpleNamespace(getSetting=lambda key: ''), lambda *args: None,
                      port=0, dependencies=shared)
    assert not closed


def test_failure_after_bind_releases_port_before_http_thread_starts(tmp_path, monkeypatch):
    from resources.lib.configuration import server as module
    from resources.lib.dependencies import catalog
    original, servers = module.ThreadingHTTPServer, []
    def listener(*args):
        value = original(*args)
        servers.append(value)
        return value
    def fail(*args):
        raise ValueError('fixture metadata failure')
    monkeypatch.setattr(module, 'ThreadingHTTPServer', listener)
    monkeypatch.setattr(catalog, 'installed', fail)
    with pytest.raises(ValueError, match='fixture metadata failure'):
        Configuration(Path(__file__).resolve().parents[1], tmp_path,
                      SimpleNamespace(getSetting=lambda key: ''), lambda *args: None, port=0)
    assert len(servers) == 1 and servers[0].fileno() == -1


def test_key_saved_privately_and_kodi_mutation_occurs_only_in_main_pump(configuration):
    server = configuration
    token = server.activate()
    outcome = {}
    def post():
        outcome['response'] = request(server, '/api/config',
            {'settings': {'max_seconds': 25, 'agent_code_mode': 'on'}, 'keys': {'doubao': 'test-private-key'}}, token)
    worker = threading.Thread(target=post)
    worker.start()
    deadline = time.monotonic() + 3
    while worker.is_alive() and time.monotonic() < deadline:
        server.pump()
        time.sleep(.01)
    worker.join(1)
    assert json.loads(outcome['response'])['ok']
    assert json.loads(request(server, '/api/state', token=token))['settings']['agent_code_mode'] == 'on'
    assert server.test_writes and set(server.test_writes) == {threading.current_thread().ident}
    path = server.profile / 'doubao-api-key.txt'
    assert path.read_text() == 'test-private-key\n' and path.stat().st_mode & 0o777 == 0o600
    assert b'test-private-key' not in request(server, '/api/state', token=token)
    assert b'test-private-key' not in outcome['response']


def test_config_endpoint_cannot_serve_arbitrary_local_files(configuration):
    token = configuration.activate()
    with pytest.raises(HTTPError) as error:
        request(configuration, '/../../doubao-api-key.txt', token=token)
    assert error.value.code == 404


def test_computer_pairing_uses_short_code_and_limits_attempts(configuration):
    server = configuration
    token = server.activate()
    value = json.loads(request(server, '/api/pair', {'code': server.pin}))
    assert value == {'token': token}
    for unused in range(5):
        with pytest.raises(HTTPError):
            request(server, '/api/pair', {'code': 'bad'})
    with pytest.raises(HTTPError):
        request(server, '/api/pair', {'code': server.pin})


def test_web_keys_set_fixed_paths_and_empty_input_preserves_selected_file(configuration):
    server = configuration
    server.settings.apply({'doubao_key_file': '/tmp/old-doubao', 'agent_key_file': '/tmp/old-agent'})
    server.save_config({'keys': {'doubao': 'fixture-doubao-key', 'codex': 'fixture-codex-key'}})
    values = server.settings.values()
    from resources.lib.doubao_config import load_key as doubao_key
    from resources.lib.configuration.credentials import load_key as codex_key
    for field, filename in [('doubao_key_file', 'doubao-api-key.txt'), ('agent_key_file', 'codex-api-key.txt')]:
        assert values[field] == str(server.profile / filename)
        assert (server.profile / filename).stat().st_mode & 0o777 == 0o600
    assert doubao_key(server.profile, values['doubao_key_file']) == 'fixture-doubao-key'
    assert codex_key(server.profile, values['agent_key_file']) == 'fixture-codex-key'
    server.settings.apply({'doubao_key_file': '/tmp/selected-on-tv'})
    server.save_config({'settings': {'max_seconds': 25}, 'keys': {'doubao': '', 'codex': ''}})
    assert server.settings.values()['doubao_key_file'] == '/tmp/selected-on-tv'


def test_key_write_failure_does_not_change_plugin_path(configuration, monkeypatch):
    import resources.lib.doubao_config as module
    server = configuration
    server.settings.apply({'doubao_key_file': '/tmp/existing-key'})
    def fail(*args):
        raise OSError('fixture disk full')
    monkeypatch.setattr(module, 'save_key', fail)
    with pytest.raises(OSError):
        server.save_config({'keys': {'doubao': 'fixture-key'}})
    assert server.settings.values()['doubao_key_file'] == '/tmp/existing-key'


def test_open_kodi_settings_cannot_receive_web_key_or_unsaved_component_changes(configuration):
    server = configuration
    server.settings_open = lambda: True
    with pytest.raises(ValueError, match='关闭 Kodi'):
        server.save_config({'settings': {'agent_backend': 'codex'}, 'keys': {'codex': 'fixture-private-key'}})
    assert not (server.profile / 'codex-api-key.txt').exists()
    assert server.settings.values()['agent_backend'] == 'off'


def test_web_proxy_password_is_write_only_and_unrelated_save_preserves_it(configuration):
    server = configuration
    server.save_config({'settings': {'codex_proxy': 'http://127.0.0.1:7890',
        'codex_proxy_username': 'fixture-user', 'codex_proxy_password': 'fixture-proxy-secret'}})
    state = server.state()
    assert state['settings']['codex_proxy_username'] == 'fixture-user'
    assert state['settings']['codex_proxy_password'] == ''
    assert state['secret_configured']['codex_proxy_password']
    assert state['schema']['codex_proxy_password']['secret']
    assert 'fixture-proxy-secret' not in json.dumps(state)
    server.save_config({'settings': {'agent_timeout': 90}})
    assert server.settings.values()['codex_proxy_password'] == 'fixture-proxy-secret'
    server.save_config({'settings': {'codex_proxy_password': ''}})
    assert not server.settings.values()['codex_proxy_password']
    assert not server.state()['secret_configured']['codex_proxy_password']


def test_component_preview_uses_unsaved_selection_without_writing_settings(configuration):
    server = configuration
    token = server.activate()
    before = server.settings.values()
    body = {'settings': {'recognition_engine': 'onnx', 'onnx_model': 'firered-ctc',
                         'agent_backend': 'codex', 'tts_enabled': 'on'}}
    result = json.loads(request(server, '/api/dependencies/preview', body, token))
    ids = [item['id'] for item in result['missing']]
    assert ids == ['onnx-runtime', 'asr-firered-ctc', 'codex', 'matcha-bilingual']
    assert result['job']['state'] == 'idle'
    assert not server.test_writes and server.settings.values() == before
    assert server.dependencies.values['recognition_engine'] == 'off'
    assert not server.state()['dependencies']['components']


def test_component_download_uses_current_web_draft_without_saving_or_replacing_kodi_selection(configuration, monkeypatch):
    server = configuration
    token = server.activate()
    calls, started = [], threading.Event()
    def blocked(root, profile, identifier, cancelled, progress):
        calls.append(identifier); started.set()
        while not cancelled(): time.sleep(.01)
        raise InterruptedError()
    import resources.lib.dependencies.service as module
    monkeypatch.setattr(module, 'install', blocked)
    body = {'settings': {'recognition_engine': 'onnx', 'onnx_model': 'firered-ctc'}}
    result = json.loads(request(server, '/api/dependencies/download', body, token))
    assert started.wait(1)
    assert result['job']['pending'] == ['onnx-runtime', 'asr-firered-ctc']
    assert server.dependencies.values['recognition_engine'] == 'off'
    assert not server.test_writes
    request(server, '/api/dependencies/download', {'settings': {'agent_backend': 'codex'}}, token)
    request(server, '/api/dependencies/cancel', {}, token)
    server.dependencies.thread.join(2)
    assert calls == ['onnx-runtime']
    assert server.state()['dependencies']['job']['state'] == 'cancelled'


@pytest.mark.parametrize('settings', [{'onnx_model': '../invalid'}, {'recognition_engine': 'invalid'},
    {'agent_key_file': '/tmp/secret'}, {'permission_level': 'full'}, [], {'tts_enabled': True}])
def test_component_draft_endpoints_reject_invalid_or_unrelated_settings(configuration, settings):
    token = configuration.activate()
    for path in ['/api/dependencies/preview', '/api/dependencies/download']:
        with pytest.raises(HTTPError) as error:
            request(configuration, path, {'settings': settings}, token)
        assert error.value.code == 400
    assert not configuration.test_writes


def pumped_request(server, path, body, token=None):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(request, server, path, body, server.activate() if token is None else token)
        deadline = time.monotonic() + 4
        while not future.done() and time.monotonic() < deadline:
            server.pump(); time.sleep(.01)
        return json.loads(future.result())


def test_media_search_switches_save_to_kodi_and_remain_independent(configuration):
    server = configuration
    state = server.state()
    assert state['settings']['media_search_emby'] == state['settings']['media_search_juku'] == 'on'
    pumped_request(server, '/api/config', {'settings':{'media_search_emby':'off'}})
    state = server.state()
    assert state['settings']['media_search_emby'] == 'off' and state['settings']['media_search_juku'] == 'on'
    pumped_request(server, '/api/config', {'settings':{'media_search_emby':'on','media_search_juku':'off'}})
    state = server.state()
    assert state['settings']['media_search_emby'] == 'on' and state['settings']['media_search_juku'] == 'off'


@pytest.mark.parametrize('path,changes', [
    ('/api/config', {'max_seconds': 25}),
    ('/api/input', {'silence_ms': 700}),
])
def test_successful_web_save_renews_session_without_repairing(configuration, monkeypatch, path, changes):
    import resources.lib.configuration.server as module
    now = [1000.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    server = configuration
    token = server.activate()
    pin, original_expiry = server.pin, server.expires
    now[0] = original_expiry - 10
    assert pumped_request(server, path, {'settings': changes}, token)['ok']
    assert server.expires == now[0] + 1800
    assert server.token == token and server.pin == pin
    now[0] = original_expiry + 1
    state = json.loads(request(server, '/api/state', token=token))
    assert all(state['settings'][name] == value for name, value in changes.items())
    now[0] = server.expires
    with pytest.raises(HTTPError) as error:
        request(server, '/api/state', token=token)
    assert error.value.code == 401


@pytest.mark.parametrize('path', ['/api/config', '/api/input'])
@pytest.mark.parametrize('failure', ['invalid', 'settings_open', 'expired'])
def test_failed_web_save_does_not_renew_session(configuration, monkeypatch, path, failure):
    import resources.lib.configuration.server as module
    now = [1000.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    server = configuration
    token = server.activate()
    original_expiry = server.expires
    now[0] = original_expiry - 10
    changes = {'max_seconds': 25} if path == '/api/config' else {'silence_ms': 700}
    if failure == 'invalid':
        changes = {'max_seconds': 0} if path == '/api/config' else {'silence_ms': 0}
    elif failure == 'settings_open':
        server.settings_open = lambda: True
    else:
        now[0] = original_expiry
    with pytest.raises(HTTPError) as error:
        pumped_request(server, path, {'settings': changes}, token)
    assert error.value.code == (401 if failure == 'expired' else 400)
    assert server.expires == original_expiry
    assert not server.test_writes


def test_web_polling_does_not_renew_session(configuration, monkeypatch):
    import resources.lib.configuration.server as module
    now = [1000.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    server = configuration
    token = server.activate()
    original_expiry = server.expires
    now[0] = original_expiry - 10
    request(server, '/api/state', token=token)
    assert server.expires == original_expiry


def test_input_autosave_is_restricted_and_applies_in_main_pump(configuration):
    server = configuration
    pumped_request(server, '/api/input', {'settings': {'voice_input': 'b25', 'silence_ms': 700}})
    assert server.settings.values()['voice_input'] == 'b25'
    assert server.settings.values()['silence_ms'] == 700 and server.input_changed
    assert set(server.test_writes) == {threading.current_thread().ident}
    for changes in [{'agent_backend': 'codex'}, {'silence_ms': 0}, {}, []]:
        with pytest.raises(HTTPError): pumped_request(server, '/api/input', {'settings': changes})
    assert server.settings.values()['agent_backend'] == 'off'


def test_input_change_and_binding_reject_active_capture_without_mutation(configuration):
    import fcntl
    server = configuration
    with (data_path(server.profile, 'capture.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for path in ['/api/input', '/api/input/bind', '/api/input/unbind']:
            with pytest.raises(HTTPError) as error:
                pumped_request(server, path, {'settings': {'voice_input': 'b25'}})
            assert error.value.code == 400
            assert json.load(error.value)['error'] == '请先关闭当前弹窗后再修改输入设备或绑定按键。'
    assert not server.test_writes and not (data_path(server.profile, 'voice-binding.json')).exists()


def test_web_binding_launches_same_tv_listener_and_reports_result(configuration):
    from resources.lib.voice_binding import update_binding
    server = configuration
    calls = []; server.builtin = calls.append
    pumped_request(server, '/api/input/bind', {})
    state = server.state()['binding']
    assert state['state'] == 'requested'
    assert calls == ['RunScript(script.voice.assistant,--bind-voice-key,--bind-request=' + state['id'] + ')']
    with pytest.raises(HTTPError): pumped_request(server, '/api/input/bind', {})
    update_binding(server.profile, state['id'], 'done', key='61588', target='z.xml')
    assert server.state()['binding']['target'] == 'z.xml'
    assert not server.test_writes


def test_web_removal_cleans_xml_and_reloads_without_changing_settings(configuration, monkeypatch):
    from resources.lib import voice_binding
    server = configuration
    keys = server.profile / 'keymaps'
    target = voice_binding.bind(keys, 61588)
    calls = []
    server.builtin = calls.append
    monkeypatch.setattr(voice_binding, 'keymap_directory', lambda: keys)
    result = pumped_request(server, '/api/input/unbind', {})
    assert not target.exists()
    assert server.state()['binding']['state'] == 'removed'
    assert calls == ['Action(ReloadKeymaps)'] and not server.test_writes


def test_web_removal_reports_open_kodi_settings_and_binding_errors(configuration, monkeypatch):
    from resources.lib import voice_binding
    server = configuration
    server.settings_open = lambda: True
    with pytest.raises(HTTPError) as error:
        pumped_request(server, '/api/input/unbind', {})
    assert '关闭 Kodi 插件设置窗口' in json.load(error.value)['error']
    server.settings_open = lambda: False
    voice_binding.begin_binding(server.profile)
    with pytest.raises(HTTPError) as error:
        pumped_request(server, '/api/input/unbind', {})
    assert '正在等待遥控按键' in json.load(error.value)['error']
