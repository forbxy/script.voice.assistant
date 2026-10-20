"""Release only our own modal dialog before handing focus to Kodi or another addon."""
import time
import xbmcgui


def handoff(window, command, monitor, builtin, rpc=None, release=None):
    if monitor.abortRequested() or window.cancelled.is_set():
        return False
    identifier = 9999 if getattr(window, 'backgrounded', False) else xbmcgui.getCurrentWindowDialogId()
    (release or window.close)()
    deadline = time.monotonic() + 2
    # close() is queued to Kodi's GUI thread; do not race ActivateWindow against it.
    while identifier != 9999 and xbmcgui.getCurrentWindowDialogId() == identifier:
        if monitor.waitForAbort(.02) or window.cancelled.is_set():
            return False
        if time.monotonic() >= deadline:
            raise RuntimeError('助手窗口尚未释放，未执行界面切换，请重试。')
    if monitor.abortRequested() or window.cancelled.is_set():
        return False
    if isinstance(command, dict):
        if 'emby_play' in command:
            from ..intelligence.emby_playback import dispatch
            return dispatch(command, cancelled=lambda: monitor.abortRequested() or window.cancelled.is_set(),
                            rpc=rpc, wait=monitor.waitForAbort)
        from ..intelligence.interface_tools import perform_operations
        if rpc is None:
            raise RuntimeError('缺少Kodi界面操作接口。')
        perform_operations(command, rpc, cancelled=lambda: monitor.abortRequested() or window.cancelled.is_set())
    else:
        builtin(command)
    return True
