import socket
import threading
from types import SimpleNamespace

from resources.lib.cmcc_service import Controller
from resources.lib.inputs.polling import Properties, active, wait_inputs


def test_selected_input_and_previous_lease_are_serviced_without_polling_unused_adapters():
    controllers = [SimpleNamespace(session=None, peers={}) for unused in range(3)]
    adapters = [(name, name, controller, None)
                for name, controller in zip(('cmcc', 'nanosic', 'ur02'), controllers)]
    assert active(adapters, 'nanosic') == [controllers[1]]
    # A setting change must still flush the old recording and release its lock.
    controllers[0].session = object()
    controllers[2].peers['pending-client'] = object()
    assert active(adapters, 'usb') == [controllers[0], controllers[2]]


def test_hid_input_wakes_long_idle_wait_and_includes_extra_gatt_descriptor(tmp_path):
    controller = Controller(tmp_path, '', lambda token: None)
    incoming, sender = socket.socketpair()
    gatt, gatt_sender = socket.socketpair()
    controller.hid = incoming.fileno()
    controller.extra_readers = lambda: [gatt]
    finished = threading.Event()
    try:
        assert controller.poll_descriptors() == ([incoming.fileno(), gatt], [])
        for source in (sender, gatt_sender):
            worker = threading.Thread(target=lambda: (wait_inputs([controller], 5), finished.set()))
            worker.start()
            source.sendall(b'x')
            try:
                assert finished.wait(1), 'HID/GATT data did not wake the idle service'
            finally:
                worker.join(6)
            (incoming if source is sender else gatt).recv(1)
            finished.clear()
    finally:
        incoming.close(); sender.close(); gatt.close(); gatt_sender.close()


def test_buffered_pcm_wakes_wait_when_client_can_accept_output(tmp_path):
    controller = Controller(tmp_path, '', lambda token: None)
    peer, client = socket.socketpair()
    try:
        controller.session = SimpleNamespace(peer=peer, outgoing=bytearray(b'pcm'))
        controller.peers[peer] = {}
        assert controller.poll_descriptors() == ([peer], [peer])
        finished = threading.Event()
        worker = threading.Thread(target=lambda: (wait_inputs([controller], 5), finished.set()))
        worker.start()
        try:
            assert finished.wait(1), 'PCM delivery waited for the idle timeout'
        finally:
            worker.join(6)
    finally:
        peer.close(); client.close()


def test_unchanged_status_does_not_rewrite_kodi_but_new_sessions_publish_immediately():
    writes = []
    home = SimpleNamespace(setProperty=lambda *args: writes.append(args))
    properties = Properties(home)
    for unused in range(100):
        properties.set('CMCCConnected', 'true')
        properties.set('CMCCSession', '')
    assert writes == [('VoiceAssistant.CMCCConnected', 'true'), ('VoiceAssistant.CMCCSession', '')]
    properties.set('CMCCSession', 'a' * 32)
    properties.set('CMCCSession', 'a' * 32)
    properties.set('CMCCSession', '')
    properties.set('CMCCSession', 'b' * 32)
    assert writes[-3:] == [('VoiceAssistant.CMCCSession', 'a' * 32),
                          ('VoiceAssistant.CMCCSession', ''),
                          ('VoiceAssistant.CMCCSession', 'b' * 32)]
