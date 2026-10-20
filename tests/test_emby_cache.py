"""Emby cache freshness: real changes, deletion, capability fallback and cursors."""
import copy
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from resources.lib.intelligence import emby
from resources.lib.intelligence.emby_client import Unavailable


@pytest.fixture
def cache_env(tmp_path):
    account = {'_identity': 'test-account', '_revision': 'test-revision', 'ServerId': 'test-server'}
    items = [dict(Id='1', Name='惊天营救', Type='Movie', ProductionYear=2020),
             dict(Id='2', Name='测试电视剧', Type='Series', ProductionYear=2024)]
    changed, events = [], []
    flags = {'supported': True, 'fail': False, 'cancel': False, 'offset': 0, 'reject_saved': False}

    class Client:
        items_path = 'Users/test/Items'

        def __init__(self):
            self.clock_offset = flags['offset']

        def check(self):
            if flags['cancel']:
                raise InterruptedError('cancelled')

        def request(self, path, params):
            self.check()
            events.append(('request', params))
            if flags['fail']:
                raise Unavailable('server unavailable')
            count = len(items)
            if flags['supported'] and params.get('MinDateLastSaved', '').startswith('2100'):
                count = 0
            return {'TotalRecordCount': count, 'Items': copy.deepcopy(items[:min(1, count)])}

        def pages(self, params):
            self.check()
            events.append(('delta' if params.get('MinDateLastSaved') else 'full', params))
            if params.get('MinDateLastSaved') and flags['reject_saved']:
                raise Unavailable('unsupported saved filter')
            return copy.deepcopy(changed if params.get('MinDateLastSaved') else items)

        def item(self, identifier):
            self.check()
            events.append(('item', identifier))
            value = next((row for row in items + changed if str(row['Id']) == str(identifier)), None)
            if value is None:
                raise Unavailable('item removed')
            return copy.deepcopy(value)

    path = tmp_path/'cache/emby-test-account.json'
    state_path = tmp_path/'cache/emby-state/test-account.json'

    def expire():
        state = json.loads(state_path.read_text())
        state['checked_at'] -= 301
        state_path.write_text(json.dumps(state))

    return SimpleNamespace(load=lambda: emby.catalogue(tmp_path, account, Client()), path=path,
                           state_path=state_path, items=items, changed=changed, flags=flags,
                           account=account, events=events, expire=expire)


def test_expired_check_does_not_rescan_or_rewrite_unchanged_titles(cache_env):
    e = cache_env
    expected = e.load()
    before, modified = e.path.read_bytes(), e.path.stat().st_mtime_ns
    e.events.clear()
    assert e.load() == expected and not e.events
    e.expire()
    assert e.load() == expected
    assert e.path.read_bytes() == before and e.path.stat().st_mtime_ns == modified
    assert [kind for kind, _ in e.events] == ['request', 'delta', 'request']
    assert json.loads(e.state_path.read_text())['mode'] == 'saved_since'


def test_same_count_rename_and_year_change_updates_only_saved_records(cache_env):
    e = cache_env
    e.load(); e.events.clear(); e.expire()
    e.items[0].update(Name='惊天营救新版', ProductionYear=2021)
    e.changed.append(e.items[0])
    rows = e.load()
    assert {row['title'] for row in rows} == {'惊天营救新版', '测试电视剧'}
    assert rows[0]['year'] == 2021
    assert not any(kind == 'full' for kind, _ in e.events)
    assert json.loads(e.path.read_text())['items'][0]['Name'] == '惊天营救新版'


def test_non_title_metadata_changes_only_advance_the_small_cursor(cache_env):
    e = cache_env
    e.load(); before = e.path.read_bytes(); e.expire()
    old_cursor = json.loads(e.state_path.read_text())['checked_at']
    e.changed.append(dict(e.items[0], Overview='new metadata', ImageTags={'Primary': 'new'},
                          UserData={'PlaybackPositionTicks': 1000}))
    e.load()
    assert e.path.read_bytes() == before
    assert json.loads(e.state_path.read_text())['checked_at'] > old_cursor
    assert b'Overview' not in e.path.read_bytes() and b'PlaybackPositionTicks' not in e.path.read_bytes()


@pytest.mark.parametrize('operation', ['add', 'remove', 'replace'])
def test_membership_changes_rebuild_without_leaving_deleted_ids(cache_env, operation):
    e = cache_env
    e.load(); e.events.clear(); e.expire()
    new = dict(Id='3', Name='新电影', Type='Movie', ProductionYear=2026)
    if operation in ('remove', 'replace'):
        e.items.pop(0)
    if operation in ('add', 'replace'):
        e.items.append(new); e.changed.append(new)
    rows = e.load()
    assert {row['_emby_id'] for row in rows} == {row['Id'] for row in e.items}
    assert any(kind == 'full' for kind, _ in e.events)


def test_silently_ignored_time_filter_uses_full_comparison_without_rewriting(cache_env):
    e = cache_env
    e.flags['supported'] = False
    e.load(); before = e.path.read_bytes(); e.events.clear(); e.expire()
    e.load()
    assert e.path.read_bytes() == before
    assert any(kind == 'full' for kind, _ in e.events)
    assert not any(kind == 'delta' for kind, _ in e.events)
    assert json.loads(e.state_path.read_text())['mode'] == 'full'


def test_missing_server_date_falls_back_to_full_verification(cache_env):
    e = cache_env
    e.flags['offset'] = None
    e.load(); e.events.clear(); e.expire()
    e.load()
    assert any(kind == 'full' for kind, _ in e.events)
    assert not any(kind == 'delta' for kind, _ in e.events)


@pytest.mark.parametrize('cancel', [True, False])
def test_failed_check_does_not_mark_stale_data_as_fresh(cache_env, cancel):
    e = cache_env
    e.load(); e.expire()
    before, state = e.path.read_bytes(), e.state_path.read_bytes()
    e.flags['cancel' if cancel else 'fail'] = True
    with pytest.raises(InterruptedError if cancel else Unavailable):
        e.load()
    assert e.path.read_bytes() == before and e.state_path.read_bytes() == state


def test_cursor_from_another_snapshot_cannot_skip_changed_titles(cache_env):
    e = cache_env
    e.load(); e.expire()
    state = json.loads(e.state_path.read_text())
    state['snapshot']['generation'] = 'other-generation'
    state['checked_at'] += 1000
    e.state_path.write_text(json.dumps(state))
    cache = json.loads(e.path.read_text()); cache['updated_at'] -= 301
    e.path.write_text(json.dumps(cache))
    e.items[0]['Name'] = '惊天营救修改版'; e.changed.append(e.items[0])
    assert e.load()[0]['title'] == '惊天营救修改版'


def test_change_query_uses_server_clock_and_overlaps_inflight_saves(cache_env):
    e = cache_env
    e.flags['offset'] = 3600
    e.load(); e.events.clear(); e.expire()
    cursor = json.loads(e.state_path.read_text())['since']
    e.load()
    query = next(params for kind, params in e.events if kind == 'delta')
    actual = datetime.fromisoformat(query['MinDateLastSaved'].replace('Z', '+00:00')).timestamp()
    assert 0 <= cursor-actual < 1
    assert 'MinDateLastSavedForUser' not in query


def test_invalid_delta_falls_back_to_complete_snapshot(cache_env):
    e = cache_env
    e.load(); e.expire(); before = e.path.read_bytes()
    e.changed.extend([e.items[0], e.items[0]])
    e.load()
    assert e.path.read_bytes() == before
    assert json.loads(e.state_path.read_text())['mode'] == 'full'


def test_server_losing_incremental_support_falls_back_to_full_comparison(cache_env):
    e = cache_env
    e.load(); e.expire(); e.events.clear(); before = e.path.read_bytes()
    e.flags['reject_saved'] = True
    e.load()
    assert e.path.read_bytes() == before
    assert [kind for kind, _ in e.events] == ['request', 'delta', 'full']
    assert json.loads(e.state_path.read_text())['mode'] == 'full'


@pytest.mark.parametrize('legacy', [False, True])
def test_saved_secondary_version_does_not_rescan_grouped_catalogue(cache_env, legacy):
    e = cache_env
    if not legacy:
        e.items[0]['PresentationUniqueKey'] = 'same-film'
    e.load(); e.events.clear(); e.expire()
    e.items[0]['PresentationUniqueKey'] = 'same-film'
    e.changed.append(dict(e.items[0], Id='secondary'))
    rows = e.load()
    assert {row['_emby_id'] for row in rows} == {'1', '2'}
    assert not any(kind == 'full' for kind, _ in e.events)
    assert json.loads(e.path.read_text())['items'][0]['PresentationUniqueKey'] == 'same-film'
    # Subsequent changes to the same version must also avoid a scan or rewrite.
    before = e.path.read_bytes(); e.expire(); e.events.clear(); e.load()
    assert e.path.read_bytes() == before and not any(kind == 'full' for kind, _ in e.events)


def test_same_title_different_presentation_is_not_treated_as_a_version(cache_env):
    e = cache_env
    e.items[0]['PresentationUniqueKey'] = 'old-film'
    e.load(); e.expire(); e.events.clear()
    e.changed.append(dict(e.items[0], Id='different', PresentationUniqueKey='new-film'))
    e.load()
    assert any(kind == 'full' for kind, _ in e.events)


def test_removed_canonical_version_is_not_kept_by_matching_presentation(cache_env):
    e = cache_env
    e.items[0]['PresentationUniqueKey'] = 'same-film'
    e.load(); e.expire(); e.events.clear()
    e.items[0] = dict(e.items[0], Id='replacement')
    e.changed.append(e.items[0])
    rows = e.load()
    assert {row['_emby_id'] for row in rows} == {'replacement', '2'}
    assert any(kind == 'full' for kind, _ in e.events)
