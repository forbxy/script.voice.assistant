"""Playback acknowledgements, foreground handoff and timings for one task."""
import time


CONTROL_TOOLS = frozenset(('control_playback', 'seek_playback', 'set_volume', 'set_mute',
    'set_playback_track', 'set_audio_delay', 'adjust_subtitles', 'set_video_view',
    'set_playlist_mode', 'edit_playlist'))
CONTROL_READS = frozenset(('get_kodi_status', 'get_playback_details', 'get_playlist'))


def local_control(text):
    """Recognize controls that can finish before any conversation UI exists."""
    from ..intelligence.command_rules import common_request
    request = common_request(text)
    return bool(request and (request.get('tool') in CONTROL_TOOLS or 'volume_delta' in request or 'continue_kind' in request))


def controls_completed(presentation, outcome):
    """Only host-recorded successful controls may dismiss a response window."""
    if presentation.get('choices') or not outcome:
        return False
    controlled = False
    # Earlier steering rounds stay in the audit trace, but cannot dismiss the
    # answer to a later question that did not itself perform playback control.
    for row in outcome.get('round_tools', outcome.get('tools', [])):
        if not isinstance(row, dict) or row.get('ok') is not True:
            return False
        name = row.get('name')
        if name in CONTROL_TOOLS or (name == 'sleep_timer' and
                (row.get('arguments') or {}).get('action') in ('set', 'cancel')):
            controlled = True
        elif name not in CONTROL_READS:
            return False
    return controlled


class TaskPlayback:
    def __init__(self, rpc, builtin, monitor, requests, elapsed, before, read_state, close_for_playback):
        self.native_rpc, self.builtin, self.monitor, self.requests = rpc, builtin, monitor, requests
        self.elapsed, self.before, self.read_state, self.close_for_playback = elapsed, before, read_state, close_for_playback
        self.timing = []
        self.state = 'idle'

    def rpc(self, method, params):
        if method != 'Player.Open':
            return self.native_rpc(method, params)
        started = time.monotonic()
        timing = {'requested_seconds': self.elapsed()}
        self.timing.append(timing)
        answer = self.requests.call('before-playback')
        if not answer or not answer.get('ready'):
            raise InterruptedError('已取消，未开始打开影片。')
        timing.update(ui_ready_seconds=answer['at'], ui_wait_seconds=round(time.monotonic() - started, 3))
        timing['dispatch_seconds'] = self.elapsed()
        started = time.monotonic()
        try:
            self.state = 'dispatched'
            return self.native_rpc(method, params)
        finally:
            timing['rpc_seconds'] = round(time.monotonic() - started, 3)

    def request(self, command, target):
        timing = {'operation': 'external_playback', 'requested_seconds': self.elapsed()}
        self.timing.append(timing)
        self.state = 'pending'
        return self.requests.call('playback', (command, target, timing), allow_completed=True)

    def dispatch(self, payload, progress, background, speech, task):
        command, target, timing = payload
        if task.cancelled() or progress.iscanceled():
            task.cancel()
            return None
        if task.steering and task.steering.pending():
            timing['deferred_for_input'] = True
            return None
        from .navigation import handoff
        try:
            if speech:
                speech.stop()
            timing['dispatch_seconds'] = self.elapsed()
            result = handoff(progress.window, command, self.monitor, self.builtin,
                             rpc=self.native_rpc, release=background.hide)
            if result:
                timing['handoff_seconds'] = self.elapsed()
                if isinstance(result, dict) and result.get('confirmed'):
                    timing['playback_confirmed'] = True
                    self.state = 'playing'
                    return dict(result, target=target, note='已确认所选视频起播。')
                self.state = 'dispatched'
                return {'status': 'dispatched', 'target': target,
                        'note': '已释放助手窗口并提交打开请求；尚未确认视频起播。'}
        except Exception:
            self.state = 'failed'
            background.foreground()
            raise

    def present(self, presentation, tools, outcome=None):
        if getattr(tools, 'ui_action', None):
            presentation['ui_action'] = tools.ui_action
        if getattr(tools, 'ui_handed_off', False):
            presentation['ui_handed_off'] = True
        after = self.read_state()
        if self.close_for_playback(self.before, after, tools):
            self.state = 'playing'
            presentation['interaction'] = 'close'
        elif controls_completed(presentation, outcome):
            self.state = 'controlled'
            presentation['interaction'] = 'close'
        for timing in self.timing:
            if 'handoff_seconds' in timing:
                timing['reply_after_handoff_seconds'] = round(self.elapsed() - timing['handoff_seconds'], 3)
