"""Revalidate Emby choices and hand playback to this box's local Next Gen."""
from . import emby


def verify(client, item):
    from .media_match import normalize
    actual = client.item(item['_emby_id'])
    if (normalize(actual.get('Name', '')) != normalize(item['title']) or emby.year(actual) != item['year']
            or {'Movie': 'movie', 'Series': 'tvshow', 'Episode': 'episode'}.get(actual.get('Type')) != item['media_type']):
        raise ValueError('Emby 项目已变化，请重新查询后再播放。')
    return actual


def episodes(client, show, actual, values=None):
    if values is None:
        values = client.pages({'IncludeItemTypes': 'Episode', 'ParentId': show['_emby_id'], 'Recursive': 'true',
            'EnableImages': 'true', 'EnableUserData': 'true', 'Fields': 'ProductionYear,UserDataPlayCount,UserDataLastPlayedDate',
            'SortBy': 'ParentIndexNumber,IndexNumber', 'SortOrder': 'Ascending'}, maximum=10000)
    rows, verified_series = [], {show['_emby_id']}
    for value in values:
        if value.get('Type') != 'Episode':
            raise emby.Unavailable('Emby 返回的项目不是剧集，请重新查询。')
        try:
            series = emby.identifier(value.get('SeriesId'))
        except emby.Unavailable:
            raise emby.Unavailable('无法确认 Emby 剧集属于所选电视剧，已停止播放，请重新查询。') from None
        if series not in verified_series:
            # Emby can group copies across libraries and return episodes from
            # another SeriesId. Prove that identity from fresh series metadata,
            # once per distinct ID, rather than trusting the episode's name.
            presentation = actual.get('PresentationUniqueKey')
            if not isinstance(presentation, str) or not presentation.strip() or len(presentation) > 512:
                raise emby.Unavailable('无法确认 Emby 剧集属于所选电视剧，已停止播放，请重新查询。')
            other = client.item(series)
            if (other.get('Type') != 'Series' or str(other.get('Id')) != series
                    or other.get('PresentationUniqueKey') != presentation):
                raise emby.Unavailable('Emby 返回的剧集不属于所选电视剧，已停止播放，请重新查询。')
            verified_series.add(series)
        data = value.get('UserData') or {}
        row = emby.candidate(value, client.account)
        season, episode = value.get('ParentIndexNumber'), value.get('IndexNumber')
        row.update(episodeid=row['_emby_id'], season=season if type(season) is int and season >= 0 else -1,
                   episode=episode if type(episode) is int and episode > 0 else 0, showtitle=show['title'],
                   lastplayed=str(data.get('LastPlayedDate') or ''),
                   # Emby counts starts as PlayCount, even while Played=false.
                   # Kodi's playcount means completed views; only Played can
                   # establish completion for the shared continuation chooser.
                   playcount=int(data.get('Played') is True),
                   resume={'position': ticks(data.get('PlaybackPositionTicks')) / 10000000,
                           'total': ticks(value.get('RunTimeTicks')) / 10000000},
                   thumbnail=emby.thumbnail(value, client.account))
        rows.append(row)
    return rows


def ticks(value):
    return value if type(value) is int and 0 <= value <= 10**16 else 0


def label(item):
    value = item['title'] + ('（{}）'.format(item['year']) if item.get('year') else '')
    if item.get('showtitle'):
        value = '{} 第{}季第{}集：{}'.format(item['showtitle'], item['season'], item['episode'], item['title'])
    return value + ' · Emby · ' + item['server_name']


def open_item(tools, item, cancelled=lambda: False, progress=lambda value: None):
    if not emby.available(tools):
        raise emby.Unavailable('Emby 当前不可用。')
    if tools.permissions is not None and not tools.permissions.allows('play_media'):
        raise ValueError('当前权限不允许播放媒体。')
    account = emby.account_for(tools.profile, item)
    client = emby.Client(account, cancelled)
    actual = verify(client, item)
    resume = item['_emby_request']['resume']
    if item['media_type'] == 'tvshow':
        from .watch_progress import continuation, beginning, in_season
        progress('正在读取 Emby 剧集观看记录…')
        rows = episodes(client, item, actual)
        season = item['_emby_request'].get('season')
        result = (in_season(rows, season, resume) if season is not None else
                  continuation(rows) if resume else beginning(rows))
        if result['status'] == 'no_history':
            result = beginning(rows)
        if not result.get('recommended'):
            tools.latest_matches = []
            choices = emby.remember(tools, result.get('candidates', [])[:12], item['_emby_request'])
            tools.latest_matches = choices
            note = ('第{}季：'.format(season) if season is not None else '') + result['note']
            return {'note': note, 'choices': [dict(row, label=label(row), value='播放' + row['title']) for row in choices]}
        item = emby.remember(tools, [result['recommended']], item['_emby_request'])[0]
        actual = verify(client, item)
    client.check()
    action = {'emby_play': {key: item[key] for key in
              ('title', 'year', 'media_type', '_emby_id', '_emby_server', '_emby_account')},
              'profile': str(tools.profile), 'resume': resume, 'target': emby.ADDON}
    progress('正在通过 Emby 打开《' + item['title'] + '》…')
    if tools.defer_ui:
        pending = tools._queue_playback(action, emby.ADDON)
    else:
        pending = dispatch(action, cancelled, rpc=tools.rpc)
        tools.acted = tools.playback_started = True
    tools.latest_matches = []
    return dict(pending, kind='emby', id=item['id'], title=item['title'],
                note='正在通过 Emby 打开《' + item['title'] + '》。')


def dispatch(action, cancelled=lambda: False, rpc=None, wait=None):
    """Called after our modal releases; reload credentials and recheck the ID."""
    item = action['emby_play']
    client = emby.Client(emby.account_for(action['profile'], item), cancelled)
    actual = verify(client, item)
    if item['media_type'] not in ('movie', 'episode'):
        raise ValueError('请先选择具体的 Emby 电影或剧集。')
    client.check()
    if rpc is None:
        def rpc(method, params):
            import json
            import xbmc
            value = json.loads(xbmc.executeJSONRPC(json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params))))
            if 'error' in value:
                raise ValueError('Kodi 播放状态查询失败。')
            return value['result']
    from .emby_local import play
    options = {'wait': wait} if wait is not None else {}
    if item['media_type'] == 'episode':
        from .emby_queue import prepare
        options['queue'] = prepare(client, item, actual, rpc)
        client.check()
    return play(item, actual, action['resume'], rpc, cancelled, **options)
