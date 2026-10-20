"""Exercise actual Unix descriptor leases with a local, offline CLI fixture."""
import json
from pathlib import Path
import threading
import time

import pytest

from resources.lib import codebuddy_resident as resident
from resources.lib.agent_tools import KodiTools
from resources.lib.codebuddy_agent import CodeBuddyAgent
from resources.lib.codebuddy_protocol import command
from resources.lib.codebuddy_regions import invalidate_auth
from resources.lib.intelligence.errors import AgentError
from resources.lib.intelligence.permissions import Permissions
from resources.lib.intelligence.sessions import Sessions

ROOT = Path(__file__).resolve().parents[1]


def test_cancel_while_acquiring_standby_never_starts_cold_fallback(tmp_path, monkeypatch):
    def cancelled(*args, **kwargs):
        raise InterruptedError('cancelled')
    def unexpected(*args, **kwargs):
        pytest.fail('Cancellation must not launch a replacement native process')
    monkeypatch.setattr(resident, 'WarmConnection', cancelled)
    monkeypatch.setattr(resident, 'Connection', unexpected)
    with pytest.raises(InterruptedError):
        resident.connection_factory(ROOT, tmp_path)([], tmp_path)

# No network, credentials or machine data are accessed by this executable.
NATIVE = r'''#!/usr/bin/env python3
import json,os,socket,sys,time
from pathlib import Path
if '--prewarm' in sys.argv:
    time.sleep(float(os.environ.get('TEST_BUDDY_PREWARM_DELAY', '0')))
    identity=sys.argv[sys.argv.index('--prewarm-id')+1]
    path=Path('/tmp/codebuddy-prewarm-'+identity+'.sock')
    server=socket.socket(socket.AF_UNIX);server.bind(str(path));server.listen(1)
    while True:
        peer,_=server.accept()
        with peer:
            msg=json.loads(peer.makefile('rb').readline())
            peer.sendall((json.dumps({'ok':True,'status':'idle'})+'\n').encode())
        if msg['cmd']=='activate':break
    server.close();path.unlink(missing_ok=True)
def send(value): print(json.dumps(value),flush=True)
initialized=turns=0
for row in sys.stdin:
    value=json.loads(row)
    if value['type']=='control_request':
        initialized+=1
        send({'type':'control_response','response':{'subtype':'success',
            'request_id':value['request_id'],'response':{'account':{'userId':'fixture','token':'private-fixture'},
            'currentModelId':'fixture'}}})
    elif value['type']=='user':
        turns+=1
        if value['message']['content'].endswith('volume'):
            send({'type':'control_request','request_id':'fixture-tool','request':{'subtype':'mcp_message',
                'server_name':'kodi','message':{'jsonrpc':'2.0','id':1,'method':'tools/call',
                'params':{'name':'set_volume','arguments':{'volume':20}}}}})
            json.loads(sys.stdin.readline())
        elif value['message']['content'].endswith('exit'):
            sys.exit(0)
        send({'type':'result','session_id':'fixture-'+str(os.getpid()),
            'result':json.dumps({'pid':os.getpid(),'initialized':initialized,'turns':turns})})
'''


def wait_for(check, seconds=4):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(.02)
    pytest.fail('CodeBuddy fixture did not reach the expected state')


@pytest.fixture
def warm(tmp_path, monkeypatch):
    binary = tmp_path / 'codebuddy-headless'
    binary.write_text(NATIVE)
    binary.chmod(0o755)
    monkeypatch.setattr(resident, 'preload_delay', lambda: 0)
    monkeypatch.setattr(resident, 'command', lambda root, profile, model, native:
                        command(root, profile, model, native, binary=binary))
    owner = resident.Resident(ROOT, tmp_path)
    values = {'agent_backend': 'codebuddy', 'codebuddy_region': 'external', 'codebuddy_model': 'fixture',
              'permission_level': 'settings'}
    owner.tick(values)
    def agent(sessions=None, calls=None, **kwargs):
        tools = KodiTools(lambda *args: calls.append(args) if calls is not None else None)
        return CodeBuddyAgent(ROOT, tmp_path, tools, model='fixture', binary=binary, sessions=sessions,
            connection_factory=resident.connection_factory(ROOT, tmp_path), **kwargs)
    try:
        yield owner, values, agent
    finally:
        owner.close()
        assert not owner.thread.is_alive()


def test_prewarm_handoff_and_followups_share_native_but_not_host_callbacks(warm, tmp_path):
    owner, values, agent = warm
    wait_for(lambda: owner.status['state'] == 'ready')
    standby_pid = owner.status['pid']
    startup = owner.connection.process.args
    assert '--input-format=stream-json' in startup and '--strict-mcp-config' in startup
    assert startup[startup.index('--model') + 1] == 'fixture'
    assert startup[startup.index('--tools') + 1] == 'AskUserQuestion'
    assert (Path(resident.address(tmp_path)).stat().st_mode & 0o777) == 0o600
    session, first_calls, next_calls = Sessions(tmp_path), [], []
    try:
        old_stop = threading.Event()
        first = json.loads(agent(session, first_calls).run('volume', cancelled=old_stop.is_set)['reply'])
        assert first == {'pid': standby_pid, 'initialized': 1, 'turns': 1}
        assert isinstance(session.codebuddy[1], resident.WarmConnection)
        # run_turn sets this event in its finally block, after a healthy result.
        old_stop.set()
        next_reply = json.loads(agent(session, next_calls).run('volume')['reply'])
        assert next_reply == {'pid': standby_pid, 'initialized': 1, 'turns': 2}
        assert first_calls == next_calls == [('Application.SetVolume', {'volume': 20})]
    finally:
        session.close()
    assert not Path('/proc', str(standby_pid)).exists()
    wait_for(lambda: owner.status['state'] == 'ready')
    assert owner.status['pid'] != standby_pid
    # A new window has a different native process and no previous conversation.
    following = Sessions(tmp_path)
    try:
        reply = json.loads(agent(following).run('new')['reply'])
        assert reply['turns'] == 1 and reply['pid'] != standby_pid
    finally:
        following.close()


def test_stop_task_destroys_cached_native_and_next_turn_reconnects(warm, tmp_path):
    owner, values, agent = warm
    session = Sessions(tmp_path)
    try:
        first = json.loads(agent(session).run('first')['reply'])
        stop = threading.Event()
        def progress(text):
            if '正在理解' in text:
                stop.set()
        with pytest.raises(InterruptedError):
            agent(session).run('cancelled', cancelled=stop.is_set, progress=progress)
        assert session.codebuddy is None
        assert not Path('/proc', str(first['pid'])).exists()
        reply = json.loads(agent(session).run('again')['reply'])
        assert reply['pid'] != first['pid'] and reply['turns'] == 1
    finally:
        session.close()


def test_rotation_permission_prompt_and_account_changes_close_retained_connection(warm, tmp_path):
    owner, values, agent = warm
    session = Sessions(tmp_path)
    try:
        first = json.loads(agent(session).run('first')['reply'])
        assert session.refresh(signature='other')
        assert session.codebuddy is None and not Path('/proc', str(first['pid'])).exists()
        previous = json.loads(agent(session).run('next')['reply'])
        invalidate_auth(tmp_path, 'external')
        changed = json.loads(agent(session).run('new account revision')['reply'])
        assert changed['pid'] != previous['pid']
        # Approval strategy changes must not retain a connection with old policy.
        previous = changed
        changed = json.loads(agent(session, permissions=Permissions('settings', 'full_access')).run('permissions')['reply'])
        assert changed['pid'] != previous['pid']
        previous = changed
        changed = json.loads(agent(session).run('speech schema', speech=lambda *args: None)['reply'])
        assert changed['pid'] != previous['pid']
    finally:
        session.close()


def test_warm_replacement_on_configuration_change_and_off(warm):
    owner, values, agent = warm
    wait_for(lambda: owner.status['state'] == 'ready')
    pid = owner.status['pid']
    owner.tick(dict(values, codebuddy_region='internal'))
    wait_for(lambda: owner.status['state'] == 'ready' and owner.status['pid'] != pid)
    assert not Path('/proc', str(pid)).exists()
    pid = owner.status['pid']
    owner.tick(dict(values, agent_backend='off'))
    wait_for(lambda: owner.status['state'] == 'off')
    assert not Path('/proc', str(pid)).exists()


def test_native_crash_does_not_replay_a_task(warm, tmp_path):
    owner, values, agent = warm
    session = Sessions(tmp_path)
    try:
        first = json.loads(agent(session).run('first')['reply'])
        with pytest.raises(AgentError):
            agent(session).run('exit')
        assert session.codebuddy is None and not Path('/proc', str(first['pid'])).exists()
        assert json.loads(agent(session).run('retry')['reply'])['turns'] == 1
    finally:
        session.close()


def test_cancel_during_prewarm_and_shutdown_release_processes(warm, tmp_path, monkeypatch):
    owner, values, agent = warm
    owner.tick(dict(values, agent_backend='off'))
    wait_for(lambda: owner.status['state'] == 'off')
    monkeypatch.setenv('TEST_BUDDY_PREWARM_DELAY', '2')
    owner.tick(values)
    wait_for(lambda: owner.status['state'] == 'warming')
    pid = owner.status['pid']
    stop = threading.Event()
    timer = threading.Timer(.2, stop.set)
    timer.start()
    try:
        with pytest.raises(InterruptedError):
            agent(Sessions(tmp_path)).run('cancel before activation', cancelled=stop.is_set)
    finally:
        timer.join()
    wait_for(lambda: not Path('/proc', str(pid)).exists())


def test_early_request_starts_before_preload_time_and_missing_service_falls_back(warm, tmp_path):
    owner, values, agent = warm
    owner.close()
    cold = Sessions(tmp_path)
    try:
        reply = json.loads(agent(cold).run('fallback')['reply'])
        assert not isinstance(cold.codebuddy[1], resident.WarmConnection)
    finally:
        cold.close()
    # A restarted owner has not reached its automatic preloading deadline.
    second = resident.Resident(ROOT, tmp_path)
    second.preload_at = time.monotonic() + 60
    second.tick(values)
    early = Sessions(tmp_path)
    try:
        reply = json.loads(agent(early).run('early request')['reply'])
        assert isinstance(early.codebuddy[1], resident.WarmConnection)
        assert reply['turns'] == 1
    finally:
        early.close()
        second.close()
