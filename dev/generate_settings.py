"""Generate Kodi settings and labels from the same contract used by the web UI."""
from pathlib import Path
import json
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from resources.lib.settings import FIELDS, PREFIX


def build():
    labels = {}
    # Keep existing localization IDs when options are removed, so Kodi's cached
    # labels do not suddenly refer to a different setting after a hot update.
    existing = {}
    original = (ROOT / 'resources/language/resource.language.zh_cn/strings.po').read_text(encoding='utf-8')
    for match in re.finditer(r'msgctxt "#(\d+)"\nmsgid ("[^\n]*")', original):
        identifier = int(match[1])
        if identifier >= 31000:
            existing.setdefault(json.loads(match[2]), []).append(identifier)
    next_identifier = max((value for values in existing.values() for value in values), default=30999) + 1
    def label(text):
        nonlocal next_identifier
        available = existing.get(text, [])
        if available:
            identifier = available.pop(0)
        else:
            identifier = next_identifier
            next_identifier += 1
        labels[identifier] = text
        return str(identifier)
    root = ET.Element('settings', version='1')
    section = ET.SubElement(root, 'section', id='script.voice.assistant')
    groups = {}
    for index, (key, title) in enumerate((('configuration', '扫码配置与测试'),
                                        ('input', '音频输入设备'), ('asr', '语音转文字'),
                                        ('agent', '智能体'), ('speech', '语音合成'))):
        category = ET.SubElement(section, 'category', id=key, label=label(title))
        groups[key] = ET.SubElement(category, 'group', id=str(index + 1))
    for name, field in FIELDS.items():
        setting = ET.SubElement(groups[field.section], 'setting', id=PREFIX + name,
            type='path' if name in ('doubao_key_file', 'agent_key_file') else
                 ('integer' if type(field.default) is int else 'string'), label=label(field.label))
        ET.SubElement(setting, 'level').text = '0'
        ET.SubElement(setting, 'default').text = str(field.default)
        constraints = ET.SubElement(setting, 'constraints')
        hidden = name in ('recognition_engine', 'codex_chatgpt_model', 'codex_chatgpt_effort')
        if hidden:
            ET.SubElement(setting, 'visible').text = 'false'
        if field.options:
            options = ET.SubElement(constraints, 'options')
            for value, text in field.options:
                ET.SubElement(options, 'option', label=label(text)).text = value
            ET.SubElement(setting, 'control', type='spinner', format='string')
        elif type(field.default) is int:
            ET.SubElement(constraints, 'minimum').text = str(field.minimum)
            ET.SubElement(constraints, 'maximum').text = str(field.maximum)
            ET.SubElement(constraints, 'step').text = '100' if name == 'silence_ms' else '1'
            ET.SubElement(setting, 'control', type='spinner', format='integer')
        else:
            ET.SubElement(constraints, 'allowempty').text = 'true'
            if name in ('doubao_key_file', 'agent_key_file'):
                ET.SubElement(ET.SubElement(setting, 'control', type='button', format='file'), 'heading').text = setting.get('label')
            else:
                control = ET.SubElement(setting, 'control', type='edit', format='string')
                if field.secret:
                    ET.SubElement(control, 'hidden').text = 'true'
        if field.visible and not hidden:
            dependency = ET.SubElement(ET.SubElement(setting, 'dependencies'), 'dependency', type='visible')
            rules = field.visible.split('&')
            parent = ET.SubElement(dependency, 'and') if len(rules) > 1 else dependency
            for rule in rules:
                operator = '!=' if '!=' in rule else '='
                other, value = rule.split(operator)
                attributes = {'setting': PREFIX + other, 'operator': 'is'}
                if operator == '!=':
                    attributes['negated'] = 'true'
                values = value.split(',')
                alternatives = ET.SubElement(parent, 'and' if operator == '!=' else 'or') if len(values) > 1 else parent
                for item in values:
                    ET.SubElement(alternatives, 'condition', **attributes).text = item
    actions = (
        ('asr', 'select_asr', '选择语音转文字', '--select-asr'),
        ('agent', 'select_codex_model', '选择订阅模型', '--select-codex-model'),
        ('agent', 'select_codex_effort', '选择订阅思考强度', '--select-codex-effort'),
        ('agent', 'codex_login', '登录 Codex', '--codex-login'),
        ('agent', 'codex_account', '查看订阅账号', '--codex-account'),
        ('agent', 'codex_logout', '退出订阅登录', '--codex-logout'),
        ('agent', 'codebuddy_configure', '配置 / 登录 CodeBuddy', '--configure'),
        ('agent', 'clear_agent_context', '清空对话 session', '--clear-agent-context'),
        ('configuration', 'configure', '扫码配置语音助手', '--configure'),
        ('configuration', 'dependencies', '下载 / 查看所需组件', '--dependencies'),
        ('input', 'bind_voice_key', '绑定语音按键', '--bind-voice-key'))
    for group, name, text, argument in actions:
        setting = ET.SubElement(groups[group], 'setting', id=name, type='action', label=label(text))
        if name == 'bind_voice_key':
            groups[group].remove(setting)
            groups[group].insert(0, setting)
        ET.SubElement(setting, 'level').text = '0'
        if name == 'codebuddy_configure' or name.startswith('select_codex_'):
            dependency = ET.SubElement(ET.SubElement(setting, 'dependencies'), 'dependency', type='visible')
            ET.SubElement(dependency, 'condition', setting=PREFIX + 'agent_backend', operator='is').text = (
                'codebuddy' if name == 'codebuddy_configure' else 'codex')
        ET.SubElement(setting, 'data').text = 'RunScript(script.voice.assistant,' + argument + ')'
        ET.SubElement(ET.SubElement(setting, 'control', type='button', format='action'), 'close').text = 'true'
    ET.indent(root)
    ET.ElementTree(root).write(ROOT / 'resources/settings.xml', encoding='UTF-8', xml_declaration=True)
    for language in ('zh_cn', 'en_gb'):
        path = ROOT / ('resources/language/resource.language.' + language + '/strings.po')
        original = path.read_text(encoding='utf-8')
        original = original.split('msgctxt "#31000"')[0].rstrip()
        lines = [original, '']
        for identifier, text in labels.items():
            lines += ['msgctxt "#' + str(identifier) + '"', 'msgid ' + json.dumps(text, ensure_ascii=False),
                      'msgstr ' + json.dumps(text, ensure_ascii=False), '']
        path.write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    build()
