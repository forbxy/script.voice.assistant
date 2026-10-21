"""Successful player controls dismiss the UI; queries and failed tasks remain."""
from types import SimpleNamespace

import pytest

from resources.lib.ui.task_playback import TaskPlayback, local_control


@pytest.mark.parametrize('phrase,expected', [
    ('暂停', True), ('继续播放', True), ('停止播放', True), ('快进三十秒', True),
    ('声音小一点', True), ('静音', True), ('关闭字幕', True), ('从头播放', True),
    ('跳转到十分钟', True), ('跳到01:30', True), ('定位到十分钟', False),
    ('播放黑客帝国', False), ('继续看电影', True), ('继续看电视剧', True), ('继续看电视', True),
    ('库里有没有阿拉丁', False),
    ('上传日志', False), ('重启系统', False), ('不要暂停', False),
    ('暂停然后解释一下剧情', False), ('能不能暂停播放', False),
])
def test_only_definite_local_controls_take_the_small_window_path(phrase, expected):
    assert local_control(phrase) is expected


def show(trace, choices=(), round_tools=None):
    presentation = dict(message='处理结果', interaction='continue', choices=list(choices))
    outcome = dict(tools=trace)
    if round_tools is not None:
        outcome['round_tools'] = round_tools
    playback = TaskPlayback(None, None, None, None, lambda: 0, (False, ''),
                            lambda: (False, ''), lambda *args: False)
    playback.present(presentation, SimpleNamespace(), outcome)
    return presentation, playback.state


@pytest.mark.parametrize('name,arguments', [
    ('control_playback', {'action': 'pause'}), ('control_playback', {'action': 'stop'}),
    ('control_playback', {'action': 'next'}), ('seek_playback', {'seconds': 30}),
    ('set_volume', {'volume': 30}), ('set_mute', {'muted': True}),
    ('set_playback_track', {'track': 'audio', 'index': 0}),
    ('set_audio_delay', {'milliseconds': 100}), ('adjust_subtitles', {'delay_ms': 100}),
    ('set_video_view', {'mode': 'normal'}), ('set_playlist_mode', {'repeat': 'all'}),
    ('edit_playlist', {'action': 'remove', 'position': 2}),
    ('sleep_timer', {'action': 'set', 'minutes': 30}), ('sleep_timer', {'action': 'cancel'}),
])
def test_control_success_closes_without_requiring_fullscreen_playback(name, arguments):
    shown, state = show([{'name': name, 'arguments': arguments, 'ok': True}])
    assert shown['interaction'] == 'close' and state == 'controlled'


def test_track_query_followed_by_verified_track_change_closes():
    trace = [{'name': 'get_playback_details', 'arguments': {}, 'ok': True},
             {'name': 'set_playback_track', 'arguments': {'track': 'audio', 'index': 1}, 'ok': True}]
    assert show(trace)[0]['interaction'] == 'close'


@pytest.mark.parametrize('trace', [
    [], [{'name': 'get_playback_details', 'ok': True}],
    [{'name': 'sleep_timer', 'arguments': {'action': 'status'}, 'ok': True}],
    [{'name': 'control_playback', 'ok': False}], [{'name': 'control_playback'}],
    [{'name': 'set_volume', 'ok': True}, {'name': 'set_mute', 'ok': False}],
    [{'name': 'get_playback_details', 'ok': False}, {'name': 'set_mute', 'ok': True}],
    [{'name': 'set_volume', 'ok': True}, {'name': 'get_system_status', 'ok': True}],
    [{'name': 'upload_logs', 'ok': True}], [{'name': 'system_action', 'ok': True}],
    [{'name': 'play_media', 'ok': True}], [{'name': 'codex_command', 'ok': True}],
])
def test_query_failure_maintenance_and_unconfirmed_playback_do_not_dismiss_results(trace):
    assert show(trace)[0]['interaction'] == 'continue'


def test_visible_choices_and_followup_question_keep_the_window_open():
    trace = [{'name': 'set_mute', 'ok': True}]
    assert show(trace, choices=[{'label': '选择音轨'}])[0]['interaction'] == 'continue'
    assert show(trace, round_tools=[])[0]['interaction'] == 'continue'
