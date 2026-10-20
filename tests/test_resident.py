from resources.lib.data_paths import data_path
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from resources.lib.intelligence import resident as api
from resources.lib.settings import FIELDS
from resources.lib.storage import write_json, read_json


def test_owner_reuses_process_and_restarts_for_configuration_or_stop(tmp_path, monkeypatch):
    binary = tmp_path / 'resources/native/linux_aarch64/codex/codex-app-server'
    binary.parent.mkdir(parents=True);binary.write_text('fake')
    profile = tmp_path / 'profile';profile.mkdir()
    launched, killed = [], []
    class Process:
        def __init__(self, command, **kwargs):
            self.pid = 90000 + len(launched)
            self.dead = False
            launched.append((command, kwargs))
            (data_path(profile, 'codex.sock')).touch()
        def poll(self):return 0 if self.dead else None
        def wait(self, timeout):self.dead = True;return 0
    monkeypatch.setattr(api.subprocess, 'Popen', Process)
    monkeypatch.setattr(api, 'codex_binary', lambda *args: binary)
    monkeypatch.setattr(api.os, 'killpg', lambda pid, sig: killed.append(pid))
    values = {name: field.default for name, field in FIELDS.items()}
    values.update(permission_level='full', agent_backend='codex')
    manager = api.Resident(tmp_path, profile)
    manager.preload_at = 0
    try:
        manager.tick(values)
        first = read_json(manager.state_path, {})
        manager.next_check = 0;manager.tick(values)
        assert len(launched) == 1
        assert first == read_json(manager.state_path, {})
        assert launched[0][1]['start_new_session']
        assert 'unix://' + str(data_path(profile, 'codex.sock')) in launched[0][0]
        assert (data_path(profile, 'codex.sock')).stat().st_mode & 0o777 == 0o600
        lease = (data_path(profile, 'codex-resident.lock')).open('a')
        api.fcntl.flock(lease, api.fcntl.LOCK_EX)
        changed = dict(values, codex_chatgpt_effort='low')
        manager.next_check = 0;manager.tick(changed)
        assert len(launched) == 1  # No interrupting work for a settings change.
        # A stop request must work even while the task holds the lease.
        write_json(manager.stop_path, {'nonce': first['nonce']})
        manager.tick(values)
        assert killed and manager.process is None
        lease.close()
        manager.next_check = 0;manager.tick(changed)
        assert len(launched) == 2
        assert read_json(manager.state_path, {})['nonce'] != first['nonce']
        manager.next_check = 0;manager.tick(values)
        assert len(launched) == 3
        monkeypatch.setattr(api, 'load_key', lambda *args: 'test-key')
        api_values = dict(values, codex_provider='api', agent_code_mode='off')
        manager.next_check = 0;manager.tick(api_values)
        assert 'features.code_mode=false' in launched[-1][0]
        count = len(launched)
        api_values['agent_code_mode'] = 'on'
        manager.next_check = 0;manager.tick(api_values)
        assert len(launched) == count + 1
        assert 'features.code_mode=true' in launched[-1][0]
        credentials = dict(api_values, codex_proxy='http://localhost:7890',
                           codex_proxy_username='proxy-user', codex_proxy_password='p@ss:word')
        manager.next_check = 0;manager.tick(credentials)
        assert launched[-1][1]['env']['HTTPS_PROXY'] == 'http://proxy-user:p%40ss%3Aword@localhost:7890'
        count = len(launched)
        credentials['codex_proxy_password'] = 'changed'
        manager.next_check = 0;manager.tick(credentials)
        assert len(launched) == count + 1
    finally:manager.close()
    assert not manager.state_path.exists() and not manager.socket_path.exists()


def test_resident_transport_releases_completed_turn_and_stops_failed_turn(tmp_path, monkeypatch):
    command = ['/native', '--listen', 'stdio://']
    profile = tmp_path
    state = {'nonce': 'instance-one', 'pid': 123,
             'signature': api.fingerprint(command, '', profile, data_path(profile, 'codex-home'), '')}
    path = data_path(profile, 'codex-resident.json');write_json(path, state)
    class Timeout(Exception):pass
    class Failure(Exception):pass
    class Raw:
        def settimeout(self, value):pass
        def connect(self, path):pass
        def close(self):pass
    class Websocket:
        def __init__(self):self.messages = [];self.closed = False
        def settimeout(self, value):pass
        def send(self, data):
            value = json.loads(data)
            if value.get('method') == 'thread/unsubscribe':
                self.messages.append(json.dumps({'id': value['id'], 'result': {'status': 'unsubscribed'}}))
        def recv(self):return self.messages.pop(0)
        def close(self, **kwargs):self.closed = True
    sock = Websocket()
    monkeypatch.setattr(api.socket, 'socket', lambda *args: Raw())
    monkeypatch.setattr(api, 'websocket_module', lambda root: SimpleNamespace(
        create_connection=lambda *a, **kw: sock, WebSocketTimeoutException=Timeout, WebSocketException=Failure))
    def connect():return api.ResidentConnection('/addon', profile, command, '', profile,
                                               lambda: False, 30, data_path(profile, 'codex-home'))
    client = connect()
    sock.messages.append(json.dumps({'method': 'turn/completed', 'params': {
        'threadId': 'thread-a', 'turn': {'id': 'turn-a', 'status': 'completed'}}}))
    client.read();client.close('thread-a', 'turn-a')
    assert sock.closed and path.exists()
    assert not (data_path(profile, 'codex-resident-stop.json')).exists()
    calls = []
    def owner_stop(target, value):
        calls.append((target.name, value));path.unlink()
    monkeypatch.setattr(api, 'write_json', owner_stop)
    client = connect();client.close('thread-b', 'turn-b')
    assert calls == [('codex-resident-stop.json', {'nonce': 'instance-one'})]


def test_configuration_fingerprint_excludes_plaintext_secret_and_includes_ceiling():
    command = ['native', '-c', 'features.shell_tool=true']
    original = api.fingerprint(command, 'secret-one', '/work', '/home', '')
    assert 'secret' not in original
    assert original != api.fingerprint(command, 'secret-two', '/work', '/home', '')
    assert original != api.fingerprint(['native', '-c', 'features.shell_tool=false'], 'secret-one', '/work', '/home', '')


def test_stop_reaps_terminal_in_its_own_session(tmp_path):
    marker = tmp_path / 'child.pid'
    child_code = 'import os,signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path(' + repr(str(marker)) + ').write_text(str(os.getpid()));time.sleep(30)'
    parent_code = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",' + repr(child_code) + '],start_new_session=True);time.sleep(30)'
    manager = api.Resident(tmp_path, tmp_path)
    process = subprocess.Popen([sys.executable, '-c', parent_code], start_new_session=True)
    manager.process = process
    child = None
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:time.sleep(.01)
        child = int(marker.read_text())
        assert os.getpgid(child) != os.getpgid(process.pid)
        manager._stop()
        stat = Path('/proc', str(child), 'stat')
        deadline = time.monotonic() + 2
        while stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z' and time.monotonic() < deadline:time.sleep(.01)
        assert not stat.exists() or stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
        assert process.poll() is not None
    finally:
        manager.close()
        if child:
            try:os.kill(child, signal.SIGKILL)
            except ProcessLookupError:pass
