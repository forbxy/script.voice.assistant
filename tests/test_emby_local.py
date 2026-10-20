"""Loopback transport, verified startup, resume/restart and failure recovery."""
from types import SimpleNamespace
import threading

import pytest

from resources.lib.intelligence import emby_local, playback_tools


@pytest.fixture
def local_env(monkeypatch):
    now, calls = [0.], []
    flags = {'path': 'http://127.0.0.1:57342/dynamic/http/tvshows/srv/0/0/32/metadata/episode.mkv',
             'active': True, 'failed': False, 'cancelled': False, 'position': 120., 'canseek': True}
    item = dict(_emby_server='srv', _emby_id='32')
    actual = dict(UserData={'PlaybackPositionTicks': 1200000000})
    ready, failed = threading.Event(), threading.Event()
    ready.set()
    monkeypatch.setattr(playback_tools, 'playback_watcher', lambda: SimpleNamespace(ready=ready, failed=failed))
    monkeypatch.setattr(emby_local, 'send', lambda *args: calls.append(('local', args[:2])))
    monkeypatch.setattr(emby_local, 'service_ready', lambda cancelled: None)
    def rpc(method, params):
        calls.append((method, params))
        if method == 'Player.GetActivePlayers':
            return [{'playerid': 1, 'type': 'video'}] if flags['active'] and now[0] > .1 else []
        if method == 'Player.GetItem':return {'item': {'file': flags['path']}}
        if method == 'Player.GetProperties':
            return {'speed': 1, 'time': {'seconds': flags['position']}, 'canseek': flags['canseek']}
        if method == 'Player.Seek':flags['position'] = emby_local.seconds(params['value']['time']);return 'OK'
        raise AssertionError(method)
    def wait(seconds):
        now[0] += seconds
        if flags['failed']:failed.set()
    def play(resume=True):
        return emby_local.play(item, actual, resume, rpc, lambda: flags['cancelled'], wait,
                               clock=lambda: now[0], timeout=3)
    return SimpleNamespace(play=play, flags=flags, calls=calls, item=item, actual=actual, ready=ready, now=now,
                           rpc=rpc, wait=wait)


@pytest.mark.parametrize('resume', [True, False])
def test_multi_version_uses_one_source_and_keeps_resume_confirmation(local_env, resume):
    e = local_env
    e.item.update(media_type='episode', title='测试剧集')
    e.actual['MediaSources'] = [dict(Id='first', Path='http://media.invalid/a.mkv', Size=100),
                                dict(Id='largest', Path='http://media.invalid/b.mkv', Size=200)]
    def rpc(method, params):
        if method == 'Addons.GetAddonDetails':
            return {'addon': dict(addonid=params['addonid'], enabled=True, version='12.4.24')}
        if method == 'Player.Open':
            e.calls.append((method, params));e.flags['path'] = params['item']['file'];return 'OK'
        return e.rpc(method, params)
    result = emby_local.play(e.item, e.actual, resume, rpc, wait=e.wait, clock=lambda:e.now[0], timeout=3)
    assert result['confirmed'] and result['source_selection'] == dict(index=1, reason='size', width=0, bytes=200)
    assert not any(method == 'local' for method, params in e.calls)
    assert e.flags['position'] == (120 if resume else 0)
    assert 'url' not in str(result) and 'media.invalid' not in str(result)


def test_native_single_source_launch_disables_content_lookup(monkeypatch):
    import sys
    calls = []
    class Info:
        def setMediaType(self, value):calls.append(('kind', value))
        def setTitle(self, value):calls.append(('title', value))
    class Entry:
        def __init__(self, **kwargs):calls.append(('entry', kwargs))
        def setContentLookup(self, value):calls.append(('lookup', value))
        def setMimeType(self, value):calls.append(('mime', value))
        def setProperty(self, key, value):calls.append((key, value))
        def getVideoInfoTag(self):return Info()
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace(ListItem=Entry))
    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace(PlayList=lambda id:SimpleNamespace(
        clear=lambda:calls.append(('clear', id)), add=lambda path, entry:calls.append(('add', path)), size=lambda:1)))
    emby_local.open_source({'url':'http://127.0.0.1:57342/metadata','mime_type':'video/x-matroska'},
                          {'title':'测试剧集','media_type':'episode'}, lambda method, params:calls.append((method, params)))
    assert ('lookup', False) in calls and ('kind', 'episode') in calls
    assert ('mime', 'video/x-matroska') in calls
    assert calls[-3:] == [('clear', 1), ('add', 'http://127.0.0.1:57342/metadata'),
                          ('Player.Open', {'item': {'playlistid': 1, 'position': 0}})]


def test_loopback_is_confirmed_without_server_remote_control(local_env):
    e = local_env
    assert e.play() == {'status': 'playing', 'confirmed': True, 'playerid': 1}
    assert ('local', ('srv', '32')) in e.calls
    assert not any(method == 'Player.Seek' for method, _ in e.calls)


@pytest.mark.parametrize('resume,start,expected', [(False, 120., 0.), (True, 0., 120.)])
def test_restart_and_missing_native_resume_seek_only_the_confirmed_file(local_env, resume, start, expected):
    e = local_env; e.flags['position'] = start
    assert e.play(resume)['confirmed']
    seek = next(params for method, params in e.calls if method == 'Player.Seek')
    assert emby_local.seconds(seek['value']['time']) == expected
    assert e.actual['UserData']['PlaybackPositionTicks'] == 1200000000


@pytest.mark.parametrize('problem', ['no_player', 'wrong_file', 'av_error', 'cancelled', 'not_ready', 'cannot_seek'])
def test_accepting_a_request_is_not_reported_as_success_without_target_playback(local_env, problem):
    e = local_env
    if problem == 'no_player':e.flags['active'] = False
    elif problem == 'wrong_file':e.flags['path'] = 'http://127.0.0.1:57342/dynamic/http/tvshows/other/0/0/32/meta/a.mkv'
    elif problem == 'av_error':e.flags['failed'] = True
    elif problem == 'cancelled':e.flags['cancelled'] = True
    elif problem == 'not_ready':e.ready.clear()
    else:e.flags.update(canseek=False, position=0.)
    with pytest.raises(InterruptedError if problem == 'cancelled' else emby_local.Unavailable):
        e.play()
    assert not any(method == 'Player.Seek' for method, _ in e.calls)


def test_local_transport_does_not_send_credentials_or_shell_text(monkeypatch):
    sent = []
    class Connection:
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def settimeout(self, value):pass
        def sendall(self, data):sent.append(data)
        def recv(self, count):return b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n'
    def connect(address, timeout):
        assert address == ('127.0.0.1', 57342) and timeout == 3
        return Connection()
    monkeypatch.setattr(emby_local.socket, 'create_connection', connect)
    emby_local.send('server', '32', lambda: False)
    assert sent == [b'EVENT service;-1;?mode=play&server=server&item=32']
    with pytest.raises(emby_local.Unavailable):emby_local.send('server;bad', '32', lambda: False)
    with pytest.raises(InterruptedError):emby_local.send('server', '32', lambda: True)


def test_failed_transport_does_not_count_as_playback(monkeypatch):
    def offline(*args, **kwargs):raise OSError('private-network-detail')
    monkeypatch.setattr(emby_local.socket, 'create_connection', offline)
    with pytest.raises(emby_local.Unavailable) as caught:
        emby_local.send('server', '32', lambda: False)
    assert 'private-network-detail' not in str(caught.value)


def test_service_probe_is_a_non_media_head_and_rejects_a_missing_service(monkeypatch):
    sent = []
    class Connection:
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def settimeout(self, timeout):pass
        def sendall(self, data):sent.append(data)
        def recv(self, count):return b'HTTP/1.1 404 Not Found\r\n\r\n'
    monkeypatch.setattr(emby_local.socket, 'create_connection', lambda *a, **k:Connection())
    emby_local.service_ready(lambda:False)
    assert sent[0].startswith(b'HEAD / HTTP/1.1\r\n')
    def missing(*args, **kwargs):raise OSError('private-network-details')
    monkeypatch.setattr(emby_local.socket, 'create_connection', missing)
    with pytest.raises(emby_local.Unavailable, match='尚未就绪'):
        emby_local.service_ready(lambda:False)


@pytest.mark.parametrize('failed', [False, True])
def test_ui_restores_conversation_on_failure_and_records_only_confirmed_playback(monkeypatch, failed):
    import sys
    from resources.lib.ui.task_playback import TaskPlayback
    changes = []
    def handoff(*args, **kwargs):
        kwargs['release']()
        if failed:raise emby_local.Unavailable('起播失败')
        return {'confirmed': True, 'status': 'playing'}
    monkeypatch.setitem(sys.modules, 'resources.lib.ui.navigation', SimpleNamespace(handoff=handoff))
    playback = TaskPlayback(None, None, None, None, lambda: 1., None, lambda: None, lambda *args: False)
    background = SimpleNamespace(hide=lambda: changes.append('hide'), foreground=lambda: changes.append('restore'))
    progress = SimpleNamespace(window=object(), iscanceled=lambda: False)
    task = SimpleNamespace(cancelled=lambda: False, steering=None)
    timing = {}
    if failed:
        with pytest.raises(emby_local.Unavailable):
            playback.dispatch(({}, 'emby', timing), progress, background, None, task)
        assert changes == ['hide', 'restore'] and playback.state == 'failed'
        assert 'handoff_seconds' not in timing
    else:
        result = playback.dispatch(({}, 'emby', timing), progress, background, None, task)
        assert result['confirmed'] and result['status'] == playback.state == 'playing'
        assert timing['playback_confirmed'] and changes == ['hide']
