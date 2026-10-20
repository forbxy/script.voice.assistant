"""Search switches govern routing and agent tools independently of source order."""
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence import command_media, emby, emby_client, juku
from resources.lib.intelligence.commands import Commands
from resources.lib.intelligence.media_order import sources, instruction
from resources.lib.intelligence.media_results import SearchResult
from resources.lib.intelligence.runtime import create_tools
from resources.lib.intelligence.sessions import signature
from resources.lib.settings import Settings, web_schema


@pytest.mark.parametrize('source', ['emby', 'juku'])
def test_defaults_settings_roundtrip_and_kodi_web_schema(source):
    stored = {}
    config = Settings(SimpleNamespace(getSetting=lambda key:stored.get(key, ''),
        setSetting=lambda key, value:stored.update({key:value})))
    key = 'media_search_' + source
    assert config.get(key) == web_schema()[key]['default'] == 'on'
    assert web_schema()[key]['section'] == 'agent' and not web_schema()[key]['visible']
    config.apply({key:'off'})
    assert stored['v2_' + key] == 'off' and config.get(key) == 'off'
    other = 'media_search_' + ('juku' if source == 'emby' else 'emby')
    assert config.get(other) == 'on'
    root = ET.parse(Path(__file__).resolve().parents[1]/'resources/settings.xml')
    field = root.find('.//category[@id="agent"]//setting[@id="v2_' + key + '"]')
    assert field.findtext('default') == 'on' and field.find('dependencies') is None
    assert {row.text for row in field.findall('constraints/options/option')} == {'on','off'}


@pytest.mark.parametrize('source,module', [('emby',emby_client), ('juku',juku)])
def test_disabled_source_does_not_read_accounts_files_or_query_kodi(monkeypatch, source, module):
    def forbidden(*args, **kwargs):pytest.fail('Disabled source was accessed')
    monkeypatch.setattr(module.Path, 'is_file', forbidden)
    tools = KodiTools(forbidden, **{'media_search_'+source:False})
    assert not module.available(tools)


@pytest.mark.parametrize('emby_on,juku_on', [(True,True),(True,False),(False,True),(False,False)])
@pytest.mark.parametrize('order', ['emby_juku','juku_emby'])
def test_disabled_sources_are_never_constructed_or_searched(monkeypatch, emby_on, juku_on, order):
    calls = []
    tools = KodiTools(lambda *args:pytest.fail('Unexpected Kodi request'),
        media_search_emby=emby_on, media_search_juku=juku_on, media_search_order=order)
    def local(*args):calls.append('local');return SearchResult(source='local')
    monkeypatch.setattr(command_media, 'search', local)
    class Emby:
        enabled = True
        def __init__(self, *args):assert emby_on
        def search(self, request):calls.append('emby');return SearchResult(source='emby')
    class Juku:
        enabled = True
        def __init__(self, *args):assert juku_on
        def search(self, source, request):calls.append(source);return SearchResult(source=source)
    monkeypatch.setattr(emby, 'Source', Emby)
    monkeypatch.setattr(juku, 'Source', Juku)
    assert Commands(tools).process('播放测试片名') is None
    expected = []
    for name in order.split('_'):
        if {'emby':emby_on,'juku':juku_on}[name]:expected += [name] if name=='emby' else ['private','online']
    assert calls == ['local'] + expected
    assert sources(tools) == tuple(name for name in order.split('_') if {'emby':emby_on,'juku':juku_on}[name])
    heading = instruction(tools).split('。')[0]
    assert ('Emby 动态库' in heading) is emby_on and ('巨库' in heading) is juku_on
    if not (emby_on or juku_on):
        assert heading == '\n当前媒体搜索顺序：Kodi 本地媒体库'


@pytest.mark.parametrize('backend', ['off','codex','codebuddy'])
@pytest.mark.parametrize('emby_on,juku_on', [(True,True),(True,False),(False,True),(False,False)])
def test_all_backends_get_only_enabled_source_tools(monkeypatch, backend, emby_on, juku_on):
    checks = []
    monkeypatch.setattr(emby, 'available', lambda tools:checks.append('emby') or True)
    monkeypatch.setattr(juku, 'available', lambda tools:checks.append('juku') or True)
    values = Settings(SimpleNamespace(getSetting=lambda key:'')).values()
    values.update(agent_backend=backend, media_search_emby='on' if emby_on else 'off',
                  media_search_juku='on' if juku_on else 'off')
    tools = create_tools(None, values, lambda *args:pytest.fail('Unexpected Kodi call'))
    names = {row['function']['name'] for row in tools.definitions()}
    for source, enabled in [('emby',emby_on),('juku',juku_on)]:
        assert ('search_'+source in names) is enabled
        assert ('play_'+source in names) is enabled
        assert (source in checks) is enabled
        if not enabled:assert not tools.execute('search_'+source, {'query':'测试'})['ok']
    assert 'search_media' in names and 'play_media' in names


def test_cached_available_tools_do_not_override_disabled_switch(monkeypatch):
    monkeypatch.setattr(emby, 'available', lambda tools:True)
    monkeypatch.setattr(juku, 'available', lambda tools:True)
    tools = KodiTools(lambda *args:pytest.fail('Unexpected source request'))
    assert {'search_emby','search_juku'} <= {row['function']['name'] for row in tools.definitions()}
    tools.media_search_emby = tools.media_search_juku = False
    assert not tools.execute('search_emby', {'query':'测试'})['ok']
    assert not tools.execute('search_juku', {'query':'测试'})['ok']


def test_native_thread_catalogue_is_renewed_when_a_source_switch_changes():
    defaults = {'agent_backend':'codex', 'media_search_emby':'on', 'media_search_juku':'on'}
    assert signature(defaults) == signature({'agent_backend':'codex'})
    assert signature(defaults) != signature(dict(defaults, media_search_emby='off'))
    assert signature(defaults) != signature(dict(defaults, media_search_juku='off'))
