from pathlib import Path
import ctypes.util
import pytest
from resources.lib import native_platform as module


@pytest.mark.parametrize('machine,size,major,architecture', [('aarch64', 4, 62, 'linux_armv7l'),
    ('aarch64', 8, 63, 'linux_aarch64'), ('armv7l', 4, 62, 'linux_armv7l'),
    ('x86_64', 8, 60, 'linux_x86_64'), ('AMD64', 8, 61, 'linux_x86_64')])
def test_remote_library_uses_python_bits_and_ffmpeg_abi(monkeypatch, machine, size, major, architecture):
    monkeypatch.setattr(module.platform, 'machine', lambda: machine)
    monkeypatch.setattr(module.struct, 'calcsize', lambda value: size)
    monkeypatch.setattr(ctypes.util, 'find_library', lambda name: 'libavcodec.so.' + str(major))
    result = module.decoder_path('/addon', 'cmcc', 'libcmcc-siren-ffmpeg')
    assert result == Path('/addon/resources/native') / architecture / 'cmcc' / ('libcmcc-siren-ffmpeg' + str(major) + '.so')
