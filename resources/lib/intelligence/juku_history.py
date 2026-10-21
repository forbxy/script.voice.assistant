"""Private local progress, keyed by Juku's original resource rather than an expiring stream URL."""
import fcntl
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlsplit

from ..data_paths import data_path
from ..storage import read_json, write_json

HISTORY = 'history/juku-playback.json'
PLAY_ACTIONS = ('pan_play', 'play_private_link', 'play_hls', 'cloud_down_play', 'cloud_down_task_play')
ROUTE_FIELDS = frozenset(('action', 'url', 'code', 'password', 'provider', 'pan_name', 'file_id',
    'file_name', 'file_size', 'movie_id', 'vod_id', 'vod_title', 'title', 'display_title', 'type_id',
    'is_tv', 'season_number', 'episode_number', 'episode_title', 'link_id', 'link_type', 'link_idx',
    'single_file', 'from_share', 'remark', 'hash', 'task_id', 'pic', 'fanart'))


def seconds(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 864000 else 0.


def route(value):
    """Only accept Juku play routes, never an arbitrary plugin action or builtin."""
    if not isinstance(value, str) or len(value) > 65536:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != 'plugin' or parsed.netloc != 'plugin.video.juku':
        return None
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=80)
    except ValueError:
        return None
    if len({key for key, value in pairs}) != len(pairs):
        return None
    params = {key: value for key, value in pairs if key in ROUTE_FIELDS and len(value) <= 16384}
    if params.get('action') not in PLAY_ACTIONS:
        return None
    if params['action'] == 'pan_play' and not params.get('file_id'):
        return None
    if not (params.get('url') or params.get('movie_id')):
        return None
    return params


def key(params):
    stable = {name: value for name, value in params.items() if name not in (
        'vod_title', 'title', 'display_title', 'episode_title', 'file_name', 'file_size', 'pic', 'fanart', 'remark')}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class Store:
    def __init__(self, profile):
        self.profile = Path(profile)
        self.path = data_path(self.profile, HISTORY)

    def records(self):
        if self.path.exists() and self.path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('巨库播放记录过大，无法读取。')
        value = read_json(self.path, {})
        rows = value.get('records', {}) if isinstance(value, dict) else {}
        if not isinstance(rows, dict):
            return {}
        return {handle: row for handle, row in rows.items() if isinstance(row, dict)
                and row.get('key') == handle and row.get('source') in ('online', 'private')
                and isinstance(row.get('title'), str) and row['title'].strip()
                and isinstance(row.get('catalogue_id'), str)
                and row.get('media_type') in ('movie', 'episode')
                and type(row.get('updated_at')) in (int, float) and math.isfinite(row['updated_at'])
                and type(row.get('position')) in (int, float) and seconds(row['position']) == row['position']
                and type(row.get('total')) in (int, float) and seconds(row['total']) == row['total']
                and (row['media_type'] == 'movie' or (type(row.get('season')) is int and row['season'] >= 0
                     and type(row.get('episode')) is int and row['episode'] > 0))}

    def get(self, handle):
        return self.records().get(handle)

    def save(self, record):
        handle = record['key']
        with data_path(self.profile, 'juku-history.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            rows = self.records()
            rows[handle] = dict(record)
            rows = dict(sorted(rows.items(), key=lambda item: item[1].get('updated_at', 0), reverse=True)[:100])
            write_json(self.path, {'version': 1, 'records': rows})
        return handle


def recorded(params, metadata, position, total, ended=False, now=None):
    """Called only after Kodi confirms that this original plugin route is playing."""
    movie = str(params.get('movie_id') or params.get('vod_id') or '')
    if movie.startswith(('mov_', 'ep_mov_')):
        return None  # Private-library progress already belongs to Juku's own backend.
    media_type = metadata.get('type')
    if media_type not in ('movie', 'episode'):
        media_type = ('movie' if params.get('type_id') == '1' or params.get('is_tv') == '0' else
                      'episode' if params.get('is_tv') == '1' or params.get('type_id') == '2' else None)
    if media_type is None:
        return None
    title = str(params.get('vod_title') or metadata.get('showtitle') or metadata.get('title') or '')[:512]
    if not title:
        return None
    season, episode = metadata.get('season'), metadata.get('episode')
    for name, value in (('season', season), ('episode', episode)):
        if type(value) is not int or value < (0 if name == 'season' else 1):
            raw = params.get(name + '_number', '')
            value = int(raw) if str(raw).isdigit() and len(str(raw)) < 5 else None
        if name == 'season':season = value
        else:episode = value
    if media_type == 'episode' and (season is None or episode is None):
        return None
    return {'key': key(params), 'source': 'online', 'catalogue_id': movie, 'title': title,
            'year': metadata.get('year', 0), 'media_type': media_type, 'season': season, 'episode': episode,
            'episode_title': str(metadata.get('title') or params.get('episode_title') or '')[:512],
            'position': seconds(position), 'total': seconds(total), 'played': bool(ended),
            'updated_at': time.time() if now is None else now, 'route': dict(params)}
