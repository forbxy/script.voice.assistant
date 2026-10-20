"""Exercise actual native message ownership/parsing on an isolated D-Bus daemon."""
import ctypes as C
import select
import shutil
import subprocess
import time

import pytest

from resources.lib.bluez_gatt import GattBus, characteristics
from resources.lib.ur02_audio import TX, RX, CTL


@pytest.fixture
def buses(tmp_path, monkeypatch):
    daemon = shutil.which('dbus-daemon')
    if not daemon:
        pytest.skip('No local D-Bus daemon')
    address = 'unix:path=' + str(tmp_path / 'dbus.sock')
    process = subprocess.Popen([daemon, '--session', '--nofork', '--address=' + address],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    receiver = sender = None
    try:
        deadline = time.monotonic() + 2
        while not (tmp_path / 'dbus.sock').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        monkeypatch.setenv('DBUS_SYSTEM_BUS_ADDRESS', address)
        receiver = GattBus('A4:C1:38:9D:08:BD')
        sender = GattBus('A4:C1:38:9D:08:BD')
        lib = sender.lib
        lib.sd_bus_request_name.argtypes = [C.c_void_p, C.c_char_p, C.c_uint64]
        lib.sd_bus_request_name.restype = C.c_int
        sender._check(lib.sd_bus_request_name(sender.bus, b'org.bluez', 0))
        lib.sd_bus_message_new_signal.argtypes = [C.c_void_p, C.POINTER(C.c_void_p),
                                                 C.c_char_p, C.c_char_p, C.c_char_p]
        lib.sd_bus_message_new_signal.restype = C.c_int
        lib.sd_bus_send.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p]
        lib.sd_bus_send.restype = C.c_int
        lib.sd_bus_flush.argtypes = [C.c_void_p]
        lib.sd_bus_flush.restype = C.c_int
        yield receiver, sender
    finally:
        if receiver: receiver.close()
        if sender: sender.close()
        process.terminate(); process.wait(timeout=2)


def emit(sender, path, data):
    lib, check, m = sender.lib, sender._check, C.c_void_p()
    try:
        check(lib.sd_bus_message_new_signal(sender.bus, C.byref(m), path.encode(),
                                           b'org.freedesktop.DBus.Properties', b'PropertiesChanged'))
        check(lib.sd_bus_message_append(m, b's', C.c_char_p(b'org.bluez.GattCharacteristic1')))
        check(lib.sd_bus_message_open_container(m, b'a', b'{sv}'))
        check(lib.sd_bus_message_open_container(m, b'e', b'sv'))
        check(lib.sd_bus_message_append(m, b's', C.c_char_p(b'Value')))
        check(lib.sd_bus_message_open_container(m, b'v', b'ay'))
        buffer = C.create_string_buffer(data)
        check(lib.sd_bus_message_append_array(m, b'y', buffer, len(data)))
        for unused in range(3): check(lib.sd_bus_message_close_container(m))
        check(lib.sd_bus_message_open_container(m, b'a', b's'))
        check(lib.sd_bus_message_close_container(m))
        check(lib.sd_bus_send(sender.bus, m, None))
        check(lib.sd_bus_flush(sender.bus))
    finally:
        lib.sd_bus_message_unref(m)


def test_native_signal_copies_bytes_and_filters_other_device(buses):
    receiver, sender = buses
    path = '/org/bluez/hci0/dev_A4_C1_38_9D_08_BD/service003b/char0041'
    emit(sender, path.replace('9D_08_BD', '11_22_33'), b'wrong')
    emit(sender, path, bytes.fromhex('0b0100030000800100'))
    select.select([receiver.fd], [], [], 1)
    assert receiver.poll() == [(path, bytes.fromhex('0b0100030000800100'))]
    assert receiver.poll() == []
    # Exercise outgoing aya{sv} allocation and error cleanup without a real
    # Bluetooth adapter. The fake owner has no GATT method implementation.
    with pytest.raises(OSError): receiver.write(path, b'\x0c\x00')


def test_native_notification_overflow_is_reported_instead_of_discarding_audio(buses):
    receiver, sender = buses
    receiver.events.extend([('x', b'1')] * 1024)
    emit(sender, '/org/bluez/hci0/dev_A4_C1_38_9D_08_BD/service003b/char0041', b'\x08')
    select.select([receiver.fd], [], [], 1)
    with pytest.raises(OSError, match='缓冲超限'): receiver.poll()


def test_notifications_survive_a_synchronous_method_call_and_keep_burst_order(buses):
    receiver, sender = buses
    path = '/org/bluez/hci0/dev_A4_C1_38_9D_08_BD/service003b/char0041'
    values = [bytes([index]) * 20 for index in range(100)]
    for data in values:
        emit(sender, path, data)
    with pytest.raises(OSError):
        receiver.write(path, b'\x0c\x00')
    assert receiver.poll() == [(path, data) for data in values]


def test_uuid_discovery_filters_device_and_ota_and_bounds_query_timeout(monkeypatch):
    prefix = '/org/bluez/hci0/dev_A4_C1_38_9D_08_BD/'
    paths = [prefix + 'service1/char1', prefix + 'service1/char1/desc1',
             prefix + 'service2/char2', prefix + 'service3/char3', prefix + 'service4/char4']
    values = dict(zip([paths[0], *paths[2:]], ['other-vendor-ota', TX, RX, CTL]))
    def run(command, **unused):
        output = '\n'.join(paths) if 'tree' in command else 's "' + values[command[-3]] + '"\n'
        return type('Result', (), {'returncode': 0, 'stdout': output})()
    monkeypatch.setattr(subprocess, 'run', run)
    found = characteristics('a4:c1:38:9d:08:bd', {TX, RX, CTL})
    assert set(found) == {TX, RX, CTL} and paths[0] not in found.values()
    def timeout(*args, **unused): raise subprocess.TimeoutExpired('busctl', 3)
    monkeypatch.setattr(subprocess, 'run', timeout)
    with pytest.raises(OSError, match='超时'): characteristics('a4:c1:38:9d:08:bd', {TX})
