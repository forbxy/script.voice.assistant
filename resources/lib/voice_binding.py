"""Learned key binding, following controller.switcher's numeric Kodi key format."""
from .data_paths import data_path
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

ACTION = 'RunScript(script.voice.assistant,--voice)'
LONG_PRESS = 0x01000000


def binding_status(profile):
    import time
    from .storage import read_json
    value = read_json(data_path(Path(profile), 'voice-binding.json'), {})
    if value.get('state') in ('requested', 'listening') and time.time() - value.get('updated_at', 0) > 30:
        return {'state': 'cancelled', 'error': '绑定等待已结束，请重试。'}
    return value


def begin_binding(profile):
    import secrets
    if binding_status(profile).get('state') in ('requested', 'listening'):
        raise ValueError('正在等待遥控按键，请在电视上完成绑定。')
    identifier = secrets.token_hex(16)
    update_binding(profile, identifier, 'requested', initial=True)
    return identifier


def update_binding(profile, identifier, state, initial=False, **details):
    import time
    from .storage import write_json
    if not initial and binding_status(profile).get('id') != identifier:
        raise ValueError('绑定请求已过期，请重新开始。')
    write_json(data_path(Path(profile), 'voice-binding.json'), dict(details, id=identifier, state=state, updated_at=time.time()))


def bound_codes(directory):
    """Resolve global voice bindings without letting window-local Back override them."""
    names = json.loads((Path(__file__).resolve().parents[1] / 'config/keyboard-mapping.json').read_text(encoding='utf-8'))
    bindings = {}
    paths = (p for p in Path(directory).glob('*') if p.is_file() and p.suffix.lower() == '.xml')
    for path in sorted(paths, key=lambda p: (p.name.casefold(), p.name)):
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        for keyboard in root.findall('global/keyboard'):
            for key in keyboard:
                code = key.get('id') if key.tag.lower() == 'key' else names.get(key.tag.lower())
                mod = key.get('mod', '').lower()
                if code is not None and mod in ('', 'longpress'):
                    bindings[(str(code), mod)] = (key.text or '').strip()
    return {code for (code, mod), action in bindings.items() if action == ACTION}


def current_codes():
    return bound_codes(keymap_directory())


def keymap_directory():
    import xbmcvfs
    return xbmcvfs.translatePath('special://userdata/keymaps/')


def is_voice_key(action, codes):
    return button_code(getattr(action, 'getButtonCode', lambda: 0)()) in codes


def button_code(value):
    value = int(value)
    if not 0 < value <= 0xffffffff:
        return None
    code = value & ~LONG_PRESS
    return str(code) if code else None


def target_file(directory):
    directory = Path(directory)
    files = sorted((p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == '.xml'),
                   key=lambda p: (p.name.casefold(), p.name)) if directory.exists() else []
    return files[-1] if files else directory / 'voice-assistant.xml'


def _replace(path, data, mode):
    import tempfile
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as output:
        temporary = Path(output.name)
        try:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def bind(directory, code):
    code = button_code(code)
    if code is None:
        raise ValueError('没有收到有效的按键。')
    target = target_file(directory)
    original = target.read_bytes() if target.exists() else None
    try:
        root = ET.fromstring(original, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))) if original is not None else ET.Element('keymap')
    except ET.ParseError:
        raise ValueError('按字母排序最后的 XML 无法解析：' + target.name) from None
    if root.tag != 'keymap':
        raise ValueError('目标 XML 的根节点不是 keymap：' + target.name)
    names = json.loads((Path(__file__).resolve().parents[1] / 'config/keyboard-mapping.json').read_text(encoding='utf-8'))
    global_nodes = root.findall('global')
    if not global_nodes:
        global_nodes = [ET.SubElement(root, 'global')]
    # Update only this key in global; preserve all unrelated devices/windows.
    for context in global_nodes:
        for keyboard in context.findall('keyboard'):
            for key in list(keyboard):
                if not isinstance(key.tag, str):
                    continue
                identifier = key.get('id') if key.tag.lower() == 'key' else names.get(key.tag.lower())
                if str(identifier) == code and key.get('mod', '').lower() in ('', 'longpress'):
                    keyboard.remove(key)
    context = global_nodes[-1]
    keyboards = context.findall('keyboard')
    keyboard = keyboards[-1] if keyboards else ET.SubElement(context, 'keyboard')
    # Kodi suppresses hold autorepeat only with an explicit longpress mapping.
    # Both gestures launch once; no repeated interpreter creation while held.
    for mod in ('', 'longpress'):
        attributes = {'id': code}
        if mod:
            attributes['mod'] = mod
        ET.SubElement(keyboard, 'key', attributes).text = ACTION
    ET.indent(root, space='    ')
    data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    if data == original:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target_file(directory) != target or (target.read_bytes() if target.exists() else None) != original:
        raise ValueError('按键文件已发生变化，请重新绑定。')
    mode = target.stat().st_mode & 0o777 if original is not None else 0o644
    if original is not None:
        _replace(target.with_suffix(target.suffix + '.voice-assistant.bak'), original, mode)
    _replace(target, data, mode)
    return target


def unbind(directory):
    """Remove voice-entry mappings only; delete XMLs emptied by that removal."""
    paths = sorted((p for p in Path(directory).glob('*') if p.is_file() and p.suffix.lower() == '.xml'),
                   key=lambda p: (p.name.casefold(), p.name))
    changes = []
    for path in paths:
        original = path.read_bytes()
        try:
            root = ET.fromstring(original, parser=ET.XMLParser(
                target=ET.TreeBuilder(insert_comments=True, insert_pis=True)))
        except ET.ParseError:
            raise ValueError('按键 XML 无法解析：' + path.name) from None
        if root.tag != 'keymap':
            continue
        removed = False
        for parent in root.iter():
            for key in list(parent):
                if isinstance(key.tag, str) and not len(key) and (key.text or '').strip() == ACTION:
                    parent.remove(key)
                    removed = True
        if not removed:
            continue
        # Window / device / key hierarchy. Empty wrappers and comments are not
        # bindings; an explicit empty key is still someone else's mapping.
        remaining = any(isinstance(key.tag, str) for context in root for device in context for key in device)
        ET.indent(root, space='    ')
        data = ET.tostring(root, encoding='utf-8', xml_declaration=True) if remaining else None
        changes.append((path, original, data, path.stat().st_mode & 0o777))
    for path, original, data, mode in changes:
        if path.read_bytes() != original:
            raise ValueError('按键文件已发生变化，请重试。')
        _replace(path.with_suffix(path.suffix + '.voice-assistant-unbind.bak'), original, mode)
    written = []
    try:
        for path, original, data, mode in changes:
            if path.read_bytes() != original:
                raise ValueError('按键文件已发生变化，请重试。')
            if data is None:
                path.unlink()
            else:
                _replace(path, data, mode)
            written.append((path, original, data, mode))
    except Exception:
        for path, original, data, mode in reversed(written):
            if ((data is None and not path.exists()) or
                    (data is not None and path.exists() and path.read_bytes() == data)):
                _replace(path, original, mode)
        raise
    return [path for path, original, data, mode in changes]


def remove_binding(profile, directory=None):
    import secrets
    status = binding_status(profile)
    if status.get('state') in ('requested', 'listening'):
        raise ValueError('正在等待遥控按键，请先完成绑定或等待取消。')
    paths = unbind(keymap_directory() if directory is None else directory)
    update_binding(profile, secrets.token_hex(16), 'removed', initial=True,
                   files=[path.name for path in paths])
    return paths
