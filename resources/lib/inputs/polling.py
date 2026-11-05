"""Wait once for the selected input; publish service properties only on change."""
from ..io_wait import wait


class Properties:
    def __init__(self, window):
        self.window = window
        self.values = {}

    def set(self, name, value):
        if self.values.get(name) != value:
            self.window.setProperty('VoiceAssistant.' + name, value)
            self.values[name] = value


def active(adapters, selected):
    # Finish a previous input's lease/IPC after a setting change before leaving
    # it asleep. Disabled, unused inputs have no HID or clients to service.
    return [controller for source, prefix, controller, discover in adapters
            if source == selected or controller.session is not None or controller.peers]


def wait_inputs(controllers, timeout):
    readers, writers = [], []
    for controller in controllers:
        incoming, outgoing = controller.poll_descriptors()
        readers.extend(incoming)
        writers.extend(outgoing)
    wait(readers, writers, [], max(0, timeout))
