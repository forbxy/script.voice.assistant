import threading

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.commands import Commands, movie_request
from resources.lib.intelligence.permissions import Permissions
from resources.lib.intelligence.presentation import parse
from resources.lib.intelligence.sessions import Sessions
from resources.lib.intelligence.command_request import media_request


@pytest.mark.parametrize('phrase,season,resume', [
    ('播放第二季百变大咖秀', 2, True), ('播放第2季《百变大咖秀》', 2, True),
    ('请帮我继续看第十二季百变大咖秀', 12, True), ('播放电视剧第２季百变大咖秀', 2, True),
    ('从头播放第二季百变大咖秀', 2, False), ('播放第二季《百变大咖秀》，从头开始', 2, False),
    ('播放第零季百变大咖秀', 0, True),
    ('播放百变大咖秀第二季', 2, True), ('播放《百变大咖秀》第2季', 2, True),
    ('请帮我继续看百变大咖秀第十二季', 12, True), ('播放电视剧百变大咖秀第２季', 2, True),
    ('从头播放百变大咖秀第二季', 2, False), ('播放《百变大咖秀》第二季，从头开始', 2, False),
    ('播放百变大咖秀第零季', 0, True)])
def test_explicit_season_is_not_part_of_the_search_title(phrase, season, resume):
    result = media_request(phrase)
    assert result['title'] == result['literal_title'] == '百变大咖秀'
    assert result['kind'] == 'tvshow' and result['season'] == season and result['resume'] is resume


@pytest.mark.parametrize('phrase', ['播放《第2季百变大咖秀》', '播放《庆余年第二季》'])
def test_season_inside_a_quoted_title_remains_literal(phrase):
    result = media_request(phrase)
    assert result['title'] == phrase[3:-1] and 'season' not in result


@pytest.mark.parametrize('phrase', ['播放《百变大咖秀》（2012）第二季',
                                   '播放《百变大咖秀》第二季（2012）'])
def test_suffix_season_preserves_title_year(phrase):
    result = media_request(phrase)
    assert result['title'] == '百变大咖秀' and result['year'] == 2012 and result['season'] == 2


def test_invalid_season_or_non_series_kind_never_becomes_a_play_request():
    assert media_request('播放《第2季百变大咖秀》')['title'] == '第2季百变大咖秀'
    assert media_request('播放第1001季百变大咖秀') is None
    assert media_request('播放第2季电影黑客帝国') is None
    assert media_request('播放百变大咖秀第1001季') is None
    assert media_request('播放电影黑客帝国第二季') is None


@pytest.fixture
def library():
    movies = [{'movieid': 162, 'title': '黑客帝国', 'year': 1999},
              {'movieid': 163, 'title': '黑客帝国2', 'year': 2003},
              {'movieid': 164, 'title': '你好，李焕英', 'year': 2021}]
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        if method == 'VideoLibrary.GetMovies':
            rules = params.get('filter', {}).get('and', [params['filter']] if 'filter' in params else [])
            rows = [row for row in movies if all(str(row.get(rule['field'], '')) == rule['value'] for rule in rules)]
            return {'movies': rows[:12], 'limits': {'total': len(rows)}}
        if method == 'VideoLibrary.GetTVShows': return {'tvshows': [], 'limits': {'total': 0}}
        if method == 'PVR.GetProperties': return {'available': False}
        if method == 'VideoLibrary.GetMovieDetails':
            return {'moviedetails': next(row for row in movies if row['movieid'] == params['movieid'])}
        if method == 'Player.Open': return 'OK'
        raise AssertionError(method)
    return movies, calls, KodiTools(rpc)


@pytest.mark.parametrize('text,title,year,resume', [
    ('播放黑客帝国', '黑客帝国', None, True),
    ('我想看黑客帝国。', '黑客帝国', None, True),
    ('请帮我播放电影《黑客帝国》（1999）。', '黑客帝国', 1999, True),
    ('继续播放黑客帝国', '黑客帝国', None, True),
    ('接着看《黑客帝国》', '黑客帝国', None, True),
    ('播放《你好，李焕英》', '你好，李焕英', None, True),
    ('从头播放黑客帝国', '黑客帝国', None, False),
    ('请帮我从头看《黑客帝国》', '黑客帝国', None, False),
    ('我想从开头开始观看黑客帝国', '黑客帝国', None, False),
    ('从最开始播放黑客帝国', '黑客帝国', None, False),
    ('重新播放黑客帝国', '黑客帝国', None, False),
    ('我要重新看一遍《黑客帝国》', '黑客帝国', None, False),
    ('重播黑客帝国', '黑客帝国', None, False),
    ('再看一遍黑客帝国', '黑客帝国', None, False),
    ('再播放一遍黑客帝国', '黑客帝国', None, False),
    ('重看黑客帝国', '黑客帝国', None, False),
    ('重放一次黑客帝国', '黑客帝国', None, False),
    ('再看一次黑客帝国', '黑客帝国', None, False),
    ('重新开始播放黑客帝国', '黑客帝国', None, False),
    ('重新从头播放黑客帝国', '黑客帝国', None, False),
    ('从头再看一遍黑客帝国', '黑客帝国', None, False),
    ('从一开始播放黑客帝国', '黑客帝国', None, False),
    ('播放《黑客帝国》，从头开始', '黑客帝国', None, False),
    ('播放黑客帝国从头看', '黑客帝国', None, False),
    ('把黑客帝国从头播放', '黑客帝国', None, False),
    ('请帮我把《黑客帝国》再看一遍。', '黑客帝国', None, False),
    ('《黑客帝国》重播一遍', '黑客帝国', None, False),
    ('播放《从头开始》', '从头开始', None, True),
    ('播放《重新看一遍》', '重新看一遍', None, True),
    ('播放不辞而别', '不辞而别', None, True),
    ('从头播放不辞而别', '不辞而别', None, False),
    ('播放不辞而别从头看', '不辞而别', None, False),
])
def test_explicit_movie_utterances_preserve_literal_titles(text, title, year, resume):
    assert movie_request(text) == {'title': title, 'year': year, 'resume': resume}


@pytest.mark.parametrize('text', [
    '推荐几部科幻电影', '不要播放黑客帝国', '先别播放黑客帝国',
    '如果有空再播放黑客帝国', '黑客帝国好看吗', '找一部好看的电影',
    '播放《黑客帝国》，然后关机', '播放《黑客帝国》之前先关字幕',
    '播放《黑客帝国》和《黑客帝国2》', '播放《黑客帝国》（9999）', '我想看一部科幻电影',
])
def test_unclear_negative_and_compound_requests_have_no_side_effects(library, text):
    _, calls, tools = library
    assert Commands(tools).process(text) is None
    assert not calls


def test_unique_exact_movie_plays_without_an_agent(library):
    _, calls, tools = library
    result = Commands(tools).process('我想看黑客帝国')
    assert result['model_invocations'] == 0 and result['agent_backend'] == 'local'
    assert calls[-1] == ('Player.Open', {'item': {'movieid': 162}, 'options': {'resume': True}})
    assert len(calls) == 2
    assert '正在打开' in parse(result['reply'], tools)['message']
    assert '已开始播放' not in result['reply']


def test_resume_is_default_and_cloud_punctuation_preserved(library):
    _, calls, tools = library
    Commands(tools).process('继续播放黑客帝国')
    assert calls[-1][1]['options']['resume'] is True
    Commands(tools).process('播放《你好，李焕英》')
    assert calls[-1][1]['item'] == {'movieid': 164}


def test_duplicates_require_selection_and_year_can_disambiguate(library):
    movies, calls, tools = library
    movies.append({'movieid': 200, 'title': '黑客帝国', 'year': 2020})
    result = Commands(tools).process('播放黑客帝国')
    assert [row['id'] for row in parse(result['reply'], tools)['choices']] == [162, 200]
    assert not tools.acted and not any(name == 'Player.Open' for name, _ in calls)
    resumed = Commands(tools).process('继续播放黑客帝国')
    Commands(tools).process('选择', parse(resumed['reply'], tools)['choices'][0])
    assert calls[-1][1]['options']['resume'] is True
    Commands(tools).process('播放《黑客帝国》（1999）')
    assert calls[-1][1]['item'] == {'movieid': 162}


def test_partial_movie_names_require_selection_and_compounds_never_autoplay(library):
    _, calls, tools = library
    result = Commands(tools).process('播放黑客')
    assert parse(result['reply'], tools)['choices']
    assert Commands(tools).process('播放黑客帝国然后关机') is None
    assert not any(name == 'Player.Open' for name, _ in calls)


def test_selected_movie_is_checked_and_played_by_real_id(library):
    _, calls, tools = library
    tools._remember([{'movieid': 162, 'title': '黑客帝国'}], 'movie')
    result = Commands(tools).process('播放选择', {'kind': 'movie', 'id': 162, 'title': '黑客帝国'})
    assert result['model_invocations'] == 0
    assert [name for name, _ in calls] == ['VideoLibrary.GetMovieDetails', 'Player.Open']
    assert calls[-1][1]['options']['resume'] is True


@pytest.mark.parametrize('phrase', ['从头播放黑客帝国', '重新播放黑客帝国', '再看一遍黑客帝国',
                                   '播放《黑客帝国》，从头开始', '把黑客帝国从头播放'])
def test_restart_intent_survives_version_choice_and_cannot_be_overridden(library, phrase):
    movies, calls, tools = library
    movies.append({'movieid': 200, 'title': '黑客帝国', 'year': 2020})
    result = Commands(tools).process(phrase)
    assert not any(name == 'Player.Open' for name, _ in calls)
    selected = parse(result['reply'], tools)['choices'][0]
    selected['_command_resume'] = True
    Commands(tools).process('选择', selected)
    assert calls[-1][1] == {'item': {'movieid': 162}, 'options': {'resume': False}}


def test_stale_selection_or_changed_title_never_opens(library):
    movies, calls, tools = library
    tools._remember([{'movieid': 162, 'title': '黑客帝国'}], 'movie')
    movies[0]['title'] = '另一个影片'
    result = Commands(tools).process('播放选择', {'kind': 'movie', 'id': 162, 'title': '黑客帝国'})
    assert '已变化' in parse(result['reply'], tools)['message']
    assert not any(name == 'Player.Open' for name, _ in calls)
    result = Commands(tools).process('播放选择', {'kind': 'movie', 'id': 999, 'title': '不存在'})
    assert '不在当前查询结果' in parse(result['reply'], tools)['message']


def test_cancel_between_search_and_play_never_writes(library):
    _, calls, tools = library
    event = threading.Event()
    rpc = tools.rpc
    def cancel_after_query(*args):
        value = rpc(*args)
        event.set()
        return value
    tools.rpc = cancel_after_query
    with pytest.raises(InterruptedError):
        Commands(tools).process('播放黑客帝国', cancelled=event.is_set)
    assert not any(name == 'Player.Open' for name, _ in calls)


def test_read_failure_can_fall_back_but_play_failure_cannot_repeat(library):
    _, calls, tools = library
    rpc = tools.rpc
    def fail_read(*args): raise ValueError('媒体库不可用')
    tools.rpc = fail_read
    assert Commands(tools).process('播放黑客帝国') is None
    def fail_play(name, params):
        if name == 'Player.Open': raise ValueError('播放器未接受请求')
        return rpc(name, params)
    tools.rpc = fail_play
    result = Commands(tools).process('播放黑客帝国')
    assert result is not None and result['tools'][-1]['ok'] is False
    assert '播放器未接受请求' in parse(result['reply'], tools)['message']


def test_permission_check_still_applies_to_local_play(library):
    _, calls, tools = library
    class Deny(Permissions):
        def allows(self, name): return False
    tools.permissions = Deny()
    result = Commands(tools).process('播放黑客帝国')
    assert not result['tools'][-1]['ok']
    assert not any(name == 'Player.Open' for name, _ in calls)


def test_local_execution_is_remembered_without_creating_native_thread(tmp_path):
    session = Sessions(tmp_path, 'forever', signature='same')
    session.record_local('播放黑客帝国', '正在打开《黑客帝国》。')
    loaded = Sessions(tmp_path, 'forever', signature='same')
    assert loaded.thread_id is None and loaded.last_local_action['request'] == '播放黑客帝国'
    assert not (tmp_path / 'agents').exists()


def test_offline_commands_do_not_even_import_native_agents():
    import subprocess
    import sys
    code = '''import sys
from resources.lib.intelligence.runtime import create_tools
from resources.lib.intelligence.commands import Commands
calls=[]
tools=create_tools(None,{'agent_backend':'off','permission_level':'media','approval_strategy':'request'},
                   lambda method,params:calls.append(method) or params.get('volume',0))
result=Commands(tools).process('音量调到三十')
assert result['model_invocations']==0 and calls==['Application.SetVolume']
assert all(name not in sys.modules for name in ('resources.lib.codex_agent','resources.lib.codebuddy_agent',
    'resources.lib.intelligence.resident','resources.lib.codebuddy_resident','http.client'))
'''
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)
