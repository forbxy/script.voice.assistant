"""Keep Emby titles until the server changes, checking small native queries first."""
from datetime import datetime, timezone
import time
import uuid

from .emby_client import MAX_ITEMS, Unavailable, identifier
from ..data_paths import data_path
from ..storage import read_json, write_json

PARAMS = {'IncludeItemTypes': 'Movie,Series', 'Recursive': 'true', 'EnableImages': 'false',
          'EnableUserData': 'false', 'Fields': 'ProductionYear,PresentationUniqueKey', 'SortBy': 'SortName',
          'SortOrder': 'Ascending'}
KEYS = ('Id', 'Name', 'Type', 'ProductionYear', 'PresentationUniqueKey')


def minimal(items):
    from .emby import candidate
    result, seen = [], set()
    for item in items:
        # Validate cached and incremental records just like a full catalogue.
        if not isinstance(item, dict):
            raise Unavailable('Emby 目录项目无效。')
        candidate(item, {'_identity': '', 'ServerId': ''})
        key = identifier(item.get('Id'))
        if key in seen:
            raise Unavailable('Emby 目录包含重复项目，请重新查询。')
        seen.add(key)
        row = {name: item[name] for name in KEYS if name in item}
        presentation = row.get('PresentationUniqueKey')
        if presentation is not None and (not isinstance(presentation, str) or len(presentation) > 512):
            raise Unavailable('Emby 影片标识无效。')
        if not presentation:
            row.pop('PresentationUniqueKey', None)
        result.append(row)
    return result


def counted(client, **extra):
    value = client.request(client.items_path, dict(PARAMS, Limit=1, StartIndex=0,
                                                  EnableTotalRecordCount='true', **extra))
    if (not isinstance(value, dict) or type(value.get('TotalRecordCount')) is not int
            or not 0 <= value['TotalRecordCount'] <= MAX_ITEMS or not isinstance(value.get('Items'), list)
            or len(value['Items']) != min(1, value['TotalRecordCount'])):
        raise Unavailable('Emby 变更查询结果无效。')
    return value['TotalRecordCount']


def binding(cache):
    # The cursor must never be applied to another snapshot after a partial write.
    return {key: cache.get(key) for key in ('revision', 'updated_at', 'generation')}


def supported(client, count):
    # Some proxies accept unknown parameters but silently ignore them. Check both
    # an empty future range and a past range covering the entire current library.
    try:
        return (counted(client, MinDateLastSaved='2100-01-01T00:00:00Z') == 0
                and counted(client, MinDateLastSaved='1900-01-01T00:00:00Z') == count)
    except InterruptedError:
        raise
    except Unavailable:
        return False


def same(left, right):
    return {row['Id']: row for row in left} == {row['Id']: row for row in right}


def merge_versions(previous, changed, client):
    """A saved secondary version is not a new work in the grouped catalogue."""
    old = {str(row['Id']): row for row in previous}
    presentations, names = {}, {}
    for row in previous:
        key = row.get('PresentationUniqueKey')
        if key:
            presentations.setdefault((row['Type'], key), []).append(row)
        names.setdefault((row['Type'], row['Name'], row.get('ProductionYear')), []).append(row)
    updates = {}
    for row in changed:
        client.check()
        key = str(row['Id'])
        if key in old:
            updates[key] = row
            continue
        presentation = row.get('PresentationUniqueKey')
        if not presentation:
            row = minimal([client.item(key)])[0]
            presentation = row.get('PresentationUniqueKey')
        if not presentation:
            return None
        matches = presentations.get((row['Type'], presentation), [])
        if not matches:
            # Upgrade an existing cache lazily: a matching name is only a hint.
            # Server metadata must prove the identity; never merge by name alone.
            matches = names.get((row['Type'], row['Name'], row.get('ProductionYear')), [])
        if len(matches) != 1:
            return None
        canonical = minimal([client.item(str(matches[0]['Id']))])[0]
        if (str(canonical['Id']) != str(matches[0]['Id']) or canonical['Type'] != row['Type']
                or canonical.get('PresentationUniqueKey') != presentation):
            return None
        updates[str(canonical['Id'])] = canonical
    old.update(updates)
    return list(old.values())


def catalogue(profile, account, client, interval):
    from .emby import candidate
    path = data_path(profile, 'cache/emby-' + account['_identity'] + '.json')
    state_path = data_path(profile, 'cache/emby-state/' + account['_identity'] + '.json')
    cache, state, now = read_json(path, {}), read_json(state_path, {}), time.time()
    valid = (isinstance(cache, dict) and cache.get('version') == 1
             and cache.get('revision') == account['_revision'] and isinstance(cache.get('items'), list)
             and len(cache['items']) <= MAX_ITEMS and type(cache.get('updated_at')) in (int, float))
    state_valid = (valid and isinstance(state, dict) and state.get('version') == 1
                   and state.get('snapshot') == binding(cache) and state.get('mode') in ('saved_since', 'full')
                   and type(state.get('checked_at')) in (int, float))
    checked_at = state['checked_at'] if state_valid else cache.get('updated_at', 0) if valid else 0
    if valid and 0 <= now-checked_at < interval:
        client.check()
        return [candidate(item, account) for item in cache['items']]

    started = time.time()
    mode, items = 'full', None
    previous = minimal(cache['items']) if valid else None
    if previous is not None:
        count = counted(client)
        offset = getattr(client, 'clock_offset', None)
        if type(offset) in (int, float):
            if state_valid and state['mode'] == 'saved_since':
                mode = 'saved_since'
            elif supported(client, count):
                mode = 'saved_since'
            # The first query's server Date avoids relying on a synchronized box
            # clock. Overlap covers HTTP Date's second precision and in-flight saves.
            since = state.get('since') if state_valid else cache['updated_at'] + offset - 5
            if (mode == 'saved_since' and count == len(previous) and type(since) in (int, float)
                    and 0 <= since <= started + offset):
                date = datetime.fromtimestamp(since, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                try:
                    changed = minimal(client.pages(dict(PARAMS, MinDateLastSaved=date)))
                    merged = merge_versions(previous, changed, client)
                    # Unknown works, removed canonical IDs and count changes
                    # still require a complete scan to remove obsolete entries.
                    if merged is not None and counted(client) == count:
                        items = merged
                except InterruptedError:
                    raise
                except Unavailable:
                    # A server/proxy can lose this capability after an upgrade.
                    mode = 'full'

    if items is None:
        items = minimal(client.pages(PARAMS))
    rows = [candidate(item, account) for item in items]
    client.check()
    offset = getattr(client, 'clock_offset', None)
    if previous is None and type(offset) in (int, float):
        mode = 'saved_since' if supported(client, len(items)) else 'full'
    if previous is None or not same(previous, items):
        cache = {'version': 1, 'revision': account['_revision'], 'updated_at': started,
                 'generation': uuid.uuid4().hex, 'items': items}
        write_json(path, cache)
    # Unchanged title data is never rewritten just to advance its check time.
    # Keep the small cursor separate from the multi-megabyte title snapshot.
    if type(offset) in (int, float):
        since = started + offset - 5
    else:
        mode, since = 'full', None
    write_json(state_path, {'version': 1, 'snapshot': binding(cache), 'checked_at': time.time(),
                            'since': since, 'mode': mode})
    return rows
