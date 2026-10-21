"""Task completion and UI requests release waiters without involving Kodi."""
import threading
from types import SimpleNamespace

import pytest

from resources.lib.intelligence.task import TaskWorker
from resources.lib.ui.task_requests import MainThreadRequests


def task(run=None, multi=False):
    def default_run(*args, **kwargs):
        return {'reply':'完成','tools':[]}
    return TaskWorker('任务', {'agent_backend':'codex'}, SimpleNamespace(completed=lambda rows: None),
        lambda options:SimpleNamespace(media={}),
        lambda tools,options:(SimpleNamespace(run=run or default_run),tools), {}, lambda:False, multi_input=multi)


def test_round_boundary_preserves_worker_until_ui_has_collected_late_input():
    worker = task(multi=True)
    worker.start()
    try:
        assert worker.round_ready.wait(2)
        assert worker.state == 'waiting_input' and not worker.done.is_set()
        worker.steering.submit('补充')
        worker.advance(worker.steering.followup())
        assert worker.round_ready.wait(2)
        worker.advance(None)
        assert worker.done.wait(2)
        assert worker.state == 'completed'
        worker.cancel()
        assert worker.state == 'completed'
    finally:
        worker.cancel();worker.join()


def test_cancellation_at_round_boundary_does_not_need_ui_acknowledgement():
    worker = task(multi=True)
    worker.start()
    try:
        assert worker.round_ready.wait(2)
        worker.cancel()
        assert worker.done.wait(2)
        assert worker.state == 'cancelled' and worker.error is None
    finally:
        worker.cancel();worker.join()


def test_followup_question_does_not_inherit_control_completion_from_previous_round():
    trace = [{'name': 'set_mute', 'arguments': {'muted': True}, 'ok': True}]
    def run(text, **kwargs):
        return {'reply': '已静音' if text == '任务' else '问题的答案', 'tools': trace if text == '任务' else []}
    worker = task(run, multi=True)
    worker.start()
    try:
        assert worker.round_ready.wait(2)
        assert worker.outcome['round_tools'] == trace
        worker.advance('补充问题')
        assert worker.round_ready.wait(2)
        assert worker.outcome['tools'] == trace and worker.outcome['round_tools'] == []
        worker.advance(None)
        assert worker.done.wait(2)
    finally:
        worker.cancel();worker.join()


def test_worker_exception_is_terminal_and_preserves_the_error():
    def failed(*args,**kwargs):
        raise RuntimeError('expected failure')
    worker = task(failed)
    worker.start()
    assert worker.done.wait(2)
    worker.join()
    assert worker.state == 'failed' and worker.error == 'expected failure'


@pytest.mark.parametrize('backend', ['off', 'codex', 'codebuddy'])
def test_empty_explicit_library_search_never_starts_an_agent(monkeypatch, backend):
    from resources.lib.agent_tools import KodiTools
    from resources.lib.intelligence import command_media
    from resources.lib.intelligence.media_results import SearchResult
    from resources.lib.intelligence.presentation import parse
    monkeypatch.setattr(command_media, 'search', lambda *args: SearchResult())
    tools = KodiTools(lambda *args: pytest.fail('Unexpected Kodi call'),
                      media_search_emby=False, media_search_juku=False)
    worker = TaskWorker('库里有没有完全无关的片名', {'agent_backend': backend},
        SimpleNamespace(completed=lambda rows: None), lambda options: tools,
        lambda *args: pytest.fail('An explicit search must not become an agent playback task'), {}, lambda: False)
    worker.start()
    try:
        assert worker.done.wait(2)
        assert worker.state == 'completed' and worker.error is None
        assert worker.outcome['model_invocations'] == 0 and not tools.acted
        assert parse(worker.outcome['reply'], tools)['choices'] == []
    finally:
        worker.cancel();worker.join()


def test_resident_client_cancel_callback_does_not_retain_a_finished_task():
    import gc
    import weakref
    worker = task()
    callback, reference = worker.cancelled, weakref.ref(worker)
    worker.start()
    assert worker.done.wait(2)
    worker.join()
    del worker
    gc.collect()
    assert reference() is None and callback() is False


@pytest.mark.parametrize('cancel', [False,True])
def test_request_waiter_exits_when_handler_fails_or_task_is_cancelled(cancel):
    stop,done = threading.Event(),threading.Event()
    requests = MainThreadRequests(stop.is_set)
    caught=[]
    def worker():
        try:requests.call('settings-get',None,error_type=ValueError)
        except Exception as error:caught.append(error)
        finally:done.set()
    thread=threading.Thread(target=worker);thread.start()
    request=requests.queue.get(timeout=2)
    if cancel:
        stop.set()
    else:
        def failed():raise ValueError('expected UI failure')
        requests.resolve(request,failed)
        assert request.ready.is_set()
    assert done.wait(2)
    thread.join()
    assert isinstance(caught[0],InterruptedError if cancel else ValueError)
    if not cancel:assert str(caught[0])=='expected UI failure'
