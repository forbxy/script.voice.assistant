import base64
from email.parser import BytesParser
from email.policy import default
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave

import pytest

from resources.lib import codex_dictation as api
from resources.lib.codex_dictation_recognizer import CodexDictationRecognizer
from legacy import engines


def status(account='test-account', **values):
    claim = base64.urlsafe_b64encode(json.dumps({
        'https://api.openai.com/auth': {'chatgpt_account_id': account}}).encode()).decode().rstrip('=')
    return dict({'authMethod': 'chatgpt', 'authToken': 'header.' + claim + '.signature'}, **values)


def test_subscription_only_auth_and_no_secret_in_errors():
    headers = api.authorization(status())
    assert headers['ChatGPT-Account-Id'] == 'test-account'
    assert headers['originator'] == 'codex_vscode'
    for value in (None, {}, status(authMethod='apiKey'), status(authToken='secret\nheader'),
                  status(account='injected\r\n'), status(authToken='private-invalid-token')):
        with pytest.raises(api.DictationError) as error:
            api.authorization(value)
        assert 'secret' not in str(error.value) and 'private' not in str(error.value)


def test_multipart_contains_only_file_and_preserves_exact_pcm():
    pcm = bytes(range(256)) * 70
    body, content_type = api.multipart_pcm(pcm)
    message = BytesParser(policy=default).parsebytes(
        ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
    parts = list(message.iter_parts())
    assert len(parts) == 1
    assert parts[0].get_param('name', header='content-disposition') == 'file'
    assert parts[0].get_content_type() == 'audio/wav'
    with wave.open(io.BytesIO(parts[0].get_payload(decode=True))) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        assert audio.readframes(audio.getnframes()) == pcm
    for bad in (b'', b'odd', bytes(api.MAX_PCM + 2)):
        with pytest.raises(api.DictationError):
            api.multipart_pcm(bad)


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self, results):
        self.results, self.requests = list(results), []
    def open(self, request, timeout):
        self.requests.append(request)
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(value)


def http_error(code):
    return urllib.error.HTTPError(api.ENDPOINT, code, 'private-secret-body', {}, io.BytesIO(b'secret'))


def test_401_refreshes_once_and_uses_fixed_url_and_same_audio():
    opener = Opener([http_error(401), b'{"text":"whole sentence"}'])
    headers = api.authorization(status())
    refreshed = dict(headers, Authorization='Bearer refreshed')
    calls = []
    def refresh():
        calls.append(True)
        return refreshed
    assert api.transcribe(bytes(3200), headers, refresh, opener=opener) == 'whole sentence'
    assert calls == [True]
    assert all(r.full_url == api.ENDPOINT and r.method == 'POST' for r in opener.requests)
    assert opener.requests[0].data == opener.requests[1].data
    assert opener.requests[1].get_header('Authorization') == 'Bearer refreshed'
    opener = Opener([http_error(401)])
    with pytest.raises(api.DictationError, match='账号已改变'):
        api.transcribe(bytes(3200), headers, lambda: api.authorization(status('other-account')), opener=opener)
    assert len(opener.requests) == 1


@pytest.mark.parametrize('result', [http_error(401), http_error(403), http_error(429), http_error(302),
    TimeoutError('secret'), urllib.error.URLError('secret'), b'bad-secret-json',
    b'[]', b'{"text":123}', json.dumps({'text': 'x' * 4001}).encode(), bytes(api.MAX_RESPONSE + 1)])
def test_provider_errors_and_oversize_results_are_bounded_and_sanitized(result):
    headers = api.authorization(status())
    opener = Opener([result, http_error(401)])
    with pytest.raises(api.DictationError) as error:
        api.transcribe(bytes(3200), headers, lambda: headers, opener=opener)
    assert 'secret' not in str(error.value)


def test_redirect_handler_never_forwards_credentials():
    request = urllib.request.Request(api.ENDPOINT, headers={'Authorization': 'Bearer test'})
    assert api.NoRedirect().redirect_request(request, None, 302, '', {}, 'https://other.example') is None


def test_managed_auth_uses_native_rpc_and_closes_connection(monkeypatch):
    calls = []
    class Account:
        def __init__(self, *args, **kwargs):
            assert kwargs['proxy'] == ''
            assert kwargs['connection_factory'] is api.DictationConnection
            self.connection = self
            self.user_agent = 'codex_vscode/test (Linux; aarch64)'
        def __enter__(self):
            return self
        def request(self, method, params):
            calls.append((method, params))
            return status()
        def __exit__(self, *args):
            calls.append('closed')
    monkeypatch.setattr(api, 'CodexAccount', Account)
    headers = api.managed_headers('/addon', '/profile', 30, refresh=True)
    assert headers['ChatGPT-Account-Id'] == 'test-account'
    assert headers['User-Agent'] == 'codex_vscode/test (Linux; aarch64)'
    assert calls == [('getAuthStatus', {'includeToken': True, 'refreshToken': True}), 'closed']


def test_dictation_identity_is_child_only_and_uses_native_user_agent(monkeypatch):
    calls = []
    original_environment = dict(os.environ)
    monkeypatch.setattr(api.Connection, '__init__', lambda self, *args, **kwargs: calls.append((args, kwargs)))
    def request(self, method, params):
        calls.append((method, params))
        return {'userAgent': 'codex_vscode/native-test (Linux; aarch64)'}
    monkeypatch.setattr(api.Connection, 'request', request)
    command = ['/native/codex', 'app-server']
    connection = api.DictationConnection(command, '', '/work', lambda: False, 30, '/home', '')
    params = {'clientInfo': {'name': 'coreelec_voice_assistant'}, 'capabilities': {'experimentalApi': True}}
    connection.request('initialize', params)
    assert calls[0][0][0] == ['env', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE=codex_vscode'] + command
    assert calls[0][1] == {'start_new_session': False}
    assert calls[1][1]['clientInfo'] == api.DICTATION_CLIENT
    assert params['clientInfo']['name'] == 'coreelec_voice_assistant'
    assert command == ['/native/codex', 'app-server'] and dict(os.environ) == original_environment
    headers = api.authorization(status(), connection.user_agent)
    assert headers['User-Agent'] == 'codex_vscode/native-test (Linux; aarch64)'
    for value in ('bad\r\nprivate', '', None, 'x' * 1025):
        with pytest.raises(api.DictationError) as error:
            api.authorization(status(), value)
        assert 'private' not in str(error.value)


def test_engine_selection_is_independent_of_agent_and_hotwords(monkeypatch):
    monkeypatch.setattr(engines, 'CodexDictationRecognizer', lambda *args: args)
    stop = lambda: False
    result = engines.create_recognizer('/addon', engine='codex', profile='/profile',
        hotwords_file='/must-not-read', hotword_entries=[('扣的', 'Kodi')], cancelled=stop,
        codex_dictation_timeout=15, codex_proxy='http://localhost:1234')
    assert result == ('/addon', '/profile', 15, stop, 'http://localhost:1234')


@pytest.mark.parametrize('phase', ['auth', 'http'])
def test_cancellation_kills_worker_and_inherited_auth_child(tmp_path, phase):
    worker = tmp_path / 'worker.py'
    worker.write_text('''import json,os,subprocess,sys,time
config=json.loads(sys.stdin.buffer.readline())
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
open(sys.argv[2],'w').write(str(child.pid))
if sys.argv[1]=='auth':time.sleep(60)
print(json.dumps({'type':'ready'}),flush=True)
time.sleep(60)
''')
    pidfile = tmp_path / 'child.pid'
    processes = []
    def spawn(args, **kwargs):
        assert kwargs['start_new_session']
        assert 'Bearer' not in repr(args)
        process = subprocess.Popen([sys.executable, '-u', str(worker), phase, str(pidfile)], **kwargs)
        processes.append(process)
        return process
    stop = threading.Event()
    timer = threading.Timer(.3, stop.set)
    timer.start()
    began = time.monotonic()
    try:
        with pytest.raises(InterruptedError):
            with CodexDictationRecognizer('/addon', tmp_path, cancelled=stop.is_set,
                                          process_factory=spawn) as recognizer:
                recognizer.accept(bytes(3200))
                recognizer.finish()
    finally:
        timer.join()
    assert time.monotonic() - began < 1.5
    assert all(process.poll() is not None for process in processes)
    child = int(pidfile.read_text())
    # An orphan can briefly remain as a zombie until the container init reaps it.
    for unused in range(20):
        stat = Path('/proc') / str(child) / 'stat'
        if not stat.exists() or stat.read_text().split()[2] == 'Z':
            break
        time.sleep(.02)
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail('Auth child survived cancellation')
