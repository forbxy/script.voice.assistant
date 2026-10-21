"""Kodi view of the exact same dependency jobs shown on the configuration page."""
import xbmc
import xbmcgui
from .service import request
from ..ui import NOTIFICATION_ICON


def show(profile):
    dialog, monitor = xbmcgui.Dialog(), xbmc.Monitor()
    state = request(profile)
    if state['job']['state'] != 'running':
        if not state['missing']:
            dialog.notification('语音助手', '所需组件已安装。', icon=NOTIFICATION_ICON, sound=False)
            return
        names = '\n'.join(row['label'] for row in state['missing'])
        size = sum(row['bytes'] for row in state['missing']) / 1024 ** 2
        if not dialog.yesno('下载所需组件', names + '\n下载约 {:.1f} MiB，是否现在下载？'.format(size)):
            return
        state = request(profile, 'start')
    progress = xbmcgui.DialogProgress()
    progress.create('语音助手 · 下载组件')
    try:
        while state['job']['state'] == 'running' and not monitor.abortRequested():
            job = state['job']
            percent = job.get('phase_percent', job.get('percent', 0))
            phase = {'uploading': '正在接收上传', 'downloading': '正在下载', 'verifying': '正在校验', 'extracting': '正在解压'}.get(job.get('phase'), '')
            detail = '{} {} · {:.1f}%\n{:.1f} / {:.1f} MiB · {:.1f} MiB/s'.format(
                phase, job.get('label', ''), percent, job.get('downloaded', 0) / 1024**2,
                job.get('total', 0) / 1024**2, job.get('speed', 0) / 1024**2)
            progress.update(int(percent), detail)
            if progress.iscanceled():
                request(profile, 'cancel')
                return
            monitor.waitForAbort(.2)
            state = request(profile)
    finally:
        progress.close()
    if state['job']['state'] == 'error':
        dialog.ok('组件下载失败', state['job'].get('error', '请稍后重试。'))
    elif state['job']['state'] == 'done':
        dialog.notification('语音助手', '组件已就绪。', icon=NOTIFICATION_ICON, sound=False)
