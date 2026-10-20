import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from resources.lib.proxy import configured_proxy, validate_proxy
from resources.lib.settings import Settings, get_proxy
from resources.lib import codex_dictation as dictation
from resources.lib import http_proxy as proxy_api


@pytest.mark.parametrize('password', ['p@ss:/% #?中文', '', '  keep these spaces  '])
def test_separate_credentials_preserve_literal_characters_and_address(password):
    stored = {}
    config = Settings(SimpleNamespace(getSetting=lambda k: stored.get(k, ''),
        setSetting=lambda k, v: stored.update({k: v})))
    values = config.apply({'codex_proxy': 'http://[::1]:7890',
        'codex_proxy_username': '用户@proxy', 'codex_proxy_password': password})
    assert stored['v2_codex_proxy'] == 'http://[::1]:7890'
    assert stored['v2_codex_proxy_password'] == password
    expected_user = '%E7%94%A8%E6%88%B7%40proxy'
    proxy = configured_proxy(values)
    assert urlsplit(proxy).hostname == '::1' and expected_user in proxy
    assert get_proxy(config.addon) == proxy and validate_proxy(proxy) == proxy
    config.apply({'agent_timeout': 90})
    assert get_proxy(config.addon) == proxy
    with pytest.raises(ValueError):
        config.apply({'codex_proxy': proxy})


@pytest.mark.parametrize('changes', [
    {'codex_proxy_username': 'invalid:user'},
    {'codex_proxy_password': 'line\nbreak'},
    {'codex_proxy_password': 'password-without-user'},
])
def test_invalid_proxy_credentials_do_not_partially_save(changes):
    stored = {'v2_codex_proxy': 'http://localhost:7890'}
    original = dict(stored)
    config = Settings(SimpleNamespace(getSetting=lambda k: stored.get(k, ''),
        setSetting=lambda k, v: stored.update({k: v})))
    with pytest.raises(ValueError):
        config.apply(dict(changes, agent_timeout=90))
    assert stored == original


def test_bypassing_proxy_does_not_attach_proxy_credentials_to_destination(monkeypatch):
    from urllib.request import Request
    monkeypatch.setattr(proxy_api, 'proxy_bypass', lambda host: True)
    request = Request('https://chatgpt.com/backend-api/transcribe')
    handler = proxy_api.AuthenticatedProxyHandler({'https': 'http://user:secret@localhost:7890'})
    assert handler.proxy_open(request, 'http://user:secret@localhost:7890', 'https') is None
    assert not request.has_header('Proxy-Authorization')


@pytest.mark.parametrize('password', ['p@ss:/% #?中文', ''])
def test_dictation_sends_basic_auth_only_in_connect_not_account_headers(password, monkeypatch):
    requests = []
    class Proxy(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            requests.append((self.path, dict(self.headers)))
            self.send_response(407)
            self.send_header('Proxy-Authenticate', 'Basic realm="test"')
            self.end_headers()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # A developer's NO_PROXY settings must not bypass this loopback proxy test.
    monkeypatch.setenv('no_proxy', '')
    monkeypatch.setenv('NO_PROXY', '')
    proxy = configured_proxy({'codex_proxy': 'http://127.0.0.1:' + str(server.server_port),
        'codex_proxy_username': '测试@user', 'codex_proxy_password': password})
    try:
        with pytest.raises(dictation.DictationError) as error:
            dictation.transcribe(bytes(3200), {'Authorization': 'Bearer private-account-token',
                'ChatGPT-Account-Id': 'private-account'}, lambda: {}, timeout=2, proxy=proxy)
        if password:
            assert password not in str(error.value)
        assert len(requests) == 1
        path, headers = requests[0]
        assert path == 'chatgpt.com:443'
        expected = base64.b64encode(('测试@user:' + password).encode()).decode()
        assert headers['Proxy-Authorization'] == 'Basic ' + expected
        assert 'private-account' not in json.dumps(headers)
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
