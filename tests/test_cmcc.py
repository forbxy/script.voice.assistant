from resources.lib.data_paths import data_path
import fcntl
import importlib.util
import json
from pathlib import Path
import socket
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from resources.lib.audio import AudioError, SpeechGate, record
from resources.lib import cmcc_audio
from resources.lib.cmcc_audio import CmccClient, Frames, input_source, message
from resources.lib.cmcc_service import Controller, find_remote
from test_voice import Source, audio

DOWN = bytes.fromhex('f88203010000000000')
UP = bytes.fromhex('f88203000000000000')


def packet(sequence, fragment, data):
    return b'\xfc' + struct.pack('<HBB', sequence, fragment, 0) + data


def test_hid_fragments_recover_after_loss_and_sequence_wrap():
    frames = Frames()
    fragments = [bytes([index]) * 16 for index in (1, 2, 3)]
    assert frames.feed(packet(65535, 0, fragments[0])) is None
    assert frames.feed(packet(65535, 2, fragments[2])) is None
    assert frames.feed(packet(65535, 1, fragments[1])) is None
    assert frames.feed(packet(0, 0, fragments[0])) is None
    assert frames.feed(packet(0, 1, fragments[1])) is None
    assert frames.feed(packet(0, 2, fragments[2])) == b''.join(fragments)[:40]
    assert frames.discarded == 2
    assert frames.feed(b'\xfc\x00') is None


def test_discovery_matches_identity_and_not_a_hidraw_number(tmp_path):
    device = tmp_path / 'hidraw17/device'
    device.mkdir(parents=True)
    (device / 'uevent').write_text('HID_ID=0005:00000416:00000300\nHID_NAME=CMCC_Voice_Remote\n')
    assert find_remote(tmp_path) == '/dev/hidraw17'
    (device / 'uevent').write_text('HID_ID=0005:00000416:00000300\nHID_NAME=CMCC_Voice_Remote_fake\n')
    assert find_remote(tmp_path) is None


class Decoder:
    def __init__(self):
        self.closed = False

    def decode(self, frame):
        assert not self.closed
        return audio(800, 0.02)

    def close(self):
        self.closed = True


@pytest.fixture
def controller(tmp_path):
    launches, outputs = [], []
    decoders = []
    def factory():
        decoders.append(Decoder())
        return decoders[-1]
    value = Controller(tmp_path, '', launches.append, decoder_factory=factory, activation='hold')
    value._output = outputs.append
    value.open()
    value.launches, value.outputs, value.decoders = launches, outputs, decoders
    yield value
    value.close()


def feed_audio(controller, frames=10):
    for sequence in range(frames):
        for fragment in range(3):
            controller.on_report(packet(sequence, fragment, bytes(16)))


def lock_busy(profile):
    with (data_path(profile, 'capture.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def start_loop(controller):
    stop = threading.Event()
    thread = threading.Thread(target=lambda: loop(controller, stop))
    thread.start()
    return stop, thread


def loop(controller, stop):
    while not stop.is_set():
        controller.tick(timeout=0.005)


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_voice_buffers_first_audio_and_lease_suppresses_f5_until_result_closes(controller):
    controller.on_report(DOWN)
    controller.on_report(DOWN)  # Duplicate report cannot spawn another capture.
    controller.on_report(b'\x01\x00\x00\x3e\x00\x00\x00\x00')
    feed_audio(controller)
    controller.on_report(UP)  # User may finish before Kodi has loaded the model.
    assert controller.outputs == [True, False]
    assert len(controller.launches) == 1 and lock_busy(controller.profile)
    stop, thread = start_loop(controller)
    client = None
    try:
        client = CmccClient(controller.profile, controller.launches[0])
        with client.microphone() as source:
            pcm = record(source, SpeechGate())
        assert pcm == audio(800, 0.02) * 10
        assert client.statistics['bluetooth_frames'] == 10
        assert lock_busy(controller.profile)  # Recording ended; AI/result still owns the lease.
        controller.on_report(DOWN)
        assert len(controller.launches) == 1
        client.close()
        wait_for(lambda: controller.session is None)
        assert not lock_busy(controller.profile)
    finally:
        if client:
            client.close()
        stop.set(); thread.join()


def test_high_numbered_server_and_client_descriptors_keep_capture_service_running(controller, monkeypatch):
    import resources.lib.inputs.pcm as pcm
    original_socket = socket.socket
    def high_socket(*args):
        connection = original_socket(*args)
        try:
            fd = fcntl.fcntl(connection, fcntl.F_DUPFD_CLOEXEC, 2048)
        finally:
            connection.close()
        return original_socket(fileno=fd)
    fd = fcntl.fcntl(controller.listener, fcntl.F_DUPFD_CLOEXEC, 2048)
    controller.listener.close()
    controller.listener = original_socket(fileno=fd)
    controller.listener.setblocking(False)
    monkeypatch.setattr(pcm, 'socket', SimpleNamespace(socket=high_socket,
        AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM))
    controller.on_report(DOWN)
    feed_audio(controller, frames=10)
    controller.on_report(UP)
    stop, thread = start_loop(controller)
    try:
        with CmccClient(controller.profile, controller.launches[0]) as client:
            assert controller.listener.fileno() >= 2048 and client.socket.fileno() >= 2048
            with client.microphone() as source:
                assert record(source, SpeechGate()) == audio(800, .02) * 10
            assert client.statistics['bluetooth_frames'] == 10
        wait_for(lambda:controller.session is None)
        assert thread.is_alive() and not lock_busy(controller.profile)
    finally:
        stop.set(); thread.join()


def test_usb_capture_already_owns_lock_so_cmcc_never_starts_microphone(controller):
    with (data_path(controller.profile, 'capture.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        controller.on_report(DOWN)
        controller.on_report(UP)
    assert not controller.outputs and not controller.launches


def test_cancel_and_client_crash_stop_microphone_and_release_capture_lock(controller):
    controller.on_report(DOWN)
    stop, thread = start_loop(controller)
    client = None
    try:
        client = CmccClient(controller.profile, controller.launches[0])
        client.stop_capture()
        wait_for(lambda: controller.outputs == [True, False])
        assert lock_busy(controller.profile)
        client.close()
        wait_for(lambda: controller.session is None)
        assert not lock_busy(controller.profile)
        assert controller.decoders[0].closed
    finally:
        if client:
            client.close()
        stop.set(); thread.join()


def test_missing_kodi_client_and_watchdog_stop_recording(controller):
    clock = [100.0]
    controller.clock = lambda: clock[0]
    controller.on_report(DOWN, maximum=3)
    clock[0] += 3.01
    controller.tick(timeout=0)
    assert controller.outputs == [True, False]
    clock[0] += 2.01
    controller.tick(timeout=0)
    assert controller.session is None and not lock_busy(controller.profile)


def test_disconnect_stops_audio_and_informs_attached_capture(controller):
    controller.on_report(DOWN)
    stop, thread = start_loop(controller)
    client = None
    try:
        client = CmccClient(controller.profile, controller.launches[0])
        controller.detach()
        with pytest.raises(AudioError, match='连接已断开'):
            while not client.read():
                pass
    finally:
        if client:
            client.close()
        stop.set(); thread.join()
    assert controller.outputs == [True, False]


def test_bad_socket_token_cannot_steal_or_cancel_recording(controller):
    controller.on_report(DOWN)
    stop, thread = start_loop(controller)
    try:
        with pytest.raises(AudioError):
            CmccClient(controller.profile, 'a' * 32)
        assert controller.session and controller.session.recording
        assert lock_busy(controller.profile)
    finally:
        stop.set(); thread.join()


def test_transport_handles_split_headers_and_rejects_oversized_messages():
    client = CmccClient.__new__(CmccClient)
    client.socket, writer = socket.socketpair()
    client.socket.setblocking(False)
    client.buffer = bytearray()
    try:
        encoded = message(b'A', bytes(640))
        writer.sendall(encoded[:3])
        assert client._receive(0) is None
        writer.sendall(encoded[3:])
        assert client._receive(0.1) == (b'A', bytes(640))
        writer.sendall(struct.pack('!cI', b'A', 10000000))
        with pytest.raises(AudioError):
            client._receive(0.1)
    finally:
        client.close(); writer.close()


def test_physical_key_release_ends_without_waiting_for_silence_or_losing_pause():
    class KeySource(Source):
        key_controlled = True
        eof = False
        def read(self):
            block = next(self.chunks, None)
            if block is None:
                self.eof = True
                return b''
            return block
    chunks = [audio(800, 0.2), audio(0, 1.2), audio(800, 0.2)]
    result = record(KeySource(chunks), SpeechGate(silence_seconds=0.5))
    assert result == b''.join(chunks)


def test_decoder_failure_always_sends_stop_and_releases_unclaimed_lock(controller):
    controller.on_report(DOWN)
    controller.decoders[0].decode = lambda unused: (_ for _ in ()).throw(AudioError('decoder error'))
    feed_audio(controller, 1)
    assert controller.outputs == [True, False]
    assert not controller.session.recording
    assert controller.decoders[0].closed
    controller.release()
    assert not lock_busy(controller.profile)


def test_pending_session_is_published_before_start_can_generate_f5(controller):
    def output(enabled):
        if enabled:
            assert controller.launches == [controller.session.token]
            assert lock_busy(controller.profile)
        controller.outputs.append(enabled)
    controller._output = output
    controller.on_report(DOWN)
    assert controller.outputs == [True]


def test_tap_keeps_audio_after_release_and_second_tap_ends_once(controller):
    controller.activation = 'tap'
    controller.on_report(DOWN)
    controller.on_report(DOWN)  # Repeated report during the same physical press.
    controller.on_report(UP)
    feed_audio(controller, 3)
    assert controller.session.recording
    assert controller.session.decoded == 3
    assert not controller.session.key_controlled
    assert controller.outputs == [True]
    controller.on_report(DOWN)
    controller.on_report(DOWN)
    controller.on_report(UP)
    assert not controller.session.recording
    assert len(controller.launches) == 1
    assert controller.outputs == [True, False]


def test_tap_client_stops_on_silence_keeps_lease_and_allows_next_session(controller):
    controller.activation = 'tap'
    controller.on_report(DOWN)
    controller.on_report(UP)
    feed_audio(controller, 10)
    controller.decoders[0].decode = lambda unused: audio(0, 0.02)
    feed_audio(controller, 30)
    stop, thread = start_loop(controller)
    try:
        with CmccClient(controller.profile, controller.launches[0]) as client:
            with client.microphone() as microphone:
                assert not microphone.key_controlled
                pcm = record(microphone, SpeechGate(silence_seconds=0.5))
            assert len(pcm) == 22400  # 0.2 s speech + 0.5 s silence.
            wait_for(lambda: not controller.session.recording)
            assert client.statistics['bluetooth_frames'] == 40
            assert lock_busy(controller.profile)
            controller.on_report(DOWN)  # Result is still open; no new capture.
            controller.on_report(UP)
            assert len(controller.launches) == 1
        wait_for(lambda: controller.session is None)
        assert not lock_busy(controller.profile)
        controller.on_report(DOWN)
        controller.on_report(UP)
        assert len(controller.launches) == 2
        assert controller.session.recording
    finally:
        stop.set(); thread.join()


def test_source_selection_never_falls_back_to_usb_when_cmcc_is_selected():
    assert input_source('cmcc', True, False) == 'cmcc'
    assert input_source('auto', True, True) == 'cmcc'
    assert input_source('auto', True, False) == 'usb'
    assert input_source('usb', True, True) == 'usb'


def test_normal_f5_claims_one_session_and_late_f5_cannot_open_usb(tmp_path, monkeypatch, recording_progress):
    properties = {'VoiceAssistant.CMCCConnected': 'true', 'VoiceAssistant.CMCCSession': 'a' * 32}
    settings = {'voice_input': 'cmcc', 'recognition_engine': 'onnx'}
    addon = SimpleNamespace(getSetting=lambda key: settings.get(key.removeprefix('v2_'), ''),
                            getAddonInfo=lambda key: str(tmp_path))
    home = SimpleNamespace(getProperty=lambda key: properties.get(key, ''))
    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace(log=lambda *args: None, LOGWARNING=2, LOGERROR=3,
        Monitor=lambda: SimpleNamespace(waitForAbort=lambda seconds: False, abortRequested=lambda: False)))
    monkeypatch.setitem(sys.modules, 'xbmcaddon', SimpleNamespace(Addon=lambda *args: addon))
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace(Window=lambda *args: home))
    monkeypatch.setitem(sys.modules, 'xbmcvfs', SimpleNamespace(translatePath=lambda path: path))
    monkeypatch.setattr(sys, 'argv', ['default.py', '--voice'])
    monkeypatch.delitem(sys.modules, 'resources.lib.application', raising=False)
    spec = importlib.util.spec_from_file_location('cmcc_f5_route', Path(__file__).resolve().parents[1] / 'default.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    claimed = [False]
    clients = []
    class Client:
        def __init__(self, profile, token, socket_name):
            assert profile == tmp_path and token == 'a' * 32
            assert socket_name == 'cmcc.sock'
            clients.append(token)
            if claimed[0]:
                raise AudioError('session already claimed')
            claimed[0] = True
        def microphone(self, unused=''):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *unused):
            properties['VoiceAssistant.CMCCSession'] = ''
    monkeypatch.setattr('resources.lib.inputs.pcm.Client', Client)
    runs = []
    def run(addon, profile, microphone_factory=None):
        assert microphone_factory is not None
        runs.append('cmcc')
        entry.main()  # Repeated F5 while the first result is still open.
    application = sys.modules['resources.lib.application']
    monkeypatch.setattr(application, 'capture', lambda addon, profile, factory=None, values=None, **kw: run(addon, profile, factory))
    entry.main()
    entry.main()  # Deferred F5 after the first result closed.
    entry.main()
    properties['VoiceAssistant.CMCCConnected'] = 'false'
    entry.main()  # Fixed CMCC source never switches a stale F5 to USB.
    assert runs == ['cmcc']
    assert clients == ['a' * 32]  # Busy UI rejected before even opening a socket.
    properties['VoiceAssistant.CMCCConnected'] = 'true'
    properties['VoiceAssistant.CMCCSession'] = 'a' * 32
    claimed[0] = False
    entry.main()  # A fresh second voice session can run after the first exits.
    assert runs == ['cmcc', 'cmcc']
    assert not (data_path(tmp_path, 'capture.lock')).exists()


def test_idle_f5_does_not_import_recognition_backends(tmp_path, monkeypatch):
    import builtins
    addon = SimpleNamespace(getSetting=lambda key: 'cmcc' if key == 'v2_voice_input' else '',
                            getAddonInfo=lambda key: str(tmp_path))
    home = SimpleNamespace(getProperty=lambda key: '')
    for name, module in {
        'xbmc': SimpleNamespace(Monitor=lambda: SimpleNamespace(waitForAbort=lambda seconds: False, abortRequested=lambda: False)), 'xbmcaddon': SimpleNamespace(Addon=lambda *args: addon),
        'xbmcgui': SimpleNamespace(Window=lambda *args: home),
        'xbmcvfs': SimpleNamespace(translatePath=lambda path: path),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, 'argv', ['default.py', '--voice'])
    original = builtins.__import__
    def import_guard(name, *args, **kwargs):
        assert name not in {'resources.lib.engines', 'resources.lib.ncnn_recognizer',
                            'resources.lib.hotwords', 'resources.lib.streaming'}
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', import_guard)
    monkeypatch.delitem(sys.modules, 'resources.lib.application', raising=False)
    spec = importlib.util.spec_from_file_location('cmcc_idle_check', Path(__file__).resolve().parents[1] / 'default.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    entry.main()
