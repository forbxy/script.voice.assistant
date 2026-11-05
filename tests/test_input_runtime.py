"""Exercise the service loop, including Kodi's positive-timeout requirement."""
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace


def test_idle_service_processes_selected_input_settings_changes_and_shutdown(tmp_path, monkeypatch):
    clock, cycles, settings_reads, polled = [10.0], [], [], []
    inputs, properties = {}, {}
    values = {'voice_input': 'nanosic', 'max_seconds': 30, 'media_search_juku': 'on'}
    root = Path(__file__).resolve().parents[1]

    class Monitor:
        def abortRequested(self): return False
        def waitForAbort(self, timeout):
            # Real Kodi treats zero as infinite; such a loop never services HID.
            assert timeout > 0
            cycles.append(timeout)
            return len(cycles) > 4

    home = SimpleNamespace(getProperty=lambda name: properties.get(name, ''),
        setProperty=lambda name, value: properties.update({name: value}),
        clearProperty=lambda name: properties.pop(name, None))
    addon = SimpleNamespace(getAddonInfo=lambda name: {'path': str(root),
        'profile': str(tmp_path), 'version': 'test'}[name])
    class Settings:
        def __init__(self, addon): pass
        def values(self): settings_reads.append(True); return dict(values)

    class Input:
        def __init__(self, source):
            self.source = source
            self.session, self.peers, self.hid = None, {}, None
            self.status, self.error = 'disconnected', ''
            self.ticks, self.detached, self.closed = 0, False, False
            inputs[source] = self
        def open(self): pass
        def attach(self, device): self.hid = 123; self.status = 'ready'
        def detach(self, reason): self.hid = None; self.detached = True
        def tick(self, maximum, timeout): self.ticks += 1
        def close(self): self.closed = True

    class Dependencies:
        def __init__(self, *args):
            self.lock, self.runtime = threading.RLock(), {}
            self.web_saved = True
        def state(self, values): return {'missing': []}
        def close(self): pass

    class Web:
        def __init__(self, *args, **kwargs):
            self.http, self.lock = SimpleNamespace(server_port=8767), threading.RLock()
            self.snapshot = {'settings': dict(values), 'logged_in': False}
            self.input_changed, self.calls = False, 0
        def pump(self):
            self.calls += 1
            if self.calls == 2:
                self.snapshot['settings'] = dict(values, voice_input='usb')
                self.input_changed = True
        def close(self): pass

    class BackgroundService:
        def __init__(self, *args, **kwargs):
            self.status = {'state': 'ready'}
            self.engine = SimpleNamespace(ready=threading.Event())
            self.current, self.enabled = None, False
        def tick(self, *args, **kwargs): pass
        def close(self): pass

    noop = lambda *args, **kwargs: None
    modules = {
        'xbmc': SimpleNamespace(Monitor=Monitor, getCondVisibility=lambda name: False,
            log=noop, LOGWARNING=1, LOGERROR=2, executebuiltin=noop,
            audioSuspend=noop, audioResume=noop),
        'xbmcaddon': SimpleNamespace(Addon=lambda name: addon),
        'xbmcgui': SimpleNamespace(Window=lambda number: home),
        'xbmcvfs': SimpleNamespace(translatePath=lambda path: path),
        'resources.lib.profile_upgrade': SimpleNamespace(prepare_profile=lambda *a, **k: True),
        'resources.lib.application': SimpleNamespace(rpc=noop),
        'resources.lib.intelligence.library_changes': SimpleNamespace(LibraryChanges=BackgroundService),
        'resources.lib.configuration.server': SimpleNamespace(Configuration=Web, DEFAULT_PORT=8767),
        'resources.lib.dependencies.service': SimpleNamespace(Manager=Dependencies),
        'resources.lib.asr.service': SimpleNamespace(Service=BackgroundService),
        'resources.lib.intelligence.resident': SimpleNamespace(Resident=BackgroundService),
        'resources.lib.codebuddy_resident': SimpleNamespace(Resident=BackgroundService),
        'resources.lib.speech.service': SimpleNamespace(Service=BackgroundService),
        'resources.lib.speech.output': SimpleNamespace(AlsaOutput=noop),
        'resources.lib.intelligence.juku_tracking': SimpleNamespace(create=lambda *args: BackgroundService()),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = root/'resources/lib/inputs/runtime.py'
    spec = importlib.util.spec_from_file_location('resources.lib.inputs.runtime_check', path)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    monkeypatch.setattr(runtime, 'Settings', Settings)
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock[0])
    def wait_inputs(controllers, timeout):
        polled.append([controller.source for controller in controllers])
        clock[0] += max(.1, timeout)
    monkeypatch.setattr(runtime, 'wait_inputs', wait_inputs)
    for source, name in [('cmcc', 'Controller'), ('nanosic', 'NanosicController'),
                         ('ur02', 'Ur02Controller'), ('r22', 'R22Controller'), ('b25', 'B25Controller')]:
        monkeypatch.setattr(runtime, name, lambda *a, source=source, **k: Input(source))
        monkeypatch.setattr(runtime, 'find_'+source, lambda: '/dev/test')
    runtime.main()
    assert len(settings_reads) == 1  # Later settings come from the shared web owner.
    assert polled[0] == ['nanosic'] and polled[1:] == [[], [], []]
    assert inputs['nanosic'].ticks == 1 and inputs['nanosic'].detached
    assert not any(controller.ticks for source, controller in inputs.items() if source != 'nanosic')
    assert all(controller.closed for controller in inputs.values())
    assert not (tmp_path/'runtime/cmcc-stop.flag').exists()
