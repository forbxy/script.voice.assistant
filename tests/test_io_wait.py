"""Exercise real descriptors above FD_SETSIZE without opening thousands of files."""
import errno
import fcntl
import socket
from types import SimpleNamespace

import pytest

from resources.lib import io_wait


@pytest.fixture
def sockets():
    reader, writer = socket.socketpair()
    duplicate = fcntl.fcntl(reader, fcntl.F_DUPFD_CLOEXEC, 2048)
    reader.close()
    reader = socket.socket(fileno=duplicate)
    try:
        yield reader, writer
    finally:
        reader.close()
        writer.close()


def test_large_descriptor_read_write_timeout_and_object_identity(sockets):
    reader, writer = sockets
    assert reader.fileno() >= 2048
    assert io_wait.wait([reader], [], [], 0) == ([], [], [])
    assert io_wait.wait([], [reader], [], 0) == ([], [reader], [])
    writer.sendall(b'voice')
    assert io_wait.wait([reader, reader.fileno()], [reader], [], .1) == (
        [reader, reader.fileno()], [reader], [])
    assert reader.recv(5) == b'voice'


def test_peer_close_wakes_reader_to_observe_eof(sockets):
    reader, writer = sockets
    writer.close()
    assert io_wait.wait([reader], [], [], .1) == ([reader], [], [])
    assert reader.recv(1) == b''


def test_invalid_descriptors_and_timeouts_keep_existing_error_handlers(sockets):
    reader, writer = sockets
    descriptor = reader.fileno()
    reader.close()
    with pytest.raises(ValueError):io_wait.wait([reader], [], [], 0)
    with pytest.raises(OSError) as caught:io_wait.wait([descriptor], [], [], 0)
    assert caught.value.errno == errno.EBADF
    with pytest.raises(ValueError):io_wait.wait([], [], [], -.1)


def test_non_poll_platform_uses_its_native_select(monkeypatch):
    calls = []
    monkeypatch.setattr(io_wait, 'select', SimpleNamespace(select=lambda *args:calls.append(args) or ('r','w','x')))
    assert io_wait.wait([1], [2], [3], .25) == ('r','w','x')
    assert calls == [([1], [2], [3], .25)]
