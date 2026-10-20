import json
from datetime import datetime, timedelta, timezone

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.live_tv import epg
from resources.lib.intelligence.permissions import Permissions
from resources.lib.intelligence.presentation import parse
from test_live_tv import Pvr, channel


def broadcast(title='唐人街探案Ⅱ', **extra):
    now = datetime.now(timezone.utc)
    return dict(title=title, broadcastid=18, isactive=True, genre=['其他/未知'],
        starttime=(now - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S'),
        endtime=(now + timedelta(minutes=60)).strftime('%Y-%m-%d %H:%M:%S'), **extra)


def test_current_epg_includes_all_channels_and_program_evidence_without_classifying():
    now = broadcast(plot='[COLOR button_focus]当前: [/COLOR]唐人街探案Ⅱ',
                    filenameandpath='pvr://private', cast='not needed')
    rpc = Pvr([channel(282, 'CCTV-6高清', 12, broadcastnow=now),
               channel(210, 'CCTV-1 综合', 1, broadcastnow=broadcast('人世间36')),
               channel(249, '陕西-西部电影', 55)])
    tools = KodiTools(rpc, permissions=Permissions('media'), defer_ui=True)
    tools.execute('search_channels', {})
    result = tools.execute('get_epg', {})
    assert result['ok'] and result['total'] == 3
    assert result['with_current_program'] == 2 and result['without_current_program'] == 1
    assert [row['id'] for row in result['channels']] == [210, 282, 249]
    program = result['channels'][1]['current_program']
    assert program['title'] == '唐人街探案Ⅱ' and program['genre'] == ['其他/未知']
    assert program['plot'] == '当前: 唐人街探案Ⅱ'
    assert 'filenameandpath' not in program and 'cast' not in program
    expected = datetime.fromisoformat(now['starttime']).replace(tzinfo=timezone.utc)
    assert datetime.fromisoformat(program['starttime']) == expected
    assert datetime.fromisoformat(result['queried_at']).tzinfo is not None
    assert result['channels'][2]['current_program'] is None
    assert not tools.latest_matches and not tools.acted and tools.ui_action is None
    assert parse('{"message":"当前节目如下。"}', tools)['choices'] == []


def test_epg_returns_verified_channels_for_explicit_choices_and_subsequent_playback():
    rpc = Pvr([channel(282, 'CCTV-6高清', 12, broadcastnow=broadcast())])
    tools = KodiTools(rpc, permissions=Permissions('media'), defer_ui=True)
    result = tools.execute('get_epg', {'query': '央视六套'})
    assert result['ok'] and result['total'] == 1
    choice = {'message': 'CCTV-6 正在播唐人街探案Ⅱ。', 'choices': [
        {'kind': 'channel', 'id': 282, 'label': 'CCTV-6：唐人街探案Ⅱ', 'value': '播放这个频道'}]}
    parsed = parse(json.dumps(choice), tools)
    assert parsed['choices'][0]['id'] == 282
    followup = KodiTools(rpc, media=list(tools.media.values()), defer_ui=True)
    assert followup.execute('play_channel', {'channelid': 282})['ok']
    assert ('Player.Open', {'item': {'channelid': 282}}) in rpc.calls


def test_epg_optional_next_program_and_bounded_plot():
    next_program = dict(broadcast('下一档节目', plot='很长的简介' * 200), isactive=False,
                        starttime=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
    rpc = Pvr([channel(282, 'CCTV-6高清', 12, broadcastnow=broadcast(), broadcastnext=next_program)])
    assert 'next_program' not in epg(rpc)['channels'][0]
    result = epg(rpc, include_next=True)
    assert result['channels'][0]['next_program']['title'] == '下一档节目'
    assert len(result['channels'][0]['next_program']['plot']) == 600


@pytest.mark.parametrize('update', [
    {'isactive': False}, {'title': ''},
    {'starttime': '2999-01-01 00:00:00'}, {'endtime': '2000-01-01 00:00:00'},
])
def test_stale_future_and_empty_current_entries_do_not_claim_to_be_airing(update):
    rpc = Pvr([channel(1, '电影频道', 1, broadcastnow=dict(broadcast(), **update))])
    result = epg(rpc)
    assert result['channels'][0]['current_program'] is None
    assert result['without_current_program'] == 1


def test_epg_reads_all_rpc_pages_then_returns_requested_page_with_global_coverage():
    rows = [channel(i, '频道' + str(i), i, broadcastnow=broadcast()) for i in range(1, 604)]
    rows[1]['hidden'] = True
    rows[2]['channeltype'] = 'radio'
    rpc = Pvr(rows)
    result = epg(rpc, offset=195, limit=200)
    assert result['total'] == 601 and result['with_current_program'] == 601
    assert len(result['channels']) == 200 and result['next_offset'] == 395
    assert len([call for call in rpc.calls if call[0] == 'PVR.GetChannels']) == 2
    assert epg(rpc, query='不存在的频道')['total'] == 0


def test_epg_failure_clears_stale_recommendations_and_is_cancellable():
    rpc = Pvr();tools = KodiTools(rpc)
    tools.execute('search_channels', {})
    rpc.available = False
    assert not tools.execute('get_epg', {})['ok']
    assert not tools.latest_matches
    assert not tools.execute('get_epg', {'limit': 201})['ok']
    with pytest.raises(InterruptedError):
        tools.execute('get_epg', {}, cancelled=lambda: True)
