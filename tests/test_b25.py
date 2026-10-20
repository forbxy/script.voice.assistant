import struct
import threading
import time
from types import SimpleNamespace

import pytest

from resources.lib.b25_audio import B25Client
from resources.lib.b25_service import B25Controller, DOWN, UP, find_remote
from resources.lib.inputs.registry import claim
from resources.lib.ur02_audio import CTL, RX, TX, MIC_OPEN, MIC_CLOSE, ImaDecoder
from resources.lib.settings import validate

CAPS = bytes.fromhex('0b0100030000780000')
START = bytes.fromhex('04000200')


class Bus:
    def __init__(self):
        self.writes = []
    def write(self, path, data):
        self.writes.append(data)
    def poll(self):
        return []
    def close(self):
        pass


@pytest.fixture
def controller(tmp_path):
    now, launches = [100.0], []
    c = B25Controller(tmp_path, '', launches.append, bus=Bus(),
                      decoder_factory=lambda: ImaDecoder(native=False), clock=lambda: now[0])
    c.paths = {CTL: 'ctl', RX: 'rx', TX: 'tx'}
    c.on_gatt('ctl', CAPS)
    c.extra_readers = lambda: []
    c.now, c.launches = now, launches
    c.open()
    yield c
    c.close()


@pytest.mark.parametrize('name,product', [('B21', '00000006'), ('B25', '0000000A')])
def test_discovery_checks_hid_identity_without_fixed_address(tmp_path, name, product):
    p = tmp_path / 'hidraw27/device'
    p.mkdir(parents=True)
    (p / 'uevent').write_text('HID_NAME='+name+'\nHID_ID=0005:00000957:'+product+'\n')
    assert find_remote(tmp_path) == '/dev/hidraw27'
    (p / 'uevent').write_text('HID_NAME=B25\nHID_ID=0005:00000957:0000000B\n')
    assert find_remote(tmp_path) is None
    assert validate('voice_input', 'b25') == 'b25'


@pytest.mark.parametrize('name,identity,expected', [
    ('RemoteG11', '0005:00000957:0000000A', '/dev/hidraw27'),
    ('RemoteG11', '0005:00001234:00005678', '/dev/hidraw27'),
    ('RemoteG11', '0003:00000957:0000000A', None),
    ('RemoteG110', '0005:00000957:0000000A', None),
    ('RemoteG11', '', None),
])
def test_remoteg11_matches_only_its_exact_bluetooth_name(tmp_path, name, identity, expected):
    device = tmp_path / 'hidraw27/device'
    device.mkdir(parents=True)
    (device / 'uevent').write_text('HID_NAME=' + name + '\nHID_ID=' + identity + '\n')
    assert find_remote(tmp_path) == expected


@pytest.mark.parametrize('first', ['ctl', 'hid'])
def test_dual_trigger_once_and_instant_release_keeps_tap_audio(controller, first):
    c = controller
    actions = {'ctl': lambda: c.on_gatt('ctl', b'\x08'), 'hid': lambda: c.on_report(DOWN)}
    actions[first]()
    actions['hid' if first == 'ctl' else 'ctl']()
    c.on_report(UP)
    assert c.session.recording and not c.session.key_controlled
    assert len(c.launches) == 1 and c.bus.writes == [MIC_OPEN]
    c.on_gatt('ctl', START)
    c.on_gatt('rx', b'\x17' * 120)
    c.on_gatt('rx', b'\xf0' * 120)
    assert c.session.decoded_bytes == 960
    assert c.session.decoded == 2
    c.now[0] += 1
    actions[first]()
    actions['hid' if first == 'ctl' else 'ctl']()
    assert not c.session.recording and c.bus.writes == [MIC_OPEN, MIC_CLOSE]
    assert not c.session.pcm_pending
    assert c.extra_statistics()['bluetooth_remote'] == 'Homatics'


def test_timeout_cancel_and_next_utterance(controller):
    c = controller
    c.on_gatt('ctl', b'\x08', maximum=3)
    c.now[0] += 3.1
    c.tick(timeout=0)
    assert not c.session.recording and c.bus.writes[-1] == MIC_CLOSE
    c.release()
    c.now[0] += 1
    c.on_gatt('ctl', b'\x08')
    assert len(c.launches) == 2 and c.session.recording
    c.release()
    c.on_gatt('rx', b'\x17' * 120)
    assert c.session is None and c.bus.writes[-1] == MIC_CLOSE


def test_socket_manual_finish_preserves_final_partial_pcm_and_source(controller):
    c = controller
    c.on_gatt('ctl', b'\x08')
    c.on_gatt('ctl', START)
    c.on_gatt('rx', b'\x17' * 120)
    c.on_gatt('rx', b'\xf0' * 120)
    running = threading.Event()
    running.set()
    thread = threading.Thread(target=lambda: pump(c, running))
    thread.start()
    try:
        with B25Client(c.profile, c.session.token) as client:
            assert client.input_source == 'b25' and not client.key_controlled
            c.end_capture()
            chunks = []
            while not client.finished:
                chunks.append(client.read())
            assert b''.join(chunks) == ImaDecoder(native=False).decode(b'\x17'*120+b'\xf0'*120)
            assert client.statistics['bluetooth_remote'] == 'Homatics'
        deadline = time.monotonic() + 1
        while c.session and time.monotonic() < deadline:
            time.sleep(.01)
        assert c.session is None
        c.now[0] += 1
        c.on_gatt('ctl', b'\x08')
        with B25Client(c.profile, c.session.token) as client:
            client.stop_capture()  # OK/silence closes remote before releasing lease.
            assert client.finished and not c.session.recording
            assert c.bus.writes[-1] == MIC_CLOSE
    finally:
        running.clear()
        thread.join()


def test_finish_during_socket_send_keeps_tail_without_exporting_mutable_queue(controller, monkeypatch):
    from resources.lib import cmcc_service
    c = controller
    c.on_gatt('ctl', b'\x08')
    c.on_gatt('ctl', START)
    c.on_gatt('rx', b'\x17' * 120)
    c.on_gatt('rx', b'\xf0' * 120)
    initial, tail = bytes(c.session.outgoing), bytes(c.session.pcm_pending)
    assert initial and tail
    class Peer:
        def send(self, data):
            # A real socket holds this export while the GIL is released.
            with memoryview(data) as exported:
                c.end_capture()
                assert bytes(exported) == initial
                return len(exported)
        def close(self): pass
    peer = Peer()
    c.session.peer = peer
    monkeypatch.setattr(cmcc_service, 'wait', lambda readers, writers, *args: ([], writers, []))
    c.tick(timeout=0)
    from resources.lib.cmcc_audio import message
    assert not c.session.recording and not c.session.pcm_pending
    assert c.session.outgoing.startswith(message(b'A', tail))


def pump(controller, running):
    while running.is_set():
        controller.tick(timeout=.001)


def test_bound_key_before_lease_waits_for_b25_client(tmp_path, monkeypatch):
    properties, calls, waits = {}, [], []
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            properties['VoiceAssistant.B25Session'] = 'b' * 32
        return False
    class Client:
        def __init__(self, profile, token, socket_name):
            assert socket_name == 'b25.sock'
            calls.append(token)
        def __enter__(self):
            return SimpleNamespace(microphone='b25-microphone')
        def __exit__(self, *unused):
            pass
    monkeypatch.setattr('resources.lib.inputs.pcm.Client', Client)
    with claim(tmp_path, 'b25', SimpleNamespace(getProperty=lambda k: properties.get(k, '')),
               SimpleNamespace(waitForAbort=wait)) as microphone:
        assert microphone == 'b25-microphone'
    assert calls == ['b' * 32] and len(waits) == 3
