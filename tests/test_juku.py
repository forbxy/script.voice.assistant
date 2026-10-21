import json
from urllib.parse import parse_qs, urlsplit

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence import juku
from resources.lib.intelligence.commands import Commands, media_request
from resources.lib.intelligence.juku_playback import builtin, route, share_route
from resources.lib.intelligence.presentation import parse
from resources.lib.intelligence.sessions import Sessions


TITLE = '疾速追杀'


def page(rows):
    return {'items': rows, 'total': len(rows), 'pagination': {'total': len(rows)}}


class API:
    def __init__(self, private=(), online=(), details=None):
        self.private, self.online = list(private), list(online)
        self.details = details or {str(row['id']): row for row in self.online}
        self.calls = []
        self.cancelled = lambda: False

    def check(self):
        if self.cancelled():
            raise InterruptedError('cancelled')

    def get(self, path, params=None, raw=False):
        self.check()
        self.calls.append((path, params))
        if path == '/api/private/libraries': return [{'id': 'lib1'}]
        if path == '/api/private/libraries/lib1/movies': return page(self.private)
        if path == '/api/mv/videos/search': return page(self.online)
        if path.startswith('/api/mv/videos/'): return self.details[path.rsplit('/', 1)[1]]
        if path.endswith('/links'): return [{'id': 'link1', 'link_type': 'direct', 'url': 'https://media.invalid/a.mp4'}]
        if path == '/api/playback/status': return {'resume_seconds': 35}
        if path == '/api/pan/status': return {}
        if path == '/api/pan/order': return {'order': ['7']}
        raise AssertionError(path)

    pages = juku.Client.pages


@pytest.fixture
def tools(monkeypatch):
    # Juku fallback tests use an empty Kodi catalogue and intercept all writes.
    def rpc(name, params):
        if name == 'VideoLibrary.GetMovies': return {'movies': [], 'limits': {'total': 0}}
        if name == 'VideoLibrary.GetTVShows': return {'tvshows': [], 'limits': {'total': 0}}
        if name == 'PVR.GetProperties': return {'available': False}
        raise AssertionError(name)
    monkeypatch.setattr(juku, 'available', lambda tools: True)
    return KodiTools(rpc, defer_ui=True)


def install(monkeypatch, api):
    monkeypatch.setattr(juku, 'Client', lambda cancelled=lambda: False: api)


@pytest.mark.parametrize('source', ['private', 'online'])
@pytest.mark.parametrize('is_tv', [False, True])
@pytest.mark.parametrize('legacy_conflict', [False, True])
def test_is_tv_survives_ranking_and_controls_choice_type_and_playback_route(monkeypatch, tools, source,
                                                                         is_tv, legacy_conflict):
    rows = [{'id': str(i), 'title': TITLE, 'year': 2014 + i, 'isTv': is_tv} for i in (1, 2)]
    if legacy_conflict:
        for row in rows:
            row.update(typeId=1 if is_tv else 2, type_id=1 if is_tv else 2)
    api = API(**{source: rows})
    install(monkeypatch, api)
    outcome = Commands(tools).process('播放' + TITLE)
    choices = parse(outcome['reply'], tools)['choices']
    assert len(choices) == 2 and not tools.acted
    assert all(row['media_type'] == ('tvshow' if is_tv else 'movie') for row in choices)
    assert all((' · 电视剧 · ' if is_tv else ' · 电影 · ') in row['label'] for row in choices)
    assert not any(path.startswith('/api/mv/videos/') and path != '/api/mv/videos/search' for path, _ in api.calls)
    Commands(tools).process('选择', choices[0])
    params = parse_qs(urlsplit(tools.ui_action.split('"')[1]).query)
    assert params['is_tv'] == ['1' if is_tv else '0']
    assert params['type_id'] == ['2' if is_tv else '1']
    if source == 'private':
        assert tools.ui_action.startswith('ActivateWindow' if is_tv else 'PlayMedia')


@pytest.mark.parametrize('source', ['private', 'online'])
@pytest.mark.parametrize('phrase', ['库里有没有疾速追杀', '从库里搜索疾速追沙'])
def test_juku_library_search_only_offers_candidates_until_selected(monkeypatch, tools, source, phrase):
    api = API(**{source: [dict(id='1', title=TITLE, year=2014, isTv=False)]})
    install(monkeypatch, api)
    result = Commands(tools).process(phrase)
    choices = parse(result['reply'], tools)['choices']
    assert len(choices) == 1 and choices[0]['title'] == TITLE
    assert choices[0]['_juku_source'] == source and choices[0]['media_type'] == 'movie'
    assert not tools.acted and tools.ui_action is None
    assert not result['matching']['automatic'] and result['matching']['target'] is None
    assert not any(path.startswith('/api/mv/videos/') and path != '/api/mv/videos/search'
                   or path.endswith('/links') for path, _ in api.calls)
    Commands(tools).process('选择', choices[0])
    assert tools.acted and 'plugin.video.juku' in tools.ui_action


def test_explicit_season_uses_juku_resource_picker_instead_of_unverified_single_file(monkeypatch, tools):
    row = {'id': 's1', 'title': '百变大咖秀', 'year': 2012, 'type_id': 2,
           'panList': {'7': [{'url': 'https://115.com/s/example', 'id': 'link'}]}}
    api = API(online=[row]); install(monkeypatch, api)
    result = Commands(tools).process('播放第二季百变大咖秀')
    assert tools.ui_action.startswith('ActivateWindow(Videos,')
    assert '第2季' in parse(result['reply'], tools)['message']
    assert not any(path == '/api/list' for path, _ in api.calls)


def test_absent_juku_is_silent_without_http_or_addon_rpc(monkeypatch):
    monkeypatch.setattr(juku.Path, 'is_file', lambda path: False)
    tools = KodiTools(lambda *a: pytest.fail('No addon RPC for absent Juku'))
    assert not juku.available(tools)
    monkeypatch.setattr(juku, 'Client', lambda *a: pytest.fail('No HTTP'))
    assert not juku.Source(tools, lambda: False).search('private', media_request('播放测试')).rows


@pytest.mark.parametrize('addon', [{}, {'addonid': juku.ADDON, 'enabled': False},
                                 {'addonid': 'another', 'enabled': True}])
def test_disabled_or_unregistered_juku_skips(monkeypatch, addon):
    monkeypatch.setattr(juku.Path, 'is_file', lambda path: True)
    assert not juku.available(KodiTools(lambda *args: {'addon': addon}))


def test_private_exact_precedes_online_and_handoff_is_deferred(monkeypatch, tools):
    api = API(private=[{'id': 'private1', 'title': TITLE, 'type_id': 1, 'year': 2014}])
    install(monkeypatch, api)
    result = Commands(tools).process('播放' + TITLE)
    assert result['model_invocations'] == 0 and tools.acted
    assert not any('/api/mv/' in path for path, _ in api.calls)
    assert tools.ui_action.startswith('PlayMedia("plugin://plugin.video.juku/?')
    assert 'resume_time=35' in tools.ui_action and tools.ui_action.endswith(',resume)')
    assert 'media.invalid' not in result['reply'] and 'media.invalid' not in json.dumps(result['tools'])


def test_local_match_never_queries_juku(monkeypatch):
    monkeypatch.setattr(juku, 'Client', lambda *a: pytest.fail('Local result must win'))
    movie = {'movieid': 1, 'title': TITLE, 'year': 2014}
    def rpc(name, params):
        if name == 'VideoLibrary.GetMovies': return {'movies': [movie], 'limits': {'total': 1}}
        if name == 'Player.Open': return 'OK'
        raise AssertionError(name)
    assert Commands(KodiTools(rpc)).process('播放' + TITLE)['model_invocations'] == 0


def test_private_empty_falls_back_to_online_and_never_calls_voice_or_poll(monkeypatch, tools):
    api = API(online=[{'id': '42', 'title': TITLE, 'year': 2014, 'typeId': 1}])
    install(monkeypatch, api)
    result = Commands(tools).process('播放' + TITLE)
    assert '资源页面' in parse(result['reply'], tools)['message']
    paths = [path for path, _ in api.calls]
    assert paths.index('/api/private/libraries/lib1/movies') < paths.index('/api/mv/videos/search')
    assert not any('voice/command' in path or 'push/poll' in path for path in paths)
    assert tools.ui_action.startswith('ActivateWindow(Videos,') and 'action=detail' in tools.ui_action


def test_all_empty_yields_to_existing_agent_flow(monkeypatch, tools):
    api = API()
    install(monkeypatch, api)
    assert Commands(tools).process('播放完全不存在的影片') is None
    assert tools.ui_action is None and not tools.acted


def test_ambiguous_online_results_stay_in_assistant_and_survive_session(monkeypatch, tools, tmp_path):
    rows = [{'id': str(i), 'title': TITLE, 'year': 2014 + i, 'typeId': 1} for i in (1, 2)]
    api = API(online=rows)
    install(monkeypatch, api)
    outcome = Commands(tools).process('从头播放' + TITLE)
    choices = parse(outcome['reply'], tools)['choices']
    assert len(choices) == 2 and not tools.acted and tools.ui_action is None
    sessions = Sessions(tmp_path, 'forever', signature='same')
    sessions.completed(tools.media.values())
    loaded = Sessions(tmp_path, 'forever', signature='same')
    next_tools = KodiTools(tools.rpc, media=loaded.media, defer_ui=True)
    choice = dict(choices[1], _juku_id='evil', _juku_request={'resume': True})
    result = Commands(next_tools).process('播放选择', choice)
    assert 'id=2' in next_tools.ui_action and 'evil' not in next_tools.ui_action
    assert result['model_invocations'] == 0


@pytest.mark.parametrize('query', ['激素追杀', '今天营救', '傲兰多'])
def test_external_query_is_literal_without_pinyin_or_fuzzy_rewrites(monkeypatch, tools, query):
    api = API()
    install(monkeypatch, api)
    assert Commands(tools).process('播放' + query) is None
    assert [params['q'] for path, params in api.calls if path.endswith('/search')] == [query]
    assert [params['keyword'] for path, params in api.calls if path.endswith('/movies')] == [query]


def test_partial_title_does_not_autoplay_a_sequel(monkeypatch, tools):
    api = API(online=[{'id': '2', 'title': TITLE + '2', 'year': 2017}])
    install(monkeypatch, api)
    outcome = Commands(tools).process('播放' + TITLE)
    assert len(parse(outcome['reply'], tools)['choices']) == 1
    assert not tools.acted and tools.ui_action is None


def test_year_and_type_are_verified_from_online_detail(monkeypatch, tools):
    api = API(online=[{'id': '1', 'title': TITLE, 'year': 2014},
                      {'id': '2', 'title': TITLE, 'year': 2014}], details={
        '1': {'id': '1', 'title': TITLE, 'year': 2014, 'typeId': 2},
        '2': {'id': '2', 'title': TITLE, 'year': 2014, 'typeId': 1}})
    install(monkeypatch, api)
    Commands(tools).process('播放电影《' + TITLE + '》（2014）')
    assert 'id=2' in tools.ui_action


def test_stale_or_forged_selection_cannot_play(monkeypatch, tools):
    api = API(online=[{'id': '1', 'title': TITLE + '2', 'year': 2017}])
    install(monkeypatch, api)
    result = Commands(tools).process('播放' + TITLE)
    choice = parse(result['reply'], tools)['choices'][0]
    api.details['1'] = {'id': '1', 'title': '另一部片子', 'year': 2017}
    outcome = Commands(tools).process('选择', choice)
    assert '已变化' in outcome['reply'] and tools.ui_action is None
    outcome = Commands(tools).process('选择', dict(choice, id=999))
    assert '不在当前查询结果' in outcome['reply']


def test_cancellation_after_reads_never_hands_off(monkeypatch, tools):
    api = API(online=[{'id': '1', 'title': TITLE, 'year': 2014, 'typeId': 1}])
    original = api.get
    def get(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == '/api/mv/videos/1': api.cancelled = lambda: True
        return value
    api.get = get
    install(monkeypatch, api)
    with pytest.raises(InterruptedError):
        Commands(tools).process('播放' + TITLE)
    assert not tools.acted and tools.ui_action is None


def test_private_read_failure_is_not_reported_as_no_results(monkeypatch, tools):
    api = API()
    api.get = lambda *args, **kwargs: (_ for _ in ()).throw(juku.Unavailable('unavailable'))
    install(monkeypatch, api)
    progress = []
    assert Commands(tools).process('播放' + TITLE, progress=progress.append) is None
    assert '暂时不可用' in progress[-1] and not tools.acted


def test_truncated_search_never_autoplays(monkeypatch, tools):
    api = API(online=[{'id': '1', 'title': TITLE, 'year': 2014}])
    api.pages = lambda path, *a, **kw: (api.private, True) if '/private/' in path else (api.online, False)
    install(monkeypatch, api)
    result = Commands(tools).process('播放' + TITLE)
    assert parse(result['reply'], tools)['choices'] and not tools.acted


def test_private_failure_still_allows_online_choices(monkeypatch, tools):
    api = API(online=[{'id': '1', 'title': TITLE, 'year': 2014}])
    original = api.get
    def get(path, *args, **kwargs):
        if '/private/' in path: raise juku.Unavailable('private unavailable')
        return original(path, *args, **kwargs)
    api.get = get
    install(monkeypatch, api)
    result = Commands(tools).process('播放' + TITLE)
    assert parse(result['reply'], tools)['choices'] and not tools.acted


def test_external_permission_ceiling_prevents_handoff(monkeypatch, tools):
    api = API(online=[{'id': '1', 'title': TITLE, 'year': 2014}])
    install(monkeypatch, api)
    from types import SimpleNamespace
    tools.permissions = SimpleNamespace(allows=lambda name: False)
    result = Commands(tools).process('播放' + TITLE)
    assert '权限不允许' in result['reply'] and not tools.acted and tools.ui_action is None


def test_restart_removes_private_resume_offset(monkeypatch, tools):
    api = API(private=[{'id': 'p1', 'title': TITLE, 'year': 2014, 'type_id': 1}])
    install(monkeypatch, api)
    Commands(tools).process('从头播放' + TITLE)
    assert tools.ui_action.endswith(',noresume)') and 'resume_time' not in tools.ui_action
    assert not any(path == '/api/playback/status' for path, _ in api.calls)


def test_private_share_single_file_uses_juku_parser_and_encodes_once():
    class ShareAPI(API):
        def get(self, path, params=None, raw=False):
            assert path == '/api/list'
            return {'files': [{'file_id': 'f+/=', 'file_name': '电影.mkv'}], 'total': 1}
    params = {'url': 'https://115.com/s/a?password=x&b=2', 'code': 'a"),Reboot(',
              'vod_title': '电影&标题', 'pan_name': '7', 'movie_id': 'private1'}
    command, folder = share_route(ShareAPI(), params, False)
    assert command.endswith(',noresume)') and not folder
    query = parse_qs(urlsplit(command.split('"')[1]).query)
    assert query['url'] == ['https://115.com/s/a?password=x&b=2']
    assert query['code'] == ['a"),Reboot('] and query['file_id'] == ['f+/=']
    assert query['action'] == ['pan_play'] and query['movie_id'] == ['private1']


@pytest.mark.parametrize('files,total', [([], 0), ([{'file_id': 'f', 'is_dir': True}], 1),
    ([{'file_id': 'f'}], 20), ([{'file_id': 'f'}, {'file_id': 'g'}], 2)])
def test_multiple_or_incomplete_share_files_open_picker(files, total):
    api = API()
    api.get = lambda *args, **kwargs: {'files': files, 'total': total}
    command, folder = share_route(api, {'url': 'https://115.com/s/a', 'pan_name': '7', 'movie_id': 'm1'}, True)
    assert folder and command.startswith('ActivateWindow(') and 'movie_id=m1' in command


def test_http_client_size_protocol_and_private_errors_are_bounded():
    class Response:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return self.data[:limit]
    class Opener:
        def __init__(self, data): self.data = data
        def open(self, url, timeout): return Response(self.data)
    client = juku.Client()
    for body in (b'not json', b'{}', b'{"success":false,"password":"private"}', b'x' * (2*1024*1024+1)):
        client.opener = Opener(body)
        with pytest.raises(juku.Unavailable) as error:
            client.get('/api/private/libraries')
        assert 'private' not in str(error.value)


def test_pagination_collects_all_and_rejects_missing_rows():
    client = API()
    def get(path, params):
        return {'items': [{'id': params['page']}], 'pagination': {'total': 2}}
    client.get = get
    rows, complete = client.pages('/api/mv/videos/search', {})
    assert complete and [row['id'] for row in rows] == [1, 2]
    client.get = lambda *args: {'items': [], 'pagination': {'total': 2}}
    with pytest.raises(juku.Unavailable): client.pages('/api/mv/videos/search', {})


@pytest.fixture
def priority_library(monkeypatch):
    movies = [{'movieid': 1, 'title': '希望的另一面', 'year': 2017},
              {'movieid': 2, 'title': '希望与荣耀', 'year': 1987}]
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        if method == 'VideoLibrary.GetMovies':
            rows = movies
            if 'filter' in params:
                rules = params['filter'].get('and', [params['filter']])
                rows = [row for row in rows if all(str(row.get(rule['field'], '')) == rule['value'] for rule in rules)]
            return {'movies': rows, 'limits': {'total': len(rows)}}
        if method == 'VideoLibrary.GetTVShows': return {'tvshows': [], 'limits': {'total': 0}}
        if method == 'PVR.GetProperties': return {'available': False}
        if method == 'VideoLibrary.GetMovieDetails':
            return {'moviedetails': next(row for row in movies if row['movieid'] == params['movieid'])}
        if method == 'Player.Open': return 'OK'
        raise AssertionError(method)
    monkeypatch.setattr(juku, 'available', lambda tools: True)
    return movies, calls, KodiTools(rpc, defer_ui=True)


def test_juku_exact_precedes_local_partial_choices_without_loading_their_posters(monkeypatch, priority_library):
    _, calls, tools = priority_library
    api = API(online=[{'id': '42', 'title': '希望', 'year': 2026, 'typeId': 1}])
    install(monkeypatch, api)
    outcome = Commands(tools).process('播放希望')
    assert outcome['model_invocations'] == 0 and tools.acted
    assert 'id=42' in tools.ui_action and 'plugin.video.juku' in tools.ui_action
    assert not any(name in ('Player.Open', 'VideoLibrary.GetMovieDetails') for name, _ in calls)
    assert not parse(outcome['reply'], tools)['choices']
    assert sum(path == '/api/mv/videos/search' for path, _ in api.calls) == 1


def test_juku_exact_also_precedes_local_high_phonetic_autoplay(monkeypatch, priority_library):
    movies, calls, tools = priority_library
    movies[:] = [{'movieid': 1, 'title': '惊天营救', 'year': 2020}]
    api = API(online=[{'id': '42', 'title': '今天营救', 'year': 2026, 'typeId': 1}])
    install(monkeypatch, api)
    outcome = Commands(tools).process('播放今天营救')
    assert 'id=42' in tools.ui_action and outcome['model_invocations'] == 0
    assert not any(name == 'Player.Open' for name, _ in calls)


def test_juku_multiple_exact_years_replace_local_partial_choices_and_keep_restart(monkeypatch, priority_library):
    _, calls, tools = priority_library
    api = API(online=[{'id': '42', 'title': '希望', 'year': 2026, 'typeId': 1},
                      {'id': '43', 'title': '希望', 'year': 1970, 'typeId': 1},
                      {'id': '44', 'title': '希望之旅', 'year': 1990, 'typeId': 1}])
    install(monkeypatch, api)
    outcome = Commands(tools).process('从头播放希望')
    choices = parse(outcome['reply'], tools)['choices']
    assert {row['year'] for row in choices} == {2026, 1970}
    assert all(row['kind'] == 'juku' and row['title'] == '希望' and not row['_juku_request']['resume'] for row in choices)
    assert not tools.acted and tools.ui_action is None
    assert not any(name in ('Player.Open', 'VideoLibrary.GetMovieDetails') for name, _ in calls)


@pytest.mark.parametrize('count', [1, 2])
def test_local_complete_titles_never_query_juku_even_with_multiple_versions(monkeypatch, priority_library, count):
    movies, calls, tools = priority_library
    movies[:] = [{'movieid': i, 'title': '希望', 'year': 2020 + i} for i in range(1, count + 1)]
    monkeypatch.setattr(juku, 'Client', lambda *a: pytest.fail('Local complete title must win'))
    outcome = Commands(tools).process('播放希望')
    assert outcome['model_invocations'] == 0
    assert sum(name == 'Player.Open' for name, _ in calls) == (count == 1)
    assert len(parse(outcome['reply'], tools)['choices']) == (0 if count == 1 else count)


@pytest.mark.parametrize('failure', ['no_exact', 'unavailable', 'absent'])
@pytest.mark.parametrize('automatic', [False, True])
def test_without_juku_exact_existing_local_selection_and_autoplay_still_work(
        monkeypatch, priority_library, failure, automatic):
    movies, calls, tools = priority_library
    if automatic:
        movies[:] = [{'movieid': 1, 'title': '惊天营救', 'year': 2020}]
    query = '今天营救' if automatic else '希望'
    api = API(online=[{'id': '42', 'title': query + '之旅', 'year': 2026, 'typeId': 1}])
    install(monkeypatch, api)
    if failure == 'unavailable':
        api.get = lambda *a, **k: (_ for _ in ()).throw(juku.Unavailable('unavailable'))
    elif failure == 'absent':
        monkeypatch.setattr(juku, 'available', lambda tools: False)
        monkeypatch.setattr(juku, 'Client', lambda *a: pytest.fail('Missing addon must stay silent'))
    progress = []
    outcome = Commands(tools).process('播放' + query, progress=progress.append)
    assert outcome['matching']['automatic'] is automatic and outcome['model_invocations'] == 0
    assert sum(name == 'Player.Open' for name, _ in calls) == automatic
    shown = parse(outcome['reply'], tools)['choices']
    mixed = failure == 'no_exact' and not automatic
    assert {row['kind'] for row in shown} == ({'movie', 'juku'} if mixed else {'movie'} if shown else set())
    if not automatic:
        assert {row['title'] for row in shown} == {'希望的另一面', '希望与荣耀'} | ({'希望之旅'} if mixed else set())
    if failure == 'absent':
        assert not any('巨库' in message for message in progress)
    assert sum(path == '/api/mv/videos/search' for path, _ in api.calls) <= 1


def test_private_partial_does_not_hide_online_exact(monkeypatch, priority_library):
    _, calls, tools = priority_library
    api = API(private=[{'id': 'p1', 'title': '希望之旅', 'year': 1990, 'type_id': 1}],
              online=[{'id': '42', 'title': '希望', 'year': 2026, 'typeId': 1}])
    install(monkeypatch, api)
    Commands(tools).process('播放希望')
    assert 'action=detail' in tools.ui_action and 'id=42' in tools.ui_action
    assert not any(name == 'Player.Open' for name, _ in calls)
    assert sum(path == '/api/mv/videos/search' for path, _ in api.calls) == 1


def test_local_empty_private_partial_still_checks_online_exact(monkeypatch, tools):
    api = API(private=[{'id': 'p1', 'title': '希望之旅', 'year': 1990, 'type_id': 1}],
              online=[{'id': '42', 'title': '希望', 'year': 2026, 'typeId': 1}])
    install(monkeypatch, api)
    Commands(tools).process('播放希望')
    assert tools.acted and 'id=42' in tools.ui_action
    assert sum(path == '/api/mv/videos/search' for path, _ in api.calls) == 1


def test_incomplete_juku_exact_cannot_autoplay_over_local_choices(monkeypatch, priority_library):
    _, calls, tools = priority_library
    api = API(online=[{'id': '42', 'title': '希望', 'year': 2026, 'typeId': 1}])
    api.pages = lambda path, *a, **kw: (api.private, True) if '/private/' in path else (api.online, False)
    install(monkeypatch, api)
    outcome = Commands(tools).process('播放希望')
    assert {row['kind'] for row in parse(outcome['reply'], tools)['choices']} == {'juku'}
    assert not tools.acted and tools.ui_action is None
    assert not any(name == 'Player.Open' for name, _ in calls)


def test_cancelling_juku_priority_query_does_not_fall_through_to_local_play(monkeypatch, priority_library):
    movies, calls, tools = priority_library
    movies[:] = [{'movieid': 1, 'title': '惊天营救', 'year': 2020}]
    api = API()
    api.get = lambda *a, **k: (_ for _ in ()).throw(InterruptedError('cancelled'))
    install(monkeypatch, api)
    with pytest.raises(InterruptedError):
        Commands(tools).process('播放今天营救')
    assert not tools.acted and tools.ui_action is None
    assert not any(name == 'Player.Open' for name, _ in calls)


def test_normalized_local_whole_title_still_precedes_juku(monkeypatch, priority_library):
    movies, calls, tools = priority_library
    movies[:] = [{'movieid': 1, 'title': '疾速追杀2', 'year': 2017}]
    monkeypatch.setattr(juku, 'Client', lambda *a: pytest.fail('Normalized complete local title must win'))
    outcome = Commands(tools).process('播放疾速追杀二')
    assert outcome['matching']['automatic'] and outcome['matching']['best_score'] == 1
    assert calls[-1] == ('Player.Open', {'item': {'movieid': 1}, 'options': {'resume': True}})


def test_explicit_year_can_disambiguate_juku_over_local_partial(monkeypatch, priority_library):
    movies, calls, tools = priority_library
    movies[:] = [{'movieid': 1, 'title': '希望之旅', 'year': 2026}]
    api = API(online=[{'id': '42', 'title': '希望', 'year': 2026, 'typeId': 1},
                      {'id': '43', 'title': '希望', 'year': 1970, 'typeId': 1}])
    install(monkeypatch, api)
    outcome = Commands(tools).process('播放《希望》（2026）')
    assert outcome['model_invocations'] == 0 and 'id=42' in tools.ui_action
    assert not any(name == 'Player.Open' for name, _ in calls)


def test_sources_only_read_and_do_not_register_choices_or_execute_playback(priority_library, monkeypatch):
    from resources.lib.intelligence import command_media
    _, calls, tools = priority_library
    tools.media[('movie',999)]={'kind':'movie','id':999,'title':'保留条目'}
    tools.latest_matches=['preserved']
    local = command_media.search(tools,media_request('播放希望'),lambda:False,lambda value:None)
    api=API(online=[{'id':'42','title':'希望','year':2026,'typeId':1}]);install(monkeypatch,api)
    external=juku.Source(tools,lambda:False).search('online',media_request('播放希望'))
    assert local.rows and external.exact and not external.automatic
    assert tools.media=={('movie',999):{'kind':'movie','id':999,'title':'保留条目'}}
    assert tools.latest_matches==['preserved'] and not tools.acted and tools.ui_action is None
    assert not any(name=='Player.Open' for name,_ in calls)


def test_external_route_failure_preserves_local_candidates(monkeypatch, priority_library):
    _,calls,tools=priority_library
    api=API(online=[{'id':'42','title':'希望','year':2026,'typeId':1}]);install(monkeypatch,api)
    original=api.get
    def get(path,*args,**kwargs):
        if path=='/api/mv/videos/42':raise juku.Unavailable('route unavailable')
        return original(path,*args,**kwargs)
    api.get=get
    outcome=Commands(tools).process('播放希望')
    assert {row['kind'] for row in parse(outcome['reply'],tools)['choices']}=={'movie'}
    assert not tools.acted and not any(name=='Player.Open' for name,_ in calls)


@pytest.mark.parametrize('source', ['private', 'online'])
def test_juku_phonetic_candidate_autoplays_with_the_local_score(monkeypatch, tools, source):
    from resources.lib.intelligence.media_match import rank
    row = {'id': '42', 'title': '疾速追杀', 'year': 2014, 'typeId': 1, 'score': 1}
    api = API(**{source: [row]})
    install(monkeypatch, api)
    outcome = Commands(tools).process('播放疾速追沙')
    expected = rank('疾速追沙', [{'kind': 'movie', 'id': 1, 'title': row['title']}])[0][0]['score']
    assert outcome['matching']['automatic'] and outcome['matching']['best_score'] == expected
    assert outcome['matching']['target']['source'] == source
    assert tools.acted and 'plugin.video.juku' in tools.ui_action
    assert outcome['model_invocations'] == 0


def test_better_juku_fuzzy_match_precedes_lower_local_matches(monkeypatch, priority_library):
    movies, calls, tools = priority_library
    movies[:] = [{'movieid': 1, 'title': '疾速追杀外传', 'year': 2020}]
    api = API(online=[{'id': '42', 'title': '疾速追杀', 'year': 2014, 'typeId': 1}])
    install(monkeypatch, api)
    outcome = Commands(tools).process('播放疾速追沙')
    assert outcome['matching']['automatic'] and 'id=42' in tools.ui_action
    assert not any(name == 'Player.Open' for name, _ in calls)


def test_close_candidates_across_sources_share_one_margin_and_choices(monkeypatch, priority_library):
    movies, calls, tools = priority_library
    movies[:] = [{'movieid': 1, 'title': '惊天营救', 'year': 2020}]
    api = API(online=[{'id': '1', 'title': '京天营救', 'year': 2026, 'typeId': 1}])
    install(monkeypatch, api)
    outcome = Commands(tools).process('从头播放今天营救')
    choices = parse(outcome['reply'], tools)['choices']
    assert [(row['kind'], row['title']) for row in choices] == [('movie', '惊天营救'), ('juku', '京天营救')]
    assert not outcome['matching']['automatic'] and not tools.acted and tools.ui_action is None
    assert outcome['matching']['best_score'] == outcome['matching']['second_score']
    assert choices[0]['_command_resume'] is False and choices[1]['_juku_request']['resume'] is False
    # Selection uses its own source/ID, even when both catalogues return ID 1.
    selected = Commands(tools).process('选择', choices[1])
    assert selected['model_invocations'] == 0 and 'id=1' in tools.ui_action
    assert not any(name == 'Player.Open' for name, _ in calls)


def test_private_near_match_does_not_hide_better_online_fuzzy_match(monkeypatch, tools):
    api = API(private=[{'id': 'p', 'title': '疾速追杀外传', 'year': 2020, 'type_id': 1}],
              online=[{'id': '42', 'title': '疾速追杀', 'year': 2014, 'typeId': 1}])
    install(monkeypatch, api)
    outcome = Commands(tools).process('播放疾速追沙')
    assert outcome['matching']['automatic'] and outcome['matching']['target']['source'] == 'online'
    assert 'id=42' in tools.ui_action


def test_medium_candidates_are_merged_and_sorted_instead_of_local_only(monkeypatch, priority_library):
    _, _, tools = priority_library
    api = API(online=[{'id': '42', 'title': '希望呀', 'year': 2026, 'typeId': 1}])
    install(monkeypatch, api)
    outcome = Commands(tools).process('播放希望')
    choices = parse(outcome['reply'], tools)['choices']
    assert [row['title'] for row in choices] == ['希望呀', '希望的另一面', '希望与荣耀']
    assert choices[0]['kind'] == 'juku' and ' · 电影 · 巨库在线目录' in choices[0]['label']
    assert not tools.acted


@pytest.mark.parametrize('title', ['楚门的世界', '疾速追杀3'])
def test_remote_autoplay_score_and_alias_cannot_override_title_or_sequel(monkeypatch, tools, title):
    api = API(online=[{'id': '42', 'title': title, 'year': 2014, 'typeId': 1,
                      'score': 100, 'autoPlay': True, 'matchField': 'alias', 'matchedText': '疾速追杀2'}])
    install(monkeypatch, api)
    assert Commands(tools).process('播放疾速追杀2') is None
    assert not tools.acted and not tools.media


def test_low_confidence_candidates_fall_through_to_ai(monkeypatch, tools):
    install(monkeypatch, API(online=[{'id': '42', 'title': '完全无关的片名', 'year': 2026}]))
    assert Commands(tools).process('播放疾速追沙') is None
    assert not tools.acted and tools.latest_matches == []


def test_incomplete_fuzzy_result_requires_choice(monkeypatch, tools):
    api = API(online=[{'id': '42', 'title': '疾速追杀', 'year': 2014, 'typeId': 1}])
    api.pages = lambda path, *a, **kw: (api.private, True) if '/private/' in path else (api.online, False)
    install(monkeypatch, api)
    outcome = Commands(tools).process('播放疾速追沙')
    assert not outcome['matching']['automatic'] and '查询不完整' in outcome['reply']
    assert len(parse(outcome['reply'], tools)['choices']) == 1 and not tools.acted


def test_duplicate_api_rows_do_not_create_artificial_competitors(monkeypatch, tools):
    row = {'id': '42', 'title': '疾速追杀', 'year': 2014, 'typeId': 1}
    install(monkeypatch, API(online=[row, dict(row)]))
    result = Commands(tools).process('播放疾速追沙')
    assert result['matching']['automatic'] and result['matching']['candidates'] == 1 and tools.acted


def test_one_character_exact_juku_title_keeps_existing_playback(monkeypatch, tools):
    install(monkeypatch, API(online=[{'id': '42', 'title': '门', 'year': 2007, 'typeId': 1}]))
    result = Commands(tools).process('播放《门》')
    assert result['matching']['best_score'] == 1 and result['matching']['automatic']
    assert tools.acted and 'id=42' in tools.ui_action


def test_same_film_in_juku_does_not_block_local_fuzzy_autoplay(monkeypatch, priority_library):
    movies, calls, tools = priority_library
    movies[:] = [{'movieid': 1, 'title': '疾速追杀', 'year': 2014}]
    install(monkeypatch, API(online=[{'id': '42', 'title': '疾速追杀', 'year': 2014, 'typeId': 1}]))
    outcome = Commands(tools).process('播放疾速追沙')
    assert outcome['matching']['automatic'] and outcome['matching']['target']['kind'] == 'movie'
    assert calls[-1] == ('Player.Open', {'item': {'movieid': 1}, 'options': {'resume': True}})


def test_same_film_online_does_not_block_private_fuzzy_autoplay(monkeypatch, tools):
    row = {'id': 'p', 'title': '疾速追杀', 'year': 2014, 'type_id': 1}
    install(monkeypatch, API(private=[row], online=[dict(row, id='42')]))
    outcome = Commands(tools).process('播放疾速追沙')
    assert outcome['matching']['automatic'] and outcome['matching']['target']['source'] == 'private'
    assert 'movie_id=p' in tools.ui_action


@pytest.mark.parametrize('other', [
    {'year': 2026, 'typeId': 1}, {'year': 2014, 'typeId': 2},
    {'year': 0, 'typeId': 1}, {'year': 2014},
])
def test_unknown_or_distinct_versions_are_not_deduplicated(monkeypatch, priority_library, other):
    movies, _, tools = priority_library
    movies[:] = [{'movieid': 1, 'title': '疾速追杀', 'year': 2014}]
    install(monkeypatch, API(online=[dict(id='42', title='疾速追杀', **other)]))
    outcome = Commands(tools).process('播放疾速追沙')
    assert not outcome['matching']['automatic'] and not tools.acted
    assert len(parse(outcome['reply'], tools)['choices']) == 2


def test_same_source_versions_still_require_a_choice_when_cross_source_duplicates_exist(monkeypatch, priority_library):
    movies, _, tools = priority_library
    movies[:] = [{'movieid': i, 'title': '疾速追杀', 'year': 2014} for i in (1, 2)]
    install(monkeypatch, API(online=[{'id': '42', 'title': '疾速追杀', 'year': 2014, 'typeId': 1}]))
    outcome = Commands(tools).process('播放疾速追沙')
    choices = parse(outcome['reply'], tools)['choices']
    assert not outcome['matching']['automatic'] and not tools.acted
    assert [(row['kind'], row['id']) for row in choices] == [('movie', 1), ('movie', 2)]
