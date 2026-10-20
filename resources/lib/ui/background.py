"""Keep one conversation alive while its view is deliberately hidden."""
import queue

import xbmcgui

from ..inputs.registry import claim, pending
from .activation import PREFIX


class Background:
    def __init__(self, window, home, profile, monitor, source):
        self.window, self.home, self.profile, self.monitor = window, home, profile, monitor
        self.source = source
        self.discarded_token = ''
        self.close()

    def pending(self):
        token = pending(self.home, self.source) if self.source != 'usb' else ''
        return '' if token == self.discarded_token else token

    def poll(self):
        """Return true when this tick must not collect voice/keyboard input."""
        if self.monitor.abortRequested():
            return self.window.backgrounded
        if self.window.background_requested.is_set():
            self.window.background_requested.clear()
            if (self.window.task_active and not self.window.recording
                    and not self.window.question_token):
                self.hide()
        if not self.window.backgrounded:
            return False
        token = self.pending()
        if token or self.home.getProperty(PREFIX + 'ForegroundRequested'):
            if not token and self.source != 'usb':
                # The Kodi binding can run slightly before Bluetooth publishes
                # its token. Wait for that same press rather than recording it
                # on the next tick after the window has already reopened.
                for unused in range(20):
                    if self.monitor.waitForAbort(.025):
                        return True
                    token = self.pending()
                    if token:
                        break
            # The Bluetooth key already opened a capture lease. Claim then
            # disconnect it: the service closes the remote mic on peer EOF.
            # Suppress the old property until the service has observed EOF.
            if token:
                self.discarded_token = token
                try:
                    with claim(self.profile, self.source, self.home, self.monitor, token):
                        pass
                except Exception:
                    self.window.activity('已恢复任务窗口；蓝牙录音会话已结束，请再次按语音键后说话。')
            self.foreground()
        return True

    def hide(self):
        self.home.clearProperty(PREFIX + 'ForegroundRequested')
        self.home.clearProperty(PREFIX + 'USBRecordRequested')
        self.home.setProperty(PREFIX + 'Background', 'true')
        if not self.window.backgrounded:
            self.window.set_background(True)

    def foreground(self):
        if not self.window.backgrounded:
            return
        self.home.clearProperty(PREFIX + 'ForegroundRequested')
        self.home.clearProperty(PREFIX + 'USBRecordRequested')
        self.home.clearProperty(PREFIX + 'Background')
        # A key may also have queued a native dialog action. Reopening is
        # the sole action for this press, never a supplement/recording.
        retained = []
        while True:
            try:
                event = self.window.events.get_nowait()
            except queue.Empty:
                break
            if event[0] != 'voice':
                retained.append(event)
        for event in retained:
            self.window.events.put(event)
        self.window.set_background(False)

    def complete(self, failed=False):
        if self.window.backgrounded and not self.monitor.abortRequested():
            xbmcgui.Dialog().notification('AI 语音助手',
                '后台任务处理失败，请按语音键查看。' if failed else '后台任务已完成。', time=5000, sound=False)

    def close(self):
        for name in ('Background', 'ForegroundRequested'):
            self.home.clearProperty(PREFIX + name)
