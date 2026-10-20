"""Model-facing Emby tools: only local candidate handles, never credentials."""
from . import emby


def require_available(tools):
    if not emby.available(tools):
        raise ValueError('Emby 当前不可用。')


def search_emby(tools, query, kind='all', year=None, limit=10, cancelled=lambda: False):
    require_available(tools)
    if not query.strip():
        raise ValueError('请提供影视片名。')
    request = dict(title=query.strip(), kind=kind, year=year, resume=True)
    source = emby.Source(tools, cancelled)
    result = source.search(request)
    from .media_match import rank
    ranked, _ = rank(request['title'], result.rows, year, cancelled, limit=None)
    matches = emby.remember(tools, [row['row'] for row in ranked[:limit]], request)
    tools.latest_matches = matches
    return {'matches': [dict(kind='emby', id=row['id'], title=row['title'], year=row['year'],
                            media_type=row['media_type'], server_name=row['server_name'], score=round(match['score'], 4))
                        for row, match in zip(matches, ranked)],
            'complete': result.complete and len(ranked) <= limit,
            'note': '只查询未播放；候选 ID 只能用于 play_emby 或 get_emby_episodes。'}


def selected(tools, id):
    require_available(tools)
    item = tools.media.get(('emby', id))
    if item is None:
        raise ValueError('这个 ID 不在 Emby 查询结果中，请先查找影视。')
    return item


def get_emby_episodes(tools, id, season=None, offset=0, limit=20, cancelled=lambda: False):
    show = selected(tools, id)
    if show['media_type'] != 'tvshow':
        raise ValueError('请先选择 Emby 电视剧。')
    from .emby_playback import episodes, verify
    from .watch_progress import continuation
    client = emby.Client(emby.account_for(tools.profile, show), cancelled)
    actual = verify(client, show)
    rows = episodes(client, show, actual)
    result = continuation(rows)
    filtered = [row for row in rows if season is None or row['season'] == season]
    matches = emby.remember(tools, filtered[offset:offset+limit], show['_emby_request'])
    public = lambda row: {key: row[key] for key in ('kind', 'id', 'title', 'year', 'season', 'episode',
                                                  'showtitle', 'playcount', 'lastplayed', 'resume')}
    response = {'matches': [public(row) for row in matches], 'total': len(filtered),
                'next_offset': offset+len(matches) if offset+len(matches) < len(filtered) else None,
                'progress_status': result['status'], 'note': result['note']}
    if result.get('recommended'):
        recommended = emby.remember(tools, [result['recommended']], show['_emby_request'])[0]
        response['recommended'] = public(recommended)
    tools.latest_matches = matches
    return response


def play_emby(tools, id, resume=True, cancelled=lambda: False):
    item = selected(tools, id)
    from .emby_playback import open_item
    return open_item(tools, dict(item, _emby_request=dict(item['_emby_request'], resume=resume)), cancelled)
