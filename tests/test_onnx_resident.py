from resources.lib.data_paths import data_path
import json
from pathlib import Path
import sys
import time

import pytest

from resources.lib.asr import service as module
from resources.lib.asr.recognizer import Recognizer


PROGRAM = r'''
import json,os,struct,sys,ctypes
ctypes.CDLL(None).prctl(1,15) # Same PR_SET_PDEATHSIG as the real native worker.
def reply(value):print(json.dumps(value),flush=True)
reply({'ready':True,'streaming':True,
       'max_active_paths':int(os.environ.get('VOICE_ASR_MAX_ACTIVE_PATHS','4')),
       'hotwords_score':float(os.environ.get('VOICE_ASR_HOTWORDS_SCORE','2'))})
count=0
while True:
 header=sys.stdin.buffer.read(4)
 if not header:break
 length=struct.unpack('<I',header)[0]
 if length==0xffffffff:
  count=0;reply({'reset':True});continue
 if length:
  sys.stdin.buffer.read(length);count+=length
 reply({'result':{'text':str(count)},'endpoint':0})
'''


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.01)
    assert predicate()


@pytest.fixture
def service(tmp_path, monkeypatch):
    program = tmp_path / 'worker.py'; program.write_text(PROGRAM)
    monkeypatch.setattr(module, 'command', lambda *a: [sys.executable, '-u', str(program)])
    monkeypatch.setattr(module.Service, 'available', lambda *a: True)
    data = module.catalog(str(Path(__file__).resolve().parents[1]))
    monkeypatch.setattr(module, 'catalog', lambda root: data)
    clock = [100]
    owner = module.Service(tmp_path, tmp_path, clock=lambda: clock[0], delay=60)
    owner.tick({'recognition_engine': 'onnx', 'onnx_model': 'first'})
    try: yield owner, clock, tmp_path
    finally: owner.close()


def test_preload_waits_one_minute_but_first_use_loads_and_reuses_model(service):
    owner, clock, profile = service
    wait_for(lambda: owner.status['state'] == 'waiting')
    assert owner.native is None
    with Recognizer(profile, 'first') as client:
        assert client.accept(b'\x01\x00' * 100) == '200'
        assert client.finish() == '200'
        pid = owner.native.process.pid
    wait_for(lambda: owner.status['state'] == 'ready')
    time.sleep(.05)
    assert owner.native.process.pid == pid and owner.native.process.poll() is None
    with Recognizer(profile, 'first') as client:
        assert client.accept(b'\x01\x00' * 50) == '100'  # Fresh stream, no previous PCM.
        assert client.finish() == '100'
    assert owner.native.process.pid == pid


@pytest.mark.parametrize('model', ['zipformer-2025', 'zipformer-2025-fp16', 'zipformer-2025-fp32'])
def test_decoding_settings_reach_child_reload_zipformer_and_preserve_active_stream(service, model):
    owner, clock, profile = service
    clock[0] = 200
    values = {'recognition_engine': 'onnx', 'onnx_model': model,
              'zipformer_max_active_paths': 4, 'zipformer_hotwords_score': '2'}
    owner.tick(values)
    with Recognizer(profile, model) as client:
        assert client.accept(b'\x01\x00' * 30) == '60'
        pid = owner.native.process.pid
        owner.tick(dict(values, zipformer_max_active_paths=8, zipformer_hotwords_score='6'))
        assert client.accept(b'\x01\x00' * 20) == '100'
        assert owner.native.process.pid == pid  # Finish this utterance with its original settings.
        assert client.finish() == '100'
    with Recognizer(profile, model) as client:
        assert owner.native.process.pid != pid
        assert owner.native.ready['max_active_paths'] == 8
        assert owner.native.ready['hotwords_score'] == 6
        assert owner.status['decoding'] == {'max_active_paths': 8, 'hotwords_score': 6}
        assert client.accept(b'\x01\x00' * 10) == '20'
        assert client.finish() == '20'
        pid = owner.native.process.pid
    wait_for(lambda: not owner.clients)
    owner.tick(dict(values, zipformer_max_active_paths=8, zipformer_hotwords_score='6'))
    time.sleep(.1)
    assert owner.native.process.pid == pid
    assert any(row['event'] == 'unload' and row.get('reason') == 'decoding_changed' for row in owner.history)


def test_zipformer_controls_do_not_reload_other_models(service):
    owner, clock, profile = service
    with Recognizer(profile, 'first') as client:
        assert client.finish() == '0'
        pid = owner.native.process.pid
    wait_for(lambda: not owner.clients)
    owner.tick({'recognition_engine': 'onnx', 'onnx_model': 'first',
                'zipformer_max_active_paths': 16, 'zipformer_hotwords_score': '0.5'})
    time.sleep(.1)
    assert owner.native.process.pid == pid
    assert 'decoding' not in owner.status


def test_boot_timer_model_switch_and_cloud_unload(service):
    owner, clock, profile = service
    clock[0] += 59
    owner.tick(owner.values)
    wait_for(lambda: owner.status['state'] == 'waiting')
    assert owner.native is None
    clock[0] += 1
    owner.tick(owner.values)
    wait_for(lambda: owner.status['state'] == 'ready')
    first = owner.native.process.pid
    owner.tick({'recognition_engine': 'onnx', 'onnx_model': 'second'})
    wait_for(lambda: owner.loaded_model == 'second')
    assert owner.native.process.pid != first
    owner.tick({'recognition_engine': 'doubao'})
    wait_for(lambda: owner.status['state'] == 'disabled')
    assert owner.native is None


def test_disconnect_during_capture_does_not_hold_service_lock(service):
    owner, clock, profile = service
    client = Recognizer(profile, 'first')
    pid = owner.native.process.pid
    client.accept(b'\0\0' * 100)
    client.close()
    wait_for(lambda: owner.status['state'] == 'ready' and not owner.clients)
    assert owner.native.process.pid == pid
    with Recognizer(profile, 'first') as next_client:
        assert next_client.accept(b'\0\0') == '2'
        assert next_client.finish() == '2'
    assert owner.native.process.pid == pid
    events = json.loads((data_path(profile, 'asr/lifecycle.json')).read_text())
    assert sum(row['event'] == 'load_started' for row in events) == 1
    assert any(row['event'] == 'client_cancelled' and row['model_retained'] for row in events)


def test_empty_recording_and_cancel_during_native_work_keep_model_and_reset_stream(service):
    import socket
    import struct
    owner, clock, profile = service
    with Recognizer(profile, 'first') as client:
        assert client.finish() == '0'
        pid = owner.native.process.pid
    # No detected speech: the frontend closes without calling finish().
    client = Recognizer(profile, 'first')
    client.close()
    wait_for(lambda: not owner.clients and owner.status['state'] == 'ready')
    assert owner.native.process.pid == pid
    # Make the already loaded test worker delay one framed operation. Wait
    # until the backend enters it, then disconnect while its read is pending.
    original = owner.native.exchange
    entered = __import__('threading').Event()
    def delayed(data=b'', reset=False):
        if data:
            entered.set(); time.sleep(.2)
        return original(data, reset)
    owner.native.exchange = delayed
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(data_path(profile, 'asr/control.sock')))
    sock.sendall(b'{"model":"first"}\n')
    assert sock.recv(4096)
    sock.sendall(struct.pack('<I', 200) + b'\0\0' * 100)
    assert entered.wait(1)
    sock.close()
    # A new request can wait behind recovery of the disconnected client.
    with Recognizer(profile, 'first') as next_client:
        assert next_client.accept(b'\0\0') == '2'
        assert next_client.finish() == '2'
    assert owner.native.process.pid == pid


def test_cancel_cannot_keep_native_with_invalid_reset_acknowledgement(service):
    owner, clock, profile = service
    client = Recognizer(profile, 'first')
    original = owner.native.exchange
    owner.native.exchange = lambda data=b'', reset=False: {'reset': False} if reset else original(data)
    client.close()
    wait_for(lambda: any(row['event'] == 'reset_failed' for row in owner.history))
    # Logging reset_failed precedes cleanup on the service thread.
    wait_for(lambda: any(row['event'] == 'unload' and row['reason'] == 'client_error' for row in owner.history))


def test_shutdown_with_open_recording_client_finishes_promptly(service):
    owner, clock, profile = service
    client = Recognizer(profile, 'first')
    began = time.monotonic()
    owner.close()
    assert time.monotonic() - began < 2
    client.close()
    # Fixture cleanup is also allowed after an explicit shutdown.
    owner.close = lambda: None
