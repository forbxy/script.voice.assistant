import json

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.live_tv import normalize
from resources.lib.intelligence.permissions import Permissions
from resources.lib.intelligence.presentation import parse


def channel(identifier, title, number, **extra):
    return dict(channelid=identifier, channel=title, label=title, channelnumber=number,
                channeltype='tv', hidden=False, locked=False, **extra)


class Pvr:
    def __init__(self, rows=None):
        self.rows = rows or [channel(210, 'CCTV-1 综合', 1), channel(289, 'CCTV-1高清', 2),
            channel(197, 'CCTV-2 财经', 3), channel(177, 'CCTV-10 科教', 20),
            channel(228, 'CCTV-11 戏曲', 21), channel(200, 'CCTV-13 新闻', 26),
            channel(235, '陕西新闻资讯', 45), channel(191, 'CCTV-5 体育', 9), channel(202, 'CCTV-5+ 体育赛事', 11)]
        self.calls, self.available = [], True

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == 'PVR.GetProperties':return {'available': self.available}
        if method == 'PVR.GetChannels':
            assert params['channelgroupid'] == 'alltv'
            start, end = params['limits']['start'], params['limits']['end']
            return {'channels': self.rows[start:end], 'limits': {'total': len(self.rows)}}
        if method == 'PVR.GetChannelDetails':
            return {'channeldetails': next((r for r in self.rows if r['channelid'] == params['channelid']), {})}
        if method == 'Player.Open':return 'OK'
        raise AssertionError('Unexpected RPC: ' + method)


@pytest.mark.parametrize('query', ['CCTV1', 'CCTV-1', 'CCTV一', '央视一套', '中央一台', 'ＣＣＴＶ－１', 'cctv 1'])
def test_cctv_aliases_recommend_same_real_channel_without_matching_ten_or_eleven(query):
    rpc = Pvr();tools = KodiTools(rpc, permissions=Permissions('media'))
    result = tools.execute('search_channels', {'query': query})
    assert result['ok'] and result['total'] == 2
    assert [row['id'] for row in result['channels']] == [210, 289]
    assert result['recommended']['id'] == 210
    assert all(row['kind'] == 'channel' for row in result['channels'])
    assert all(method.startswith('PVR.') for method, params in rpc.calls)


def test_cctv_numbers_plus_and_ordinary_name_search_do_not_conflate_stations():
    rpc = Pvr();tools = KodiTools(rpc)
    assert normalize('中央十三台') == 'cctv13'
    assert normalize('央视十套') == 'cctv10'
    assert normalize('央视二十一套') == 'cctv21'
    assert tools.execute('search_channels', {'query': 'CCTV十'})['channels'][0]['id'] == 177
    assert tools.execute('search_channels', {'query': 'CCTV五'})['total'] == 1
    assert tools.execute('search_channels', {'query': 'CCTV五加'})['channels'][0]['id'] == 202
    assert tools.execute('search_channels', {'query': 'CCTV5+'})['channels'][0]['id'] == 202
    assert tools.execute('search_channels', {'query': '1'})['recommended']['id'] == 210
    news = tools.execute('search_channels', {'query': '新闻'})
    assert news['total'] == 2 and news['recommended'] is None
    assert tools.execute('search_channels', {'query': '不存在的频道'})['total'] == 0


def test_channel_search_pages_filters_hidden_and_preserves_selectable_ids():
    rpc = Pvr([channel(i, '频道' + str(i), i) for i in range(1, 604)])
    rpc.rows[0]['hidden'] = True
    tools = KodiTools(rpc)
    first = tools.execute('search_channels', {'limit': 2})
    assert first['total'] == 602 and first['next_offset'] == 2
    assert [row['id'] for row in first['channels']] == [2, 3]
    assert len([m for m, p in rpc.calls if m == 'PVR.GetChannels']) == 2
    second = tools.execute('search_channels', {'offset': first['next_offset'], 'limit': 2})
    assert [row['id'] for row in second['channels']] == [4, 5]
    assert second['recommended'] is None


def test_channel_playback_uses_real_id_and_defers_fullscreen_until_modal_handoff():
    rpc = Pvr();builtins = [];tools = KodiTools(rpc, builtins.append, defer_ui=True)
    assert not tools.execute('play_channel', {'channelid': 210})['ok']
    assert not rpc.calls
    result = tools.execute('search_channels', {'query': 'CCTV一'})
    following = KodiTools(rpc, builtins.append, media=tools.media.values(), defer_ui=True)
    played = following.execute('play_channel', {'channelid': result['recommended']['id']})
    assert played['ok'] and played['deferred'] == 'ui_navigation'
    assert following.ui_action == 'ActivateWindow(FullscreenVideo)' and builtins == []
    assert rpc.calls[-1] == ('Player.Open', {'item': {'channelid': 210}})
    assert following.playback_started


@pytest.mark.parametrize('state', ['hidden', 'locked', 'removed', 'radio'])
def test_channel_visibility_and_lock_are_rechecked_before_open(state):
    rpc = Pvr();tools = KodiTools(rpc)
    tools.execute('search_channels', {'query': 'CCTV1'})
    if state in ('hidden', 'locked'):rpc.rows[0][state] = True
    elif state == 'removed':rpc.rows.pop(0)
    else:rpc.rows[0]['channeltype'] = 'radio'
    assert not tools.execute('play_channel', {'channelid': 210})['ok']
    assert not any(method == 'Player.Open' for method, params in rpc.calls)


def test_unavailable_pvr_and_cancelled_queries_cannot_navigate_or_play():
    rpc = Pvr();rpc.available = False;builtins = []
    tools = KodiTools(rpc, builtins.append, defer_ui=True)
    assert not tools.execute('search_channels', {'query': 'CCTV1'})['ok']
    assert not tools.execute('navigate', {'destination': 'live_tv'})['ok']
    assert tools.ui_action is None and not builtins
    with pytest.raises(InterruptedError):tools.execute('search_channels', {}, cancelled=lambda: True)


def test_live_tv_navigation_is_distinct_from_library_tvshows():
    rpc = Pvr();builtins = [];tools = KodiTools(rpc, builtins.append, defer_ui=True)
    assert tools.execute('navigate', {'destination': 'live_tv'})['ok']
    assert tools.ui_action == 'ActivateWindow(TVChannels)' and not builtins
    assert tools.execute('navigate', {'destination': 'tvshows'})['ok']
    assert tools.ui_action == 'ActivateWindow(Videos,tvshowtitles,return)'


def test_channel_choices_are_verified_and_offer_channel_wording():
    tools = KodiTools(Pvr());tools.execute('search_channels', {'query': '新闻'})
    outcome = parse('{"message":"选一个频道。"}', tools)
    assert len(outcome['choices']) == 2
    assert all(row['kind'] == 'channel' and '直播频道' in row['value'] for row in outcome['choices'])
    forged = {'message': '请选择', 'choices': [{'label': '伪造', 'value': '播放', 'kind': 'channel', 'id': 999}]}
    tools.latest_matches = []
    assert not parse(json.dumps(forged), tools)['choices']


def test_new_toolset_rotates_retained_thread_so_live_tools_are_registered(tmp_path, monkeypatch):
    from resources.lib.intelligence import sessions
    signature = sessions.signature({})
    old = sessions.Sessions(tmp_path, 'forever', signature)
    old.started('old-tool-thread')
    assert sessions.Sessions(tmp_path, 'forever', signature).thread_id == 'old-tool-thread'
    monkeypatch.setattr(sessions, 'TOOLSET_REVISION', sessions.TOOLSET_REVISION + 1)
    assert sessions.Sessions(tmp_path, 'forever', sessions.signature({})).thread_id is None
