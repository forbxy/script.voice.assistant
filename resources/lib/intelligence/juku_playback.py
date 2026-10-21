"""Hand verified titles to Juku's own parsing routes, after releasing our dialog."""
import re
from urllib.parse import urlencode, unquote

from .juku import Unavailable, escaped, media_type, artwork, year, accept, ADDON

PROVIDERS = {'1': 'ALIYUN', '2': 'QUARK', '3': 'UC', '4': 'BAIDU', '5': 'XUNLEI',
             '6': 'CLOUD123', '7': 'CLOUD115', '8': 'CHINA_MOBILE', '9': 'CHINA_TELECOM', '11': 'GUANGYA'}
SHARES = dict(zip(PROVIDERS, ('https://www.alipan.com/s/', 'https://pan.quark.cn/s/',
    'https://drive.uc.cn/s/', 'https://pan.baidu.com/s/', 'https://pan.xunlei.com/s/',
    'https://www.123pan.com/s/', 'https://115.com/s/', 'https://caiyun.139.com/m/i?',
    'https://cloud.189.cn/t/', 'https://www.guangyapan.com/s/')))


def builtin(params, folder, resume):
    # Encode every parameter exactly once; quotes/commas cannot escape the URL.
    url = 'plugin://plugin.video.juku/?' + urlencode({k: v for k, v in params.items() if v is not None})
    if folder:
        return 'ActivateWindow(Videos,"' + url + '",return)', True
    return 'PlayMedia("' + url + '",' + ('resume' if resume else 'noresume') + ')', False


def route(client, item, actual):
    tv = media_type(actual) == 'tvshow'
    resume = item['_juku_request']['resume']
    params = {'vod_title': actual['title'], 'vod_id': actual.get('vod_id') or actual['id'],
              'type_id': '2' if tv else '1', 'is_tv': '1' if tv else '0',
              'pic': artwork(actual.get('cover_url') or actual.get('pic')),
              'fanart': artwork(actual.get('backdrop_url') or actual.get('backdropPath'))}
    if item['_juku_source'] == 'private':
        params['movie_id'] = actual['id']
        links = client.get('/api/private/movies/' + escaped(actual['id']) + '/links')
        if not isinstance(links, list) or not all(isinstance(row, dict) for row in links):
            raise Unavailable('巨库播放源格式无效。')
        if not links:
            raise Unavailable('巨库没有可用播放源。')
        # Multiple episodes/sources belong in Juku's resource chooser. Never play
        # an arbitrary episode or direct a folder share to the single-file route.
        if len(links) != 1 or tv:
            return builtin(dict(params, action='private_movie_links', title=actual['title']), True, resume)
        link = links[0]
        kind = link.get('link_type') or 'pan'
        params.update({k: link[k] for k in ('provider', 'url', 'password', 'remark', 'file_id',
                      'file_name', 'file_size', 'season_number', 'episode_number', 'episode_title') if k in link})
        params.update(action='play_private_link', link_id=link.get('id'), link_type=kind)
        if resume:
            status = client.get('/api/playback/status', {'item_id': actual['id']}, raw=True)
            position = status.get('resume_seconds', 0)
            if type(position) in (int, float) and 0 < position < 864000:
                params['resume_time'] = position
        if kind == 'pan':
            params.update(action='list_files', code=link.get('password', ''), pan_name=link.get('provider', ''))
            return share_route(client, params, resume)
        return builtin(params, False, resume)

    fallback = dict(params, action='detail', id=actual['id'])
    if tv and item['_juku_request'].get('season') is not None:
        # Juku resolves its own season/episode resources. Do not autoplay a
        # lone share file whose season we cannot establish.
        return builtin(fallback, True, resume)
    # Use Juku's configured drive order, but only drives the user has logged in.
    # With no usable drive (or only HLS/download sources), Juku presents its
    # own source picker; no login, cloud-save or download is triggered by us.
    pans = actual.get('panList') or {}
    if not isinstance(pans, dict) or not pans:
        return builtin(fallback, True, resume)
    status = client.get('/api/pan/status', raw=True)
    order = client.get('/api/pan/order', raw=True).get('order')
    if not isinstance(order, list):
        raise Unavailable('巨库网盘顺序无效。')
    for provider in order:
        key = str(provider)
        login = status.get(PROVIDERS.get(key, key)) or {}
        links = pans.get(key) or []
        if not isinstance(links, list):
            raise Unavailable('巨库分享链接格式无效。')
        if not isinstance(login, dict) or not login.get('logged_in') or not links:
            continue
        if len(links) > 1:
            return builtin(dict(params, action='pan_sources', id=actual['id'], pan_name=key), True, resume)
        link = links[0]
        if not isinstance(link, dict):
            raise Unavailable('巨库分享链接格式无效。')
        params.update(action='list_files', url=link.get('url', ''), code=link.get('code', ''),
                      pan_name=key, link_id=link.get('id'), link_idx=1)
        return share_route(client, params, resume)
    return builtin(fallback, True, resume)


def share_route(client, params, resume):
    """Only an explicit single file can autoplay; preserve all metadata otherwise."""
    url = params.get('url') or ''
    if not isinstance(url, str) or not url:
        raise Unavailable('巨库未提供分享链接。')
    key = str(params.get('pan_name') or '')
    if not url.startswith(('https://', 'http://')):
        prefix = SHARES.get(key)
        if not prefix:
            key = next((k for k, v in PROVIDERS.items() if v == key), key)
            prefix = SHARES.get(key)
        if prefix:
            url = prefix + (url[4:] if key == '8' and url.startswith('cid=') else url)
    params['url'] = url
    fid = re.search(r'(?:[#?&/](?:fid|file_id|target_id)=([^&#\s]+))', url, re.I)
    file_id = params.get('file_id') or (unquote(fid[1]) if fid else '')
    if not file_id:
        data = client.get('/api/list', {'share_url': url, 'code': params.get('code', ''),
                                      'page': 1, 'page_size': 50}, raw=True)
        files = data.get('files') or []
        if not isinstance(files, list) or not all(isinstance(row, dict) for row in files):
            raise Unavailable('巨库文件列表格式无效。')
        # A first page with one row is insufficient if pagination says more.
        total = data.get('total', len(files))
        if len(files) == 1 and total == 1 and isinstance(files[0], dict):
            row = files[0]
            if not row.get('is_dir') and not row.get('is_folder'):
                file_id = row.get('file_id')
                params.update(file_name=row.get('file_name', ''), file_size=row.get('file_size', 0))
    if not file_id:
        return builtin(dict(params, action='list_files'), True, resume)
    params.update(action='pan_play', file_id=file_id, single_file='1', from_share='1')
    return builtin(params, False, resume)


def open_item(tools, item, client, cancelled, progress):
    """Only this shared path validates a result and schedules its Juku route."""
    if item.get('_juku_history'):
        from .juku_resume import open_item as resume_item
        return resume_item(tools, item, client, cancelled, progress)
    if tools.profile is not None and item['_juku_source'] == 'online' and item['_juku_request']['resume']:
        from .juku_history import Store
        matches = [row for row in Store(tools.profile).records().values()
                   if row.get('source') == 'online' and row.get('catalogue_id') == item['_juku_id']
                   and row.get('title') == item['title'] and not row.get('played')
                   and (item['_juku_request'].get('season') is None or
                        row.get('season') == item['_juku_request']['season'])]
        if matches:
            from .juku_resume import open_item as resume_item
            record = max(matches, key=lambda row: row['updated_at'])
            return resume_item(tools, dict(item, _juku_history=record['key']), client, cancelled, progress)
    from .command_request import normalize
    # Re-read authoritative metadata after a persisted choice or before autoplay.
    if item['_juku_source'] == 'private':
        rows, complete = client.pages('/api/private/libraries/' + escaped(item['_juku_library']) + '/movies',
            {'keyword': item['title'], 'page_size': 100}, private=True)
        actual = next((row for row in rows if str(row.get('id')) == item['_juku_id']), {})
    else:
        actual = client.get('/api/mv/videos/' + escaped(item['_juku_id']))
    if (not isinstance(actual, dict) or str(actual.get('id')) != item['_juku_id']
            or normalize(actual.get('title', '')) != normalize(item['title'])
            or year(actual) != item['year'] or not accept(actual, item['_juku_request'])):
        raise ValueError('巨库项目已变化，请重新查询后再播放。')
    if tools.permissions is not None and not tools.permissions.allows('play_media'):
        raise ValueError('当前权限不允许播放媒体。')
    progress('正在交给巨库解析《' + item['title'] + '》…')
    command, folder = route(client, item, actual)
    client.check()
    if cancelled():
        raise InterruptedError('已取消播放。')
    tools.latest_matches = []
    pending = {}
    if tools.defer_ui:
        pending = tools._queue_playback(command, ADDON)
    else:
        tools.builtin(command)
        tools.acted = True
    season = item['_juku_request'].get('season')
    message = ('正在打开《' + item['title'] + '》的巨库资源页面，请选择' +
               ('第{}季的播放源或剧集。'.format(season) if season is not None else '播放源或剧集。') if folder else
               '正在通过巨库打开《' + item['title'] + '》。')
    return dict(pending, id=item['id'], kind='juku', title=item['title'], source=item['_juku_source'],
                destination='resource_picker' if folder else 'playback',
                note=message)
