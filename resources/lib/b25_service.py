"""Homatics ATVV adapter; B21/B25 identities and RemoteG11 Bluetooth name."""
from pathlib import Path

from .ur02_service import Ur02Controller

DOWN = bytes.fromhex('022102')
UP = bytes.fromhex('020000')
IDENTITIES = {'B21': '0005:00000957:00000006', 'B25': '0005:00000957:0000000A'}


def find_remote(sysfs=Path('/sys/class/hidraw')):
    for path in sorted(sysfs.glob('hidraw*')):
        try:
            info = dict(line.split('=', 1) for line in
                        (path / 'device/uevent').read_text().splitlines() if '=' in line)
        except OSError:
            continue
        name, identity = info.get('HID_NAME'), info.get('HID_ID', '').upper()
        expected = IDENTITIES.get(name)
        # RemoteG11 uses exact Bluetooth name matching. Ur02Controller.attach
        # validates the ATVV characteristics and capabilities before capture.
        if (expected and identity == expected
                or name == 'RemoteG11' and identity.startswith('0005:')):
            return '/dev/' + path.name
    return None


class B25Controller(Ur02Controller):
    remote_name = 'Homatics'
    socket_name = 'b25.sock'
    input_source = 'b25'

    def on_report(self, data, maximum=10):
        if data == DOWN and not self.pressed:
            self.pressed = True
            self.trigger('hid', maximum)
        elif data == UP:
            # B25 emits this immediately, even when the key is physically held.
            self.pressed = False
        elif self.caps is None:
            self.query()
