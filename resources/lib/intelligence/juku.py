"""Optional Juku catalogue fallback. Only returned IDs become selectable items."""
from pathlib import Path
import time

ADDON = 'plugin.video.juku'
MAX_PAGES = 5


class Unavailable(ValueError):
    """An incomplete/failed search must not be reported as an empty catalogue."""


def available(tools):
    if not getattr(tools, 'media_search_juku', True):
        return False
    # Avoid HTTP imports, daemon traffic and Kodi errors on boxes without Juku.
    if not (Path(__file__).resolve().parents[4] / ADDON / 'addon.xml').is_file():
        return False
    try:
        addon = tools.rpc('Addons.GetAddonDetails', {'addonid': ADDON,
                                                   'properties': ['enabled']}).get('addon', {})
        return addon.get('addonid') == ADDON and addon.get('enabled') is True
    except (ValueError, OSError, RuntimeError):
        return False


class Client:
    """Bounded, read-only loopback API; never use the global proxy or push queue."""
    def __init__(self, cancelled=lambda: False):
        from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.cancelled = cancelled
        self.deadline = time.monotonic() + 25

    def check(self):
        if self.cancelled():
            raise InterruptedError('已取消查找。')
        if time.monotonic() >= self.deadline:
            raise Unavailable('巨库查询超时。')

    def get(self, path, params=None, raw=False):
        from http.client import HTTPException
        import json
        from urllib.parse import urlencode
        self.check()
        url = 'http://127.0.0.1:17654' + path
        if params:
            url += '?' + urlencode(params)
        try:
            with self.opener.open(url, timeout=min(6, self.deadline - time.monotonic())) as response:
                data = response.read(2 * 1024 * 1024 + 1)
            self.check()
            if len(data) > 2 * 1024 * 1024:
                raise Unavailable('巨库返回数据过大。')
            value = json.loads(data)
            if not isinstance(value, dict) or value.get('success') is False:
                raise Unavailable('巨库未返回有效查询结果。')
            if not raw and (value.get('success') is not True or 'data' not in value):
                raise Unavailable('巨库查询接口不可用。')
            return value if raw else value['data']
        except InterruptedError:
            raise
        except (OSError, ValueError, HTTPException) as error:
            # Do not expose HTTP bodies, private share URLs or drive credentials.
            raise Unavailable('巨库查询暂时不可用。') from error

    def pages(self, path, params, private=False):
        rows = []
        for page in range(1, MAX_PAGES + 1):
            data = self.get(path, dict(params, page=page))
            if not isinstance(data, dict) or not isinstance(data.get('items'), (list, type(None))):
                raise Unavailable('巨库列表格式无效。')
            items = data.get('items') or []
            if not all(isinstance(row, dict) for row in items):
                raise Unavailable('巨库项目格式无效。')
            rows.extend(items)
            pagination = data if private else data.get('pagination', {})
            if not isinstance(pagination, dict):
                raise Unavailable('巨库分页格式无效。')
            total = pagination.get('total')
            if type(total) is not int or total < len(rows):
                raise Unavailable('巨库未返回有效分页信息。')
            if len(rows) == total:
                return rows, True
            if not items:
                raise Unavailable('巨库分页不完整。')
        return rows, False


def escaped(identifier):
    from urllib.parse import quote
    return quote(str(identifier), safe='')


def media_type(row):
    is_tv = row.get('isTv')
    if type(is_tv) is bool:
        return 'tvshow' if is_tv else 'movie'
    value = str(row.get('typeId') or row.get('type_id') or '')
    return 'movie' if value == '1' else 'tvshow' if value in ('2', '3', '4', '7') else ''


def year(row):
    try:
        return int(row.get('year') or 0)
    except (ValueError, TypeError):
        return 0


def accept(row, request):
    return (isinstance(row, dict) and isinstance(row.get('title'), str) and bool(row['title'].strip())
            and type(row.get('id')) in (str, int) and 0 < len(str(row['id'])) <= 200
            and (request['year'] is None or year(row) == request['year'])
            and (request['kind'] == 'all' or media_type(row) == request['kind']))


def private_rows(client, request):
    libraries = client.get('/api/private/libraries')
    if not isinstance(libraries, list) or not all(isinstance(row, dict) and row.get('id') for row in libraries):
        raise Unavailable('巨库私有库列表无效。')
    rows, complete = [], len(libraries) <= 16
    for library in libraries[:16]:
        found, done = client.pages('/api/private/libraries/' + escaped(library['id']) + '/movies',
            {'keyword': request['title'], 'page_size': 100}, private=True)
        complete = complete and done
        rows.extend(dict(row, _library=str(library['id'])) for row in found if accept(row, request))
    return rows, complete


def online_rows(client, request):
    rows, complete = client.pages('/api/mv/videos/search', {'q': request['title'], 'pageSize': 20})
    # Older results may omit both isTv and typeId.
    # Verify missing types using bounded detail reads, not title guesses.
    if request['kind'] != 'all':
        verified, reads = [], 0
        for row in rows:
            if not media_type(row):
                if reads >= 12:
                    complete = False
                    continue
                if 'id' not in row:
                    raise Unavailable('巨库缺少项目编号。')
                row = client.get('/api/mv/videos/' + escaped(row['id']))
                reads += 1
            verified.append(row)
        rows = verified
    return [row for row in rows if accept(row, request)], complete


def remember(tools, rows, source, request):
    result, seen = [], set()
    next_id = max((key[1] for key in tools.media if key[0] == 'juku'), default=0) + 1
    for row in rows:
        key = (str(row['id']), row.get('_library', ''))
        if key in seen:
            continue
        seen.add(key)
        item = {'kind': 'juku', 'id': next_id, 'title': row['title'], 'year': year(row),
                '_juku_source': source, '_juku_id': str(row['id']),
                '_juku_library': row.get('_library', ''), '_juku_request': dict(request),
                'thumbnail': '',
                'media_type': media_type(row)}
        item['thumbnail'] = artwork(row.get('cover_url') or row.get('pic'))
        # Keep only catalogue metadata, never links, passwords or API responses.
        tools.media[('juku', next_id)] = item
        result.append(item)
        next_id += 1
    return result


def candidates(rows, source):
    """Project catalogue rows into the same title-scoring input as Kodi items."""
    result, seen = [], set()
    for row in rows:
        identifier, library = str(row['id']), str(row.get('_library', ''))
        if (identifier, library) in seen:
            continue
        seen.add((identifier, library))
        # Server score/autoPlay/matchedText are deliberately not scoring input:
        # aliases can refer to another film. Rank the actual selectable title.
        result.append({'kind': 'juku', 'id': identifier, 'title': row['title'], 'year': year(row),
                       'isTv': row.get('isTv'),
                       'typeId': row.get('typeId') or row.get('type_id'),
                       'pic': artwork(row.get('cover_url') or row.get('pic')),
                       '_library': library, '_juku_source': source})
    return result


def exact_rows(rows, request):
    from .media_match import normalize
    return [row for row in rows if normalize(row['title']) == normalize(request['title'])]


def search(client, request, source='auto'):
    """Native AI tools may explicitly select a source; auto stays private-first."""
    if source == 'online':
        rows, complete = online_rows(client, request)
        return rows, complete, 'online'
    try:
        rows, complete = private_rows(client, request)
    except Unavailable:
        if source == 'private':
            raise
        rows, complete = [], False
    if rows or source == 'private':
        return rows, complete, 'private'
    online, done = online_rows(client, request)
    return online, complete and done, 'online'


def artwork(value):
    from urllib.parse import urlencode
    if not isinstance(value, str):
        return ''
    if value.startswith(('/api/', '/posters/')):
        return 'http://127.0.0.1:17654' + value
    if value.startswith('/'):
        return 'http://127.0.0.1:17654/api/image/proxy?' + urlencode({'url': 'https://image.tmdb.org/t/p/original' + value})
    return value if value.startswith(('https://', 'http://')) else ''



class Source:
    """One bounded read-only catalogue session, with no source or playback policy."""
    def __init__(self, tools, cancelled):
        self.enabled = available(tools)
        self.cancelled = cancelled
        self.client = None

    def search(self, source, request):
        from .media_results import SearchResult
        if not self.enabled:
            return SearchResult(source=source, state='skipped')
        if self.client is None:
            self.client = Client(self.cancelled)
        try:
            rows, complete = (private_rows if source == 'private' else online_rows)(self.client, request)
            return SearchResult(source=source, rows=rows, exact=bool(exact_rows(rows, request)),
                                complete=complete, client=self.client)
        except Unavailable:
            return SearchResult(source=source, state='unavailable', complete=False, client=self.client)
