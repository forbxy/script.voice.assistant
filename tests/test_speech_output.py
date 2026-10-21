import struct
import math
import os
import subprocess
import sys
import time

import pytest

from resources.lib.speech.output import AlsaOutput, alsa_device, kodi_volume_gain, scale_pcm, wake_noise
from resources.lib.speech.service import Stream

REAL_POPEN = subprocess.Popen


def test_selected_device_and_no_silent_default_fallback():
    assert alsa_device('ALSA:surround71:CARD=AMLAUGESOUND,DEV=0|AML-AUGESOUND') == (
        'plug:{SLAVE="surround71:CARD=AMLAUGESOUND,DEV=0"}')
    assert alsa_device('ALSA:hdmi:CARD=USB,DEV=1') == 'plug:{SLAVE="hdmi:CARD=USB,DEV=1"}'
    for value in ('PULSE:default', '', None, 'ALSA:', 'ALSA:hw:0\n'):
        with pytest.raises(ValueError): alsa_device(value)


@pytest.mark.parametrize('setting,pcm', [
    ('ALSA:@|Default (Generic)', 'sysdefault'),
    ('ALSA:@:CARD=Generic,DEV=0|Generic', 'sysdefault:CARD=Generic'),
    ('ALSA:@:CARD=USB,DEV=2|USB', 'front:CARD=USB,DEV=2'),
    ('ALSA:@:CARD=USB', 'sysdefault:CARD=USB'),
])
def test_kodi_stereo_alias_keeps_selected_card_and_nonzero_device(setting, pcm):
    assert alsa_device(setting) == 'plug:{SLAVE="' + pcm + '"}'


def test_direct_pcm_honors_volume_and_mute():
    data = struct.pack('<4h', -32768, -1000, 1000, 32767)
    assert scale_pcm(data, 1) == data
    assert scale_pcm(data, 0) == b'\0' * len(data)
    assert struct.unpack('<4h', scale_pcm(data, .5)) == (-16384, -500, 500, 16384)


@pytest.mark.parametrize('volume,expected', [
    (-10, 0), (0, 0), (25, .0056234133), (50, .0316227766),
    (75, .1778279410), (90, .5011872336), (99.9, 1), (100, 1), (110, 1),
])
def test_master_volume_matches_kodi_decibels(volume, expected):
    assert kodi_volume_gain(volume) == pytest.approx(expected)
    assert kodi_volume_gain(volume, muted=True) == 0


def test_relative_volume_updates_live_without_changing_kodi(tmp_path, monkeypatch):
    output, _, _, values = output_fixture(monkeypatch, tmp_path, '')
    output.volume()
    assert output.gain == .5  # Default compensation, Kodi at 100%.
    values['volume'] = 50
    output.next_volume = 0
    output.poll()
    assert output.gain == pytest.approx(.0158113883)
    output.poll(relative_volume=100)
    assert output.gain == pytest.approx(.0316227766)
    values['muted'] = True
    output.next_volume = 0
    output.poll()
    assert output.gain == 0
    values.update(volume=100, muted=False)
    output.poll(relative_volume=0)
    assert output.gain == 0
    assert values == {'volume': 100, 'muted': False}


@pytest.mark.parametrize('relative,expected', [(0, 0), (25, 3000), (50, 6000), (100, 12000)])
def test_relative_volume_scales_actual_output_pcm(tmp_path, monkeypatch, relative, expected):
    output, _, target, _ = output_fixture(monkeypatch, tmp_path,
        'import sys;open(sys.argv[1],"wb").write(sys.stdin.buffer.read())')
    stream = Stream('a'*32, tmp_path)
    stream.put(struct.pack('<h', 12000)*1000); stream.put(None)
    try:
        output.start(stream, lead_in=0, relative_volume=relative)
        assert output.done.wait(3)
        assert output.poll() == 'ended'
        output.stop()
        assert target.read_bytes() == struct.pack('<h', expected)*1000
    finally:
        output.stop(); stream.close()


def test_wake_noise_is_nonzero_one_second_at_kodi_noise_floor():
    samples = struct.unpack('<22050h', wake_noise())
    rms = math.sqrt(sum(x*x for x in samples) / len(samples))
    assert -103 < 20 * math.log10(rms / 32768) < -97
    assert abs(sum(samples) / len(samples)) < .02
    assert min(samples) < 0 < max(samples)
    assert max(abs(x) for x in samples) <= 4


@pytest.mark.parametrize('seconds', [0, .5, 2, 5])
@pytest.mark.parametrize('rate', [16000, 22050])
def test_configured_lead_in_preserves_speech_and_exact_duration(tmp_path, monkeypatch, seconds, rate):
    output, _, target, _ = output_fixture(monkeypatch, tmp_path,
        'import sys;open(sys.argv[1],"wb").write(sys.stdin.buffer.read())')
    stream = Stream('a'*32, tmp_path)
    data = struct.pack('<h', 1000)*1000
    stream.put(data);stream.put(None)
    try:
        output.start(stream, lead_in=seconds, rate=rate)
        assert output.done.wait(3)
        assert output.poll() == 'ended'
        output.stop()
        expected_prefix = wake_noise(seconds, rate)
        assert len(expected_prefix) == round(rate * seconds)*2
        assert target.read_bytes() == expected_prefix + scale_pcm(data, .5)
    finally: output.stop();stream.close()


def output_fixture(monkeypatch, tmp_path, script):
    events = []
    values = {'volume': 100, 'muted': False}
    def rpc(method, params):
        return {'value': 'ALSA:hw:CARD=USB,DEV=1|USB'} if method == 'Settings.GetSettingValue' else values
    real_popen = REAL_POPEN
    target = tmp_path/'pcm'
    def popen(command, **kwargs):
        assert command[:4] == ['aplay', '-q', '-D', 'voice_assistant']
        assert command[command.index('-r') + 1] == str(output.rate)
        config_fd, = kwargs['pass_fds']
        assert kwargs['env']['ALSA_CONFIG_PATH'] == '/proc/self/fd/' + str(config_fd)
        config = os.pread(config_fd, 8192, 0).decode()
        assert 'rate 48000 channels 2 format S16_LE' in config
        assert 'route_policy duplicate' in config
        assert 'hw:CARD=USB,DEV=1' in config
        return real_popen([sys.executable, '-c', script, str(target)], **kwargs)
    monkeypatch.setattr('resources.lib.speech.output.subprocess.Popen', popen)
    output = AlsaOutput(rpc, lambda: events.append('suspend'), lambda: events.append('resume'))
    return output, events, target, values


def test_stream_drains_and_releases_device_with_no_kodi_player(tmp_path, monkeypatch):
    output, events, target, values = output_fixture(monkeypatch, tmp_path,
        'import sys;open(sys.argv[1],"wb").write(sys.stdin.buffer.read())')
    stream = Stream('a'*32, tmp_path)
    try:
        stream.put(struct.pack('<h', 1000)*1000)
        output.start(stream)
        stream.put(struct.pack('<h', -1000)*1000);stream.put(None)
        assert output.done.wait(3)
        assert output.poll() == 'ended'
        assert output.bytes_written == 44100 + 4000
        output.stop()
        assert events == ['suspend','resume']
        # Exactly one second of very low noise per open, not between speech chunks.
        assert target.read_bytes() == wake_noise() + struct.pack('<h',500)*1000 + struct.pack('<h',-500)*1000
        assert output.process is None and not output.suspended
        assert output.configuration is None
        output.stop();assert events == ['suspend','resume']
    finally: output.stop();stream.close()


@pytest.mark.parametrize('volume, muted', [(50, True), (0, False)])
def test_muting_or_zero_volume_silences_preamble_and_speech(tmp_path, monkeypatch, volume, muted):
    output, _, target, values = output_fixture(monkeypatch, tmp_path,
        'import sys;open(sys.argv[1],"wb").write(sys.stdin.buffer.read())')
    values.update(volume=volume, muted=muted)
    stream = Stream('a'*32, tmp_path)
    stream.put(struct.pack('<h', 1000)*1000);stream.put(None)
    try:
        output.start(stream)
        assert output.done.wait(3)
        assert output.poll() == 'ended'
        output.stop()
        assert target.read_bytes() == b'\0'*(44100+2000)
    finally: output.stop();stream.close()


def test_cancel_interrupts_blocked_output_and_restores_kodi(tmp_path, monkeypatch):
    output, events, target, values = output_fixture(monkeypatch,tmp_path,'import time;time.sleep(30)')
    stream=Stream('b'*32,tmp_path);stream.put(b'\0'*100000)
    try:
        output.start(stream);process=output.process
        time.sleep(.05);start=time.monotonic();output.stop()
        assert time.monotonic()-start < 1.5
        assert process.poll() is not None and events == ['suspend','resume']
    finally:output.stop();stream.close()


def test_cancel_waiting_for_next_sentence_restores_kodi(tmp_path, monkeypatch):
    output, events, target, values = output_fixture(monkeypatch,tmp_path,
        'import sys;sys.stdin.buffer.read()')
    stream=Stream('c'*32,tmp_path);stream.put(b'\0'*100)
    try:
        output.start(stream);thread=output.thread
        time.sleep(.05);output.stop()
        assert not thread.is_alive() and events == ['suspend','resume']
    finally:output.stop();stream.close()


def test_failed_spawn_restores_kodi(tmp_path, monkeypatch):
    output, events, _, _ = output_fixture(monkeypatch,tmp_path,'')
    def fail(*args, **kwargs):raise OSError('missing aplay')
    monkeypatch.setattr('resources.lib.speech.output.subprocess.Popen',fail)
    stream=Stream('d'*32,tmp_path);stream.put(b'\0'*100)
    with pytest.raises(OSError):output.start(stream)
    assert events == ['suspend','resume'] and not output.suspended


def test_device_busy_is_failure_and_still_restores_kodi(tmp_path, monkeypatch):
    output, events, _, _ = output_fixture(monkeypatch,tmp_path,'import sys;sys.exit(1)')
    stream=Stream('e'*32,tmp_path);stream.put(b'\0'*100);stream.put(None)
    try:
        output.start(stream)
        assert output.done.wait(3)
        assert output.poll() != 'ended'
        output.stop();assert events == ['suspend','resume']
    finally:output.stop();stream.close()


def test_async_kodi_release_retries_busy_without_losing_pcm(tmp_path, monkeypatch):
    output, events, target, _ = output_fixture(monkeypatch,tmp_path,
        'import sys;open(sys.argv[1],"wb").write(sys.stdin.buffer.read())')
    success = subprocess.Popen
    attempts = []
    def launch(command, **kwargs):
        attempts.append(command)
        if len(attempts) == 1:
            return REAL_POPEN([sys.executable,'-c',
                'import sys;sys.stderr.write("Device or resource busy");sys.exit(1)'], **kwargs)
        return success(command, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', launch)
    stream=Stream('f'*32,tmp_path);data=struct.pack('<h',1000)*12000
    stream.put(data);stream.put(None)
    try:
        output.start(stream)
        assert output.done.wait(3)
        assert output.poll() == 'ended' and output.open_retries == 1
        output.stop()
        assert target.read_bytes() == wake_noise() + scale_pcm(data,.5)
        assert events == ['suspend','resume']
    finally:output.stop();stream.close()
