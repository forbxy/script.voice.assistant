"""A cancellable worker for local commands and agent rounds, independent of Kodi UI."""
import queue
import threading

from .steering import Steering
from .presentation import parse


class Cancellation:
    """Native resident clients may retain this callback, never the task or view."""
    def __init__(self, abort_requested):
        self.stop, self.abort_requested = threading.Event(), abort_requested

    def __call__(self):
        return self.stop.is_set() or self.abort_requested()


class TaskWorker:
    def __init__(self, text, values, sessions, make_tools, make_agent, tool_options,
                 abort_requested, multi_input=False, speech=None, selection=None):
        self.text, self.values, self.sessions = text, values, sessions
        self.make_tools, self.make_agent, self.tool_options = make_tools, make_agent, tool_options
        self.speech, self.selection = speech, selection
        self.cancelled = Cancellation(abort_requested)
        self.stop, self.done = self.cancelled.stop, threading.Event()
        self.round_ready, self.next_ready = threading.Event(), threading.Event()
        self.steering = Steering() if multi_input else None
        self.activities = queue.Queue()
        self.message = ''
        self.next_text = None
        self.tools = self.outcome = None
        self.error = None
        self.state = 'created'
        self.thread = threading.Thread(target=self._run, name='assistant-agent')

    def start(self):
        self.state = 'running'
        self.thread.start()

    def cancel(self):
        self.stop.set()
        if not self.done.is_set():
            self.state = 'cancelling'

    def report(self, message):
        self.message = message
        self.activities.put(message)

    def advance(self, followup):
        self.next_text = followup
        self.round_ready.clear()
        self.next_ready.set()

    def join(self):
        self.thread.join()

    def _run(self):
        try:
            self._rounds()
            self.state = 'cancelled' if self.cancelled() else 'completed'
        except InterruptedError:
            self.state = 'cancelled'
        except Exception as error:
            self.error, self.state = str(error), 'failed'
        finally:
            self.make_tools = self.make_agent = None
            self.done.set()

    def _rounds(self):
        from .commands import Commands, movie_request
        self.tools = self.make_tools(self.tool_options)
        current, selection, agent, trace = self.text, self.selection, None, []
        while True:
            options = {'steering': self.steering} if self.steering else {}
            if self.speech and self.speech.available:
                options.update(speech=self.speech.say, speech_options=self.values)
            outcome = Commands(self.tools).process(current, selection, self.cancelled, self.report)
            selection = None
            if outcome is not None:
                if hasattr(self.sessions, 'record_local'):
                    self.sessions.record_local(current, parse(outcome['reply'], self.tools)['message'])
            elif self.values.get('agent_backend') == 'off':
                outcome = Commands._outcome(current,
                    '未找到相近的影视或频道，本地命令引擎尚未执行播放。' if movie_request(current) else '')
            else:
                if agent is None:
                    agent, self.tools = self.make_agent(self.tools, self.tool_options)
                outcome = agent.run(current, cancelled=self.cancelled, progress=self.report, **options)
            trace.extend(outcome['tools'])
            self.outcome = dict(outcome, tools=list(trace))
            self.sessions.completed(self.tools.media.values())
            if self.steering is None:
                return
            self.state = 'waiting_input'
            self.round_ready.set()
            while not self.next_ready.wait(.05):
                if self.cancelled():
                    raise InterruptedError('已取消。')
            self.next_ready.clear()
            if self.cancelled():
                raise InterruptedError('已取消。')
            current = self.next_text
            if current is None:
                return
            self.state = 'running'
