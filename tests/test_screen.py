from resources.lib.data_paths import data_path
import json
import fcntl
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import zlib
import xml.etree.ElementTree as ET

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence import screen
from resources.lib.intelligence.permissions import Permissions
from test_codex_agent import dynamic, finished, make_agent


@pytest.fixture
def kodi(monkeypatch, tmp_path):
    monkeypatch.setattr(screen, 'framebuffer', lambda cancelled: None)
    monkeypatch.setattr(screen, 'amvideocap', lambda *args: None)
    class Render:
        def getAspectRatio(self):return 16 / 9
        def capture(self, width, height):
            self.width, self.height = width, height
            state.captures.append((width, height))
        def getImage(self, wait):
            state.waits.append(wait)
            if state.no_frame:return bytearray()
            return bytearray(b'\x12\x34\x56\xff' * (self.width * self.height))
        def getImageFormat(self):return state.pixel_format
    state = SimpleNamespace(playing=True, captures=[], waits=[], commands=[], no_frame=False,
                            pixel_format='BGRA', write_screen=True, properties={}, dialog=9999, focus=[])
    def builtin(command):
        state.commands.append(command)
        if state.write_screen:
            path = Path(command[len('TakeScreenshot('):-1])
            path.write_bytes(screen.png_bgra(b'\x12\x34\x56\xff', 1, 1))
    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace(
        Player=lambda: SimpleNamespace(isPlayingVideo=lambda: state.playing),
        RenderCapture=Render, executebuiltin=builtin, sleep=lambda ms: None))
    monkeypatch.setitem(sys.modules, 'xbmcaddon', SimpleNamespace(
        Addon=lambda identifier: SimpleNamespace(getAddonInfo=lambda key: str(tmp_path))))
    monkeypatch.setitem(sys.modules, 'xbmcvfs', SimpleNamespace(translatePath=lambda path: path))
    home = SimpleNamespace(getProperty=lambda key: state.properties.get(key, ''),
        setProperty=lambda key, value: state.properties.__setitem__(key, value),
        clearProperty=lambda key: state.properties.pop(key, None))
    group = SimpleNamespace(isVisible=lambda: not state.properties.get(screen.HIDE_PROPERTY))
    window = SimpleNamespace(getControl=lambda identifier: group, getFocusId=lambda: 400,
                             setFocusId=state.focus.append)
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace(Window=lambda identifier: home if identifier == 10000 else window,
        getCurrentWindowDialogId=lambda: state.dialog))
    return state


def test_bgra_encoding_has_correct_rgb_pixel_and_png_crc():
    png = screen.png_bgra(b'\x12\x34\x56\xff', 1, 1)
    assert screen.png_size(png) == (1, 1)
    position = 8;compressed = b''
    while position < len(png):
        length = struct.unpack('>I', png[position:position + 4])[0]
        name = png[position + 4:position + 8];data = png[position + 8:position + 8 + length]
        crc = struct.unpack('>I', png[position + 8 + length:position + 12 + length])[0]
        assert crc == zlib.crc32(name + data) & 0xffffffff
        if name == b'IDAT':compressed += data
        position += length + 12
    assert zlib.decompress(compressed) == b'\0\x56\x34\x12'
    with pytest.raises(ValueError):screen.png_bgra(b'', 1, 1)
    with pytest.raises(ValueError):screen.png_size(png[:-2])


def test_auto_uses_video_only_without_gui_or_playback_actions(kodi, tmp_path):
    result = screen.capture()
    assert result['source'] == 'video'
    assert screen.png_size(result['data']) == (960, 540)
    assert kodi.captures == [(960, 540)] and not kodi.commands
    assert not list(tmp_path.iterdir())


def test_auto_falls_back_to_screen_when_no_video_and_removes_temporary_files(kodi, tmp_path):
    kodi.playing = False
    result = screen.capture()
    assert result['source'] == 'screen' and result['width'] == result['height'] == 1
    assert not kodi.captures and len(kodi.commands) == 1
    assert not list((data_path(tmp_path, 'captures')).iterdir())
    assert screen.capture('screen')['source'] == 'screen'
    with pytest.raises(ValueError, match='没有'):screen.capture('video')


def test_explicit_screen_overrides_video_and_describes_visible_overlays(kodi):
    result = screen.capture('screen')
    assert result['source'] == 'screen' and '弹窗' in result['note']
    assert not kodi.captures and len(kodi.commands) == 1


def test_failed_video_capture_never_silently_returns_an_obscured_screen(kodi, monkeypatch):
    kodi.no_frame = True
    clock = iter([0, 3])
    monkeypatch.setattr(screen.time, 'monotonic', lambda: next(clock))
    with pytest.raises(ValueError, match='未返回完整视频帧'):screen.capture('video')
    assert not kodi.commands


def test_failed_render_capture_uses_driver_video_only_without_changing_playback(kodi, monkeypatch):
    kodi.no_frame = True
    calls = []
    data = screen.png_bgra(b'\x12\x34\x56\xff', 1, 1)
    def native(width, height, cancelled):
        calls.append((width, height))
        return data
    monkeypatch.setattr(screen, 'amvideocap', native)
    result = screen.capture('video')
    assert calls == [(960, 540)] and result['data'] == data
    assert result['source'] == 'video' and result['backend'] == 'amvideocap'
    assert not kodi.commands and not kodi.properties


def test_amvideocap_crops_aligned_rows_and_preserves_bgr_channels(tmp_path, monkeypatch):
    import os
    device, system = tmp_path / 'device', tmp_path / 'sys'
    device.write_bytes(b'unchanged');system.mkdir()
    (system / 'frame_addr').write_text('0x12340000-0x12350000-0x0\n')
    monkeypatch.setattr(Path, 'is_char_device', lambda self: self == device)
    closed, calls, reads = [], [], []
    monkeypatch.setattr(os, 'open', lambda path, flags: 42)
    monkeypatch.setattr(os, 'close', closed.append)
    raw = b'\x12\x34\x56\x78\x9a\xbc' + b'\xee' * 90
    raw += b'\x11\x22\x33\x44\x55\x66' + b'\xdd' * 90
    def pread(descriptor, count, offset):
        assert (descriptor, count, offset) == (42, 192, 0)
        reads.append(True)
        return b'\xee' * 192 if len(reads) == 1 else raw
    monkeypatch.setattr(os, 'pread', pread)
    def ioctl(descriptor, command, value):
        calls.append((command, value if isinstance(value, int) else None))
        expected = {0x80045631: 300, 0x80045610: 0x1000200, 0x80045611: 32, 0x80045612: 2}
        if command in expected:struct.pack_into('I', value, 0, expected[command])
    monkeypatch.setattr(fcntl, 'ioctl', ioctl)
    data = screen.amvideocap(2, 2, lambda: False, device, system)
    expected = b'\x12\x34\x56\xff\x78\x9a\xbc\xff\x11\x22\x33\xff\x44\x55\x66\xff'
    assert data == screen.png_bgra(expected, 2, 2)
    assert (0x40045606, 0) in calls and (0x40085605, 500) in calls
    assert closed == [42] and len(reads) == 2 and device.read_bytes() == b'unchanged'


def test_amvideocap_rejects_compressed_only_frame_before_opening_device(tmp_path, monkeypatch):
    import os
    device, system = tmp_path / 'device', tmp_path / 'sys'
    device.touch();system.mkdir();(system / 'frame_addr').write_text('0x0-0x0-0x0\n')
    monkeypatch.setattr(Path, 'is_char_device', lambda self: self == device)
    monkeypatch.setattr(os, 'open', lambda *args: pytest.fail('A compressed-only frame must not be captured'))
    with pytest.raises(ValueError, match='线性画面副本'):
        screen.amvideocap(32, 32, lambda: False, device, system)


@pytest.mark.parametrize('fault', ['short', 'state', 'size', 'ioctl', 'cancel'])
def test_amvideocap_failures_close_the_handle_and_never_return_stale_pixels(tmp_path, monkeypatch, fault):
    import os
    device, system = tmp_path / 'device', tmp_path / 'sys'
    device.touch();system.mkdir();(system / 'frame_addr').write_text('0x1234-0x5678-0x0\n')
    monkeypatch.setattr(Path, 'is_char_device', lambda self: self == device)
    closed, read = [], []
    monkeypatch.setattr(os, 'open', lambda *args: 42)
    monkeypatch.setattr(os, 'close', closed.append)
    def pread(*args):
        read.append(True)
        return b'\0' * (1 if fault == 'short' else 96)
    monkeypatch.setattr(os, 'pread', pread)
    def ioctl(descriptor, command, value):
        if fault == 'ioctl':raise OSError('unavailable')
        expected = {0x80045631: 0xffff if fault == 'state' else 300,
                    0x80045610: 0x1000200, 0x80045611: 1 if fault == 'size' else 32, 0x80045612: 1}
        if command in expected:struct.pack_into('I', value, 0, expected[command])
    monkeypatch.setattr(fcntl, 'ioctl', ioctl)
    with pytest.raises(InterruptedError if fault == 'cancel' else ValueError):
        screen.amvideocap(32, 1, lambda: fault == 'cancel' and bool(read), device, system)
    assert closed == [42]


def test_cancel_screen_capture_cleans_files_and_no_future_capture_runs(kodi, tmp_path):
    kodi.write_screen = False
    calls = [False, False, True]
    with pytest.raises(InterruptedError):screen.capture('screen', lambda: calls.pop(0))
    assert not list((data_path(tmp_path, 'captures')).iterdir())
    kodi.pixel_format = 'unexpected'
    with pytest.raises(ValueError, match='像素格式'):screen.capture('video')


def test_native_image_tool_content_is_not_embedded_in_text_or_activity_trace(tmp_path):
    event = dynamic({'source': 'video'});event['params']['tool'] = 'capture_screen'
    agent, protocol = make_agent(tmp_path, [event, event, finished()])
    captures = []
    def capture(source, cancelled):
        captures.append(source)
        return {'source': source, 'width': 1, 'height': 1, 'data': screen.png_bgra(b'\0\0\0\xff', 1, 1)}
    agent.tools = KodiTools(lambda *args: {}, permissions=Permissions('media'), screen_capture=capture)
    result = agent.run('请描述当前画面')
    responses = [r['result'] for r in protocol.sent if r.get('id') == event['id']]
    assert captures == ['video'] and len(responses) == 2 and responses[0] == responses[1]
    content = responses[0]['contentItems']
    assert [row['type'] for row in content] == ['inputText', 'inputImage']
    assert content[1]['imageUrl'].startswith('data:image/png;base64,')
    assert json.loads(content[0]['text'])['image_attached'] is True
    assert '_image' not in content[0]['text'] and 'base64' not in json.dumps(result)
    assert not agent.tools.acted and agent.tools.ui_action is None


def test_failed_capture_returns_only_an_error_not_stale_image(tmp_path):
    event = dynamic({});event['params']['tool'] = 'capture_screen'
    agent, protocol = make_agent(tmp_path, [event, finished()])
    def unavailable(source, cancelled):raise ValueError('播放器未提供画面')
    agent.tools = KodiTools(lambda *args: {}, screen_capture=unavailable)
    result = agent.run('查看画面')
    response = next(r['result'] for r in protocol.sent if r.get('id') == event['id'])
    assert response['success'] is False and len(response['contentItems']) == 1
    assert result['tools'][0]['ok'] is False


def test_osd_framebuffer_reads_active_page_and_removes_stride_padding(tmp_path, monkeypatch):
    system = tmp_path / 'sys';system.mkdir()
    (system / 'name').write_text('OSD FB');(system / 'stride').write_text('16')
    device = tmp_path / 'fb';original = bytes(range(64));device.write_bytes(original)
    info = [2, 2, 4, 4, 1, 2, 32, 0, 16, 8, 0, 8, 8, 0, 0, 8, 0, 24, 8, 0, 0, 0, 0, 0]
    def ioctl(stream, command, output):
        assert command == 0x4600 and stream.mode == 'rb'
        struct.pack_into('24I', output, 0, *info)
    monkeypatch.setattr(fcntl, 'ioctl', ioctl)
    data, width, height = screen.framebuffer(lambda: False, device, system)
    assert (width, height) == (2, 2)
    assert data == screen.png_bgra(original[36:44] + original[52:60], 2, 2)
    assert device.read_bytes() == original
    info[6] = 16
    with pytest.raises(ValueError, match='像素格式'):screen.framebuffer(lambda: False, device, system)
    (system / 'name').write_text('Other renderer')
    assert screen.framebuffer(lambda: False, device, system) is None


def test_osd_capture_uses_real_interface_instead_of_video_only_builtin(kodi, monkeypatch):
    monkeypatch.setattr(screen, 'framebuffer', lambda cancelled: (
        screen.png_bgra(b'\x12\x34\x56\xff', 1, 1), 1, 1))
    result = screen.capture('screen')
    assert result['backend'] == 'framebuffer' and result['width'] == 1
    assert not kodi.commands and not kodi.captures


def test_screen_hides_assistant_and_restores_focus_even_when_capture_fails(kodi, monkeypatch):
    kodi.dialog = 13001
    def failed(cancelled):
        assert kodi.properties[screen.HIDE_PROPERTY] == 'true'
        raise ValueError('cannot capture')
    monkeypatch.setattr(screen, 'framebuffer', failed)
    with pytest.raises(ValueError, match='cannot capture'):screen.capture('screen')
    assert not kodi.properties and kodi.focus == [400]


def test_screen_cancel_always_restores_assistant_visibility(kodi, monkeypatch):
    def cancelled(cancelled):raise InterruptedError('cancelled')
    monkeypatch.setattr(screen, 'framebuffer', cancelled)
    with pytest.raises(InterruptedError):screen.capture('screen')
    assert screen.HIDE_PROPERTY not in kodi.properties


def test_all_assistant_xml_controls_are_inside_the_hideable_group():
    root = Path(__file__).resolve().parents[1] / 'resources/skins/Default/1080i'
    for path in root.glob('*.xml'):
        controls = ET.parse(path).getroot().find('controls')
        groups = controls.findall('control')
        assert len(groups) == 1 and groups[0].get('id') == str(screen.CAPTURE_GROUP), path.name
        assert 'VoiceAssistant.CaptureHidden' in groups[0].findtext('visible'), path.name
