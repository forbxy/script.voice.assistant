from resources.lib.data_paths import data_path
import fcntl
import json
import socket
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from resources.lib.r22_audio import Frames, R22Client
from resources.lib.r22_service import R22Controller, find_remote, DOWN, UP
from resources.lib.cmcc_audio import HEADER
from resources.lib.inputs.registry import claim
from resources.lib.settings import validate

START = bytes.fromhex('fb070301') + bytes(17)
STOP = bytes.fromhex('fb070300') + bytes(17)
FRAME = b'\xad\0\0' + bytes(54)
def packet(a=8,b=56):
    return b'\xfb' + b''.join(bytes((1,x))+FRAME+b'\0' for x in (a,b))

class Decoder:
    def __init__(self): self.closed=False
    def decode(self, frames):
        assert not self.closed and len(frames)==114
        return struct.pack('<h',900)*240
    def close(self): self.closed=True

@pytest.fixture
def controller(tmp_path):
    launches=[]
    c=R22Controller(tmp_path,'',launches.append,decoder_factory=Decoder)
    c.launches=launches;c.open()
    yield c
    c.close()

def test_frames_validate_boundaries_and_count_sequence_loss():
    f=Frames();assert f.feed(packet())==FRAME*2
    assert f.feed(packet(200,248))==FRAME*2 and f.discarded==0
    assert f.feed(packet(200,248))==FRAME*2 and f.discarded==2
    assert f.feed(packet()[:-1]) is None
    assert f.feed(packet().replace(b'\xad',b'\x9c')) is None
    assert f.feed(packet())==FRAME*2

def test_identity_uses_name_and_not_fixed_hidraw(tmp_path):
    p=tmp_path/'hidraw31/device';p.mkdir(parents=True)
    (p/'uevent').write_text('HID_NAME=R22-IBLV2\nHID_ID=0005:00000416:00000300\n')
    assert find_remote(tmp_path)=='/dev/hidraw31'
    (p/'uevent').write_text('HID_NAME=CMCC_Voice_Remote\nHID_ID=0005:00000416:00000300\n')
    assert find_remote(tmp_path) is None
    assert validate('voice_input','r22')=='r22'

@pytest.mark.parametrize('first', [DOWN, START])
def test_duplicate_start_and_physical_release_are_one_capture(controller,first):
    c=controller
    for report in (first,DOWN,START,DOWN,START):c.on_report(report)
    assert len(c.launches)==1 and c.session.key_controlled and c.activation=='hold'
    c.on_report(packet());c.on_report(packet(200,248))
    assert c.session.decoded==2
    c.on_report(STOP);c.on_report(UP);c.on_report(UP)
    assert not c.session.recording and c.session.decoder.closed
    data=c.session.outgoing;messages=[]
    while data:
        kind,size=HEADER.unpack_from(data);messages.append((kind,bytes(data[5:5+size])));data=data[5+size:]
    assert [k for k,v in messages]==[b'A',b'A',b'F']
    assert len(messages[0][1])==480
    assert json.loads(messages[-1][1])['bluetooth_msbc_frames']==4
    c.release();c.on_report(DOWN);assert len(c.launches)==2

def test_cancel_while_held_cannot_reopen_from_duplicate_f5(controller):
    c=controller;c.on_report(DOWN);c.on_report(packet());c.release()
    for r in (DOWN,START,packet(),STOP,DOWN):c.on_report(r)
    assert c.session is None and len(c.launches)==1
    c.on_report(UP);c.on_report(DOWN)
    assert len(c.launches)==2

def test_busy_capture_does_not_start_halfway_through_utterance(controller):
    c=controller
    with (data_path(c.profile, 'capture.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);c.on_report(DOWN)
        assert c.session is None
    c.on_report(START);assert c.session is None
    c.on_report(UP);c.on_report(DOWN);assert c.session is not None

def test_maximum_and_invalid_reports_bound_capture(controller):
    c=controller;c.on_report(DOWN,maximum=.03)
    for i in range(4):c.on_report(packet(*( (8,56) if i%2==0 else (200,248))))
    assert c.session.decoded==2 and not c.session.recording
    c.release();c.on_report(UP);c.on_report(DOWN)
    for _ in range(5):c.on_report(b'\xfbBAD')
    assert not c.session.recording

def test_socket_drains_all_audio_and_client_stop_ignores_later_frames(controller):
    c=controller;c.on_report(DOWN)
    running=threading.Event();running.set()
    def run():
        while running.is_set():c.tick(timeout=.001)
    thread=threading.Thread(target=run);thread.start()
    try:
        with R22Client(c.profile,c.session.token) as client:
            assert client.key_controlled and not client.supports_toggle and client.input_source=='r22'
            c.on_report(packet());c.on_report(packet(200,248));c.on_report(STOP)
            pcm=b''
            while not client.finished:pcm+=client.read()
            assert len(pcm)==960 and client.statistics['bluetooth_msbc_frames']==4
        end=time.monotonic()+1
        while c.session and time.monotonic()<end:time.sleep(.01)
        assert c.session is None
        c.on_report(UP);c.on_report(DOWN)
        with R22Client(c.profile,c.session.token) as client:
            client.stop_capture();assert client.finished and not c.session.recording
            c.on_report(packet());assert c.session.decoded==0
    finally:
        running.clear();thread.join()

def test_claim_waits_for_r22_start_before_f5_and_never_uses_cmcc(monkeypatch,tmp_path):
    token='a'*32;calls=[];ready=[False]
    home=SimpleNamespace(getProperty=lambda name:token if ready[0] else '')
    def wait(seconds):ready[0]=True;return False
    class Client:
        def __init__(self,profile,value,socket_name):
            assert socket_name=='r22.sock'
            calls.append(value)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def microphone(self):pass
    monkeypatch.setattr('resources.lib.inputs.pcm.Client',Client)
    with claim(tmp_path,'r22',home,SimpleNamespace(waitForAbort=wait)) as microphone:
        assert callable(microphone)
    assert calls==[token]
