import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.commands import Commands
from resources.lib.intelligence.command_rules import common_request, number
from resources.lib.intelligence.presentation import parse
from resources.lib.intelligence.permissions import Permissions


@pytest.mark.parametrize('text,expected', [
    ('三十', 30), ('百分之三十', None), ('０', 0), ('一百', 100), ('一百零二', 102),
    ('九十五', 95), ('一千零二十', 1020), ('两百五', None), ('一二十', None), ('十百', None),
])
def test_clear_numbers_only(text, expected):
    assert number(text) == expected


@pytest.mark.parametrize('text,tool,arguments', [
    ('暂停播放', 'control_playback', {'action': 'pause'}),
    ('停止播放', 'control_playback', {'action': 'stop'}),
    ('下一项', 'control_playback', {'action': 'next'}),
    ('静音', 'set_mute', {'muted': True}),
    ('取消静音', 'set_mute', {'muted': False}),
    ('打开字幕', 'set_playback_track', {'track': 'subtitle', 'enabled': True}),
    ('关闭字幕', 'set_playback_track', {'track': 'subtitle', 'enabled': False}),
    ('请把音量调到百分之三十。', 'set_volume', {'volume': 30}),
    ('音量设置为0%', 'set_volume', {'volume': 0}),
    ('快进30秒', 'seek_playback', {'seconds': 30}),
    ('快进一分半', 'seek_playback', {'seconds': 90}),
    ('后退一分钟三十秒', 'seek_playback', {'seconds': -90}),
    ('后退半分钟', 'seek_playback', {'seconds': -30}),
    ('打开电影库', 'navigate', {'destination': 'movies'}),
    ('打开电视剧库', 'navigate', {'destination': 'tvshows'}),
    ('我要看电视', 'navigate', {'destination': 'live_tv'}),
    ('打开收藏夹', 'navigate', {'destination': 'favourites'}),
    ('返回主页', 'navigate', {'destination': 'home'}),
])
def test_whole_command_rules(text, tool, arguments):
    request = common_request(text)
    assert request['tool'] == tool and request['arguments'] == arguments


@pytest.mark.parametrize('text', [
    '不要静音', '取消静音之后暂停', '能不能暂停播放', '如果声音太大就静音',
    '音量调到101', '音量调到-1', '音量调到三十再播放', '快进两小时',
    '快进一秒然后关机', '快进一点', '字幕打开了吗', '重启Kodi', '音量提高十格',
])
def test_no_partial_execution_of_questions_negative_or_multiple_requests(text):
    assert common_request(text) is None


@pytest.fixture
def controls():
    calls = []
    state = {'volume': 60, 'muted': False, 'canseek': True}
    addons = [{'addonid': 'plugin.hongguo.ce', 'name': '红果短剧 · CE',
               'enabled': True, 'type': 'xbmc.python.pluginsource'}]
    channels = [{'channelid': 210, 'channel': 'CCTV-1 综合', 'channelnumber': 1, 'channeltype': 'tv'},
                {'channelid': 211, 'channel': 'CCTV-10 科教', 'channelnumber': 10, 'channeltype': 'tv'}]
    def rpc(method, params):
        calls.append((method, params))
        if method == 'Application.GetProperties': return dict(state)
        if method == 'Application.SetVolume': state['volume'] = params['volume']; return state['volume']
        if method == 'Application.SetMute': state['muted'] = params['mute']; return state['muted']
        if method == 'Player.GetActivePlayers': return [{'playerid': 1, 'type': 'video'}]
        if method == 'Player.GetProperties':
            return {'time': {'hours': 0, 'minutes': 10, 'seconds': 0},
                    'canseek': state['canseek'],
                    'totaltime': {'hours': 1, 'minutes': 0, 'seconds': 0}}
        if method == 'Addons.GetAddons': return {'addons': addons}
        if method == 'Addons.GetAddonDetails':
            return {'addon': next(row for row in addons if row['addonid'] == params['addonid'])}
        if method == 'PVR.GetProperties': return {'available': True}
        if method == 'PVR.GetChannels': return {'channels': channels, 'limits': {'total': len(channels)}}
        if method == 'PVR.GetChannelDetails':
            return {'channeldetails': next(row for row in channels if row['channelid'] == params['channelid'])}
        if method in ('Player.PlayPause', 'Player.Stop', 'Player.GoTo', 'Player.Seek', 'Player.SetSubtitle', 'Player.Open'):
            return 'OK'
        raise AssertionError(method)
    return state, addons, calls, KodiTools(rpc, defer_ui=True)


@pytest.mark.parametrize('text,method,params', [
    ('暂停', 'Player.PlayPause', {'playerid': 1, 'play': False}),
    ('继续播放', 'Player.PlayPause', {'playerid': 1, 'play': True}),
    ('停止播放', 'Player.Stop', {'playerid': 1}),
    ('静音', 'Application.SetMute', {'mute': True}),
    ('音量调到三十', 'Application.SetVolume', {'volume': 30}),
    ('关闭字幕', 'Player.SetSubtitle', {'playerid': 1, 'subtitle': 'off', 'enable': False}),
])
def test_controls_execute_real_tools_without_model(controls, text, method, params):
    _, _, calls, tools = controls
    result = Commands(tools).process(text)
    assert result['model_invocations'] == 0
    assert (method, params) in calls and result['tools'][-1]['ok']
    if text in ('暂停', '继续播放', '停止播放'):
        assert tools.playback_started == (text == '继续播放')


def test_relative_volume_clamps_and_seek_uses_seconds(controls):
    state, _, calls, tools = controls
    Commands(tools).process('音量提高五十')
    assert state['volume'] == 100
    Commands(tools).process('音量降低一百')
    assert state['volume'] == 0
    Commands(tools).process('声音大一点')
    assert state['volume'] == 5
    Commands(tools).process('快进一分半')
    assert calls[-1] == ('Player.Seek', {'playerid': 1, 'value': {'time': {
        'hours': 0, 'minutes': 11, 'seconds': 30, 'milliseconds': 0}}})


@pytest.mark.parametrize('text,seconds', [
    ('跳转到十分钟', 600), ('跳到一分三十秒', 90), ('跳至01:30', 90),
    ('跳转至01:30:00', 5400), ('快进到半小时', 1800), ('快退到半分钟', 30),
    ('后退到第十分钟', 600), ('把播放进度调到一个半小时', 5400),
    ('把进度拖到一小时半', 5400), ('请帮我跳转到零秒。', 0),
    ('跳转到第1小时2分3秒处', 3723), ('跳到1个小时', 3600),
    ('播放进度跳到一小时零二分零三秒', 3723), ('视频跳到三十分钟的位置', 1800),
    ('将播放位置调到30秒吧', 30), ('跳转到半个小时', 1800),
    ('跳转到一分半', 90), ('跳到九十分钟', 5400), ('跳到九十秒', 90),
    ('跳到 ０１：３０', 90), ('跳转到0:00:00', 0), ('跳到90:00', 5400),
    ('跳转到23:59:59', 86399), ('跳到二十三小时五十九分五十九秒', 86399),
])
def test_absolute_seek_requires_an_explicit_elapsed_time(text, seconds):
    request = common_request(text)
    assert request['tool'] == 'seek_playback'
    assert request['arguments'] == {'seconds': seconds, 'absolute': True}


@pytest.mark.parametrize('text', [
    '定位到十分钟', '定位十分钟', '把进度定位到十分钟', '定位到北京',
    '不要跳转到十分钟', '跳转到十分钟了吗', '能不能跳到十分钟',
    '跳到十分钟然后暂停', '跳到十分钟，再关机', '如果太长就跳到十分钟',
    '跳转到30', '跳到明天', '跳转到十点半', '跳到-30秒', '跳到1.5分钟',
    '跳到01:60', '跳到01:60:00', '跳到24:00:00', '跳到1440:00',
    '跳到一分六十秒', '跳到一小时六十分', '跳到两百五分钟', '跳到半',
    '跳到1:2:3:4', '跳到0', '跳转到一万秒',
])
def test_ambiguous_invalid_and_location_requests_are_not_local_seeks(text):
    assert common_request(text) is None


@pytest.mark.parametrize('level', ['media', 'settings', 'full'])
@pytest.mark.parametrize('phrase,seconds', [
    ('跳转到一分三十秒', 90), ('跳到零秒', 0), ('快进到半小时', 1800),
    ('跳到两个小时', 3600),  # Clamp to this fixture's one-hour film.
])
def test_absolute_seek_uses_time_from_start_without_resuming_or_reopening(controls, level, phrase, seconds):
    _, _, calls, tools = controls
    tools.permissions = Permissions(level)
    result = Commands(tools).process(phrase)
    assert result['model_invocations'] == 0 and result['tools'][-1]['ok']
    assert calls[-1] == ('Player.Seek', {'playerid': 1, 'value': {'time': {
        'hours': seconds // 3600, 'minutes': seconds // 60 % 60, 'seconds': seconds % 60, 'milliseconds': 0}}})
    assert not any(method in ('Player.PlayPause', 'Player.Open') for method, _ in calls)
    assert tools.acted and not tools.playback_started


@pytest.mark.parametrize('phrase', ['跳转到十分钟', '快进三十秒'])
def test_seek_rejects_streams_without_seek_support(controls, phrase):
    state, _, calls, tools = controls
    state['canseek'] = False
    result = Commands(tools).process(phrase)
    assert not result['tools'][-1]['ok'] and '不支持跳转' in result['reply']
    assert not any(method == 'Player.Seek' for method, _ in calls)


def test_absolute_seek_handles_no_active_player_and_unknown_duration(controls):
    _, _, calls, tools = controls
    original = tools.rpc
    tools.rpc = lambda *args: []
    result = Commands(tools).process('跳转到十分钟')
    assert not result['tools'][-1]['ok'] and '没有正在播放' in result['reply']
    assert not calls
    def unknown(method, params):
        result = original(method, params)
        if method == 'Player.GetProperties':
            result['totaltime'] = dict(hours=0, minutes=0, seconds=0)
        return result
    tools.rpc = unknown
    result = tools.execute('seek_playback', {'seconds': 5400, 'absolute': True})
    assert result['ok'] and result['position_seconds'] == 5400
    assert calls[-1][1]['value']['time'] == dict(hours=1, minutes=30, seconds=0, milliseconds=0)


@pytest.mark.parametrize('arguments', [
    {'seconds': -1, 'absolute': True}, {'seconds': 86400, 'absolute': True},
    {'seconds': 3601}, {'seconds': 5400, 'absolute': False},
    {'seconds': True, 'absolute': True}, {'seconds': 10, 'absolute': 'true'},
])
def test_ai_seek_arguments_cannot_bypass_absolute_or_relative_bounds(controls, arguments):
    _, _, calls, tools = controls
    assert not tools.execute('seek_playback', arguments)['ok']
    assert not calls


@pytest.mark.parametrize('phrase', ['从头播放', '从头看', '从头开始', '重新播放', '重新开始',
                                   '重新看', '重播', '重看', '重放', '再看一遍', '再播放一次'])
def test_explicit_current_video_restart_seeks_to_zero_then_unpauses(controls, phrase):
    _, _, calls, tools = controls
    result = Commands(tools).process(phrase)
    assert result['model_invocations'] == 0 and result['tools'][-1]['ok']
    assert calls[-2:] == [('Player.Seek', {'playerid': 1, 'value': {'percentage': 0.0}}),
                         ('Player.PlayPause', {'playerid': 1, 'play': True})]
    assert tools.playback_started


def test_live_video_without_seeking_is_not_restarted(controls):
    state, _, calls, tools = controls
    state['canseek'] = False
    result = Commands(tools).process('重新播放')
    assert not result['tools'][-1]['ok'] and '不支持' in result['reply']
    assert not any(method in ('Player.Open', 'Player.Seek', 'Player.PlayPause') for method, _ in calls)


def test_navigation_and_addons_wait_for_assistant_handoff(controls):
    _, _, _, tools = controls
    Commands(tools).process('打开电影库')
    assert tools.ui_action == 'ActivateWindow(Videos,movietitles,return)'
    result = Commands(tools).process('打开红果短剧')
    assert tools.ui_action == 'RunAddon(plugin.hongguo.ce)'
    assert result['tools'][-1]['name'] == 'open_addon'
    assert Commands(tools).process('打开红果') is None  # No fuzzy launch.
    assert Commands(tools).process('打开红果短剧插件')['tools'][-1]['ok']
    assert Commands(tools).process('打开插件《红果短剧插件》') is None


def test_duplicate_and_disabled_addons_are_not_implicitly_opened(controls):
    _, addons, _, tools = controls
    addons.append(dict(addons[0], addonid='plugin.hongguo.other'))
    result = Commands(tools).process('打开红果短剧')
    assert len(parse(result['reply'], tools)['choices']) == 2 and tools.ui_action is None
    addons.pop()
    addons[0]['enabled'] = False
    result = Commands(tools).process('打开红果短剧')
    assert not result['tools'][-1]['ok'] and tools.ui_action is None


def test_local_launch_excludes_same_named_background_components(controls):
    _, addons, calls, tools = controls
    addons.append({'addonid': 'script.module.hongguo', 'name': '红果短剧',
                   'type': 'xbmc.python.module', 'enabled': True})
    result = Commands(tools).process('打开红果短剧')
    assert result['model_invocations'] == 0 and result['tools'][-1]['ok']
    assert tools.ui_action == 'RunAddon(plugin.hongguo.ce)'
    assert next(params for method, params in calls if method == 'Addons.GetAddons')['properties'] == ['name', 'enabled']


def test_cctv_one_cannot_accidentally_open_cctv_ten(controls):
    _, _, calls, tools = controls
    result = Commands(tools).process('播放CCTV一')
    assert result['model_invocations'] == 0
    assert ('Player.Open', {'item': {'channelid': 210}}) in calls
    assert tools.ui_action == 'ActivateWindow(FullscreenVideo)'


@pytest.mark.parametrize('phrase,addonid,name', [
    ('打开筛选', 'plugin.video.filteredmovies', 'Filtered Movies'),
    ('打开筛选页', 'plugin.video.filteredmovies', 'Filtered Movies'),
    ('打开筛选插件', 'plugin.video.filteredmovies', 'Filtered Movies'),
    ('请帮我打开筛选页。', 'plugin.video.filteredmovies', 'Filtered Movies'),
    ('启动遥控器插件', 'script.controller.switcher', 'Controller Switcher'),
    ('打开遥控器插件', 'script.controller.switcher', 'Controller Switcher'),
    ('运行插件遥控器', 'script.controller.switcher', 'Controller Switcher'),
])
def test_addon_aliases_use_verified_ids_without_model_or_name_guessing(controls, phrase, addonid, name):
    _, addons, calls, tools = controls
    addons.append({'addonid': addonid, 'name': name, 'enabled': True, 'type': 'xbmc.python.script'})
    # A different addon with the spoken Chinese name must not shadow this alias.
    addons.append({'addonid': 'script.other', 'name': '筛选', 'enabled': True, 'type': 'xbmc.python.script'})
    result = Commands(tools).process(phrase)
    assert result['model_invocations'] == 0 and result['tools'][-1]['ok']
    assert tools.ui_action == 'RunAddon(' + addonid + ')'
    assert [method for method, _ in calls] == ['Addons.GetAddonDetails']


def test_disabled_or_missing_alias_target_does_not_launch_another_addon(controls):
    _, addons, calls, tools = controls
    addons.append({'addonid': 'plugin.video.filteredmovies', 'name': 'Filtered Movies',
                   'enabled': False, 'type': 'xbmc.python.script'})
    result = Commands(tools).process('打开筛选')
    assert not result['tools'][-1]['ok'] and tools.ui_action is None
    def missing(method, params):
        raise ValueError('插件不存在')
    tools.rpc = missing
    result = Commands(tools).process('打开遥控器插件')
    assert result is not None and not result['tools'][-1]['ok'] and tools.ui_action is None


@pytest.mark.parametrize('phrase', ['不要打开筛选', '打开筛选，然后关闭声音', '打开筛选吗'])
def test_addon_aliases_still_require_a_whole_explicit_command(controls, phrase):
    _, _, calls, tools = controls
    assert Commands(tools).process(phrase) is None
    assert not tools.acted


def test_quoted_addon_name_keeps_literal_matching():
    request = common_request('打开《筛选》')
    assert request == {'addon': '筛选', 'literal_name': True}
