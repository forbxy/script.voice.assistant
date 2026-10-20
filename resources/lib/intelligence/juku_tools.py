"""Optional model tools for Juku; only safe catalogue metadata leaves the host."""
from . import juku


def require_available(tools):
    # Tool lists are stable during one agent turn, but installs can change after
    # they were advertised. Recheck at execution without starting the daemon.
    if not juku.available(tools):
        raise ValueError('巨库当前不可用。')


def search_juku(tools, query, kind='all', year=None, source='auto', limit=10, cancelled=lambda: False):
    require_available(tools)
    if not query.strip():
        raise ValueError('请提供影视片名。')
    request = {'title': query.strip(), 'literal_title': query.strip(), 'year': year,
               'kind': kind, 'resume': True, 'allow_fuzzy': False}
    client = juku.Client(cancelled)
    rows, complete, origin = juku.search(client, request, source)
    client.check()
    from .media_match import rank
    ranked, _ = rank(request['title'], juku.candidates(rows, origin), year, cancelled, limit=None)
    matches = juku.remember(tools, [candidate['row'] for candidate in ranked[:limit]], origin, request)
    tools.latest_matches = matches
    # Keep IDs needed for local selection in tools.media, not drive passwords,
    # share URLs, library identifiers or full private API responses in the model.
    public = [dict(kind='juku', id=row['id'], title=row['title'], year=row['year'],
                   media_type=row['media_type'], source=origin, score=round(candidate['score'], 4))
              for row, candidate in zip(matches, ranked)]
    return {'matches': public, 'source': origin, 'complete': complete and len(ranked) <= limit,
            'returned': len(public), 'truncated': not complete or len(ranked) > limit,
            'note': ('查询不完整或仅返回部分候选，可缩小片名或指定年份；不能据此断言巨库没有影片。'
                     if not complete or len(ranked) > limit else
                     '未找到匹配片名。' if not matches else '查询未执行播放；请按用户意愿选择候选后调用play_juku。')}


def play_juku(tools, id, resume=True, cancelled=lambda: False):
    require_available(tools)
    remembered = tools.media.get(('juku', id))
    if remembered is None:
        raise ValueError('这个 ID 不在巨库查询结果中，请先使用 search_juku 查找影视。')
    # Per-call playback intent overrides the search default, without changing a
    # stored choice that another turn can still select.
    item = dict(remembered, _juku_request=dict(remembered['_juku_request'], resume=resume))
    from .juku_playback import open_item
    return open_item(tools, item, juku.Client(cancelled), cancelled, lambda value: None)
