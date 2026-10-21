"""Continuation compares bounded, user-specific history across local and Emby libraries."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from test_command_resume import history
from resources.lib.intelligence import emby, recent_playback
from resources.lib.intelligence.commands import Commands
from resources.lib.intelligence.presentation import parse


@pytest.fixture
def remote(history, monkeypatch):
    local, calls, tools = history
    account = dict(_identity='account', ServerId='server', ServerName='Emby', UserId='user')
    movie = dict(Id='101', Name='远程电影', Type='Movie', ProductionYear=2020,
                 RunTimeTicks=72000000000, ImageTags={'Primary': 'poster'},
                 UserData=dict(LastPlayedDate='2026-10-21T05:00:00.0000000Z',
                               PlaybackPositionTicks=1200000000, Played=False))
    series = dict(Id='200', Name='远程电视剧', Type='Series', ProductionYear=2020)
    first = dict(Id='201', Name='第一集', Type='Episode', SeriesId='200', SeriesName='远程电视剧',
                 ParentIndexNumber=1, IndexNumber=1, ProductionYear=2020, RunTimeTicks=36000000000,
                 UserData=dict(LastPlayedDate='2026-10-21T06:00:00Z', PlaybackPositionTicks=1500000000,
                               PlayCount=3, Played=False))
    second = dict(first, Id='202', Name='第二集', IndexNumber=2,
                  UserData=dict(LastPlayedDate='', PlaybackPositionTicks=0, Played=False))
    items = {'101': movie, '200': series, '201': first, '202': second}
    requests, reads = [], []
    state = {'error': False, 'cancel': False, 'available': True, 'data': None}
    class Client:
        def __init__(self, account, cancelled=lambda: False):
            self.account, self.cancelled, self.deadline = account, cancelled, 10**20
        @property
        def items_path(self):
            return 'Users/user/Items'
        def check(self):
            if self.cancelled():raise InterruptedError('cancelled')
        def request(self, path, params):
            self.check()
            requests.append((path, params))
            if state['error']:raise emby.Unavailable('server unavailable')
            if state['cancel']:state['cancelled'] = True
            if state['data'] is not None:return state['data']
            types = params['IncludeItemTypes'].split(',')
            rows = [row for row in (movie, first, second) if row['Type'] in types]
            rows.sort(key=lambda row: row['UserData'].get('LastPlayedDate', ''), reverse=True)
            return dict(Items=copy.deepcopy(rows[:params['Limit']]), TotalRecordCount=335292)
        def item(self, identifier):
            self.check()
            reads.append(identifier)
            return copy.deepcopy(items[identifier])
        def pages(self, params, maximum=10000):
            self.check()
            assert params['IncludeItemTypes'] == 'Episode' and params['ParentId'] == '200'
            return copy.deepcopy([first, second])
    monkeypatch.setattr(emby, 'Client', Client)
    monkeypatch.setattr(emby, 'accounts', lambda profile: [account])
    monkeypatch.setattr(emby, 'available', lambda tools: state['available'])
    monkeypatch.setattr(emby, 'catalogue', lambda *args: pytest.fail('Recent history must not refresh the title cache'))
    return SimpleNamespace(tools=tools, local=local, calls=calls, movie=movie, series=series,
                           first=first, second=second, requests=requests, reads=reads, state=state)


@pytest.mark.parametrize('phrase,item_id', [
    ('继续播放', '201'), ('继续看', '201'), ('继续看电视剧', '201'),
    ('继续播放电视剧', '201'), ('继续看电影', '101'), ('继续播放电影', '101'),
])
def test_latest_emby_item_is_resumed_with_the_existing_verified_playback_route(remote, phrase, item_id):
    result = Commands(remote.tools).process(phrase)
    assert result['model_invocations'] == 0 and result['tools'][-1]['name'] == 'play_emby'
    assert remote.tools.ui_action['emby_play']['_emby_id'] == item_id
    assert remote.tools.ui_action['resume'] is True
    assert not any(method == 'Player.Open' for method, _ in remote.calls)
    assert len(remote.requests) == 1
    path, params = remote.requests[0]
    assert path == 'Users/user/Items' and params['Limit'] == 13 and params['StartIndex'] == 0
    assert params['SortBy'] == 'DatePlayed' and params['SortOrder'] == 'Descending'
    assert params['EnableUserData'] == 'true'
    assert params['IncludeItemTypes'] == ('Movie' if '电影' in phrase else 'Episode' if '电视剧' in phrase else 'Movie,Episode')


def test_recent_completed_emby_episode_advances_using_full_series_history(remote):
    remote.first['UserData'].update(Played=True, PlaybackPositionTicks=0)
    result = Commands(remote.tools).process('继续看电视剧')
    assert result['tools'][-1]['ok'] and remote.tools.ui_action['emby_play']['_emby_id'] == '202'
    assert remote.tools.ui_action['resume'] is True


def test_tied_emby_episode_history_uses_the_existing_episode_choice_flow(remote):
    remote.second['UserData'].update(remote.first['UserData'])
    result = Commands(remote.tools).process('继续看电视剧')
    shown = parse(result['reply'], remote.tools)
    assert len(shown['choices']) == 2 and remote.tools.ui_action is None
    assert {row['_emby_id'] for row in shown['choices']} == {'201', '202'}


def test_finished_emby_movie_requires_confirmation_before_restarting(remote):
    remote.movie['UserData'].update(Played=True, PlaybackPositionTicks=0)
    result = Commands(remote.tools).process('继续看电影')
    shown = parse(result['reply'], remote.tools)
    assert '已看完' in shown['message'] and shown['choices'][0]['kind'] == 'emby'
    assert not remote.tools.acted


def test_emby_movie_without_an_offset_reports_playback_errors_without_retry(remote, monkeypatch):
    from resources.lib.intelligence import emby_playback
    remote.movie['UserData']['PlaybackPositionTicks'] = 0
    attempts = []
    def fail(*args, **kwargs):
        attempts.append('play')
        raise emby.Unavailable('播放请求失败')
    monkeypatch.setattr(emby_playback, 'open_item', fail)
    result = Commands(remote.tools).process('继续看电影')
    assert '播放请求失败' in result['reply'] and attempts == ['play']
    assert remote.tools.ui_action is None


def test_local_history_can_be_newer_than_emby_history(remote):
    remote.local['movies'][0]['lastplayed'] = '2026-10-22 20:00:00'
    result = Commands(remote.tools).process('继续播放')
    assert result['tools'][-1]['name'] == 'play_media'
    assert remote.calls[-1] == ('Player.Open', {'item': {'movieid': 1}, 'options': {'resume': True}})
    assert not remote.reads  # No need to hydrate older Emby candidates.


def test_tied_local_and_emby_history_requires_a_verified_choice(remote):
    stamp = datetime(2026, 10, 21, 5, tzinfo=timezone.utc).astimezone()
    remote.local['movies'][0]['lastplayed'] = stamp.strftime('%Y-%m-%d %H:%M:%S')
    result = Commands(remote.tools).process('继续看电影')
    shown = parse(result['reply'], remote.tools)
    assert len(shown['choices']) == 2 and not remote.tools.acted
    assert {row['kind'] for row in shown['choices']} == {'movie', 'emby'}
    choice = next(row for row in shown['choices'] if row['kind'] == 'emby')
    choice['_emby_request'] = {'resume': False}  # Incoming selection cannot change trusted intent.
    result = Commands(remote.tools).process('选择', selection=choice)
    assert result['tools'][-1]['ok'] and remote.tools.ui_action['resume'] is True


@pytest.mark.parametrize('phrase,item', [('继续播放', {'type': 'unknown'}),
    ('继续看电影', {'type': 'movie'}), ('继续播放电视剧', {'type': 'episode'})])
def test_current_player_is_resumed_without_querying_any_history(remote, phrase, item):
    remote.local.update(players=[{'playerid': 1, 'type': 'video'}], item=item)
    result = Commands(remote.tools).process(phrase)
    assert result['tools'][-1]['name'] == 'control_playback'
    assert remote.calls[-1] == ('Player.PlayPause', {'playerid': 1, 'play': True})
    assert not remote.requests and not remote.reads


@pytest.mark.parametrize('disabled', ['setting', 'addon'])
def test_disabled_or_unavailable_emby_is_skipped_silently(remote, disabled):
    if disabled == 'setting':remote.tools.media_search_emby = False
    else:remote.state['available'] = False
    result = Commands(remote.tools).process('继续看电影')
    assert result['tools'][-1]['ok'] and not remote.requests
    assert remote.calls[-1][1]['item'] == {'movieid': 1}


def test_incomplete_server_history_does_not_silently_play_an_older_local_item(remote):
    remote.state['error'] = True
    result = Commands(remote.tools).process('继续看电影')
    shown = parse(result['reply'], remote.tools)
    assert '查询失败' in shown['message'] and len(shown['choices']) == 1
    assert not remote.tools.acted and not any(method == 'Player.Open' for method, _ in remote.calls)


def test_failed_local_query_does_not_hide_the_confirmable_emby_history(remote):
    original = remote.tools.rpc
    def rpc(method, params):
        if method == 'VideoLibrary.GetMovies':raise ValueError('Local unavailable')
        return original(method, params)
    remote.tools.rpc = rpc
    result = Commands(remote.tools).process('继续看电影')
    shown = parse(result['reply'], remote.tools)
    assert '查询失败' in shown['message'] and shown['choices'][0]['kind'] == 'emby'
    assert not remote.tools.acted


def test_cancellation_after_the_read_never_prepares_playback(remote):
    remote.state['cancel'] = True
    with pytest.raises(InterruptedError):
        Commands(remote.tools).process('继续播放', cancelled=lambda: remote.state.get('cancelled', False))
    assert not remote.tools.acted and remote.tools.ui_action is None


def test_no_history_returns_a_local_message_without_starting_an_unwatched_series(remote):
    remote.local['movies'].clear(); remote.local['tvshows'].clear()
    remote.state['data'] = {'Items': [], 'TotalRecordCount': 335292}
    result = Commands(remote.tools).process('继续播放')
    assert result['model_invocations'] == 0 and '没有找到' in result['reply']
    assert not remote.tools.acted and remote.tools.ui_action is None


@pytest.mark.parametrize('response', [None, [], {'Items': {}},
    {'Items': [{'Type': 'Movie', 'Name': '无有效ID', 'UserData': {'LastPlayedDate': '2026-10-21T05:00:00Z'}}]},
    {'Items': [{'Type': 'Series'}]}, {'Items': [{}] * 14}])
def test_malformed_server_history_never_becomes_a_guessed_playback(remote, response):
    class BadClient:
        def __init__(self, account, cancelled):self.account, self.items_path = account, 'Users/user/Items'
        def request(self, *args):return response
    import unittest.mock
    with unittest.mock.patch.object(emby, 'Client', BadClient):
        result = Commands(remote.tools).process('继续看电影')
    assert '查询失败' in result['reply'] and not remote.tools.acted


def test_timestamp_compares_kodi_local_time_with_emby_utc():
    stamp = datetime(2026, 10, 21, 5, 2, 50, tzinfo=timezone.utc)
    local = stamp.astimezone().replace(tzinfo=None).isoformat(sep=' ')
    assert recent_playback.timestamp(local) == recent_playback.timestamp('2026-10-21T05:02:50.0000000Z')
    for value in ('', None, 'not-a-date', '1970-01-01', '2026-99-01'):
        assert recent_playback.timestamp(value) is None
