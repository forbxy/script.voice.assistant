"""Juku recent history and exact-resource replay; secrets stay in private userdata."""
import math

from . import juku
from .juku_history import Store, seconds


def stamp(value):
    if type(value) in (int, float) and math.isfinite(value):
        return float(value) if 1000000000 < value < 100000000000 else None
    from .recent_playback import timestamp
    return timestamp(value)


def collect(tools, kind, cancelled):
    if tools.profile is None:
        return [], True
    store = Store(tools.profile)
    rows = [row for row in store.records().values() if row.get('source') == 'online']
    complete = True
    client = juku.Client(cancelled)
    try:
        config = client.get('/api/private/emby/config')
        user = str(config.get('primary_user') or config.get('username') or 'admin') if isinstance(config, dict) else 'admin'
        data = client.get('/api/playback/history', {'user_id': user, 'limit': 30}, raw=True)
        if not isinstance(data.get('items'), list) or len(data['items']) > 30:
            raise juku.Unavailable('巨库观看记录格式无效。')
        for value in data['items']:
            if not isinstance(value, dict):raise juku.Unavailable('巨库观看记录无效。')
            movie, item = str(value.get('movie_id') or ''), str(value.get('item_id') or '')
            if not movie.startswith('mov_') or not item.startswith(('mov_', 'ep_mov_')):
                continue
            updated = stamp(value.get('updated_at'))
            title = value.get('title')
            series = value.get('is_series') is True
            season, episode = value.get('season_number'), value.get('episode_number')
            if updated is None or not isinstance(title, str) or not title.strip():continue
            if series and (type(season) is not int or season < 0 or type(episode) is not int or episode < 1):
                complete = False
                continue
            import hashlib
            handle = hashlib.sha256(('private:' + user + ':' + item).encode()).hexdigest()
            rows.append({'key': handle, 'source': 'private', 'catalogue_id': movie, 'item_id': item,
                'title': title[:512], 'year': juku.year(value), 'media_type': 'episode' if series else 'movie',
                'season': season if series else None, 'episode': episode if series else None,
                'episode_title': str(value.get('item_title') or '')[:512], 'link_id': str(value.get('link_id') or ''),
                'position': seconds(value.get('resume_seconds')), 'total': seconds(value.get('total_seconds')),
                'played': value.get('played') is True, 'updated_at': updated, 'user_id': user})
    except InterruptedError:
        raise
    except (ValueError, OSError, RuntimeError):
        complete = False
    filtered = []
    for row in rows:
        if cancelled():raise InterruptedError('已取消巨库记录查询。')
        category = 'tvshow' if row.get('media_type') == 'episode' else 'movie'
        if (kind == 'all' or kind == category) and stamp(row.get('updated_at')) is not None:
            filtered.append(row)
    return filtered, complete


def remember(tools, record):
    """The model/session receives an opaque history handle, never replay parameters."""
    Store(tools.profile).save(record)
    kind = 'tvshow' if record['media_type'] == 'episode' else 'movie'
    request = dict(title=record['title'], kind=kind, year=None, resume=True)
    row = {'id': record['catalogue_id'] or record['key'], 'title': record['title'],
           'year': record.get('year', 0), 'isTv': kind == 'tvshow'}
    item = juku.remember(tools, [row], record['source'], request)[0]
    item.update(_juku_history=record['key'], media_type=record['media_type'],
                lastplayed=record['updated_at'], playcount=int(record.get('played') is True),
                resume={'position': record['position'], 'total': record['total']})
    if kind == 'tvshow':
        item.update(showtitle=record['title'], season=record['season'], episode=record['episode'])
    return item


def saved(tools, item):
    value = Store(tools.profile).get(item.get('_juku_history'))
    if (not isinstance(value, dict) or value.get('title') != item['title']
            or value.get('source') != item['_juku_source'] or (value.get('catalogue_id', '') != item['_juku_id']
            and value.get('key') != item['_juku_id'])):
        raise juku.Unavailable('巨库播放记录已变化，请重新查询。')
    return value


def private_route(client, record, resume):
    """Re-read links; choose one confirmed episode/version, never an arbitrary file."""
    from .juku_playback import builtin, share_route
    movie = record['catalogue_id']
    config = client.get('/api/private/emby/config')
    user = str(config.get('primary_user') or config.get('username') or 'admin') if isinstance(config, dict) else 'admin'
    if user != record.get('user_id'):
        raise juku.Unavailable('巨库当前用户已变化，请重新查询观看记录。')
    # Verify the historical title/ID against the current configured libraries.
    request = dict(title=record['title'], kind='tvshow' if record['media_type'] == 'episode' else 'movie', year=None)
    rows, complete = juku.private_rows(client, request)
    actual = next((row for row in rows if str(row.get('id')) == movie and row.get('title') == record['title']), None)
    if actual is None:
        raise juku.Unavailable('巨库历史对应的私有库项目已不可用，请重新查询。')
    links = client.get('/api/private/movies/' + juku.escaped(movie) + '/links')
    if not isinstance(links, list) or not all(isinstance(row, dict) for row in links):
        raise juku.Unavailable('巨库历史播放源无效。')
    is_tv = record['media_type'] == 'episode'
    target = (record.get('season'), record.get('episode'))
    position = record['position'] if resume else 0
    if is_tv:
        valid = [row for row in links if type(row.get('season_number')) is int and row['season_number'] >= 0
                 and type(row.get('episode_number')) is int and row['episode_number'] > 0]
        if resume and record.get('played'):
            following = sorted({(row['season_number'], row['episode_number']) for row in valid
                                if (row['season_number'], row['episode_number']) > target})
            if not following:return None, False, '巨库这部电视剧没有后续可确定的剧集，请选择其他内容。'
            for candidate in following[:30]:
                item = 'ep_{}_{}_{}'.format(movie, *candidate)
                state = client.get('/api/playback/status', {'item_id': item, 'user_id': user}, raw=True)
                if not isinstance(state, dict) or type(state.get('played')) is not bool:
                    raise juku.Unavailable('无法确认巨库后续剧集的观看状态，请选择剧集。')
                if not state['played']:
                    target, position = candidate, seconds(state.get('resume_seconds'))
                    break
            else:
                return None, False, '巨库后续剧集均已看完或数量较多，请选择接下来要看的剧集。'
        links = [row for row in valid if (row['season_number'], row['episode_number']) == target]
    params = dict(vod_title=record['title'], vod_id=movie, movie_id=movie,
                  type_id='2' if is_tv else '1', is_tv='1' if is_tv else '0')
    if is_tv:params.update(season_number=target[0], episode_number=target[1])
    if not links:raise juku.Unavailable('巨库历史对应的播放源或剧集已不存在。')
    preferred = [row for row in links if str(row.get('id')) == record.get('link_id')]
    if preferred:links = preferred
    if len(links) != 1:
        command, folder = builtin(dict(params, action='private_movie_links', title=record['title']), True, resume)
        return command, folder, '请在巨库选择对应剧集或播放版本。'
    link = links[0]
    params.update({name: link[name] for name in ('provider', 'url', 'password', 'remark', 'file_id', 'file_name',
        'file_size', 'season_number', 'episode_number', 'episode_title') if name in link})
    params.update(action='play_private_link', link_id=link.get('id'), link_type=link.get('link_type') or 'pan', resume_time=position)
    if params['link_type'] == 'pan':
        params.update(action='list_files', code=params.get('password', ''), pan_name=params.get('provider', ''))
        command, folder = share_route(client, params, resume)
    else:
        command, folder = builtin(params, False, resume)
    return command, folder, '正在通过巨库恢复《' + record['title'] + '》。'


def open_item(tools, item, client, cancelled, progress):
    from .juku_playback import builtin
    if not juku.available(tools):raise juku.Unavailable('巨库当前不可用。')
    if tools.permissions is not None and not tools.permissions.allows('play_media'):
        raise ValueError('当前权限不允许播放媒体。')
    record = saved(tools, item)
    resume = item['_juku_request']['resume']
    progress('正在恢复巨库最近观看内容…')
    if record['source'] == 'private':
        command, folder, note = private_route(client, record, resume)
    else:
        from .juku_history import route
        from urllib.parse import urlencode
        if resume and record.get('played') and record['media_type'] == 'episode':
            following = record.get('next')
            if (not isinstance(following, dict) or following.get('catalogue_id') != record['catalogue_id']
                    or following.get('title') != record['title'] or following.get('media_type') != 'episode'
                    or (following.get('season', -1), following.get('episode', -1)) <= (record['season'], record['episode'])):
                if not record['catalogue_id']:
                    return dict(note='没有已确认的下一集，请在巨库选择剧集。', id=item['id'])
                if cancelled():raise InterruptedError('已取消巨库续播。')
                command, folder = builtin(dict(action='detail', id=record['catalogue_id']), True, True)
                pending = tools._queue_playback(command, juku.ADDON) if tools.defer_ui else {}
                if not tools.defer_ui:tools.builtin(command);tools.acted = True
                return dict(pending, note='请在巨库选择接下来要看的剧集。', id=item['id'])
            record = following
        params = route('plugin://plugin.video.juku/?' + urlencode(record.get('route') or {}))
        if params is None:raise juku.Unavailable('巨库原播放资源信息不完整，请重新查找影片。')
        params.update(resume_time=record['position'] if resume else 0)
        command, folder = builtin(params, False, resume)
        note = '正在通过巨库恢复《' + record['title'] + '》。'
    if cancelled():raise InterruptedError('已取消巨库续播。')
    tools.latest_matches = []
    pending = {}
    if command:
        if tools.defer_ui:pending = tools._queue_playback(command, juku.ADDON)
        else:tools.builtin(command); tools.acted = True
    return dict(pending, note=note, kind='juku', id=item['id'],
                destination='resource_picker' if folder else 'playback' if command else 'no_next_episode')
