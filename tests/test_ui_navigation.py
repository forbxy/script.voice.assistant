import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from resources.lib.agent_tools import KodiTools
from resources.lib.intelligence.permissions import Permissions


def test_addon_search_finds_real_ids_in_media_tier_and_preserves_enabled_state():
    rows = [
        {'addonid': 'plugin.hongguo.ce', 'name': '红果短剧 · CE', 'type': 'xbmc.python.script', 'enabled': True},
        {'addonid': 'plugin.video.other', 'name': '红果旧版', 'type': 'xbmc.python.pluginsource', 'enabled': False},
        {'addonid': 'script.module.hongguo', 'name': '红果依赖', 'type': 'xbmc.python.module', 'enabled': True},
    ]
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        return {'addons': rows}
    tools = KodiTools(rpc, permissions=Permissions('media'))
    result = tools.execute('search_addons', {'query': '红果', 'runnable_only': True})
    assert result['ok'] and result['total'] == 2
    assert {row['addonid']: row['enabled'] for row in result['addons']} == {
        'plugin.hongguo.ce': True, 'plugin.video.other': False}
    assert calls == [('Addons.GetAddons', {'properties': ['name', 'enabled']})]
    expected = dict(rows[0], can_open=True)
    assert tools.execute('search_addons', {'query': 'PLUGIN.HONGGUO.CE', 'runnable_only': True})['addons'] == [expected]
    assert tools.execute('search_addons', {'query': '红果 短剧', 'runnable_only': True})['addons'] == [expected]
    assert tools.execute('search_addons', {'query': '不存在'})['total'] == 0
    first = tools.execute('search_addons', {'query': '', 'limit': 1, 'runnable_only': True})
    second = tools.execute('search_addons', {'query': '', 'limit': 1, 'offset': first['next_offset'], 'runnable_only': True})
    assert first['total'] == 2 and second['next_offset'] is None
    assert first['addons'] + second['addons'] == result['addons']
    assert not tools.execute('search_addons', {'query': '', 'offset': -1})['ok']


def test_open_addon_is_validated_and_queued_without_claiming_it_opened():
    calls = []
    tools = KodiTools(lambda *args: {'addon': {'enabled': True, 'type': 'xbmc.python.script'}}, calls.append, defer_ui=True)
    result = tools.execute('open_addon', {'addonid': 'plugin.hongguo.ce'})
    assert result['ok'] and result['deferred'] == 'ui_navigation' and 'opened' not in result
    assert tools.ui_action == 'RunAddon(plugin.hongguo.ce)' and not calls
    assert not tools.execute('open_addon', {'addonid': 'evil),Reboot('})['ok']
    tools = KodiTools(lambda *args: {'addon': {'enabled': False}}, calls.append, defer_ui=True)
    assert not tools.execute('open_addon', {'addonid': 'plugin.missing'})['ok']
    assert tools.ui_action is None and not calls


def test_plugin_info_finds_inputstream_in_one_read_with_manifest_details():
    calls = []
    row = {'addonid': 'inputstream.rtmp', 'name': '[B]RTMP Input[/B]', 'type': 'kodi.inputstream',
           'enabled': True, 'version': '21.1.2.1', 'author': 'spiff', 'summary': 'RTMP 流的客户端',
           'description': '实现 RTMP 流支持。[CR][CR]由播放器调用。'}
    def rpc(method, params):
        calls.append((method, params))
        return {'addons': [row]}
    tools = KodiTools(rpc, permissions=Permissions('media'), defer_ui=True)
    result = tools.execute('search_addons', {'query': 'RTMP Input'})
    assert result['ok'] and result['total'] == 1
    addon = result['addons'][0]
    assert addon['addonid'] == 'inputstream.rtmp' and addon['type'] == 'kodi.inputstream'
    assert addon['version'] == row['version'] and addon['author'] == 'spiff'
    assert addon['summary'] == row['summary'] and addon['description'] == '实现 RTMP 流支持。 由播放器调用。'
    assert addon['name'] == 'RTMP Input' and addon['can_open'] is False
    assert calls == [('Addons.GetAddons', {'properties': [
        'name', 'enabled', 'version', 'summary', 'description', 'author']})]
    assert tools.ui_action is None and not tools.acted


def test_all_types_and_disabled_components_are_included_in_info_pagination():
    rows = [
        {'addonid': 'module.a', 'name': 'A', 'type': 'xbmc.python.module', 'enabled': True},
        {'addonid': 'plugin.b', 'name': 'B', 'type': 'xbmc.python.script', 'enabled': False},
        {'addonid': 'plugin.c', 'name': 'C', 'type': 'xbmc.python.pluginsource', 'enabled': True},
    ]
    tools = KodiTools(lambda *args: {'addons': rows})
    first = tools.execute('search_addons', {'query': '', 'limit': 2})
    second = tools.execute('search_addons', {'query': '', 'limit': 2, 'offset': first['next_offset']})
    assert first['total'] == second['total'] == 3 and second['next_offset'] is None
    result = first['addons'] + second['addons']
    assert [row['addonid'] for row in result] == ['module.a', 'plugin.b', 'plugin.c']
    assert [row['can_open'] for row in result] == [False, False, True]
    assert tools.execute('search_addons', {'query': 'MODULE.A'})['addons'][0]['addonid'] == 'module.a'


@pytest.mark.parametrize('kind', ['kodi.inputstream', 'kodi.pvrclient', 'xbmc.python.module', 'xbmc.service', 'xbmc.gui.skin', None])
def test_readable_background_component_cannot_be_launched(kind):
    launched = []
    tools = KodiTools(lambda *args: {'addon': {'enabled': True, 'type': kind}}, launched.append, defer_ui=True)
    result = tools.execute('open_addon', {'addonid': 'component.example'})
    assert not result['ok'] and '不能直接打开' in result['error']
    assert tools.ui_action is None and not tools.acted and not launched


def test_only_last_valid_navigation_runs_and_fullscreen_requires_video():
    calls = []
    tools = KodiTools(lambda *args: [], calls.append, defer_ui=True)
    assert tools.execute('navigate', {'destination': 'movies'})['ok']
    assert tools.execute('navigate', {'destination': 'home'})['ok']
    assert tools.ui_action == 'ActivateWindow(Home)' and not calls
    assert not tools.execute('navigate', {'destination': 'fullscreen'})['ok']


@pytest.mark.parametrize('cancel', [False, True])
def test_handoff_waits_for_modal_to_disappear_and_cancellation_prevents_launch(monkeypatch, cancel):
    order = []
    current = [13001]
    cancelled = threading.Event()
    def wait(seconds):
        order.append('gui-tick')
        current[0] = 9999
        if cancel:
            cancelled.set()
        return False
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace(getCurrentWindowDialogId=lambda: current[0]))
    path = Path(__file__).resolve().parents[1] / 'resources/lib/ui/navigation.py'
    spec = importlib.util.spec_from_file_location('navigation_test', path)
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    window = SimpleNamespace(cancelled=cancelled, close=lambda: order.append('close'))
    monitor = SimpleNamespace(abortRequested=lambda: False, waitForAbort=wait)
    result = module.handoff(window, 'RunAddon(plugin.hongguo.ce)', monitor, order.append)
    assert order[:2] == ['close', 'gui-tick']
    assert result is not cancel
    assert ('RunAddon(plugin.hongguo.ce)' in order) is not cancel


def test_handoff_sends_structured_rpc_only_after_own_modal_closes(monkeypatch):
    current, order = [13001], []
    def wait(seconds):
        current[0] = 10103
        order.append('gui-tick')
        return False
    monkeypatch.setitem(sys.modules, 'xbmcgui', SimpleNamespace(getCurrentWindowDialogId=lambda: current[0]))
    spec = importlib.util.spec_from_file_location('resources.lib.ui.navigation_structured_test',
        Path(__file__).resolve().parents[1] / 'resources/lib/ui/navigation.py')
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    def rpc(method, params):
        assert current[0] == 10103
        order.append((method, params))
        if method == 'GUI.GetProperties':return {'currentwindow': {'id': current[0]}}
        if method == 'XBMC.GetInfoBooleans':return {'System.HasHiddenInput': False}
    action = {'window_id': 10103, 'operations': [{'method': 'Input.SendText', 'params': {'text': '三体', 'done': False}}]}
    window = SimpleNamespace(cancelled=threading.Event(), close=lambda: order.append('close'))
    monitor = SimpleNamespace(abortRequested=lambda: False, waitForAbort=wait)
    assert module.handoff(window, action, monitor, lambda value: pytest.fail('Unexpected builtin'), rpc)
    assert order[:2] == ['close', 'gui-tick']
    assert order[-1] == ('Input.SendText', {'text': '三体', 'done': False})
