import json

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.commands import Commands, media_request
from resources.lib.intelligence.media_match import rank, normalize
from resources.lib.intelligence.presentation import parse


def item(title, identifier=1, kind='movie', **kwargs):
    return dict(title=title, id=identifier, kind=kind, **kwargs)


@pytest.mark.parametrize('spoken,title', [('今天营救', '惊天营救'), ('急速追杀二', '疾速追杀2'),
    ('黑客帝果', '黑客帝国'), ('奥蓝多', '奥兰多')])
def test_high_written_and_phonetic_matches_can_autoplay(spoken, title):
    rows, automatic = rank(spoken, [item(title), item('无关的片名', 2)])
    assert automatic and rows[0]['row']['title'] == title


def test_a_close_runner_up_or_another_version_requires_selection():
    rows, automatic = rank('黑客帝果', [item('黑客帝国'), item('黑客地锅', 2)])
    assert len(rows) == 2 and not automatic
    rows, automatic = rank('今天营救', [item('惊天营救', year=2020), item('惊天营救', 2, year=2021)])
    assert len(rows) == 2 and not automatic
    assert rank('今天营救', [item('惊天营救', year=2020), item('惊天营救', 2, year=2021)], year=2020)[1]


@pytest.mark.parametrize('query,title', [('疾速追杀2', '疾速追杀3'), ('CCTV1', 'CCTV10'),
    ('央视一', 'CCTV-10 科教'), ('CCTV5+', 'CCTV5 体育'), ('第一季第2集', '第一季第3集')])
def test_numbered_titles_stations_and_episode_identifiers_do_not_collapse(query, title):
    kind = 'channel' if 'CCTV' in title else 'movie'
    assert rank(query, [item(title, kind=kind)]) == ([], False)


def test_missing_numbers_short_names_and_partial_titles_are_choices():
    assert not rank('疾速追杀', [item('疾速追杀2')])[1]
    assert not rank('黑客', [item('黑客帝国')])[1]
    assert not rank('空负', [item('功夫')])[1]
    assert rank('黑客', [item('黑客帝国')])[0]


def test_one_character_titles_only_match_the_whole_written_title():
    rows, automatic = rank('门', [item('门'), item('们', 2), item('门徒', 3)])
    assert automatic and [row['row']['title'] for row in rows] == ['门']
    assert rank('门', [item('们'), item('门徒', 2)]) == ([], False)


def test_channel_aliases_and_quality_suffixes_use_whole_station_identity():
    rows, auto = rank('湖南卫是', [item('湖南卫视高清', kind='channel')])
    assert auto and rows[0]['row']['id'] == 1
    assert rank('央视一', [item('CCTV-1 综合', kind='channel')])[1]
    assert not rank('湖南卫是', [item('湖南卫视高清', kind='channel'),
                               item('湖南卫视标清', 2, kind='channel')])[1]


@pytest.mark.parametrize('phrase,title,kind', [('播放电视剧十八岁的天空', '十八岁的天空', 'tvshow'),
    ('继续看电视剧《十八岁的天空》', '十八岁的天空', 'tvshow'),
    ('播放频道《湖南卫是》', '湖南卫是', 'channel'), ('播放电影黑客帝果', '黑客帝果', 'movie')])
def test_explicit_media_types_are_retained(phrase, title, kind):
    request = media_request(phrase)
    assert request['title'] == title and request['kind'] == kind


@pytest.fixture
def library(tmp_path):
    rows = {'movie': [item('惊天营救'), item('黑客帝国', 2), item('疾速追杀2', 3), item('电影人生', 4)],
            'tvshow': [item('十八岁的天空', 10, 'tvshow')],
            'channel': [item('湖南卫视高清', 20, 'channel')]}
    state = {'history': True, 'fail_open': False}
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        if method == 'PVR.GetProperties': return {'available': True}
        if method == 'Player.Open':
            if state['fail_open']: raise ValueError('播放失败')
            return 'OK'
        if method == 'VideoLibrary.GetEpisodes':
            episode = {'episodeid': 11, 'title': '第一集', 'showtitle': '十八岁的天空', 'season': 1, 'episode': 1,
                       'lastplayed': '2026-10-18 10:00:00' if state['history'] else '',
                       'resume': {'position': 120 if state['history'] else 0, 'total': 2000}, 'playcount': 0}
            return {'episodes': [episode], 'limits': {'start': 0, 'end': 1, 'total': 1}}
        for kind, name in (('movie', 'Movie'), ('tvshow', 'TVShow'), ('channel', 'Channel')):
            if method == ('PVR.GetChannels' if kind == 'channel' else 'VideoLibrary.Get' + name + 's'):
                found = rows[kind]
                if 'filter' in params:
                    found = [row for row in found if row['title'] == params['filter']['value']]
                def convert(row):
                    result = dict(row, **{kind + 'id': row['id']})
                    if kind == 'channel': result.update(channel=row['title'], channeltype='tv')
                    return result
                page = params['limits']
                return {('channels' if kind == 'channel' else kind + 's'): [convert(row) for row in found[page['start']:page['end']]],
                        'limits': {'total': len(found), 'start': page['start'], 'end': min(page['end'], len(found))}}
            if method == ('PVR.GetChannelDetails' if kind == 'channel' else 'VideoLibrary.Get' + name + 'Details'):
                row = next(row for row in rows[kind] if row['id'] == params[kind + 'id'])
                value = dict(row, **{kind + 'id': row['id']})
                if kind == 'channel': value.update(channel=row['title'], channeltype='tv')
                return {kind + 'details': value}
        raise AssertionError(method)
    return rows, state, calls, KodiTools(rpc, profile=tmp_path, defer_ui=True)


def test_high_match_revalidates_cached_title_and_plays_without_model(library):
    rows, state, calls, tools = library
    result = Commands(tools).process('播放黑客帝果')
    assert result['model_invocations'] == 0 and result['matching']['automatic']
    assert calls[-1] == ('Player.Open', {'item': {'movieid': 2}, 'options': {'resume': True}})
    count = len(calls)
    rows['movie'][1]['title'] = '已改名'
    result = Commands(tools).process('播放黑客帝果')
    assert '已变化' in result['reply'] and not any(name == 'Player.Open' for name, _ in calls[count:])


def test_low_matches_offer_verified_choices_and_click_keeps_resume(library):
    _, _, calls, tools = library
    result = Commands(tools).process('播放黑客')
    assert not result['matching']['automatic'] and not tools.acted
    choice = parse(result['reply'], tools)['choices'][0]
    Commands(tools).process('选择', choice)
    assert calls[-1][1] == {'item': {'movieid': 2}, 'options': {'resume': True}}


def test_only_visible_movie_candidates_get_art_and_the_index_stays_lightweight(library):
    rows, _, calls, tools = library
    rows['movie'][1]['art'] = {'poster': 'image://movie-poster/'}
    rows['movie'][1]['thumbnail'] = ''  # Kodi can provide only art.poster, as in the reported film.
    result = Commands(tools).process('播放黑客')
    choice = parse(result['reply'], tools)['choices'][0]
    assert choice['art']['poster'] == 'image://movie-poster/'
    assert tools.media[('movie', 2)]['art'] == choice['art']
    detail_calls = [params for method, params in calls if method == 'VideoLibrary.GetMovieDetails']
    assert len(detail_calls) == 1 and detail_calls[0]['movieid'] == 2
    assert set(detail_calls[0]['properties']) == {'title', 'thumbnail', 'art'}
    cache = json.loads((tools.profile / 'cache/media-search.json').read_text())
    assert not any('art' in row or 'thumbnail' in row for row in cache['groups']['movie'])
    calls.clear()
    Commands(tools).process('播放黑客帝果')
    assert not any(method == 'VideoLibrary.GetMovieDetails' and 'art' in params['properties'] for method, params in calls)


def test_duplicate_tvshow_candidates_get_their_own_posters(library):
    rows, _, calls, tools = library
    rows['tvshow'][0]['art'] = {'poster': 'image://series-first/'}
    rows['tvshow'].append(item('十八岁的天空', 12, 'tvshow', art={'poster': 'image://series-second/'}))
    result = Commands(tools).process('从头看电视剧十八岁的天空')
    choices = parse(result['reply'], tools)['choices']
    assert [row['art']['poster'] for row in choices] == ['image://series-first/', 'image://series-second/']
    assert all(row['_command_resume'] is False for row in choices)
    assert not any(method == 'Player.Open' for method, _ in calls)


@pytest.mark.parametrize('failure', ['missing', 'changed', 'cancelled'])
def test_choice_art_failure_does_not_attach_the_wrong_image_or_swallow_cancellation(library, failure):
    _, _, calls, tools = library
    original = tools.rpc
    def rpc(method, params):
        if method == 'VideoLibrary.GetMovieDetails' and 'art' in params['properties']:
            if failure == 'missing':raise ValueError('没有图片')
            if failure == 'cancelled':raise InterruptedError('已取消')
            return {'moviedetails': {'movieid': 2, 'title': '已经变成其他影片', 'art': {'poster': 'image://wrong/'}}}
        return original(method, params)
    tools.rpc = rpc
    if failure == 'cancelled':
        with pytest.raises(InterruptedError):Commands(tools).process('播放黑客')
    else:
        choices = parse(Commands(tools).process('播放黑客')['reply'], tools)['choices']
        assert choices and not choices[0].get('art')
    assert not any(method == 'Player.Open' for method, _ in calls)


def test_approximate_channel_choices_keep_existing_icons_without_art_queries(library):
    rows, _, calls, tools = library
    rows['channel'][0]['thumbnail'] = 'image://channel-icon/'
    rows['channel'].append(item('湖南卫视标清', 21, 'channel', thumbnail='image://second-icon/'))
    result = Commands(tools).process('播放频道《湖南卫是》')
    choices = parse(result['reply'], tools)['choices']
    assert {row['thumbnail'] for row in choices} == {'image://channel-icon/', 'image://second-icon/'}
    assert not any(method.endswith('Details') for method, _ in calls)


def test_series_resumes_from_history_or_offers_real_episodes_when_unknown(library):
    _, state, calls, tools = library
    result = Commands(tools).process('播放电视剧十八岁的天空')
    assert result['matching']['automatic'] and result['model_invocations'] == 0
    assert calls[-1][1] == {'item': {'episodeid': 11}, 'options': {'resume': True}}
    state['history'] = False
    count = len(calls)
    result = Commands(tools).process('播放电视剧十八岁的天空')
    assert '没有观看记录' in result['reply'] and parse(result['reply'], tools)['choices'][0]['kind'] == 'episode'
    assert not any(name == 'Player.Open' for name, _ in calls[count:])


def test_channel_typo_uses_actual_id_and_rechecks_title(library):
    rows, _, calls, tools = library
    result = Commands(tools).process('播放频道《湖南卫是》')
    assert result['model_invocations'] == 0 and result['matching']['automatic']
    assert calls[-1] == ('Player.Open', {'item': {'channelid': 20}})
    assert tools.ui_action == 'ActivateWindow(FullscreenVideo)'


def test_literal_movie_title_starting_with_type_word_is_not_truncated(library):
    _, _, calls, tools = library
    Commands(tools).process('播放电影人生')
    assert calls[-1] == ('Player.Open', {'item': {'movieid': 4}, 'options': {'resume': True}})


@pytest.mark.parametrize('phrase', ['从头看黑客帝果', '重新播放黑客帝果', '再看一遍黑客帝果'])
def test_approximate_title_preserves_explicit_restart(library, phrase):
    _, _, calls, tools = library
    result = Commands(tools).process(phrase)
    assert result['matching']['automatic']
    assert calls[-1][1] == {'item': {'movieid': 2}, 'options': {'resume': False}}


def test_tvshow_restart_ignores_recent_episode_and_starts_regular_series_beginning(library):
    _, _, calls, tools = library
    original = tools.rpc
    def rpc(method, params):
        result = original(method, params)
        if method == 'VideoLibrary.GetEpisodes':
            first = result['episodes'][0]
            result = {'episodes': [dict(first, episodeid=13, episode=3),
                                  dict(first, episodeid=9, season=0), first],
                      'limits': {'start': 0, 'end': 3, 'total': 3}}
        return result
    tools.rpc = rpc
    result = Commands(tools).process('从头看电视剧十八岁的天空')
    assert result['model_invocations'] == 0
    assert calls[-1][1] == {'item': {'episodeid': 11}, 'options': {'resume': False}}


@pytest.mark.parametrize('prefix,suffix', [('不要把', '从头播放'), ('别把', '重新播放'),
                                        ('播放', '不要从头播放')])
def test_negative_restart_is_not_recovered_as_a_fuzzy_long_title(library, prefix, suffix):
    rows, _, calls, tools = library
    title = '一个非常非常非常长而且语音识别几乎完全正确的电影名称'
    rows['movie'].append(item(title, 100))
    assert Commands(tools).process(prefix + title + suffix) is None
    assert not any(method == 'Player.Open' for method, _ in calls)


def test_play_failure_does_not_fall_back_or_repeat_and_cancel_never_plays(library):
    _, state, calls, tools = library
    state['fail_open'] = True
    result = Commands(tools).process('播放黑客帝果')
    assert not result['tools'][-1]['ok'] and '播放失败' in result['reply']
    assert sum(method == 'Player.Open' for method, _ in calls) == 1
    with pytest.raises(InterruptedError):
        Commands(tools).process('播放黑客帝果', cancelled=lambda: True)
    assert sum(method == 'Player.Open' for method, _ in calls) == 1


def test_index_cache_is_private_and_playback_revalidates_without_reloading_library(library):
    _, _, calls, tools = library
    Commands(tools).process('播放黑客帝果')
    path = tools.profile / 'cache/media-search.json'
    assert path.stat().st_mode & 0o777 == 0o600
    assert set(json.loads(path.read_text())['groups']) == {'movie', 'tvshow'}
    calls.clear()
    Commands(tools).process('播放黑客帝果')
    assert not any(method in ('VideoLibrary.GetMovies', 'VideoLibrary.GetTVShows') and 'filter' not in params
                   for method, params in calls)
    assert any(method == 'VideoLibrary.GetMovieDetails' for method, _ in calls)


@pytest.mark.parametrize('tail', ['然后关机', '并重启', '再打开筛选', '之后把音量调到三十', '然后重新播放'])
def test_long_title_does_not_turn_a_compound_request_into_autoplay(library, tail):
    rows, _, calls, tools = library
    title = '一个非常非常非常长而且语音识别几乎完全正确的电影名称'
    rows['movie'].append(item(title, 100))
    assert Commands(tools).process('播放' + title + tail) is None
    assert not any(method == 'Player.Open' for method, _ in calls)


def test_channel_route_does_not_drop_a_compound_request_guard(library):
    rows, _, calls, tools = library
    title = 'CCTV-1' + '非常' * 10 + '综合频道'
    rows['channel'][0]['title'] = title
    assert Commands(tools).process('播放' + title + '然后关机') is None
    assert not any(method == 'Player.Open' for method, _ in calls)
