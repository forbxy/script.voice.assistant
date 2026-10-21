"""Whole-season Emby queues, selected position, source routes and rollback."""
import copy
import sys
from types import SimpleNamespace

import pytest

from resources.lib.intelligence import emby, emby_local, emby_queue
from resources.lib.intelligence.emby_client import Unavailable


@pytest.fixture
def season_env():
    account = {'_identity': 'account', 'ServerId': 'server'}
    show = dict(Id='show', Type='Series', Name='测试剧', ProductionYear=2026, PresentationUniqueKey='same')
    values = [dict(Id=str(season * 10 + episode), Type='Episode', Name='第{}集'.format(episode),
                   SeriesId='show', SeasonId='season'+str(season), ParentIndexNumber=season, IndexNumber=episode,
                   MediaSources=[dict(Id='src'+str(season * 10 + episode), Path='https://media.invalid/{}.mkv'.format(episode))])
              for season, episode in ((2, 1), (1, 3), (1, 1), (1, 2))]
    actual = copy.deepcopy(values[-1])
    actual['UserData'] = {'PlaybackPositionTicks': 1200000000}
    item = emby.candidate(actual, account)
    calls, flags = [], {'vfs': True}

    class Client:
        def __init__(self):
            self.account = account
        def check(self):
            pass
        def item(self, key):
            calls.append(('item', key))
            return copy.deepcopy(next((value for value in values if value['Id'] == key),
                                      show if key == 'show' else dict(show, Id=key)))
        def pages(self, params, maximum):
            calls.append(('pages', params))
            return copy.deepcopy(values)

    def rpc(method, params):
        calls.append((method, params))
        return {'addon': dict(addonid=params['addonid'], enabled=flags['vfs']
                             if params['addonid'] == 'vfs.stream.fast' else True, version='12.4.24')}

    return SimpleNamespace(prepare=lambda: emby_queue.prepare(Client(), item, actual, rpc), actual=actual,
                           item=item, values=values, show=show, calls=calls, flags=flags, Client=Client)


def test_entire_selected_season_includes_earlier_episodes_and_starts_in_middle(season_env):
    e = season_env
    queue = e.prepare()
    assert queue['season'] == 1 and queue['position'] == 1
    assert [entry['item']['_emby_id'] for entry in queue['entries']] == ['11', '12', '13']
    assert all(entry['item']['season'] == 1 for entry in queue['entries'])
    assert all(entry['item']['showtitle'] == '测试剧' for entry in queue['entries'])
    query = next(params for kind, params in e.calls if kind == 'pages')
    assert query['ParentId'] == 'season1'
    assert ('item', '11') in e.calls and ('item', '13') in e.calls and ('item', '21') not in e.calls


@pytest.mark.parametrize('vfs', [True, False])
def test_mixed_single_and_multi_sources_keep_selection_rule_and_local_nextgen_route(season_env, vfs):
    e = season_env
    e.flags['vfs'] = vfs
    e.values[-1]['MediaSources'].append(dict(Id='largest', Path='https://media.invalid/large.mkv', Size=200))
    e.actual['MediaSources'] = copy.deepcopy(e.values[-1]['MediaSources'])
    source = e.prepare()['entries'][1]['source']
    metadata = bytes.fromhex(source['url'].split('/')[-2].split('-')[4]).decode()
    assert source['url'].startswith('http://127.0.0.1:57342/dynamic/http/tvshows/server/0/show/12/')
    assert ('<>' in metadata) is not vfs
    assert (source['selection'] is not None) is vfs
    if vfs:
        assert source['selection']['index'] == 1 and source['selection']['reason'] == 'size'


def test_missing_season_id_falls_back_to_series_query_but_never_queues_other_seasons(season_env):
    e = season_env
    e.actual.pop('SeasonId')
    queue = e.prepare()
    assert len(queue['entries']) == 3
    assert next(params for kind, params in e.calls if kind == 'pages')['ParentId'] == 'show'


def test_list_response_omitting_sources_cannot_drop_other_episodes_versions(season_env, monkeypatch):
    e = season_env
    e.values[1]['MediaSources'].append(dict(Id='better', Path='https://media.invalid/better.mkv',
                                           MediaStreams=[dict(Type='Video', Width=3840)]))

    def without_sources(client, params, maximum):
        return [{key: value for key, value in row.items() if key != 'MediaSources'} for row in copy.deepcopy(e.values)]

    monkeypatch.setattr(e.Client, 'pages', without_sources)
    queue = e.prepare()
    assert queue['entries'][2]['source']['selection']['index'] == 1
    assert queue['entries'][2]['source']['selection']['width'] == 3840


@pytest.mark.parametrize('problem', ['no_season', 'no_episode', 'removed', 'missing_source', 'wrong_series', 'duplicate'])
def test_invalid_or_ambiguous_season_fails_before_touching_playback(season_env, problem):
    e = season_env
    if problem == 'no_season':e.actual.pop('ParentIndexNumber')
    elif problem == 'no_episode':e.actual['IndexNumber'] = 0
    elif problem == 'removed':e.values.pop()
    elif problem == 'missing_source':e.values[1].pop('MediaSources')
    elif problem == 'wrong_series':e.values[1]['SeriesId'] = 'other';e.show.pop('PresentationUniqueKey')
    elif problem == 'duplicate':e.values.append(dict(e.values[1], Id='duplicate'))
    with pytest.raises(Unavailable):e.prepare()
    assert not any(kind.startswith(('Player.', 'Playlist.')) for kind, _ in e.calls)


@pytest.fixture
def kodi_queue(monkeypatch):
    calls, flags = [], {'cancelled': False, 'add_error': False}

    class Info:
        def __getattr__(self, key):
            return lambda value: calls.append((key, value))

    class Entry:
        def __init__(self, label, path):self.path = path
        def getPath(self):return self.path
        def setContentLookup(self, value):calls.append(('lookup', value))
        def setMimeType(self, value):pass
        def setProperty(self, *args):pass
        def setArt(self, value):calls.append(('art', value))
        def getVideoInfoTag(self):return Info()

    entries = [Entry('旧队列', 'https://old.invalid/old.mkv')]

    class Playlist:
        def size(self):return len(entries)
        def __getitem__(self, index):return entries[index]
        def clear(self):calls.append(('clear', None));entries.clear()
        def add(self, path, entry):
            if flags['add_error'] and 'old.invalid' not in path:
                raise RuntimeError('queue add failed')
            entries.append(entry)

    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace(PlayList=lambda _: Playlist()))
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace(ListItem=Entry))

    def rpc(method, params):
        calls.append((method, params))
        return 'OK'

    return SimpleNamespace(calls=calls, flags=flags, entries=entries, rpc=rpc)


def test_native_queue_has_season_metadata_and_opens_selected_position(season_env, kodi_queue):
    queue = season_env.prepare()
    e = kodi_queue
    emby_queue.open_queue(queue, e.rpc, lambda: e.flags['cancelled'])
    assert len(e.entries) == 3
    assert ('setSeason', 1) in e.calls and ('setEpisode', 2) in e.calls and ('setTvShowTitle', '测试剧') in e.calls
    assert e.calls[-1] == ('Player.Open', {'item': {'playlistid': 1, 'position': 1},
                                         'options': {'resume': False, 'shuffled': False, 'repeat': 'off'}})


@pytest.mark.parametrize('cancel', [True, False])
def test_cancel_or_queue_build_failure_preserves_previous_queue(season_env, kodi_queue, cancel):
    queue = season_env.prepare()
    e = kodi_queue
    e.flags['cancelled' if cancel else 'add_error'] = True
    with pytest.raises(InterruptedError if cancel else RuntimeError):
        emby_queue.open_queue(queue, e.rpc, lambda: e.flags['cancelled'])
    assert [row.getPath() for row in e.entries] == ['https://old.invalid/old.mkv']
    assert not any(method == 'Player.Open' for method, params in e.calls)


def test_cancel_after_queue_replacement_restores_previous_entries(season_env, kodi_queue):
    queue = season_env.prepare()
    e = kodi_queue

    def cancelled():
        return bool(e.entries) and 'old.invalid' not in e.entries[0].getPath()

    with pytest.raises(InterruptedError):
        emby_queue.open_queue(queue, e.rpc, cancelled)
    assert [row.getPath() for row in e.entries] == ['https://old.invalid/old.mkv']
    assert not any(method == 'Player.Open' for method, params in e.calls)


@pytest.mark.parametrize('resume', [True, False])
def test_queue_playback_confirms_selected_file_and_keeps_resume_position(season_env, kodi_queue, monkeypatch, resume):
    e = season_env
    queue = e.prepare()
    now, state = [0.], {'position': 0.}
    source = queue['entries'][queue['position']]['source']
    monkeypatch.setattr(emby_local, 'service_ready', lambda cancelled: None)
    from resources.lib.intelligence import playback_tools
    monkeypatch.setattr(playback_tools, 'playback_watcher', lambda: None)

    def rpc(method, params):
        if method == 'Player.GetActivePlayers':return [{'playerid': 1, 'type': 'video'}] if now[0] > .1 else []
        if method == 'Player.GetItem':return {'item': {'file': source['url']}}
        if method == 'Player.GetProperties':return {'speed': 1, 'canseek': True, 'time': {'seconds': state['position']}}
        if method == 'Player.Seek':state['position'] = emby_local.seconds(params['value']['time']);return 'OK'
        return kodi_queue.rpc(method, params)

    result = emby_local.play(e.item, e.actual, resume, rpc, wait=lambda seconds: now.__setitem__(0, now[0]+seconds),
                             clock=lambda: now[0], timeout=3, queue=queue)
    assert result['confirmed'] and result['queued_count'] == 3 and result['playlist_position'] == 1
    assert state['position'] == (120 if resume else 0)
