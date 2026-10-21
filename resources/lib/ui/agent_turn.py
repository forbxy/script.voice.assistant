"""UI controller for a task; worker execution and playback have separate owners."""
import queue
import time
import xbmc
import xbmcgui
import xbmcaddon

from ..data_paths import data_path
from ..recording_ui import RecordingProgress
from ..intelligence.runtime import create_agent, create_tools
from ..intelligence.presentation import parse
from ..intelligence.task import TaskWorker
from ..storage import write_json
from ..settings import Settings, assistant_settings, apply_assistant_setting
from ..version import VERSION
from .task_requests import MainThreadRequests
from .task_playback import TaskPlayback


def playback_state():
    """Underlying video window stays active while our modal dialog is on top."""
    try:
        player = xbmc.Player()
        if player.isPlayingVideo():
            return bool(xbmc.getCondVisibility('Window.IsActive(fullscreenvideo)')), player.getPlayingFile()
    except RuntimeError:
        pass
    return False, ''


def close_for_playback(before, after, tools):
    return bool(after[0] and (after != before or getattr(tools, 'playback_started', False)
                            or getattr(tools, 'fullscreen_requested', False)))


class TurnController:
    def __init__(self, root, profile, values, text, sessions, rpc, builtin, home, monitor,
                 progress=None, poll_input=None, on_message=None, speech=None, ui_context=None, selection=None, background=None):
        self.root, self.profile, self.values, self.text = root, profile, values, text
        self.home, self.monitor, self.background, self.speech = home, monitor, background, speech
        self.poll_input, self.on_message, self.finishing = poll_input, on_message, False
        self.embedded = progress is not None
        self.progress = progress or RecordingProgress(str(root))
        self.agent_name = ('本地命令' if values.get('agent_backend') == 'off' else
                           'CodeBuddy' if values.get('agent_backend') == 'codebuddy' else 'Codex')
        before = playback_state()
        self.open_progress()
        self.began = time.monotonic()
        self.questions = queue.Queue()
        self.question_flow = None
        if self.embedded and hasattr(self.progress, 'window') and on_message:
            from .questions import QuestionFlow
            self.question_flow = QuestionFlow(self.questions, self.progress.window, on_message,
                foreground=background.foreground if background else None)
        self.requests = MainThreadRequests(lambda: self.task.cancelled())
        self.playback = TaskPlayback(rpc, builtin, monitor, self.requests, self.elapsed, before, playback_state, close_for_playback)
        options = dict(assistant_get=self.requests.settings_get, assistant_set=self.requests.settings_set,
            defer_ui=True, ui_context=ui_context, playback_handoff=self.playback.request if
            self.embedded and background and hasattr(self.progress, 'window') else None)
        self.task = TaskWorker(text, values, sessions,
            lambda options: create_tools(profile, values, self.playback.rpc, builtin, self.requests.confirm, sessions, **options),
            lambda tools, options: create_agent(root, profile, values, self.playback.rpc, builtin, self.requests.confirm,
                sessions, request_user_input=self.questions.put if self.question_flow else None, tools=tools, **options),
            options, monitor.abortRequested, multi_input=bool(poll_input), speech=speech, selection=selection)
        self.requests.cancelled = self.task.cancelled
        self.task.message = '正在连接 ' + self.agent_name + '…'

    def elapsed(self):
        return round(time.monotonic() - self.began, 3)

    def open_progress(self):
        self.progress.create('语音助手 · ' + self.agent_name, self.text[-160:])
        self.progress.set_recording(False)

    def drain_activity(self):
        while True:
            try:
                message = self.task.activities.get_nowait()
            except queue.Empty:
                return
            if hasattr(self.progress, 'activity'):
                self.progress.activity(message)

    def before_playback(self):
        self.drain_activity()
        self.progress.update(0, self.task.message + '\n返回取消')
        if self.monitor.waitForAbort(.05) or self.progress.iscanceled():
            self.task.cancel()
            return None
        return {'ready': True, 'at': self.elapsed()}

    def settings(self, request):
        config = Settings(xbmcaddon.Addon('script.voice.assistant'))
        if request.kind == 'settings-get':
            return assistant_settings(config, request.payload)
        name, value = request.payload
        return apply_assistant_setting(config, name, value, self.home.getProperty('VoiceAssistant.CodexLoggedIn') == 'true')

    def confirm(self, payload):
        if not self.embedded:
            self.progress.close()
        if self.background:
            self.background.foreground()
        try:
            return xbmcgui.Dialog().yesno(*payload)
        finally:
            if not self.embedded:
                self.progress = RecordingProgress(str(self.root))
            self.open_progress()

    def collect_input(self, background_input):
        if not self.poll_input or self.finishing or background_input:
            return
        target = self.question_flow.token if self.question_flow else ''
        supplement = self.poll_input(self.drain_activity)
        if self.monitor.abortRequested() or self.progress.iscanceled():
            self.task.cancel()
            return
        if not supplement:
            return
        try:
            if self.question_flow and target:
                self.question_flow.answer(supplement, target)
            elif not isinstance(supplement, dict):
                if self.speech:
                    self.speech.restart()
                self.task.steering.submit(supplement)
                if self.on_message:
                    self.on_message('user', supplement.strip())
                self.task.report('正在发送补充要求…')
        except ValueError as error:
            self.task.report(str(error))

    def advance_round(self):
        if not self.task.round_ready.is_set():
            return
        followup = self.task.steering.followup()
        if followup is not None:
            if self.on_message:
                self.on_message('assistant', parse(self.task.outcome['reply'], self.task.tools)['message'])
            self.task.report('上一轮已结束，继续处理补充要求。')
        self.finishing = followup is None
        self.task.advance(followup)

    def pump(self):
        while not self.task.done.is_set():
            background_input = self.background.poll() if self.background else False
            self.drain_activity()
            if self.monitor.abortRequested() or self.progress.iscanceled():
                self.task.cancel()
                return
            request = self.requests.take()
            if request and request.kind == 'before-playback':
                self.requests.resolve(request, self.before_playback)
                continue
            if request and request.kind in ('settings-get', 'settings-set'):
                self.requests.resolve(request, lambda: self.settings(request))
                continue
            if request and request.kind == 'confirm':
                self.requests.resolve(request, lambda: self.confirm(request.payload))
            if self.question_flow:
                self.question_flow.tick()
            self.collect_input(background_input)
            if self.task.cancelled():
                return
            if request and request.kind == 'playback':
                self.requests.resolve(request, lambda: self.playback.dispatch(request.payload,
                    self.progress, self.background, self.speech, self.task))
            self.advance_round()
            self.home.setProperty('VoiceAssistant.State', 'thinking')
            message = ('请选择选项或用语音、文字回答' if self.question_flow and self.question_flow.token else self.task.message)
            self.progress.update(0, message + ('\n返回取消' if self.embedded else '\n' + self.text[-120:] + '\n返回取消'))
            self.monitor.waitForAbort(.05)

    def result(self):
        self.drain_activity()
        if self.task.cancelled() or self.task.state == 'cancelled':
            if self.speech:
                self.speech.stop()
            if not self.monitor.abortRequested() and getattr(self.progress, 'task_stopped', lambda: False)():
                return {'message': '任务已停止。已完成的操作不会自动撤销，可以继续输入新要求。',
                        'interaction': 'continue', 'choices': []}
            return None
        if self.task.error is not None:
            raise RuntimeError(self.task.error)
        outcome, tools = self.task.outcome, self.task.tools
        scheduled = tools.commit_deferred(self.monitor.abortRequested)
        presentation = parse(outcome['reply'], tools)
        self.playback.present(presentation, tools, outcome)
        self.home.setProperty('VoiceAssistant.AgentReply', presentation['message'])
        write_json(data_path(self.profile, 'last_agent.json'), {'state': 'done', 'version': VERSION,
            'permissions': self.values['permission_level'], 'approval_strategy': self.values['approval_strategy'],
            'elapsed_seconds': self.elapsed(), 'tools': outcome['tools'], 'reply': presentation['message'], 'maintenance': scheduled,
            'agent_backend': outcome.get('agent_backend', self.values.get('agent_backend', 'codex')),
            'model_invocations': outcome.get('model_invocations'), 'matching': outcome.get('matching'),
            'playback_timing': self.playback.timing})
        return presentation

    def run(self):
        self.task.start()
        try:
            self.pump()
            self.task.join()
            return self.result()
        finally:
            self.task.cancel()
            self.task.join()
            if self.question_flow:
                self.question_flow.clear()
            self.progress.close()


def run_turn(root, profile, values, text, sessions, rpc, builtin, home, monitor, progress=None, poll_input=None, on_message=None, speech=None,
             ui_context=None, selection=None, background=None):
    return TurnController(root, profile, values, text, sessions, rpc, builtin, home, monitor, progress,
                          poll_input, on_message, speech, ui_context, selection, background).run()
