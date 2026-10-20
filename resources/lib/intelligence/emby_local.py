"""Send playback to Next Gen on loopback and confirm the requested video."""
import socket
import time
from urllib.parse import urlencode, unquote, urlsplit

from .emby_client import Unavailable, identifier


def send(server, item, cancelled):
    if cancelled():
        raise InterruptedError('已取消，未提交 Emby 播放。')
    query = urlencode({'mode': 'play', 'server': identifier(server), 'item': identifier(item)})
    try:
        # Same local protocol used by Next Gen's service.py / RunPlugin entry.
        # No URL, token, shell command or server-side remote-control session.
        with socket.create_connection(('127.0.0.1', 57342), timeout=3) as connection:
            connection.settimeout(3)
            connection.sendall(('EVENT service;-1;?' + query).encode('utf-8'))
            response = connection.recv(1024)
        if not response.startswith(b'HTTP/1.1 200 '):
            raise Unavailable('Next Gen 未接受本机播放请求，请检查插件状态。')
    except OSError:
        raise Unavailable('无法连接 Next Gen 本机播放服务，请确认插件已启动。') from None


def matches(row, item, actual, selected=None):
    """A title alone is insufficient: never seek a different same-name video."""
    path = row.get('file')
    if not isinstance(path, str) or not path:
        return False
    if selected is not None:
        return (path.split('|', 1)[0] == selected['url'].split('|', 1)[0]
                or bool(selected['path']) and path == selected['path'])
    parts = unquote(urlsplit(path).path).split('/')
    server = item['_emby_server']
    ids = {str(item['_emby_id'])}
    sources = actual.get('MediaSources')
    sources = [row for row in sources if isinstance(row, dict)] if isinstance(sources, list) else []
    ids.update(str(source['ItemId']) for source in sources if source.get('ItemId'))
    # Both synced and dynamic Next Gen paths have server/library/0/item-id.
    if server in parts:
        index = parts.index(server)
        if len(parts) > index + 3 and parts[index + 3] in ids:
            return True
    # Native-path mode can avoid the local HTTP proxy entirely.
    paths = [actual.get('Path')] + [source.get('Path') for source in sources]
    return any(path == value for value in paths if isinstance(value, str) and value)


def seconds(value):
    return sum((value.get(name, 0) or 0) * factor for name, factor in
               (('hours', 3600), ('minutes', 60), ('seconds', 1), ('milliseconds', .001)))


def service_ready(cancelled):
    if cancelled():
        raise InterruptedError('已取消，未提交 Emby 播放。')
    try:
        with socket.create_connection(('127.0.0.1', 57342), timeout=3) as connection:
            connection.settimeout(3)
            # The root is deliberately not a media URL: HEAD cannot select a
            # version, initialize playback or download the video.
            connection.sendall(b'HEAD / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n')
            response = connection.recv(1024)
        if not response.startswith(b'HTTP/1.'):
            raise OSError('Invalid local service response')
    except OSError:
        raise Unavailable('Next Gen 本机播放服务尚未就绪，请稍后重试。') from None


def open_source(selected, item, rpc):
    try:
        import xbmc
        import xbmcgui
    except ImportError:
        return rpc('Player.Open', {'item': {'file': selected['url']}, 'options': {'resume': False}})
    entry = xbmcgui.ListItem(label=item['title'], path=selected['url'])
    # Dynamic Next Gen HEAD replies advertise HLS even for ordinary files.
    # Match its own ListItem setup: don't let Kodi misclassify this as a playlist.
    entry.setContentLookup(False)
    entry.setMimeType(selected['mime_type'])
    entry.setProperty('mimetype', selected['mime_type'])
    entry.setProperty('IsPlayable', 'true')
    info = entry.getVideoInfoTag()
    info.setMediaType(item['media_type'])
    info.setTitle(item['title'])
    # Populate the video queue before opening it. Passing a bare ListItem to
    # Player.play can leave an empty/stale playlist after a failed STRM open.
    queue = xbmc.PlayList(1)
    queue.clear()
    queue.add(selected['url'], entry)
    if queue.size() != 1:
        raise Unavailable('未能建立所选 Emby 视频的播放队列，请重试。')
    rpc('Player.Open', {'item': {'playlistid': 1, 'position': 0}})


def play(item, actual, resume, rpc, cancelled=lambda: False, wait=time.sleep, clock=time.monotonic, timeout=25):
    from .playback_tools import playback_watcher
    from .emby_versions import prepare
    if cancelled():
        raise InterruptedError('已取消，未提交 Emby 播放。')
    selected = prepare(item, actual, rpc)
    watcher = playback_watcher()
    before = rpc('Player.GetActivePlayers', {})
    old_files = {}
    for player in before:
        if player.get('type') == 'video':
            old_files[player['playerid']] = rpc('Player.GetItem', {
                'playerid': player['playerid'], 'properties': ['file']}).get('item', {}).get('file')
    if selected is None:
        send(item['_emby_server'], item['_emby_id'], cancelled)
    else:
        if cancelled():
            raise InterruptedError('已取消，未提交 Emby 播放。')
        service_ready(cancelled)
        open_source(selected, item, rpc)
    started, ready_at, stable_at, sought = clock(), None, None, False
    position = (actual.get('UserData') or {}).get('PlaybackPositionTicks', 0) if resume else 0
    position = position / 10000000 if type(position) is int and 0 <= position <= 10**16 else 0
    while clock() - started < timeout:
        if cancelled():
            raise InterruptedError('已取消等待 Emby 起播；打开请求已提交。')
        if watcher is not None and watcher.failed.is_set():
            raise Unavailable('Next Gen 已提交播放，但 Kodi 起播失败，请检查片源。')
        players = rpc('Player.GetActivePlayers', {})
        for player in players:
            if player.get('type') != 'video':
                continue
            playerid = player['playerid']
            try:
                row = rpc('Player.GetItem', {'playerid': playerid, 'properties': ['file']}).get('item', {})
                if not matches(row, item, actual, selected):
                    if row.get('file') and row['file'] != old_files.get(playerid):
                        raise Unavailable('当前播放项目已变化，未向其他影片设置播放位置。')
                    continue
                properties = rpc('Player.GetProperties', {'playerid': playerid,
                    'properties': ['time', 'speed', 'canseek']})
            except Unavailable:
                raise
            except (ValueError, RuntimeError):
                continue  # The old player can disappear between two RPC reads.
            if watcher is not None and not watcher.ready.is_set():
                continue
            if properties.get('speed', 0) == 0:
                continue
            now = clock()
            if ready_at is None:
                ready_at = now
            elapsed = seconds(properties.get('time') or {})
            # Next Gen normally applies its own resume seek after AVStarted.
            # Let that finish first; then enforce explicit restart / a missed seek
            # on this verified file, including when its native start stayed at 0.
            if now - ready_at < .5:
                continue
            if not sought and abs(elapsed - position) > 2:
                if not properties.get('canseek'):
                    raise Unavailable('视频已打开，但不能定位到请求的播放位置。')
                if cancelled():
                    raise InterruptedError('已取消，未修改播放位置。')
                milliseconds = round(position * 1000)
                rpc('Player.Seek', {'playerid': playerid, 'value': {'time': {
                    'hours': milliseconds // 3600000, 'minutes': milliseconds // 60000 % 60,
                    'seconds': milliseconds // 1000 % 60, 'milliseconds': milliseconds % 1000}}})
                sought, stable_at = True, None
                continue
            if abs(elapsed - position) <= 2 + max(0, now-ready_at):
                if stable_at is None:
                    stable_at = now
                if now - stable_at >= .4:
                    result = {'status': 'playing', 'playerid': playerid, 'confirmed': True}
                    if selected is not None:
                        result['source_selection'] = selected['selection']
                    return result
            else:
                stable_at = None
        wait(.1)
    raise Unavailable('Next Gen 打开请求已提交，但未确认所选视频起播，请检查播放错误后重试。')
