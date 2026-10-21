"""Optional Emby dynamic titles, projected into the shared media scorer."""
import re
import time

from .emby_client import ADDON, Client, Unavailable, accounts, available, identifier
from .media_results import SearchResult

CACHE_AGE = 300


def year(item):
    value = item.get('ProductionYear', 0)
    return value if type(value) is int and 1800 <= value <= 2200 else 0


def candidate(item, account):
    media_type = {'Movie': 'movie', 'Series': 'tvshow', 'Episode': 'episode'}.get(item.get('Type'))
    if media_type is None or not isinstance(item.get('Name'), str) or not 0 < len(item['Name']) <= 512:
        raise Unavailable('Emby 影视条目无效。')
    item_id = identifier(item.get('Id'))
    return {'kind': 'emby', 'id': account['_identity'] + ':' + item_id, 'title': item['Name'],
            'year': year(item), 'media_type': media_type, '_emby_id': item_id,
            '_emby_server': account['ServerId'], '_emby_account': account['_identity'],
            'server_name': str(account.get('ServerName') or 'Emby')[:80]}


def thumbnail(item, account):
    tag = (item.get('ImageTags') or {}).get('Primary')
    # Emby can append a revision timestamp: <image hash>_<ticks>.
    if not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,128}', tag):
        return ''
    # Use Next Gen's local image proxy. Never put an AccessToken in a texture URL.
    return ('http://127.0.0.1:57342/picture/' + identifier(account['ServerId']) + '/p-'
            + identifier(item['Id']) + '-0-p-' + tag + '--poster.jpg')


def remember(tools, rows, request):
    result = []
    next_id = max((key[1] for key in tools.media if key[0] == 'emby'), default=0) + 1
    for row in rows:
        existing = next((value for key, value in tools.media.items() if key[0] == 'emby'
                         and value.get('_emby_account') == row['_emby_account']
                         and value.get('_emby_id') == row['_emby_id']), None)
        current_id = existing['id'] if existing else next_id
        if not existing:
            next_id += 1
        item = dict(row, id=current_id, _emby_request=dict(request))
        tools.media[('emby', current_id)] = item
        result.append(item)
    return result


def account_for(profile, item):
    account = next((row for row in accounts(profile) if row['_identity'] == item.get('_emby_account')
                    and row['ServerId'] == item.get('_emby_server')), None)
    if account is None:
        raise Unavailable('Emby 登录账号已变化，请重新查找影片。')
    return account


def catalogue(profile, account, client):
    from .emby_cache import catalogue as load
    return load(profile, account, client, CACHE_AGE)


class Source:
    def __init__(self, tools, cancelled):
        self.tools, self.cancelled = tools, cancelled
        self.enabled = available(tools)

    def search(self, request):
        if not self.enabled:
            return SearchResult(source='emby')
        from .emby_index import search
        rows, complete, exact, deadline = [], True, False, time.monotonic() + 30
        for account in accounts(self.tools.profile):
            client = Client(account, self.cancelled)
            client.deadline = deadline
            try:
                candidates, matched = search(self.tools.profile, account, client, request,
                    lambda: catalogue(self.tools.profile, account, client), CACHE_AGE)
                exact = exact or matched
                rows.extend(row for row in candidates
                            if (request['kind'] == 'all' or row['media_type'] == request['kind'])
                            and (request.get('year') is None or row['year'] == request['year']))
            except InterruptedError:
                raise
            except (Unavailable, OSError):
                complete = False
        return SearchResult(source='emby', rows=rows, exact=exact, complete=complete)
