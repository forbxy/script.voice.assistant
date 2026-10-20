from pathlib import Path
import pytest
from resources.lib import native_platform as module


@pytest.mark.parametrize('size,major,architecture', [(4, 62, 'linux_armv7l'), (8, 63, 'linux_aarch64')])
def test_remote_library_uses_python_bits_and_ffmpeg_abi(monkeypatch, size, major, architecture):
    monkeypatch.setattr(module.struct, 'calcsize', lambda value: size)
    monkeypatch.setattr(module.ctypes.util, 'find_library', lambda name: 'libavcodec.so.' + str(major))
    result = module.decoder_path('/addon', 'cmcc', 'libcmcc-siren-ffmpeg')
    assert result == Path('/addon/resources/native') / architecture / 'cmcc' / ('libcmcc-siren-ffmpeg' + str(major) + '.so')
