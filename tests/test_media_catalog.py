"""Catalogue lifetime follows content changes, not elapsed conversation time."""
import json
from types import SimpleNamespace

import pytest

from resources.lib.intelligence.library_changes import LibraryChanges, revisions
from resources.lib.intelligence.media_catalog import catalogue


@pytest.fixture
def library(tmp_path):
    rows = {'movie': [{'movieid': 1, 'title': '原片名', 'year': 2020}],
            'tvshow': [{'tvshowid': 2, 'title': '电视剧', 'year': 2021}]}
    reads = []
    def rpc(method, params):
        if method == 'PVR.GetProperties':
            return {'available': False}
        kind, key = ('movie', 'movies') if method == 'VideoLibrary.GetMovies' else ('tvshow', 'tvshows')
        assert method in ('VideoLibrary.GetMovies', 'VideoLibrary.GetTVShows')
        reads.append(kind)
        batch = [dict(row) for row in rows[kind]]
        return {key: batch, 'limits': {'start': 0, 'end': len(batch), 'total': len(batch)}}
    return SimpleNamespace(profile=tmp_path, rpc=rpc), rows, reads


def test_unchanged_library_keeps_cache_beyond_five_minutes(library, monkeypatch):
    tools, _, reads = library
    tracker = LibraryChanges(tools.profile)
    clock = [10000]
    monkeypatch.setattr('resources.lib.intelligence.media_catalog.time.time', lambda: clock[0])
    before = catalogue(tools, 'all', lambda: False)
    assert reads == ['movie', 'tvshow']
    clock[0] += 60 * 60 * 24 * 30
    assert catalogue(tools, 'all', lambda: False) == before
    assert reads == ['movie', 'tvshow']
    tracker.close()


@pytest.mark.parametrize('method,payload', [
    ('OnUpdate', {'item': {'type': 'movie', 'id': 1}}),
    ('OnUpdate', {'type': 'movie', 'id': 1, 'added': True, 'playcount': 0}),
    ('OnRemove', {'type': 'movie', 'id': 1}),
])
def test_only_changed_kind_reloads_and_returns_new_names(library, method, payload):
    tools, rows, reads = library
    tracker = LibraryChanges(tools.profile)
    catalogue(tools, 'all', lambda: False)
    rows['movie'][0]['title'] = '新片名'
    tracker.notify('xbmc', 'VideoLibrary.' + method, json.dumps(payload))
    actual = catalogue(tools, 'all', lambda: False)
    assert actual[0]['title'] == '新片名'
    assert reads == ['movie', 'tvshow', 'movie']
    tracker.close()


def test_added_or_removed_show_changes_candidate_set(library):
    tools, rows, reads = library
    tracker = LibraryChanges(tools.profile)
    catalogue(tools, 'tvshow', lambda: False)
    rows['tvshow'].append({'tvshowid': 3, 'title': '新增电视剧', 'year': 2026})
    tracker.notify('xbmc', 'VideoLibrary.OnUpdate', {'type': 'tvshow', 'id': 3, 'added': True})
    assert len(catalogue(tools, 'tvshow', lambda: False)) == 2
    rows['tvshow'].pop(0)
    tracker.notify('xbmc', 'VideoLibrary.OnRemove', {'type': 'tvshow', 'id': 2})
    assert [row['title'] for row in catalogue(tools, 'tvshow', lambda: False)] == ['新增电视剧']
    assert reads == ['tvshow'] * 3
    tracker.close()


@pytest.mark.parametrize('sender,method,payload', [
    ('xbmc', 'VideoLibrary.OnUpdate', {'item': {'type': 'movie', 'id': 1}, 'playcount': 1}),
    ('xbmc', 'VideoLibrary.OnUpdate', {'type': 'movie', 'id': 1, 'playcount': 0}),
    ('xbmc', 'VideoLibrary.OnUpdate', {'type': 'episode', 'id': 1}),
    ('xbmc', 'VideoLibrary.OnRemove', {'type': 'musicvideo', 'id': 1}),
    ('xbmc', 'Player.OnStop', {'item': {'type': 'movie', 'id': 1}}),
    ('xbmc', 'AudioLibrary.OnScanFinished', None),
    ('other-addon', 'VideoLibrary.OnRemove', {'type': 'movie', 'id': 1}),
])
def test_playback_changes_and_unrelated_notifications_do_not_reload(library, sender, method, payload):
    tools, _, reads = library
    tracker = LibraryChanges(tools.profile)
    catalogue(tools, 'all', lambda: False)
    assert not tracker.notify(sender, method, json.dumps(payload))
    catalogue(tools, 'all', lambda: False)
    assert reads == ['movie', 'tvshow']
    tracker.close()


@pytest.mark.parametrize('operation', ['Scan', 'Clean'])
def test_bulk_changes_coalesce_and_finished_operation_invalidates(library, operation):
    tools, _, reads = library
    tracker = LibraryChanges(tools.profile)
    catalogue(tools, 'all', lambda: False)
    tracker.notify('xbmc', 'VideoLibrary.On' + operation + 'Started', 'null')
    started = revisions(tools.profile)
    for identifier in range(100):
        tracker.notify('xbmc', 'VideoLibrary.OnUpdate', {'type': 'movie', 'id': identifier, 'added': True})
    assert revisions(tools.profile) == started
    catalogue(tools, 'all', lambda: False)
    tracker.notify('xbmc', 'VideoLibrary.On' + operation + 'Finished', 'null')
    assert revisions(tools.profile) != started
    catalogue(tools, 'all', lambda: False)
    assert reads == ['movie', 'tvshow'] * 3
    tracker.close()


def test_revision_change_while_reading_does_not_commit_partial_catalogue(library):
    tools, rows, _ = library
    tracker = LibraryChanges(tools.profile)
    original = tools.rpc
    def changed(method, params):
        result = original(method, params)
        tracker.notify('xbmc', 'VideoLibrary.OnUpdate', {'type': 'movie', 'id': 1})
        return result
    tools.rpc = changed
    with pytest.raises(ValueError, match='正在更新'):
        catalogue(tools, 'movie', lambda: False)
    assert not (tools.profile / 'cache/media-search.json').exists()
    tools.rpc = original
    assert catalogue(tools, 'movie', lambda: False)[0]['title'] == rows['movie'][0]['title']
    tracker.close()


def test_listener_restart_recovers_changes_missed_while_offline(library):
    tools, rows, reads = library
    first = LibraryChanges(tools.profile)
    catalogue(tools, 'movie', lambda: False)
    first.close()
    rows['movie'][0]['title'] = '停用期间改名'
    second = LibraryChanges(tools.profile)
    assert catalogue(tools, 'movie', lambda: False)[0]['title'] == '停用期间改名'
    assert reads == ['movie'] * 2
    first.close()  # An older listener must not delete the new owner's revision.
    assert revisions(tools.profile)
    second.close()
    assert revisions(tools.profile) is None


@pytest.mark.parametrize('failure', ['owner_exited', 'pid_reused', 'broken_state'])
def test_dead_or_untrustworthy_revision_is_not_used(library, failure):
    tools, _, _ = library
    tracker = LibraryChanges(tools.profile)
    state = json.loads(tracker.path.read_text())
    if failure == 'owner_exited':
        state['owner_pid'] = 2147483647
    elif failure == 'pid_reused':
        state['owner_start'] = 'old-boot:old-process'
    else:
        state['revisions'] = ['invalid']
    tracker.path.write_text(json.dumps(state))
    assert revisions(tools.profile) is None
    tracker.close()


def test_without_service_short_fallback_expires_each_group_independently(library, monkeypatch):
    tools, rows, reads = library
    clock = [10000]
    monkeypatch.setattr('resources.lib.intelligence.media_catalog.time.time', lambda: clock[0])
    catalogue(tools, 'movie', lambda: False)
    clock[0] += 250
    catalogue(tools, 'tvshow', lambda: False)
    clock[0] += 100
    rows['movie'][0]['title'] = '更新'
    assert catalogue(tools, 'all', lambda: False)[0]['title'] == '更新'
    assert reads == ['movie', 'tvshow', 'movie']
