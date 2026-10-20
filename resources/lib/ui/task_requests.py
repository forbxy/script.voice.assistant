"""Explicit worker requests; the UI thread alone resolves their side effects."""
import queue
import threading


class Request:
    def __init__(self, kind, payload):
        self.kind, self.payload = kind, payload
        self.ready = threading.Event()
        self.value = None
        self.error = None


class MainThreadRequests:
    def __init__(self, cancelled):
        self.cancelled = cancelled
        self.queue = queue.Queue()

    def call(self, kind, payload=None, error_type=RuntimeError, allow_completed=False):
        if self.cancelled():
            raise InterruptedError('已取消。')
        request = Request(kind, payload)
        self.queue.put(request)
        while not request.ready.wait(.05):
            if self.cancelled():
                raise InterruptedError('已取消。')
        if request.error is not None:
            raise error_type(request.error)
        if self.cancelled() and not (allow_completed and request.value):
            raise InterruptedError('已取消。')
        return request.value

    def take(self):
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    @staticmethod
    def resolve(request, operation):
        try:
            request.value = operation()
        except Exception as error:
            request.error = str(error)
        finally:
            request.ready.set()

    def confirm(self, title, message):
        return bool(self.call('confirm', (title, message)))

    def settings_get(self, query):
        return self.call('settings-get', query, error_type=ValueError)

    def settings_set(self, setting, value):
        return self.call('settings-set', (setting, value), error_type=ValueError)
