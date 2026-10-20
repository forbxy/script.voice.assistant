import gzip
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

import pytest

from resources.lib.doubao_config import load_key, read_key_file, save_key
from resources.lib.doubao_protocol import audio_request, full_request, parse_response, request_body
from resources.lib.doubao_recognizer import DoubaoRecognizer


def response(value, flags=3, sequence=-3, event=None, error=None, compressed=True):
    payload = json.dumps(value).encode()
    if compressed:
        payload = gzip.compress(payload)
    data = bytes((0x11, ((15 if error is not None else 9) << 4) | flags,
                  0x10 | int(compressed), 0))
    if flags & 1:
        data += struct.pack('>i', sequence)
    if flags & 4:
        data += struct.pack('>i', event)
    if error is not None:
        data += struct.pack('>I', error)
    return data + struct.pack('>I', len(payload)) + payload


def test_request_has_no_language_hotwords_context_or_text_rewriting():
    data = full_request()
    assert data[:4] == bytes.fromhex('11111100')
    assert struct.unpack_from('>i', data, 4)[0] == 1
    value = json.loads(gzip.decompress(data[12:]))
    assert value == request_body()
    assert value['audio'] == {'format': 'pcm', 'codec': 'raw', 'rate': 16000, 'bits': 16, 'channel': 1}
    assert set(value['request']) == {'model_name', 'enable_itn', 'enable_punc', 'enable_ddc', 'result_type'}
    assert not value['request']['enable_ddc']


def test_final_audio_packet_keeps_every_sample_and_has_negative_sequence():
    pcm = struct.pack('<hhh', -32768, 1234, 32767)
    data = audio_request(9, pcm, last=True)
    assert data[:4] == bytes.fromhex('11231100')
    assert struct.unpack_from('>i', data, 4)[0] == -9
    assert gzip.decompress(data[12:]) == pcm
    with pytest.raises(ValueError):
        audio_request(2, b'odd')


def test_gzip_final_event_sequence_and_uncompressed_initial_ack():
    value = parse_response(response({'result': {'text': ' 科幻片。 '}}, flags=7, event=12))
    assert value == {'last': True, 'sequence': -3, 'event': 12, 'text': '科幻片。'}
    assert not parse_response(response({}, flags=0, compressed=False))['last']
    assert parse_response(response({'result': [{'text': '打开'}, {'text': 'Kodi'}]}))['text'] == '打开Kodi'


def test_server_errors_do_not_echo_response_secrets():
    with pytest.raises(RuntimeError) as error:
        parse_response(response({'message': 'secret-key-and-private-voice'}, error=45000081, flags=0))
    assert '45000081' in str(error.value)
    assert 'secret' not in str(error.value)


def test_malformed_length_and_compression_bomb_are_rejected():
    with pytest.raises(ValueError):
        parse_response(response({})[:-1])
    with pytest.raises(ValueError):
        parse_response(response({'text': 'A' * (2 * 1024 * 1024)}))


def test_private_key_import_and_missing_credentials(tmp_path):
    key = 'test-doubao-key'
    source = tmp_path / 'doubao.txt'
    source.write_text(key + '\n')
    save_key(tmp_path, read_key_file(source))
    assert load_key(tmp_path) == key
    assert (tmp_path / 'doubao-api-key.txt').stat().st_mode & 0o777 == 0o600
    source.write_text(json.dumps({'api_key': key}))
    assert read_key_file(source) == key
    with pytest.raises(ValueError):
        save_key(tmp_path, 'key\r\nInjected-Header')
    with pytest.raises(ValueError, match='配置 API Key'):
        load_key(tmp_path / 'missing')


FAKE_WORKER = '''import json,sys,struct,time,hashlib
config=json.loads(sys.stdin.buffer.readline())
if MODE=='connect_hang':time.sleep(60)
print(json.dumps({'type':'ready','log_id':'test-log'}),flush=True)
chunks=[]
while True:
 header=sys.stdin.buffer.read(4)
 if not header:break
 size=struct.unpack('>I',header)[0]
 if not size:
  if MODE=='finish_hang':time.sleep(60)
  if MODE=='failure':print(json.dumps({'type':'error','error':'provider failed'}),flush=True)
  else:
   pcm=b''.join(chunks)
   print(json.dumps({'type':'result','last':True,'text':str(len(pcm))+':'+hashlib.sha256(pcm).hexdigest()}),flush=True)
  break
 block=sys.stdin.buffer.read(size)
 chunks.append(block)
'''


def fake_process(tmp_path, mode='normal'):
    file = tmp_path / (mode + '-worker.py')
    file.write_text('MODE=' + repr(mode) + '\n' + FAKE_WORKER)
    processes = []
    def spawn(args, **kwargs):
        assert 'test-doubao-key' not in repr(args)
        process = subprocess.Popen([sys.executable, '-u', str(file)], **kwargs)
        processes.append(process)
        return process
    return spawn, processes


def test_real_worker_pipe_preserves_all_pcm_and_reaps_process(tmp_path):
    import hashlib
    save_key(tmp_path, 'test-doubao-key')
    spawn, processes = fake_process(tmp_path)
    pcm = bytes(range(256)) * 100
    with DoubaoRecognizer('/addon', tmp_path, process_factory=spawn) as recognizer:
        assert recognizer.transcribe(pcm) == str(len(pcm)) + ':' + hashlib.sha256(pcm).hexdigest()
        assert recognizer.log_id == 'test-log'
    assert all(p.poll() is not None for p in processes)


@pytest.mark.parametrize('mode', ['connect_hang', 'finish_hang'])
def test_cancellation_kills_network_worker_during_connect_and_final_wait(tmp_path, mode):
    save_key(tmp_path, 'test-doubao-key')
    spawn, processes = fake_process(tmp_path, mode)
    stop = threading.Event()
    timer = threading.Timer(.2, stop.set)
    timer.start()
    began = time.monotonic()
    try:
        with pytest.raises(InterruptedError):
            with DoubaoRecognizer('/addon', tmp_path, cancelled=stop.is_set, process_factory=spawn) as recognizer:
                recognizer.accept(bytes(3200))
                recognizer.finish()
    finally:
        timer.join()
    assert time.monotonic() - began < 1.0
    assert all(p.poll() is not None for p in processes)


def test_provider_failure_is_reported_and_worker_is_reaped(tmp_path):
    save_key(tmp_path, 'test-doubao-key')
    spawn, processes = fake_process(tmp_path, 'failure')
    with pytest.raises(RuntimeError, match='provider failed'):
        with DoubaoRecognizer('/addon', tmp_path, process_factory=spawn) as recognizer:
            recognizer.accept(bytes(3200))
            recognizer.finish()
    assert all(p.poll() is not None for p in processes)


def test_final_response_timeout_is_bounded_and_reaps_worker(tmp_path):
    save_key(tmp_path, 'test-doubao-key')
    spawn, processes = fake_process(tmp_path, 'finish_hang')
    with DoubaoRecognizer('/addon', tmp_path, process_factory=spawn) as recognizer:
        recognizer.timeout = .1
        recognizer.accept(bytes(3200))
        with pytest.raises(RuntimeError, match='超时'):
            recognizer.finish()
    assert all(p.poll() is not None for p in processes)
