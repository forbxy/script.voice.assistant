"""Read the installed Next Gen account locally; keep tokens out of media records."""
import hashlib
import json
from pathlib import Path
import re
import time

ADDON = 'plugin.service.emby-next-gen'
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,128}')
MAX_ITEMS = 50000


class Unavailable(ValueError):
    pass


def identifier(value):
    if type(value) not in (str, int) or not IDENTIFIER.fullmatch(str(value)):
        raise Unavailable('Emby 项目编号无效。')
    return str(value)


def accounts(profile):
    """Profiles belong to the current Kodi user, never a global/system account."""
    if profile is None:
        return []
    result = []
    directory = Path(profile).parent / ADDON
    for path in sorted(directory.glob('servers_*.json'))[:16]:
        try:
            if path.stat().st_size > 1024 * 1024:
                continue
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('ServerRemoved'):
                continue
            if not all(isinstance(value.get(k), str) and value[k] for k in
                       ('ServerId', 'ServerUrl', 'AccessToken', 'UserId', 'DeviceId')):
                continue
            for key in ('ServerId', 'UserId', 'DeviceId'):
                identifier(value[key])
            from urllib.parse import urlsplit
            url = urlsplit(value['ServerUrl'])
            if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
                    or url.query or url.fragment):
                continue
            # Account changes invalidate persisted choices; relogging the same
            # account also invalidates the title cache without storing its token.
            value['_identity'] = hashlib.sha256(json.dumps(
                [value[k] for k in ('ServerId', 'UserId', 'DeviceId')]).encode()).hexdigest()
            value['_revision'] = hashlib.sha256(json.dumps(
                [value[k] for k in ('ServerUrl', 'AccessToken')]).encode()).hexdigest()
            result.append(value)
        except (OSError, ValueError, TypeError):
            continue
    return result


def available(tools):
    if not getattr(tools, 'media_search_emby', True):
        return False
    # No RPC or network work for absent/unconfigured optional addons.
    if not (Path(__file__).resolve().parents[4] / ADDON / 'addon.xml').is_file() or not accounts(tools.profile):
        return False
    try:
        addon = tools.rpc('Addons.GetAddonDetails', {'addonid': ADDON,
                                                   'properties': ['enabled']}).get('addon', {})
        return addon.get('addonid') == ADDON and addon.get('enabled') is True
    except (ValueError, OSError, RuntimeError):
        return False


class Client:
    def __init__(self, account, cancelled=lambda: False):
        from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        self.account, self.cancelled = account, cancelled
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.deadline = time.monotonic() + 30
        self.clock_offset = None

    def check(self):
        if self.cancelled():
            raise InterruptedError('已取消 Emby 查询。')
        if time.monotonic() >= self.deadline:
            raise Unavailable('Emby 查询超时。')

    def request(self, path, params=None, method='GET'):
        from urllib.request import Request
        from urllib.parse import urlencode
        from http.client import HTTPException
        self.check()
        base = self.account['ServerUrl'].rstrip('/')
        if not base.endswith('/emby'):
            base += '/emby'
        url = base + '/' + path
        if params:
            url += '?' + urlencode(params)
        headers = {'X-Emby-Token': self.account['AccessToken'], 'Accept': 'application/json'}
        try:
            request = Request(url, data=b'' if method == 'POST' else None, headers=headers, method=method)
            with self.opener.open(request, timeout=min(8, max(.1, self.deadline-time.monotonic()))) as response:
                from email.utils import parsedate_to_datetime
                try:
                    date = parsedate_to_datetime(response.headers.get('Date', ''))
                    if date.tzinfo is not None:
                        self.clock_offset = date.timestamp() - time.time()
                except (AttributeError, TypeError, ValueError, OverflowError):
                    pass
                data = response.read(8 * 1024 * 1024 + 1)
            self.check()
            if len(data) > 8 * 1024 * 1024:
                raise Unavailable('Emby 返回数据过大。')
            return json.loads(data) if data else None
        except InterruptedError:
            raise
        except (OSError, ValueError, HTTPException):
            # Exception bodies/URLs can contain server names or credentials.
            raise Unavailable('Emby 查询或播放请求失败，请检查服务器连接。') from None

    @property
    def items_path(self):
        return 'Users/' + identifier(self.account['UserId']) + '/Items'

    def pages(self, params, maximum=MAX_ITEMS):
        rows, seen, total = [], set(), None
        while len(rows) < maximum:
            self.check()
            response = self.request(self.items_path, dict(params, StartIndex=len(rows), Limit=1000,
                                                        EnableTotalRecordCount='true'))
            if not isinstance(response, dict) or not isinstance(response.get('Items'), list):
                raise Unavailable('Emby 目录格式无效。')
            batch, count = response['Items'], response.get('TotalRecordCount')
            if (type(count) is not int or not 0 <= count <= maximum or total is not None and total != count
                    or len(rows) + len(batch) > count or not batch and len(rows) < count):
                raise Unavailable('Emby 目录分页不完整，请稍后重试。')
            total = count
            for row in batch:
                if not isinstance(row, dict) or not isinstance(row.get('Name'), str) or not row['Name'].strip():
                    raise Unavailable('Emby 目录项目无效。')
                key = identifier(row.get('Id'))
                if key in seen:
                    raise Unavailable('Emby 目录分页重复，请稍后重试。')
                seen.add(key)
            rows.extend(batch)
            if len(rows) == total:
                return rows
        raise Unavailable('Emby 目录过大，无法完整比较候选。')

    def item(self, item_id):
        result = self.request(self.items_path + '/' + identifier(item_id),
            {'Fields': 'ProductionYear,PresentationUniqueKey,UserDataLastPlayedDate,MediaSources,Chapters',
             'EnableImages': 'true', 'EnableUserData': 'true'})
        if not isinstance(result, dict) or str(result.get('Id')) != str(item_id):
            raise Unavailable('Emby 项目已失效，请重新查询。')
        return result

    def session(self):
        value = self.request('Sessions', {'DeviceId': self.account['DeviceId']})
        if not isinstance(value, list):
            raise Unavailable('Emby 播放会话格式无效。')
        rows = [row for row in value if isinstance(row, dict)
                and row.get('DeviceId') == self.account['DeviceId'] and row.get('UserId') == self.account['UserId']
                and row.get('SupportsRemoteControl') is True]
        if len(rows) != 1:
            raise Unavailable('未找到这台盒子的唯一 Emby 播放会话，请确认 Next Gen 已连接。')
        return identifier(rows[0].get('Id'))
