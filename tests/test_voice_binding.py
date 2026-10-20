import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import pytest
from resources.lib.voice_binding import ACTION, bind, button_code, target_file, bound_codes, unbind, remove_binding, binding_status


def test_resolve_global_voice_key_ignores_dialog_back_and_honors_overrides(tmp_path):
    first = tmp_path / 'a.xml'
    first.write_text('<keymap><global><keyboard><f5>' + ACTION + '</f5></keyboard></global><Window13000><keyboard><f5>Back</f5></keyboard></Window13000></keymap>')
    assert bound_codes(tmp_path) == {'61588'}
    last = tmp_path / 'z.XML'
    last.write_text('<keymap><global><keyboard><key id="61588">Info</key></keyboard></global></keymap>')
    assert bound_codes(tmp_path) == set()
    bind(tmp_path, 61588)
    assert bound_codes(tmp_path) == {'61588'}


def test_last_xml_global_only_preserves_other_devices_windows_and_backup(tmp_path):
    first = tmp_path / 'a-remote.xml'
    first.write_text('<keymap><global><keyboard><f5>First</f5></keyboard></global></keymap>')
    last = tmp_path / 'z-current.XML'
    original = b'<keymap><!--keep comment--><global><keyboard><f5>Old</f5><key id="61588">Duplicate</key><f5 mod="longpress">OldHold</f5><f5 mod="ctrl">Control</f5><left>Left</left></keyboard><remote><select>Select</select></remote></global><FullscreenVideo><keyboard><f5>Info</f5></keyboard></FullscreenVideo><joystick profile="game.controller.default"><a>Select</a></joystick></keymap>'
    last.write_bytes(original)
    (tmp_path / 'zz-backup.xml.saved').write_text('not a keymap')
    assert bind(tmp_path, 61588) == last
    root = ET.parse(last).getroot()
    assert root.find('FullscreenVideo/keyboard/f5').text == 'Info'
    assert root.find('global/remote/select').text == 'Select'
    assert root.find('global/keyboard/left').text == 'Left'
    assert root.find('global/keyboard/f5').get('mod') == 'ctrl'
    keys = root.findall('global/keyboard/key')
    assert [(k.get('id'), k.get('mod', ''), k.text) for k in keys] == [('61588', '', ACTION), ('61588', 'longpress', ACTION)]
    assert '<!--keep comment-->' in last.read_text()
    assert last.with_suffix('.XML.voice-assistant.bak').read_bytes() == original
    assert 'First' in first.read_text()
    once = last.read_bytes()
    bind(tmp_path, 61588 | 0x01000000)
    assert last.read_bytes() == once
    assert last.with_suffix('.XML.voice-assistant.bak').read_bytes() == original


def test_empty_directory_creates_global_mapping(tmp_path):
    target = bind(tmp_path / 'new', 61527)
    assert target.name == 'voice-assistant.xml'
    assert ET.parse(target).find('global/keyboard/key').text == ACTION


@pytest.mark.parametrize('content', ['<broken>', '<other/>'])
def test_bad_last_xml_never_falls_back_or_overwrites(tmp_path, content):
    (tmp_path / 'a.xml').write_text('<keymap/>')
    last = tmp_path / 'z.xml'
    last.write_text(content)
    with pytest.raises(ValueError):
        bind(tmp_path, 61588)
    assert last.read_text() == content
    assert (tmp_path / 'a.xml').read_text() == '<keymap/>'
    assert not list(tmp_path.glob('*.bak'))


def test_filename_order_ignores_case_and_creates_missing_global(tmp_path):
    (tmp_path / 'A.xml').write_text('<keymap/>')
    last = tmp_path / 'Z.xml'
    last.write_text('<keymap><Home><keyboard><a>Keep</a></keyboard></Home></keymap>')
    assert target_file(tmp_path) == last
    bind(tmp_path, 61588)
    assert ET.parse(last).find('Home/keyboard/a').text == 'Keep'


def test_listener_ignores_synthetic_actions_and_captures_once(monkeypatch):
    class Window:
        def __init__(self, *a, **k):pass
    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace(WindowXMLDialog=Window))
    monkeypatch.setitem(sys.modules, 'xbmcvfs', SimpleNamespace())
    path = Path(__file__).resolve().parents[1] / 'resources/lib/ui/key_binding.py'
    spec = importlib.util.spec_from_file_location('resources.lib.ui.binding_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    listener = module.KeyListener()
    listener.onInit()
    listener.onAction(SimpleNamespace(getButtonCode=lambda: 0))
    assert not listener.received.is_set()
    listener.onAction(SimpleNamespace(getButtonCode=lambda: 61588 | 0x01000000))
    listener.onAction(SimpleNamespace(getButtonCode=lambda: 61527))
    assert listener.key == '61588' and listener.received.is_set()
    assert button_code(-1) is None


def test_remove_bindings_from_all_xmls_preserves_unrelated_keys_and_deletes_empty_file(tmp_path):
    first = tmp_path / 'a.xml'
    first.write_text('<keymap><!--keep--><global><keyboard><f5>' + ACTION + '</f5>'
        '<f5 mod="longpress">' + ACTION + '</f5><left>Left</left></keyboard>'
        '<remote><select>Select</select></remote></global><Home><keyboard><f5>Back</f5></keyboard></Home></keymap>')
    empty = bind(tmp_path / 'only', 61588)
    second = tmp_path / 'z.XML'
    second.write_bytes(empty.read_bytes())
    other = tmp_path / 'b.xml'
    other.write_text('<keymap><global><keyboard><key id="10"/></keyboard></global></keymap>')
    before, untouched = first.read_bytes(), other.read_bytes()
    first.chmod(0o640)
    assert unbind(tmp_path) == [first, second]
    assert not second.exists()
    root = ET.parse(first).getroot()
    assert root.find('global/keyboard/f5') is None
    assert root.find('global/keyboard/left').text == 'Left'
    assert root.find('global/remote/select').text == 'Select'
    assert root.find('Home/keyboard/f5').text == 'Back'
    assert '<!--keep-->' in first.read_text()
    assert first.stat().st_mode & 0o777 == 0o640
    assert first.with_suffix('.xml.voice-assistant-unbind.bak').read_bytes() == before
    assert other.read_bytes() == untouched
    assert bound_codes(tmp_path) == set()
    assert unbind(tmp_path) == []


def test_removal_only_deletes_emptied_ours_not_an_unrelated_empty_keymap(tmp_path):
    ours = bind(tmp_path, 61588)
    untouched = tmp_path / 'a.xml'
    untouched.write_text('<keymap><global><keyboard/></global></keymap>')
    unbind(tmp_path)
    assert not ours.exists()
    assert untouched.exists()


def test_empty_other_mapping_and_other_window_voice_entry_are_handled(tmp_path):
    path = tmp_path / 'keys.xml'
    path.write_text('<keymap><global><keyboard><f5>' + ACTION + '</f5><left/></keyboard></global>'
        '<FullscreenVideo><remote><select>' + ACTION + '</select></remote></FullscreenVideo></keymap>')
    unbind(tmp_path)
    root = ET.parse(path).getroot()
    assert root.find('global/keyboard/left') is not None
    assert root.find('FullscreenVideo/remote/select') is None


def test_remove_does_not_restore_or_modify_a_bind_backup(tmp_path):
    path = tmp_path / 'keys.xml'
    path.write_text('<keymap><global><keyboard><f5>Old</f5></keyboard></global></keymap>')
    bind(tmp_path, 61588)
    backup = path.with_suffix('.xml.voice-assistant.bak')
    before = backup.read_bytes()
    unbind(tmp_path)
    assert not path.exists()
    assert backup.read_bytes() == before


def test_bad_xml_stops_removal_before_any_mapping_is_modified(tmp_path):
    path = bind(tmp_path, 61588)
    before = path.read_bytes()
    (tmp_path / 'z.xml').write_text('<broken>')
    with pytest.raises(ValueError, match='无法解析'):
        unbind(tmp_path)
    assert path.read_bytes() == before


def test_failed_multi_file_removal_restores_even_a_previously_deleted_xml(tmp_path, monkeypatch):
    from resources.lib import voice_binding as module
    first = bind(tmp_path, 61588)
    last = tmp_path / 'z.xml'
    last.write_text('<keymap><global><keyboard><f5>' + ACTION + '</f5><left>Left</left></keyboard></global></keymap>')
    original = {path: path.read_bytes() for path in (first, last)}
    replace = module._replace
    def fail(path, data, mode):
        if path == last:
            raise OSError('fixture write failure')
        return replace(path, data, mode)
    monkeypatch.setattr(module, '_replace', fail)
    with pytest.raises(OSError, match='fixture'):
        unbind(tmp_path)
    assert all(path.read_bytes() == data for path, data in original.items())


def test_remove_updates_status_without_requiring_a_previous_binding_record(tmp_path):
    keys, profile = tmp_path / 'keys', tmp_path / 'profile'
    path = bind(keys, 61588)
    assert remove_binding(profile, keys) == [path]
    status = binding_status(profile)
    assert status['state'] == 'removed' and status['files'] == [path.name]
    assert 'key' not in status and 'target' not in status
    assert remove_binding(profile, keys) == []


def test_removal_cannot_run_while_waiting_for_a_key(tmp_path):
    from resources.lib.voice_binding import begin_binding
    keys, profile = tmp_path / 'keys', tmp_path / 'profile'
    path = bind(keys, 61588)
    begin_binding(profile)
    with pytest.raises(ValueError, match='正在等待'):
        remove_binding(profile, keys)
    assert path.exists()


def test_native_remove_action_reloads_keymaps_and_notifies_without_sound(tmp_path, monkeypatch):
    keys, profile = tmp_path / 'keys', tmp_path / 'profile'
    bound = bind(keys, 61588)
    calls, notifications = [], []
    monkeypatch.setitem(sys.modules, 'xbmc', SimpleNamespace(executebuiltin=calls.append))
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace(WindowXMLDialog=object,
        Dialog=lambda: SimpleNamespace(notification=lambda *args, **kwargs: notifications.append((args, kwargs)))))
    monkeypatch.setitem(sys.modules, 'xbmcvfs', SimpleNamespace(translatePath=lambda path: path))
    from resources.lib import voice_binding
    monkeypatch.setattr(voice_binding, 'keymap_directory', lambda: keys)
    path = Path(__file__).resolve().parents[1] / 'resources/lib/ui/key_binding.py'
    spec = importlib.util.spec_from_file_location('resources.lib.ui.remove_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.remove(SimpleNamespace(getAddonInfo=lambda name: str(profile)))
    assert not bound.exists() and calls == ['Action(ReloadKeymaps)']
    assert notifications[0][1]['sound'] is False


def test_native_settings_expose_binding_and_removal_together():
    root = Path(__file__).resolve().parents[1]
    settings = ET.parse(root / 'resources/settings.xml')
    group = settings.find('.//setting[@id="unbind_voice_key"]/..')
    assert group.find('setting[@id="bind_voice_key"]') is not None
    assert group.find('setting[@id="unbind_voice_key"]/data').text == 'RunScript(script.voice.assistant,--unbind-voice-key)'
