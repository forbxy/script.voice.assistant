"""Emby catalogue isolation, shared matching and deferred playback integration."""
import copy
import json
from pathlib import Path
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.data_paths import data_path
from resources.lib.intelligence import emby, emby_client, emby_playback, emby_local
from resources.lib.intelligence.commands import Commands, media_request
from resources.lib.intelligence.media_resolver import choose_sources, preferred_sources
from resources.lib.intelligence.media_results import SearchResult
from resources.lib.intelligence.media_match import rank
from resources.lib.intelligence.media_order import instruction
from resources.lib.intelligence.presentation import parse
from resources.lib.intelligence.sessions import Sessions
from resources.lib.settings import FIELDS, Settings


@pytest.fixture
def environment(tmp_path, monkeypatch):
    profile = tmp_path / 'script.voice.assistant'
    profile.mkdir()
    directory = tmp_path / emby.ADDON
    directory.mkdir()
    config = directory / 'servers_srv1.json'
    credentials = dict(ServerId='srv1', ServerUrl='http://emby.invalid', AccessToken='private-test-token',
                       UserId='user1', DeviceId='device1', ServerName='测试服')
    config.write_text(json.dumps(credentials))
    account = emby.accounts(profile)[0]
    movie = dict(Id='11', Name='惊天营救', Type='Movie', ProductionYear=2020,
                 UserData={'PlaybackPositionTicks': 1250000000}, ImageTags={'Primary': 'abc123'})
    show = dict(Id='20', Name='测试电视剧', Type='Series', ProductionYear=2020)
    values = {'11': movie, '20': show}
    events = []
    catalogue = [movie, show]
    changed = []
    episodes = [dict(Id=str(30+i), Name='剧集'+str(i), Type='Episode', ProductionYear=2020,
                     ParentIndexNumber=1, IndexNumber=i, SeriesId='20', RunTimeTicks=10000000000,
                     MediaSources=[dict(Id='source'+str(i), Path='http://media.invalid/'+str(i)+'.mkv')],
                     UserData={'Played': i == 1, 'PlayCount': int(i == 1),
                               'LastPlayedDate': '2026-10-20T10:00:00Z' if i == 1 else ''}) for i in (1, 2, 3)]
    values.update({row['Id']: row for row in episodes})
    class Client:
        def __init__(self, account, cancelled=lambda: False):
            self.account, self.cancelled = account, cancelled
            self.clock_offset = 0
        @property
        def items_path(self):
            return 'Users/' + self.account['UserId'] + '/Items'
        def check(self):
            if self.cancelled():
                raise InterruptedError('cancelled')
        def pages(self, params, maximum=50000):
            self.check(); events.append(('pages', params))
            if params.get('MinDateLastSaved'):
                return copy.deepcopy(changed)
            return copy.deepcopy(episodes if params['IncludeItemTypes'] == 'Episode' else catalogue)
        def item(self, id):
            self.check(); events.append(('item', id))
            return copy.deepcopy(values[id])
        def session(self):
            self.check(); events.append(('session', self.account['DeviceId']))
            return 'this-box'
        def request(self, path, params=None, method='GET'):
            self.check(); events.append((method, path, params))
            if method == 'GET' and path == self.items_path:
                count = 0 if params.get('MinDateLastSaved', '').startswith('2100') else len(catalogue)
                return {'TotalRecordCount': count, 'Items': copy.deepcopy(catalogue[:min(count, 1)])}
    monkeypatch.setattr(emby, 'Client', Client)
    monkeypatch.setattr(emby, 'available', lambda tools: True)
    queues = []
    def local_play(item, actual, resume, rpc, cancelled, **options):
        if cancelled():raise InterruptedError('cancelled')
        if options.get('queue') is not None:queues.append(options['queue'])
        events.append(('local-play', item['_emby_id'],
                       {'StartPositionTicks': (actual.get('UserData') or {}).get('PlaybackPositionTicks', 0) if resume else 0}))
        return {'status': 'playing', 'confirmed': True, 'playerid': 1}
    monkeypatch.setattr(emby_local, 'play', local_play)
    local = []
    def rpc(method, params):
        if method == 'PVR.GetProperties':
            return {'available': False}
        if method in ('VideoLibrary.GetMovies', 'VideoLibrary.GetTVShows'):
            rows = list(local) if method.endswith('Movies') else []
            if params.get('filter'):
                query = params['filter'].get('value')
                rows = [row for row in rows if row['title'] == query]
            return {('movies' if method.endswith('Movies') else 'tvshows'): rows,
                    'limits': {'total': len(rows), 'start': 0, 'end': len(rows)}}
        if method == 'Addons.GetAddonDetails':
            return {'addon': {'addonid': params['addonid'], 'enabled': False}}
        if method == 'VideoLibrary.GetMovieDetails':
            return {'moviedetails': next(row for row in local if row['movieid'] == params['movieid'])}
        if method == 'Player.Open':
            events.append(('Player.Open', params)); return 'OK'
        raise AssertionError(method)
    tools = KodiTools(rpc, profile=profile, defer_ui=True)
    return SimpleNamespace(profile=profile, config=config, credentials=credentials, account=account, events=events,
                           tools=tools, movie=movie, show=show, values=values, episodes=episodes,
                           catalogue=catalogue, changed=changed, local=local, Client=Client, queues=queues)


def test_shared_phonetic_scoring_and_no_play_until_dialog_handoff(environment):
    e = environment
    result = Commands(e.tools).process('播放今天营救')
    assert result['model_invocations'] == 0 and result['matching']['automatic']
    assert result['matching']['best_score'] == .9125
    assert e.tools.ui_action['emby_play']['_emby_id'] == '11'
    assert not any(row[0] == 'POST' for row in e.events)
    emby_playback.dispatch(e.tools.ui_action)
    post = next(row for row in e.events if row[0] == 'local-play')
    assert post == ('local-play', '11', {'StartPositionTicks': 1250000000})
    assert not any(row[0] in ('POST', 'session') for row in e.events)
    assert 'private-test-token' not in json.dumps(result) + json.dumps(e.tools.ui_action)


@pytest.mark.parametrize('phrase', ['库里有没有惊天营救', '从库里搜索今天营救'])
def test_emby_library_search_only_offers_matches_until_selected(environment, phrase):
    e = environment
    result = Commands(e.tools).process(phrase)
    choices = parse(result['reply'], e.tools)['choices']
    assert len(choices) == 1 and choices[0]['title'] == '惊天营救'
    assert choices[0]['media_type'] == 'movie' and choices[0]['thumbnail']
    assert not e.tools.acted and e.tools.ui_action is None
    assert not result['matching']['automatic'] and result['matching']['target'] is None
    Commands(e.tools).process('选择', choices[0])
    assert e.tools.ui_action['emby_play']['_emby_id'] == '11' and e.tools.ui_action['resume']


def test_emby_season_library_search_keeps_season_for_the_later_click(environment):
    e = environment
    result = Commands(e.tools).process('从库里搜索测试电视剧第一季')
    choices = parse(result['reply'], e.tools)['choices']
    assert len(choices) == 1 and choices[0]['media_type'] == 'tvshow'
    assert choices[0]['_emby_request']['season'] == 1 and e.tools.ui_action is None
    Commands(e.tools).process('选择', choices[0])
    assert e.tools.ui_action['emby_play']['_emby_id'] == '32' and e.tools.ui_action['resume']


@pytest.mark.parametrize('phrase', ['从头播放惊天营救', '重新播放惊天营救', '把惊天营救从头播放'])
def test_explicit_restart_does_not_clear_resume(environment, phrase):
    e = environment
    Commands(e.tools).process(phrase)
    emby_playback.dispatch(e.tools.ui_action)
    assert e.events[-1][2]['StartPositionTicks'] == 0
    assert e.movie['UserData']['PlaybackPositionTicks'] == 1250000000


@pytest.mark.parametrize('image_tag', ['abc123', 'abc123_639280474299173365'])
def test_close_candidates_and_versions_remain_selectable_and_have_art(environment, image_tag):
    e = environment
    e.movie['ImageTags'] = {'Primary': image_tag}
    e.catalogue.append(dict(e.movie, Id='12', ProductionYear=2021))
    e.values['12'] = e.catalogue[-1]
    result = Commands(e.tools).process('播放今天营救')
    assert not result['matching']['automatic'] and not e.tools.acted
    choices = parse(result['reply'], e.tools)['choices']
    assert len(choices) == 2 and all(' · 电影 · Emby · 测试服' in c['label'] for c in choices)
    assert all(c['thumbnail'].startswith('http://127.0.0.1:57342/picture/') for c in choices)
    assert all(c['thumbnail'].endswith('-' + image_tag + '--poster.jpg') for c in choices)
    selected = Commands(e.tools).process('选择', selection=choices[0])
    assert '正在通过 Emby' in parse(selected['reply'], e.tools)['message']


def test_local_exact_avoids_all_emby_network_queries(environment):
    e = environment
    e.local.append({'movieid': 8, 'title': '惊天营救', 'year': 2020})
    result = Commands(e.tools).process('播放惊天营救')
    assert e.events == [('Player.Open', {'item': {'movieid': 8}, 'options': {'resume': True}})]
    assert result['model_invocations'] == 0


def test_cache_is_minimal_lazy_and_rotates_on_login_change(environment):
    e = environment
    source = emby.Source(e.tools, lambda: False)
    source.search(media_request('播放今天营救'))
    source.search(media_request('播放奥蓝多'))
    assert sum(event[0] == 'pages' for event in e.events) == 1
    path = data_path(e.profile, 'cache/emby-' + e.account['_identity'] + '.json')
    content = path.read_text()
    assert 'private-test-token' not in content and 'PlaybackPositionTicks' not in content
    cache = json.loads(content); cache['updated_at'] -= emby.CACHE_AGE + 1; path.write_text(json.dumps(cache))
    source.search(media_request('播放今天营救'))
    assert sum(event[0] == 'pages' and not event[1].get('MinDateLastSaved') for event in e.events) == 1
    e.config.write_text(json.dumps(dict(e.credentials, AccessToken='new-private-token')))
    source.search(media_request('播放今天营救'))
    assert sum(event[0] == 'pages' and not event[1].get('MinDateLastSaved') for event in e.events) == 2


def test_persisted_selection_retains_opaque_identity_but_rechecks_account_and_title(environment):
    e = environment
    Commands(e.tools).process('播放惊天营救')
    item = next(iter(e.tools.media.values()))
    sessions = Sessions(e.profile, 'forever'); sessions.completed(e.tools.media.values())
    restored = KodiTools(e.tools.rpc, profile=e.profile, media=Sessions(e.profile, 'forever').media, defer_ui=True)
    assert restored.media[('emby', item['id'])]['_emby_id'] == '11'
    e.movie['Name'] = '另一个片名'
    result = Commands(restored).process('选择', selection=item)
    assert '项目已变化' in parse(result['reply'], restored)['message'] and not restored.acted
    e.config.write_text(json.dumps(dict(e.credentials, UserId='different-user')))
    with pytest.raises(emby.Unavailable, match='账号已变化'):
        emby_playback.dispatch(e.tools.ui_action)


def test_series_uses_all_real_history_and_resumes_next_unwatched_episode(environment):
    e = environment
    result = Commands(e.tools).process('播放电视剧测试电视剧')
    assert e.tools.ui_action['emby_play']['_emby_id'] == '32'
    assert result['model_invocations'] == 0
    e.events.clear()
    emby_playback.dispatch(e.tools.ui_action, rpc=e.tools.rpc)
    assert e.events[-1][2]['StartPositionTicks'] == 0
    assert [entry['item']['episode'] for entry in e.queues[-1]['entries']] == [1, 2, 3]
    assert e.queues[-1]['position'] == 1 and e.queues[-1]['season'] == 1


def test_series_saved_position_wins_and_whole_show_restart_uses_first_episode(environment):
    e = environment
    e.episodes[1]['UserData'] = {'PlaybackPositionTicks': 1200000000, 'LastPlayedDate': '2026-10-20T11:00:00Z'}
    Commands(e.tools).process('播放电视剧测试电视剧')
    assert e.tools.ui_action['emby_play']['_emby_id'] == '32'
    emby_playback.dispatch(e.tools.ui_action, rpc=e.tools.rpc)
    assert e.events[-1][2]['StartPositionTicks'] == 1200000000
    Commands(e.tools).process('从头播放电视剧测试电视剧')
    assert e.tools.ui_action['emby_play']['_emby_id'] == '31'
    assert e.tools.ui_action['resume'] is False


def test_emby_play_count_does_not_mark_an_unfinished_episode_complete(environment):
    e = environment
    e.episodes[1]['UserData'] = {'PlaybackPositionTicks': 0, 'Played': False,
                               'PlayCount': 4, 'LastPlayedDate': '2026-10-20T11:00:00Z'}
    result = Commands(e.tools).process('播放电视剧测试电视剧')
    assert e.tools.ui_action['emby_play']['_emby_id'] == '32'
    assert result['model_invocations'] == 0


@pytest.mark.parametrize('phrase,expected,resume', [
    ('播放电视剧测试电视剧', '32', True),
    ('播放第一季测试电视剧', '32', True),
    ('从头播放第一季测试电视剧', '31', False)])
def test_grouped_series_from_another_library_can_resume_or_restart(environment, phrase, expected, resume):
    e = environment
    e.show['PresentationUniqueKey'] = 'same-series-key'
    e.values['21'] = dict(e.show, Id='21')
    for row in e.episodes:row['SeriesId'] = '21'
    result = Commands(e.tools).process(phrase)
    assert result['model_invocations'] == 0
    assert e.tools.ui_action['emby_play']['_emby_id'] == expected
    assert e.tools.ui_action['resume'] is resume
    # One check for the alternate parent covers all its episodes; the already
    # revalidated selected series does not need another metadata request.
    assert e.events.count(('item', '20')) == e.events.count(('item', '21')) == 1
    emby_playback.dispatch(e.tools.ui_action, rpc=e.tools.rpc)
    assert e.events[-1] == ('local-play', expected, {'StartPositionTicks': 0})


def test_agent_episode_tool_accepts_the_same_verified_grouped_series(environment):
    e = environment
    e.show['PresentationUniqueKey'] = 'same-series-key'
    e.values['21'] = dict(e.show, Id='21')
    for row in e.episodes:row['SeriesId'] = '21'
    show = e.tools.execute('search_emby', {'query': '测试电视剧', 'kind': 'tvshow'})['matches'][0]
    result = e.tools.execute('get_emby_episodes', {'id': show['id'], 'season': 1})
    assert result['ok'] and result['total'] == 3 and len(result['matches']) == 3
    assert result['recommended']['episode'] == 2
    assert e.events.count(('item', '21')) == 1
    assert e.tools.ui_action is None


@pytest.mark.parametrize('original,alternate,other_type', [
    ('selected-key', 'another-key', 'Series'),  # Same title and year are insufficient.
    (None, None, 'Series'), ('', '', 'Series'), ('   ', '   ', 'Series'),
    ('selected-key', None, 'Series'), ('selected-key', 'selected-key', 'Movie')])
def test_unproven_grouped_series_never_plays_or_offers_wrong_episodes(environment, original, alternate, other_type):
    e = environment
    e.show['PresentationUniqueKey'] = original
    e.values['21'] = dict(e.show, Id='21', Type=other_type, PresentationUniqueKey=alternate)
    for row in e.episodes:
        row.update(SeriesId='21', SeriesName=e.show['Name'], PresentationUniqueKey=original)
    result = Commands(e.tools).process('播放第一季测试电视剧')
    assert '所选电视剧' in result['reply']
    assert '归属已变化' not in result['reply']
    assert e.tools.ui_action is None
    assert not any(row['media_type'] == 'episode' for row in e.tools.media.values() if row['kind'] == 'emby')
    assert not any(event[0] == 'local-play' for event in e.events)


@pytest.mark.parametrize('series', [None, '', 'bad/series'])
def test_episode_without_valid_parent_cannot_be_played(environment, series):
    e = environment
    e.episodes[0]['SeriesId'] = series
    result = Commands(e.tools).process('播放第一季测试电视剧')
    assert '无法确认 Emby 剧集属于所选电视剧' in result['reply']
    assert e.tools.ui_action is None


@pytest.mark.parametrize('history,restart,expected', [(True, False, '42'), (True, True, '41'), (False, False, '41')])
@pytest.mark.parametrize('title', ['第二季测试电视剧', '测试电视剧第二季'])
def test_explicit_season_only_uses_that_seasons_emby_history(environment, history, restart, expected, title):
    e = environment
    for i in (1, 2):
        row = dict(e.episodes[1], Id=str(40+i), ParentIndexNumber=2, IndexNumber=i,
                   UserData={'PlaybackPositionTicks': 1200000000, 'LastPlayedDate': '2026-10-20T09:00:00Z'}
                   if history and i == 2 else {})
        e.episodes.append(row); e.values[row['Id']] = row
    phrase = ('从头播放' if restart else '播放') + title
    result = Commands(e.tools).process(phrase)
    assert result['model_invocations'] == 0
    assert e.tools.ui_action['emby_play']['_emby_id'] == expected
    assert e.tools.ui_action['resume'] is not restart
    emby_playback.dispatch(e.tools.ui_action, rpc=e.tools.rpc)
    assert [entry['item']['_emby_id'] for entry in e.queues[-1]['entries']] == ['41', '42']
    assert e.queues[-1]['position'] == (1 if expected == '42' else 0)
    assert e.queues[-1]['season'] == 2


def test_missing_emby_season_neither_plays_another_season_nor_reoffers_whole_show(environment):
    e = environment
    result = Commands(e.tools).process('播放第二季测试电视剧')
    parsed = parse(result['reply'], e.tools)
    assert '没有找到第2季' in parsed['message'] and not parsed['choices']
    assert e.tools.ui_action is None


def test_completed_season_does_not_advance_into_a_later_emby_season(environment):
    e = environment
    for row in e.episodes:row['UserData'] = {'Played': True, 'LastPlayedDate': '2026-10-20T11:00:00Z'}
    other = dict(e.episodes[0], Id='41', ParentIndexNumber=2, UserData={})
    e.episodes.append(other); e.values['41'] = other
    result = Commands(e.tools).process('播放第一季测试电视剧')
    assert '没有后续未看剧集' in result['reply'] and e.tools.ui_action is None


def test_title_choice_retains_requested_emby_season_across_turns(environment):
    e = environment
    duplicate = dict(e.show, Id='21', ProductionYear=2021)
    e.catalogue.append(duplicate); e.values['21'] = duplicate
    other = dict(e.episodes[0], Id='41', ParentIndexNumber=2, UserData={})
    e.episodes.append(other); e.values['41'] = other
    result = Commands(e.tools).process('播放第二季测试电视剧')
    choices = parse(result['reply'], e.tools)['choices']
    assert len(choices) == 2
    chosen = copy.deepcopy(next(row for row in choices if row['_emby_id'] == '20'))
    restored = KodiTools(e.tools.rpc, profile=e.profile, media=json.loads(json.dumps(list(e.tools.media.values()))), defer_ui=True)
    chosen['_emby_request']['season'] = 1
    Commands(restored).process('选择', selection=chosen)
    assert restored.ui_action['emby_play']['_emby_id'] == '41'


def test_ambiguous_history_returns_episode_choices_without_playing(environment):
    e = environment
    for row in e.episodes:
        row['UserData'] = {'PlaybackPositionTicks': 1200000000}
    result = Commands(e.tools).process('播放电视剧测试电视剧')
    choices = parse(result['reply'], e.tools)['choices']
    assert len(choices) == 3 and all(c['media_type'] == 'episode' for c in choices)
    assert not e.tools.acted and e.tools.ui_action is None


def test_model_tools_have_only_known_handles_and_are_available_in_both_agents(environment):
    e = environment
    matches = e.tools.execute('search_emby', {'query': '今天营救'})
    assert matches['ok'] and matches['matches'][0]['kind'] == 'emby'
    assert not {'_emby_server', '_emby_id', 'AccessToken'}.intersection(matches['matches'][0])
    assert not e.tools.execute('play_emby', {'id': 999})['ok']
    assert e.tools.execute('play_emby', {'id': matches['matches'][0]['id']})['ok']
    show = e.tools.execute('search_emby', {'query': '测试电视剧', 'kind': 'tvshow'})['matches'][0]
    episodes = e.tools.execute('get_emby_episodes', {'id': show['id'], 'limit': 1})
    assert episodes['ok'] and episodes['next_offset'] == 1 and episodes['recommended']['episode'] == 2


def test_absent_emby_is_silent_without_network_or_rpc(tmp_path, monkeypatch):
    tools = KodiTools(lambda *args: pytest.fail('unexpected RPC'), profile=tmp_path/'assistant')
    monkeypatch.setattr(emby_client, 'Client', lambda *args: pytest.fail('unexpected network'))
    assert not emby_client.available(tools)
    assert not emby.Source(tools, lambda: False).search(media_request('播放惊天营救')).rows
    assert not any(t['function']['name'] in ('search_emby', 'play_emby') for t in tools.definitions())


def test_incomplete_emby_catalogue_does_not_autoplay(environment, monkeypatch):
    e = environment
    class Broken(e.Client):
        def pages(self, *args, **kwargs):
            raise emby.Unavailable('unavailable')
    monkeypatch.setattr(emby, 'Client', Broken)
    result = emby.Source(e.tools, lambda: False).search(media_request('播放惊天营救'))
    assert not result.complete and not result.rows


def test_cancelled_search_or_dispatch_never_sends_play(environment):
    e = environment
    with pytest.raises(InterruptedError):
        emby.Source(e.tools, lambda: True).search(media_request('播放惊天营救'))
    Commands(e.tools).process('播放惊天营救')
    with pytest.raises(InterruptedError):
        emby_playback.dispatch(e.tools.ui_action, lambda: True)
    assert not any(row[0] == 'POST' for row in e.events)


def test_session_targets_only_matching_device_user_and_rejects_multiple_matches():
    account = {'DeviceId': 'box1', 'UserId': 'user1'}
    client = emby_client.Client.__new__(emby_client.Client); client.account = account
    rows = [dict(Id='phone', DeviceId='phone', UserId='user1', SupportsRemoteControl=True),
            dict(Id='box', DeviceId='box1', UserId='user1', SupportsRemoteControl=True)]
    client.request = lambda *args: rows
    assert client.session() == 'box'
    rows.append(dict(rows[1], Id='other'))
    with pytest.raises(emby.Unavailable, match='唯一'):
        client.session()
    rows.clear()
    with pytest.raises(emby.Unavailable):
        client.session()


@pytest.mark.parametrize('responses', [
    [{'Items': [], 'TotalRecordCount': 2}],
    [{'Items': [{'Id': '1', 'Name': 'a'}], 'TotalRecordCount': 2},
     {'Items': [{'Id': '1', 'Name': 'a'}], 'TotalRecordCount': 2}],
    [{'Items': [{'Id': '1', 'Name': 'a'}], 'TotalRecordCount': 2},
     {'Items': [{'Id': '2', 'Name': 'b'}], 'TotalRecordCount': 3}],
    [{'Items': [], 'TotalRecordCount': 50001}],
])
def test_invalid_or_incomplete_pages_cannot_be_treated_as_a_unique_candidate(responses):
    client = emby_client.Client.__new__(emby_client.Client)
    client.account = {'UserId': 'user1'}; client.check = lambda: None
    iterator = iter(responses); client.request = lambda *args: next(iterator)
    with pytest.raises(emby.Unavailable):
        client.pages({})


@pytest.mark.parametrize('order,expected', [('emby_juku', 'emby'), ('juku_emby', 'private')])
def test_same_title_uses_configured_source_order_but_better_match_still_wins(order, expected):
    e = dict(kind='emby', id='1', title='惊天营救', year=2020, media_type='movie')
    j = dict(id='2', title='惊天营救', year=2020, type_id=1)
    local = SearchResult()
    emby_result = SearchResult(source='emby', rows=[e], exact=True)
    juku_result = SearchResult(source='private', rows=[j], exact=True)
    order_sources = [emby_result, juku_result] if order == 'emby_juku' else [juku_result, emby_result]
    result = choose_sources([local] + order_sources, {'title': '惊天营救'})
    assert result.source == expected and result.automatic
    rows = preferred_sources([e, dict(kind='juku', id='2', title=j['title'], year=2020, typeId=1, _juku_source='private')])
    assert len(rows) == 1 and rows[0]['kind'] == 'emby'
    weak = dict(e, title='惊天动地')
    assert rank('今天营救', [weak, dict(e, id='2')])[0][0]['row']['id'] == '2'


def test_settings_schema_and_xml_expose_search_order_for_all_backends():
    assert FIELDS['agent_backend'].default == 'off'
    assert dict(FIELDS['agent_backend'].options)['off'] == '本地命令引擎'
    assert FIELDS['media_search_order'].default == 'emby_juku' and not FIELDS['media_search_order'].visible
    root = ET.parse(Path(__file__).resolve().parents[1]/'resources/settings.xml')
    field = root.find('.//setting[@id="v2_media_search_order"]')
    assert field.find('dependencies') is None and field.findtext('default') == 'emby_juku'
    assert 'Emby 动态库 → 巨库' in instruction(SimpleNamespace(media_search_order='emby_juku'))
    assert '巨库（先私有库、再在线目录） → Emby 动态库' in instruction(SimpleNamespace(media_search_order='juku_emby'))


@pytest.mark.parametrize('order,expected', [('emby_juku', ['emby']), ('juku_emby', ['private'])])
def test_resolver_calls_sources_in_configured_order_and_stops_after_exact(environment, monkeypatch, order, expected):
    from resources.lib.intelligence import juku
    e = environment
    e.tools.media_search_order = order
    calls = []
    monkeypatch.setattr(juku, 'available', lambda tools: True)
    def emby_search(self, request):
        calls.append('emby')
        return SearchResult(source='emby', rows=[emby.candidate(e.movie, e.account)], exact=True)
    def juku_search(self, source, request):
        calls.append(source)
        return SearchResult(source=source, rows=[{'id':'j1','title':'惊天营救','type_id':1,'year':2020}], exact=True)
    monkeypatch.setattr(emby.Source, 'search', emby_search)
    monkeypatch.setattr(juku.Source, 'search', juku_search)
    monkeypatch.setattr(e.tools, '_queue_playback', lambda *args: {})
    monkeypatch.setattr(emby_playback, 'open_item', lambda *args: {'note':'emby'})
    from resources.lib.intelligence import juku_playback
    monkeypatch.setattr(juku_playback, 'open_item', lambda *args: {'note':'juku'})
    Commands(e.tools).process('播放惊天营救')
    assert calls == expected


def test_unsupported_optional_source_never_hides_other_valid_candidates(environment, monkeypatch):
    e = environment
    monkeypatch.setattr(emby, 'catalogue', lambda *args: (_ for _ in ()).throw(OSError('unwritable cache')))
    result = emby.Source(e.tools, lambda: False).search(media_request('播放惊天营救'))
    assert not result.complete and not result.rows


def test_api_uses_header_auth_no_proxy_or_redirect_and_no_token_in_errors(environment):
    from urllib.error import HTTPError
    e = environment
    client = emby_client.Client(e.account)
    calls = []
    class Response:
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def read(self, limit):return b'{}'
    def open(request, timeout):
        calls.append(request)
        return Response()
    client.opener.open = open
    client.request('Sessions')
    assert calls[0].get_header('X-emby-token') == 'private-test-token'
    assert 'private-test-token' not in calls[0].full_url
    from urllib.request import HTTPRedirectHandler
    redirects = [handler for handler in client.opener.handlers if isinstance(handler, HTTPRedirectHandler)]
    assert len(redirects) == 1 and redirects[0].redirect_request(None, None, None, None, None, None) is None
    def error(*args, **kwargs):
        raise HTTPError('http://emby.invalid/?private-test-token', 403, 'private-test-token', {}, None)
    client.opener.open = error
    with pytest.raises(emby.Unavailable) as caught:
        client.request('Sessions')
    assert 'private-test-token' not in str(caught.value)


def test_exact_index_survives_new_source_without_reading_full_catalogue(environment, monkeypatch):
    from resources.lib.intelligence import emby_index
    e = environment
    request = media_request('播放惊天营救')
    first = emby.Source(e.tools, lambda: False).search(request)
    assert first.exact and len(first.rows) == 1
    index = next((e.profile/'cache').glob('emby-*.sqlite'))
    assert index.stat().st_mode & 0o777 == 0o600
    e.events.clear()
    monkeypatch.setattr(emby, 'catalogue', lambda *args: pytest.fail('Exact lookup loaded the whole catalogue'))
    second = emby.Source(e.tools, lambda: False).search(request)
    assert second.exact and second.rows == first.rows and not e.events
    assert emby_index.lookup(e.profile, e.account, dict(request, year=2021), 300) == []
    assert b'private-test-token' not in index.read_bytes()


@pytest.mark.parametrize('operation', ['corrupt', 'snapshot', 'expired', 'account'])
def test_derived_index_cannot_bypass_cache_freshness_or_account_changes(environment, operation):
    from resources.lib.intelligence import emby_index
    e = environment
    request = media_request('播放惊天营救')
    emby.Source(e.tools, lambda: False).search(request)
    snapshot, index, state_path = emby_index.paths(e.profile, e.account)
    if operation == 'corrupt':
        index.write_bytes(b'not a database')
    elif operation == 'snapshot':
        cache = json.loads(snapshot.read_text()); cache['items'][0]['Name'] = '已经改名'
        snapshot.write_text(json.dumps(cache))
    elif operation == 'expired':
        state = json.loads(state_path.read_text()); state['checked_at'] -= 301
        state_path.write_text(json.dumps(state))
    else:
        e.account['_revision'] = 'another-account-revision'
    assert emby_index.lookup(e.profile, e.account, request, 300) is None
    if operation == 'corrupt':
        assert emby.Source(e.tools, lambda: False).search(request).exact


def test_no_exact_index_hit_preserves_all_fuzzy_candidates(environment):
    e = environment
    source = emby.Source(e.tools, lambda: False)
    source.search(media_request('播放惊天营救'))
    fuzzy = source.search(media_request('播放今天营救'))
    assert not fuzzy.exact and len(fuzzy.rows) == 2
    assert rank('今天营救', fuzzy.rows)[1]


def test_slow_posters_share_a_time_budget_instead_of_waiting_per_candidate(environment, monkeypatch):
    from resources.lib.intelligence import media_actions
    e = environment
    e.catalogue.extend([dict(e.movie, Id='12', ProductionYear=2021), dict(e.movie, Id='13', ProductionYear=2022)])
    clock, attempts = [100.], []
    monkeypatch.setattr(media_actions, 'monotonic', lambda: clock[0])
    class Slow(e.Client):
        def item(self, id):
            attempts.append(id); clock[0] += 3
            raise emby.Unavailable('poster timed out')
    monkeypatch.setattr(emby, 'Client', Slow)
    result = Commands(e.tools).process('播放今天营救')
    assert len(parse(result['reply'], e.tools)['choices']) == 3
    assert attempts == ['11']
