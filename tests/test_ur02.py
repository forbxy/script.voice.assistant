import importlib.util
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest

from resources.lib.audio import AudioError
from resources.lib.cmcc_audio import input_source
from resources.lib.ur02_audio import (TX, RX, CTL, MIC_OPEN, MIC_CLOSE, GET_CAPS,
                                     AudioFrames, ImaDecoder, capabilities)
from resources.lib.ur02_service import Ur02Controller


CAPS = bytes.fromhex('0b0100030000800100')
VOICE = bytes.fromhex('02cf00000000')
UP = bytes.fromhex('020000000000')


class Bus:
    def __init__(self):
        self.writes = []
        self.closed = False
    def write(self, path, value):
        assert not self.closed
        self.writes.append(value)
    def poll(self):
        return []
    def close(self):
        self.closed = True


@pytest.fixture
def controller(tmp_path):
    clock, launches, bus = [100.0], [], Bus()
    c = Ur02Controller(tmp_path, '', launches.append, bus=bus,
                       decoder_factory=lambda: ImaDecoder(native=False), clock=lambda: clock[0])
    c.paths = {TX: 'tx', RX: 'rx', CTL: 'ctl'}
    c.on_gatt('ctl', CAPS)
    c.clock_value, c.launches = clock, launches
    c.open()
    c.extra_readers = lambda: []
    yield c
    c.close()


def frame(c, value=b'\x11' * 128):
    for offset in range(0, len(value), 20):
        c.on_gatt('rx', value[offset:offset + 20])


def test_adpcm_known_samples_nibble_order_and_continuity():
    d = ImaDecoder(native=False)
    assert struct.unpack('<4h', d.decode(bytes.fromhex('17f0'))) == (1, 12, -18, -14)
    data = bytes(range(256))
    whole = ImaDecoder(native=False).decode(data)
    split = ImaDecoder(native=False)
    assert split.decode(data[:113]) + split.decode(data[113:]) == whole
    split.reset(-12, 88)
    assert struct.unpack('<2h', split.decode(b'\x7f')) == (32767, -28669)
    with pytest.raises(AudioError): split.reset(0, 89)


def test_fragment_loss_fails_instead_of_joining_different_headerless_frames():
    f = AudioFrames()
    assert all(f.feed(b'\x11' * 20) is None for _ in range(6))
    assert f.feed(b'\x22' * 8) == b'\x11' * 120 + b'\x22' * 8
    for unused in range(5): f.feed(b'\x11' * 20)
    with pytest.raises(AudioError): f.feed(b'\x22' * 8)
    assert not f.partial and f.discarded == 1
    with pytest.raises(AudioError): capabilities(bytes.fromhex('0b010001000080'))
    assert capabilities(CAPS)['frame_bytes'] == 128


@pytest.mark.parametrize('first,second', [('hid', 'ctl'), ('ctl', 'hid')])
def test_two_voice_channels_claim_once_and_release_keeps_recording(controller, first, second):
    c = controller
    actions = {'hid': lambda: c.on_report(VOICE), 'ctl': lambda: c.on_gatt('ctl', b'\x08')}
    actions[first](); actions[second](); c.on_report(UP)
    assert len(c.launches) == 1 and c.bus.writes == [MIC_OPEN]
    assert c.session.recording and not c.session.key_controlled
    c.on_gatt('ctl', bytes.fromhex('04000200'))
    for unused in range(2): frame(c)
    assert c.session.decoded == 2 and c.session.decoded_bytes == 1024
    assert len(c.session.pcm_pending) == 384
    c.clock_value[0] += 1
    actions[first](); actions[second]()
    assert not c.session.recording and c.bus.writes == [MIC_OPEN, MIC_CLOSE]
    assert not c.session.pcm_pending  # Preserve final short PCM block.


def test_timeout_and_cancel_close_microphone_and_do_not_reopen(controller):
    c = controller
    c.on_gatt('ctl', b'\x08', maximum=3)
    c.clock_value[0] += 3.1
    c.tick(timeout=0)
    assert c.bus.writes == [MIC_OPEN, MIC_CLOSE] and not c.session.recording
    c.release()
    c.clock_value[0] += 1
    c.on_gatt('ctl', b'\x08')
    c.release()
    assert c.session is None and c.bus.writes[-1] == MIC_CLOSE


def test_sync_resets_decoder_and_stream_clock_and_busy_result_cannot_open_another_capture(controller):
    c = controller
    c.on_gatt('ctl', b'\x08')
    c.clock_value[0] += 0.5
    c.on_gatt('ctl', bytes.fromhex('04000200'))
    assert c.session.last_audio == c.clock_value[0]
    c.on_gatt('ctl', bytes.fromhex('0a020001fff411'))
    assert c.session.decoder.state == (-12, 17)
    c.end_capture()
    c.clock_value[0] += 1
    c.on_gatt('ctl', b'\x08')
    assert len(c.launches) == 1 and c.bus.writes == [MIC_OPEN, MIC_CLOSE]
    assert input_source('ur02', True, True) == 'ur02'
    assert input_source('auto', True, True, True, True, True, True) == 'ur02'


def test_unsupported_remote_codec_closes_capture(controller):
    c = controller
    c.on_gatt('ctl', b'\x08')
    c.on_gatt('ctl', bytes.fromhex('04000100'))
    assert not c.session.recording and c.bus.writes[-1] == MIC_CLOSE


@pytest.mark.parametrize('preference,arrival,abort', [
    ('ur02', 3, False),
    ('ur02', None, False), ('ur02', None, True),
])
def test_f5_before_service_lease_waits_and_never_opens_an_idle_dialog(
        tmp_path, monkeypatch, preference, arrival, abort, recording_progress):
    from resources.lib import ur02_audio
    props = {'VoiceAssistant.UR02Connected': 'true'}
    waits, claims, runs = [], [], []
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == arrival:
            props['VoiceAssistant.UR02Session'] = 'c' * 32
        return abort
    addon = SimpleNamespace(getSetting=lambda key: {'v2_voice_input': preference, 'v2_recognition_engine': 'onnx'}.get(key, ''),
                            getAddonInfo=lambda key: str(tmp_path))
    home = SimpleNamespace(getProperty=lambda key: props.get(key, ''))
    for name, module in {
        'xbmc': SimpleNamespace(Monitor=lambda: SimpleNamespace(waitForAbort=wait, abortRequested=lambda: False),
                               log=lambda *args: None, LOGWARNING=2),
        'xbmcaddon': SimpleNamespace(Addon=lambda *args: addon),
        'xbmcgui': SimpleNamespace(Window=lambda *args: home),
        'xbmcvfs': SimpleNamespace(translatePath=lambda value: value),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, 'argv', ['default.py', '--voice'])
    monkeypatch.delitem(sys.modules, 'resources.lib.application', raising=False)
    spec = importlib.util.spec_from_file_location('ur02_early_f5',
        Path(__file__).resolve().parents[1] / 'default.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    class Client:
        def __init__(self, profile, token, socket_name):
            assert socket_name == 'ur02.sock'
            claims.append(token)
        def microphone(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *unused):
            props['VoiceAssistant.UR02Session'] = ''
    monkeypatch.setattr('resources.lib.inputs.pcm.Client', Client)
    monkeypatch.setattr(sys.modules['resources.lib.application'], 'capture',
        lambda addon, profile, microphone_factory=None, values=None, **kw: runs.append(microphone_factory))
    entry.main()
    assert len(waits) == (arrival if arrival else (1 if abort else 20))
    assert claims == (['c' * 32] if arrival else [])
    assert len(runs) == (1 if arrival else 0)
    if arrival:
        entry.main()  # A later F5 without a new lease must not open again.
        assert len(claims) == len(runs) == 1
