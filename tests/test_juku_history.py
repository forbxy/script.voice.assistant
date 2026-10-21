import json
from types import SimpleNamespace
from urllib.parse import urlencode, parse_qs, urlsplit

import pytest

from resources.lib.intelligence.juku_history import Store, route, recorded, key
from resources.lib.intelligence.juku_tracking import Tracker
from resources.lib.intelligence import juku, juku_resume
from resources.lib.intelligence.commands import Commands
from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.presentation import parse


def params(episode=None, file='file-1'):
    value = dict(action='pan_play', vod_id='101', vod_title='测试剧' if episode else '测试电影',
                 url='https://115.com/s/private-test-share', code='private-test-code', file_id=file,
                 pan_name='7', is_tv='1' if episode else '0', type_id='2' if episode else '1')
    if episode:value.update(season_number='1', episode_number=str(episode))
    return value


def record(episode=None, file='file-1', position=60, now=1792550000):
    return recorded(params(episode, file), dict(type='episode' if episode else 'movie', title='测试',
        season=1, episode=episode, year=2022), position, 600, now=now)


def url(value):return 'plugin://plugin.video.juku/?' + urlencode(value)


@pytest.mark.parametrize('value', ['plugin://other/?action=pan_play',
    'plugin://plugin.video.juku/?action=clear_history',
    'plugin://plugin.video.juku/?action=pan_play&url=https://example.com',
    'plugin://plugin.video.juku/?action=play_hls&action=clear_history&url=https://example.com',
    'https://example.com/?action=play_hls', 'plugin://[invalid'])
def test_only_identifiable_juku_play_routes_can_be_saved(value):
    assert route(value) is None


def test_resource_identity_is_stable_across_resume_and_title_parameters():
    first = params()
    second = dict(first, title='另一个显示名', resume_time='90', pic='http://art.invalid')
    assert key(route(url(first))) == key(route(url(second)))
    assert key(first) != key(params(file='different-version'))


def test_store_is_private_bounded_and_does_not_write_into_plugin_directory(tmp_path):
    store = Store(tmp_path)
    for index in range(105):store.save(record(file=str(index), now=1792550000+index))
    assert len(store.records()) == 100 and store.path.stat().st_mode & 0o777 == 0o600
    assert store.path == tmp_path/'history/juku-playback.json'


class Player:
    def __init__(self):
        self.active, self.position, self.total = True, 60, 600
        self.params = params()
        self.seeks = []
        self.tag = SimpleNamespace(getMediaType=lambda: 'movie', getTitle=lambda: '测试电影',
            getTVShowTitle=lambda: '', getYear=lambda: 2022, getSeason=lambda: -1, getEpisode=lambda: -1)
        self.extra = {}
    def isPlayingVideo(self):return self.active
    def getPlayingItem(self):
        original = url(dict(self.params, **self.extra))
        return SimpleNamespace(getProperty=lambda name: original, getPath=lambda: 'https://temporary.invalid/signed',
                               getVideoInfoTag=lambda: self.tag)
    def getTime(self):return self.position
    def getTotalTime(self):return self.total
    def seekTime(self, value):self.seeks.append(value);self.position = value


def test_tracker_saves_pause_seek_and_stop_without_a_network_request(tmp_path):
    player = Player(); clock = [0]
    tracker = Tracker(tmp_path, player, clock=lambda: clock[0], now=lambda: 1792550000+clock[0])
    tracker.tick()
    assert tracker.store.get(key(player.params))['position'] == 60
    player.position = 123
    tracker.event('paused');tracker.tick()
    assert tracker.store.get(key(player.params))['position'] == 123
    player.position = 245
    tracker.event('seek');tracker.tick()
    tracker.event('stopped');player.active = False;tracker.tick()
    row = tracker.store.get(key(player.params))
    assert row['position'] == 245 and not row['played'] and not player.seeks
    assert 'temporary.invalid' not in tracker.store.path.read_text()


def test_ended_callback_marks_completion_and_cannot_be_overwritten_by_old_player(tmp_path):
    player = Player(); tracker = Tracker(tmp_path, player)
    tracker.tick();tracker.event('ended');tracker.tick()
    tracker.next_tick = 0;tracker.tick()
    assert tracker.store.get(key(player.params))['played'] is True


def test_tracker_never_seeks_without_an_explicit_resume_offset(tmp_path):
    player = Player();Store(tmp_path).save(record(position=300))
    player.position = 0
    tracker = Tracker(tmp_path, player);tracker.tick()
    assert not player.seeks
    player.extra = {'resume_time': '150'}
    tracker.event('started');tracker.tick()
    assert player.seeks == [150]
    tracker.event('seek');tracker.tick()
    assert player.seeks == [150] and tracker.store.get(key(player.params))['position'] == 150


def test_non_juku_or_private_library_playback_is_not_recorded(tmp_path):
    player = Player(); player.params.update(movie_id='mov_1')
    tracker = Tracker(tmp_path, player);tracker.tick()
    assert not tracker.store.records()
    player.getPlayingItem = lambda: SimpleNamespace(getProperty=lambda key: 'plugin://other/?action=pan_play')
    tracker.event('started');tracker.tick()
    assert not tracker.store.records()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(juku, 'available', lambda tools: True)
    state = {'items': [], 'links': [], 'private': [], 'status': {}}
    requests, launched = [], []
    class Client:
        def __init__(self, cancelled=lambda: False):self.cancelled = cancelled
        def check(self):
            if self.cancelled():raise InterruptedError('cancelled')
        def get(self, path, values=None, raw=False):
            self.check();requests.append((path, values))
            if path == '/api/private/emby/config':return {'primary_user': 'configured-user'}
            if path == '/api/playback/history':return {'items': state['items']}
            if path == '/api/playback/status':return state['status'].get(values['item_id'], {'played': False, 'resume_seconds': 0})
            if path == '/api/private/libraries':return [{'id': 'library'}]
            if path.endswith('/links'):return state['links']
            raise AssertionError(path)
        def pages(self, path, values, private=False):return state['private'], True
    monkeypatch.setattr(juku, 'Client', Client)
    def rpc(method, values):
        if method == 'Player.GetActivePlayers':return []
        if method in ('VideoLibrary.GetMovies', 'VideoLibrary.GetTVShows'):return {}
        raise AssertionError(method)
    tools = KodiTools(rpc, profile=tmp_path, defer_ui=True)
    tools._emby_available = False
    return SimpleNamespace(tools=tools, state=state, requests=requests, store=Store(tmp_path))


def test_online_history_joins_recent_commands_and_replays_original_file(environment):
    e = environment;e.store.save(record(position=123))
    result = Commands(e.tools).process('继续播放电影')
    assert result['model_invocations'] == 0 and result['tools'][-1]['name'] == 'juku_play'
    command = e.tools.ui_action
    target = parse_qs(urlsplit(command.split('"')[1]).query)
    assert target['file_id'] == ['file-1'] and target['resume_time'] == ['123.0']
    payload = json.dumps(list(e.tools.media.values()))
    assert 'private-test-share' not in payload and 'private-test-code' not in payload
    assert any(values and values.get('user_id') == 'configured-user' for path, values in e.requests)


def test_private_history_uses_exact_episode_and_fresh_link(environment):
    e = environment
    e.state['items'] = [dict(item_id='ep_mov_1_2_3', movie_id='mov_1', title='私有电视剧',
        is_series=True, season_number=2, episode_number=3, resume_seconds=123, total_seconds=600,
        updated_at=1792550000, played=False)]
    e.state['private'] = [dict(id='mov_1', title='私有电视剧', year=2022, isTv=True)]
    e.state['links'] = [dict(id='link', link_type='direct', provider='7',
        url='https://media.invalid/episode3', season_number=2, episode_number=3)]
    result = Commands(e.tools).process('继续看电视剧')
    assert result['model_invocations'] == 0
    target = parse_qs(urlsplit(e.tools.ui_action.split('"')[1]).query)
    assert target['season_number'] == ['2'] and target['episode_number'] == ['3']
    assert target['resume_time'] == ['123.0'] and target['action'] == ['play_private_link']


def test_private_completed_episode_chooses_next_real_episode(environment):
    e = environment
    e.state['items'] = [dict(item_id='ep_mov_1_1_1', movie_id='mov_1', title='私有电视剧',
        is_series=True, season_number=1, episode_number=1, resume_seconds=600, total_seconds=600,
        updated_at=1792550000, played=True)]
    e.state['private'] = [dict(id='mov_1', title='私有电视剧', isTv=True)]
    e.state['links'] = [dict(id='second', link_type='direct', url='https://media.invalid/2',
                             season_number=1, episode_number=2)]
    Commands(e.tools).process('继续看电视剧')
    target = parse_qs(urlsplit(e.tools.ui_action.split('"')[1]).query)
    assert target['episode_number'] == ['2'] and float(target['resume_time'][0]) == 0


def test_completed_online_episode_only_advances_to_saved_same_show_route(environment):
    e = environment
    current = record(episode=1);current['played'] = True
    current['next'] = record(episode=2, file='episode-2', position=0)
    e.store.save(current)
    Commands(e.tools).process('继续看电视剧')
    target = parse_qs(urlsplit(e.tools.ui_action.split('"')[1]).query)
    assert target['file_id'] == ['episode-2'] and target['episode_number'] == ['2']


def test_missing_next_episode_opens_resource_picker_instead_of_replaying_completed_episode(environment):
    e = environment;current = record(episode=1);current['played'] = True;e.store.save(current)
    result = Commands(e.tools).process('继续看电视剧')
    assert e.tools.ui_action.startswith('ActivateWindow') and '接下来' in result['reply']


def test_incoming_history_selection_cannot_replace_private_replay_parameters(environment):
    e = environment;value = record();e.store.save(value)
    item = juku_resume.remember(e.tools, value)
    e.tools._juku_available = True
    item_from_ui = dict(item, route={'action': 'clear_history'}, _juku_history='fake')
    result = Commands(e.tools).process('选择', selection=item_from_ui)
    assert result['model_invocations'] == 0 and 'clear_history' not in e.tools.ui_action


def test_disabled_juku_has_no_history_api_reads(environment):
    e = environment;e.store.save(record());e.tools.media_search_juku = False
    result = Commands(e.tools).process('继续播放')
    assert not e.requests and not e.tools.acted and '没有找到' in result['reply']


def test_tracker_disabled_does_not_save_or_seek(tmp_path):
    player = Player();player.extra = {'resume_time': '120'}
    tracker = Tracker(tmp_path, player);tracker.event('started');tracker.tick(enabled=False)
    assert not tracker.store.records() and not player.seeks


def test_playlist_race_preserves_current_episode_and_other_series_is_not_saved(tmp_path):
    player = Player();player.params = params(1)
    player.tag = SimpleNamespace(getMediaType=lambda: 'episode', getTitle=lambda: '第一集',
        getTVShowTitle=lambda: '测试剧', getYear=lambda: 2022, getSeason=lambda: 1, getEpisode=lambda: 1)
    def disappearing():raise IndexError('replaced playlist')
    tracker = Tracker(tmp_path, player, next_item=disappearing);tracker.tick()
    assert 'next' not in tracker.store.get(key(player.params))
    following = record(episode=2);following['catalogue_id'] = 'other'
    tracker.next_item = lambda: following;tracker.event('paused');tracker.tick()
    assert 'next' not in tracker.store.get(key(player.params))


def test_malformed_stored_records_do_not_prevent_saving_valid_progress(tmp_path):
    store = Store(tmp_path)
    store.path.write_text(json.dumps({'records': {'bad': None, 'other': {'updated_at': 'bad'}}}))
    store.save(record())
    assert list(store.records()) == [record()['key']]


def test_completed_movie_requires_confirmation_then_starts_at_zero(environment):
    e = environment;value = record(position=600);value['played'] = True;e.store.save(value)
    result = Commands(e.tools).process('继续看电影')
    assert not e.tools.acted and '已看完' in result['reply']
    choice = parse(result['reply'], e.tools)['choices'][0]
    Commands(e.tools).process('选择', selection=choice)
    target = parse_qs(urlsplit(e.tools.ui_action.split('"')[1]).query)
    assert float(target['resume_time'][0]) == 0 and e.tools.ui_action.endswith(',noresume)')


def test_private_completed_episode_skips_watched_and_preserves_next_partial_offset(environment):
    e = environment
    e.state['items'] = [dict(item_id='ep_mov_1_1_1', movie_id='mov_1', title='私有电视剧',
        is_series=True, season_number=1, episode_number=1, resume_seconds=600, total_seconds=600,
        updated_at=1792550000, played=True)]
    e.state['private'] = [dict(id='mov_1', title='私有电视剧', isTv=True)]
    e.state['links'] = [dict(id=str(ep), link_type='direct', url='https://media.invalid/video',
        season_number=1, episode_number=ep) for ep in (2, 3)]
    e.state['status'] = {'ep_mov_1_1_2': {'played': True},
                         'ep_mov_1_1_3': {'played': False, 'resume_seconds': 89}}
    Commands(e.tools).process('继续看电视剧')
    target = parse_qs(urlsplit(e.tools.ui_action.split('"')[1]).query)
    assert target['episode_number'] == ['3'] and float(target['resume_time'][0]) == 89


def test_private_multiple_versions_are_presented_instead_of_arbitrarily_played(environment):
    e = environment
    e.state['items'] = [dict(item_id='mov_1', movie_id='mov_1', title='私有电影', is_series=False,
        resume_seconds=123, total_seconds=600, updated_at=1792550000, played=False)]
    e.state['private'] = [dict(id='mov_1', title='私有电影', isTv=False)]
    e.state['links'] = [dict(id=str(i), link_type='direct', url='https://media.invalid/video') for i in (1, 2)]
    result = Commands(e.tools).process('继续看电影')
    assert e.tools.ui_action.startswith('ActivateWindow') and '版本' in result['reply']


def test_cancellation_cannot_open_next_episode_picker(environment):
    e = environment;value = record(episode=1);value['played'] = True
    item = juku_resume.remember(e.tools, value)
    with pytest.raises(InterruptedError):
        juku_resume.open_item(e.tools, item, juku.Client(), lambda: True, lambda value: None)
    assert not e.tools.acted


def test_history_api_failure_requires_selection_before_replaying_online_record(environment, monkeypatch):
    e = environment;e.store.save(record())
    class Offline:
        def __init__(self, cancelled):pass
        def get(self, *args, **kwargs):raise juku.Unavailable('test failure')
    monkeypatch.setattr(juku, 'Client', Offline)
    result = Commands(e.tools).process('继续播放')
    shown = parse(result['reply'], e.tools)
    assert not e.tools.acted and len(shown['choices']) == 1 and '查询失败' in shown['message']


def test_named_online_play_resumes_the_recorded_resource(environment):
    e = environment;value = record(position=199);e.store.save(value)
    item = juku.remember(e.tools, [dict(id='101', title='测试电影', isTv=False)], 'online',
        dict(title='测试电影', kind='movie', year=None, resume=True))[0]
    from resources.lib.intelligence.juku_playback import open_item
    open_item(e.tools, item, juku.Client(), lambda: False, lambda value: None)
    target = parse_qs(urlsplit(e.tools.ui_action.split('"')[1]).query)
    assert target['file_id'] == ['file-1'] and float(target['resume_time'][0]) == 199
