"""Read-only Kodi exact/fuzzy candidates; ordering and playback live elsewhere."""
from .command_request import normalize
from .media_results import SearchResult


def artwork(tools, item, cancelled):
    """Fetch art only for visible choices; the whole-library index stays small."""
    kind = item['kind']
    if kind not in ('movie', 'tvshow'):
        return
    if cancelled():
        raise InterruptedError('已取消查找。')
    name = 'Movie' if kind == 'movie' else 'TVShow'
    try:
        value = tools.rpc('VideoLibrary.Get' + name + 'Details', {
            kind + 'id': item['id'], 'properties': ['title', 'thumbnail', 'art']}).get(kind + 'details') or {}
    except InterruptedError:
        raise
    except (ValueError, OSError, RuntimeError):
        return
    if cancelled():
        raise InterruptedError('已取消查找。')
    if value.get(kind + 'id') == item['id'] and normalize(value.get('title', '')) == normalize(item['title']):
        item.update({key: value[key] for key in ('thumbnail', 'art') if key in value})


def title_filter(title, year):
    query = {'field': 'title', 'operator': 'is', 'value': title}
    if year is not None:
        query = {'and': [query, {'field': 'year', 'operator': 'is', 'value': str(year)}]}
    return query


def exact_movies(tools, request, cancelled):
    title = request['literal_title']
    value = tools.rpc('VideoLibrary.GetMovies', {
        'properties': ['title', 'year', 'thumbnail', 'art', 'playcount', 'lastplayed', 'resume'],
        'filter': title_filter(title, request['year']), 'limits': {'start': 0, 'end': 12},
        'sort': {'method': 'title', 'order': 'ascending'}})
    if cancelled():
        raise InterruptedError('已取消。')
    rows = [row for row in value.get('movies') or [] if normalize(row.get('title', '')) == normalize(title)
            and (request['year'] is None or row.get('year') == request['year'])]
    items = [dict(kind='movie', id=row['movieid'], **{key: row[key] for key in
             ('title', 'year', 'thumbnail', 'art', 'playcount', 'lastplayed', 'resume') if key in row}) for row in rows]
    return SearchResult(rows=[{'row': item, 'score': 1.} for item in items], movie_exact=True,
                        exact=bool(items), automatic=len(items) == 1 and value.get('limits', {}).get('total', len(items)) == 1)


def search(tools, request, cancelled, progress):
    from .media_catalog import catalogue
    from .media_match import rank, diagnostics
    try:
        if request['kind'] not in ('tvshow', 'channel'):
            progress('正在查找电影…')
            exact = exact_movies(tools, request, cancelled)
            if exact.rows:
                return exact
        progress('正在比较片名和读音…')
        rows, automatic = [], False
        if request['kind'] in ('all', 'tvshow'):
            value = tools.rpc('VideoLibrary.GetTVShows', {'properties': ['title', 'year'],
                'filter': title_filter(request['title'], request['year']), 'limits': {'start': 0, 'end': 12}})
            exact = [item for item in value.get('tvshows', [])
                     if normalize(item.get('title', '')) == normalize(request['title'])
                     and (request['year'] is None or item.get('year') == request['year'])]
            rows = [dict(row={'kind': 'tvshow', 'id': item['tvshowid'], 'title': item['title'],
                              'year': item.get('year', 0)}, score=1.) for item in exact]
            automatic = len(rows) == 1 and value.get('limits', {}).get('total', len(rows)) == 1
        if not rows:
            if not request.get('allow_fuzzy', True):
                return SearchResult()
            rows, automatic = rank(request['title'], catalogue(tools, request['kind'], cancelled),
                                   request['year'], cancelled)
        if cancelled():
            raise InterruptedError('已取消查找。')
        matching = diagnostics(request['title'], rows, automatic)
        return SearchResult(rows=rows, automatic=automatic, exact=any(row['score'] == 1. for row in rows), matching=matching)
    except InterruptedError:
        raise
    except (ValueError, OSError, RuntimeError):
        return SearchResult(state='unavailable', complete=False)
