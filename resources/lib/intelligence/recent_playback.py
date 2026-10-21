"""Compare bounded Kodi/Emby viewing history without building a title catalogue."""
from datetime import datetime
import time


LIMIT = 13  # Twelve visible choices plus one to detect a non-unique latest item.


def timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # Kodi serializes local time; Emby normally returns UTC with a Z suffix.
        date = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        return date.timestamp() if date.year > 1970 else None
    except (ValueError, OverflowError, OSError):
        return None


def latest(tools, kind, cancelled, progress):
    from . import emby
    records, complete, clients = [], True, {}
    for category in (('movie', 'tvshow') if kind == 'all' else (kind,)):
        if cancelled():
            raise InterruptedError('已取消查询观看记录。')
        name, key = ('Movies', 'movies') if category == 'movie' else ('TVShows', 'tvshows')
        fields = ['title', 'year', 'thumbnail', 'art', 'lastplayed', 'playcount', 'resume'] if category == 'movie' else [
                  'title', 'year', 'thumbnail', 'art', 'lastplayed', 'watchedepisodes']
        try:
            value = tools.rpc('VideoLibrary.Get' + name, {'properties': fields,
                'sort': {'method': 'lastplayed', 'order': 'descending'}, 'limits': {'start': 0, 'end': LIMIT}})
            if not isinstance(value, dict) or not isinstance(value.get(key, []), list):
                raise ValueError('本地最近观看记录格式无效。')
            for row in value.get(key, []):
                stamp = timestamp(row.get('lastplayed'))
                if stamp is not None:
                    records.append((stamp, 'local', category, row))
        except InterruptedError:
            raise
        except (ValueError, OSError, RuntimeError):
            complete = False
    if cancelled():
        raise InterruptedError('已取消查询观看记录。')
    if tools.media_search_emby and tools._emby_available is None:
        tools._emby_available = emby.available(tools)
    if tools.media_search_emby and tools._emby_available:
        progress('正在查询 Emby 最近观看记录…')
        deadline = time.monotonic() + 15
        for account in emby.accounts(tools.profile):
            if cancelled():
                raise InterruptedError('已取消查询观看记录。')
            client = emby.Client(account, cancelled)
            client.deadline = min(getattr(client, 'deadline', deadline), deadline)
            clients[account['_identity']] = client
            try:
                value = client.request(client.items_path, {'IncludeItemTypes':
                    {'all': 'Movie,Episode', 'movie': 'Movie', 'tvshow': 'Episode'}[kind],
                    'Recursive': 'true', 'EnableUserData': 'true', 'EnableImages': 'true',
                    'Fields': 'ProductionYear,UserDataLastPlayedDate',
                    'SortBy': 'DatePlayed', 'SortOrder': 'Descending', 'StartIndex': 0, 'Limit': LIMIT})
                if not isinstance(value, dict) or not isinstance(value.get('Items'), list) or len(value['Items']) > LIMIT:
                    raise emby.Unavailable('Emby 最近观看记录格式无效。')
                batch = []
                for row in value['Items']:
                    if not isinstance(row, dict) or row.get('Type') not in (
                            ('Movie', 'Episode') if kind == 'all' else ('Movie',) if kind == 'movie' else ('Episode',)):
                        raise emby.Unavailable('Emby 最近观看记录类型无效。')
                    # Only server-provided history dates participate in recency.
                    data = row.get('UserData') or {}
                    if not isinstance(data, dict):
                        raise emby.Unavailable('Emby 观看状态格式无效。')
                    stamp = timestamp(data.get('LastPlayedDate'))
                    if stamp is not None:
                        item = emby.candidate(row, account)
                        item['_recent_value'] = row
                        batch.append((stamp, 'emby', item['media_type'], item))
                records.extend(batch)
            except InterruptedError:
                raise
            except (ValueError, OSError, RuntimeError):
                complete = False
    if cancelled():
        raise InterruptedError('已取消查询观看记录。')
    if not records:
        return [], complete
    newest = max(row[0] for row in records)
    selected = [record for record in records if record[0] == newest]
    rows, seen = [], set()
    for stamp, source, category, row in selected[:LIMIT]:
        if cancelled():
            raise InterruptedError('已取消查询观看记录。')
        if source == 'local':
            rows.extend(tools._remember([row], category))
            continue
        client = clients[row['_emby_account']]
        value = row.pop('_recent_value')
        try:
            if category == 'episode':
                series = emby.identifier(value.get('SeriesId'))
                actual = client.item(series)
                if actual.get('Type') != 'Series' or str(actual.get('Id')) != series:
                    raise emby.Unavailable('无法确认最近观看剧集的电视剧归属。')
                row = emby.candidate(actual, client.account)
            else:
                data = value.get('UserData') or {}
                from .emby_playback import ticks
                row.update(lastplayed=data['LastPlayedDate'], playcount=int(data.get('Played') is True),
                    resume={'position': ticks(data.get('PlaybackPositionTicks')) / 10000000,
                            'total': ticks(value.get('RunTimeTicks')) / 10000000})
            row['thumbnail'] = emby.thumbnail(actual if category == 'episode' else value, client.account)
            key = (row['_emby_account'], row['_emby_id'])
            if key not in seen:
                seen.add(key)
                request = dict(title=row['title'], kind=row['media_type'], year=row['year'], resume=True)
                rows.extend(emby.remember(tools, [row], request))
        except InterruptedError:
            raise
        except (ValueError, OSError, RuntimeError):
            complete = False
    return rows, complete
