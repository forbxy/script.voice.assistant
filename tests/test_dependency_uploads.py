import hashlib
import http.client
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from resources.lib.configuration.server import Configuration
from resources.lib.dependencies import catalog as api
from resources.lib.dependencies.service import Manager
from resources.lib.dependencies.uploads import cache_path, locate, receive


def asset(name, data, target=None):
    return {'url': 'https://github.com/example/release/' + name, 'filename': name,
            'md5': hashlib.md5(data).hexdigest(), 'sha256': hashlib.sha256(data).hexdigest(),
            'bytes': len(data), 'expanded_bytes': len(data), 'format': 'raw', 'target': target or name}


@pytest.fixture
def packages(tmp_path):
    root, profile = tmp_path / 'addon', tmp_path / 'profile'
    profile.mkdir()
    first, second, other = b'server-one', b'host-two', b'unselected-model' * 4096
    rows = [asset('codex-server.bin', first, 'server'), asset('codex-host.bin', second, 'host')]
    model = asset('other-model.onnx', other, 'model.onnx')
    data = {'components': {'codex': {'version': '1', 'label': 'Codex fixture',
        'assets': rows, 'required': ['server', 'host']}, 'asr-other': {'version': '2', 'label': 'Other fixture',
        'assets': [model], 'required': ['model.onnx']}}, 'asr_models': {}}
    path = root / 'resources/config/components.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data))
    return root, profile, rows, model, (first, second, other)


def wait_done(manager):
    if manager.thread:
        manager.thread.join(4)
        assert not manager.thread.is_alive()


def test_upload_metadata_does_not_change_existing_component_receipts(packages):
    root, profile, rows, model, payloads = packages
    spec = api.specification(root, 'codex')
    original = dict(spec, assets=[{key: value for key, value in row.items() if key not in ('filename', 'md5')}
                                 for row in rows])
    assert api.revision(spec) == hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()[:16]
    changed = dict(spec, assets=[dict(rows[0], sha256='0' * 64), rows[1]])
    assert api.revision(changed) != api.revision(spec)


@pytest.mark.parametrize('filename', ['wrong.bin', '../codex-server.bin', '/codex-server.bin',
                                       'folder\\codex-server.bin', 'codex-server.bin\x00'])
def test_unknown_names_and_path_injection_are_rejected(packages, filename):
    root, profile, rows, model, payloads = packages
    with pytest.raises(ValueError):
        locate(root, filename)
    assert not list(profile.rglob('*.part'))


def test_streaming_has_bounded_reads_and_validates_both_hashes(packages):
    root, profile, rows, model, payloads = packages
    data = b'x' * (400 * 1024 + 13)
    row = asset('large.onnx', data)
    path = root / 'resources/config/components.json'
    manifest = json.loads(path.read_text())
    manifest['components']['asr-other']['assets'] = [row]
    path.write_text(json.dumps(manifest))
    class Bounded(io.BytesIO):
        def read(self, count=-1):
            assert 0 < count <= 128 * 1024
            return super().read(count)
    progress = []
    identifier, actual = receive(root, profile, row['filename'], len(data), Bounded(data), lambda: False,
        lambda done, total, speed: progress.append(done))
    assert identifier == 'asr-other' and actual == row
    assert cache_path(profile, row).read_bytes() == data
    assert len(progress) >= 4 and progress[-1] == len(data)
    assert cache_path(profile, row).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('mode', ['size', 'md5', 'sha256', 'short', 'cancel'])
def test_bad_or_cancelled_upload_is_never_committed(packages, mode):
    root, profile, rows, model, payloads = packages
    data = payloads[0]
    if mode == 'sha256':
        path = root / 'resources/config/components.json'
        manifest = json.loads(path.read_text());manifest['components']['codex']['assets'][0]['sha256'] = '0' * 64
        path.write_text(json.dumps(manifest))
    body = b'x' * len(data) if mode == 'md5' else data[:-1] if mode == 'short' else data
    with pytest.raises((ValueError, InterruptedError)) as error:
        receive(root, profile, rows[0]['filename'], len(data) - (mode == 'size'), io.BytesIO(body),
                lambda: mode == 'cancel', lambda *a: None)
    if mode in ('md5', 'sha256'):
        assert mode.upper() in str(error.value)
    assert not list(profile.rglob('*.part')) and not list(profile.rglob('*.uploading-*'))


def test_multi_asset_upload_waits_then_installs_offline_without_changing_selection(packages, monkeypatch):
    root, profile, rows, model, payloads = packages
    def network(*args, **kwargs):
        pytest.fail('Offline upload must not use the network')
    monkeypatch.setattr('resources.lib.dependencies.download.urlopen', network)
    manager = Manager(root, profile)
    try:
        manager.update({'recognition_engine': 'off', 'agent_backend': 'off'})
        first = manager.upload(rows[0]['filename'], len(payloads[0]), io.BytesIO(payloads[0]))
        assert first['pending_files'] == [rows[1]['filename']]
        state = manager.state(manager.values)
        assert state['job']['state'] == 'waiting_files' and not state['components']
        assert any(row['filename'] == rows[0]['filename'] and row['cached'] for row in state['upload_files'])
        assert not api.installed(root, profile, 'codex')
        manager.upload(rows[1]['filename'], len(payloads[1]), io.BytesIO(payloads[1]))
        wait_done(manager)
        assert manager.job['state'] == 'done'
        assert api.installed(root, profile, 'codex')
        manager.upload(model['filename'], len(payloads[2]), io.BytesIO(payloads[2]))
        wait_done(manager)
        assert api.installed(root, profile, 'asr-other')
        assert manager.values == {'recognition_engine': 'off', 'agent_backend': 'off'}
        assert not list(profile.glob('components/.downloads/*.part'))
        before = api.require(root, profile, 'codex') / 'installed.json'
        modified = before.stat().st_mtime_ns
        manager.upload(rows[0]['filename'], len(payloads[0]), io.BytesIO(payloads[0]))
        assert before.stat().st_mtime_ns == modified  # Never replace an already active component.
    finally:
        manager.close()


def test_failed_retry_does_not_destroy_a_previously_verified_file(packages):
    root, profile, rows, model, payloads = packages
    receive(root, profile, rows[0]['filename'], len(payloads[0]), io.BytesIO(payloads[0]),
            lambda: False, lambda *args: None)
    with pytest.raises(ValueError, match='MD5'):
        receive(root, profile, rows[0]['filename'], len(payloads[0]), io.BytesIO(b'x' * len(payloads[0])),
                lambda: False, lambda *args: None)
    assert cache_path(profile, rows[0]).read_bytes() == payloads[0]


def test_upload_and_online_install_are_mutually_exclusive_and_cancellable(packages):
    root, profile, rows, model, payloads = packages
    manager = Manager(root, profile)
    began, release = threading.Event(), threading.Event()
    errors = []
    class Blocked(io.BytesIO):
        def read(self, length):
            began.set();release.wait(3)
            return super().read(length)
    def work():
        try: manager.upload(rows[0]['filename'], len(payloads[0]), Blocked(payloads[0]))
        except Exception as error: errors.append(error)
    thread = threading.Thread(target=work)
    try:
        thread.start();assert began.wait(2)
        manager.start({'agent_backend': 'codex'})
        assert manager.thread is None
        with pytest.raises(ValueError, match='正在处理'):
            manager.upload(model['filename'], len(payloads[2]), io.BytesIO(payloads[2]))
        manager.cancel();release.set();thread.join(3)
        assert errors and isinstance(errors[0], InterruptedError)
        assert manager.job['state'] == 'cancelled' and not manager.receiving
        assert not list(profile.rglob('*.part'))
    finally:
        release.set();thread.join(4);manager.close()


def post_upload(server, name, data, token=None, **headers):
    connection = http.client.HTTPConnection('127.0.0.1', server.http.server_port, timeout=5)
    try:
        fields = {'Content-Type': 'application/octet-stream', **headers}
        if token is not None: fields['Authorization'] = 'Bearer ' + token
        connection.request('POST', '/api/dependencies/upload?filename=' + quote(name, safe=''), data, fields)
        result = connection.getresponse()
        return result.status, json.loads(result.read())
    finally:
        connection.close()


def test_http_binary_upload_is_authenticated_and_independent_of_selected_modules(packages, monkeypatch):
    root, profile, rows, model, payloads = packages
    path = root / 'resources/config/components.json'
    data = json.loads(path.read_text())
    data['components']['codebuddy'] = {'version': '1', 'label': 'CodeBuddy fixture',
        'assets': [asset('codebuddy.bin', b'codebuddy')], 'required': ['codebuddy.bin']}
    path.write_text(json.dumps(data))
    api.catalog.cache_clear()
    monkeypatch.setattr(Configuration, 'job', lambda *args: {'job': 'fixture'})
    server = Configuration(root, profile, SimpleNamespace(getSetting=lambda key: ''), lambda *args: None, port=0)
    try:
        assert post_upload(server, model['filename'], payloads[2])[0] == 401
        token = server.activate()
        assert post_upload(server, model['filename'], payloads[2], token, Origin='http://evil.test')[0] == 401
        assert post_upload(server, '../bad', b'x', token)[0] == 400
        assert post_upload(server, model['filename'], payloads[2], token, **{'Content-Type': 'application/json'})[0] == 400
        assert post_upload(server, model['filename'], b'x' * len(payloads[2]), token)[0] == 400
        previous = server.expires
        status, result = post_upload(server, model['filename'], payloads[2], token)
        assert status == 200 and result['ok'] and server.expires > previous
        wait_done(server.dependencies)
        assert api.installed(root, profile, 'asr-other')
        assert server.settings.get('recognition_engine') == 'off'
        server.expires = time.monotonic() - 1
        assert post_upload(server, rows[0]['filename'], payloads[0], token)[0] == 401
    finally:
        server.close()
