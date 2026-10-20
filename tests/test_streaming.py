import threading
import time

from resources.lib.audio import SpeechGate
from resources.lib.streaming import StreamingSession
from test_voice import audio


class Microphone:
    device = 'test-usb'

    def __init__(self, chunks, delay=0.02):
        self.chunks = iter(chunks)
        self.delay = delay
        self.closed = False

    def read(self):
        time.sleep(self.delay)
        return next(self.chunks, b'')

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.closed = True


class Recognizer:
    def __init__(self, delay=0):
        self.delay = delay
        self.received = bytearray()
        self.closed = False
        self.finished = False

    def accept(self, pcm):
        time.sleep(self.delay)
        self.received.extend(pcm)
        return '实时文字'

    def finish(self):
        self.finished = True
        return '实时文字完成'

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.closed = True


def session(microphone, recognizer, **limits):
    return StreamingSession('', SpeechGate(**limits),
        microphone_factory=lambda unused: microphone,
        recognizer_factory=lambda *unused: recognizer)


def wait_for(predicate):
    end = time.monotonic() + 3
    while not predicate() and time.monotonic() < end:
        time.sleep(0.005)
    assert predicate(), 'streaming worker did not reach expected state'


def test_partial_text_is_published_before_capture_finishes():
    microphone = Microphone([audio(800)] * 10)
    recognizer = Recognizer()
    stream = session(microphone, recognizer, max_seconds=0.9)
    stream.start()
    try:
        wait_for(lambda: bool(stream.snapshot()['text']))
        assert not stream.capture_done.is_set()
        wait_for(lambda: stream.done)
        result = stream.snapshot()
        assert result['partial_while_recording']
        assert result['text'] == '实时文字完成'
        assert result['first_partial_seconds'] < result['capture_seconds']
    finally:
        stream.cancel()
        stream.join()
    assert microphone.closed and recognizer.closed


def test_slow_decoder_does_not_block_or_drop_capture_audio():
    microphone = Microphone([audio(800)] * 10, delay=0.001)
    recognizer = Recognizer(delay=0.02)
    stream = session(microphone, recognizer, max_seconds=0.9)
    stream.start()
    try:
        wait_for(lambda: stream.capture_done.is_set())
        assert not stream.decode_done.is_set()
        wait_for(lambda: stream.done)
        assert len(recognizer.received) == int(0.9 * 32000)
        assert stream.snapshot()['queued_seconds'] == 0.9
    finally:
        stream.cancel()
        stream.join()


def test_soft_first_syllable_is_preserved_even_when_speech_detection_is_late():
    chunks = [audio(0)] * 10 + [audio(100)] * 10 + [audio(800)] * 3
    microphone = Microphone(chunks, delay=0.001)
    recognizer = Recognizer()
    stream = session(microphone, recognizer, max_seconds=2.3)
    stream.start()
    try:
        wait_for(lambda: stream.done)
        assert recognizer.received == b''.join(chunks)
    finally:
        stream.cancel()
        stream.join()


def test_silent_session_never_decodes_or_finishes_a_false_transcript():
    microphone = Microphone([audio(0)] * 5, delay=0.001)
    recognizer = Recognizer()
    stream = session(microphone, recognizer, start_timeout=0.5)
    stream.start()
    try:
        wait_for(lambda: stream.done)
        assert not recognizer.finished
        assert not stream.snapshot()['text']
    finally:
        stream.cancel()
        stream.join()


def test_cancel_closes_both_workers_without_finalizing_transcript():
    microphone = Microphone([audio(800)] * 100)
    recognizer = Recognizer(delay=0.01)
    stream = session(microphone, recognizer)
    stream.start()
    try:
        wait_for(lambda: bool(stream.snapshot()['text']))
        stream.cancel()
        wait_for(lambda: stream.done)
        assert not recognizer.finished
    finally:
        stream.cancel()
        stream.join()
    assert microphone.closed and recognizer.closed


def test_model_error_stops_capture_and_propagates_reason():
    microphone = Microphone([audio(800)] * 100)

    def broken(*unused):
        raise RuntimeError('model is missing')

    stream = StreamingSession('', SpeechGate(),
        microphone_factory=lambda unused: microphone, recognizer_factory=broken)
    stream.start()
    try:
        wait_for(lambda: stream.done)
        assert stream.snapshot()['error'] == 'model is missing'
    finally:
        stream.cancel()
        stream.join()
    assert microphone.closed


def test_cancellation_during_hotword_preparation_is_not_a_recognition_error():
    microphone = Microphone([audio(800)] * 100)
    def prepare(*unused):
        while not stream.stop.wait(.01):
            pass
        raise InterruptedError('cancelled while preparing hotwords')
    stream = StreamingSession('', SpeechGate(), microphone_factory=lambda unused: microphone,
                              recognizer_factory=prepare)
    stream.start()
    try:
        wait_for(lambda: stream.snapshot()['audio_seconds'] > 0)
        stream.cancel()
        wait_for(lambda: stream.done)
        assert not stream.snapshot().get('error')
    finally:
        stream.cancel()
        stream.join()


def test_recognizer_endpoint_stops_loud_background_and_preserves_final_words():
    microphone = Microphone([audio(1600)] * 100, delay=.005)
    class EndpointRecognizer(Recognizer):
        @property
        def is_endpoint(self):
            return len(self.received) >= 9600
    recognizer = EndpointRecognizer()
    stream = session(microphone, recognizer, recognizer_endpoint=True, max_seconds=10)
    stream.start()
    try:
        wait_for(lambda: stream.done)
        result = stream.snapshot()
        assert result['endpoint_detected']
        assert result['text'] == '实时文字完成' and recognizer.finished
        assert .3 <= result['audio_seconds'] < 10
        assert len(recognizer.received) == int(round(result['queued_seconds'] * 32000))
        assert not stream.stop.is_set()  # End-of-speech is not cancellation.
    finally:
        stream.cancel()
        stream.join()
    assert microphone.closed and recognizer.closed


def test_recognizer_endpoint_mode_still_enforces_capture_maximum():
    microphone = Microphone([audio(1600)] * 100, delay=.001)
    recognizer = Recognizer()
    stream = session(microphone, recognizer, recognizer_endpoint=True, max_seconds=.6)
    stream.start()
    try:
        wait_for(lambda: stream.done)
        assert stream.snapshot()['audio_seconds'] == .6
        assert recognizer.finished and not stream.endpoint.is_set()
    finally:
        stream.cancel()
        stream.join()


def test_manual_finish_closes_loud_microphone_and_finishes_all_queued_audio():
    microphone = Microphone([audio(1600)] * 300, delay=.01)
    recognizer = Recognizer(delay=.03)
    stream = session(microphone, recognizer, max_seconds=30)
    stream.start()
    try:
        wait_for(lambda: stream.snapshot()['speech_detected'])
        began = time.monotonic()
        stream.finish_capture()
        wait_for(lambda: stream.capture_done.is_set())
        assert time.monotonic() - began < .5 and microphone.closed
        wait_for(lambda: stream.done)
        result = stream.snapshot()
        assert result['manual_stop_requested']
        assert result['audio_seconds'] < 30
        assert result['text'] == '实时文字完成' and recognizer.finished
        assert len(recognizer.received) == round(result['queued_seconds'] * 32000)
        assert not stream.stop.is_set()
        before = stream.snapshot()
        stream.finish_capture()  # Late/repeated finish never restarts capture.
        assert stream.snapshot() == before
    finally:
        stream.cancel()
        stream.join()
