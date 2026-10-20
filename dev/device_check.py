"""Run on the test machine without Kodi: native ASR fixture and USB capture."""
import json
from pathlib import Path
import sys
import time
import wave

ROOT = Path('/storage/.kodi/addons/script.voice.assistant')
sys.path.insert(0, str(ROOT))
from resources.lib.audio import open_microphone, rms, usb_capture_devices
from resources.lib.engines import create_recognizer

report = {'usb_capture_devices': usb_capture_devices(), 'engine': 'sherpa-ncnn'}
fixture = Path(sys.argv[1])
with wave.open(str(fixture), 'rb') as audio:
    assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
    pcm = audio.readframes(audio.getnframes())
start = time.monotonic()
with create_recognizer(ROOT) as recognizer:
    report['model_load_seconds'] = round(time.monotonic() - start, 3)
    decode = time.monotonic()
    report['fixture_text'] = recognizer.transcribe(pcm)
    report['decode_seconds'] = round(time.monotonic() - decode, 3)
    report['fixture_audio_seconds'] = len(pcm) / 32000
    if report['fixture_text'] != '甚至出现交易几乎停滞的情况':
        raise RuntimeError('Unexpected recognition: ' + report['fixture_text'])
with open_microphone() as source:
    report['capture_device'] = source.device
    start = time.monotonic()
    captured = bytearray()
    while time.monotonic() - start < 1.5:
        captured.extend(source.read())
        time.sleep(0.01)
    report.update(captured_samples=len(captured) // 2, capture_rms=round(rms(captured), 2))
    report['capture_state'] = 'pcm_received' if captured else 'no_pcm_received'
print(json.dumps(report, ensure_ascii=False, indent=2))
