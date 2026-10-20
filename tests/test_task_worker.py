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


def test_worker_exception_is_terminal_and_preserves_the_error():
    def failed(*args,**kwargs):
        raise RuntimeError('expected failure')
    worker = task(failed)
    worker.start()
    assert worker.done.wait(2)
    worker.join()
    assert worker.state == 'failed' and worker.error == 'expected failure'


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
