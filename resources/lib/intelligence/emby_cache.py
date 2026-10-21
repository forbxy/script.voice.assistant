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


FULL_REASONS = {
    'cache_missing': '缓存不存在、无法读取或为空',
    'cache_format': '缓存格式无效',
    'cache_version': '缓存版本不匹配',
    'account_revision_changed': '服务器地址或登录凭据已变化',
    'cache_items': '缓存条目格式无效',
    'cache_too_large': '缓存条目数超出限制',
    'cache_timestamp': '缓存时间无效',
    'server_date_missing': '缺少可用的服务器时间',
    'saved_filter_unsupported': '服务器未通过增量时间过滤能力检查',
    'server_count_changed': '服务器条目数与缓存不一致',
    'since_invalid': '增量查询时间游标无效',
    'changed_item_missing_presentation': '新增编号缺少多版本归属标识',
    'canonical_not_found': '增量条目无法对应到缓存中的影片',
    'canonical_ambiguous': '增量条目对应到多个缓存影片',
    'canonical_identity_changed': '服务器复核的影片归属与缓存不一致',
    'delta_query_failed': '增量目录查询失败',
    'delta_invalid': '增量目录校验失败',
    'version_lookup_failed': '多版本影片归属复核失败',
    'count_verify_failed': '增量归并后的条目数复核失败',
    'server_count_changed_during_delta': '增量查询期间服务器条目数发生变化',
}


class RefreshLog:
    """Small, credential-free checkpoints; logging must not affect cache work."""
    def __init__(self, account):
        self.started = time.monotonic()
        self.fields = {'account': account['_identity'][:12], 'check': uuid.uuid4().hex[:8]}
        self.timings, self.stage = {}, 'cache_read'

    def run(self, stage, function, *args, **kwargs):
        self.stage = stage
        started = time.monotonic()
        try:
            return function(*args, **kwargs)
        finally:
            self.timings[stage] = self.timings.get(stage, 0) + time.monotonic() - started

    def emit(self, event, **fields):
        try:
            import json
            import xbmc
            value = dict(self.fields, event=event, stage=self.stage,
                         elapsed_ms=round((time.monotonic() - self.started) * 1000, 1),
                         stages_ms={key: round(seconds * 1000, 1) for key, seconds in self.timings.items()},
                         **fields)
            if value.get('full_reason'):
                value['reason_text'] = FULL_REASONS[value['full_reason']]
            label = {'full_start': '全量刷新开始', 'full_complete': '全量刷新完成',
                     'check_complete': '目录检查完成', 'check_failed': '目录检查失败',
                     'check_cancelled': '目录检查取消'}[event]
            xbmc.log('[voice-emby-cache] ' + label + ' ' + json.dumps(value, ensure_ascii=False), xbmc.LOGINFO)
        except Exception:
            pass


def cache_status(cache, account):
    if not cache:
        return 'cache_missing'
    if not isinstance(cache, dict):
        return 'cache_format'
    if cache.get('version') != 1:
        return 'cache_version'
    if cache.get('revision') != account['_revision']:
        return 'account_revision_changed'
    if not isinstance(cache.get('items'), list):
        return 'cache_items'
    if len(cache['items']) > MAX_ITEMS:
        return 'cache_too_large'
    if type(cache.get('updated_at')) not in (int, float):
        return 'cache_timestamp'
    return 'valid'


def cursor_status(state, cache, valid):
    if not valid:
        return 'cache_invalid'
    if not state:
        return 'missing_or_unreadable'
    if not isinstance(state, dict) or state.get('version') != 1:
        return 'invalid_format_or_version'
    if state.get('snapshot') != binding(cache):
        return 'snapshot_mismatch'
    if state.get('mode') not in ('saved_since', 'full'):
        return 'invalid_mode'
    if type(state.get('checked_at')) not in (int, float):
        return 'invalid_checked_at'
    return 'valid'


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


def supported(client, count, trace):
    # Some proxies accept unknown parameters but silently ignore them. Check both
    # an empty future range and a past range covering the entire current library.
    try:
        future = counted(client, MinDateLastSaved='2100-01-01T00:00:00Z')
        trace.fields['probe_future_count'] = future
        if future != 0:
            trace.fields['saved_filter'] = 'future_range_not_empty'
            return False
        past = counted(client, MinDateLastSaved='1900-01-01T00:00:00Z')
        trace.fields['probe_past_count'] = past
        trace.fields['saved_filter'] = 'supported' if past == count else 'past_count_mismatch'
        return past == count
    except InterruptedError:
        raise
    except Unavailable as error:
        trace.fields.update(saved_filter='probe_failed', probe_error=type(error).__name__)
        return False


def same(left, right):
    return {row['Id']: row for row in left} == {row['Id']: row for row in right}


def merge_versions(previous, changed, client, trace):
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
            trace.fields['merge_step'] = 'secondary_details'
            row = minimal([client.item(key)])[0]
            presentation = row.get('PresentationUniqueKey')
        if not presentation:
            trace.fields['full_reason'] = 'changed_item_missing_presentation'
            return None
        matches = presentations.get((row['Type'], presentation), [])
        if not matches:
            # Upgrade an existing cache lazily: a matching name is only a hint.
            # Server metadata must prove the identity; never merge by name alone.
            matches = names.get((row['Type'], row['Name'], row.get('ProductionYear')), [])
        if len(matches) != 1:
            trace.fields.update(full_reason='canonical_not_found' if not matches else 'canonical_ambiguous',
                                canonical_matches=len(matches))
            return None
        trace.fields['merge_step'] = 'canonical_details'
        canonical = minimal([client.item(str(matches[0]['Id']))])[0]
        if (str(canonical['Id']) != str(matches[0]['Id']) or canonical['Type'] != row['Type']
                or canonical.get('PresentationUniqueKey') != presentation):
            trace.fields['full_reason'] = 'canonical_identity_changed'
            return None
        updates[str(canonical['Id'])] = canonical
    old.update(updates)
    return list(old.values())


def catalogue(profile, account, client, interval):
    from .emby import candidate
    path = data_path(profile, 'cache/emby-' + account['_identity'] + '.json')
    state_path = data_path(profile, 'cache/emby-state/' + account['_identity'] + '.json')
    trace = RefreshLog(account)
    try:
        cache = trace.run('cache_read', read_json, path, {})
        state = trace.run('cursor_read', read_json, state_path, {})
        now = time.time()
        status = cache_status(cache, account)
        valid = status == 'valid'
        cursor = cursor_status(state, cache, valid)
        state_valid = cursor == 'valid'
        checked_at = state['checked_at'] if state_valid else cache.get('updated_at', 0) if valid else 0
        if valid and 0 <= now-checked_at < interval:
            client.check()
            return [candidate(item, account) for item in cache['items']]

        trace.fields.update(cache_status=status, cursor_status=cursor, interval_s=interval,
                            cache_age_s=round(now-checked_at, 1) if valid else None,
                            old_count=len(cache['items']) if isinstance(cache, dict)
                            and isinstance(cache.get('items'), list) else None)
        started = time.time()
        mode, items = 'full', None
        previous = trace.run('cache_validate', minimal, cache['items']) if valid else None
        if previous is None:
            trace.fields['full_reason'] = status
        else:
            count = trace.run('count_before', counted, client)
            trace.fields['server_count_before'] = count
            offset = getattr(client, 'clock_offset', None)
            trace.fields['server_clock_offset_s'] = round(offset, 3) if type(offset) in (int, float) else None
            if type(offset) not in (int, float):
                trace.fields['full_reason'] = 'server_date_missing'
            else:
                if state_valid and state['mode'] == 'saved_since':
                    mode = 'saved_since'
                    trace.fields['saved_filter'] = 'previously_supported'
                elif trace.run('capability_probe', supported, client, count, trace):
                    mode = 'saved_since'
                # Use server time and overlap to cover in-flight saves.
                since = state.get('since') if state_valid else cache['updated_at'] + offset - 5
                trace.fields['since'] = since if type(since) in (int, float) else None
                if mode != 'saved_since':
                    trace.fields['full_reason'] = 'saved_filter_unsupported'
                elif count != len(previous):
                    trace.fields['full_reason'] = 'server_count_changed'
                elif type(since) not in (int, float) or not 0 <= since <= started + offset:
                    trace.fields['full_reason'] = 'since_invalid'
                else:
                    date = datetime.fromtimestamp(since, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                    try:
                        changed = trace.run('delta_fetch', client.pages, dict(PARAMS, MinDateLastSaved=date))
                        changed = trace.run('delta_validate', minimal, changed)
                        trace.fields['changed_count'] = len(changed)
                        merged = trace.run('delta_merge', merge_versions, previous, changed, client, trace)
                        if merged is not None:
                            trace.fields['merged_count'] = len(merged)
                            after = trace.run('count_after', counted, client)
                            trace.fields['server_count_after'] = after
                            if after == count:
                                items = merged
                            else:
                                trace.fields['full_reason'] = 'server_count_changed_during_delta'
                    except InterruptedError:
                        raise
                    except Unavailable as error:
                        trace.fields.update(full_reason={
                            'delta_fetch': 'delta_query_failed', 'delta_validate': 'delta_invalid',
                            'delta_merge': 'version_lookup_failed', 'count_after': 'count_verify_failed'
                        }[trace.stage], fallback_error=type(error).__name__)
                        mode = 'full'

        full = items is None
        if full:
            # Emit before the expensive scan, even if it later times out or is cancelled.
            trace.emit('full_start')
            items = trace.run('full_fetch', client.pages, PARAMS)
            items = trace.run('full_validate', minimal, items)
            trace.emit('full_complete', new_count=len(items))
        rows = trace.run('candidates', lambda: [candidate(item, account) for item in items])
        client.check()
        offset = getattr(client, 'clock_offset', None)
        if previous is None and type(offset) in (int, float):
            mode = 'saved_since' if trace.run('capability_probe', supported, client, len(items), trace) else 'full'
        rewrite = previous is None or not trace.run('compare', same, previous, items)
        if rewrite:
            cache = {'version': 1, 'revision': account['_revision'], 'updated_at': started,
                     'generation': uuid.uuid4().hex, 'items': items}
            trace.run('snapshot_write', write_json, path, cache)
        # Unchanged titles are never rewritten just to advance the small cursor.
        if type(offset) in (int, float):
            since = started + offset - 5
        else:
            mode, since = 'full', None
        trace.run('cursor_write', write_json, state_path,
                  {'version': 1, 'snapshot': binding(cache), 'checked_at': time.time(), 'since': since, 'mode': mode})
        trace.emit('check_complete', refresh='full' if full else 'incremental', new_count=len(items),
                   snapshot_written=rewrite, next_mode=mode)
        return rows
    except Exception as error:
        trace.emit('check_cancelled' if isinstance(error, InterruptedError) else 'check_failed',
                   error_type=type(error).__name__)
        raise
