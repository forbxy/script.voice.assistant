import copy
import json
from pathlib import Path

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.permissions import Permissions
from resources.lib.intelligence.interface_tools import perform_operations


class Kodi:
    def __init__(self):
        self.calls, self.commands = [], []
        self.item = {'id': 10, 'type': 'movie', 'title': '测试电影', 'year': 2020,
            'file': 'https://user:private-password@example.invalid/film.mkv',
            'genre': ['科幻'], 'director': ['测试导演'], 'plot': '公开的剧情。', 'cast': [{'name': '甲', 'role': '乙'}]}
        self.tracks = {'audiostreams': [{'index': 0, 'language': 'eng', 'name': 'English'},
                                      {'index': 1, 'language': 'zho', 'name': '国语'}],
                       'subtitles': [{'index': 2, 'language': 'zho', 'name': '简体中文'}],
                       'currentaudiostream': {'index': 0}, 'currentsubtitle': {'index': 2}, 'subtitleenabled': True}
        self.position, self.delay, self.window = 0, .2, 10025
        self.view = {'zoom': 1.0}
        self.queue = [{'id': 10, 'type': 'movie', 'title': '第一部', 'file': self.item['file']},
                      {'id': 11, 'type': 'movie', 'title': '第二部', 'file': '/private/movie.mkv'}]
        self.favourites = []
        self.alarms = set()
        self.hidden = False
        self.episodes = [{'episodeid': i, 'title': str(i), 'showtitle': '测试剧', 'season': 2, 'episode': i,
                          'lastplayed': '2026-10-17 08:00:00' if i == 2 else '',
                          'resume': {'position': 100 if i == 2 else 0, 'total': 1000}, 'playcount': 0}
                         for i in (3, 1, 2)]
        self.fail_add = False

    def builtin(self, command):
        self.commands.append(command)
        if command.startswith('AlarmClock('):self.alarms.add('voice_assistant_sleep')
        elif command.startswith('CancelAlarm('):self.alarms.discard('voice_assistant_sleep')

    def rpc(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'Player.GetActivePlayers':return [{'playerid': 1, 'type': 'video'}]
        if method == 'Player.GetItem':return {'item': copy.deepcopy(self.item)}
        if method == 'Player.Open' and 'playlistid' in params.get('item', {}):
            self.item = copy.deepcopy(self.queue[params['item']['position']])
            return 'OK'
        if method == 'Player.GetProperties':
            values = dict(self.tracks, speed=0, time={'hours': 0, 'minutes': 0, 'seconds': 10, 'milliseconds': 0},
                totaltime={'hours': 1, 'minutes': 0, 'seconds': 0, 'milliseconds': 0},
                canseek=True, canchangespeed=True, playlistid=1, position=self.position, repeat='off', shuffled=False)
            return {key: copy.deepcopy(values.get(key)) for key in params['properties']}
        if method == 'Player.GetAudioDelay':return {'offset': self.delay}
        if method == 'Player.SetAudioDelay':self.delay = params['offset'];return {'offset': self.delay}
        if method == 'Player.GetViewMode':return dict(self.view)
        if method == 'Player.SetViewMode':self.view = {'mode': params['viewmode']};return 'OK'
        if method.startswith('VideoLibrary.Get') and method.endswith('Details'):
            kind = 'tvshow' if 'TVShow' in method else 'episode' if 'Episode' in method else 'movie'
            return {kind + 'details': dict(self.item, **{kind + 'id': params[kind + 'id']})}
        if method == 'VideoLibrary.GetEpisodes':
            start, end = params['limits']['start'], params['limits']['end']
            return {'episodes': copy.deepcopy(self.episodes[start:end]), 'limits': {'start': start, 'end': min(end, len(self.episodes)), 'total': len(self.episodes)}}
        if method in ('VideoLibrary.GetMovies', 'VideoLibrary.GetTVShows'):
            kind = 'movie' if method.endswith('Movies') else 'tvshow'
            return {kind + 's': [dict(self.item, **{kind + 'id': 10})], 'limits': {'total': 15}}
        if method == 'Playlist.GetItems':return {'items': copy.deepcopy(self.queue), 'limits': {'total': len(self.queue)}}
        if method == 'Playlist.Clear':self.queue.clear()
        if method in ('Playlist.Add', 'Playlist.Insert'):
            if self.fail_add:
                self.fail_add = False
                raise ValueError('Failed to add')
            items = params['item'] if isinstance(params['item'], list) else [params['item']]
            rows = []
            for item in items:
                kind = next((k for k in ('movie', 'episode') if k + 'id' in item), None)
                rows.append({'type': kind or 'unknown', 'id': item.get((kind or '') + 'id', -1),
                             'file': item.get('file', ''), 'title': str(item)})
            if method == 'Playlist.Add':self.queue.extend(rows)
            else:self.queue[params['position']:params['position']] = rows
        if method == 'Playlist.Remove':self.queue.pop(params['position'])
        if method == 'Playlist.Swap':
            a, b = params['position1'], params['position2'];self.queue[a], self.queue[b] = self.queue[b], self.queue[a]
        if method == 'Favourites.GetFavourites':return {'favourites': copy.deepcopy(self.favourites)}
        if method == 'Favourites.AddFavourite':self.favourites.append(copy.deepcopy(params))
        if method == 'GUI.GetProperties':return {'currentwindow': {'id': self.window}}
        if method == 'XBMC.GetInfoBooleans':
            return {key: self.hidden if key == 'System.HasHiddenInput' else bool(self.alarms) for key in params['booleans']}
        return 'OK'


@pytest.fixture
def fixture(tmp_path):
    kodi = Kodi()
    tools = KodiTools(kodi.rpc, kodi.builtin, profile=tmp_path,
        media=[{'kind': 'movie', 'id': 10, 'title': '测试电影'}, {'kind': 'movie', 'id': 11, 'title': '第二部'},
               {'kind': 'tvshow', 'id': 20, 'title': '测试剧'}, {'kind': 'episode', 'id': 2, 'title': '第二集'}],
        ui_context={'window': {'id': 10025}}, permissions=Permissions('full', 'full_access'))
    return tools, kodi


def test_current_playback_has_tracks_metadata_and_optional_cast_without_paths(fixture):
    tools, kodi = fixture
    result = tools.execute('get_playback_details', {})
    assert result['ok'] and result['paused'] and len(result['playback']['audiostreams']) == 2
    assert 'cast' not in result['item']
    assert 'cast' not in next(params['properties'] for name, params in kodi.calls if name == 'Player.GetItem')
    assert 'private-password' not in json.dumps(result) and 'file' not in result['item']
    result = tools.execute('get_playback_details', {'include_cast': True})
    assert result['item']['cast'] == [{'name': '甲', 'role': '乙'}]


def test_track_indices_are_revalidated_and_subtitle_can_be_turned_off(fixture):
    tools, kodi = fixture
    assert not tools.execute('set_playback_track', {'track': 'audio', 'index': 500})['ok']
    assert not any(name == 'Player.SetAudioStream' for name, _ in kodi.calls)
    assert tools.execute('set_playback_track', {'track': 'audio', 'index': 1})['ok']
    assert kodi.calls[-1] == ('Player.SetAudioStream', {'playerid': 1, 'stream': 1})
    assert tools.execute('set_playback_track', {'track': 'subtitle', 'enabled': False})['ok']
    assert kodi.calls[-1][1]['subtitle'] == 'off'
    assert not tools.execute('set_playback_track', {'track': 'audio', 'index': 1, 'enabled': False})['ok']


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), -1, 10.5, 10**1000])
def test_invalid_numeric_filters_never_reach_kodi(fixture, value):
    tools, kodi = fixture
    assert not tools.execute('find_media', {'min_rating': value})['ok']
    assert not kodi.calls


def test_audio_delay_uses_relative_value_and_rejects_out_of_range(fixture):
    tools, kodi = fixture
    assert tools.execute('set_audio_delay', {'milliseconds': 500})['audio_delay_ms'] == 300
    assert kodi.delay == -.3
    assert tools.execute('set_audio_delay', {'milliseconds': 13, 'relative': False})['audio_delay_ms'] == 25
    kodi.delay = -9.9
    assert not tools.execute('set_audio_delay', {'milliseconds': 500})['ok']
    assert tools.execute('set_video_view', {'zoom_percent': 120})['ok']
    assert kodi.view == {'mode': {'zoom': 1.2}}


def test_subtitle_adjustment_defers_exact_steps_without_touching_keys(fixture):
    tools, kodi = fixture;tools.defer_ui = True
    result = tools.execute('adjust_subtitles', {'delay_ms': -500, 'move': 'up', 'steps': 2})
    assert result['deferred'] == 'ui_navigation'
    operations = tools.ui_action['operations']
    assert [row['params']['action'] for row in operations] == ['subtitledelayminus'] * 5 + ['subtitleshiftup'] * 2
    assert not any(method == 'Input.ExecuteAction' for method, _ in kodi.calls)
    perform_operations(tools.ui_action, kodi.rpc)
    assert len([method for method, _ in kodi.calls if method == 'Input.ExecuteAction']) == 7
    assert not tools.execute('adjust_subtitles', {'delay_ms': 50})['ok']


def test_combined_filters_are_native_bounded_and_keep_verified_media(fixture):
    tools, kodi = fixture
    result = tools.execute('find_media', {'genre': '科幻', 'year_from': 2000, 'year_to': 2025,
        'min_rating': 8.5, 'max_minutes': 120, 'watched': 'unwatched'})
    assert result['ok'] and result['next_offset'] == 1 and result['total'] == 15
    params = kodi.calls[-1][1]
    rules = {row['field']: row for row in params['filter']['and']}
    assert rules['rating']['value'] == ['8.5', '10']
    assert rules['time']['value'] == '02:00:01'
    assert rules['playcount']['value'] == '0'
    assert (('movie', 10) in tools.media) and 'private-password' not in json.dumps(result)
    kodi.calls.clear()
    assert not tools.execute('find_media', {'year_from': 2025, 'year_to': 2000})['ok']
    assert not tools.execute('find_media', {'kind': 'tvshow', 'max_minutes': 20})['ok']
    assert not kodi.calls


def test_movie_episode_details_and_updates_do_not_guess_ids_or_reset_other_fields(fixture):
    tools, kodi = fixture
    assert not tools.execute('get_media_details', {'kind': 'movie', 'id': 999})['ok']
    assert tools.execute('get_media_details', {'kind': 'episode', 'id': 2})['ok']
    assert 'year' not in kodi.calls[-1][1]['properties']
    result = tools.execute('update_watch_state', {'kind': 'movie', 'id': 10, 'rating': 8})
    assert result['ok']
    assert kodi.calls[-1] == ('VideoLibrary.SetMovieDetails', {'movieid': 10, 'userrating': 8})
    assert not tools.execute('update_watch_state', {'kind': 'movie', 'id': 10})['ok']


def test_queue_uses_actual_indices_and_rejects_stale_snapshot(fixture):
    tools, kodi = fixture
    result = tools.execute('get_playlist', {})
    assert result['ok'] and result['items'][1]['position'] == 1
    assert 'private-password' not in json.dumps(result)
    kodi.queue.reverse()
    assert not tools.execute('edit_playlist', {'action': 'remove', 'position': 1})['ok']
    assert len(kodi.queue) == 2
    tools.execute('get_playlist', {})
    assert tools.execute('edit_playlist', {'action': 'move', 'position': 1, 'to': 0})['ok']
    assert kodi.queue[0]['id'] == 10
    result = tools.execute('edit_playlist', {'action': 'play_next', 'kind': 'movie', 'id': 11})
    assert result['ok']
    assert ('Playlist.Insert', {'playlistid': 1, 'position': 1, 'item': {'movieid': 11}}) in kodi.calls


def test_tv_queue_sorts_seasons_and_resumes_from_true_history(fixture):
    tools, kodi = fixture;tools.defer_ui = True
    result = tools.execute('play_tvshow_queue', {'tvshowid': 20})
    assert result['ok'] and result['queued_count'] == 2 and result['playing']['id'] == 2
    assert [row['id'] for row in kodi.queue] == [2, 3]
    assert ('Player.Open', {'item': {'playlistid': 1, 'position': 0}, 'options': {'shuffled': False, 'repeat': 'off'}}) in kodi.calls
    assert ('Player.Seek', {'playerid': 1, 'value': {'time': {'hours': 0, 'minutes': 1, 'seconds': 40, 'milliseconds': 0}}}) in kodi.calls
    assert tools.ui_action == 'ActivateWindow(FullscreenVideo)' and not kodi.commands


def test_duplicate_episode_versions_and_missing_history_never_replace_queue(fixture):
    tools, kodi = fixture
    kodi.episodes.append(dict(kodi.episodes[0], episodeid=99))
    result = tools.execute('play_tvshow_queue', {'tvshowid': 20, 'season': 2, 'episode': 1})
    assert result['ok'] and not result['started'] and result['status'] == 'multiple_versions'
    assert not any(name == 'Playlist.Clear' for name, _ in kodi.calls)
    kodi.episodes.pop()
    for row in kodi.episodes:row.update(resume={'position': 0, 'total': 1000}, lastplayed='')
    result = tools.execute('play_tvshow_queue', {'tvshowid': 20})
    assert not result['started'] and not any(name == 'Playlist.Clear' for name, _ in kodi.calls)


def test_failed_queue_build_restores_previous_queue_without_starting_video(fixture):
    tools, kodi = fixture;kodi.fail_add = True
    result = tools.execute('play_tvshow_queue', {'tvshowid': 20, 'season': 2, 'episode': 1})
    assert not result['ok'] and [row['id'] for row in kodi.queue] == [10, 11]
    assert not any(name == 'Player.Open' for name, _ in kodi.calls)


def test_playlist_start_checks_snapshot_and_only_seeks_matching_new_video(fixture):
    tools, kodi = fixture
    assert not tools.execute('play_playlist', {'position': 1})['ok']
    tools.execute('get_playlist', {})
    assert tools.execute('play_playlist', {'position': 1})['ok']
    assert kodi.item['id'] == 11


@pytest.mark.parametrize('resume', [None, False])
def test_playlist_continues_saved_offset_by_default_unless_restart_is_explicit(fixture, resume):
    tools, kodi = fixture
    original = tools.rpc
    def rpc(method, params):
        result = original(method, params)
        if method == 'VideoLibrary.GetMovieDetails':
            result['moviedetails']['resume'] = {'position': 125, 'total': 1000}
        return result
    tools.rpc = rpc
    tools.execute('get_playlist', {})
    arguments = {'position': 1}
    if resume is not None:arguments['resume'] = resume
    result = tools.execute('play_playlist', arguments)
    assert result['ok']
    seek = [params for method, params in kodi.calls if method == 'Player.Seek']
    assert bool(seek) is (resume is None)
    if seek:assert seek[0]['value']['time'] == {'hours': 0, 'minutes': 2, 'seconds': 5, 'milliseconds': 0}


def test_explicit_tv_queue_restart_starts_first_episode_and_keeps_version_choice(fixture):
    tools, kodi = fixture
    result = tools.execute('play_tvshow_queue', {'tvshowid': 20, 'resume': False})
    assert result['ok'] and [row['id'] for row in kodi.queue] == [1, 2, 3]
    assert not any(method == 'Player.Seek' for method, _ in kodi.calls)
    kodi.calls.clear()
    kodi.episodes.append(dict(next(row for row in kodi.episodes if row['episode'] == 1), episodeid=99))
    result = tools.execute('play_tvshow_queue', {'tvshowid': 20, 'resume': False})
    assert result['ok'] and not result['started'] and result['status'] == 'ambiguous'
    assert len(result['candidates']) == 2
    assert not any(method in ('Playlist.Clear', 'Player.Open') for method, _ in kodi.calls)


def test_queue_resume_never_seeks_a_different_video(fixture, monkeypatch):
    from types import SimpleNamespace
    from resources.lib.intelligence import playback_tools
    tools, kodi = fixture
    monkeypatch.setattr(playback_tools, 'playback_watcher', lambda: SimpleNamespace(
        ready=SimpleNamespace(is_set=lambda: True), failed=SimpleNamespace(is_set=lambda: False)))
    original = kodi.rpc
    def changed(method, params):
        result = original(method, params)
        if method == 'Player.GetItem':return {'item': {'id': 999, 'type': 'movie'}}
        return result
    tools.rpc = changed
    result = tools.execute('play_tvshow_queue', {'tvshowid': 20})
    assert not result['ok'] and '播放项目已变化' in result['error']
    assert not any(name == 'Player.Seek' for name, _ in kodi.calls)


@pytest.mark.parametrize('when', ['before_open', 'before_seek'])
def test_cancelled_queue_start_never_opens_or_seeks_after_cancellation(fixture, monkeypatch, when):
    from resources.lib.intelligence import playback_tools
    tools, kodi = fixture
    monkeypatch.setattr(playback_tools, 'playback_watcher', lambda: None)
    checks = [True] if when == 'before_open' else [False, False, True]
    with pytest.raises(InterruptedError):
        playback_tools.start_queue(tools, 0, kodi.queue[0], 10, lambda: checks.pop(0))
    assert not any(name == 'Player.Seek' for name, _ in kodi.calls)
    assert any(name == 'Player.Open' for name, _ in kodi.calls) is (when == 'before_seek')


def test_favourite_refs_hide_paths_and_add_is_idempotent(fixture):
    tools, kodi = fixture
    assert tools.execute('add_favourite', {})['added']
    assert tools.execute('add_favourite', {})['already_present']
    assert len(kodi.favourites) == 1
    result = tools.execute('list_favourites', {})
    assert 'private-password' not in json.dumps(result)
    identity = result['favourites'][0]['id']
    kodi.favourites[0]['title'] = '重命名'
    assert tools.execute('list_favourites', {})['favourites'][0]['id'] == identity
    assert tools.execute('open_favourite', {'id': identity})['ok']
    kodi.favourites.clear()
    assert not tools.execute('open_favourite', {'id': identity})['ok']


def test_window_favourite_is_deferred_structured_rpc_not_builtin_interpolation(fixture):
    tools, kodi = fixture;tools.defer_ui = True
    kodi.favourites = [{'title': '插件目录', 'type': 'window', 'window': 'videos',
        'windowparameter': 'plugin://plugin.example/?query=a,b("literal")'}]
    identity = tools.execute('list_favourites', {})['favourites'][0]['id']
    result = tools.execute('open_favourite', {'id': identity})
    assert result['deferred'] == 'ui_navigation' and not kodi.commands
    perform_operations(tools.ui_action, kodi.rpc)
    assert kodi.calls[-1] == ('GUI.ActivateWindow', {'window': 'videos', 'parameters': [kodi.favourites[0]['windowparameter']]})


def test_timer_has_own_name_and_native_state_survives_new_tools_instance(fixture, tmp_path):
    tools, kodi = fixture
    assert tools.execute('sleep_timer', {'action': 'set', 'minutes': 30})['active']
    assert kodi.commands[-1] == 'AlarmClock(voice_assistant_sleep,PlayerControl(Stop),30,silent)'
    fresh = KodiTools(kodi.rpc, kodi.builtin, profile=tmp_path)
    result = fresh.execute('sleep_timer', {'action': 'status'})
    assert result['active'] and 1795 <= result['remaining_seconds'] <= 1800
    assert not fresh.execute('sleep_timer', {'action': 'cancel'})['active']
    assert not fresh.execute('sleep_timer', {'action': 'status'})['active']


def test_search_text_only_reaches_original_visible_nonpassword_keyboard(fixture):
    tools, kodi = fixture;tools.defer_ui = True
    assert not tools.execute('send_search_text', {'text': '三体'})['ok']
    tools.ui_context = {'window': {'id': 10103}}
    assert tools.execute('send_search_text', {'text': '三体'})['deferred']
    with pytest.raises(ValueError, match='页面已变化'):perform_operations(tools.ui_action, kodi.rpc)
    kodi.window = 10103;kodi.hidden = True
    with pytest.raises(ValueError, match='密码'):perform_operations(tools.ui_action, kodi.rpc)
    kodi.hidden = False
    perform_operations(tools.ui_action, kodi.rpc)
    assert kodi.calls[-1] == ('Input.SendText', {'text': '三体', 'done': False})


def test_library_scan_is_background_refresh_is_targeted_and_media_permission_cannot_run_it(fixture):
    tools, kodi = fixture
    assert tools.execute('library_maintenance', {'action': 'scan'})['accepted']
    assert kodi.calls[-1] == ('VideoLibrary.Scan', {'showdialogs': False})
    assert tools.execute('library_maintenance', {'action': 'refresh', 'kind': 'movie', 'id': 10})['accepted']
    assert kodi.calls[-1] == ('VideoLibrary.RefreshMovie', {'movieid': 10, 'ignorenfo': False})
    tools.permissions = Permissions('media', 'full_access')
    assert not tools.execute('library_maintenance', {'action': 'scan'})['ok']
    assert tools.execute('get_playlist', {})['ok']
