"""Sample only confirmed Juku playback; Kodi callbacks never perform file or network work."""
import queue
import time

from .juku_history import Store, route, recorded, key, seconds


class Tracker:
    def __init__(self, profile, player, clock=time.monotonic, now=time.time, next_item=lambda: None):
        self.store, self.player, self.clock, self.now = Store(profile), player, clock, now
        self.events = queue.SimpleQueue()
        self.current = None
        self.file = None
        self.next_tick = self.next_save = 0
        self.restore = None
        self.inactive = False
        self.next_item = next_item

    def event(self, event):
        self.events.put(event)

    def _save(self, ended=False):
        if self.current is not None:
            value = dict(self.current, played=bool(ended))
            if ended and value['total'] > 0:
                value['position'] = value['total']
            self.store.save(value)

    def tick(self, enabled=True):
        force = False
        while not self.events.empty():
            event = self.events.get()
            if event in ('stopped', 'ended', 'error', 'started'):
                self._save(event == 'ended')
                self.current, self.file, self.restore = None, None, None
                self.inactive = event != 'started'
            force = True
        now = self.clock()
        if not enabled:
            self.current, self.file, self.restore = None, None, None
            return
        if self.inactive:
            return
        if not force and now < self.next_tick:
            return
        self.next_tick = now + 1
        if not self.player.isPlayingVideo():
            self._save()
            self.current, self.file, self.restore = None, None, None
            return
        try:
            item = self.player.getPlayingItem()
            original = item.getProperty('original_listitem_url') or item.getPath()
            params = route(original)
            if params is None:
                self._save()
                self.current, self.file, self.restore = None, None, None
                return
            handle = key(params)
            if handle != self.file:
                self._save()
                self.current, self.file = None, handle
                # Only an explicit requested resume offset can seek a player.
                from urllib.parse import parse_qs, urlsplit
                raw = parse_qs(urlsplit(original).query).get('resume_time', ['0'])[0]
                try:target = seconds(float(raw))
                except ValueError:target = 0
                self.restore = (target, now + 12) if target > 0 else None
            tag = item.getVideoInfoTag()
            metadata = {'type': tag.getMediaType(), 'title': tag.getTitle(), 'showtitle': tag.getTVShowTitle(),
                        'year': tag.getYear(), 'season': tag.getSeason(), 'episode': tag.getEpisode()}
            position, total = self.player.getTime(), self.player.getTotalTime()
            if self.restore:
                target, deadline = self.restore
                if abs(position - target) < 3 or now > deadline:
                    self.restore = None
                elif 0 <= position < 3 and target > 3:
                    self.player.seekTime(min(target, total) if total > 0 else target)
                    self.restore = None
                    return  # Sample actual progress on the next tick, never the requested offset.
            value = recorded(params, metadata, position, total, now=self.now())
            if value is not None and (self.current is None or position != self.current['position'] or force):
                try:
                    following = self.next_item() if value['media_type'] == 'episode' else None
                except (RuntimeError, IndexError):
                    following = None  # Kodi may replace the playlist during the sample.
                if (following and following.get('catalogue_id') == value['catalogue_id']
                        and following.get('title') == value['title'] and following['media_type'] == 'episode'
                        and (following['season'], following['episode']) > (value['season'], value['episode'])):
                    value['next'] = following
                self.current = value
                if force or now >= self.next_save:
                    self._save()
                    self.next_save = now + 10
        except RuntimeError:
            # Player teardown races callbacks; keep the last confirmed sample.
            pass

    def close(self):
        self._save()
        self.current, self.file = None, None


def create(profile):
    import xbmc
    class Player(xbmc.Player):
        tracker = None
        def event(self, name):
            if self.tracker is not None:self.tracker.event(name)
        def onAVStarted(self):self.event('started')
        def onPlayBackPaused(self):self.event('paused')
        def onPlayBackResumed(self):self.event('resumed')
        def onPlayBackSeek(self, time, seekOffset):self.event('seek')
        def onPlayBackStopped(self):self.event('stopped')
        def onPlayBackEnded(self):self.event('ended')
        def onPlayBackError(self):self.event('error')
    player = Player()
    def next_item():
        playlist = xbmc.PlayList(1)
        index = playlist.getposition() + 1
        if index <= 0 or index >= playlist.size():return None
        item = playlist[index]
        params = route(item.getPath())
        if params is None:return None
        tag = item.getVideoInfoTag()
        return recorded(params, {'type': tag.getMediaType(), 'title': tag.getTitle(),
            'showtitle': tag.getTVShowTitle(), 'year': tag.getYear(), 'season': tag.getSeason(),
            'episode': tag.getEpisode()}, 0, 0, now=0)
    tracker = Tracker(profile, player, next_item=next_item)
    player.tracker = tracker
    return tracker
