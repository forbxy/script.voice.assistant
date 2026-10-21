import json

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.command_rules import common_request
from resources.lib.intelligence.commands import Commands
from resources.lib.intelligence.presentation import parse


@pytest.fixture
def history():
    state = {
        'players': [], 'item': {}, 'available': True,
        'movies': [{'movieid': 1, 'title': '电影甲', 'lastplayed': '2026-10-18 20:00:00',
                    'playcount': 0, 'resume': {'position': 120, 'total': 7200}}],
        'tvshows': [{'tvshowid': 3, 'title': '电视剧甲', 'lastplayed': '2026-10-18 19:00:00'}],
        'episodes': [
            {'episodeid': 30, 'title': '第一集', 'showtitle': '电视剧甲', 'season': 1, 'episode': 1,
             'lastplayed': '2026-10-18 19:00:00', 'playcount': 0, 'resume': {'position': 100, 'total': 2000}},
            {'episodeid': 31, 'title': '第二集', 'showtitle': '电视剧甲', 'season': 1, 'episode': 2,
             'lastplayed': '', 'playcount': 0, 'resume': {'position': 0, 'total': 0}}],
        # Serialized dates tie, but Kodi's full timestamp sort already ranked 8 first.
        'channels': [{'channelid': 8, 'channel': '频道乙', 'channeltype': 'tv', 'lastplayed': '2026-10-18'},
                     {'channelid': 7, 'channel': '频道甲', 'channeltype': 'tv', 'lastplayed': '2026-10-18'}]}
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        if method == 'Player.GetActivePlayers': return state['players'][:]
        if method == 'Player.GetItem': return {'item': state['item']}
        if method in ('Player.Open', 'Player.PlayPause'): return 'OK'
        if method == 'PVR.GetProperties': return {'available': state['available']}
        if method == 'PVR.GetChannelDetails':
            return {'channeldetails': next(x for x in state['channels'] if x['channelid'] == params['channelid'])}
        details = {'VideoLibrary.GetMovieDetails': ('movie', 'moviedetails'),
                   'VideoLibrary.GetEpisodeDetails': ('episode', 'episodedetails'),
                   'VideoLibrary.GetTVShowDetails': ('tvshow', 'tvshowdetails')}
        if method in details:
            kind, key = details[method]
            return {key: next(x for x in state[kind + 's'] if x[kind + 'id'] == params[kind + 'id'])}
        listings = {'VideoLibrary.GetMovies': 'movies', 'VideoLibrary.GetTVShows': 'tvshows',
                    'VideoLibrary.GetEpisodes': 'episodes', 'PVR.GetChannels': 'channels'}
        if method in listings:
            key = listings[method]
            rows = state[key]
            if key in ('movies', 'tvshows'):
                rows = sorted(rows, key=lambda row: row.get('lastplayed', ''), reverse=True)
            start, end = params['limits']['start'], params['limits']['end']
            return {key: rows[start:end], 'limits': {'start': start, 'end': min(end, len(rows)), 'total': len(rows)}}
        raise AssertionError(method)
    tools = KodiTools(rpc, defer_ui=True)
    return state, calls, tools


@pytest.mark.parametrize('phrase,kind', [('继续看电影', 'movie'), ('继续看电视', 'channel'),
    ('继续看电视剧', 'tvshow'), ('请帮我继续看电视剧。', 'tvshow'), ('接着看电影', 'movie'),
    ('继续播放电影', 'movie'), ('继续播放电视剧', 'tvshow'), ('继续播放', 'all'),
    ('继续', 'all'), ('恢复播放', 'all'), ('继续看', 'all'), ('接着看', 'all')])
def test_whole_continuation_rules(phrase, kind):
    assert common_request(phrase) == {'continue_kind': kind}


@pytest.mark.parametrize('phrase', ['不要继续看电影', '继续看电视剧然后关机', '继续看电视吗', '如果继续看电影'])
def test_continuation_never_executes_a_partial_request(phrase):
    assert common_request(phrase) is None


@pytest.mark.parametrize('phrase,item', [('继续看电影', {'type': 'movie'}),
    ('继续看电视剧', {'type': 'episode'}), ('继续看电视', {'type': 'channel', 'channeltype': 'tv'})])
def test_matching_current_player_resumes_without_reopening(history, phrase, item):
    state, calls, tools = history
    state.update(players=[{'playerid': 1, 'type': 'video'}], item=item)
    result = Commands(tools).process(phrase)
    assert result['model_invocations'] == 0
    assert calls[-1] == ('Player.PlayPause', {'playerid': 1, 'play': True})
    assert not any(name.startswith(('VideoLibrary', 'PVR')) or name == 'Player.Open' for name, _ in calls)


@pytest.mark.parametrize('active,item', [('audio', {'type': 'song'}), ('video', {'type': 'episode'}),
    ('video', {'type': 'channel', 'channeltype': 'radio'}), ('video', {'type': 'unknown'})])
def test_wrong_category_is_not_resumed(history, active, item):
    state, calls, tools = history
    state.update(players=[{'playerid': 1, 'type': active}], item=item)
    result = Commands(tools).process('继续看电影')
    assert result['model_invocations'] == 0
    assert calls[-1] == ('Player.Open', {'item': {'movieid': 1}, 'options': {'resume': True}})
    assert not any(name == 'Player.PlayPause' for name, _ in calls)


def test_recent_movie_resumes_actual_offset_even_if_already_watched_before(history):
    state, calls, tools = history
    state['movies'][0]['playcount'] = 2
    Commands(tools).process('继续看电影')
    assert calls[-1][1] == {'item': {'movieid': 1}, 'options': {'resume': True}}
    query = next(params for method, params in calls if method == 'VideoLibrary.GetMovies')
    assert query['sort'] == {'method': 'lastplayed', 'order': 'descending'}


@pytest.mark.parametrize('phrase', ['继续播放', '继续', '恢复播放', '继续看', '接着看'])
def test_untyped_resume_without_a_player_compares_both_media_categories(history, phrase):
    state, calls, tools = history
    result = Commands(tools).process(phrase)
    assert result['model_invocations'] == 0 and result['tools'][-1]['ok']
    assert calls[-1][1] == {'item': {'movieid': 1}, 'options': {'resume': True}}
    assert {name for name, _ in calls} >= {'VideoLibrary.GetMovies', 'VideoLibrary.GetTVShows'}
    state['tvshows'][0]['lastplayed'] = '2026-10-18 21:00:00'
    result = Commands(tools).process(phrase)
    assert result['tools'][-1]['ok']
    assert calls[-1][1] == {'item': {'episodeid': 30}, 'options': {'resume': True}}


@pytest.mark.parametrize('player_type', ['video', 'audio'])
def test_untyped_resume_uses_an_active_player_before_any_history(history, player_type):
    state, calls, tools = history
    state['players'] = [{'playerid': 1 if player_type == 'video' else 0, 'type': player_type}]
    result = Commands(tools).process('继续播放')
    assert result['tools'][-1]['ok'] and result['model_invocations'] == 0
    assert calls[-1] == ('Player.PlayPause', {'playerid': state['players'][0]['playerid'], 'play': True})
    assert not any(method.startswith('VideoLibrary') for method, _ in calls)


def test_finished_movie_requires_explicit_restart(history):
    state, calls, tools = history
    state['movies'][0].update(playcount=1, resume={'position': 0, 'total': 0})
    result = Commands(tools).process('继续看电影')
    parsed = parse(result['reply'], tools)
    assert '已看完' in parsed['message'] and not tools.acted
    Commands(tools).process('选择', parsed['choices'][0])
    assert calls[-1][1] == {'item': {'movieid': 1}, 'options': {'resume': True}}


def test_missing_offset_is_explained_and_write_errors_are_not_retried(history):
    state, calls, tools = history
    state['movies'][0]['resume'] = {'position': 0, 'total': 0}
    result = Commands(tools).process('继续看电影')
    assert '从头播放' in parse(result['reply'], tools)['message']
    assert calls[-1][1]['options']['resume'] is True
    old_rpc = tools.rpc
    def fail_play(name, params):
        if name == 'Player.Open': raise ValueError('播放请求失败')
        return old_rpc(name, params)
    tools.rpc = fail_play
    result = Commands(tools).process('继续看电影')
    assert not result['tools'][-1]['ok'] and '播放请求失败' in result['reply']


def test_tied_movies_preserve_resume_after_verified_choice(history):
    state, calls, tools = history
    state['movies'].append(dict(state['movies'][0], movieid=2, title='电影乙'))
    result = Commands(tools).process('继续看电影')
    assert not tools.acted
    # JSON roundtrip models persisted media between successive conversation turns.
    remembered = json.loads(json.dumps(list(tools.media.values())))
    next_tools = KodiTools(tools.rpc, media=remembered)
    selected = parse(result['reply'], tools)['choices'][1]
    selected['_command_resume'] = False  # Incoming selection cannot change the trusted intent.
    Commands(next_tools).process('选择', selected)
    assert calls[-1][1] == {'item': {'movieid': 2}, 'options': {'resume': True}}


def test_tvshow_uses_existing_complete_progress_then_resumes_or_advances(history):
    state, calls, tools = history
    Commands(tools).process('继续看电视剧')
    assert calls[-1][1] == {'item': {'episodeid': 30}, 'options': {'resume': True}}
    state['episodes'][0].update(playcount=1, resume={'position': 0, 'total': 0})
    Commands(tools).process('继续看电视剧')
    assert calls[-1][1] == {'item': {'episodeid': 31}, 'options': {'resume': True}}
    state['episodes'][1].update(playcount=1, lastplayed='2026-10-18 20:00:00')
    count = sum(method == 'Player.Open' for method, _ in calls)
    result = Commands(tools).process('继续看电视剧')
    assert '没有后续未看剧集' in result['reply']
    assert sum(method == 'Player.Open' for method, _ in calls) == count


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('title', ['第2季《电视剧甲》', '《电视剧甲》第2季'])
def test_explicit_season_scopes_local_history_and_never_plays_another_season(history, restart, title):
    state, calls, tools = history
    first = dict(state['episodes'][0], episodeid=40, title='第二季第一集', season=2, episode=1,
                 playcount=0, resume={'position': 0, 'total': 2000}, lastplayed='')
    second = dict(first, episodeid=41, title='第二季第二集', episode=2,
                  resume={'position': 180, 'total': 2000}, lastplayed='2026-10-18 18:00:00')
    state['episodes'].extend([first, second])
    phrase = ('从头播放' if restart else '播放') + title
    result = Commands(tools).process(phrase)
    assert result['model_invocations'] == 0
    assert calls[-1] == ('Player.Open', {'item': {'episodeid': 40 if restart else 41}, 'options': {'resume': not restart}})


def test_missing_season_does_not_fall_back_to_whole_show_or_repeat_show_choices(history):
    state, calls, tools = history
    result = Commands(tools).process('播放第二季《电视剧甲》')
    parsed = parse(result['reply'], tools)
    assert '没有找到第2季' in parsed['message'] and not parsed['choices']
    assert not any(name == 'Player.Open' for name, _ in calls)


def test_season_without_history_starts_its_first_episode_and_survives_title_choice(history):
    state, calls, tools = history
    for row in state['episodes']:row.update(season=2, playcount=0, lastplayed='', resume={'position': 0, 'total': 2000})
    state['tvshows'].append(dict(state['tvshows'][0], tvshowid=4))
    result = Commands(tools).process('播放第二季《电视剧甲》')
    choices = parse(result['reply'], tools)['choices']
    assert len(choices) == 2 and not any(name == 'Player.Open' for name, _ in calls)
    remembered = json.loads(json.dumps(list(tools.media.values())))
    restored = KodiTools(tools.rpc, media=remembered, defer_ui=True)
    selection = dict(choices[0], _command_season=1)
    Commands(restored).process('选择', selection=selection)
    assert calls[-1][1]['item'] == {'episodeid': 30}


def test_tied_shows_can_be_chosen_without_agent(history):
    state, calls, tools = history
    state['tvshows'].append(dict(state['tvshows'][0], tvshowid=4, title='电视剧乙'))
    result = Commands(tools).process('继续看电视剧')
    row = parse(result['reply'], tools)['choices'][0]
    assert row['kind'] == 'tvshow' and row['_command_resume']
    result = Commands(tools).process('选择', row)
    assert result['model_invocations'] == 0
    assert calls[-1][1] == {'item': {'episodeid': 30}, 'options': {'resume': True}}


def test_ambiguous_episode_history_does_not_play_automatically(history):
    state, calls, tools = history
    state['episodes'][1].update(lastplayed=state['episodes'][0]['lastplayed'], resume={'position': 50, 'total': 2000})
    result = Commands(tools).process('继续看电视剧')
    assert not tools.acted and len(parse(result['reply'], tools)['choices']) == 2


def test_live_tv_uses_kodis_full_timestamp_order_and_rechecks_channel(history):
    state, calls, tools = history
    result = Commands(tools).process('继续看电视')
    assert result['model_invocations'] == 0
    assert calls[-1] == ('Player.Open', {'item': {'channelid': 8}})
    assert tools.ui_action == 'ActivateWindow(FullscreenVideo)'
    assert any(method == 'PVR.GetChannelDetails' for method, _ in calls)
    tools.acted = False
    state['channels'][0]['locked'] = True
    result = Commands(tools).process('继续看电视')
    assert not tools.acted and not result['tools'][-1]['ok']


@pytest.mark.parametrize('phrase,key', [('继续看电影', 'movies'), ('继续看电视剧', 'tvshows'), ('继续看电视', 'channels')])
def test_no_history_is_local_clear_and_has_no_write(history, phrase, key):
    state, calls, tools = history
    for row in state[key]: row['lastplayed'] = '1970-01-01' if key == 'channels' else ''
    result = Commands(tools).process(phrase)
    assert '没有找到' in result['reply'] and result['model_invocations'] == 0 and not tools.acted
    assert not any(method == 'Player.Open' for method, _ in calls)


def test_unavailable_pvr_and_query_error_do_not_fall_through_to_model(history):
    state, calls, tools = history
    state['available'] = False
    assert '尚未就绪' in Commands(tools).process('继续看电视')['reply']
    def broken(*args): raise ValueError('查询失败')
    tools.rpc = broken
    assert '查询失败' in Commands(tools).process('继续看电影')['reply']


def test_cancel_after_history_read_prevents_play(history):
    _, calls, tools = history
    cancelled = False
    old_rpc = tools.rpc
    def cancel_on_read(name, params):
        nonlocal cancelled
        value = old_rpc(name, params)
        if name == 'VideoLibrary.GetMovies': cancelled = True
        return value
    tools.rpc = cancel_on_read
    with pytest.raises(InterruptedError):
        Commands(tools).process('继续看电影', cancelled=lambda: cancelled)
    assert not any(method == 'Player.Open' for method, _ in calls)
