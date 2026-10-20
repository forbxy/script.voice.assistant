from types import SimpleNamespace

import pytest

from resources.lib import audio
from resources.lib.usb_capture import restore_capture_controls


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


def store(pointer, value):
    pointer._obj.value = value
    return 0


def element(**values):
    defaults = dict(name=b'Mic', active=True, gains={0: -1200}, db_range=(-1200, 3000),
                    raw={0: 0}, raw_range=(0, 28), switches={0: 0},
                    volume=True, switch=True, common_volume=False,
                    common_switch=False, exclusive=False)
    return SimpleNamespace(**{**defaults, **values})


class Alsa:
    """Mutable ALSA controls; exercises pointer output and per-channel writes."""
    def __init__(self, elements=(), card=7, open_error=0, attach_error=0):
        self.elements = dict(enumerate(elements, 1))
        self.attached = []
        self.closed = self.freed = 0
        self.events = []
        self.snd_pcm_info_malloc = Function(lambda out: store(out, 42))
        self.snd_pcm_info = Function(lambda *args: 0)
        self.snd_pcm_info_get_card = Function(lambda *args: card)
        self.snd_pcm_info_free = Function(self.free)
        self.snd_mixer_open = Function(lambda out, flags: open_error or store(out, 43))
        self.snd_mixer_attach = Function(lambda handle, name: self.attach(name, attach_error))
        self.snd_mixer_selem_register = Function(lambda *args: 0)
        self.snd_mixer_load = Function(lambda *args: 0)
        self.snd_mixer_close = Function(self.close)
        self.snd_mixer_first_elem = Function(lambda *args: 1 if self.elements else None)
        self.snd_mixer_elem_next = Function(lambda current: current + 1 if current + 1 in self.elements else None)
        self.snd_mixer_selem_get_name = Function(lambda elem: self.elements[elem].name)
        for suffix, field in [('is_active', 'active'), ('has_capture_volume', 'volume'),
                              ('has_capture_switch', 'switch'), ('has_common_volume', 'common_volume'),
                              ('has_common_switch', 'common_switch'),
                              ('has_capture_switch_exclusive', 'exclusive')]:
            setattr(self, 'snd_mixer_selem_' + suffix,
                    Function(lambda elem, field=field: int(getattr(self.elements[elem], field))))
        self.snd_mixer_selem_has_capture_channel = Function(
            lambda elem, channel: int(channel in self.elements[elem].raw))
        self.snd_mixer_selem_get_capture_dB_range = Function(
            lambda elem, low, high: self.range(elem, low, high, 'db_range'))
        self.snd_mixer_selem_get_capture_volume_range = Function(
            lambda elem, low, high: self.range(elem, low, high, 'raw_range'))
        for suffix, field in [('dB', 'gains'), ('volume', 'raw'), ('switch', 'switches')]:
            setattr(self, 'snd_mixer_selem_get_capture_' + suffix,
                    Function(lambda elem, channel, out, field=field:
                             store(out, getattr(self.elements[elem], field)[channel])))
            setattr(self, 'snd_mixer_selem_set_capture_' + suffix,
                    Function(lambda elem, channel, value, *extra, field=field:
                             self.set(elem, channel, value, field)))

    def range(self, elem, low, high, field):
        limits = getattr(self.elements[elem], field)
        if limits is None:
            return -22
        store(low, limits[0])
        return store(high, limits[1])

    def set(self, elem, channel, value, field):
        self.events.append((elem, channel, field, value))
        getattr(self.elements[elem], field)[channel] = value
        return 0

    def free(self, *args):
        self.freed += 1

    def attach(self, name, error):
        self.attached.append(name)
        return error

    def close(self, *args):
        self.closed += 1
        return 0


@pytest.fixture
def proc(tmp_path):
    card = tmp_path / 'card7'
    card.mkdir()
    (card / 'usbid').write_text('1234:abcd')
    return tmp_path


def test_unknown_usb_brand_and_dynamic_card_gain_and_mute(proc):
    mic = element(name=b'ADC Capture')
    lib = Alsa([mic])
    result = restore_capture_controls(lib, 123, proc)
    assert mic.gains == {0: 0} and mic.switches == {0: 1}
    assert result['status'] == 'ready' and result['card'] == 7
    assert len(result['changed']) == 2
    assert lib.attached == [b'hw:7']
    assert lib.closed == lib.freed == 1


@pytest.mark.parametrize('card', [-1, 0, 8])
def test_virtual_or_non_usb_pcm_cannot_change_any_mixer(proc, card):
    mic = element()
    lib = Alsa([mic], card=card)
    assert restore_capture_controls(lib, 123, proc)['status'] == 'not_usb'
    assert lib.attached == [] and lib.events == [] and lib.closed == 0
    assert lib.freed == 1


def test_preserves_higher_gain_on_other_channel_and_repeated_opens(proc):
    mic = element(gains={0: -1200, 1: 1200}, raw={0: 0, 1: 8}, switches={0: 1, 1: 1})
    lib = Alsa([mic])
    restore_capture_controls(lib, 123, proc)
    assert mic.gains == {0: 0, 1: 1200}
    assert lib.events == [(1, 0, 'gains', 0)]
    assert restore_capture_controls(lib, 123, proc)['changed'] == []
    # A hotplug/OS reinitialization may reset the same device between opens.
    mic.gains[0], mic.switches[0] = -1200, 0
    restore_capture_controls(lib, 123, proc)
    assert mic.gains[0] == 0 and mic.switches[0] == 1


@pytest.mark.parametrize('name', [b'Capture', b'Microphone', b'Digital Input'])
def test_no_db_metadata_only_recovers_minimum_raw_step(proc, name):
    mic = element(name=name, db_range=None, raw={0: 0, 1: 3}, switches={0: 1, 1: 1})
    lib = Alsa([mic])
    restore_capture_controls(lib, 123, proc)
    assert mic.raw == {0: 14, 1: 3}
    assert lib.events == [(1, 0, 'raw', 14)]


@pytest.mark.parametrize('limits,expected', [((-6000, -600), -600), ((600, 2400), 600)])
def test_target_respects_hardware_db_range(proc, limits, expected):
    mic = element(db_range=limits, gains={0: limits[0]}, switches={0: 1})
    result = restore_capture_controls(Alsa([mic]), 123, proc)
    assert result['status'] == 'ready' and mic.gains[0] == expected


def test_capture_only_switch_and_fixed_gain_are_supported(proc):
    mic = element(volume=False, gains={}, raw={0: 0})
    fixed = element(db_range=None, raw_range=(4, 4), raw={0: 4}, switches={0: 1})
    lib = Alsa([mic, fixed])
    restore_capture_controls(lib, 123, proc)
    assert mic.switches[0] == 1 and fixed.raw[0] == 4
    assert lib.events == [(1, 0, 'switches', 1)]


@pytest.mark.parametrize('values', [dict(active=False), dict(volume=False, switch=False),
    dict(common_volume=True, common_switch=True), dict(exclusive=True),
    dict(name=b'Loopback Capture'), dict(name=b'Monitor'), dict(name=b'Stereo Mix')])
def test_does_not_change_playback_common_controls_or_input_routing(proc, values):
    lib = Alsa([element(**values)])
    assert restore_capture_controls(lib, 123, proc)['changed'] == []
    assert not lib.events


@pytest.mark.parametrize('stage', ['open', 'attach', 'load'])
def test_unavailable_mixer_does_not_prevent_recording_and_closes_resources(proc, stage):
    lib = Alsa([element()], open_error=-13 if stage == 'open' else 0,
               attach_error=-19 if stage == 'attach' else 0)
    if stage == 'load':
        lib.snd_mixer_load = Function(lambda *args: -22)
    result = restore_capture_controls(lib, 123, proc)
    assert result['status'] == 'unavailable' and result['errors']
    assert lib.closed == (stage != 'open') and lib.freed == 1


def test_a_control_write_failure_still_unmutes_and_processes_other_controls(proc):
    mic, other = element(), element(name=b'Capture')
    lib = Alsa([mic, other])
    original = lib.snd_mixer_selem_set_capture_dB
    lib.snd_mixer_selem_set_capture_dB = Function(
        lambda elem, *args: -13 if elem == 1 else original(elem, *args))
    result = restore_capture_controls(lib, 123, proc)
    assert result['status'] == 'partial'
    assert mic.gains[0] == -1200 and mic.switches[0] == 1
    assert other.gains[0] == 0 and other.switches[0] == 1


def test_missing_mixer_api_and_no_adjustable_controls_are_nonfatal(proc):
    assert restore_capture_controls(SimpleNamespace(), 123, proc)['status'] == 'unavailable'
    lib = Alsa()
    assert restore_capture_controls(lib, 123, proc)['status'] == 'ready'
    assert lib.closed == 1


def test_unavailable_gain_metadata_still_recovers_capture_mute(proc):
    mic = element(db_range=None, raw_range=None)
    result = restore_capture_controls(Alsa([mic]), 123, proc)
    assert result['status'] == 'partial'
    assert mic.switches[0] == 1


def test_alsa_capture_restores_before_start_without_external_program(monkeypatch, proc):
    from resources.lib import usb_capture
    lib = Alsa([element()])
    order = []
    lib.snd_pcm_open = Function(lambda out, *args: store(out, 123))
    lib.snd_pcm_set_params = Function(lambda *args: order.append('params') or 0)
    lib.snd_pcm_start = Function(lambda *args: order.append('start') or 0)
    lib.snd_pcm_close = Function(lambda *args: 0)
    for name in ('snd_pcm_readi', 'snd_pcm_prepare'):
        setattr(lib, name, Function(lambda *args: 0))
    lib.snd_strerror = Function(lambda *args: b'error')
    monkeypatch.setattr(audio.ctypes, 'CDLL', lambda name: lib)
    restore = usb_capture.restore_capture_controls
    def recover(native, handle):
        order.append('mixer')
        return restore(native, handle, proc)
    monkeypatch.setattr(usb_capture, 'restore_capture_controls', recover)
    with audio.AlsaCapture('my-custom-usb-pcm') as source:
        assert source.statistics['usb_capture_mixer']['status'] == 'ready'
    assert order == ['params', 'mixer', 'start']
    assert lib.attached == [b'hw:7']
