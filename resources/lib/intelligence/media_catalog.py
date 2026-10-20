"""Small private title/ID cache; actual playback always revalidates the target."""
import time

from ..data_paths import data_path
from ..storage import read_json, write_json

VERSION = 2
FALLBACK_MAX_AGE = 300  # Only when no live service is listening for library changes.


def _library(rpc, kind, cancelled):
    method, key = ('VideoLibrary.GetMovies', 'movies') if kind == 'movie' else ('VideoLibrary.GetTVShows', 'tvshows')
    rows, seen, total = [], set(), None
    while len(rows) < 50000:
        if cancelled():
            raise InterruptedError('已取消查找。')
        start = len(rows)
        result = rpc(method, {'properties': ['title', 'year'], 'limits': {'start': start, 'end': start + 10000},
                              'sort': {'method': 'title', 'order': 'ascending'}})
        batch = result.get(key, [])
        limits = result.get('limits', {})
        count = limits.get('total')
        if (limits.get('start', start) != start or total is not None and count != total
                or count is not None and (start + len(batch) > count or not batch and start < count)):
            raise ValueError('影视库查询不完整，请稍后重试。')
        total = count
        for row in batch:
            identifier = row.get(kind + 'id')
            if type(identifier) is not int or identifier <= 0 or identifier in seen or not row.get('title'):
                raise ValueError('影视库项目无效或重复，请重新查询。')
            seen.add(identifier)
            rows.append({'kind': kind, 'id': identifier, 'title': row['title'], 'year': row.get('year', 0)})
        if total == len(rows) or total is None and len(batch) < 10000:
            return rows
    raise ValueError('影视库项目过多，无法完整比较候选。')


def catalogue(tools, kind, cancelled):
    from .library_changes import revisions
    groups = ('movie', 'tvshow') if kind == 'all' else (kind,) if kind != 'channel' else ()
    path = data_path(tools.profile, 'cache/media-search.json') if tools.profile is not None else None
    cache = read_json(path, {}) if path else {}
    now = time.time()
    if not isinstance(cache, dict) or cache.get('version') != VERSION:
        cache = {'version': VERSION, 'updated_at': now, 'groups': {}}
    current_revisions = revisions(tools.profile) if path and groups else None
    cached = cache.get('groups')
    if not isinstance(cached, dict):
        cached = cache['groups'] = {}
    for name in ('revisions', 'group_updated_at'):
        if not isinstance(cache.get(name), dict):
            cache[name] = {}
    rows, changed = [], False
    for current in groups:
        revision = current_revisions[current] if current_revisions else None
        timestamp = cache['group_updated_at'].get(current)
        fresh = (cache['revisions'].get(current) == revision and
                 (current_revisions is not None or type(timestamp) in (int, float)
                  and 0 <= now - timestamp < FALLBACK_MAX_AGE))
        if not isinstance(cached.get(current), list) or not fresh:
            cached[current] = _library(tools.rpc, current, cancelled)
            cache['revisions'][current] = revision
            cache['group_updated_at'][current] = now
            changed = True
        rows.extend(cached[current])
    if cancelled():
        raise InterruptedError('已取消查找。')
    if path and groups:
        after = revisions(tools.profile)
        if any((current_revisions or {}).get(current) != (after or {}).get(current) for current in groups):
            raise ValueError('影视库正在更新，请稍后重试。')
    if changed and path:
        cache['updated_at'] = now
        write_json(path, cache)
    if kind in ('all', 'channel'):
        from .live_tv import _channels
        try:
            channels = _channels(tools.rpc, cancelled)
        except ValueError:
            if kind == 'channel':
                raise
        else:
            rows.extend({'kind': 'channel', 'id': row['channelid'],
                         'title': row.get('channel') or row.get('label', ''),
                         'channelnumber': row.get('channelnumber', 0), 'locked': row.get('locked', False),
                         'thumbnail': row.get('thumbnail', '')}
                        for row in channels)
    return rows
