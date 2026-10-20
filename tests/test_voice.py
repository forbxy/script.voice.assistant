import struct

from resources.lib import audio as capture_module

from resources.lib.audio import RATE, SpeechGate, record, rms, usb_capture_devices
from legacy.recognizer import VoskRecognizer, display_text


def audio(level, duration=0.1):
    # Alternating samples have energy without introducing a DC-only fixture.
    return struct.pack('<hh', level, -level) * int(RATE * duration / 2)


def test_silence_does_not_trigger_speech():
    gate = SpeechGate(start_timeout=0.5)
    results = [gate.feed(audio(0)) for _ in range(5)]
    assert not gate.speech
    assert not results[0][1] and results[-1][1]


def test_click_is_not_speech():
    gate = SpeechGate()
    gate.feed(audio(2000, 0.05))
    gate.feed(audio(0, 0.2))
    assert not gate.speech


def test_short_low_volume_usb_packets_do_not_hide_real_speech():
    gate = SpeechGate()
    for unused in range(9):
        gate.feed(audio(800, 0.02))
        gate.feed(audio(100, 0.01))
    assert gate.speech


def test_voice_followed_by_silence_ends_recording():
    gate = SpeechGate(silence_seconds=0.3)
    assert not gate.feed(audio(800, 0.2))[1]
    assert gate.speech
    assert not gate.feed(audio(0, 0.2))[1]
    assert gate.feed(audio(0, 0.2))[1]


def test_continuous_noise_is_bounded():
    gate = SpeechGate(max_seconds=0.5)
    assert not gate.feed(audio(800, 0.2))[1]
    assert gate.feed(audio(800, 0.4))[1]


def test_level_preserves_full_scale_and_empty_audio():
    assert rms(audio(1000)) == 1000
    assert rms(b'') == 0
    assert rms(struct.pack('<h', -32768)) == 32768


def test_cloud_ur02_stop_uses_ac_noise_floor_without_changing_pcm():
    def offset_audio(offset, amplitude, duration):
        return struct.pack('<hh', offset + amplitude, offset - amplitude) * int(RATE * duration / 2)
    opening = offset_audio(20000, 400, .1)
    phrase = offset_audio(-18000, 2400, .1)
    quiet = offset_audio(22000, 500, .1)
    chunks = [opening] * 3 + [phrase] * 8 + [quiet] * 20
    offered = []
    gate = SpeechGate(startup_seconds=.31, adaptive_noise=True, silence_seconds=.5)
    result = record(Source(chunks), gate, on_audio=lambda pcm, speech: offered.append(pcm))
    assert gate.speech and 1.5 <= gate.elapsed < 1.8
    assert result == b''.join(offered) == b''.join(chunks)[:len(result)]
    assert rms(quiet) > 20000 and rms(quiet, centered=True) == 500


class Source:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def read(self):
        return next(self.chunks)


def test_record_preserves_initial_audio_and_trailing_silence():
    chunks = [audio(0), audio(800), audio(800), audio(0, 0.4)]
    result = record(Source(chunks), SpeechGate(silence_seconds=0.3))
    assert result == b''.join(chunks)


def test_bluetooth_opening_noise_does_not_close_before_later_speech():
    startup = [audio(2000, 0.1)] * 2 + [audio(0, 0.1)] * 12
    phrase = [audio(800, 0.1)] * 6
    silence = [audio(0, 0.1)] * 11
    chunks = startup + phrase + silence
    offered = []
    gate = SpeechGate(startup_seconds=0.3)
    result = record(Source(chunks), gate, on_audio=lambda pcm, speech: offered.append(pcm))
    assert gate.speech
    assert result == b''.join(chunks)
    assert b''.join(offered) == result  # No opening audio or sentence onset is trimmed.


def test_bluetooth_startup_guard_keeps_no_speech_and_maximum_time_limits():
    gate = SpeechGate(startup_seconds=0.3, start_timeout=0.5)
    results = [gate.feed(audio(2000 if i < 2 else 0)) for i in range(5)]
    assert not gate.speech and results[-1][1]
    gate = SpeechGate(startup_seconds=0.3, max_seconds=0.2)
    assert gate.feed(audio(2000, 0.2))[1]


def test_record_never_returns_silent_audio_for_asr():
    assert record(Source([audio(0, 0.5)]), SpeechGate(start_timeout=0.5)) == b''


def test_cancellation_drops_pcm():
    calls = [False, True]
    result = record(Source([audio(800, 0.2)]), SpeechGate(), lambda: calls.pop(0))
    assert result == b''


def test_record_bounds_oversized_input():
    result = record(Source([audio(800, 2)]), SpeechGate(max_seconds=0.5))
    assert len(result) == RATE


def test_dongle_stopping_packets_after_key_release_keeps_phrase(monkeypatch):
    ticks = iter([0, 0, 0.1, 0.2, 0.3, 0.4, 1.5, 1.6])
    monkeypatch.setattr(capture_module.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(capture_module.time, 'sleep', lambda unused: None)
    voice = audio(800, 0.2)
    assert record(Source([voice, b'', b'']), SpeechGate()) == voice


def test_idle_dongle_without_packets_times_out_as_no_speech(monkeypatch):
    ticks = iter([0, 0, 6.1])
    monkeypatch.setattr(capture_module.time, 'monotonic', lambda: next(ticks))
    assert record(Source([b'']), SpeechGate()) == b''


def test_usb_discovery_skips_onboard_and_uses_card_identifier(tmp_path):
    onboard = tmp_path / 'card0'
    onboard.mkdir()
    (onboard / 'id').write_text('AMLAUGESOUND')
    (onboard / 'pcm1c').mkdir()
    usb = tmp_path / 'card3'
    usb.mkdir()
    (usb / 'stream0').touch()
    (usb / 'id').write_text('Device_1\n')
    (usb / 'pcm0c').mkdir()
    assert usb_capture_devices(tmp_path) == ['plughw:CARD=Device_1,DEV=0']


def test_chinese_spacing_keeps_english_words():
    assert display_text(' 打开 客厅 的 电视 ') == '打开客厅的电视'
    assert display_text('hello world') == 'hello world'
    assert display_text('播放 Hello World 音乐') == '播放 Hello World 音乐'


def test_transcription_keeps_all_endpoint_segments_and_final_result():
    class Library:
        calls = 0

        def vosk_recognizer_accept_waveform(self, *unused):
            self.calls += 1
            return int(self.calls == 1)

        def vosk_recognizer_result(self, *unused):
            return '{"text": "打开 客厅"}'.encode()

        def vosk_recognizer_final_result(self, *unused):
            return '{"text": "的 电视"}'.encode()

        def vosk_recognizer_partial_result(self, *unused):
            return '{"partial": "的 电"}'.encode()

    recognizer = VoskRecognizer.__new__(VoskRecognizer)
    recognizer.lib = Library()
    recognizer.recognizer = 1
    recognizer.parts = []
    assert recognizer.transcribe(audio(1000, 0.5)) == '打开客厅的电视'
    assert recognizer.transcribe(audio(1000), lambda: True) == ''
