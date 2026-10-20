"""Restore usable USB capture controls through the existing system ALSA library."""
import ctypes
from pathlib import Path


class MixerError(Exception):
    pass


def _check(value):
    if value < 0:
        raise MixerError('ALSA error {}'.format(value))
    return value


def _declare(lib, signatures):
    for name, (args, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = args, result


def _card(lib, pcm):
    pointer = ctypes.c_void_p
    _declare(lib, {
        'snd_pcm_info_malloc': ([ctypes.POINTER(pointer)], ctypes.c_int),
        'snd_pcm_info': ([pointer, pointer], ctypes.c_int),
        'snd_pcm_info_get_card': ([pointer], ctypes.c_int),
        'snd_pcm_info_free': ([pointer], None),
    })
    info = pointer()
    _check(lib.snd_pcm_info_malloc(ctypes.byref(info)))
    try:
        _check(lib.snd_pcm_info(pcm, info))
        return lib.snd_pcm_info_get_card(info)
    finally:
        lib.snd_pcm_info_free(info)


def _mixer_api(lib):
    pointer, integer, long = ctypes.c_void_p, ctypes.c_int, ctypes.c_long
    signatures = {
        'snd_mixer_open': ([ctypes.POINTER(pointer), integer], integer),
        'snd_mixer_attach': ([pointer, ctypes.c_char_p], integer),
        'snd_mixer_selem_register': ([pointer, pointer, pointer], integer),
        'snd_mixer_load': ([pointer], integer),
        'snd_mixer_close': ([pointer], integer),
        'snd_mixer_first_elem': ([pointer], pointer),
        'snd_mixer_elem_next': ([pointer], pointer),
        'snd_mixer_selem_get_name': ([pointer], ctypes.c_char_p),
    }
    for name in ('is_active', 'has_capture_volume', 'has_capture_switch',
                 'has_common_volume', 'has_common_switch', 'has_capture_switch_exclusive'):
        signatures['snd_mixer_selem_' + name] = ([pointer], integer)
    signatures['snd_mixer_selem_has_capture_channel'] = ([pointer, integer], integer)
    for name in ('get_capture_dB_range', 'get_capture_volume_range'):
        signatures['snd_mixer_selem_' + name] = ([pointer, ctypes.POINTER(long), ctypes.POINTER(long)], integer)
    for name in ('dB', 'volume', 'switch'):
        value_type = integer if name == 'switch' else long
        signatures['snd_mixer_selem_get_capture_' + name] = ([pointer, integer, ctypes.POINTER(value_type)], integer)
        arguments = [pointer, integer, value_type] + ([integer] if name == 'dB' else [])
        signatures['snd_mixer_selem_set_capture_' + name] = (arguments, integer)
    _declare(lib, signatures)


def _range(function, element):
    low, high = ctypes.c_long(), ctypes.c_long()
    _check(function(element, ctypes.byref(low), ctypes.byref(high)))
    return low.value, high.value


def _value(function, element, channel, kind=ctypes.c_long):
    value = kind()
    _check(function(element, channel, ctypes.byref(value)))
    return value.value


def _restore_element(lib, element, changed, errors):
    if not lib.snd_mixer_selem_is_active(element):
        return
    name = (lib.snd_mixer_selem_get_name(element) or b'').decode('utf-8', 'replace')
    # Leave input routing and playback-monitor paths as selected by the user.
    if (lib.snd_mixer_selem_has_capture_switch_exclusive(element)
            or any(word in name.lower() for word in ('loopback', 'monitor', 'stereo mix', 'what u hear'))):
        return
    volume = (lib.snd_mixer_selem_has_capture_volume(element)
              and not lib.snd_mixer_selem_has_common_volume(element))
    switch = (lib.snd_mixer_selem_has_capture_switch(element)
              and not lib.snd_mixer_selem_has_common_switch(element))
    if not volume and not switch:
        return
    db_range = raw_range = None
    if volume:
        try:
            low, high = _range(lib.snd_mixer_selem_get_capture_dB_range, element)
            if high > low:
                db_range = (low, high)
        except MixerError:
            pass  # Some USB devices provide volume steps without a dB scale.
        if db_range is None:
            try:
                raw_range = _range(lib.snd_mixer_selem_get_capture_volume_range, element)
            except MixerError as error:
                errors.append(dict(control=name, error=str(error)))
                volume = False
    # ALSA channel IDs run from 0 (front left / mono) through 31.
    for channel in range(32):
        if not lib.snd_mixer_selem_has_capture_channel(element, channel):
            continue
        try:
            if volume:
                if db_range is not None:
                    current = _value(lib.snd_mixer_selem_get_capture_dB, element, channel)
                    target = min(max(0, db_range[0]), db_range[1])
                    if current < target:
                        _check(lib.snd_mixer_selem_set_capture_dB(element, channel, target, 1))
                        actual = _value(lib.snd_mixer_selem_get_capture_dB, element, channel)
                        changed.append(dict(control=name, channel=channel, kind='gain_db',
                                            before=current / 100, after=actual / 100))
                else:
                    low, high = raw_range
                    current = _value(lib.snd_mixer_selem_get_capture_volume, element, channel)
                    if current == low and high > low:
                        target = low + (high - low + 1) // 2
                        _check(lib.snd_mixer_selem_set_capture_volume(element, channel, target))
                        changed.append(dict(control=name, channel=channel, kind='gain_steps',
                                            before=current, after=target))
        except MixerError as error:
            errors.append(dict(control=name, channel=channel, error=str(error)))
        # A gain write failure must not prevent restoring a separate capture mute.
        try:
            if switch and not _value(lib.snd_mixer_selem_get_capture_switch, element, channel, ctypes.c_int):
                _check(lib.snd_mixer_selem_set_capture_switch(element, channel, 1))
                changed.append(dict(control=name, channel=channel, kind='unmute'))
        except MixerError as error:
            errors.append(dict(control=name, channel=channel, error=str(error)))


def restore_capture_controls(lib, pcm, proc=Path('/proc/asound')):
    """Best effort, per-open recovery; never alter playback or non-USB cards.

    Resolve the physical card from the opened PCM, including custom ALSA names.
    No model names, vendor IDs, card numbers or mixer element names are required.
    """
    result = dict(status='unavailable', changed=[], errors=[])
    mixer = ctypes.c_void_p()
    try:
        card = _card(lib, pcm)
        directory = proc / ('card' + str(card))
        if card < 0 or not any((directory / marker).is_file() for marker in ('usbid', 'stream0')):
            result['status'] = 'not_usb'
            return result
        result['card'] = card
        _mixer_api(lib)
        _check(lib.snd_mixer_open(ctypes.byref(mixer), 0))
        _check(lib.snd_mixer_attach(mixer, ('hw:' + str(card)).encode()))
        _check(lib.snd_mixer_selem_register(mixer, None, None))
        _check(lib.snd_mixer_load(mixer))
        element = lib.snd_mixer_first_elem(mixer)
        while element:
            try:
                _restore_element(lib, element, result['changed'], result['errors'])
            except MixerError as error:
                result['errors'].append(dict(error=str(error)))
            element = lib.snd_mixer_elem_next(element)
        result['status'] = 'partial' if result['errors'] else 'ready'
    except (AttributeError, MixerError, OSError) as error:
        # USB devices without adjustable capture controls still record normally.
        result['errors'].append(dict(error=str(error)))
    finally:
        if mixer:
            lib.snd_mixer_close(mixer)
    return result
