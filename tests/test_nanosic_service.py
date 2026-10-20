import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from resources.lib.audio import AudioError
from resources.lib import cmcc_audio, nanosic_audio, nanosic_service, ur02_audio
from resources.lib.cmcc_audio import input_source
from resources.lib.nanosic_service import DOWN, UP, NanosicController
from test_nanosic import START, STOP, packets
from test_voice import audio


class Microphone:
    def __init__(self):
        self.writes = []

    def write(self, enabled):
        self.writes.append(enabled)


class Decoder:
    def __init__(self, failures):
        self.failures = failures
        self.closed = False

    def decode(self, frame):
        assert not self.closed
        if self.failures[0]:
            self.failures[0] -= 1
            raise AudioError('bad startup frame')
        return audio(700, 0.02)

    def close(self):
        self.closed = True


@pytest.fixture
def controller(tmp_path):
    clock, failures = [100.0], [0]
    microphone, launches = Microphone(), []
    decoders = []
    def factory():
        decoder = Decoder(failures)
        decoders.append(decoder)
        return decoder
    c = NanosicController(tmp_path, '', launches.append, decoder_factory=factory,
                          microphone=microphone, clock=lambda: clock[0])
    c.clock_value, c.failures, c.decoders, c.launches = clock, failures, decoders, launches
    c.open()
    yield c
    c.close()


def send_frame(c, sequence):
    for p in packets(sequence, bytes(range(60))):
        c.on_report(p)


def test_tap_reopens_after_physical_stop_and_ignores_feedback_f5(controller):
    c = controller
    c.on_report(START)
    c.on_report(DOWN)
    c.on_report(START)  # Host feedback must not create another lease/window.
    c.on_report(DOWN)
    send_frame(c, 5)
    c.on_report(STOP)
    c.on_report(UP)
    c.on_report(UP)
    assert c.session.recording and len(c.launches) == 1
    c.clock_value[0] += 0.06
    c.tick(timeout=0)
    assert c.microphone.writes == [True]
    # Continued audio may arrive without another start marker.
    send_frame(c, 6)
    assert c.session.decoded == 2
    c.on_report(START)
    c.on_report(DOWN)  # Command-induced F5 cannot end the recording.
    assert c.session.recording and len(c.launches) == 1


def test_cancel_before_scheduled_reopen_cannot_turn_microphone_back_on(controller):
    c = controller
    c.on_report(START); c.on_report(DOWN); c.on_report(STOP); c.on_report(UP)
    c.end_capture()
    c.clock_value[0] += 0.1
    c.tick(timeout=0)
    assert c.microphone.writes == [False]
    assert not c.session.recording
    c.release()
    c.on_report(START)  # A delayed vendor start is suppressed during cooldown.
    assert len(c.launches) == 1 and c.session is None


def test_hold_mode_ends_on_release_without_reopening(controller):
    c = controller
    c.activation = 'hold'
    c.on_report(START); c.on_report(DOWN)
    send_frame(c, 1)
    c.on_report(STOP); c.on_report(UP)
    c.clock_value[0] += 0.1
    c.tick(timeout=0)
    assert c.session.key_controlled and not c.session.recording
    assert c.microphone.writes == [False]


def test_isolated_bad_frame_is_dropped_but_sustained_failure_closes_microphone(controller):
    c = controller
    c.on_report(START)
    c.failures[0] = 1
    send_frame(c, 1)
    send_frame(c, 2)
    assert c.session.recording and c.session.decoded == 1 and c.decode_errors == 1
    c.failures[0] = 5
    for i in range(3, 8):
        send_frame(c, i)
    assert not c.session.recording and c.microphone.writes == [False]


def test_maximum_duration_wins_over_pending_reopen(controller):
    c = controller
    c.on_report(START, maximum=3); c.on_report(DOWN); c.on_report(UP)
    c.clock_value[0] += 3.01
    c.tick(timeout=0)
    assert c.microphone.writes == [False] and not c.session.recording


def test_gatt_control_resolves_audio_uuid_and_sends_body_not_hid_report(monkeypatch):
    prefix = '/org/bluez/hci0/dev_54_03_84_FD_38_DF/'
    calls = []
    objects = {
        prefix + 'service004f/char0053': {'org.bluez.GattCharacteristic1':
            {'UUID': {'data': '5f74756f-7265-6174-6f63-69736f6e616e'}}},
        prefix + 'service0099/char009d': {'org.bluez.GattCharacteristic1':
            {'UUID': {'data': nanosic_audio.AUDIO_OUT_UUID}}}}
    def run(command, **unused):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'data': [objects]}), stderr='')
    monkeypatch.setattr(nanosic_service.subprocess, 'run', run)
    microphone = nanosic_service.GattMicrophone('54:03:84:fd:38:df')
    microphone.write(True); microphone.write(False)
    assert microphone.path == prefix + 'service0099/char009d'
    assert calls[1][-13:] == ['8', '90', '1', '0', '255', '255', '255', '0', '0',
                             '1', 'type', 's', 'request']
    assert calls[2][-13:][2] == '0'
    assert 'service004f' not in ' '.join(calls[1])


def test_selected_input_routes_each_pending_remote_and_rejects_stale_f5(tmp_path, monkeypatch, recording_progress):
    props = {'VoiceAssistant.NanosicConnected': 'true',
             'VoiceAssistant.CMCCConnected': 'true',
             'VoiceAssistant.NanosicSession': 'b' * 32}
    settings = {'voice_input': 'nanosic', 'recognition_engine': 'onnx'}
    addon = SimpleNamespace(getSetting=lambda key: settings.get(key.removeprefix('v2_'), ''),
                            getAddonInfo=lambda key: str(tmp_path))
    home = SimpleNamespace(getProperty=lambda key: props.get(key, ''))
    for name, module in {
        'xbmc': SimpleNamespace(log=lambda *args: None, LOGWARNING=2,
                               Monitor=lambda: SimpleNamespace(waitForAbort=lambda seconds: False, abortRequested=lambda: False)),
        'xbmcaddon': SimpleNamespace(Addon=lambda *args: addon),
        'xbmcgui': SimpleNamespace(Window=lambda *args: home),
        'xbmcvfs': SimpleNamespace(translatePath=lambda value: value),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, 'argv', ['default.py', '--voice'])
    monkeypatch.delitem(sys.modules, 'resources.lib.application', raising=False)
    spec = importlib.util.spec_from_file_location('nanosic_f5', Path(__file__).resolve().parents[1] / 'default.py')
    entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)
    runs = []
    class Client:
        def __init__(self, profile, token, socket_name):
            self.source = {'a': 'cmcc', 'b': 'nanosic', 'c': 'ur02'}[token[0]]
            assert socket_name == self.source + '.sock'
        def microphone(self, unused=''):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *unused):
            props['VoiceAssistant.' + {'nanosic': 'Nanosic', 'cmcc': 'CMCC', 'ur02': 'UR02'}[self.source] + 'Session'] = ''
    monkeypatch.setattr('resources.lib.inputs.pcm.Client', Client)
    monkeypatch.setattr(sys.modules['resources.lib.application'], 'capture',
        lambda addon, profile, microphone_factory=None, values=None, **kw: runs.append(microphone_factory.__self__.source))
    entry.main(); entry.main()
    assert runs == ['nanosic']
    settings['voice_input'] = 'cmcc'
    props['VoiceAssistant.CMCCSession'] = 'a' * 32
    entry.main(); entry.main()
    assert runs == ['nanosic', 'cmcc']
    settings['voice_input'] = 'ur02'
    props['VoiceAssistant.UR02Session'] = 'c' * 32
    props['VoiceAssistant.UR02Connected'] = 'true'
    entry.main(); entry.main()
    assert runs == ['nanosic', 'cmcc', 'ur02']
    settings['voice_input'] = 'nanosic'
    props['VoiceAssistant.NanosicSession'] = 'b' * 32
    entry.main()
    assert runs[-1] == 'nanosic'  # The pending key overrides connected-source priority.
    assert input_source('nanosic', True, True, True, False) == 'nanosic'
